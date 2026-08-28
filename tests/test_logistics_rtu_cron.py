from __future__ import annotations

from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]


def test_logistics_rtu_sync_cron_is_locked_timed_and_apply_is_explicit() -> None:
    script = (PROJECT / "infra/cron/logistics_rtu_sync.sh").read_text(encoding="utf-8")
    watchdog = (PROJECT / "infra/cron/logistics_rtu_sync_watchdog.sh").read_text(encoding="utf-8")
    cron = (PROJECT / "infra/cron/logistics_rtu_sync.cron").read_text(encoding="utf-8")

    assert "flock -n" in script
    assert "LOGISTICS_RTU_SYNC_APPLY:-false" in script
    assert "LOGISTICS_RTU_SYNC_TIMEOUT_SECONDS:-50" in script
    assert 'timeout "${TIMEOUT_SECONDS}"' in script
    assert "LOGISTICS_RTU_SYNC_PAGE_SIZE:-500" in script
    assert "--date-from" in script
    assert "--limit" in script
    assert 'touch "${SUCCESS_FILE}"' in script
    assert "LOGISTICS_RTU_SYNC_MAX_AGE_SECONDS:-180" in watchdog
    assert "LOGISTICS_STAGE_OUTBOX_MAX_DELAY_SECONDS:-30" in watchdog
    assert "tasks.check_logistics_stage_outbox_health" in watchdog
    assert "CRITICAL logistics_rtu_sync is stale" in watchdog
    assert "LOGISTICS_RTU_SYNC_APPLY=true" in cron
    assert cron.count("* * * * * root") == 2
