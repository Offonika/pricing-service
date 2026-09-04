from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import func, select

from app.models import LogisticsManualReview, SiteOrderExecutionCase
from app.services import site_order_fulfillment as fulfillment
from app.services import site_order_state_projection as projection
from tasks import refresh_site_order_crm_projection as task


class FakeBitrixClient:
    def __init__(self, pages: list[dict]) -> None:
        self.pages = list(pages)
        self.calls: list[tuple[str, dict]] = []

    def call(self, method: str, params: dict) -> dict:
        self.calls.append((method, params))
        return self.pages.pop(0)


def _deal(deal_id: int, order_number: str, modified_at: str) -> dict:
    return {
        "ID": str(deal_id),
        "STAGE_ID": "EXECUTING",
        fulfillment.CRM_ORDER_NUMBER_FIELD: order_number,
        fulfillment.CRM_DELIVERY_FIELD: "Самовывоз",
        fulfillment.CRM_PAYMENT_FIELD: "1",
        "DATE_MODIFY": modified_at,
    }


def test_incremental_projection_uses_overlap_filter_and_limit() -> None:
    client = FakeBitrixClient(
        [
            {
                "result": [
                    _deal(1, "245001", "2026-09-04T11:55:00+03:00"),
                    _deal(2, "245002", "2026-09-04T11:56:00+03:00"),
                ],
                "next": 50,
            }
        ]
    )
    modified_since = datetime.fromisoformat("2026-09-04T11:50:00+03:00")

    facts = task.fetch_projection_facts(client, modified_since=modified_since, limit=2)

    assert [item.site_order_number for item in facts] == ["245001", "245002"]
    assert len(client.calls) == 1
    params = client.calls[0][1]
    assert params["filter"][">=DATE_MODIFY"] == modified_since.isoformat()


def test_full_projection_reads_all_pages_without_incremental_limit() -> None:
    client = FakeBitrixClient(
        [
            {"result": [_deal(1, "245001", "2026-09-04T11:55:00")], "next": 50},
            {"result": [_deal(2, "245002", "2026-09-04T11:56:00")]},
        ]
    )

    facts = task.fetch_projection_facts(client, modified_since=None, limit=None)

    assert [item.site_order_number for item in facts] == ["245001", "245002"]
    assert [call[1]["start"] for call in client.calls] == [0, 50]
    assert all(">=DATE_MODIFY" not in call[1]["filter"] for call in client.calls)


def test_projection_enrichment_adds_site_payment_and_onec_debt(monkeypatch) -> None:
    fact = projection.CrmProjectionFact(
        site_order_number="245383",
        bitrix_deal_id=101,
        crm_stage="EXECUTING",
        delivery_method="pickup",
        raw_delivery_method="Самовывоз",
        payment_state="unconfirmed",
        modified_at=datetime(2026, 9, 4, 11, 0),
    )
    monkeypatch.setattr(
        task.fulfillment_sync,
        "fetch_sale_order_statuses",
        lambda orders: {"245383": SimpleNamespace(status_id="F", payed=True, canceled=False)},
    )
    monkeypatch.setattr(
        task.fulfillment_sync,
        "fetch_onec_order_settlements",
        lambda orders: {
            "245383": SimpleNamespace(
                payment_confirmed=False,
                payment_amount=Decimal("1000.00"),
                debt_amount=Decimal("560.00"),
            )
        },
    )

    enriched = task.enrich_projection_facts([fact])

    assert enriched[0].payment_state == "paid"
    assert enriched[0].payment_amount == Decimal("1000.00")
    assert enriched[0].debt_amount == Decimal("560.00")
    assert enriched[0].site_status == "F"
    assert enriched[0].site_paid is True


def test_duplicate_crm_projection_creates_one_idempotent_review(db_session) -> None:
    facts = [
        projection.CrmProjectionFact(
            site_order_number="245383",
            bitrix_deal_id=101,
            crm_stage="EXECUTING",
            delivery_method="pickup",
            raw_delivery_method="Самовывоз",
            payment_state="paid",
            modified_at=datetime(2026, 9, 4, 11, 0),
        ),
        projection.CrmProjectionFact(
            site_order_number="245383",
            bitrix_deal_id=102,
            crm_stage="DISMANTLING",
            delivery_method="pickup",
            raw_delivery_method="Самовывоз",
            payment_state="paid",
            modified_at=datetime(2026, 9, 4, 12, 0),
            payment_amount=Decimal("1500.00"),
            debt_amount=Decimal("0.00"),
            site_status="F",
            site_paid=True,
            site_canceled=False,
        ),
    ]

    first = projection.upsert_crm_projection(
        db_session,
        facts,
        observed_at=datetime(2026, 9, 4, 12, 5),
    )
    second = projection.upsert_crm_projection(
        db_session,
        facts,
        observed_at=datetime(2026, 9, 4, 12, 10),
    )
    db_session.commit()

    case = db_session.scalar(
        select(SiteOrderExecutionCase).where(SiteOrderExecutionCase.site_order_number == "245383")
    )
    review_count = db_session.scalar(select(func.count(LogisticsManualReview.id)))
    assert first == {"created": 1, "updated": 0, "review": 1}
    assert second == {"created": 0, "updated": 1, "review": 0}
    assert case is not None
    assert case.bitrix_deal_id is None
    assert case.current_crm_stage == "DISMANTLING"
    assert case.payload["crm_projection"]["debt_amount"] == "0.00"
    assert case.payload["crm_projection"]["site_status"] == "F"
    assert review_count == 1
