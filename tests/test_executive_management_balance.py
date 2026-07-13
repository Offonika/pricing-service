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
