from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from app.models.procurement_order_formation import ProcurementOrderFormation
from app.services.procurement_order_formation import line_blockers
from app.services.procurement_order_formation_workspace import list_orders
from app.services.procurement_order_registry import (
    lifecycle_status_for_snapshot,
    synchronize_onec_snapshots,
    upsert_onec_order_snapshot,
)
from tasks import sync_procurement_order_registry as registry_sync_task


def _snapshot(**overrides):
    snapshot = {
        "onec_ref": "0x0123456789abcdef0123456789abcdef",
        "number": "РБГУ0000543",
        "order_date": datetime(2026, 8, 31, 8, 0),
        "posted": True,
        "marked": False,
        "supplier": {"onec_ref": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "title": "Поставщик"},
        "contract_ref": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "contract_name": "Основной договор",
        "store_ref": "0xcccccccccccccccccccccccccccccccc",
        "store_name": "Основной склад",
        "currency": "RUB",
        "procurement_contour_key": "ordinary",
        "ordered_qty": Decimal("10"),
        "open_qty": Decimal("10"),
        "lines": [
            {
                "line_no": 1,
                "item_ref_hex": "0xdddddddddddddddddddddddddddddddd",
                "onec_item_code": "0001",
                "item_name": "Товар",
                "quantity": Decimal("10"),
                "open_quantity": Decimal("10"),
                "price": Decimal("100"),
                "amount": Decimal("1000"),
            }
        ],
    }
    snapshot.update(overrides)
    return snapshot


def test_lifecycle_status_precedence() -> None:
    assert lifecycle_status_for_snapshot(_snapshot()) == "active"
    assert lifecycle_status_for_snapshot(_snapshot(cargo_dropoff_date="2026-09-01")) == "in_transit"
    assert lifecycle_status_for_snapshot(_snapshot(open_qty=Decimal("4"))) == "partially_received"
    assert lifecycle_status_for_snapshot(_snapshot(open_qty=Decimal("0"))) == "received"
    assert (
        lifecycle_status_for_snapshot(_snapshot(marked=True), previous_status="active")
        == "cancelled"
    )


def test_snapshot_without_canonical_guid_is_rejected(db_session) -> None:
    result = upsert_onec_order_snapshot(db_session, _snapshot(onec_ref="Заказ поставщику"))

    assert result.action == "conflict"
    assert result.order_id is None
    assert "invalid" in (result.conflict or "")


def test_imported_order_is_created_without_fake_approval(db_session) -> None:
    result = upsert_onec_order_snapshot(db_session, _snapshot())
    db_session.commit()

    order = db_session.get(ProcurementOrderFormation, result.order_id)
    assert result.action == "created"
    assert order is not None
    assert order.origin == "onec_import"
    assert order.lifecycle_status == "active"
    assert order.approved_at is None
    assert order.approved_by_name is None
    assert order.onec_ordered_quantity == Decimal("10")
    assert order.onec_open_quantity == Decimal("10")
    assert len(order.lines) == 1
    assert order.lines[0].onec_open_quantity == Decimal("10")
    assert order.lines[0].onec_received_quantity == Decimal("0")


def test_imported_order_links_bitrix_product_without_false_catalog_blocker(db_session) -> None:
    result = upsert_onec_order_snapshot(
        db_session,
        _snapshot(),
        catalog_product_ids={
            "0xdddddddddddddddddddddddddddddddd": "321",
        },
    )
    db_session.commit()

    order = db_session.get(ProcurementOrderFormation, result.order_id)
    assert order is not None
    assert order.lines[0].bitrix_product_id == "321"
    assert "catalog_product_missing" not in line_blockers(order.lines[0])
    assert "catalog_xml_id_mismatch" not in line_blockers(order.lines[0])
    assert any(event.event_type == "onec_import_catalog_links_updated" for event in order.events)


def test_catalog_lookup_reuses_known_links_and_resolves_only_missing_refs(
    db_session, monkeypatch
) -> None:
    upsert_onec_order_snapshot(
        db_session,
        _snapshot(),
        catalog_product_ids={
            "0xdddddddddddddddddddddddddddddddd": "321",
        },
    )
    snapshot = _snapshot(
        lines=[
            *_snapshot()["lines"],
            {
                "line_no": 2,
                "item_ref_hex": "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
                "onec_item_code": "0002",
                "item_name": "Новый товар",
                "quantity": Decimal("1"),
                "open_quantity": Decimal("1"),
                "price": Decimal("10"),
                "amount": Decimal("10"),
            },
        ]
    )
    requested_refs: list[str] = []

    def resolve_missing(refs):
        requested_refs.extend(refs)
        return {
            "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee": type("Product", (), {"product_id": "654"})()
        }

    monkeypatch.setattr(
        registry_sync_task,
        "resolve_catalog_products_by_xml_ids",
        resolve_missing,
    )

    result = registry_sync_task._catalog_product_ids_for_snapshots(db_session, [snapshot])

    assert requested_refs == ["0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"]
    assert result == {
        "0xdddddddddddddddddddddddddddddddd": "321",
        "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee": "654",
    }


def test_imported_order_without_catalog_readback_is_not_a_transmission_blocker(db_session) -> None:
    result = upsert_onec_order_snapshot(db_session, _snapshot())
    db_session.commit()

    order = db_session.get(ProcurementOrderFormation, result.order_id)
    assert order is not None
    assert order.lines[0].bitrix_product_id is None
    assert "catalog_product_missing" not in line_blockers(order.lines[0])


def test_repeated_snapshot_updates_same_order_and_tracks_receipt(db_session) -> None:
    first = synchronize_onec_snapshots(db_session, [_snapshot()])[0]
    second = synchronize_onec_snapshots(db_session, [_snapshot(open_qty=Decimal("3"))])[0]

    assert first.order_id == second.order_id
    order = db_session.get(ProcurementOrderFormation, first.order_id)
    assert order is not None
    assert order.lifecycle_status == "partially_received"
    assert order.onec_received_quantity == Decimal("7")
    assert second.action == "updated"


def test_repeated_snapshot_with_numeric_scale_difference_is_noop(db_session) -> None:
    first = synchronize_onec_snapshots(db_session, [_snapshot(open_qty=Decimal("6"))])[0]
    order = db_session.get(ProcurementOrderFormation, first.order_id)
    assert order is not None
    order.onec_open_quantity = Decimal("6.000")
    db_session.commit()
    db_session.expire_all()

    second = synchronize_onec_snapshots(db_session, [_snapshot(open_qty=Decimal("6"))])[0]

    assert second.action == "noop"


def test_legacy_exact_identity_attaches_guid_to_same_generated_order(db_session) -> None:
    order = ProcurementOrderFormation(
        stable_key="generated:test",
        status="transmitted",
        lifecycle_status="active",
        origin="generated",
        supplier_name="Поставщик",
        contract_name="Основной договор",
        warehouse_name="Склад",
        currency="RUB",
        procurement_contour="ordinary",
        route="ordinary",
        batch_id="test",
        order_date=date(2026, 8, 31),
        calculation_id="test",
        onec_status="transmitted",
        onec_document_number="РБГУ0000543",
        onec_document_date=date(2026, 8, 31),
    )
    db_session.add(order)
    db_session.commit()

    result = synchronize_onec_snapshots(db_session, [_snapshot()])[0]

    assert result.order_id == order.id
    assert order.origin == "generated"
    assert order.onec_document_ref == "0x0123456789abcdef0123456789abcdef"


def test_ambiguous_legacy_identity_is_reported_without_creation(db_session) -> None:
    for index in range(2):
        db_session.add(
            ProcurementOrderFormation(
                stable_key=f"generated:duplicate:{index}",
                status="transmitted",
                lifecycle_status="active",
                origin="generated",
                supplier_name="Поставщик",
                contract_name="Договор",
                warehouse_name="Склад",
                currency="RUB",
                procurement_contour="ordinary",
                route="ordinary",
                batch_id=str(index),
                order_date=date(2026, 8, 31),
                calculation_id=str(index),
                onec_status="transmitted",
                onec_document_number="РБГУ0000543",
                onec_document_date=date(2026, 8, 31),
            )
        )
    db_session.commit()

    result = upsert_onec_order_snapshot(db_session, _snapshot())

    assert result.action == "conflict"
    assert result.order_id is None
    assert "ambiguous" in (result.conflict or "")


def test_unified_registry_filters_and_summary_use_lifecycle(db_session) -> None:
    synchronize_onec_snapshots(db_session, [_snapshot(open_qty=Decimal("6"))])

    result = list_orders(
        db_session,
        lifecycle_status="partially_received",
        supplier="Поставщик",
        contour="ordinary",
        onec_number="0543",
        date_from=date(2026, 8, 1),
        date_to=date(2026, 8, 31),
        source="onec_import",
    )

    assert result["total"] == 1
    assert result["summary"]["by_status"] == {"partially_received": 1}
    assert result["items"][0]["ordered_quantity"] == Decimal("10")
    assert result["items"][0]["received_quantity"] == Decimal("4")
