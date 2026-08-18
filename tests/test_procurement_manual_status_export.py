from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal

from app.models.procurement_order_formation import (
    ProcurementOrderFormation,
    ProcurementOrderFormationLine,
)
from app.services.bitrix_procurement_order_formation_auth import (
    ProcurementOrderFormationSession,
)
from app.services.procurement_manual_status_export import (
    SOURCE_PREFIX,
    collect_approved_overrides,
    export_manual_status_overrides,
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
        stable_key="proc-order:manual-export:1",
        status="draft",
        version=1,
        bitrix_entity_type_id=1200,
        bitrix_item_id="8001",
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


def _propose_pension(db_session, order) -> None:
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


def test_pension_reaches_the_auto_order_overrides_file(db_session, tmp_path) -> None:
    order = _order(db_session)
    _propose_pension(db_session, order)

    overrides = tmp_path / "display-manual-overrides.json"
    overrides.write_text(json.dumps({"items": []}, ensure_ascii=False), encoding="utf-8")

    decisions, merge_rows = export_manual_status_overrides(db_session, str(overrides))

    assert [item["nomenclature_code"] for item in decisions] == ["РБ000006737"]
    assert decisions[0]["manual_status"] == "pension"
    assert decisions[0]["replacement_sku_code"] == "РБ000057818"
    assert decisions[0]["manual_approved_by"] == "Менеджер закупки"
    assert decisions[0]["sync_blockers"] == []
    assert [row["action"] for row in merge_rows] == ["added"]

    payload = json.loads(overrides.read_text(encoding="utf-8"))
    stored = payload["items"][0]
    assert stored["manual_status"] == "pension"
    assert stored["approval_source"].startswith(SOURCE_PREFIX)


def test_repeated_export_updates_the_same_record(db_session, tmp_path) -> None:
    order = _order(db_session)
    _propose_pension(db_session, order)

    overrides = tmp_path / "display-manual-overrides.json"
    overrides.write_text(json.dumps({"items": []}, ensure_ascii=False), encoding="utf-8")

    export_manual_status_overrides(db_session, str(overrides))
    _, merge_rows = export_manual_status_overrides(db_session, str(overrides))

    assert [row["action"] for row in merge_rows] == ["updated"]
    payload = json.loads(overrides.read_text(encoding="utf-8"))
    assert len(payload["items"]) == 1


def test_dry_run_keeps_the_file_untouched(db_session, tmp_path) -> None:
    order = _order(db_session)
    _propose_pension(db_session, order)

    overrides = tmp_path / "display-manual-overrides.json"
    overrides.write_text(json.dumps({"items": []}, ensure_ascii=False), encoding="utf-8")

    decisions, _ = export_manual_status_overrides(db_session, str(overrides), dry_run=True)

    assert decisions
    assert json.loads(overrides.read_text(encoding="utf-8")) == {"items": []}


def test_only_approved_decisions_are_exported(db_session) -> None:
    order = _order(db_session)
    create_classification_proposal(
        db_session,
        order.id,
        order.lines[0].id,
        {
            "proposed_status": "matrix",
            "reason": "Держим всегда",
        },
        _session(),
    )

    # «Матричный» ждёт второго согласования, поэтому в ночной расчёт не уходит.
    assert collect_approved_overrides(db_session) == []
