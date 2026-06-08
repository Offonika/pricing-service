from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models import (
    Base,
    LogisticsManualReview,
    LogisticsTransfer,
    LogisticsTransferEvent,
    LogisticsTransferState,
    LogisticsWarehouse,
)
from app.services import logistics, logistics_onec


def setup_db():
    fd, path = tempfile.mkstemp(prefix="logistics_onec_", suffix=".db")
    os.close(fd)
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    return engine, path


def _rtu_row(**overrides):
    row = {
        "rtu_external_id": "0xRTU1",
        "rtu_number": "РТУ-000001",
        "rtu_date": datetime(2026, 5, 22, 10, 0, tzinfo=timezone.utc),
        "site_order_external_id": "0xORDER1",
        "onec_order_number": "РБГУ0030399",
        "site_order_number": "216951",
        "site_delivery_method": "Самовывоз",
        "site_delivery_addition": "Савеловский Мобильный пав. Т-103 | Т-105",
        "site_delivery_address": None,
        "rtu_delivery_addition": None,
        "rtu_delivery_address": None,
        "source_warehouse_external_id": "0xSOURCE1",
        "source_warehouse_code": "SRC",
        "source_warehouse_name": "Склад РТУ",
        "is_marked": 0,
        "is_posted": 1,
        "has_printed": 1,
        "has_assembled": 1,
    }
    row.update(overrides)
    return row


def test_normalize_rtu_source_rows_applies_readiness_gate() -> None:
    normalized = logistics_onec.normalize_rtu_source_rows(
        [
            _rtu_row(),
            _rtu_row(rtu_external_id="0xRTU2", is_marked=1),
            _rtu_row(rtu_external_id="0xRTU3", has_printed=0),
        ]
    )

    assert [row.rtu_external_id for row in normalized.ready] == ["0xRTU1"]
    assert [row.review_type for row in normalized.skipped] == [
        "rtu_readiness_gate_failed",
        "rtu_readiness_gate_failed",
    ]


def test_resolve_target_warehouse_uses_payload_aliases() -> None:
    engine, path = setup_db()
    try:
        with Session(engine) as session:
            warehouse = LogisticsWarehouse(
                external_id="target-1",
                name="Савеловский",
                kind="store",
                payload={"address_aliases": ["Савеловский Мобильный пав. Т-103 | Т-105"]},
            )
            session.add(warehouse)
            session.commit()

            resolved = logistics_onec.resolve_target_warehouse(
                session,
                ["Москва, Савеловский Мобильный пав. Т-103 | Т-105"],
            )
            assert resolved.warehouse is not None
            assert resolved.warehouse.external_id == "target-1"

            session.add(
                LogisticsWarehouse(
                    external_id="target-2",
                    name="Савеловский дубль",
                    kind="store",
                    payload={"address_aliases": ["Савеловский Мобильный"]},
                )
            )
            session.commit()
            ambiguous = logistics_onec.resolve_target_warehouse(
                session,
                ["Москва, Савеловский Мобильный пав. Т-103 | Т-105"],
            )
            assert ambiguous.warehouse is None
            assert ambiguous.reason == "RTU address matched multiple warehouses"
    finally:
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_resolve_target_warehouse_uses_address_token_overlap() -> None:
    engine, path = setup_db()
    try:
        with Session(engine) as session:
            session.add(
                LogisticsWarehouse(
                    external_id="target-mitino",
                    name="Митинский радиорынок пав. 535",
                    kind="store",
                    payload={
                        "address_aliases": [
                            "ТК «Митинский радиорынок», Пятницкое шоссе, д.18, пав. 535"
                        ]
                    },
                )
            )
            session.add(
                LogisticsWarehouse(
                    external_id="target-grand",
                    name="Гранд Юг В-34",
                    kind="store",
                    payload={
                        "address_aliases": ["ТЦ Гранд Юг, Кировоградская улица, 15, пав. В-34"]
                    },
                )
            )
            session.commit()

            resolved = logistics_onec.resolve_target_warehouse(
                session,
                ["г. Москва, Пятницкое шоссе, д.18, этаж: 2, пав. 535"],
            )
            assert resolved.warehouse is not None
            assert resolved.warehouse.external_id == "target-mitino"

            unresolved = logistics_onec.resolve_target_warehouse(
                session,
                ["г. Москва, Кировоградская улица, 15, пав. Г-33/35"],
            )
            assert unresolved.warehouse is None
            assert unresolved.reason == "RTU address did not match any warehouse"
    finally:
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_resolve_target_warehouse_ignores_city_and_hours_overlap() -> None:
    engine, path = setup_db()
    try:
        with Session(engine) as session:
            session.add_all(
                [
                    LogisticsWarehouse(
                        external_id="spb-moskovskaya",
                        name="СПБ Московская",
                        kind="store",
                        payload={
                            "address_aliases": [
                                "Магазин MASTER MOBILE, г. Санкт-Петербург, ул Алтайская, дом 7"
                            ]
                        },
                    ),
                    LogisticsWarehouse(
                        external_id="spb-prosvescheniya",
                        name="СПБ Просвещения",
                        kind="store",
                        payload={
                            "address_aliases": [
                                "Магазин MASTER MOBILE, г. Санкт-Петербург, пр-кт Просвещения 36к3"
                            ]
                        },
                    ),
                    LogisticsWarehouse(
                        external_id="spb-sadovaya",
                        name="СПБ Садовая",
                        kind="store",
                        payload={
                            "address_aliases": [
                                "Магазин MASTER MOBILE, г. Санкт-Петербург, ул. Садовая 28-30 к.1"
                            ]
                        },
                    ),
                ]
            )
            session.commit()

            prosvescheniya = logistics_onec.resolve_target_warehouse(
                session,
                ["г. Санкт-Петербург, пр. Просвещения, 36 к.1, Пн - Вс: 9:30 - 20:00"],
            )
            assert prosvescheniya.warehouse is not None
            assert prosvescheniya.warehouse.external_id == "spb-prosvescheniya"

            moskovskaya = logistics_onec.resolve_target_warehouse(
                session,
                ["г. Санкт-Петербург, Алтайская улица, 7, этаж: 0, Пн - Вс: 09:30 - 20:00"],
            )
            assert moskovskaya.warehouse is not None
            assert moskovskaya.warehouse.external_id == "spb-moskovskaya"
    finally:
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_sync_ready_rtu_units_creates_and_updates_logistics_unit() -> None:
    engine, path = setup_db()
    try:
        with Session(engine) as session:
            session.add(
                LogisticsWarehouse(
                    external_id="target-1",
                    name="Савеловский",
                    kind="store",
                    payload={"address_aliases": ["Савеловский Мобильный пав. Т-103 | Т-105"]},
                )
            )
            session.commit()

            dry_run = logistics_onec.sync_ready_rtu_units(
                session,
                onec_engine=None,
                source_rows=[_rtu_row()],
                dry_run=True,
            )
            assert dry_run["synced_planned"] == 1
            assert dry_run["synced_created"] == 0

            report = logistics_onec.sync_ready_rtu_units(
                session,
                onec_engine=None,
                source_rows=[_rtu_row()],
                dry_run=False,
            )
            assert report["synced_created"] == 1
            assert report["warehouses_created"] == 1

            transfer = session.scalar(select(LogisticsTransfer))
            assert transfer is not None
            assert transfer.source_document_type == "rtu"
            assert transfer.external_id == "0xRTU1"
            assert transfer.site_order_number == "216951"
            assert transfer.lookup_code == "MMLOG1|rtu|0xRTU1|216951"
            assert transfer.barcode == transfer.lookup_code
            assert transfer.target_warehouse.external_id == "target-1"

            lookup = logistics.lookup_unit(session, transfer.lookup_code)
            assert lookup["transfer_id"] == transfer.id

            second_report = logistics_onec.sync_ready_rtu_units(
                session,
                onec_engine=None,
                source_rows=[_rtu_row(site_delivery_method="Самовывоз updated")],
                dry_run=False,
            )
            assert second_report["synced_updated"] == 1
            assert session.query(LogisticsTransfer).count() == 1
    finally:
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_sync_ready_rtu_units_routes_bad_rows_to_manual_review() -> None:
    engine, path = setup_db()
    try:
        with Session(engine) as session:
            session.add(
                LogisticsWarehouse(
                    external_id="target-1",
                    name="Савеловский",
                    kind="store",
                    payload={"address_aliases": ["Савеловский Мобильный пав. Т-103 | Т-105"]},
                )
            )
            session.commit()

            report = logistics_onec.sync_ready_rtu_units(
                session,
                onec_engine=None,
                source_rows=[
                    _rtu_row(rtu_external_id="0xRTU2", site_order_number=None),
                    _rtu_row(
                        rtu_external_id="0xRTU3",
                        site_order_number="216953",
                        site_delivery_addition="Неизвестный адрес",
                    ),
                    _rtu_row(
                        rtu_external_id="0xRTU4",
                        site_order_number="216954",
                        site_delivery_method="СДЭК (Самовывоз)",
                        site_delivery_addition="Пермь, ул. Серпуховская, 6 #SPRM12",
                    ),
                ],
                dry_run=False,
            )

            assert report["synced_created"] == 0
            assert report["manual_review_created"] == 3
            assert sorted(
                row.review_type for row in session.scalars(select(LogisticsManualReview))
            ) == [
                "rtu_external_carrier_unmapped",
                "rtu_target_warehouse_unresolved",
                "rtu_without_site_order",
            ]

            resolved_report = logistics_onec.sync_ready_rtu_units(
                session,
                onec_engine=None,
                source_rows=[
                    _rtu_row(
                        rtu_external_id="0xRTU3",
                        site_order_number="216953",
                    )
                ],
                dry_run=False,
            )
            assert resolved_report["synced_created"] == 1
            assert resolved_report["manual_review_resolved"] == 1
            resolved_review = session.scalar(
                select(LogisticsManualReview).where(
                    LogisticsManualReview.source_external_id == "0xRTU3"
                )
            )
            assert resolved_review is not None
            assert resolved_review.status == "resolved"
            assert resolved_review.payload["auto_resolved_by"] == "rtu_sync"
    finally:
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_sync_ready_rtu_units_uses_source_for_empty_pickup_address() -> None:
    engine, path = setup_db()
    try:
        with Session(engine) as session:
            session.add(
                LogisticsManualReview(
                    review_type="rtu_target_warehouse_unresolved",
                    reason="RTU has no address candidates",
                    source_document_type=logistics.SOURCE_RTU,
                    source_external_id="0xRTU1",
                    payload={"site_order_number": "216951"},
                )
            )
            session.commit()

            report = logistics_onec.sync_ready_rtu_units(
                session,
                onec_engine=None,
                source_rows=[
                    _rtu_row(
                        site_delivery_method="Самовывоз",
                        site_delivery_addition=None,
                        site_delivery_address=None,
                        rtu_delivery_addition=None,
                        rtu_delivery_address=None,
                    )
                ],
                dry_run=False,
            )

            assert report["synced_created"] == 1
            assert report["manual_review_created"] == 0
            assert report["manual_review_resolved"] == 1

            transfer = session.scalar(select(LogisticsTransfer))
            assert transfer is not None
            assert transfer.source_warehouse.external_id == "0xSOURCE1"
            assert transfer.target_warehouse.external_id == "0xSOURCE1"
            assert transfer.document_target_warehouse.external_id == "0xSOURCE1"
            assert transfer.payload["empty_pickup_address_target_source"] is True
            assert transfer.payload["business_rule"] == "pickup_empty_address_target_source"
            assert transfer.payload["target_resolution"][0]["match_type"] == (
                "empty_pickup_address_target_source"
            )

            review = session.scalar(select(LogisticsManualReview))
            assert review is not None
            assert review.status == "resolved"
    finally:
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_sync_ready_rtu_units_can_apply_external_carrier_flow() -> None:
    engine, path = setup_db()
    try:
        with Session(engine) as session:
            dry_run = logistics_onec.sync_ready_rtu_units(
                session,
                onec_engine=None,
                source_rows=[
                    _rtu_row(
                        site_delivery_method="СДЭК (Самовывоз)",
                        site_delivery_addition="Пермь, ул. Серпуховская, 6 #SPRM12",
                    )
                ],
                dry_run=True,
                external_carrier_flow=True,
            )
            assert dry_run["synced_planned"] == 1
            assert dry_run["external_carrier_planned"] == 1

            report = logistics_onec.sync_ready_rtu_units(
                session,
                onec_engine=None,
                source_rows=[
                    _rtu_row(
                        site_delivery_method="СДЭК (Самовывоз)",
                        site_delivery_addition="Пермь, ул. Серпуховская, 6 #SPRM12",
                    )
                ],
                dry_run=False,
                external_carrier_flow=True,
            )
            assert report["synced_created"] == 1
            assert report["external_carrier_handoff_created"] == 1
            assert report["manual_review_created"] == 0

            transfer = session.scalar(select(LogisticsTransfer))
            assert transfer is not None
            assert transfer.source_document_type == "rtu"
            assert transfer.target_warehouse.external_id == "0xSOURCE1"
            assert transfer.payload["external_carrier_flow"] is True
            assert transfer.payload["external_carrier_name"] == "СДЭК"

            state = session.get(LogisticsTransferState, transfer.id)
            assert state is not None
            assert state.status == logistics.STATUS_WITH_EXTERNAL_CARRIER
            assert state.current_warehouse_id is None
            assert state.last_event_type == logistics.EVENT_HANDED_TO_EXTERNAL_CARRIER

            event = session.scalar(select(LogisticsTransferEvent))
            assert event is not None
            assert event.source == "1c_sync"
            assert event.event_type == logistics.EVENT_HANDED_TO_EXTERNAL_CARRIER
            assert event.meta["carrier_name"] == "СДЭК"
            assert event.meta["carrier_terminal"] == "Пермь, ул. Серпуховская, 6 #SPRM12"

            repeat_report = logistics_onec.sync_ready_rtu_units(
                session,
                onec_engine=None,
                source_rows=[
                    _rtu_row(
                        site_delivery_method="СДЭК (Самовывоз)",
                        site_delivery_addition="Пермь, ул. Серпуховская, 6 #SPRM12",
                    )
                ],
                dry_run=False,
                external_carrier_flow=True,
            )
            assert repeat_report["synced_updated"] == 1
            assert repeat_report["external_carrier_handoff_existing"] == 1
            assert session.query(LogisticsTransferEvent).count() == 1

            default_report = logistics_onec.sync_ready_rtu_units(
                session,
                onec_engine=None,
                source_rows=[
                    _rtu_row(
                        site_delivery_method="СДЭК (Самовывоз)",
                        site_delivery_addition="Пермь, ул. Серпуховская, 6 #SPRM12",
                    )
                ],
                dry_run=False,
            )
            assert default_report["manual_review_created"] == 0
    finally:
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_external_carrier_flow_resolves_existing_manual_review() -> None:
    engine, path = setup_db()
    try:
        with Session(engine) as session:
            manual_report = logistics_onec.sync_ready_rtu_units(
                session,
                onec_engine=None,
                source_rows=[
                    _rtu_row(
                        site_delivery_method="Почта России (Доставка в отделение)",
                        site_delivery_addition="Москва, отделение 101000",
                    )
                ],
                dry_run=False,
            )
            assert manual_report["manual_review_created"] == 1
            review = session.scalar(select(LogisticsManualReview))
            assert review is not None
            assert review.status == "open"
            assert review.review_type == "rtu_external_carrier_unmapped"

            report = logistics_onec.sync_ready_rtu_units(
                session,
                onec_engine=None,
                source_rows=[
                    _rtu_row(
                        site_delivery_method="Почта России (Доставка в отделение)",
                        site_delivery_addition="Москва, отделение 101000",
                    )
                ],
                dry_run=False,
                external_carrier_flow=True,
            )
            assert report["synced_created"] == 1
            assert report["external_carrier_handoff_created"] == 1
            assert report["manual_review_resolved"] == 1
            assert review.status == "resolved"
            assert review.payload["auto_resolved_by"] == "rtu_sync"
    finally:
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_sync_warehouse_address_aliases_merges_payload() -> None:
    engine, path = setup_db()
    try:
        with Session(engine) as session:
            session.add(
                LogisticsWarehouse(
                    external_id="0xabcdef12",
                    name="Савеловский Мобильный пав. Т-103 | Т-105",
                    kind="store",
                    payload={"address_aliases": ["старый адрес"]},
                )
            )
            session.commit()

            rows = [
                {
                    "warehouse_external_id": "0xABCDEF12",
                    "warehouse_code": "SAV",
                    "warehouse_name": "Савеловский Мобильный пав. Т-103 | Т-105",
                    "department_external_id": "0xAAAA",
                    "department_code": "D1",
                    "department_name": "ТК Савеловский Мобильный",
                    "address_alias": "г. Москва, Сущевский вал, д. 5 стр 6, пав. Т-103/105",
                    "phone": "+7",
                },
                {
                    "warehouse_external_id": "0xBBBB",
                    "warehouse_code": "MIT",
                    "warehouse_name": "Митинский радиорынок пав. 535",
                    "department_external_id": "0xCCCC",
                    "department_code": "D2",
                    "department_name": "Митинский радиорынок",
                    "address_alias": "г. Москва, Пятницкое шоссе, д.18, пав. 535",
                    "phone": None,
                },
            ]

            dry_run = logistics_onec.sync_warehouse_address_aliases(
                session,
                onec_engine=None,
                source_rows=rows,
                dry_run=True,
            )
            assert dry_run["warehouses_planned_created"] == 1
            assert dry_run["warehouses_planned_updated"] == 1
            assert dry_run["aliases_added"] == 2

            report = logistics_onec.sync_warehouse_address_aliases(
                session,
                onec_engine=None,
                source_rows=rows,
                dry_run=False,
            )
            assert report["warehouses_created"] == 1
            assert report["warehouses_updated"] == 1

            sav = session.scalar(
                select(LogisticsWarehouse).where(LogisticsWarehouse.external_id == "0xabcdef12")
            )
            assert sav is not None
            assert sav.payload is not None
            assert sav.payload["address_aliases"] == [
                "старый адрес",
                "г. Москва, Сущевский вал, д. 5 стр 6, пав. Т-103/105",
            ]
            assert sav.payload["onec_departments"][0]["external_id"] == "0xaaaa"

            mit = session.scalar(
                select(LogisticsWarehouse).where(LogisticsWarehouse.external_id == "0xbbbb")
            )
            assert mit is not None
            assert mit.kind == "store"
            assert mit.payload["address_aliases"] == ["г. Москва, Пятницкое шоссе, д.18, пав. 535"]
    finally:
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_apply_warehouse_alias_overrides_requires_confirmation_payload() -> None:
    engine, path = setup_db()
    try:
        with Session(engine) as session:
            session.add(
                LogisticsWarehouse(
                    external_id="0xabcdef12",
                    name="Гранд Юг В-34",
                    kind="store",
                    payload={"address_aliases": ["старый адрес"]},
                )
            )
            session.commit()

            overrides = [
                {
                    "warehouse_external_id": "0xABCDEF12",
                    "alias": "г. Москва, Кировоградская улица, 15, пав. Г-33/35",
                    "reason": "confirmed by logistics",
                    "confirmed_by": "ops",
                }
            ]

            dry_run = logistics_onec.apply_warehouse_alias_overrides(
                session,
                overrides,
                dry_run=True,
            )
            assert dry_run["warehouses_updated"] == 1
            assert dry_run["aliases_added"] == 1
            warehouse = session.scalar(
                select(LogisticsWarehouse).where(LogisticsWarehouse.external_id == "0xabcdef12")
            )
            assert warehouse.payload["address_aliases"] == ["старый адрес"]

            report = logistics_onec.apply_warehouse_alias_overrides(
                session,
                overrides,
                dry_run=False,
            )
            assert report["warehouses_updated"] == 1
            assert report["aliases_added"] == 1
            assert report["aliases_existing"] == 0

            session.refresh(warehouse)
            assert warehouse.payload["address_aliases"] == [
                "старый адрес",
                "г. Москва, Кировоградская улица, 15, пав. Г-33/35",
            ]
            assert warehouse.payload["alias_override_history"][0]["reason"] == (
                "confirmed by logistics"
            )

            repeat = logistics_onec.apply_warehouse_alias_overrides(
                session,
                overrides,
                dry_run=False,
            )
            assert repeat["aliases_added"] == 0
            assert repeat["aliases_existing"] == 1
    finally:
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)
