"""Передача управленческой метки ассортимента в УТ 10.3.

Решение 2026-08-18: закупщик должен видеть прямо в карточке номенклатуры 1С и в
помощнике закупок, что позиция снята с ведения и что ведут вместо неё. В 1С
уходят только два управленческих свойства; жизненная лестница статусов
по-прежнему не выгружается — запрет 2026-08-05 в
`PROHIBITED_LIFECYCLE_PROPERTY_NAMES` не снимается и не редактируется.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.procurement_order_formation import (
    ProcurementClassificationProposal,
    ProcurementOrderFormationLine,
)
from app.services.exporters.ut103_nomenclature_properties import (
    NomenclaturePropertyUpdateMessage,
    NomenclaturePropertyUpdateRow,
)
from app.services.procurement_assortment_decisions import clean_string
from app.services.procurement_order_formation import APPROVED_PROPOSAL_STATUSES

MANAGEMENT_MARK_PROPERTY_NAME = "Управленческая метка ассортимента"
REPLACEMENT_PROPERTY_NAME = "Взамен ведём"

# Значения свойства «Управленческая метка ассортимента» в справочнике 1С.
MANAGEMENT_MARK_VALUE_NAMES = {
    "pension": "Допродаём",
    "do_not_order": "Не закупать",
    "replace_candidate": "Кандидат на замену",
    "nonliquid": "Выводим",
}


def collect_management_marks(db: Session) -> list[dict[str, Any]]:
    """Последняя согласованная метка по каждому коду номенклатуры."""
    rows = db.execute(
        select(ProcurementClassificationProposal, ProcurementOrderFormationLine)
        .join(
            ProcurementOrderFormationLine,
            ProcurementClassificationProposal.line_id == ProcurementOrderFormationLine.id,
        )
        .where(ProcurementClassificationProposal.status.in_(APPROVED_PROPOSAL_STATUSES))
        .order_by(ProcurementClassificationProposal.id)
    ).all()

    latest: dict[str, dict[str, Any]] = {}
    for proposal, line in rows:
        code = clean_string(line.nomenclature_code)
        status = clean_string(proposal.proposed_status)
        if not code or status not in MANAGEMENT_MARK_VALUE_NAMES:
            continue
        latest[code] = {
            "nomenclature_code": code,
            "manual_status": status,
            "mark_value_name": MANAGEMENT_MARK_VALUE_NAMES[status],
            "replacement_sku_code": clean_string(proposal.replacement_sku_code),
            "reason": clean_string(proposal.reason),
            "approved_by": clean_string(proposal.approved_by_name)
            or clean_string(proposal.approved_by_actor),
            "proposal_id": proposal.id,
        }
    return [latest[code] for code in sorted(latest)]


def build_management_mark_rows(
    marks: Sequence[dict[str, Any]],
) -> list[NomenclaturePropertyUpdateRow]:
    rows: list[NomenclaturePropertyUpdateRow] = []
    for mark in marks:
        base_key = f"mgmt-mark:{mark['proposal_id']}"
        rows.append(
            NomenclaturePropertyUpdateRow(
                idempotency_key=f"{base_key}:mark",
                nomenclature_code=mark["nomenclature_code"],
                property_name=MANAGEMENT_MARK_PROPERTY_NAME,
                value_type="property_value",
                new_value_name=mark["mark_value_name"],
                reason=mark["reason"],
                approved_by=mark["approved_by"],
            )
        )
        replacement = mark.get("replacement_sku_code")
        if replacement:
            rows.append(
                NomenclaturePropertyUpdateRow(
                    idempotency_key=f"{base_key}:replacement",
                    nomenclature_code=mark["nomenclature_code"],
                    property_name=REPLACEMENT_PROPERTY_NAME,
                    value_type="string",
                    new_value=replacement,
                    reason=mark["reason"],
                    approved_by=mark["approved_by"],
                )
            )
    return rows


def build_management_marks_message(
    marks: Sequence[dict[str, Any]],
    *,
    mode: str = "dry_run",
    approved_by: str = "",
    message_id: str | None = None,
    created_at: datetime | None = None,
) -> NomenclaturePropertyUpdateMessage:
    """Пакет свойств для УТ 10.3.

    `mode="apply"` включает реальную запись в 1С и требует отдельного разрешения
    пользователя вместе с production-флагами обмена.
    """
    return NomenclaturePropertyUpdateMessage(
        message_id=message_id or f"mgmt-marks-{uuid.uuid4().hex[:12]}",
        rows=tuple(build_management_mark_rows(marks)),
        mode=mode,
        approved_by=approved_by,
        created_at=created_at or datetime.now(UTC).replace(tzinfo=None),
    )
