from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.services import quality_case as service


def _payload(external_id: str = "quality-1", *, code: str = "РБ000032998") -> dict:
    return {
        "external_id": external_id,
        "source_return_ref": f"return-{external_id}",
        "source_return_number": "РБ000001",
        "source_return_line_key": f"return-{external_id}:1",
        "return_at": datetime(2026, 8, 1, 10, 0, 0),
        "nomenclature_code": code,
        "nomenclature_name": "Тестовый дисплей",
        "quantity": Decimal("1"),
        "preliminary_quality": "Брак",
        "owner_external_id": "130741",
        "idempotency_key": f"sync:{external_id}",
    }


def test_confirmed_ok_supersedes_preliminary_defect(db_session) -> None:
    row = service.sync_case(db_session, _payload())
    assert row["current_status"] == service.STATUS_PENDING_REVIEW
    assert row["counts_as_confirmed_product_defect"] is False

    service.start_review(
        db_session,
        case_id=row["id"],
        actor_external_id="130741",
        comment="Проверено Сергеем Бирюковым",
        idempotency_key="review:quality-1",
    )
    decided = service.record_decision(
        db_session,
        case_id=row["id"],
        actor_external_id="130741",
        decision_code="confirmed_ok_after_check",
        disposition_code="return_to_stock",
        onec_quality_correction_ref="quality-correction-1",
        comment="Товар рабочий, возвращён в оборот",
        idempotency_key="decision:quality-1",
    )

    assert decided["current_status"] == service.STATUS_DECIDED
    assert decided["counts_as_confirmed_product_defect"] is False
    metrics = service.quality_metrics(
        db_session,
        date_from=datetime(2026, 7, 1),
        date_to=datetime(2026, 9, 1),
    )
    assert metrics == [
        {
            "nomenclature_code": "РБ000032998",
            "candidate_qty": Decimal("1.000"),
            "pending_qty": Decimal("0"),
            "confirmed_product_defect_qty": Decimal("0"),
            "handling_damage_qty": Decimal("0"),
            "confirmed_not_product_defect_qty": Decimal("1.000"),
        }
    ]


def test_product_defect_and_handling_damage_are_separate_metrics(db_session) -> None:
    product = service.sync_case(db_session, _payload("quality-product", code="RB-PRODUCT"))
    handling = service.sync_case(db_session, _payload("quality-transport", code="RB-HANDLING"))

    service.record_decision(
        db_session,
        case_id=product["id"],
        actor_external_id="130741",
        decision_code="supplier_defect",
        disposition_code="return_to_supplier",
        onec_quality_correction_ref=None,
        comment="Подтверждён дефект поставщика",
        idempotency_key="decision:quality-product",
    )
    service.record_decision(
        db_session,
        case_id=handling["id"],
        actor_external_id="130741",
        decision_code="transport_damage",
        disposition_code="write_off",
        onec_quality_correction_ref=None,
        comment="Повреждено при перевозке",
        idempotency_key="decision:quality-transport",
    )

    metrics = {
        item["nomenclature_code"]: item
        for item in service.quality_metrics(
            db_session,
            date_from=datetime(2026, 7, 1),
            date_to=datetime(2026, 9, 1),
        )
    }
    assert metrics["RB-PRODUCT"]["confirmed_product_defect_qty"] == Decimal("1.000")
    assert metrics["RB-PRODUCT"]["handling_damage_qty"] == Decimal("0")
    assert metrics["RB-HANDLING"]["confirmed_product_defect_qty"] == Decimal("0")
    assert metrics["RB-HANDLING"]["handling_damage_qty"] == Decimal("1.000")


def test_return_to_stock_requires_onec_quality_correction(db_session) -> None:
    row = service.sync_case(db_session, _payload())
    with pytest.raises(HTTPException) as error:
        service.record_decision(
            db_session,
            case_id=row["id"],
            actor_external_id="130741",
            decision_code="confirmed_ok_after_check",
            disposition_code="return_to_stock",
            onec_quality_correction_ref=None,
            comment="Рабочий",
            idempotency_key="decision:missing-correction",
        )
    assert error.value.status_code == 422


def test_source_resync_does_not_overwrite_final_okk_decision(db_session) -> None:
    payload = _payload()
    row = service.sync_case(db_session, payload)
    service.record_decision(
        db_session,
        case_id=row["id"],
        actor_external_id="130741",
        decision_code="technical_defect",
        disposition_code="keep_as_defect",
        onec_quality_correction_ref=None,
        comment="Подтверждено",
        idempotency_key="decision:stable",
    )

    payload["preliminary_quality"] = "Новый"
    payload["quantity"] = Decimal("2")
    payload["return_at"] += timedelta(minutes=1)
    updated = service.sync_case(db_session, payload)

    assert updated["preliminary_quality"] == "Новый"
    assert updated["quantity"] == Decimal("2.000")
    assert updated["final_decision_code"] == "technical_defect"
    assert updated["counts_as_confirmed_product_defect"] is True
