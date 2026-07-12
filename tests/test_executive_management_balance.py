from __future__ import annotations

from datetime import date
from decimal import Decimal

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
from app.services import bitrix_executive_dashboard_auth
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
        management_internal_api_token="secret-token",
        executive_dashboard_finance_snapshot_path="/tmp/missing-finance-snapshot.json",
        executive_dashboard_cashflow_period_cache_path="/tmp/missing-cashflow-cache.json",
        executive_dashboard_warehouse_snapshot_path="/tmp/missing-warehouse-snapshot.json",
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
        ],
        {"accounting": {"configured": True, "status": "ready"}},
    )


def test_month_parser_and_leap_year_end() -> None:
    assert parse_month("2024-02") == date(2024, 2, 1)
    assert month_end(date(2024, 2, 1)) == date(2024, 2, 29)
    with pytest.raises(ValueError):
        parse_month("2024-13")


def test_operational_snapshot_keeps_missing_sources_unknown(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    monkeypatch.setattr(balance_service, "get_settings", lambda: settings)
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
