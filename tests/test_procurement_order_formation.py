from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from xml.etree import ElementTree as ET

import pytest
from sqlalchemy import select

import app.services.bitrix_order_formation as bitrix_order_service
import app.services.procurement_order_formation as order_service
from app.api.procurement_order_formation import change_order_line
from app.core.config import Settings
from app.models.procurement_order_formation import (
    ProcurementClassificationProposal,
    ProcurementOrderFormation,
    ProcurementOrderFormationEvent,
    ProcurementOrderFormationLine,
)
from app.schemas.procurement_order_formation import (
    ProcurementOrderAssistantResponse,
    ProcurementOrderLineUpdateRequest,
)
from app.services.bitrix_order_formation import BitrixCatalogProduct, build_bitrix_product_rows
from app.services.bitrix_procurement_order_formation_auth import (
    ProcurementOrderFormationSession,
)
from app.services.exporters.ut103_nomenclature_properties import (
    PropertyUpdateExchangeResult,
    PropertyUpdateItemResult,
    build_nomenclature_property_updates_xml,
)
from app.services.exporters.ut103_procurement_orders import (
    ProcurementSupplierOrderExchangeResult,
    ProcurementSupplierOrderItemResult,
    build_procurement_supplier_orders_xml,
)
from app.services.procurement_order_formation import (
    VersionConflictError,
    approve_classification_proposal,
    approve_order,
    build_classification_update_message,
    build_order_message,
    classification_blocks_line,
    create_classification_proposal,
    distribute_lines_by_suppliers,
    effective_assortment_status,
    line_blocker_details,
    line_blockers,
    normalize_guid,
    onec_binary_ref_to_guid,
    order_blocker_details,
    order_blockers,
    preview_supplier_distribution,
    record_order_exchange_result,
    record_property_update_exchange_result,
    reject_classification_proposal,
    select_line_main_supplier,
    transmit_order,
    update_order_line,
)
from app.services.procurement_order_formation_workspace import (
    assemble_assistant_orders,
    build_order_assistant,
    list_classification_proposals,
)

ONEC_REF = "0xBDB90025901E48EF11E1967C2685293E"
PRODUCT_GUID = "2685293e-967c-11e1-bdb9-0025901e48ef"


def _session(user_id: str = "42") -> ProcurementOrderFormationSession:
    return ProcurementOrderFormationSession(
        actor=f"bitrix:member:{user_id}",
        domain="crm.example.test",
        member_id="member",
        user_id=user_id,
        expires_at=datetime.now(UTC),
        user_name="Омар",
    )


def _order(db_session) -> ProcurementOrderFormation:
    order = ProcurementOrderFormation(
        stable_key="proc-order:test:1",
        status="draft",
        version=1,
        bitrix_entity_type_id=1200,
        bitrix_item_id="7001",
        bitrix_item_url="https://crm.example.test/crm/type/1200/details/7001/",
        supplier_ref="0xsupplier",
        supplier_name="Поставщик тест",
        contract_ref="0xcontract",
        contract_name="Основной договор",
        warehouse_code="MAIN",
        warehouse_name="Центральный склад",
        currency="RUB",
        procurement_contour="ordinary",
        route="ordinary",
        batch_id="2026-07-10",
        order_date=date(2026, 7, 10),
        calculation_id="display-auto-order-2026-07-10",
    )
    order.lines = [
        ProcurementOrderFormationLine(
            stable_key="line:1",
            line_number=1,
            bitrix_product_id="1646",
            bitrix_product_xml_id=PRODUCT_GUID,
            nomenclature_ref=ONEC_REF,
            nomenclature_code="РБ000006737",
            nomenclature_name="Дисплей тест",
            recommended_quantity=Decimal("5"),
            final_quantity=Decimal("5"),
            purchase_price=Decimal("115"),
            amount=Decimal("575"),
            currency="RUB",
            lifecycle_status="Продажа",
            assortment_status="Продажа",
        ),
        ProcurementOrderFormationLine(
            stable_key="line:2",
            line_number=2,
            bitrix_product_id="1647",
            bitrix_product_xml_id="11111111-2222-3333-4444-555555555555",
            nomenclature_ref="11111111-2222-3333-4444-555555555555",
            nomenclature_code="РБ000006738",
            nomenclature_name="Дисплей тест 2",
            recommended_quantity=Decimal("2"),
            final_quantity=Decimal("2"),
            purchase_price=Decimal("200"),
            amount=Decimal("400"),
            currency="RUB",
            lifecycle_status="Рабочий",
            assortment_status="Рабочий",
        ),
    ]
    db_session.add(order)
    db_session.commit()
    return order


def test_onec_binary_reference_matches_commerceml_guid() -> None:
    assert onec_binary_ref_to_guid(ONEC_REF) == PRODUCT_GUID
    assert normalize_guid(ONEC_REF) == PRODUCT_GUID


def test_catalog_lookup_uses_normalized_guid_only(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_call(_method, params, **_kwargs):
        calls.append(params)
        return {"result": [{"ID": "1646", "NAME": "Дисплей тест", "XML_ID": PRODUCT_GUID}]}

    monkeypatch.setattr(bitrix_order_service, "bitrix_call", fake_call)
    product = bitrix_order_service.resolve_catalog_product_by_xml_id(
        ONEC_REF,
        settings=Settings(),
        mapping={
            "catalog": {
                "product_id": "ID",
                "name": "NAME",
                "xml_id": "XML_ID",
            }
        },
    )

    assert product is not None
    assert calls[0]["filter"] == {"XML_ID": PRODUCT_GUID}


def test_catalog_product_keeps_preview_and_original_photo_separate(monkeypatch) -> None:
    def fake_call(_method, _params, **_kwargs):
        return {
            "result": [
                {
                    "ID": "1646",
                    "NAME": "Дисплей тест",
                    "XML_ID": PRODUCT_GUID,
                    "PREVIEW_PICTURE": {"showUrl": "/upload/thumb/display.jpg"},
                    "DETAIL_PICTURE": {
                        "downloadUrl": "https://cdn.example.test/display-original.jpg"
                    },
                }
            ]
        }

    monkeypatch.setattr(bitrix_order_service, "bitrix_call", fake_call)
    product = bitrix_order_service.resolve_catalog_product_by_xml_id(
        PRODUCT_GUID,
        settings=Settings(),
        mapping={"catalog": {"product_id": "ID", "name": "NAME", "xml_id": "XML_ID"}},
    )

    assert product is not None
    assert product.photo_thumbnail_url == "/upload/thumb/display.jpg"
    assert product.photo_original_url == "https://cdn.example.test/display-original.jpg"
    assert bitrix_order_service._decimal_or_none("1,5") == Decimal("1.5")


def test_catalog_batch_lookup_resolves_products_in_one_call(monkeypatch) -> None:
    second_guid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_call(method, params, **_kwargs):
        calls.append((method, params))
        return {
            "result": {
                "result": {
                    "catalog_0": [{"ID": "1646", "NAME": "Дисплей тест", "XML_ID": PRODUCT_GUID}],
                    "catalog_1": [{"ID": "1647", "NAME": "Дисплей два", "XML_ID": second_guid}],
                },
                "result_error": {},
            }
        }

    monkeypatch.setattr(bitrix_order_service, "bitrix_call", fake_call)
    products = bitrix_order_service.resolve_catalog_products_by_xml_ids(
        [ONEC_REF, second_guid, PRODUCT_GUID],
        settings=Settings(),
        mapping={
            "catalog": {
                "product_id": "ID",
                "name": "NAME",
                "xml_id": "XML_ID",
            }
        },
    )

    assert set(products) == {PRODUCT_GUID, second_guid}
    assert products[PRODUCT_GUID].product_id == "1646"
    assert len(calls) == 1
    assert calls[0][0] == "batch"
    assert calls[0][1]["halt"] == 1
    commands = calls[0][1]["cmd"]
    assert set(commands) == {"catalog_0", "catalog_1"}
    assert f"filter%5BXML_ID%5D={PRODUCT_GUID}" in commands["catalog_0"]
    assert f"filter%5BXML_ID%5D={second_guid}" in commands["catalog_1"]


def test_line_blockers_require_exact_guid_and_catalog_product(db_session) -> None:
    order = _order(db_session)
    line = order.lines[0]
    assert line_blockers(line) == []

    line.bitrix_product_xml_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert "catalog_xml_id_mismatch" in line_blockers(line)


def test_line_change_marks_manual_override(db_session) -> None:
    order = _order(db_session)
    updated = update_order_line(
        db_session,
        order.id,
        order.lines[0].id,
        {"final_quantity": Decimal("7")},
    )

    assert updated.version == 2
    assert updated.status == "draft"
    assert updated.lines[0].amount == Decimal("805.00")
    assert updated.lines[0].payload["manual_overrides"] == {"final_quantity": True}


def test_approved_order_is_frozen_for_manual_changes(db_session) -> None:
    order = _order(db_session)
    approved = approve_order(db_session, order.id, _session())

    with pytest.raises(ValueError, match="approved order is read-only"):
        update_order_line(
            db_session,
            order.id,
            approved.lines[0].id,
            {"final_quantity": Decimal("7")},
        )

    db_session.refresh(order)
    assert order.status == "approved"
    assert order.approved_version == 1
    assert order.lines[0].final_quantity == Decimal("5")


def test_line_change_rejects_stale_expected_version(db_session) -> None:
    order = _order(db_session)

    with pytest.raises(VersionConflictError, match="order version changed"):
        update_order_line(
            db_session,
            order.id,
            order.lines[0].id,
            {
                "expected_order_version": 2,
                "expected_line_version": 1,
                "final_quantity": Decimal("7"),
            },
        )


def test_in_app_supplier_selection_stays_pending_until_onec_confirms(
    db_session,
    monkeypatch,
) -> None:
    order = _order(db_session)
    order.supplier_ref = None
    order.supplier_code = None
    order.supplier_name = "Не определён"
    db_session.commit()

    class DummyEngine:
        def dispose(self) -> None:
            pass

    monkeypatch.setattr(order_service, "build_engine", lambda *_args, **_kwargs: DummyEngine())
    monkeypatch.setattr(
        order_service,
        "fetch_onec_supplier_by_ref",
        lambda *_args, **_kwargs: {"ref": "0xnew", "code": "S9", "name": "Samsung display"},
    )
    monkeypatch.setattr(
        order_service,
        "fetch_onec_nomenclature_by_codes",
        lambda *_args, **_kwargs: {
            "РБ000006737": {
                "nomenclature_code": "РБ000006737",
                "main_supplier_ref": "",
            }
        },
    )

    updated = select_line_main_supplier(
        db_session,
        order.id,
        order.lines[0].id,
        {
            "expected_order_version": 1,
            "expected_line_version": 1,
            "supplier_ref": "0xnew",
            "supplier_code": "S9",
            "supplier_name": "Samsung display",
        },
        _session(),
        settings=Settings(onec_database_url="mssql+pyodbc://test"),
    )

    selection = updated.lines[0].payload["main_supplier_selection"]
    assert selection["ref"] == "0xnew"
    assert selection["status"] == "pending_onec_write"
    assert updated.version == 2


def test_onec_main_supplier_wins_over_in_app_selection(db_session, monkeypatch) -> None:
    order = _order(db_session)
    order.supplier_ref = None
    order.supplier_code = None
    order.supplier_name = "Не определён"
    db_session.commit()

    class DummyEngine:
        def dispose(self) -> None:
            pass

    monkeypatch.setattr(order_service, "build_engine", lambda *_args, **_kwargs: DummyEngine())
    monkeypatch.setattr(
        order_service,
        "fetch_onec_supplier_by_ref",
        lambda *_args, **_kwargs: {"ref": "0xnew", "code": "S9", "name": "Samsung display"},
    )
    monkeypatch.setattr(
        order_service,
        "fetch_onec_nomenclature_by_codes",
        lambda *_args, **_kwargs: {
            "РБ000006737": {
                "main_supplier_ref": "0xmanual",
                "main_supplier_code": "S1",
                "main_supplier_name": "Изменён вручную в 1С",
            }
        },
    )

    with pytest.raises(ValueError, match="changed in 1C"):
        select_line_main_supplier(
            db_session,
            order.id,
            order.lines[0].id,
            {
                "expected_order_version": 1,
                "expected_line_version": 1,
                "supplier_ref": "0xnew",
                "supplier_code": "S9",
                "supplier_name": "Samsung display",
            },
            _session(),
            settings=Settings(onec_database_url="mssql+pyodbc://test"),
        )


def test_supplier_review_room_previews_and_moves_resolved_lines(db_session, monkeypatch) -> None:
    source = _order(db_session)
    source.supplier_ref = None
    source.supplier_code = None
    source.supplier_name = "Не определён"
    source.lines[0].blockers = ["supplier_1c_reference_missing"]
    supplier = {"ref": "0xnew", "code": "S9", "name": "Samsung display"}
    db_session.commit()

    def resolved(order, *, settings=None):
        del settings
        line = next(item for item in order.lines if item.nomenclature_code == "РБ000006737")
        unresolved = [item for item in order.lines if item is not line]
        return {"0xnew": [(line, supplier, "pending_onec_write")]}, unresolved

    monkeypatch.setattr(order_service, "_resolved_line_suppliers", resolved)

    preview = preview_supplier_distribution(db_session, source.id)
    assert preview["groups"][0]["supplier_name"] == "Samsung display"
    assert preview["groups"][0]["line_numbers"] == [1]
    assert preview["unresolved_line_numbers"] == [2]

    updated_source, target_ids, moved = distribute_lines_by_suppliers(
        db_session,
        source.id,
        expected_order_version=1,
        session=_session(),
    )

    assert moved == 1
    assert len(target_ids) == 1
    target = db_session.get(ProcurementOrderFormation, target_ids[0])
    assert target is not None
    assert target.supplier_ref == "0xnew"
    assert [line.nomenclature_code for line in target.lines] == ["РБ000006737"]
    assert "supplier_1c_reference_missing" not in target.lines[0].blockers
    assert target.lines[0].payload["main_supplier_selection"]["status"] == "pending_onec_write"
    assert [line.nomenclature_code for line in updated_source.lines] == ["РБ000006738"]


def test_supplier_review_room_reuses_unsent_order_from_earlier_batch(
    db_session,
    monkeypatch,
) -> None:
    source = _order(db_session)
    source.supplier_ref = None
    source.supplier_code = None
    source.supplier_name = "Не определён"
    source.batch_id = "2026-08-21"
    source.order_date = date(2026, 8, 21)
    source.stable_key = "proc-order:review-room-reuse"
    source.bitrix_item_id = "review-room-reuse"
    source.lines[0].stable_key = "line:review-room-reuse:1"
    source.lines[1].stable_key = "line:review-room-reuse:2"
    db_session.commit()

    target = _order(db_session)
    target.stable_key = "proc-order:earlier-open"
    target.batch_id = "2026-08-20"
    target.order_date = date(2026, 8, 20)
    target.supplier_ref = "0xnew"
    target.supplier_code = "S9"
    target.supplier_name = "Samsung display"
    target.lines[0].stable_key = "line:earlier"
    db_session.commit()

    supplier = {"ref": "0xnew", "code": "S9", "name": "Samsung display"}

    def resolved(order, *, settings=None):
        del settings
        return {"0xnew": [(order.lines[0], supplier, "pending_onec_write")]}, []

    monkeypatch.setattr(order_service, "_resolved_line_suppliers", resolved)

    preview = preview_supplier_distribution(db_session, source.id)

    assert preview["groups"][0]["target_order_id"] == target.id
    assert preview["groups"][0]["target_order_status"] == "existing"

    _updated_source, target_ids, moved = distribute_lines_by_suppliers(
        db_session,
        source.id,
        expected_order_version=1,
        session=_session(),
    )

    assert moved == 1
    assert target_ids == [target.id]
    db_session.refresh(target)
    assert [line.nomenclature_code for line in target.lines] == [
        "РБ000006737",
        "РБ000006738",
        "РБ000006737",
    ]


def test_supplier_review_room_does_not_reuse_order_already_exchanged_with_onec(
    db_session,
    monkeypatch,
) -> None:
    source = _order(db_session)
    source.supplier_ref = None
    source.supplier_code = None
    source.supplier_name = "Не определён"
    source.batch_id = "2026-08-21"
    source.stable_key = "proc-order:review-room-exchanged"
    source.bitrix_item_id = "review-room-exchanged"
    source.lines[0].stable_key = "line:review-room-exchanged:1"
    source.lines[1].stable_key = "line:review-room-exchanged:2"
    db_session.commit()

    exchanged = _order(db_session)
    exchanged.stable_key = "proc-order:already-exchanged"
    exchanged.batch_id = "2026-08-20"
    exchanged.supplier_ref = "0xnew"
    exchanged.supplier_code = "S9"
    exchanged.supplier_name = "Samsung display"
    exchanged.onec_status = "transmitted"
    exchanged.lines[0].stable_key = "line:already-exchanged"
    db_session.commit()

    supplier = {"ref": "0xnew", "code": "S9", "name": "Samsung display"}

    def resolved(order, *, settings=None):
        del settings
        return {"0xnew": [(order.lines[0], supplier, "pending_onec_write")]}, []

    monkeypatch.setattr(order_service, "_resolved_line_suppliers", resolved)

    preview = preview_supplier_distribution(db_session, source.id)

    assert preview["groups"][0]["target_order_id"] is None
    assert preview["groups"][0]["target_order_status"] == "new"


def test_order_does_not_require_legacy_bitrix_card_url(db_session) -> None:
    order = _order(db_session)
    order.bitrix_item_url = None
    order.bitrix_item_id = None

    assert "bitrix_item_url_missing" not in order_blockers(order)

    transmitted, mode, message_id, xml_preview, path = transmit_order(
        db_session,
        order.id,
        _session(),
        settings=Settings(procurement_order_formation_onec_apply_enabled=False),
    )

    assert transmitted.onec_status == "dry_run"
    assert mode == "dry_run"
    assert message_id == f"proc-order-{order.id}-v1"
    assert path is None
    assert "<BitrixItemUrl" in xml_preview


def test_order_assistant_exposes_original_photos_and_real_supplier_history(db_session) -> None:
    order = _order(db_session)
    order.payload = {
        "supplier_profile": {
            "qualification_class": "a",
            "profitability_pct": "34.6",
            "defect_pct": "0.8",
            "defect_history_units": 1842,
            "on_time_pct": "94",
            "payment_terms": "30/70",
            "credit_days": 45,
            "advantages": ["Компенсация брака"],
            "updated_at": "2026-08-01",
        }
    }
    for index, line in enumerate(order.lines, start=1):
        line.payload = {
            "product_card_url": f"https://master-mobile.ru/catalog/displei/{index}/",
            "photos": [
                {
                    "thumbnail": f"https://cdn.example.test/thumb/{index}.jpg",
                    "original": f"https://cdn.example.test/original/{index}.jpg",
                }
            ],
            "photo_source": "master_mobile_site",
            "profitability_pct": "34.6",
            "supplier_defect_pct": "0.8",
            "supplier_defect_history_units": 1842,
        }
    db_session.commit()

    payload = build_order_assistant(db_session)
    validated = ProcurementOrderAssistantResponse.model_validate(payload)

    assert validated.summary.lines == 2
    assert validated.summary.ready_lines == 2
    assert validated.summary.photo_missing_lines == 0
    assert validated.orders[0].supplier_profile.qualification_class == "A"
    assert validated.orders[0].supplier_profile.defect_pct == Decimal("0.8")
    assert validated.orders[0].supplier_profile.updated_at.date() == date(2026, 8, 1)
    assert validated.orders[0].lines[0].photo_original_url.endswith("/1.jpg")
    assert validated.orders[0].lines[0].product_card_url == (
        "https://master-mobile.ru/catalog/displei/1/"
    )
    assert validated.orders[0].lines[0].photo_source == "master_mobile_site"


def test_order_assistant_marks_lines_ready_only_when_the_whole_project_is_ready(
    db_session,
) -> None:
    order = _order(db_session)
    order.lines[0].payload = {
        "product_card_url": "https://master-mobile.ru/catalog/displei/1/",
        "photos": [{"original": "https://master-mobile.ru/upload/ready.webp"}],
    }
    order.lines[1].payload = {}
    db_session.commit()

    payload = build_order_assistant(db_session)

    assert payload["summary"]["lines"] == 2
    assert payload["summary"]["ready_lines"] == 0


def test_order_assistant_blocks_assembly_when_original_photo_is_missing(db_session) -> None:
    order = _order(db_session)
    for index, line in enumerate(order.lines, start=1):
        line.payload = {
            "product_card_url": f"https://master-mobile.ru/catalog/displei/{index}/",
            "photos": [{"thumbnail": f"https://cdn.example.test/thumb/{index}.jpg"}],
        }
    db_session.commit()

    result = assemble_assistant_orders(
        db_session,
        items=[{"order_id": order.id, "expected_version": order.version}],
        idempotency_key="assistant-test-missing-photo",
        session=_session(),
    )

    assert result["approved"] == 0
    assert result["blocked"] == 1
    assert "Нет исходного фото" in result["items"][0]["message"]
    assert db_session.get(ProcurementOrderFormation, order.id).status == "draft"


def test_order_assistant_blocks_assembly_when_product_card_is_missing(db_session) -> None:
    order = _order(db_session)
    for index, line in enumerate(order.lines, start=1):
        line.payload = {"photos": [{"original": f"https://cdn.example.test/original/{index}.jpg"}]}
    db_session.commit()

    result = assemble_assistant_orders(
        db_session,
        items=[{"order_id": order.id, "expected_version": order.version}],
        idempotency_key="assistant-test-missing-card",
        session=_session(),
    )

    assert result["approved"] == 0
    assert result["blocked"] == 1
    assert "Нет подтверждённой карточки товара" in result["items"][0]["message"]
    assert db_session.get(ProcurementOrderFormation, order.id).status == "draft"


def test_order_assistant_assembles_project_without_sending_to_onec(db_session) -> None:
    order = _order(db_session)
    for index, line in enumerate(order.lines, start=1):
        line.payload = {
            "product_card_url": f"https://master-mobile.ru/catalog/displei/{index}/",
            "photos": [{"original": f"https://cdn.example.test/original/{index}.jpg"}],
        }
    db_session.commit()

    result = assemble_assistant_orders(
        db_session,
        items=[{"order_id": order.id, "expected_version": order.version}],
        idempotency_key="assistant-test-ready-order",
        session=_session(),
    )
    refreshed = db_session.get(ProcurementOrderFormation, order.id)

    assert result["approved"] == 1
    assert refreshed.status == "approved"
    assert refreshed.onec_status != "pending"
    assert refreshed.onec_document_number is None
    assert build_order_assistant(db_session)["orders"] == []


def test_order_assistant_cannot_reopen_order_already_transmitted_to_onec(db_session) -> None:
    order = _order(db_session)
    order.status = "transmitted"
    order.onec_status = "transmitted"
    db_session.commit()

    result = assemble_assistant_orders(
        db_session,
        items=[{"order_id": order.id, "expected_version": order.version}],
        idempotency_key="assistant-test-transmitted-order",
        session=_session(),
    )

    assert result["approved"] == 0
    assert result["blocked"] == 1
    assert db_session.get(ProcurementOrderFormation, order.id).status == "transmitted"


def test_manual_minimum_requires_review_date(db_session) -> None:
    order = _order(db_session)
    with pytest.raises(ValueError, match="review date"):
        create_classification_proposal(
            db_session,
            order.id,
            order.lines[0].id,
            {
                "proposed_status": "matrix",
                "reason": "Приоритетная матрица",
                "manual_minimum": "3",
            },
            _session(),
        )


def test_pension_requires_replacement_code_or_explicit_absence(db_session) -> None:
    order = _order(db_session)
    line_id = order.lines[0].id
    with pytest.raises(ValueError, match="replacement nomenclature code is required"):
        create_classification_proposal(
            db_session,
            order.id,
            line_id,
            {
                "proposed_status": "pension",
                "reason": "Ведём аналог дешевле",
            },
            _session("77"),
        )

    order = create_classification_proposal(
        db_session,
        order.id,
        line_id,
        {
            "proposed_status": "pension",
            "reason": "Модель снята с производства",
            "no_replacement": True,
        },
        _session("77"),
    )
    proposal = order.lines[0].classification_proposals[0]
    assert proposal.replacement_sku_code is None


def test_pension_is_approved_by_one_person_and_blocks_the_line(db_session) -> None:
    order = _order(db_session)
    order = create_classification_proposal(
        db_session,
        order.id,
        order.lines[0].id,
        {
            "proposed_status": "pension",
            "reason": "Ведём РБ000057818 вместо этой карточки",
            "replacement_sku_code": "РБ000057818",
        },
        _session("77"),
    )
    proposal = order.lines[0].classification_proposals[0]

    # Решение 2026-08-18: второе согласование для «Допродаём» не требуется.
    assert proposal.status == "approved"
    assert proposal.approved_by_bitrix_user_id == "77"
    assert proposal.replacement_sku_code == "РБ000057818"
    assert proposal.blocks_order_line is True
    assert proposal.onec_status == "not_applicable"
    assert effective_assortment_status(order.lines[0]) == "pension"


def test_stop_statuses_and_on_demand_rules() -> None:
    assert classification_blocks_line("do_not_order", explicit_demand=True)
    assert classification_blocks_line("replace_candidate", explicit_demand=True)
    assert classification_blocks_line("nonliquid", explicit_demand=True)
    assert classification_blocks_line("pension", explicit_demand=True)
    assert classification_blocks_line("on_demand", explicit_demand=False)
    assert not classification_blocks_line("on_demand", explicit_demand=True)
    assert not classification_blocks_line("working", explicit_demand=False)


def test_classification_approval_checks_permission_and_stays_internal(
    db_session,
) -> None:
    order = _order(db_session)
    order = create_classification_proposal(
        db_session,
        order.id,
        order.lines[0].id,
        {
            "proposed_status": "matrix",
            "reason": "Приоритетная матрица",
            "manual_minimum": "3",
            "review_date": date(2026, 8, 10),
        },
        _session("77"),
    )
    proposal = order.lines[0].classification_proposals[0]
    with pytest.raises(PermissionError):
        approve_classification_proposal(
            db_session,
            order.id,
            order.lines[0].id,
            proposal.id,
            _session("99"),
            settings=Settings(procurement_order_formation_classification_approver_user_ids=["42"]),
        )

    refreshed, approved, mode, xml_preview, path = approve_classification_proposal(
        db_session,
        order.id,
        order.lines[0].id,
        proposal.id,
        _session("42"),
        settings=Settings(
            procurement_order_formation_classification_approver_user_ids=["42"],
            procurement_order_formation_property_apply_enabled=False,
        ),
    )

    assert refreshed.approved_version is None
    assert approved.status == "approved"
    assert mode == "internal"
    assert path is None
    assert xml_preview == ""
    assert approved.onec_message_id is None
    assert approved.onec_status == "not_applicable"
    assert approved.payload["storage"] == "pricing-service"
    assert approved.payload["legacy_onec_export_disabled"] is True


def test_classification_rejection_requires_other_approver_reason_and_versions(
    db_session,
) -> None:
    order = _order(db_session)
    order = create_classification_proposal(
        db_session,
        order.id,
        order.lines[0].id,
        {
            "proposed_status": "matrix",
            "reason": "Предложение автора",
        },
        _session("77"),
    )
    line = order.lines[0]
    proposal = line.classification_proposals[0]
    settings = Settings(procurement_order_formation_classification_approver_user_ids=["42", "77"])
    with pytest.raises(PermissionError):
        reject_classification_proposal(
            db_session,
            order.id,
            line.id,
            proposal.id,
            {
                "expected_order_version": order.version,
                "expected_line_version": line.version,
                "reason": "Самосогласование",
            },
            _session("77"),
            settings=settings,
        )
    with pytest.raises(ValueError, match="reason"):
        reject_classification_proposal(
            db_session,
            order.id,
            line.id,
            proposal.id,
            {
                "expected_order_version": order.version,
                "expected_line_version": line.version,
                "reason": "",
            },
            _session("42"),
            settings=settings,
        )

    previous_order_version = order.version
    previous_line_version = line.version
    refreshed, rejected = reject_classification_proposal(
        db_session,
        order.id,
        line.id,
        proposal.id,
        {
            "expected_order_version": order.version,
            "expected_line_version": line.version,
            "reason": "Недостаточно оснований",
        },
        _session("42"),
        settings=settings,
    )

    assert rejected.status == "rejected"
    assert rejected.rejection_reason == "Недостаточно оснований"
    assert rejected.onec_status == "not_sent"
    assert refreshed.version == previous_order_version + 1
    assert refreshed.lines[0].version == previous_line_version + 1
    assert "classification_approval_pending" not in line_blockers(refreshed.lines[0])


def test_classification_decisions_can_share_transaction_with_audit_event(db_session) -> None:
    order = _order(db_session)
    order = create_classification_proposal(
        db_session,
        order.id,
        order.lines[0].id,
        {
            "proposed_status": "matrix",
            "reason": "Проверка атомарного решения",
        },
        _session("77"),
    )
    line = order.lines[0]
    proposal = line.classification_proposals[0]
    order_version = order.version
    line_version = line.version
    settings = Settings(procurement_order_formation_classification_approver_user_ids=["42"])

    approve_classification_proposal(
        db_session,
        order.id,
        line.id,
        proposal.id,
        _session("42"),
        settings=settings,
        commit=False,
    )
    db_session.rollback()
    db_session.expire_all()
    rolled_back = db_session.get(ProcurementOrderFormation, order.id)
    assert rolled_back is not None
    assert rolled_back.version == order_version
    assert rolled_back.lines[0].version == line_version
    assert rolled_back.lines[0].classification_proposals[0].status == "proposed"

    reject_classification_proposal(
        db_session,
        order.id,
        line.id,
        proposal.id,
        {
            "expected_order_version": order_version,
            "expected_line_version": line_version,
            "reason": "Проверка отклонения",
        },
        _session("42"),
        settings=settings,
        commit=False,
    )
    db_session.rollback()
    db_session.expire_all()
    rolled_back = db_session.get(ProcurementOrderFormation, order.id)
    assert rolled_back is not None
    assert rolled_back.version == order_version
    assert rolled_back.lines[0].version == line_version
    assert rolled_back.lines[0].classification_proposals[0].status == "proposed"


def test_metric_blockers_use_only_agreed_thresholds(db_session) -> None:
    order = _order(db_session)
    line = order.lines[0]
    line.payload = {
        "profitability_pct": "-20",
        "product_defect_pct": "40",
        "supplier_defect_attribution": "unconfirmed",
        "price_change_pct": "10",
    }
    assert "purchase_price_change_over_10_pct" not in line_blockers(line)
    assert "supplier_defect_over_10_pct_reliable" not in line_blockers(line)

    line.payload = {"price_change_pct": "10.01"}
    assert "purchase_price_change_over_10_pct" in line_blockers(line)
    line.payload = {
        "supplier_defect_attribution": "supplier_exact",
        "supplier_defect_pct": "10.01",
        "supplier_defect_history_units": 100,
    }
    assert "supplier_defect_over_10_pct_reliable" in line_blockers(line)


def test_batch_blocker_details_explain_numbers_and_project_lines(db_session) -> None:
    order = _order(db_session)
    line = order.lines[0]
    line.blockers = ["batch_error_suspected"]
    line.payload = {
        "batch_error_return_qty": "8",
        "batch_error_share_pct": "44.4",
    }
    db_session.commit()

    detail = line_blocker_details(line)[0]
    assert detail["severity"] == "hard"
    assert detail["evidence"]["return_qty"] == Decimal("8")
    assert detail["evidence"]["share_pct"] == Decimal("44.4")
    assert "8 возвратов" in detail["message"]
    assert "44,4%" in detail["message"]
    assert {item["kind"] for item in detail["resolution_actions"]} == {
        "remove_line",
        "remove_with_replacement",
        "recalculate",
    }

    project_detail = next(
        item for item in order_blocker_details(order) if item["code"] == "batch_error_suspected"
    )
    assert project_detail["scope"] == "order"
    assert project_detail["line_id"] == line.id
    assert project_detail["line_number"] == 1

    line.payload = {
        "batch_error_return_qty": "24",
        "batch_error_share_pct": "72.7",
    }
    detail = line_blocker_details(line)[0]
    assert "24 возврата" in detail["message"]
    assert "72,7%" in detail["message"]


def test_batch_blocker_without_evidence_becomes_technical(db_session) -> None:
    order = _order(db_session)
    line = order.lines[0]
    line.blockers = ["batch_error_suspected"]
    line.payload = {}

    detail = line_blocker_details(line)[0]

    assert detail["severity"] == "technical"
    assert "повторный расчёт" in detail["message"]


def test_removing_blocked_line_requires_reason_and_keeps_audit_metadata(db_session) -> None:
    order = _order(db_session)
    line = order.lines[0]
    line.blockers = ["batch_error_suspected"]
    db_session.commit()

    with pytest.raises(ValueError, match="removal reason"):
        update_order_line(db_session, order.id, line.id, {"removed": True})

    updated = update_order_line(
        db_session,
        order.id,
        line.id,
        {
            "removed": True,
            "removal_reason": "Проверяем пересорт отдельно",
            "replacement_sku_code": "РБ000057818",
            "_removal_actor": "bitrix:member:42",
            "_removal_actor_name": "Омар",
        },
    )
    removed_line = next(item for item in updated.lines if item.id == line.id)
    assert removed_line.removed is True
    assert removed_line.payload["manual_removal"]["reason"] == "Проверяем пересорт отдельно"
    assert removed_line.payload["manual_removal"]["replacement_sku_code"] == "РБ000057818"
    assert all("line_1:batch_error_suspected" != item for item in order_blockers(updated))


def test_removing_line_through_api_records_actor_reason_and_replacement(db_session) -> None:
    order = _order(db_session)
    line = order.lines[0]
    line.blockers = ["batch_error_suspected"]
    db_session.commit()

    change_order_line(
        order.id,
        line.id,
        ProcurementOrderLineUpdateRequest(
            expected_order_version=order.version,
            expected_line_version=line.version,
            removed=True,
            removal_reason="Проверяем пересорт отдельно",
            replacement_sku_code="РБ000057818",
        ),
        db_session,
        _session(),
    )

    event = db_session.scalar(
        select(ProcurementOrderFormationEvent).where(
            ProcurementOrderFormationEvent.event_type == "order_line_removed"
        )
    )
    assert event is not None
    assert event.actor == "bitrix:member:42"
    assert event.payload["removal_reason"] == "Проверяем пересорт отдельно"
    assert event.payload["replacement_sku_code"] == "РБ000057818"


def test_supplier_order_contract_has_one_header_and_multiple_draft_lines(db_session) -> None:
    order = _order(db_session)
    order = approve_order(db_session, order.id, _session())

    transmitted, mode, message_id, xml_preview, path = transmit_order(
        db_session,
        order.id,
        _session(),
        settings=Settings(procurement_order_formation_onec_apply_enabled=False),
    )

    assert transmitted.status == "draft"
    assert mode == "dry_run"
    assert message_id == f"proc-order-{order.id}-v1"
    assert path is None
    root = ET.fromstring(xml_preview.encode("windows-1251"))
    supplier_orders = root.findall("SupplierOrders/SupplierOrder")
    assert len(supplier_orders) == 1
    assert supplier_orders[0].findtext("DraftOnly") == "true"
    assert len(supplier_orders[0].findall("Lines/Line")) == 2


def test_repeated_transmission_of_same_version_is_idempotent(db_session) -> None:
    order = _order(db_session)
    settings = Settings(procurement_order_formation_onec_apply_enabled=False)

    first = transmit_order(db_session, order.id, _session(), settings=settings)
    second = transmit_order(db_session, order.id, _session(), settings=settings)

    assert second[1] == first[1] == "dry_run"
    assert second[2] == first[2] == f"proc-order-{order.id}-v1"
    assert second[3] == first[3]


def test_transmitted_order_is_read_only(db_session) -> None:
    order = _order(db_session)
    order.status = "transmitted"
    order.onec_status = "transmitted"
    db_session.commit()

    with pytest.raises(ValueError, match="read-only"):
        update_order_line(
            db_session,
            order.id,
            order.lines[0].id,
            {
                "expected_order_version": order.version,
                "expected_line_version": order.lines[0].version,
                "final_quantity": Decimal("7"),
            },
        )


def test_bitrix_product_rows_use_purchase_price_and_catalog_id(db_session) -> None:
    order = _order(db_session)

    rows = build_bitrix_product_rows(order)

    assert rows[0]["productId"] == 1646
    assert rows[0]["price"] == "115.0000"
    assert rows[0]["quantity"] == "5.000"
    assert "retailPrice" not in rows[0]


def test_property_message_builder_rejects_missing_nomenclature_code(db_session) -> None:
    order = _order(db_session)
    line = order.lines[0]
    line.nomenclature_code = None
    proposal = ProcurementClassificationProposal(
        line=line,
        proposed_status="working",
        reason="Проверено",
        requested_by_actor="actor",
        requested_by_bitrix_user_id="42",
        idempotency_key="proposal:test",
        approved_by_name="Омар",
    )
    with pytest.raises(ValueError, match="nomenclature code"):
        build_classification_update_message(proposal, line=line, mode="dry_run")


def test_exporters_still_validate_generated_messages(db_session) -> None:
    order = _order(db_session)
    order_message = build_order_message(order, mode="dry_run", approved_by="Омар")
    assert build_procurement_supplier_orders_xml(order_message)

    proposal = ProcurementClassificationProposal(
        line=order.lines[0],
        proposed_status="working",
        previous_status="sale",
        reason="Проверено",
        requested_by_actor="actor",
        requested_by_bitrix_user_id="42",
        idempotency_key="proposal:test:xml",
        approved_by_name="Омар",
        approved_at=datetime(2026, 7, 10),
    )
    property_message = build_classification_update_message(
        proposal, line=order.lines[0], mode="dry_run"
    )
    with pytest.raises(ValueError, match="lifecycle property export"):
        build_nomenclature_property_updates_xml(property_message)


def test_onec_order_result_marks_card_transmitted(db_session) -> None:
    order = _order(db_session)
    order.onec_message_id = "proc-order-result-1"
    order.onec_status = "pending"
    order.status = "transmitting"
    db_session.commit()

    refreshed = record_order_exchange_result(
        db_session,
        ProcurementSupplierOrderExchangeResult(
            message_id="proc-order-result-1",
            status="success",
            processed_at="2026-07-10T12:00:00",
            loaded=1,
            failed=0,
            errors="",
            item_results=(
                ProcurementSupplierOrderItemResult(
                    idempotency_key="order-key",
                    result="created",
                    onec_document_ref="0xorder",
                    onec_document_number="РБ000001",
                    onec_document_date="2026-07-10",
                ),
            ),
        ),
    )

    assert refreshed is not None
    assert refreshed.status == "transmitted"
    assert refreshed.onec_document_number == "РБ000001"


def test_onec_property_conflict_is_kept_for_manual_resolution(db_session) -> None:
    order = _order(db_session)
    proposal = ProcurementClassificationProposal(
        line=order.lines[0],
        status="sent_to_1c",
        proposed_status="matrix",
        reason="Матрица",
        requested_by_actor="actor",
        requested_by_bitrix_user_id="42",
        idempotency_key="proposal:conflict",
        onec_message_id="property-result-1",
        onec_status="pending",
    )
    db_session.add(proposal)
    db_session.commit()

    result = record_property_update_exchange_result(
        db_session,
        PropertyUpdateExchangeResult(
            message_id="property-result-1",
            status="failed",
            processed_at="2026-07-10T12:00:00",
            loaded=0,
            failed=1,
            errors="Конфликт текущего значения",
            item_results=(
                PropertyUpdateItemResult(
                    idempotency_key="proposal:conflict:status",
                    nomenclature_code="РБ000006737",
                    property_name="Статус ассортимента",
                    result="conflict",
                    message="Expected current value does not match",
                ),
            ),
        ),
    )

    assert result is not None
    assert result.status == "conflict"
    assert result.onec_status == "conflict"


def test_commerceml_readback_marks_classification_reflected(db_session, monkeypatch) -> None:
    order = _order(db_session)
    proposal = ProcurementClassificationProposal(
        line=order.lines[0],
        status="applied",
        proposed_status="matrix",
        reason="Матрица",
        requested_by_actor="actor",
        requested_by_bitrix_user_id="42",
        idempotency_key="proposal:readback",
        onec_status="success",
    )
    db_session.add(proposal)
    db_session.commit()
    monkeypatch.setattr(bitrix_order_service, "load_order_formation_mapping", lambda _settings: {})
    monkeypatch.setattr(
        bitrix_order_service,
        "resolve_catalog_product_by_xml_id",
        lambda *_args, **_kwargs: BitrixCatalogProduct(
            product_id="1646",
            name="Дисплей тест",
            xml_id=PRODUCT_GUID,
            assortment_status="Матричный",
        ),
    )

    summary = bitrix_order_service.reflect_classifications_from_bitrix(
        db_session,
        settings=Settings(),
    )

    db_session.refresh(proposal)
    assert summary == {"reflected": 1, "pending": 0, "missing": 0, "unrecognized": 0}
    assert proposal.status == "reflected"
    assert proposal.line.assortment_status == "matrix"


def test_commerceml_readback_flags_unrecognized_legacy_status_instead_of_pending_forever(
    db_session, monkeypatch
) -> None:
    # Регрессия: до фикса readback-значение вроде "Эксклюзив" (старый Bitrix enum,
    # который decide_assortment_status больше не знает — это CommercialMark, не статус)
    # молча оседало как "pending" навсегда, неотличимо от карточки, которая просто
    # ещё не долетела до 1С. Теперь такие значения помечаются отдельно.
    order = _order(db_session)
    proposal = ProcurementClassificationProposal(
        line=order.lines[0],
        status="applied",
        proposed_status="matrix",
        reason="Матрица",
        requested_by_actor="actor",
        requested_by_bitrix_user_id="42",
        idempotency_key="proposal:readback-legacy",
        onec_status="success",
    )
    db_session.add(proposal)
    db_session.commit()
    monkeypatch.setattr(bitrix_order_service, "load_order_formation_mapping", lambda _settings: {})
    monkeypatch.setattr(
        bitrix_order_service,
        "resolve_catalog_product_by_xml_id",
        lambda *_args, **_kwargs: BitrixCatalogProduct(
            product_id="1646",
            name="Дисплей тест",
            xml_id=PRODUCT_GUID,
            assortment_status="Эксклюзив",
        ),
    )

    summary = bitrix_order_service.reflect_classifications_from_bitrix(
        db_session,
        settings=Settings(),
    )

    db_session.refresh(proposal)
    assert summary == {"reflected": 0, "pending": 0, "missing": 0, "unrecognized": 1}
    assert proposal.status == "applied"
    assert proposal.bitrix_readback_value == "Эксклюзив"


def test_properties_queue_marks_own_proposal_as_not_approvable(db_session) -> None:
    # Правило «второго сотрудника» раньше срабатывало только на сервере: автор
    # видел активную кнопку «Принять» и упирался в английскую ошибку
    # `classification proposal cannot be self-approved`. Теперь очередь сама
    # говорит, кому решение доступно.
    order = _order(db_session)
    create_classification_proposal(
        db_session,
        order.id,
        order.lines[0].id,
        {"proposed_status": "nonliquid", "reason": "старая модель, выводим карточку"},
        _session("77"),
    )
    settings = Settings(procurement_order_formation_classification_approver_user_ids=["42", "77"])

    author_view = list_classification_proposals(
        db_session,
        session=_session("77"),
        settings=settings,
    )
    colleague_view = list_classification_proposals(
        db_session,
        session=_session("42"),
        settings=settings,
    )
    outsider_view = list_classification_proposals(
        db_session,
        session=_session("99"),
        settings=settings,
    )

    author_proposal = author_view["items"][0]["proposal"]
    assert (author_proposal["can_approve"], author_proposal["self_proposed"]) == (False, True)
    colleague_proposal = colleague_view["items"][0]["proposal"]
    assert (colleague_proposal["can_approve"], colleague_proposal["self_proposed"]) == (True, False)
    # Не допущенный сотрудник тоже не должен видеть активную кнопку.
    assert outsider_view["items"][0]["proposal"]["can_approve"] is False
