from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
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


def test_printed_decimal_uuid_normalizes_to_onec_idrref() -> None:
    assert (
        logistics.normalize_mm_log_document_ref("83491597397407213546269390744020073903")
        == "0xb4fc002590803daf11f19eca3ecfe591"
    )
    assert (
        logistics.normalize_mm_log_document_ref("0xB4FC002590803DAF11F19ECA3ECFE591")
        == "0xb4fc002590803daf11f19eca3ecfe591"
    )
    assert logistics.normalize_mm_log_document_ref("0") is None
    assert logistics.normalize_mm_log_document_ref(str(1 << 128)) is None
    assert logistics.normalize_mm_log_document_ref("not-a-uuid") is None


def test_short_printed_qr_resolves_rtu_and_transfer_by_source_type() -> None:
    engine, path = setup_db()
    printed_code = "MMLOG1|rtu|83491597397407213546269390744020073903"
    try:
        with Session(engine) as session:
            source = LogisticsWarehouse(
                external_id="source",
                name="Источник",
                kind="warehouse",
            )
            target = LogisticsWarehouse(
                external_id="target",
                name="Получатель",
                kind="store",
            )
            session.add_all([source, target])
            session.flush()
            session.add_all(
                [
                    LogisticsTransfer(
                        source_document_type="rtu",
                        external_id="0xb4fc002590803daf11f19eca3ecfe591",
                        document_number="РБГУ0401217",
                        document_date=datetime(2026, 8, 23, tzinfo=timezone.utc),
                        source_warehouse_id=source.id,
                        target_warehouse_id=target.id,
                        barcode="RTU-FULL",
                        lookup_code=("MMLOG1|rtu|0xb4fc002590803daf11f19eca3ecfe591|241666"),
                        site_order_number="241666",
                    ),
                    LogisticsTransfer(
                        source_document_type="transfer",
                        external_id="0xb4fc002590803daf11f19eca3ecfe591",
                        document_number="ПТ-0401217",
                        document_date=datetime(2026, 8, 23, tzinfo=timezone.utc),
                        source_warehouse_id=source.id,
                        target_warehouse_id=target.id,
                        barcode="TRANSFER-FULL",
                        lookup_code=("MMLOG1|transfer|0xb4fc002590803daf11f19eca3ecfe591|401217"),
                    ),
                ]
            )
            session.commit()

            assert logistics.lookup_unit(session, printed_code)["document_number"] == (
                "РБГУ0401217"
            )
            assert (
                logistics.lookup_unit(
                    session,
                    "MMLOG1|transfer|83491597397407213546269390744020073903",
                )["document_number"]
                == "ПТ-0401217"
            )
            assert (
                logistics.lookup_unit(
                    session,
                    "MMLOG1|rtu|0xB4FC002590803DAF11F19ECA3ECFE591",
                )["site_order_number"]
                == "241666"
            )
            assert (
                logistics.lookup_unit(
                    session,
                    "MMLOG1|rtu|0xB4FC002590803DAF11F19ECA3ECFE591|241666",
                )["document_number"]
                == "РБГУ0401217"
            )
    finally:
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_unknown_printed_qr_review_is_deduplicated_and_auto_resolved() -> None:
    engine, path = setup_db()
    code = "MMLOG1|rtu|83491597397407213546269390744020073903"
    try:
        with Session(engine) as session:
            for _ in range(2):
                with pytest.raises(HTTPException) as exc_info:
                    logistics.lookup_unit(session, code)
                assert exc_info.value.status_code == 404
            open_reviews = session.scalars(
                select(LogisticsManualReview).where(
                    LogisticsManualReview.review_type == "unknown_qr",
                    LogisticsManualReview.status == "open",
                )
            ).all()
            assert len(open_reviews) == 1
            assert open_reviews[0].payload["attempt_count"] == 2

            source = LogisticsWarehouse(
                external_id="source",
                name="Источник",
                kind="warehouse",
            )
            target = LogisticsWarehouse(
                external_id="target",
                name="Получатель",
                kind="store",
            )
            session.add_all([source, target])
            session.flush()
            session.add(
                LogisticsTransfer(
                    source_document_type="rtu",
                    external_id="0xb4fc002590803daf11f19eca3ecfe591",
                    document_number="РБГУ0401217",
                    document_date=datetime(2026, 8, 23, tzinfo=timezone.utc),
                    source_warehouse_id=source.id,
                    target_warehouse_id=target.id,
                    barcode="RTU-FULL",
                    lookup_code=("MMLOG1|rtu|0xb4fc002590803daf11f19eca3ecfe591|241666"),
                    site_order_number="241666",
                )
            )
            session.commit()

            result = logistics.lookup_unit(session, code)
            session.commit()
            assert result["document_number"] == "РБГУ0401217"
            review = session.scalar(select(LogisticsManualReview))
            assert review.status == "resolved"
            assert review.transfer_id == result["transfer_id"]
            assert review.payload["auto_resolved_by"] == "successful_lookup"
    finally:
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_external_carrier_qr_is_classified_without_entering_internal_pilot() -> None:
    engine, path = setup_db()
    code = "MMLOG1|rtu|79830017320027395940666266732585827759"
    external_id = "0xb4fc002590803daf11f1a1533c0eb3bb"
    try:
        with Session(engine) as session:
            with pytest.raises(HTTPException) as unknown_error:
                logistics.lookup_unit(session, code)
            assert unknown_error.value.status_code == 404

            sync_report = logistics_onec.sync_ready_rtu_units(
                session,
                onec_engine=None,
                source_rows=[
                    _rtu_row(
                        rtu_external_id=external_id,
                        site_delivery_method="СДЭК (Самовывоз)",
                        site_delivery_addition="Пермь, ул. Серпуховская, 6 #SPRM12",
                    )
                ],
                dry_run=False,
            )
            assert sync_report["manual_review_created"] == 1
            assert sync_report["by_reason"] == {"rtu_external_carrier_unmapped": 1}

            with pytest.raises(HTTPException) as classified_error:
                logistics.lookup_unit(session, code)
            assert classified_error.value.status_code == 409
            assert classified_error.value.detail == (
                "Документ относится к внешней службе доставки и пока не входит "
                "во внутренний пилот"
            )

            unknown_review = session.scalar(
                select(LogisticsManualReview).where(
                    LogisticsManualReview.review_type == "unknown_qr"
                )
            )
            assert unknown_review is not None
            assert unknown_review.status == "resolved"
            assert unknown_review.payload["auto_resolved_by"] == ("classified_external_carrier")
            assert unknown_review.payload["matched_review_id"] is not None
            assert session.query(LogisticsTransfer).count() == 0
    finally:
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_unknown_qr_deduplication_does_not_merge_long_codes_with_same_prefix() -> None:
    engine, path = setup_db()
    first_code = "X" * 64 + "-first"
    second_code = "X" * 64 + "-second"
    try:
        with Session(engine) as session:
            for code in (first_code, second_code, first_code):
                with pytest.raises(HTTPException) as exc_info:
                    logistics.lookup_unit(session, code)
                assert exc_info.value.status_code == 404

            reviews = session.scalars(
                select(LogisticsManualReview)
                .where(
                    LogisticsManualReview.review_type == "unknown_qr",
                    LogisticsManualReview.status == "open",
                )
                .order_by(LogisticsManualReview.id)
            ).all()
            assert len(reviews) == 2
            by_code = {review.payload["lookup_code"]: review for review in reviews}
            assert by_code[first_code].payload["attempt_count"] == 2
            assert by_code[second_code].payload["attempt_count"] == 1
    finally:
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_short_qr_lookup_rejects_ambiguous_external_id() -> None:
    engine, path = setup_db()
    try:
        with Session(engine) as session:
            source = LogisticsWarehouse(
                external_id="source",
                name="Источник",
                kind="warehouse",
            )
            target = LogisticsWarehouse(
                external_id="target",
                name="Получатель",
                kind="store",
            )
            session.add_all([source, target])
            session.flush()
            for index, external_id in enumerate(
                [
                    "0xb4fc002590803daf11f19eca3ecfe591",
                    "0xB4FC002590803DAF11F19ECA3ECFE591",
                ],
                start=1,
            ):
                session.add(
                    LogisticsTransfer(
                        source_document_type="rtu",
                        external_id=external_id,
                        document_number=f"РТУ-{index}",
                        document_date=datetime(2026, 8, 23, tzinfo=timezone.utc),
                        source_warehouse_id=source.id,
                        target_warehouse_id=target.id,
                        barcode=f"RTU-{index}",
                        lookup_code=f"RTU-LOOKUP-{index}",
                    )
                )
            session.commit()

            with pytest.raises(HTTPException) as exc_info:
                logistics.lookup_unit(
                    session,
                    "MMLOG1|rtu|83491597397407213546269390744020073903",
                )
            assert exc_info.value.status_code == 409
    finally:
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_rtu_source_query_uses_stable_pagination_and_targeted_filters() -> None:
    captured: dict = {}

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _traceback):
            return False

        def execute(self, statement, params):
            captured["statement"] = str(statement)
            captured["params"] = params
            return []

    class FakeEngine:
        def connect(self):
            return FakeConnection()

    rows = logistics_onec._fetch_rtu_source_rows(
        FakeEngine(),
        date_from=datetime(2026, 8, 14, tzinfo=timezone.utc),
        limit=500,
        offset=500,
        site_order_number="241666",
        rtu_external_id="0xB4FC002590803DAF11F19ECA3ECFE591",
    )

    assert rows == []
    assert "ROW_NUMBER() OVER" in captured["statement"]
    assert "ORDER BY rtu._Date_Time DESC, rtu._IDRRef DESC" in captured["statement"]
    assert "page_row_number > :page_offset" in captured["statement"]
    assert "page_row_number <= :page_end" in captured["statement"]
    assert "OFFSET" not in captured["statement"]
    assert "FETCH NEXT" not in captured["statement"]
    assert "print_event._Fld9449_TYPE = 0x08" in captured["statement"]
    assert "print_event._Fld9449_RTRef = 0x000000CB" in captured["statement"]
    assert "assembled_event._Fld9449_TYPE = 0x08" in captured["statement"]
    assert "assembled_event._Fld9449_RTRef = 0x000000CB" in captured["statement"]
    assert "ord._Fld10203RRef" in captured["statement"]
    assert "LEFT JOIN dbo._Reference68 AS pickup_dep" in captured["statement"]
    assert ":site_order_number" in captured["statement"]
    assert ":rtu_external_id" in captured["statement"]
    assert captured["params"]["page_offset"] == 500
    assert captured["params"]["page_end"] == 1000
    assert captured["params"]["site_order_number"] == "241666"
    assert captured["params"]["rtu_external_id"] == ("0xb4fc002590803daf11f19eca3ecfe591")


def test_normalize_rtu_source_rows_applies_readiness_gate() -> None:
    normalized = logistics_onec.normalize_rtu_source_rows(
        [
            _rtu_row(),
            _rtu_row(rtu_external_id="0xRTU2", is_marked=1),
            _rtu_row(rtu_external_id="0xRTU3", has_printed=0),
        ]
    )

    assert [row.rtu_external_id for row in normalized.ready] == ["0xRTU1"]
    assert normalized.skipped == []
    assert [row.review_type for row in normalized.pending_readiness] == [
        "rtu_readiness_gate_failed",
        "rtu_readiness_gate_failed",
    ]


def test_normalize_rtu_source_rows_ignores_rows_without_site_markers() -> None:
    normalized = logistics_onec.normalize_rtu_source_rows(
        [
            _rtu_row(
                site_order_number=None,
                site_delivery_method=None,
                source_warehouse_name="Розничный магазин",
            )
        ]
    )

    assert normalized.ready == []
    assert normalized.skipped == []
    assert normalized.pending_readiness == []
    assert [row.review_type for row in normalized.ignored_non_site] == ["not_site_order"]


def test_sync_readiness_rows_are_pending_without_manual_review() -> None:
    engine, path = setup_db()
    try:
        with Session(engine) as session:
            report = logistics_onec.sync_ready_rtu_units(
                session,
                onec_engine=None,
                source_rows=[_rtu_row(has_assembled=0)],
                dry_run=False,
            )

            assert report["pending_readiness"] == 1
            assert report["manual_review_created"] == 0
            assert session.query(LogisticsManualReview).count() == 0
    finally:
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_cleanup_legacy_rtu_manual_review_noise_is_idempotent() -> None:
    engine, path = setup_db()
    try:
        with Session(engine) as session:
            session.add_all(
                [
                    LogisticsManualReview(
                        review_type="rtu_readiness_gate_failed",
                        status="open",
                        source_document_type="rtu",
                        source_external_id="pending-1",
                        reason="not ready",
                        payload={"site_order_number": "216951"},
                    ),
                    LogisticsManualReview(
                        review_type="rtu_without_site_order",
                        status="open",
                        source_document_type="rtu",
                        source_external_id="retail-1",
                        reason="missing site order",
                        payload={"site_order_number": None, "site_delivery_method": None},
                    ),
                    LogisticsManualReview(
                        review_type="rtu_without_site_order",
                        status="open",
                        source_document_type="rtu",
                        source_external_id="site-1",
                        reason="missing site order",
                        payload={"site_order_number": None, "site_delivery_method": "Самовывоз"},
                    ),
                    LogisticsManualReview(
                        review_type="rtu_target_warehouse_unresolved",
                        status="open",
                        source_document_type="rtu",
                        source_external_id="review-1",
                        reason="warehouse unresolved",
                    ),
                    LogisticsManualReview(
                        review_type="rtu_without_site_order",
                        status="open",
                        source_document_type="rtu",
                        source_external_id="malformed-legacy-1",
                        reason="missing site order",
                        payload=["legacy", "payload"],
                    ),
                ]
            )
            session.commit()

            dry_run = logistics_onec.cleanup_legacy_rtu_manual_review_noise(session, dry_run=True)
            assert dry_run == {
                "dry_run": True,
                "matched": 2,
                "resolved": 0,
                "skipped_unsafe": 1,
                "by_reason": {
                    "no_positive_site_order_marker": 1,
                    "readiness_gate_is_pending_state": 1,
                },
            }
            assert session.query(LogisticsManualReview).filter_by(status="open").count() == 5

            applied = logistics_onec.cleanup_legacy_rtu_manual_review_noise(session, dry_run=False)
            assert applied["resolved"] == 2
            assert session.query(LogisticsManualReview).filter_by(status="open").count() == 3
            assert (
                logistics_onec.cleanup_legacy_rtu_manual_review_noise(session, dry_run=False)[
                    "resolved"
                ]
                == 0
            )
            resolved = session.scalar(
                select(LogisticsManualReview).where(
                    LogisticsManualReview.source_external_id == "retail-1"
                )
            )
            assert resolved is not None
            assert resolved.payload["auto_resolved_by"] == ("rtu_manual_review_noise_cleanup_v1")
    finally:
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


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


def test_resolve_target_warehouse_by_department_uses_exact_onec_mapping() -> None:
    engine, path = setup_db()
    try:
        with Session(engine) as session:
            session.add_all(
                [
                    LogisticsWarehouse(
                        external_id="target-presnya",
                        name="Электроника на Пресне В-46",
                        kind="store",
                        payload={
                            "onec_departments": [
                                {
                                    "external_id": "0x11111111111111111111111111111111",
                                    "code": "РБ0000027",
                                }
                            ]
                        },
                    ),
                    LogisticsWarehouse(
                        external_id="target-grand",
                        name="Гранд Юг В-34",
                        kind="store",
                        payload={
                            "onec_departments": [
                                {
                                    "external_id": "0x22222222222222222222222222222222",
                                    "code": "РБ0000028",
                                }
                            ]
                        },
                    ),
                ]
            )
            session.commit()

            resolved = logistics_onec.resolve_target_warehouse_by_department(
                session,
                department_external_id="0x22222222222222222222222222222222",
                department_code="РБ0000028",
            )
            assert resolved.warehouse is not None
            assert resolved.warehouse.external_id == "target-grand"
            assert resolved.matches[0]["match_type"] == "pickup_department_exact"

            unresolved = logistics_onec.resolve_target_warehouse_by_department(
                session,
                department_external_id="0x33333333333333333333333333333333",
                department_code="РБ0000099",
            )
            assert unresolved.warehouse is None
            assert unresolved.reason == ("Pickup department did not match a logistics warehouse")
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

            unchanged_report = logistics_onec.sync_ready_rtu_units(
                session,
                onec_engine=None,
                source_rows=[_rtu_row()],
                dry_run=False,
            )
            assert unchanged_report["synced_updated"] == 0

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


def test_active_unit_sync_conflict_preserves_route_and_deduplicates_audit() -> None:
    engine, path = setup_db()
    try:
        with Session(engine) as session:
            first_target = LogisticsWarehouse(
                external_id="target-1",
                name="Савеловский",
                kind="store",
                payload={"address_aliases": ["Савеловский Мобильный пав. Т-103 | Т-105"]},
            )
            second_target = LogisticsWarehouse(
                external_id="target-2",
                name="Другой магазин",
                kind="store",
                payload={"address_aliases": ["Другой магазин, павильон 7"]},
            )
            session.add_all([first_target, second_target])
            session.commit()

            created = logistics_onec.sync_ready_rtu_units(
                session,
                onec_engine=None,
                source_rows=[_rtu_row()],
                dry_run=False,
            )
            assert created["synced_created"] == 1
            transfer = session.scalar(select(LogisticsTransfer))
            assert transfer is not None
            original_target_id = transfer.target_warehouse_id
            state = session.get(LogisticsTransferState, transfer.id)
            assert state is not None
            state.status = "in_transit"
            state.last_event_type = "handed_to_driver"
            state.dropoff_warehouse_id = original_target_id
            session.commit()

            conflicting_row = _rtu_row(site_delivery_addition="Другой магазин, павильон 7")
            first_conflict = logistics_onec.sync_ready_rtu_units(
                session,
                onec_engine=None,
                source_rows=[conflicting_row],
                dry_run=False,
            )
            first_review = session.scalar(
                select(LogisticsManualReview).where(
                    LogisticsManualReview.review_type == "onec_reconciliation_conflict"
                )
            )
            assert first_review is not None
            first_review_updated_at = first_review.updated_at
            second_conflict = logistics_onec.sync_ready_rtu_units(
                session,
                onec_engine=None,
                source_rows=[conflicting_row],
                dry_run=False,
            )

            session.refresh(transfer)
            assert first_conflict["synced_updated"] == 0
            assert second_conflict["synced_updated"] == 0
            assert transfer.target_warehouse_id == original_target_id
            assert transfer.document_target_warehouse_id == original_target_id
            conflict_reviews = session.scalars(
                select(LogisticsManualReview).where(
                    LogisticsManualReview.review_type == "onec_reconciliation_conflict"
                )
            ).all()
            conflict_events = session.scalars(
                select(LogisticsTransferEvent).where(
                    LogisticsTransferEvent.event_type == "onec_reconciliation_conflict"
                )
            ).all()
            assert len(conflict_reviews) == 1
            assert conflict_reviews[0].status == "open"
            assert conflict_reviews[0].updated_at == first_review_updated_at
            assert len(conflict_events) == 1
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


def test_sync_ready_rtu_units_prefers_pickup_department_over_address() -> None:
    engine, path = setup_db()
    try:
        with Session(engine) as session:
            session.add_all(
                [
                    LogisticsWarehouse(
                        external_id="target-address",
                        name="Савеловский",
                        kind="store",
                        payload={"address_aliases": ["Савеловский Мобильный пав. Т-103 | Т-105"]},
                    ),
                    LogisticsWarehouse(
                        external_id="target-pickup",
                        name="Гранд Юг В-34",
                        kind="store",
                        payload={
                            "onec_departments": [
                                {
                                    "external_id": "0x22222222222222222222222222222222",
                                    "code": "РБ0000028",
                                }
                            ]
                        },
                    ),
                ]
            )
            session.commit()

            report = logistics_onec.sync_ready_rtu_units(
                session,
                onec_engine=None,
                source_rows=[
                    _rtu_row(
                        pickup_department_external_id=("0x22222222222222222222222222222222"),
                        pickup_department_code="РБ0000028",
                        pickup_department_name="Гранд Юг",
                    )
                ],
                dry_run=False,
            )

            assert report["synced_created"] == 1
            transfer = session.scalar(select(LogisticsTransfer))
            assert transfer is not None
            assert transfer.target_warehouse.external_id == "target-pickup"
            assert transfer.payload["pickup_department_code"] == "РБ0000028"
            assert transfer.payload["target_resolution"][0]["match_type"] == (
                "pickup_department_exact"
            )
    finally:
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_sync_ready_rtu_units_fails_closed_for_unknown_pickup_department() -> None:
    engine, path = setup_db()
    try:
        with Session(engine) as session:
            session.add(
                LogisticsWarehouse(
                    external_id="target-address",
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
                    _rtu_row(
                        pickup_department_external_id=("0x33333333333333333333333333333333"),
                        pickup_department_code="РБ0000099",
                    )
                ],
                dry_run=False,
            )

            assert report["synced_created"] == 0
            assert report["manual_review_created"] == 1
            review = session.scalar(select(LogisticsManualReview))
            assert review is not None
            assert review.reason == ("Pickup department did not match a logistics warehouse")
    finally:
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_normalize_rtu_source_rows_treats_zero_pickup_ref_as_empty() -> None:
    normalized = logistics_onec.normalize_rtu_source_rows(
        [
            _rtu_row(
                pickup_department_external_id="0x00000000000000000000000000000000",
                pickup_department_code=None,
            )
        ]
    )

    assert len(normalized.ready) == 1
    assert normalized.ready[0].pickup_department_external_id is None


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
            assert repeat_report["synced_updated"] == 0
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
            assert sav.payload["code"] == "SAV"
            assert sav.payload["onec_departments"][0]["external_id"] == "0xaaaa"

            mit = session.scalar(
                select(LogisticsWarehouse).where(LogisticsWarehouse.external_id == "0xbbbb")
            )
            assert mit is not None
            assert mit.kind == "store"
            assert mit.payload["address_aliases"] == ["г. Москва, Пятницкое шоссе, д.18, пав. 535"]
            assert mit.payload["code"] == "MIT"

            sav.payload = {key: value for key, value in sav.payload.items() if key != "code"}
            session.commit()
            code_only_report = logistics_onec.sync_warehouse_address_aliases(
                session,
                onec_engine=None,
                source_rows=rows[:1],
                dry_run=False,
            )
            assert code_only_report["aliases_added"] == 0
            assert code_only_report["warehouses_updated"] == 1
            session.refresh(sav)
            assert sav.payload["code"] == "SAV"
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
