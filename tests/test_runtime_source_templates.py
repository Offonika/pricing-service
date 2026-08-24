from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
IMMUTABLE_RUNTIME_ROOT = "/opt/MM/pricing-service-task43-current"
MUTABLE_CODE_ROOT = "/opt/MM/pricing-service/"

RUNTIME_TEMPLATES = (
    "infra/cron/manual_matching_bitrix_tasks.cron",
    "infra/cron/sync_open_procurement_supplier_orders_to_bitrix.cron",
    "infra/cron/competitor_matching_nightly.cron",
    "infra/cron/bronze_price_type_monthly_inventory.cron",
    "infra/cron/onec_assembly_crm_reconciler.cron",
    "infra/cron/receivable_ledger_sync.cron",
    "infra/cron/sku_generation_ut103.cron",
    "infra/cron/sync_telephony_mapping.cron",
    "infra/systemd/pricing-executive-dashboard-monitor.service",
    "infra/systemd/pricing-expertise-alarm-scan.service",
    "infra/systemd/pricing-expertise-sync-watchdog.service",
    "infra/systemd/pricing-expertise-sync.service",
)


@pytest.mark.parametrize("relative_path", RUNTIME_TEMPLATES)
def test_cutover_template_uses_immutable_runtime(relative_path: str) -> None:
    text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")

    assert IMMUTABLE_RUNTIME_ROOT in text
    assert MUTABLE_CODE_ROOT not in text


def test_dashboard_monitor_uses_importable_module_entrypoint() -> None:
    text = (REPO_ROOT / "infra/systemd/pricing-executive-dashboard-monitor.service").read_text(
        encoding="utf-8"
    )

    assert " -m scripts.check_executive_dashboard_runtime --mode monitor" in text


def test_onec_assembly_job_uses_importable_module_entrypoint() -> None:
    wrapper = (REPO_ROOT / "infra/cron/onec_assembly_crm_reconciler.sh").read_text(encoding="utf-8")
    schedule = (REPO_ROOT / "infra/cron/onec_assembly_crm_reconciler.cron").read_text(
        encoding="utf-8"
    )

    assert '"${PYTHON_BIN}" -m tasks.reconcile_onec_assembly_to_crm' in wrapper
    assert f"PYTHONPATH={IMMUTABLE_RUNTIME_ROOT}" in schedule


def test_telephony_wrapper_accepts_release_root() -> None:
    text = (REPO_ROOT / "infra/cron/sync_telephony_mapping.sh").read_text(encoding="utf-8")

    assert 'ROOT_DIR="${REPO_DIR:-/opt/MM/pricing-service}"' in text


def test_bronze_inventory_schedule_sets_import_path() -> None:
    text = (REPO_ROOT / "infra/cron/bronze_price_type_monthly_inventory.cron").read_text(
        encoding="utf-8"
    )

    assert f"PYTHONPATH={IMMUTABLE_RUNTIME_ROOT}" in text
