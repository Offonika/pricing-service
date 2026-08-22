from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.dependencies import get_db
from app.api.receivable_workplace import (
    ReceivableWorkplaceAuthContext,
    require_receivable_workplace_access,
)
from app.main import app
from app.models import ReceivableCase, ReceivablePkoShadowResult
from app.services.receivable_document_structure import (
    DOCUMENT_STRUCTURE_CONFIRMED_OPEN,
    ReceivableDocumentStructureCheck,
)
from app.services.receivable_pko_shadow import (
    PKO_SHADOW_DATA_QUALITY,
    PKO_SHADOW_MATCHED,
    PKO_SHADOW_NO_CANDIDATE,
    ReceivableReturnSaleLink,
    rebuild_receivable_pko_shadow,
    resolve_receivable_pko_shadow,
)
from app.services.receivable_statement_debt import ReceivableStatementEvent

BASE_DATE = datetime(2025, 1, 1, 10, 0)


def _event(
    offset: int,
    event_type: str,
    ref: str,
    amount: str,
    *,
    counterparty_ref: str = "cp-1",
    number: str | None = None,
    manager_name: str | None = None,
) -> ReceivableStatementEvent:
    return ReceivableStatementEvent(
        counterparty_ref=counterparty_ref,
        event_type=event_type,
        document_ref=ref,
        document_number=number or ref,
        document_date=BASE_DATE + timedelta(days=offset),
        amount_delta=Decimal(amount),
        manager_ref=f"manager-{ref}" if event_type == "sale" else None,
        manager_name=manager_name,
        line_no=offset,
        source_layer="regular_receivables",
    )


def test_pko_shadow_keeps_partial_boundary_sale_after_large_payment() -> None:
    result = resolve_receivable_pko_shadow(
        [
            _event(1, "sale", "sale-1", "1000"),
            _event(2, "sale", "sale-2", "1000", manager_name="Мария"),
            _event(3, "payment", "pko-1", "-1500"),
        ],
        current_balance=Decimal("500"),
    )

    assert result.status == PKO_SHADOW_MATCHED
    assert result.base_payment_ref == "pko-1"
    assert result.base_balance_after == Decimal("500.00")
    assert [(item.document_ref, item.open_amount) for item in result.documents] == [
        ("sale-2", Decimal("500.00"))
    ]


def test_pko_shadow_zero_balance_starts_clean_cycle() -> None:
    result = resolve_receivable_pko_shadow(
        [
            _event(1, "sale", "sale-old", "1000"),
            _event(2, "payment", "pko-zero", "-1000"),
            _event(3, "sale", "sale-new", "400"),
        ],
        current_balance=Decimal("400"),
    )

    assert result.status == PKO_SHADOW_MATCHED
    assert result.base_payment_ref == "pko-zero"
    assert [item.document_ref for item in result.documents] == ["sale-new"]


def test_pko_shadow_overpayment_reduces_next_sale() -> None:
    result = resolve_receivable_pko_shadow(
        [
            _event(1, "sale", "sale-old", "1000"),
            _event(2, "payment", "pko-over", "-1200"),
            _event(3, "sale", "sale-new", "500"),
        ],
        current_balance=Decimal("300"),
    )

    assert result.status == PKO_SHADOW_MATCHED
    assert [(item.document_ref, item.open_amount) for item in result.documents] == [
        ("sale-new", Decimal("300.00"))
    ]


def test_pko_shadow_applies_exact_return_to_linked_sale() -> None:
    result = resolve_receivable_pko_shadow(
        [
            _event(1, "sale", "sale-1", "1000"),
            _event(2, "payment", "pko-1", "-200"),
            _event(3, "return", "return-1", "-300"),
        ],
        current_balance=Decimal("500"),
        return_links={
            "return-1": ReceivableReturnSaleLink(
                return_document_ref="return-1",
                status="confirmed",
                sale_document_ref="sale-1",
                basis_ref="sale-1",
                basis_kind="sale",
            )
        },
    )

    assert result.status == PKO_SHADOW_MATCHED
    assert result.documents[0].open_amount == Decimal("500.00")


def test_pko_shadow_does_not_guess_unconfirmed_return() -> None:
    result = resolve_receivable_pko_shadow(
        [
            _event(1, "sale", "sale-1", "1000"),
            _event(2, "payment", "pko-1", "-200"),
            _event(3, "return", "return-1", "-300"),
        ],
        current_balance=Decimal("500"),
        return_links={
            "return-1": ReceivableReturnSaleLink(
                return_document_ref="return-1",
                status="ambiguous",
                basis_ref="order-1",
                basis_kind="order",
            )
        },
    )

    assert result.status == PKO_SHADOW_DATA_QUALITY
    assert result.reason == "unconfirmed_return_link"
    assert result.documents == ()


def test_pko_shadow_applies_additional_payment_after_base_pko() -> None:
    result = resolve_receivable_pko_shadow(
        [
            _event(1, "sale", "sale-1", "1000"),
            _event(2, "payment", "pko-base", "-200"),
            _event(3, "sale", "sale-2", "500"),
            _event(4, "payment", "pko-next", "-600"),
        ],
        current_balance=Decimal("700"),
    )

    assert result.status == PKO_SHADOW_MATCHED
    assert result.base_payment_ref == "pko-next"
    assert [(item.document_ref, item.open_amount) for item in result.documents] == [
        ("sale-1", Decimal("200.00")),
        ("sale-2", Decimal("500.00")),
    ]


def test_pko_shadow_accepts_exact_one_kopeck_difference() -> None:
    result = resolve_receivable_pko_shadow(
        [
            _event(1, "sale", "sale-1", "1000"),
            _event(2, "payment", "pko-1", "-1"),
        ],
        current_balance=Decimal("998.99"),
    )

    assert result.status == PKO_SHADOW_MATCHED
    assert result.delta == Decimal("0.01")


def test_pko_shadow_prefers_confirmed_structure() -> None:
    sale = _event(1, "sale", "sale-1", "1000")
    check = ReceivableDocumentStructureCheck(
        document_ref="sale-1",
        status=DOCUMENT_STRUCTURE_CONFIRMED_OPEN,
        open_amount=Decimal("300"),
        sale_amount=Decimal("1000"),
        closing_amount=Decimal("-700"),
        sale_number="РТУ-1",
        sale_date=sale.document_date,
        order_ref="order-1",
        order_number="ЗАК-1",
        order_date=sale.document_date,
        linked_documents=(),
    )
    result = resolve_receivable_pko_shadow(
        [sale, _event(2, "payment", "pko-1", "-700")],
        current_balance=Decimal("300"),
        structure_checks={"sale-1": check},
    )

    assert result.status == PKO_SHADOW_MATCHED
    assert result.reason == "structure_confirmed"
    assert result.documents[0].selection_rule == "pko_shadow_structure_confirmed"


def test_pko_shadow_control_counterparty_rb025702_keeps_old_origin() -> None:
    result = resolve_receivable_pko_shadow(
        [
            _event(1, "sale", "sale-2025", "1000", counterparty_ref="РБ025702"),
            _event(2, "payment", "pko-2025", "-400", counterparty_ref="РБ025702"),
            _event(370, "sale", "sale-2026", "500", counterparty_ref="РБ025702"),
            _event(371, "payment", "pko-2026", "-200", counterparty_ref="РБ025702"),
        ],
        current_balance=Decimal("900"),
    )

    assert result.status == PKO_SHADOW_MATCHED
    assert result.documents[0].document_ref == "sale-2025"
    assert result.documents[0].open_amount == Decimal("400.00")


def test_pko_shadow_reports_no_candidate_without_pko() -> None:
    result = resolve_receivable_pko_shadow(
        [_event(1, "sale", "sale-1", "1000")],
        current_balance=Decimal("1000"),
    )

    assert result.status == PKO_SHADOW_NO_CANDIDATE
    assert result.reason == "no_pko_in_statement"


def _shadow_row(as_of: date) -> ReceivablePkoShadowResult:
    return ReceivablePkoShadowResult(
        snapshot_date=as_of,
        algorithm_version="pko-shadow-v1",
        run_id="run-1",
        counterparty_ref="cp-1",
        counterparty_code="РБ025702",
        counterparty_name="Контрольный клиент",
        department_ref="dep-1",
        department_name="Горбушка",
        current_balance=Decimal("500"),
        base_payment_ref="pko-1",
        base_payment_number="ПКО-1",
        base_payment_date=BASE_DATE,
        base_balance_after=Decimal("500"),
        current_origin_document_ref="sale-old",
        current_origin_document_number="РТУ-OLD",
        current_origin_document_date=BASE_DATE,
        candidate_origin_document_ref="sale-new",
        candidate_origin_document_number="РТУ-NEW",
        candidate_origin_document_date=BASE_DATE,
        candidate_responsible_ref="manager-1",
        candidate_responsible_name="Мария",
        candidate_origin_open_amount=Decimal("500"),
        selected_open_amount=Decimal("500"),
        delta=Decimal("0"),
        status="matched",
        reason="pko_cycle_matched",
        current_documents=[],
        candidate_documents=[],
        diagnostics={},
        computed_at=BASE_DATE,
    )


def test_pko_shadow_rebuild_replaces_same_snapshot_version_atomically(
    monkeypatch,
    db_session: Session,
) -> None:
    from app.services import receivable_pko_shadow as shadow_service

    as_of = date(2026, 8, 10)
    db_session.add(
        ReceivableCase(
            snapshot_date=as_of,
            segment="buyers",
            owner_type="current_manager",
            recommendation="Проверить",
            counterparty_ref="cp-1",
            counterparty_code="РБ025702",
            counterparty_name="Контрольный клиент",
            current_balance=Decimal("500"),
            aged_bucket="1-30",
            activity_segment="active",
            origin_document_ref="sale-1",
            origin_document_number="РТУ-1",
            origin_document_date=BASE_DATE,
            current_manager_ref="manager-1",
            current_manager_name="Мария",
            department_ref="dep-1",
            department_name="Горбушка",
            is_overdue=True,
        )
    )
    db_session.commit()
    events = [
        _event(1, "sale", "sale-1", "1000", manager_name="Мария"),
        _event(2, "payment", "pko-1", "-500"),
    ]
    monkeypatch.setattr(
        shadow_service,
        "fetch_counterparty_ledger_statement_events",
        lambda *args, **kwargs: {"cp-1": events},
    )
    monkeypatch.setattr(
        shadow_service,
        "fetch_receivable_document_structure_checks",
        lambda *args, **kwargs: {},
    )
    monkeypatch.setattr(
        shadow_service,
        "fetch_receivable_return_sale_links",
        lambda *args, **kwargs: {},
    )
    monkeypatch.setattr(
        shadow_service,
        "fetch_sale_document_departments",
        lambda *args, **kwargs: {},
    )

    first = rebuild_receivable_pko_shadow(
        db_session,
        onec_engine=object(),
        snapshot_date=as_of,
    )
    db_session.commit()
    second = rebuild_receivable_pko_shadow(
        db_session,
        onec_engine=object(),
        snapshot_date=as_of,
    )
    db_session.commit()

    rows = db_session.query(ReceivablePkoShadowResult).all()
    assert len(rows) == 1
    assert rows[0].status == PKO_SHADOW_MATCHED
    assert first["run_id"] != second["run_id"]
    assert rows[0].run_id == second["run_id"]


def test_pko_shadow_api_is_full_access_bitrix_only(db_session: Session) -> None:
    as_of = date(2026, 8, 10)
    db_session.add(_shadow_row(as_of))
    db_session.commit()

    def override_db():
        yield db_session

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    try:
        for forbidden_access in (
            ReceivableWorkplaceAuthContext(
                actor="internal:management",
                source="internal",
                access_level="full",
            ),
            ReceivableWorkplaceAuthContext(
                actor="bitrix:77",
                source="bitrix",
                access_level="department",
                department_refs=frozenset({"dep-1"}),
                user_id="77",
            ),
        ):
            app.dependency_overrides[require_receivable_workplace_access] = (
                lambda access=forbidden_access: access
            )
            response = client.get(
                "/api/receivables/workplace/pko-shadow",
                params={"date": as_of.isoformat()},
            )
            assert response.status_code == 403

        app.dependency_overrides[require_receivable_workplace_access] = lambda: (
            ReceivableWorkplaceAuthContext(
                actor="bitrix:42",
                source="bitrix",
                access_level="full",
                user_id="42",
            )
        )
        response = client.get(
            "/api/receivables/workplace/pko-shadow",
            params={"date": as_of.isoformat()},
        )
    finally:
        app.dependency_overrides = {}

    assert response.status_code == 200
    body = response.json()
    assert body["summary"]["matched_count"] == 1
    assert body["payload"][0]["counterparty_code"] == "РБ025702"
    assert body["payload"][0]["candidate_responsible_name"] == "Мария"
