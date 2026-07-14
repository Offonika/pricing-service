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


def test_nightly_marks_stale_ftp_as_degraded_without_retrying_same_day() -> None:
    source = NIGHTLY.read_text(encoding="utf-8")

    assert "--ftp-only" in source
    assert 'ftp_status="stale"' in source
    assert 'overall_status="degraded_source_stale"' in source
    assert '{"success", "degraded_source_stale"}' in source
