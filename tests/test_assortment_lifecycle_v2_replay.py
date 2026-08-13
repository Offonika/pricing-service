from datetime import date, timedelta
from decimal import Decimal

from app.services.assortment_lifecycle import AssortmentStatus, DemandState
from app.services.assortment_lifecycle_v2_policy import DemandStatePolicy
from app.services.assortment_lifecycle_v2_replay import (
    HistoricalReceipt,
    HistoricalSaleObservation,
    HistoricalSupplierOrder,
    build_assortment_lifecycle_v2_trajectory,
)


def _item(**source):
    return {
        "nomenclature_code": "SKU-1",
        "name": "Дисплей",
        "source_record": {"created_at": "2025-01-01", **source},
    }


def _sale(day: date, *, suffix: int, quantity: str = "1") -> HistoricalSaleObservation:
    return HistoricalSaleObservation(
        business_date=day,
        quantity=Decimal(quantity),
        document_id=f"doc-{suffix}",
        customer_id=f"customer-{suffix}",
        sales_point_id=f"point-{suffix}",
    )


def _trajectory(*, sales=(), availability=(), item=None, policy=None, date_to=date(2026, 2, 12)):
    return build_assortment_lifecycle_v2_trajectory(
        items=[item or _item()],
        sales_observations_by_code={"SKU-1": sales},
        availability_by_code={"SKU-1": availability},
        supplier_orders_by_code={"SKU-1": [HistoricalSupplierOrder(created_at=date(2025, 1, 2))]},
        receipts_by_code={"SKU-1": [HistoricalReceipt(received_at=date(2025, 1, 3))]},
        history_start=date(2026, 1, 1),
        date_from=date(2026, 2, 1),
        date_to=date_to,
        demand_policy=policy
        or DemandStatePolicy(
            growth_multiplier=Decimal("1.2"),
            confirmation_days=7,
            max_single_day_share=Decimal("0.7"),
            min_independent_sales=2,
        ),
    )


def test_daily_replay_confirms_only_sustained_distributed_growth() -> None:
    old = [
        _sale(date(2025, 8, 15) + timedelta(days=6 * index), suffix=index) for index in range(12)
    ]
    recent = [
        _sale(date(2026, 2, 1) + timedelta(days=index), suffix=100 + index) for index in range(14)
    ]
    rows = _trajectory(
        sales=old + recent,
        availability=(date(2025, 8, 1) + timedelta(days=index) for index in range(196)),
        date_to=date(2026, 2, 18),
    )

    assert rows[0]["demand_state"] == DemandState.SPIKE.value
    growing = [row for row in rows if row["demand_state"] == DemandState.GROWING.value]
    assert growing
    assert date.fromisoformat(growing[0]["business_date"]) >= date(2026, 2, 8)
    assert growing[0]["status"] == AssortmentStatus.SALE.value


def test_single_document_customer_point_concentration_stays_spike() -> None:
    sales = [
        HistoricalSaleObservation(
            business_date=date(2026, 2, 1),
            quantity=Decimal("20"),
            document_id="one-doc",
            customer_id="one-customer",
            sales_point_id="one-point",
        ),
        *[
            _sale(date(2025, 8, 1) + timedelta(days=index * 14), suffix=100 + index)
            for index in range(12)
        ],
    ]
    rows = _trajectory(sales=sales, date_to=date(2026, 2, 18))

    assert {row["demand_state"] for row in rows} == {DemandState.SPIKE.value}
    assert {row["status"] for row in rows} == {AssortmentStatus.WORKING.value}


def test_replay_does_not_look_at_future_sale() -> None:
    rows = _trajectory(
        sales=[_sale(date(2026, 2, 10), suffix=1)],
        date_to=date(2026, 2, 11),
    )

    before = next(row for row in rows if row["business_date"] == "2026-02-09")
    after = next(row for row in rows if row["business_date"] == "2026-02-10")
    assert before["status"] == AssortmentStatus.NEW_ITEM.value
    assert before["first_sale_at"] is None
    assert after["status"] == AssortmentStatus.SALES_START.value
    assert after["first_sale_at"] == date(2026, 2, 10)


def test_new_receipt_changes_last_receipt_but_not_history_age() -> None:
    rows = build_assortment_lifecycle_v2_trajectory(
        items=[_item(first_receipt_at="2020-01-01")],
        sales_observations_by_code={},
        availability_by_code={},
        supplier_orders_by_code={"SKU-1": [HistoricalSupplierOrder(created_at=date(2019, 12, 1))]},
        receipts_by_code={"SKU-1": [HistoricalReceipt(received_at=date(2026, 2, 5))]},
        history_start=date(2026, 2, 4),
        date_from=date(2026, 2, 4),
        date_to=date(2026, 2, 6),
    )

    before, receipt_day, after = rows
    assert before["first_receipt_at"] == date(2020, 1, 1)
    assert receipt_day["first_receipt_at"] == date(2020, 1, 1)
    assert receipt_day["last_receipt_at"] == date(2026, 2, 5)
    assert after["history_age_days"] == (date(2026, 2, 6) - date(2020, 1, 1)).days


def test_manual_status_is_replayed_only_after_effective_date() -> None:
    rows = _trajectory(
        item=_item(
            manual_status="pension",
            manual_changed_at="2026-02-05",
            manual_reason="Решение менеджера",
            manual_approved_by="manager",
        ),
        date_to=date(2026, 2, 6),
    )

    assert rows[3]["status"] != AssortmentStatus.PENSION.value
    assert rows[4]["status"] == AssortmentStatus.PENSION.value
    assert rows[4]["historical_manual_status_replayed"] is True


def test_cargo_without_receipt_remains_ordered() -> None:
    rows = build_assortment_lifecycle_v2_trajectory(
        items=[_item()],
        sales_observations_by_code={},
        availability_by_code={},
        supplier_orders_by_code={
            "SKU-1": [
                HistoricalSupplierOrder(
                    created_at=date(2026, 2, 1), cargo_handoff_at=date(2026, 2, 2)
                )
            ]
        },
        receipts_by_code={},
        history_start=date(2026, 2, 1),
        date_from=date(2026, 2, 1),
        date_to=date(2026, 2, 3),
    )

    assert {row["status"] for row in rows} == {AssortmentStatus.NEWBORN.value}


def test_supplier_order_keeps_v2_pre_receipt_interval_in_trajectory() -> None:
    rows = build_assortment_lifecycle_v2_trajectory(
        items=[
            _item(
                created_at="2026-01-17",
                card_created_at="2026-01-17",
                first_supplier_order_at="2025-12-02",
                first_receipt_at="2026-01-17",
            )
        ],
        sales_observations_by_code={},
        availability_by_code={},
        supplier_orders_by_code={
            "SKU-1": [
                HistoricalSupplierOrder(
                    created_at=date(2025, 12, 2),
                    cargo_handoff_at=date(2026, 1, 9),
                )
            ]
        },
        receipts_by_code={"SKU-1": [HistoricalReceipt(received_at=date(2026, 1, 17))]},
        history_start=date(2026, 1, 1),
        date_from=date(2026, 1, 1),
        date_to=date(2026, 1, 18),
    )

    assert rows[0]["business_date"] == "2026-01-01"
    assert rows[0]["status"] == AssortmentStatus.NEWBORN.value
    assert rows[8]["first_cargo_at"] == date(2026, 1, 9)
    assert rows[-2]["status"] == AssortmentStatus.NEW_ITEM.value


def test_replay_uses_physical_inflow_before_supplier_receipt() -> None:
    rows = build_assortment_lifecycle_v2_trajectory(
        items=[
            _item(
                first_stock_inflow_at="2025-12-01",
                first_receipt_at="2026-02-05",
                first_sale_at="2025-12-10",
            )
        ],
        sales_observations_by_code={"SKU-1": []},
        availability_by_code={"SKU-1": []},
        supplier_orders_by_code={"SKU-1": []},
        receipts_by_code={"SKU-1": []},
        history_start=date(2026, 2, 1),
        date_from=date(2026, 2, 1),
        date_to=date(2026, 2, 2),
    )

    assert {row["status"] for row in rows} == {AssortmentStatus.SALES_START.value}
    assert {row["first_stock_inflow_at"] for row in rows} == {date(2025, 12, 1)}
    assert all(not row["blockers"] for row in rows)
