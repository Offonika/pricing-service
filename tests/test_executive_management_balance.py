from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_db
from app.core.config import Settings
from app.main import app
from app.models import (
    ExecutiveManagementBalanceAudit,
    ExecutiveManagementBalanceSnapshot,
)
from app.services import bitrix_executive_dashboard_auth, executive_dashboard
from app.services import executive_management_balance as balance_service
from app.services.executive_management_balance import (
    BalanceLineDraft,
    ManagementBalanceCloseError,
    build_and_persist_management_balance_snapshot,
    close_management_balance,
    get_management_balance,
    month_end,
    parse_month,
)


def _settings() -> Settings:
    return Settings(
        onec_database_url=None,
        management_internal_api_token="secret-token",
        executive_dashboard_finance_snapshot_path="/tmp/missing-finance-snapshot.json",
        executive_dashboard_cashflow_period_cache_path="/tmp/missing-cashflow-cache.json",
        executive_dashboard_warehouse_snapshot_path="/tmp/missing-warehouse-snapshot.json",
        executive_dashboard_owner_cash_control_snapshot_path=(
            "/tmp/missing-owner-cash-control-snapshot.json"
        ),
        executive_management_balance_bp_tax_snapshot_path=("/tmp/missing-bp-tax-snapshot.json"),
        executive_management_balance_payroll_snapshot_path=(
            "/tmp/missing-employee-payroll-balance-snapshot.json"
        ),
        executive_management_balance_tolerance_rub=1,
    )


def _complete_lines() -> tuple[list[BalanceLineDraft], dict[str, object]]:
    as_of = date(2026, 6, 30)
    return (
        [
            BalanceLineDraft(
                "asset",
                "cash",
                "Деньги",
                Decimal("100.00"),
                10,
                "ka_bp_accounting",
                "ready",
                as_of,
            ),
            BalanceLineDraft(
                "liability",
                "suppliers",
                "Поставщики",
                Decimal("80.00"),
                10,
                "ka_bp_accounting",
                "ready",
                as_of,
            ),
            BalanceLineDraft(
                "equity",
                "owner_capital",
                "Капитал",
                Decimal("20.00"),
                10,
                "ka_bp_accounting",
                "ready",
                as_of,
            ),
            BalanceLineDraft(
                "asset",
                "inventory_cost",
                "Товар",
                Decimal("0.00"),
                20,
                "onec_inventory_cost",
                "ready",
                as_of,
            ),
            BalanceLineDraft(
                "asset",
                "owner_cash_control",
                "Контроль переводов собственника",
                Decimal("0.00"),
                30,
                "management_owner_cash_control",
                "ready",
                as_of,
            ),
        ],
        {
            "accounting": {"configured": True, "status": "ready"},
            "salary_reconciliation": {
                "configured": True,
                "status": "ready",
                "closing_blocked": False,
                "blockers": [],
            },
        },
    )


def test_month_parser_and_leap_year_end() -> None:
    assert parse_month("2024-02") == date(2024, 2, 1)
    assert month_end(date(2024, 2, 1)) == date(2024, 2, 29)
    with pytest.raises(ValueError):
        parse_month("2024-13")


def test_inventory_quantity_reconciliation_mismatch_blocks_month_close() -> None:
    lines, source_summary = _complete_lines()
    source_summary["inventory"] = {
        "status": "partial",
        "reconciliation_status": "quantity_mismatch",
        "stock_quantity": "951101.000",
        "party_quantity": "951367.000",
        "quantity_difference": "-266.000",
    }

    errors = balance_service._validation_errors(lines, source_summary)

    mismatch = next(
        item for item in errors if item["code"] == "inventory_quantity_reconciliation_mismatch"
    )
    assert mismatch["severity"] == "error"
    assert "-266.000" in mismatch["message"]


def test_bp_tax_snapshot_populates_taxes_payable(tmp_path: Path) -> None:
    path = tmp_path / "bp-tax.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "as_of": "2026-07-13",
                "lines": {
                    "taxes_payable": {
                        "amount": "1227003.25",
                        "source_status": "ready",
                        "note": "Кредитовое сальдо счетов 68/69",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    line, summary = balance_service._load_bp_tax_line(
        balance_date=date(2026, 7, 13),
        snapshot_path=str(path),
    )

    assert line.key == "taxes_payable"
    assert line.amount == Decimal("1227003.25")
    assert line.source_key == "onec_bp_tax_accounting"
    assert line.source_status == "ready"
    assert line.source_as_of == date(2026, 7, 13)
    assert summary["configured"] is True
    assert summary["status"] == "ready"


def test_bp_tax_snapshot_requires_exact_balance_date(tmp_path: Path) -> None:
    path = tmp_path / "bp-tax.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "as_of": "2026-07-12",
                "lines": {
                    "taxes_payable": {
                        "amount": "100.00",
                        "source_status": "ready",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    line, summary = balance_service._load_bp_tax_line(
        balance_date=date(2026, 7, 13),
        snapshot_path=str(path),
    )

    assert line.amount is None
    assert line.source_status == "stale"
    assert summary["status"] == "stale"


def test_bp_balance_snapshot_populates_verified_lines_and_keeps_unverified_unknown(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bp-balance.json"
    path.write_text(
        json.dumps(
            {
                "contract_version": "executive-bp-balance-snapshot.v1",
                "as_of": "2026-06-30",
                "source_status": "partial",
                "lines": {
                    "fixed_assets_net": {
                        "amount": "1500000.00",
                        "source_status": "partial",
                        "source_key": "onec_bp_fixed_assets",
                    },
                    "tax_receivables": {
                        "amount": "100000.00",
                        "source_status": "partial",
                        "source_key": "onec_bp_tax_accounting",
                    },
                    "loans_and_interest": {
                        "amount": "70000.00",
                        "source_status": "partial",
                        "source_key": "onec_bp_loans",
                    },
                    "owner_capital": {
                        "amount": "10000.00",
                        "source_status": "partial",
                        "source_key": "onec_bp_owner_capital",
                    },
                    "other_liabilities": {
                        "amount": None,
                        "source_status": "source_unverified",
                        "source_key": "onec_bp_other_liabilities",
                    },
                },
                "excluded": [{"family": "retained_earnings_accounting"}],
            }
        ),
        encoding="utf-8",
    )

    lines, summary = balance_service._load_bp_balance_lines(
        balance_date=date(2026, 6, 30),
        snapshot_path=str(path),
    )

    amounts = {line.key: line.amount for line in lines}
    assert amounts["fixed_assets_net"] == Decimal("1500000.00")
    assert amounts["tax_receivables"] == Decimal("100000.00")
    assert amounts["loans_and_interest"] == Decimal("70000.00")
    assert amounts["owner_capital"] == Decimal("10000.00")
    assert amounts["other_liabilities"] is None
    assert summary["status"] == "partial"
    assert summary["as_of"] == "2026-06-30"


def test_opening_equity_contract_is_frozen_and_exposes_versioned_bridge(
    tmp_path: Path,
) -> None:
    path = tmp_path / "opening-equity.json"
    path.write_text(
        json.dumps(
            {
                "contract_version": "management-opening-equity-snapshot.v1",
                "baseline_date": "2026-01-01",
                "source_cutoff_date": "2025-12-31",
                "version": 2,
                "source_hash": "a" * 64,
                "source_status": "partial",
                "calculation_method": (
                    "assets_minus_liabilities_minus_known_equity_at_frozen_baseline"
                ),
                "lines": {
                    "retained_earnings": {
                        "amount": "300000000.00",
                        "source_status": "partial",
                        "source_key": "management_opening_equity",
                    },
                    "prior_period_adjustments": {
                        "amount": "125.50",
                        "source_status": "partial",
                        "source_key": "management_opening_equity",
                    },
                },
                "bridge": {"imbalance_amount": "0.00"},
                "components": [
                    {
                        "section": "asset",
                        "key": "tax_receivables",
                        "label": "Налоги к возмещению",
                        "amount": "1286476.07",
                        "source_status": "partial",
                        "source_key": "onec_bp_tax_accounting",
                    },
                    {
                        "section": "liability",
                        "key": "loans_and_interest",
                        "label": "Займы и проценты",
                        "amount": "60000.00",
                        "source_status": "partial",
                        "source_key": "onec_bp_loans",
                    },
                ],
                "control": {"daily_balancing_forbidden": True},
            }
        ),
        encoding="utf-8",
    )

    lines, summary = balance_service._load_opening_equity_lines(
        balance_date=date(2026, 7, 23),
        snapshot_path=str(path),
    )

    amounts = {line.key: line.amount for line in lines}
    assert amounts == {
        "retained_earnings": Decimal("300000000.00"),
        "prior_period_adjustments": Decimal("125.50"),
    }
    assert all(line.source_as_of == date(2026, 1, 1) for line in lines)
    assert summary["baseline_date"] == "2026-01-01"
    assert summary["source_cutoff_date"] == "2025-12-31"
    assert summary["version"] == 2
    assert summary["source_hash"] == "a" * 64
    assert summary["daily_balancing_forbidden"] is True


def test_opening_equity_replays_all_historical_components_on_baseline(
    tmp_path: Path,
) -> None:
    path = tmp_path / "opening-equity.json"
    path.write_text(
        json.dumps(
            {
                "baseline_date": "2026-01-01",
                "source_cutoff_date": "2025-12-31",
                "version": 1,
                "source_hash": "b" * 64,
                "source_status": "partial",
                "lines": {
                    "retained_earnings": {
                        "amount": "300000000.00",
                        "source_status": "partial",
                    },
                    "prior_period_adjustments": {
                        "amount": "0.00",
                        "source_status": "partial",
                    },
                },
                "components": [
                    {
                        "section": "asset",
                        "key": "cash",
                        "label": "Деньги",
                        "amount": "1000000.00",
                        "source_status": "ready",
                        "source_key": "onec_ut_money_places",
                        "as_of": "2025-12-31",
                    },
                    {
                        "section": "asset",
                        "key": "tax_receivables",
                        "label": "Налоги к возмещению",
                        "amount": "1286476.07",
                        "source_status": "partial",
                        "source_key": "onec_bp_tax_accounting",
                        "as_of": "2025-12-31",
                    },
                    {
                        "section": "liability",
                        "key": "loans_and_interest",
                        "label": "Займы и проценты",
                        "amount": "60000.00",
                        "source_status": "partial",
                        "source_key": "onec_bp_loans",
                        "as_of": "2025-12-31",
                    },
                ],
                "control": {"daily_balancing_forbidden": True},
            }
        ),
        encoding="utf-8",
    )

    lines, _summary = balance_service._load_opening_equity_lines(
        balance_date=date(2026, 1, 1),
        snapshot_path=str(path),
    )

    amounts = {line.key: line.amount for line in lines}
    assert amounts["cash"] == Decimal("1000000.00")
    assert amounts["tax_receivables"] == Decimal("1286476.07")
    assert amounts["loans_and_interest"] == Decimal("60000.00")
    historical = [line for line in lines if line.key in {"cash", "tax_receivables"}]
    assert {line.source_as_of for line in historical} == {date(2025, 12, 31)}


def test_opening_equity_is_not_applied_before_baseline(tmp_path: Path) -> None:
    lines, summary = balance_service._load_opening_equity_lines(
        balance_date=date(2025, 12, 31),
        snapshot_path=str(tmp_path / "missing.json"),
    )

    assert lines == []
    assert summary["status"] == "not_applicable"


def test_baseline_replay_drops_live_amounts_not_present_in_frozen_contract() -> None:
    lines = [
        BalanceLineDraft(
            "liability",
            "accrued_service_liability",
            "Live начисление услуг",
            Decimal("10.00"),
            10,
            "management_service_accruals",
            "ready",
            date(2026, 1, 1),
        ),
        BalanceLineDraft(
            "liability",
            "salary_blocker",
            "Неподтверждённая зарплата",
            None,
            20,
            "ut_bp_salary_reconciliation",
            "source_missing",
            None,
        ),
    ]
    opening_lines = [
        BalanceLineDraft(
            "liability",
            "service_liability",
            "Услуги по факту закрытия",
            Decimal("10.00"),
            10,
            "onec_ut_legal_entity_settlements",
            "ready",
            date(2025, 12, 31),
        )
    ]

    merged = balance_service._merge_opening_equity_lines(
        lines=lines,
        opening_lines=opening_lines,
        balance_date=date(2026, 1, 1),
    )

    assert {line.key for line in merged} == {"salary_blocker", "service_liability"}
    assert sum(
        (line.amount for line in merged if line.amount is not None and line.include_in_total),
        Decimal("0"),
    ) == Decimal("10.00")


def test_components_export_avoids_opening_equity_recursion(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_build(
        _session: Session,
        *,
        balance_date: date,
        access_context: object,
        include_contract_enrichment: bool,
    ) -> tuple[list[BalanceLineDraft], dict[str, object]]:
        captured.update(
            {
                "balance_date": balance_date,
                "access_context": access_context,
                "include_contract_enrichment": include_contract_enrichment,
            }
        )
        return _complete_lines()

    monkeypatch.setattr(balance_service, "_build_draft_lines", fake_build)

    payload = balance_service.build_management_balance_components_export(
        db_session,
        balance_date=date(2026, 6, 30),
    )

    assert captured["balance_date"] == date(2026, 6, 30)
    assert captured["include_contract_enrichment"] is False
    assert payload["as_of"] == "2026-06-30"
    assert payload["totals"]["pre_opening_imbalance"] == "0.00"
    assert len(payload["source_hash"]) == 64


def test_salary_snapshot_replaces_net_employee_line_with_gross_articles(
    tmp_path: Path,
) -> None:
    path = tmp_path / "salary.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "as_of": "2026-07-13",
                "source_status": "partial",
                "lines": {
                    "official_salary_payable": {
                        "label": "Зарплата к выплате — официальная",
                        "amount": "0.00",
                        "source_status": "ready",
                        "source_key": "onec_bp_account70",
                    },
                    "management_salary_payable": {
                        "label": "Зарплата к выплате — управленческая часть",
                        "amount": "0.00",
                        "source_status": "partial",
                        "source_key": "ut_bp_salary_reconciliation",
                    },
                    "other_employee_settlements": {
                        "label": "Прочие расчёты с сотрудниками",
                        "amount": "1546.58",
                        "source_status": "ready",
                        "source_key": "onec_ut_employee_settlements",
                    },
                    "service_employee_settlements": {
                        "label": "Служебные расчёты с сотрудниками",
                        "amount": "126120.62",
                        "source_status": "ready",
                        "source_key": "onec_ut_employee_settlements",
                    },
                    "official_salary_advances": {
                        "label": "Авансы/переплата по официальной зарплате",
                        "amount": "536426.45",
                        "source_status": "ready",
                        "source_key": "onec_bp_account70",
                    },
                    "employee_receivables": {
                        "label": "Дебиторка сотрудников",
                        "amount": "3567689.49",
                        "source_status": "ready",
                        "source_key": "onec_ut_employee_settlements",
                    },
                },
                "control": {
                    "closing_blocked": True,
                    "blockers": ["employee_mapping_incomplete"],
                    "mapping": {"approved_count": 0, "coverage_percent": "0.00"},
                    "unconfirmed_amount": "4301900.00",
                    "duplicate_payment_amount": "0.00",
                    "ambiguous_payment_amount": "0.00",
                    "ambiguous_duplicate_count": 0,
                    "account70_reconciliation_difference": None,
                },
            }
        ),
        encoding="utf-8",
    )

    lines, summary = balance_service._load_salary_reconciliation_lines(
        balance_date=date(2026, 7, 13),
        snapshot_path=str(path),
    )

    amounts = {line.key: line.amount for line in lines}
    assert amounts["official_salary_advances"] == Decimal("536426.45")
    assert amounts["employee_receivables"] == Decimal("3567689.49")
    assert amounts["other_employee_settlements"] == Decimal("1546.58")
    assert amounts["service_employee_settlements"] == Decimal("126120.62")
    assert summary["status"] == "partial"
    assert summary["closing_blocked"] is True

    errors = balance_service._validation_errors(
        _complete_lines()[0],
        {"salary_reconciliation": summary},
    )
    assert any(error["code"] == "salary_reconciliation_incomplete" for error in errors)


def test_owner_dividends_reduce_equity_from_cashflow_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "owner-cash-control.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "as_of": "2026-07-12",
                "source_status": "partial",
                "summary": {
                    "dividends_ytd": "13415228.19",
                    "dividends_current_month": "100000.00",
                    "dividend_comment_warning_count": 46,
                },
            }
        ),
        encoding="utf-8",
    )

    line = balance_service._load_owner_dividends_line(
        balance_date=date(2026, 7, 13),
        snapshot_path=str(path),
        accounting_includes_dividends=False,
        max_lag_days=1,
    )

    assert line.section == "equity"
    assert line.key == "dividends_paid_ytd"
    assert line.amount == Decimal("-13415228.19")
    assert line.adjustment_amount == Decimal("-100000.00")
    assert line.source_amount == Decimal("13415228.19")
    assert line.source_key == "management_owner_cash_control"
    assert line.source_status == "partial"
    assert line.source_as_of == date(2026, 7, 12)
    assert line.include_in_total is True
    assert line.recognition_method == "equity_distribution"
    assert "46 РКО" in str(line.note)


def test_owner_dividends_are_informational_when_accounting_already_includes_them(
    tmp_path: Path,
) -> None:
    path = tmp_path / "owner-cash-control.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "as_of": "2026-07-13",
                "source_status": "ready",
                "summary": {
                    "dividends_ytd": "500000.00",
                    "dividends_current_month": "0.00",
                },
            }
        ),
        encoding="utf-8",
    )

    line = balance_service._load_owner_dividends_line(
        balance_date=date(2026, 7, 13),
        snapshot_path=str(path),
        accounting_includes_dividends=True,
        max_lag_days=1,
    )

    assert line.amount == Decimal("-500000.00")
    assert line.include_in_total is False
    assert "информационно" in str(line.note)


def test_operational_snapshot_keeps_missing_sources_unknown(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    monkeypatch.setattr(balance_service, "get_settings", lambda: settings)
    monkeypatch.setattr(executive_dashboard, "get_settings", lambda: settings)
    response = get_management_balance(
        db_session,
        month=None,
        view="operational",
        access_context=bitrix_executive_dashboard_auth.full_executive_dashboard_context(),
    )

    inventory = next(line for line in response.assets if line.key == "inventory_cost")
    assert inventory.amount is None
    assert inventory.source_status in {"source_missing", "source_unverified"}
    assert response.status == "draft"
    assert response.can_close is False
    assert response.validation_errors


def test_closed_snapshot_is_immutable_and_close_is_idempotent(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    monkeypatch.setattr(balance_service, "get_settings", lambda: settings)
    monkeypatch.setattr(
        balance_service, "_build_draft_lines", lambda *args, **kwargs: _complete_lines()
    )
    first = build_and_persist_management_balance_snapshot(
        db_session,
        balance_date=date(2026, 6, 30),
        view="closed",
        actor="test:builder",
    )

    closed = close_management_balance(
        db_session,
        month="2026-06",
        actor="finance:42",
        confirm=True,
        note="Сверено вручную",
    )
    repeated = close_management_balance(
        db_session,
        month="2026-06",
        actor="finance:77",
        confirm=True,
        note="Повторный вызов",
    )

    assert first.version == 1
    assert closed.status == "closed"
    assert repeated.version == 1
    persisted = db_session.get(ExecutiveManagementBalanceSnapshot, first.id)
    assert persisted is not None
    assert persisted.closed_by == "finance:42"
    audit_count = db_session.scalar(
        select(func.count(ExecutiveManagementBalanceAudit.id)).where(
            ExecutiveManagementBalanceAudit.snapshot_id == first.id,
            ExecutiveManagementBalanceAudit.action == "closed",
        )
    )
    assert audit_count == 1


def test_opening_boundary_can_be_persisted_as_closed_draft(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    monkeypatch.setattr(balance_service, "get_settings", lambda: settings)
    monkeypatch.setattr(
        balance_service, "_build_draft_lines", lambda *args, **kwargs: _complete_lines()
    )

    snapshot = build_and_persist_management_balance_snapshot(
        db_session,
        balance_date=date(2026, 1, 1),
        view="closed",
        actor="test:opening-builder",
    )

    assert snapshot.period_month == date(2026, 1, 1)
    assert snapshot.balance_date == date(2026, 1, 1)
    assert snapshot.view_mode == "closed"
    assert snapshot.validation_errors == []


def test_correction_after_close_creates_new_draft_version(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    monkeypatch.setattr(balance_service, "get_settings", lambda: settings)
    monkeypatch.setattr(
        balance_service, "_build_draft_lines", lambda *args, **kwargs: _complete_lines()
    )
    first = build_and_persist_management_balance_snapshot(
        db_session, balance_date=date(2026, 6, 30), view="closed"
    )
    close_management_balance(
        db_session, month="2026-06", actor="finance:42", confirm=True, note=None
    )
    second = build_and_persist_management_balance_snapshot(
        db_session, balance_date=date(2026, 6, 30), view="closed"
    )

    assert first.version == 1
    assert second.version == 2
    assert second.status == "draft"
    assert db_session.get(ExecutiveManagementBalanceSnapshot, first.id).status == "closed"


def test_close_rejects_balance_with_missing_source(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    monkeypatch.setattr(balance_service, "get_settings", lambda: settings)
    monkeypatch.setattr(
        balance_service,
        "_build_draft_lines",
        lambda *args, **kwargs: (
            [
                BalanceLineDraft(
                    "asset",
                    "inventory_cost",
                    "Товар",
                    None,
                    10,
                    "onec_inventory_cost",
                    "source_missing",
                    None,
                )
            ],
            {},
        ),
    )
    build_and_persist_management_balance_snapshot(
        db_session, balance_date=date(2026, 6, 30), view="closed"
    )
    with pytest.raises(ManagementBalanceCloseError):
        close_management_balance(
            db_session, month="2026-06", actor="finance:42", confirm=True, note=None
        )


def test_management_balance_api_is_independent_from_dashboard_date(
    client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    monkeypatch.setattr(balance_service, "get_settings", lambda: settings)
    monkeypatch.setattr(bitrix_executive_dashboard_auth, "get_settings", lambda: settings)
    monkeypatch.setattr(executive_dashboard, "get_settings", lambda: settings)
    app.dependency_overrides[get_db] = lambda: db_session
    try:
        response = client.get(
            "/api/management/executive-dashboard/management-balance?view=operational",
            headers={"Authorization": "Bearer secret-token"},
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["month"] == date.today().strftime("%Y-%m")
    assert "date" not in payload
    assert payload["can_close"] is False
