from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from xml.etree import ElementTree as ET

import pytest

import app.services.bitrix_order_formation as bitrix_order_service
from app.core.config import Settings
from app.models.procurement_order_formation import (
    ProcurementClassificationProposal,
    ProcurementOrderFormation,
    ProcurementOrderFormationLine,
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
    line_blockers,
    normalize_guid,
    onec_binary_ref_to_guid,
    order_blockers,
    record_order_exchange_result,
    record_property_update_exchange_result,
    transmit_order,
    update_order_line,
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


def test_line_blockers_require_exact_guid_and_catalog_product(db_session) -> None:
    order = _order(db_session)
    line = order.lines[0]
    assert line_blockers(line) == []

    line.bitrix_product_xml_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert "catalog_xml_id_mismatch" in line_blockers(line)


def test_line_change_increments_version_and_revokes_approval(db_session) -> None:
    order = _order(db_session)
    approved = approve_order(db_session, order.id, _session())
    assert approved.approved_version == 1

    updated = update_order_line(
        db_session,
        order.id,
        approved.lines[0].id,
        {"final_quantity": Decimal("7")},
    )

    assert updated.version == 2
    assert updated.status == "draft"
    assert updated.approved_version is None
    assert updated.lines[0].amount == Decimal("805.00")


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


def test_order_does_not_require_legacy_bitrix_card_url(db_session) -> None:
    order = _order(db_session)
    order.bitrix_item_url = None
    order.bitrix_item_id = None

    assert "bitrix_item_url_missing" not in order_blockers(order)


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


def test_stop_statuses_and_on_demand_rules() -> None:
    assert classification_blocks_line("do_not_order", explicit_demand=True)
    assert classification_blocks_line("replace_candidate", explicit_demand=True)
    assert classification_blocks_line("nonliquid", explicit_demand=True)
    assert classification_blocks_line("on_demand", explicit_demand=False)
    assert not classification_blocks_line("on_demand", explicit_demand=True)
    assert not classification_blocks_line("working", explicit_demand=False)


def test_classification_approval_checks_permission_and_builds_property_contract(
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
    assert mode == "dry_run"
    assert path is None
    root = ET.fromstring(xml_preview.encode("windows-1251"))
    status_row = next(
        item
        for item in root.findall("Items/Item")
        if item.findtext("PropertyName") == "Статус ассортимента"
    )
    assert status_row.findtext("NewValueTag") == "matrix"
    assert status_row.findtext("ExpectedCurrentValueName") == "Продажа"
    assert root.findtext("Header/Mode") == "dry_run"
    assert any(
        item.findtext("PropertyName") == "Ручной минимальный остаток"
        for item in root.findall("Items/Item")
    )


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
    assert build_nomenclature_property_updates_xml(property_message)


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
    assert summary == {"reflected": 1, "pending": 0, "missing": 0}
    assert proposal.status == "reflected"
    assert proposal.line.assortment_status == "matrix"
