from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
BUILDER = REPO_ROOT / "scripts" / "build_pricing_service_release.sh"
PYTHON_BIN = Path(sys.executable)
RELEASE_PYTHON_SCRIPTS = (
    REPO_ROOT / "scripts" / "validate_executive_dashboard_release.py",
    REPO_ROOT / "scripts" / "validate_receivables_release.py",
    REPO_ROOT / "scripts" / "check_executive_dashboard_runtime.py",
)


def _load_executive_release_validator():
    path = REPO_ROOT / "scripts" / "validate_executive_dashboard_release.py"
    spec = importlib.util.spec_from_file_location("executive_release_validator", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_receivables_release_validator():
    path = REPO_ROOT / "scripts" / "validate_receivables_release.py"
    spec = importlib.util.spec_from_file_location("receivables_release_validator", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_executive_release_validator_defers_revision_equality_only_when_requested() -> None:
    validator = _load_executive_release_validator()

    assert validator._migration_revision_error("old", "new", skip_database_revision=True) is None
    assert (
        validator._migration_revision_error("old", "new", skip_database_revision=False)
        == "database revision old does not match code head new"
    )


def test_receivables_release_validator_uses_read_only_session_scope(
    monkeypatch,
    tmp_path: Path,
) -> None:
    validator = _load_receivables_release_validator()
    snapshot_date = date(2026, 8, 31)
    session = object()
    session_scope_calls: list[bool] = []
    freshness_calls: list[tuple[object, date]] = []

    @contextmanager
    def fake_session_scope(*, read_only: bool = False):
        session_scope_calls.append(read_only)
        yield session

    def fake_evaluate_open_debt_source_freshness(actual_session, *, snapshot_date):
        freshness_calls.append((actual_session, snapshot_date))
        return SimpleNamespace(
            source_status="cache_ready",
            source_max_document_date=snapshot_date,
            source_lag_days=0,
        )

    monkeypatch.setattr("app.infrastructure.db.session_scope", fake_session_scope)
    monkeypatch.setattr(
        "app.services.counterparty_folder_recommendations.evaluate_open_debt_source_freshness",
        fake_evaluate_open_debt_source_freshness,
    )

    component_path = tmp_path / "ui" / "src" / "components" / "ReceivablesWorkplace.tsx"
    component_path.parent.mkdir(parents=True)
    component_path.write_text(validator.REQUIRED_UI_TEXT, encoding="utf-8")
    asset_path = tmp_path / "ui" / "dist" / "assets" / "app.js"
    asset_path.parent.mkdir(parents=True)
    asset_path.write_text(validator.REQUIRED_UI_TEXT, encoding="utf-8")
    (tmp_path / "ui" / "dist" / "index.html").write_text(
        '<script src="/assets/app.js"></script>',
        encoding="utf-8",
    )

    report = validator.validate_release(tmp_path, snapshot_date=snapshot_date)

    assert report["ok"] is True
    assert session_scope_calls == [True]
    assert freshness_calls == [(session, snapshot_date)]


def _git(source: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(source), *args], check=True, capture_output=True)


def _source_tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    source = tmp_path / "source"
    releases = tmp_path / "releases"
    runtime = tmp_path / "runtime"
    (source / "ui" / "dist" / "assets").mkdir(parents=True)
    (source / "pkg" / "__pycache__").mkdir(parents=True)
    (source / "alembic" / "versions").mkdir(parents=True)
    (source / "embeddings").mkdir()
    (source / ".pytest_cache").mkdir()
    (source / ".ruff_cache").mkdir()
    runtime.mkdir()
    (runtime / "embeddings").mkdir()
    (runtime / ".env").write_text("DATABASE_URL=test\n", encoding="utf-8")
    (source / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (source / "requirements.lock").write_text("", encoding="utf-8")
    (source / "alembic" / "versions" / "0001_test.py").write_text(
        'revision = "test-revision"\ndown_revision = None\n',
        encoding="utf-8",
    )
    (source / "embeddings" / "source-only.npy").write_bytes(b"must-not-be-released")
    (runtime / "embeddings" / "persistent-index.json").write_text(
        '{"meta": {}}\n', encoding="utf-8"
    )
    (source / "pkg" / "__pycache__" / "module.pyc").write_bytes(b"cache")
    (source / ".pytest_cache" / "state").write_text("cache", encoding="utf-8")
    (source / ".ruff_cache" / "state").write_text("cache", encoding="utf-8")
    (source / "ui" / "dist" / "index.html").write_text(
        '<script src="/assets/old.js"></script>\n', encoding="utf-8"
    )
    (source / "ui" / "dist" / "assets" / "old.js").write_text("old\n", encoding="utf-8")
    _git(source, "init")
    _git(source, "config", "user.email", "test@example.invalid")
    _git(source, "config", "user.name", "Release Builder Test")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "fixture")
    return source, releases, runtime


def _run_builder(
    source: Path,
    releases: Path,
    runtime: Path,
    release_name: str,
    *,
    base_release: Path | None = None,
    overlay_paths: str | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PRICING_SERVICE_SOURCE_ROOT": str(source),
        "PRICING_SERVICE_RELEASE_ROOT": str(releases),
        "PRICING_SERVICE_RUNTIME_ENV_FILE": str(runtime / ".env"),
        "PRICING_SERVICE_MUTABLE_ROOT": str(runtime),
        "PRICING_SERVICE_RELEASE_REQUIRED_BASE_REF": subprocess.check_output(
            ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
        ).strip(),
        "PRICING_SERVICE_PYTHON_BOOTSTRAP": str(PYTHON_BIN),
        "PRICING_SERVICE_BUILD_UI": "0",
        "PRICING_SERVICE_INSTALL_VENV": "0",
        "PRICING_SERVICE_ALLOW_OVERLAY": "1" if base_release is not None else "0",
    }
    if base_release is not None:
        env["PRICING_SERVICE_BASE_RELEASE"] = str(base_release)
    if overlay_paths is not None:
        env["PRICING_SERVICE_RELEASE_OVERLAY_PATHS"] = overlay_paths
    return subprocess.run(
        [str(BUILDER), release_name],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_builder_rejects_dirty_source(tmp_path: Path) -> None:
    source, releases, runtime = _source_tree(tmp_path)
    (source / "app.py").write_text("VALUE = 2\n", encoding="utf-8")

    result = _run_builder(source, releases, runtime, "dirty")

    assert result.returncode == 2
    assert "clean Git tree" in result.stderr
    assert not (releases / "dirty").exists()


def test_builder_excludes_caches_and_makes_release_read_only(tmp_path: Path) -> None:
    source, releases, runtime = _source_tree(tmp_path)

    result = _run_builder(source, releases, runtime, "full")

    assert result.returncode == 0, result.stderr
    release = releases / "full"
    manifest = json.loads((release / "release-manifest.json").read_text(encoding="utf-8"))
    assert manifest["source_dirty"] is False
    assert (
        manifest["source_commit"]
        == subprocess.check_output(
            ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
        ).strip()
    )
    assert manifest["alembic_revision"] == "test-revision"
    assert manifest["base_release"] == "source-tree"
    assert len(manifest["content_sha256"]) == 64
    assert not list(release.rglob("__pycache__"))
    assert not list(release.rglob(".pytest_cache"))
    assert not list(release.rglob(".ruff_cache"))
    assert not list(release.rglob("*.pyc"))
    assert release.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH) == 0
    for path in release.rglob("*"):
        if path.is_symlink():
            continue
        mode = path.stat().st_mode
        assert mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH) == 0
    assert (release / "build").is_symlink()
    assert (release / "embeddings").is_symlink()
    assert (release / "embeddings").resolve() == (runtime / "embeddings").resolve()
    assert not (release / "embeddings" / "source-only.npy").exists()
    assert (release / "embeddings" / "persistent-index.json").is_file()
    assert os.access(runtime / "build", os.W_OK)


def test_builder_excludes_linked_worktree_git_pointer(tmp_path: Path) -> None:
    source, releases, runtime = _source_tree(tmp_path)
    linked_worktree = tmp_path / "linked-worktree"
    _git(source, "worktree", "add", "--detach", str(linked_worktree))
    assert (linked_worktree / ".git").is_file()

    result = _run_builder(linked_worktree, releases, runtime, "linked")

    assert result.returncode == 0, result.stderr
    release = releases / "linked"
    assert not (release / ".git").exists()
    assert (release / "app.py").is_file()


def test_release_python_scripts_disable_bytecode_writes() -> None:
    for script in RELEASE_PYTHON_SCRIPTS:
        result = subprocess.run(
            [
                str(PYTHON_BIN),
                "-c",
                (
                    "import runpy, sys; "
                    f"runpy.run_path({str(script)!r}); "
                    "print(sys.dont_write_bytecode)"
                ),
            ],
            text=True,
            capture_output=True,
            check=True,
        )
        assert result.stdout.strip() == "True"


def test_overlay_requires_clean_base_and_replaces_ui_assets(tmp_path: Path) -> None:
    source, releases, runtime = _source_tree(tmp_path)
    full = _run_builder(source, releases, runtime, "full")
    assert full.returncode == 0, full.stderr
    base_release = releases / "full"

    (source / "ui" / "dist" / "index.html").write_text(
        '<script src="/assets/new.js"></script>\n', encoding="utf-8"
    )
    (source / "ui" / "dist" / "assets" / "new.js").write_text("new\n", encoding="utf-8")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "new ui")

    overlay = _run_builder(
        source,
        releases,
        runtime,
        "overlay",
        base_release=base_release,
        overlay_paths="ui/dist/index.html ui/dist/assets/new.js",
    )

    assert overlay.returncode == 0, overlay.stderr
    overlay_release = releases / "overlay"
    assert not (overlay_release / "ui" / "dist" / "assets" / "old.js").exists()
    assert (overlay_release / "ui" / "dist" / "assets" / "new.js").is_file()
    manifest = json.loads((overlay_release / "release-manifest.json").read_text(encoding="utf-8"))
    assert manifest["base_release"] == str(base_release.resolve())

    dirty_base = tmp_path / "dirty-base"
    dirty_base.mkdir()
    (dirty_base / "release-manifest.json").write_text(
        json.dumps({"source_dirty": True}), encoding="utf-8"
    )
    rejected = _run_builder(
        source,
        releases,
        runtime,
        "rejected-overlay",
        base_release=dirty_base,
        overlay_paths="app.py",
    )
    assert rejected.returncode == 2
    assert "source_dirty=false" in rejected.stderr
