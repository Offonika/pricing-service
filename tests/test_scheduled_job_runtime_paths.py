from pathlib import Path

import pytest

ACTIVE_RELEASE = "/opt/MM/pricing-service-task43-current"
REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("cron_name", "wrapper_name"),
    (
        ("onec_sales_kpi_sync.cron", "onec_sales_kpi_sync.sh"),
        ("receivable_ledger_sync.cron", "receivable_ledger_sync.sh"),
        ("sku_result_sync_ut103.cron", "sku_result_sync_ut103.sh"),
    ),
)
def test_verified_canary_cron_templates_use_active_release(
    cron_name: str,
    wrapper_name: str,
) -> None:
    cron = (REPO_ROOT / "infra" / "cron" / cron_name).read_text(encoding="utf-8")

    assert f"REPO_DIR={ACTIVE_RELEASE}" in cron
    assert f"{ACTIVE_RELEASE}/infra/cron/{wrapper_name}" in cron
    assert f"/opt/MM/pricing-service/infra/cron/{wrapper_name}" not in cron
