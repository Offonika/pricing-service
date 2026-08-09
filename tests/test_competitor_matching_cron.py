from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
NIGHTLY = REPO_ROOT / "infra" / "cron" / "competitor_matching_nightly.sh"


def test_nightly_loads_env_before_resolving_feature_flags() -> None:
    source = NIGHTLY.read_text(encoding="utf-8")

    env_load = source.index('load_env_file_preserve_json "${ENV_FILE}"')
    embeddings_flag = source.index(
        'EMBEDDINGS_ENABLED="${COMPETITOR_MATCHING_EMBEDDINGS_ENABLED:-1}"'
    )

    assert env_load < embeddings_flag


def test_nightly_blocks_matching_when_source_is_stale() -> None:
    source = NIGHTLY.read_text(encoding="utf-8")

    assert "--ftp-only" in source
    assert 'ftp_status="stale"' in source
    stale_branch = source.index('overall_status="blocked_source_stale"')
    matcher = source.index('run_step "match_competitor_ftp"')

    assert 'write_latest_report "3"' in source
    assert "exit 3" in source
    assert stale_branch < matcher


def test_nightly_uses_https_import_without_ftp_fallback() -> None:
    source = NIGHTLY.read_text(encoding="utf-8")

    assert "tasks.import_competitor_http" in source
    assert "tasks.import_competitor_ftp" not in source
    assert "COMPETITOR_FTP_IMPORT_ENABLED" not in source
