from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from xml.etree import ElementTree as ET

import pytest

from app.models.procurement_order_formation import (
    ProcurementOrderFormation,
    ProcurementOrderFormationLine,
)
from app.services.bitrix_procurement_order_formation_auth import (
    ProcurementOrderFormationSession,
)
from app.services.exporters.ut103_nomenclature_properties import (
    NomenclaturePropertyUpdateRow,
    build_nomenclature_property_updates_xml,
)
from app.services.procurement_management_marks_export import (
    MANAGEMENT_MARK_PROPERTY_NAME,
    REPLACEMENT_PROPERTY_NAME,
    build_management_marks_message,
    collect_management_marks,
)
from app.services.procurement_order_formation import create_classification_proposal


def _session(user_id: str = "77") -> ProcurementOrderFormationSession:
    return ProcurementOrderFormationSession(
        actor=f"bitrix:member:{user_id}",
        domain="crm.example.test",
        member_id="member",
        user_id=user_id,
        expires_at=datetime.now(UTC),
        user_name="Менеджер закупки",
    )


def _order(db_session) -> ProcurementOrderFormation:
    order = ProcurementOrderFormation(
        stable_key="proc-order:marks:1",
        status="draft",
        version=1,
        bitrix_entity_type_id=1200,
        bitrix_item_id="9001",
        supplier_ref="0xsupplier",
        supplier_name="Поставщик тест",
        contract_ref="0xcontract",
        contract_name="Основной договор",
        warehouse_code="MAIN",
        warehouse_name="Центральный склад",
        currency="RUB",
        procurement_contour="ordinary",
        route="ordinary",
        batch_id="2026-08-18",
        order_date=date(2026, 8, 18),
        calculation_id="display-auto-order-2026-08-18",
    )
    order.lines = [
        ProcurementOrderFormationLine(
            stable_key="line:1",
            line_number=1,
            bitrix_product_id="1646",
            bitrix_product_xml_id="2685293e-967c-11e1-bdb9-0025901e48ef",
            nomenclature_ref="0xBDB90025901E48EF11E1967C2685293E",
            nomenclature_code="РБ000006737",
            nomenclature_name="Дисплей тест",
            recommended_quantity=Decimal("5"),
            final_quantity=Decimal("5"),
            purchase_price=Decimal("115"),
            amount=Decimal("575"),
            currency="RUB",
            lifecycle_status="Продажа",
            assortment_status="Продажа",
        )
    ]
    db_session.add(order)
    db_session.commit()
    return order


def test_pension_mark_and_replacement_reach_the_package(db_session) -> None:
    order = _order(db_session)
    create_classification_proposal(
        db_session,
        order.id,
        order.lines[0].id,
        {
            "proposed_status": "pension",
            "reason": "Ведём РБ000057818: аналог дешевле",
            "replacement_sku_code": "РБ000057818",
        },
        _session(),
    )

    marks = collect_management_marks(db_session)
    assert [mark["mark_value_name"] for mark in marks] == ["Допродаём"]

    message = build_management_marks_message(marks)
    assert message.mode == "dry_run"
    xml = build_nomenclature_property_updates_xml(message)
    root = ET.fromstring(xml)
    property_names = [node.text for node in root.iter("PropertyName")]
    assert MANAGEMENT_MARK_PROPERTY_NAME in property_names
    assert REPLACEMENT_PROPERTY_NAME in property_names


def test_lifecycle_property_export_stays_prohibited() -> None:
    # Запрет 2026-08-05 не снимается: узкий периметр решения 2026-08-18 его не трогает.
    message = build_management_marks_message(
        [
            {
                "nomenclature_code": "РБ000006737",
                "manual_status": "pension",
                "mark_value_name": "Допродаём",
                "replacement_sku_code": "",
                "reason": "тест",
                "approved_by": "Менеджер закупки",
                "proposal_id": 1,
            }
        ]
    )
    forbidden = NomenclaturePropertyUpdateRow(
        idempotency_key="forbidden:1",
        nomenclature_code="РБ000006737",
        property_name="Статус ассортимента",
        value_type="property_value",
        new_value_name="Пенсия",
    )
    with pytest.raises(ValueError, match="lifecycle property export"):
        build_nomenclature_property_updates_xml(
            type(message)(
                message_id=message.message_id,
                rows=(forbidden,),
                mode="dry_run",
            )
        )
