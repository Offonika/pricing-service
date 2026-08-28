from __future__ import annotations

from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]


def test_logistics_rtu_sync_cron_is_locked_and_apply_is_explicit() -> None:
    script = (PROJECT / "infra/cron/logistics_rtu_sync.sh").read_text(encoding="utf-8")
    cron = (PROJECT / "infra/cron/logistics_rtu_sync.cron").read_text(encoding="utf-8")

    assert "flock -n" in script
    assert "LOGISTICS_RTU_SYNC_APPLY:-false" in script
    assert "--date-from" in script
    assert "--limit" in script
    assert "--apply" in script
    assert "LOGISTICS_RTU_SYNC_APPLY=true" in cron
    assert "* * * * * root" in cron
