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
from app.models import ExecutiveServiceAccrualAudit, ExecutiveServiceAccrualEntry
from app.services import bitrix_executive_dashboard_auth
from app.services import executive_dashboard as dashboard_service
from app.services.executive_service_accruals import (
    ServiceAccrualSourceError,
    service_accrual_balance_adjustments,
    service_accrual_profit_loss_summary,
    sync_service_accruals,
)

COUNTERPARTY = "0x11111111111111111111111111111111"
CONTRACT = "0x22222222222222222222222222222222"


def _source(
    path: Path,
    *,
    payment: Decimal = Decimal("100.00"),
    documents: list[dict[str, str]] | None = None,
    documents_status: str = "source_unverified",
) -> None:
    payload = {
        "schema_version": 1,
        "generated_at": "2026-07-12T08:42:00+00:00",
        "as_of": "2026-07-12",
        "source_status": "ready",
        "rules_hash": "a" * 64,
        "rules": [
            {
                "rule_key": "rent-main",
                "version": 1,
                "counterparty_ref": COUNTERPARTY,
                "counterparty_name": "Арендодатель",
                "contract_ref": CONTRACT,
                "contract_name": "Аренда",
                "effective_from": "2026-07-01",
                "effective_to": None,
                "expense_line_key": "rent",
                "expense_line_label": "Аренда",
                "monthly_amount_rub": "100.00",
                "recognition_day": 1,
                "balance_scope_verified": True,
                "active": True,
                "approved_by": "finance",
                "approval_note": "Договор проверен",
            }
        ],
        "payments": [
            {
                "movement_id": "_Document187:payment:1",
                "business_date": "2026-07-05",
                "counterparty_ref": COUNTERPARTY,
                "contract_ref": CONTRACT,
                "amount_rub": str(payment),
            }
        ],
        "closing_documents": documents or [],
        "closing_documents_status": documents_status,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.parametrize("payment", [Decimal("100"), Decimal("80"), Decimal("150")])
def test_estimated_accrual_replaces_contract_cashflow_once(
    db_session: Session,
    tmp_path: Path,
    payment: Decimal,
) -> None:
    path = tmp_path / "source.json"
    _source(path, payment=payment)

    first = sync_service_accruals(db_session, as_of=date(2026, 7, 12), source_path=path)
    second = sync_service_accruals(db_session, as_of=date(2026, 7, 12), source_path=path)
    entry = db_session.scalar(select(ExecutiveServiceAccrualEntry))
    summary = service_accrual_profit_loss_summary(
        db_session,
        date_from=date(2026, 7, 1),
        date_to=date(2026, 7, 31),
    )

    assert first["inserted"] == 1
    assert second["unchanged"] == 1
    assert entry is not None
    assert entry.status == "estimated_without_document"
    assert entry.recognized_amount_rub == Decimal("100.00")
    assert entry.payment_amount_rub == payment.quantize(Decimal("0.01"))
    assert summary["recognized_amount"] == Decimal("100.00")
    assert summary["cashflow_replaced_amount"] == payment.quantize(Decimal("0.01"))
    assert db_session.scalar(select(func.count(ExecutiveServiceAccrualEntry.id))) == 1


def test_late_closing_document_replaces_estimate_and_removes_balance_adjustment(
    db_session: Session,
    tmp_path: Path,
) -> None:
    path = tmp_path / "source.json"
    _source(path)
    sync_service_accruals(db_session, as_of=date(2026, 7, 12), source_path=path)
    assert service_accrual_balance_adjustments(db_session, as_of=date(2026, 7, 12))[
        "amount"
    ] == Decimal("100.00")

    _source(
        path,
        documents=[
            {
                "document_ref": "0x33333333333333333333333333333333",
                "service_month": "2026-07-01",
                "contract_ref": CONTRACT,
                "amount_rub": "120.00",
            }
        ],
        documents_status="ready",
    )
    result = sync_service_accruals(db_session, as_of=date(2026, 7, 12), source_path=path)
    entry = db_session.scalar(select(ExecutiveServiceAccrualEntry))

    assert result["updated"] == 1
    assert entry is not None
    assert entry.status == "actual_document"
    assert entry.recognized_amount_rub == Decimal("120.00")
    assert service_accrual_balance_adjustments(db_session, as_of=date(2026, 7, 12))[
        "amount"
    ] == Decimal("0.00")
    assert db_session.scalar(select(func.count(ExecutiveServiceAccrualAudit.id))) == 2


def test_duplicate_payment_is_rejected_without_commit(
    db_session: Session,
    tmp_path: Path,
) -> None:
    path = tmp_path / "source.json"
    _source(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["payments"].append(dict(payload["payments"][0]))
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ServiceAccrualSourceError, match="movement_id"):
        sync_service_accruals(db_session, as_of=date(2026, 7, 12), source_path=path)
    db_session.rollback()

    assert db_session.scalar(select(func.count(ExecutiveServiceAccrualEntry.id))) == 0


def test_empty_rule_set_is_partial_not_ready(
    db_session: Session,
    tmp_path: Path,
) -> None:
    path = tmp_path / "source.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generated_at": "2026-07-12T08:42:00+00:00",
                "as_of": "2026-07-12",
                "source_status": "ready",
                "rules_hash": "a" * 64,
                "rules": [],
                "payments": [],
                "closing_documents": [],
                "closing_documents_status": "source_unverified",
            }
        ),
        encoding="utf-8",
    )

    result = sync_service_accruals(
        db_session,
        as_of=date(2026, 7, 12),
        source_path=path,
    )

    assert result["source_status"] == "partial"
    assert service_accrual_balance_adjustments(
        db_session,
        as_of=date(2026, 7, 12),
    )["source_status"] == "partial"


def test_profit_loss_replaces_cash_payment_with_accrual_without_double_count(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "source.json"
    _source(path, payment=Decimal("150.00"))
    sync_service_accruals(db_session, as_of=date(2026, 7, 12), source_path=path)
    monkeypatch.setattr(
        dashboard_service,
        "_load_cashflow_period_cache",
        lambda: (
            {
                "source_status": "ready",
                "period": {"date_from": "2026-07-01", "date_to": "2026-07-31"},
                "rows": [
                    {
                        "business_date": "2026-07-05",
                        "profit_loss_class": "operating_expense",
                        "profit_loss_line_key": "rent",
                        "profit_loss_line_label": "Аренда",
                        "profit_loss_recognition_method": "cashflow_fallback",
                        "outflow_amount": "150.00",
                        "movement_count": 1,
                        "review_count": 0,
                        "dds_subgroup": "rent",
                        "article_key": "rent",
                    }
                ],
            },
            "ready",
            "test",
        ),
    )

    result = dashboard_service._profit_loss_expenses_from_cashflow_cache(
        session=db_session,
        date_from=date(2026, 7, 1),
        date_to=date(2026, 7, 31),
    )

    rent = result["breakdown"][0]
    assert rent.amount == Decimal("100.00")
    assert rent.cashflow_amount == Decimal("150.00")
    assert rent.recognized_amount == Decimal("100.00")
    assert rent.adjustment_amount == Decimal("-50.00")
    assert result["totals"]["operating_expenses"] == Decimal("100.00")


def test_finance_api_returns_service_accrual_drilldown(
    client: TestClient,
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "source.json"
    _source(path)
    sync_service_accruals(db_session, as_of=date(2026, 7, 12), source_path=path)
    settings = Settings(management_internal_api_token="secret-token")
    monkeypatch.setattr(bitrix_executive_dashboard_auth, "get_settings", lambda: settings)
    app.dependency_overrides[get_db] = lambda: db_session
    try:
        response = client.get(
            "/api/management/executive-dashboard/service-accruals?month=2026-07",
            headers={"Authorization": "Bearer secret-token"},
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["total_count"] == 1
    assert payload["estimated_count"] == 1
    assert payload["items"][0]["note"] == "Оценочно, закрывающие документы отсутствуют"
