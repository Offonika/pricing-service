from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = REPO_ROOT / "scripts/build_pricing_service_release.sh"
SWITCH_SCRIPT = REPO_ROOT / "scripts/switch_pricing_service_release.sh"
CONTENT_HASH_SCHEME = "sha256-files-v2-no-python-cache"
BASE_COMMIT = "1" * 40
CANDIDATE_COMMIT = "2" * 40


def _run(command: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def _release_content_sha256(release: Path) -> str:
    result = _run(
        [
            "bash",
            "-c",
            """
set -euo pipefail
cd "$1"
find . -type f \
  ! -path './release-manifest.json' \
  ! -path './.release-verified' \
  ! -path '*/__pycache__/*' \
  ! -name '*.pyc' \
  ! -name '*.pyo' \
  -print0 | sort -z | xargs -0 sha256sum | sha256sum | awk '{print $1}'
""",
            "bash",
            str(release),
        ]
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _create_source_repository(tmp_path: Path) -> tuple[Path, str]:
    source = tmp_path / "source"
    source.mkdir()
    (source / "ui/dist").mkdir(parents=True)
    (source / "ui/dist/index.html").write_text("<html>release</html>", encoding="utf-8")
    (source / "alembic/versions").mkdir(parents=True)
    (source / "alembic/versions/r1.py").write_text(
        'revision = "r1"\ndown_revision = None\n',
        encoding="utf-8",
    )
    (source / "requirements.lock").write_text("", encoding="utf-8")
    (source / ".env").write_text("APP_NAME=test\n", encoding="utf-8")
    (source / ".gitignore").write_text(
        ".local/\n.artifacts/\nbuild/\ndata/\nreports/\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(
        ["git", "-C", str(source), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(["git", "-C", str(source), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "fixture"], check=True)
    commit = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    return source, commit


def _builder_env(
    tmp_path: Path,
    source: Path,
    *,
    required_base_ref: str | None,
    mutable_root: Path | None,
) -> dict[str, str]:
    env = {
        **os.environ,
        "PRICING_SERVICE_SOURCE_ROOT": str(source),
        "PRICING_SERVICE_RELEASE_ROOT": str(tmp_path / "releases"),
        "PRICING_SERVICE_BUILD_UI": "0",
    }
    if required_base_ref is not None:
        env["PRICING_SERVICE_RELEASE_REQUIRED_BASE_REF"] = required_base_ref
    else:
        env.pop("PRICING_SERVICE_RELEASE_REQUIRED_BASE_REF", None)
    if mutable_root is not None:
        mutable_root.mkdir(exist_ok=True)
        env["PRICING_SERVICE_MUTABLE_ROOT"] = str(mutable_root)
    else:
        env.pop("PRICING_SERVICE_MUTABLE_ROOT", None)
    return env


def _write_previous_release(
    path: Path,
    *,
    source_commit: str = BASE_COMMIT,
) -> None:
    path.mkdir()
    (path / "release-manifest.json").write_text(
        json.dumps({"source_commit": source_commit}),
        encoding="utf-8",
    )
    (path / "openapi.yaml").write_text("paths: {}\n", encoding="utf-8")


def _create_mutable_root(path: Path) -> Path:
    path.mkdir()
    for name in (".local", ".artifacts", "build", "data", "reports"):
        (path / name).mkdir()
    return path


def _write_candidate_release(
    path: Path,
    mutable_root: Path,
    *,
    source_commit: str = CANDIDATE_COMMIT,
    required_base_commit: str = BASE_COMMIT,
    source_verified: bool = True,
    source_dirty: bool = False,
    content_hash_scheme: str = CONTENT_HASH_SCHEME,
    fake_python_body: str = "#!/usr/bin/env bash\nexit 0\n",
) -> None:
    (path / ".venv/bin").mkdir(parents=True)
    (path / "ui/dist").mkdir(parents=True)
    (path / "scripts").mkdir()
    fake_python = path / ".venv/bin/python"
    fake_python.write_text(fake_python_body, encoding="utf-8")
    fake_python.chmod(0o755)
    (path / "ui/dist/index.html").write_text("candidate", encoding="utf-8")
    (path / "requirements.lock").write_text("", encoding="utf-8")
    for name in (".local", ".artifacts", "build", "data", "reports"):
        (path / name).symlink_to(mutable_root / name, target_is_directory=True)
    content_sha256 = _release_content_sha256(path)
    (path / "release-manifest.json").write_text(
        json.dumps(
            {
                "source_verified": source_verified,
                "source_commit": source_commit,
                "required_base_ref": BASE_COMMIT,
                "required_base_commit": required_base_commit,
                "mutable_root": str(mutable_root),
                "source_dirty": source_dirty,
                "content_hash_scheme": content_hash_scheme,
                "content_sha256": content_sha256,
            }
        ),
        encoding="utf-8",
    )


def _write_fake_systemctl(path: Path, *, log: Path | None = None) -> Path:
    body = "#!/usr/bin/env bash\n"
    if log is not None:
        body += 'printf \'%s\\n\' "$*" >> "$SYSTEMCTL_LOG"\n'
    body += "exit 0\n"
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _switch_env(
    tmp_path: Path,
    *,
    active_link: Path,
    expected_active: Path,
    mutable_root: Path,
    systemctl: Path,
) -> dict[str, str]:
    return {
        **os.environ,
        "PRICING_SERVICE_ACTIVE_LINK": str(active_link),
        "PRICING_SERVICE_EXPECTED_ACTIVE_RELEASE": str(expected_active),
        "PRICING_SERVICE_MUTABLE_ROOT": str(mutable_root),
        "PRICING_SERVICE_SWITCH_LOCK_FILE": str(tmp_path / "switch.lock"),
        "PRICING_SERVICE_RELEASE_AUDIT_LOG": str(tmp_path / "switch-audit.jsonl"),
        "PRICING_SERVICE_SYSTEMCTL_BIN": str(systemctl),
        "PRICING_SERVICE_SYSTEMD_PREFLIGHT": "0",
        "PRICING_SERVICE_NGINX_PREFLIGHT": "0",
    }


def _audit_events(path: Path) -> list[str]:
    return [json.loads(line)["event"] for line in path.read_text(encoding="utf-8").splitlines()]


def test_release_builder_creates_locked_release_specific_runtime(tmp_path: Path) -> None:
    source, source_commit = _create_source_repository(tmp_path)
    mutable_root = tmp_path / "runtime"
    env = _builder_env(
        tmp_path,
        source,
        required_base_ref=source_commit,
        mutable_root=mutable_root,
    )

    result = _run([str(BUILD_SCRIPT), "test-release"], env=env)

    assert result.returncode == 0, result.stderr
    release = Path(result.stdout.strip())
    manifest = json.loads((release / "release-manifest.json").read_text(encoding="utf-8"))
    assert manifest["source_verified"] is True
    assert manifest["source_commit"] == source_commit
    assert manifest["required_base_ref"] == source_commit
    assert manifest["required_base_commit"] == source_commit
    assert manifest["mutable_root"] == str(mutable_root)
    assert manifest["source_dirty"] is False
    assert manifest["runtime_env_file"] == str(source / ".env")
    assert manifest["alembic_revision"] == "r1"
    assert manifest["requirements_lock_sha256"] == hashlib.sha256(b"").hexdigest()
    assert len(manifest["pip_freeze_sha256"]) == 64
    assert len(manifest["ui_asset_sha256"]) == 64
    assert manifest["content_hash_scheme"] == CONTENT_HASH_SCHEME
    assert _release_content_sha256(release) == manifest["content_sha256"]
    runtime_cache = release / "app/__pycache__/runtime.pyc"
    runtime_cache.parent.mkdir(parents=True)
    runtime_cache.write_bytes(b"runtime cache")
    assert _release_content_sha256(release) == manifest["content_sha256"]
    assert (release / ".venv/bin/python").exists()
    assert (release / ".venv/bin/pip").read_text(encoding="utf-8").splitlines()[0] == (
        f"#!{release}/.venv/bin/python"
    )
    assert (release / ".env").resolve() == source / ".env"
    assert (release / "build").resolve() == mutable_root / "build"
    assert (release / "data").resolve() == mutable_root / "data"
    assert (release / "ui/dist/index.html").stat().st_mode & 0o222 == 0


def test_release_builder_requires_production_base_ref(tmp_path: Path) -> None:
    source, _ = _create_source_repository(tmp_path)
    env = _builder_env(
        tmp_path,
        source,
        required_base_ref=None,
        mutable_root=tmp_path / "runtime",
    )

    result = _run([str(BUILD_SCRIPT), "test-release"], env=env)

    assert result.returncode == 2
    assert "PRICING_SERVICE_RELEASE_REQUIRED_BASE_REF is required" in result.stderr


def test_release_builder_rejects_stale_candidate(tmp_path: Path) -> None:
    source, candidate_commit = _create_source_repository(tmp_path)
    (source / "production.txt").write_text("new production", encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "add", "production.txt"], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "production"], check=True)
    production_commit = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    subprocess.run(["git", "-C", str(source), "checkout", "-q", candidate_commit], check=True)
    env = _builder_env(
        tmp_path,
        source,
        required_base_ref=production_commit,
        mutable_root=tmp_path / "runtime",
    )

    result = _run([str(BUILD_SCRIPT), "stale-release"], env=env)

    assert result.returncode == 2
    assert "required production base is not an ancestor" in result.stderr


def test_release_builder_requires_persistent_mutable_root(tmp_path: Path) -> None:
    source, commit = _create_source_repository(tmp_path)
    missing_env = _builder_env(
        tmp_path,
        source,
        required_base_ref=commit,
        mutable_root=None,
    )
    source_env = _builder_env(
        tmp_path,
        source,
        required_base_ref=commit,
        mutable_root=source,
    )

    missing_result = _run([str(BUILD_SCRIPT), "missing-mutable"], env=missing_env)
    source_result = _run([str(BUILD_SCRIPT), "source-mutable"], env=source_env)

    assert missing_result.returncode == 2
    assert "PRICING_SERVICE_MUTABLE_ROOT is required" in missing_result.stderr
    assert source_result.returncode == 2
    assert "mutable root must not be the source worktree" in source_result.stderr


def test_release_builder_rejects_dirty_source(tmp_path: Path) -> None:
    source, commit = _create_source_repository(tmp_path)
    (source / "dirty.txt").write_text("dirty", encoding="utf-8")
    env = _builder_env(
        tmp_path,
        source,
        required_base_ref=commit,
        mutable_root=tmp_path / "runtime",
    )

    result = _run([str(BUILD_SCRIPT), "dirty-release"], env=env)

    assert result.returncode == 2
    assert "source checkout is dirty" in result.stderr


def test_failed_smoke_restores_one_backend_and_ui_release_link(tmp_path: Path) -> None:
    previous = tmp_path / "previous"
    candidate = tmp_path / "candidate"
    mutable_root = _create_mutable_root(tmp_path / "runtime")
    _write_previous_release(previous)
    _write_candidate_release(candidate, mutable_root)
    active_link = tmp_path / "active"
    active_link.symlink_to(previous)
    systemctl_log = tmp_path / "systemctl.log"
    systemctl = _write_fake_systemctl(tmp_path / "systemctl", log=systemctl_log)
    env = _switch_env(
        tmp_path,
        active_link=active_link,
        expected_active=previous,
        mutable_root=mutable_root,
        systemctl=systemctl,
    )
    env["PRICING_SERVICE_FORCE_SMOKE_FAILURE"] = "1"
    env["SYSTEMCTL_LOG"] = str(systemctl_log)

    result = _run([str(SWITCH_SCRIPT), str(candidate)], env=env)

    assert result.returncode != 0
    assert active_link.resolve() == previous
    assert systemctl_log.read_text(encoding="utf-8").splitlines() == [
        "restart pricing-service.service",
        "restart pricing-service.service",
    ]
    assert not (candidate / ".release-verified").exists()
    assert _audit_events(tmp_path / "switch-audit.jsonl") == ["attempt", "rolled_back"]


def test_successful_switch_recreates_read_only_verification_marker(tmp_path: Path) -> None:
    previous = tmp_path / "previous"
    candidate = tmp_path / "candidate"
    mutable_root = _create_mutable_root(tmp_path / "runtime")
    _write_previous_release(previous)
    _write_candidate_release(
        candidate,
        mutable_root,
        source_commit=BASE_COMMIT,
    )
    active_link = tmp_path / "active"
    active_link.symlink_to(previous)
    systemctl = _write_fake_systemctl(tmp_path / "systemctl")
    env = _switch_env(
        tmp_path,
        active_link=active_link,
        expected_active=previous,
        mutable_root=mutable_root,
        systemctl=systemctl,
    )

    result = _run([str(SWITCH_SCRIPT), str(candidate)], env=env)

    assert result.returncode == 0, result.stderr
    assert active_link.resolve() == candidate
    marker = candidate / ".release-verified"
    first_marker = marker.read_text(encoding="utf-8")
    assert first_marker.startswith("verified_at=")
    assert marker.stat().st_mode & 0o222 == 0
    content_sha256 = json.loads((candidate / "release-manifest.json").read_text(encoding="utf-8"))[
        "content_sha256"
    ]
    assert _release_content_sha256(candidate) == content_sha256

    env["PRICING_SERVICE_EXPECTED_ACTIVE_RELEASE"] = str(candidate)
    repeated_result = _run([str(SWITCH_SCRIPT), str(candidate)], env=env)

    assert repeated_result.returncode == 0, repeated_result.stderr
    assert active_link.resolve() == candidate
    assert marker.read_text(encoding="utf-8").startswith("verified_at=")
    assert marker.stat().st_mode & 0o222 == 0
    assert _audit_events(tmp_path / "switch-audit.jsonl") == [
        "attempt",
        "verified",
        "attempt",
        "verified",
    ]


def test_switch_requires_expected_active_release(tmp_path: Path) -> None:
    previous = tmp_path / "previous"
    candidate = tmp_path / "candidate"
    mutable_root = _create_mutable_root(tmp_path / "runtime")
    _write_previous_release(previous)
    _write_candidate_release(candidate, mutable_root)
    active_link = tmp_path / "active"
    active_link.symlink_to(previous)
    systemctl = _write_fake_systemctl(tmp_path / "systemctl")
    env = _switch_env(
        tmp_path,
        active_link=active_link,
        expected_active=previous,
        mutable_root=mutable_root,
        systemctl=systemctl,
    )
    env.pop("PRICING_SERVICE_EXPECTED_ACTIVE_RELEASE")

    result = _run([str(SWITCH_SCRIPT), str(candidate)], env=env)

    assert result.returncode == 2
    assert "PRICING_SERVICE_EXPECTED_ACTIVE_RELEASE is required" in result.stderr
    assert _audit_events(tmp_path / "switch-audit.jsonl") == ["rejected"]


def test_switch_refuses_candidate_without_a_valid_rollback_target(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    mutable_root = _create_mutable_root(tmp_path / "runtime")
    _write_candidate_release(candidate, mutable_root)
    active_link = tmp_path / "missing-active"
    systemctl = _write_fake_systemctl(tmp_path / "systemctl")
    env = _switch_env(
        tmp_path,
        active_link=active_link,
        expected_active=active_link,
        mutable_root=mutable_root,
        systemctl=systemctl,
    )

    result = _run([str(SWITCH_SCRIPT), str(candidate)], env=env)

    assert result.returncode == 2
    assert "no valid rollback target" in result.stderr


def test_switch_refuses_when_active_release_changed_since_preflight(tmp_path: Path) -> None:
    previous = tmp_path / "previous"
    expected = tmp_path / "expected"
    candidate = tmp_path / "candidate"
    mutable_root = _create_mutable_root(tmp_path / "runtime")
    _write_previous_release(previous)
    _write_previous_release(expected)
    _write_candidate_release(candidate, mutable_root)
    active_link = tmp_path / "active"
    active_link.symlink_to(previous)
    systemctl = _write_fake_systemctl(tmp_path / "systemctl")
    env = _switch_env(
        tmp_path,
        active_link=active_link,
        expected_active=expected,
        mutable_root=mutable_root,
        systemctl=systemctl,
    )

    result = _run([str(SWITCH_SCRIPT), str(candidate)], env=env)

    assert result.returncode == 3
    assert "active release changed since preflight" in result.stderr
    assert active_link.resolve() == previous


def test_switch_refuses_when_active_release_changes_during_validation(tmp_path: Path) -> None:
    previous = tmp_path / "previous"
    changed = tmp_path / "changed"
    candidate = tmp_path / "candidate"
    mutable_root = _create_mutable_root(tmp_path / "runtime")
    _write_previous_release(previous)
    _write_previous_release(changed)
    active_link = tmp_path / "active"
    active_link.symlink_to(previous)
    fake_python = """#!/usr/bin/env bash
next_link="${ACTIVE_LINK_TO_CHANGE}.test-next"
ln -s "$CHANGED_TARGET" "$next_link"
mv -Tf "$next_link" "$ACTIVE_LINK_TO_CHANGE"
exit 0
"""
    _write_candidate_release(candidate, mutable_root, fake_python_body=fake_python)
    systemctl = _write_fake_systemctl(tmp_path / "systemctl")
    env = _switch_env(
        tmp_path,
        active_link=active_link,
        expected_active=previous,
        mutable_root=mutable_root,
        systemctl=systemctl,
    )
    env["ACTIVE_LINK_TO_CHANGE"] = str(active_link)
    env["CHANGED_TARGET"] = str(changed)

    result = _run([str(SWITCH_SCRIPT), str(candidate)], env=env)

    assert result.returncode == 3
    assert "active release changed during preflight" in result.stderr
    assert active_link.resolve() == changed


def test_switch_refuses_candidate_built_from_another_production_base(tmp_path: Path) -> None:
    previous = tmp_path / "previous"
    candidate = tmp_path / "candidate"
    mutable_root = _create_mutable_root(tmp_path / "runtime")
    _write_previous_release(previous)
    _write_candidate_release(candidate, mutable_root, required_base_commit="3" * 40)
    active_link = tmp_path / "active"
    active_link.symlink_to(previous)
    systemctl = _write_fake_systemctl(tmp_path / "systemctl")
    env = _switch_env(
        tmp_path,
        active_link=active_link,
        expected_active=previous,
        mutable_root=mutable_root,
        systemctl=systemctl,
    )

    result = _run([str(SWITCH_SCRIPT), str(candidate)], env=env)

    assert result.returncode == 3
    assert "production base does not match active source_commit" in result.stderr
    assert active_link.resolve() == previous


def test_switch_rejects_unverified_dirty_or_legacy_manifest(tmp_path: Path) -> None:
    previous = tmp_path / "previous"
    mutable_root = _create_mutable_root(tmp_path / "runtime")
    _write_previous_release(previous)
    systemctl = _write_fake_systemctl(tmp_path / "systemctl")

    cases = (
        ({"source_verified": False}, "does not confirm source verification"),
        ({"source_dirty": True}, "source_dirty=false"),
        ({"content_hash_scheme": ""}, "unsupported release content hash scheme"),
    )
    for index, (candidate_kwargs, expected_message) in enumerate(cases):
        candidate = tmp_path / f"candidate-{index}"
        _write_candidate_release(candidate, mutable_root, **candidate_kwargs)
        active_link = tmp_path / f"active-{index}"
        active_link.symlink_to(previous)
        case_root = tmp_path / f"case-{index}"
        case_root.mkdir()
        env = _switch_env(
            case_root,
            active_link=active_link,
            expected_active=previous,
            mutable_root=mutable_root,
            systemctl=systemctl,
        )

        result = _run([str(SWITCH_SCRIPT), str(candidate)], env=env)

        assert result.returncode == 2
        assert expected_message in result.stderr
        assert active_link.resolve() == previous


def test_switch_stops_when_nginx_dump_fails_even_if_output_contains_active_path(
    tmp_path: Path,
) -> None:
    previous = tmp_path / "previous"
    candidate = tmp_path / "candidate"
    mutable_root = _create_mutable_root(tmp_path / "runtime")
    _write_previous_release(previous)
    _write_candidate_release(candidate, mutable_root)
    active_link = tmp_path / "active"
    active_link.symlink_to(previous)
    systemctl = _write_fake_systemctl(tmp_path / "systemctl")
    fake_nginx = tmp_path / "nginx"
    fake_nginx.write_text(
        f"#!/usr/bin/env bash\nprintf 'root {active_link}/ui/dist;\\n'\nexit 1\n",
        encoding="utf-8",
    )
    fake_nginx.chmod(0o755)
    env = _switch_env(
        tmp_path,
        active_link=active_link,
        expected_active=previous,
        mutable_root=mutable_root,
        systemctl=systemctl,
    )
    env["PRICING_SERVICE_NGINX_PREFLIGHT"] = "1"
    env["PRICING_SERVICE_NGINX_BIN"] = str(fake_nginx)

    result = _run([str(SWITCH_SCRIPT), str(candidate)], env=env)

    assert result.returncode == 2
    assert "nginx configuration test failed" in result.stderr
    assert active_link.resolve() == previous


def test_switch_refuses_release_with_content_hash_mismatch(tmp_path: Path) -> None:
    previous = tmp_path / "previous"
    candidate = tmp_path / "candidate"
    mutable_root = _create_mutable_root(tmp_path / "runtime")
    _write_previous_release(previous)
    _write_candidate_release(candidate, mutable_root)
    (candidate / "ui/dist/index.html").write_text("tampered", encoding="utf-8")
    active_link = tmp_path / "active"
    active_link.symlink_to(previous)
    systemctl = _write_fake_systemctl(tmp_path / "systemctl")
    env = _switch_env(
        tmp_path,
        active_link=active_link,
        expected_active=previous,
        mutable_root=mutable_root,
        systemctl=systemctl,
    )

    result = _run([str(SWITCH_SCRIPT), str(candidate)], env=env)

    assert result.returncode == 2
    assert "release content hash mismatch" in result.stderr
    assert active_link.resolve() == previous


def test_switch_refuses_wrong_mutable_root(tmp_path: Path) -> None:
    previous = tmp_path / "previous"
    candidate = tmp_path / "candidate"
    mutable_root = _create_mutable_root(tmp_path / "runtime")
    other_root = _create_mutable_root(tmp_path / "other-runtime")
    _write_previous_release(previous)
    _write_candidate_release(candidate, mutable_root)
    active_link = tmp_path / "active"
    active_link.symlink_to(previous)
    systemctl = _write_fake_systemctl(tmp_path / "systemctl")
    env = _switch_env(
        tmp_path,
        active_link=active_link,
        expected_active=previous,
        mutable_root=other_root,
        systemctl=systemctl,
    )

    result = _run([str(SWITCH_SCRIPT), str(candidate)], env=env)

    assert result.returncode == 2
    assert "candidate mutable_root does not match" in result.stderr


def test_switch_lock_blocks_parallel_cutover(tmp_path: Path) -> None:
    previous = tmp_path / "previous"
    candidate = tmp_path / "candidate"
    mutable_root = _create_mutable_root(tmp_path / "runtime")
    _write_previous_release(previous)
    _write_candidate_release(candidate, mutable_root)
    active_link = tmp_path / "active"
    active_link.symlink_to(previous)
    systemctl = _write_fake_systemctl(tmp_path / "systemctl")
    env = _switch_env(
        tmp_path,
        active_link=active_link,
        expected_active=previous,
        mutable_root=mutable_root,
        systemctl=systemctl,
    )
    lock_path = Path(env["PRICING_SERVICE_SWITCH_LOCK_FILE"])

    with lock_path.open("w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = _run([str(SWITCH_SCRIPT), str(candidate)], env=env)

    assert result.returncode == 3
    assert "another pricing-service release switch is already running" in result.stderr
    assert active_link.resolve() == previous
