from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = REPO_ROOT / "scripts/build_pricing_service_release.sh"
SWITCH_SCRIPT = REPO_ROOT / "scripts/switch_pricing_service_release.sh"


def _run(command: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def test_release_builder_creates_locked_release_specific_runtime(tmp_path: Path) -> None:
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
        ["git", "-C", str(source), "config", "user.email", "test@example.com"], check=True
    )
    subprocess.run(["git", "-C", str(source), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "fixture"], check=True)

    release_root = tmp_path / "releases"
    env = {
        **os.environ,
        "PRICING_SERVICE_SOURCE_ROOT": str(source),
        "PRICING_SERVICE_RELEASE_ROOT": str(release_root),
        "PRICING_SERVICE_BUILD_UI": "0",
    }
    result = _run([str(BUILD_SCRIPT), "test-release"], env=env)
    assert result.returncode == 0, result.stderr

    release = Path(result.stdout.strip())
    manifest = json.loads((release / "release-manifest.json").read_text(encoding="utf-8"))
    assert manifest["source_dirty"] is False
    assert manifest["runtime_env_file"] == str(source / ".env")
    assert manifest["alembic_revision"] == "r1"
    assert manifest["requirements_lock_sha256"] == hashlib.sha256(b"").hexdigest()
    assert len(manifest["pip_freeze_sha256"]) == 64
    assert len(manifest["ui_asset_sha256"]) == 64
    assert (release / ".venv/bin/python").exists()
    assert (release / ".venv/bin/pip").read_text(encoding="utf-8").splitlines()[0] == (
        f"#!{release}/.venv/bin/python"
    )
    assert (release / ".env").resolve() == source / ".env"
    assert (release / "ui/dist/index.html").stat().st_mode & 0o222 == 0


def test_failed_smoke_restores_one_backend_and_ui_release_link(tmp_path: Path) -> None:
    previous = tmp_path / "previous"
    candidate = tmp_path / "candidate"
    previous.mkdir()
    (candidate / ".venv/bin").mkdir(parents=True)
    (candidate / "ui/dist").mkdir(parents=True)
    (candidate / "scripts").mkdir()
    fake_python = candidate / ".venv/bin/python"
    fake_python.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake_python.chmod(0o755)
    (candidate / "ui/dist/index.html").write_text("candidate", encoding="utf-8")
    (candidate / "release-manifest.json").write_text("{}", encoding="utf-8")
    (candidate / "requirements.lock").write_text("", encoding="utf-8")
    active_link = tmp_path / "active"
    active_link.symlink_to(previous)

    systemctl_log = tmp_path / "systemctl.log"
    fake_systemctl = tmp_path / "systemctl"
    fake_systemctl.write_text(
        '#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" >> "$SYSTEMCTL_LOG"\n',
        encoding="utf-8",
    )
    fake_systemctl.chmod(0o755)

    env = {
        **os.environ,
        "PRICING_SERVICE_ACTIVE_LINK": str(active_link),
        "PRICING_SERVICE_SYSTEMCTL_BIN": str(fake_systemctl),
        "PRICING_SERVICE_SYSTEMD_PREFLIGHT": "0",
        "PRICING_SERVICE_NGINX_PREFLIGHT": "0",
        "PRICING_SERVICE_FORCE_SMOKE_FAILURE": "1",
        "SYSTEMCTL_LOG": str(systemctl_log),
    }
    result = _run([str(SWITCH_SCRIPT), str(candidate)], env=env)

    assert result.returncode != 0
    assert active_link.resolve() == previous
    assert systemctl_log.read_text(encoding="utf-8").splitlines() == [
        "restart pricing-service.service",
        "restart pricing-service.service",
    ]
    assert not (candidate / ".release-verified").exists()


def test_successful_switch_marks_release_verified_after_smoke(tmp_path: Path) -> None:
    previous = tmp_path / "previous"
    candidate = tmp_path / "candidate"
    previous.mkdir()
    (candidate / ".venv/bin").mkdir(parents=True)
    (candidate / "ui/dist").mkdir(parents=True)
    (candidate / "scripts").mkdir()
    fake_python = candidate / ".venv/bin/python"
    fake_python.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake_python.chmod(0o755)
    (candidate / "ui/dist/index.html").write_text("candidate", encoding="utf-8")
    (candidate / "release-manifest.json").write_text("{}", encoding="utf-8")
    (candidate / "requirements.lock").write_text("", encoding="utf-8")
    active_link = tmp_path / "active"
    active_link.symlink_to(previous)

    fake_systemctl = tmp_path / "systemctl"
    fake_systemctl.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake_systemctl.chmod(0o755)
    result = _run(
        [str(SWITCH_SCRIPT), str(candidate)],
        env={
            **os.environ,
            "PRICING_SERVICE_ACTIVE_LINK": str(active_link),
            "PRICING_SERVICE_SYSTEMCTL_BIN": str(fake_systemctl),
            "PRICING_SERVICE_SYSTEMD_PREFLIGHT": "0",
            "PRICING_SERVICE_NGINX_PREFLIGHT": "0",
        },
    )

    assert result.returncode == 0, result.stderr
    assert active_link.resolve() == candidate
    marker = candidate / ".release-verified"
    assert marker.read_text(encoding="utf-8").startswith("verified_at=")
    assert marker.stat().st_mode & 0o222 == 0


def test_switch_refuses_candidate_without_a_valid_rollback_target(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    (candidate / ".venv/bin").mkdir(parents=True)
    (candidate / "ui/dist").mkdir(parents=True)
    (candidate / ".venv/bin/python").write_text(
        "#!/usr/bin/env bash\nexit 0\n",
        encoding="utf-8",
    )
    (candidate / ".venv/bin/python").chmod(0o755)
    (candidate / "ui/dist/index.html").write_text("candidate", encoding="utf-8")
    (candidate / "release-manifest.json").write_text("{}", encoding="utf-8")
    (candidate / "requirements.lock").write_text("", encoding="utf-8")

    result = _run(
        [str(SWITCH_SCRIPT), str(candidate)],
        env={
            **os.environ,
            "PRICING_SERVICE_ACTIVE_LINK": str(tmp_path / "missing-active"),
            "PRICING_SERVICE_SYSTEMD_PREFLIGHT": "0",
            "PRICING_SERVICE_NGINX_PREFLIGHT": "0",
        },
    )

    assert result.returncode == 2
    assert "no valid rollback target" in result.stderr


def test_switch_stops_when_nginx_dump_fails_even_if_output_contains_active_path(
    tmp_path: Path,
) -> None:
    previous = tmp_path / "previous"
    candidate = tmp_path / "candidate"
    previous.mkdir()
    (candidate / ".venv/bin").mkdir(parents=True)
    (candidate / "ui/dist").mkdir(parents=True)
    fake_python = candidate / ".venv/bin/python"
    fake_python.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake_python.chmod(0o755)
    (candidate / "ui/dist/index.html").write_text("candidate", encoding="utf-8")
    (candidate / "release-manifest.json").write_text("{}", encoding="utf-8")
    (candidate / "requirements.lock").write_text("", encoding="utf-8")
    active_link = tmp_path / "active"
    active_link.symlink_to(previous)

    systemctl_log = tmp_path / "systemctl.log"
    fake_systemctl = tmp_path / "systemctl"
    fake_systemctl.write_text(
        '#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" >> "$SYSTEMCTL_LOG"\n',
        encoding="utf-8",
    )
    fake_systemctl.chmod(0o755)
    fake_nginx = tmp_path / "nginx"
    fake_nginx.write_text(
        f"#!/usr/bin/env bash\nprintf 'root {active_link}/ui/dist;\\n'\nexit 1\n",
        encoding="utf-8",
    )
    fake_nginx.chmod(0o755)

    result = _run(
        [str(SWITCH_SCRIPT), str(candidate)],
        env={
            **os.environ,
            "PRICING_SERVICE_ACTIVE_LINK": str(active_link),
            "PRICING_SERVICE_SYSTEMCTL_BIN": str(fake_systemctl),
            "PRICING_SERVICE_SYSTEMD_PREFLIGHT": "0",
            "PRICING_SERVICE_NGINX_BIN": str(fake_nginx),
            "SYSTEMCTL_LOG": str(systemctl_log),
        },
    )

    assert result.returncode == 2
    assert "nginx configuration test failed" in result.stderr
    assert active_link.resolve() == previous
    assert not systemctl_log.exists()
