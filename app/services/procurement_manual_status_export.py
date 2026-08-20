"""Перенос согласованных ручных статусов в файл ручных решений автозаказа.

Решение по карточке принимается в приложении «Формирование заказа» и хранится в
`ProcurementClassificationProposal`. Ночной контур (`build_assortment_lifecycle_facts`
и далее) читает ручные статусы только из `display-manual-overrides.json`, поэтому без
этого моста метка «Допродаём» блокировала бы одну строку заказа, но карточка
осталась бы в расчёте потребности и в рабочих списках закупки.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.procurement_order_formation import (
    ProcurementClassificationProposal,
    ProcurementLifecycleTransitionProposal,
    ProcurementOrderFormationLine,
)
from app.services.procurement_assortment_decisions import (
    clean_string,
    decision_label,
    load_manual_overrides,
    merge_manual_overrides,
    write_json,
)
from app.services.procurement_order_formation import (
    APPROVED_PROPOSAL_STATUSES,
    MANUAL_STATUS_LABELS,
)

# «Рабочий» — это возврат к расчётной формуле, а не ручной стоп, поэтому в файл
# ручных решений он не попадает.
EXPORTABLE_STATUSES = frozenset(MANUAL_STATUS_LABELS) - {"working"}

SOURCE_PREFIX = "procurement_order_formation:"
SOURCE_RULE = "procurement_order_formation_classification"
SOURCE_RULE_RU = "решение в приложении «Формирование заказа»"
SYNCED_AT_KEY = "_order_formation_status_synced_at"
SOURCE_RULE_KEY = "_order_formation_status_source_rule"


def collect_approved_overrides(db: Session) -> list[dict[str, Any]]:
    """Последнее согласованное решение по каждому коду номенклатуры."""
    rows = db.execute(
        select(ProcurementClassificationProposal, ProcurementOrderFormationLine)
        .join(
            ProcurementOrderFormationLine,
            ProcurementClassificationProposal.line_id == ProcurementOrderFormationLine.id,
        )
        .where(ProcurementClassificationProposal.status.in_(APPROVED_PROPOSAL_STATUSES))
        .order_by(ProcurementClassificationProposal.id)
    ).all()

    latest: dict[str, tuple[float, dict[str, Any]]] = {}
    for proposal, line in rows:
        code = clean_string(line.nomenclature_code)
        status = clean_string(proposal.proposed_status)
        if not code or status not in {*EXPORTABLE_STATUSES, "working"}:
            continue
        changed_at = proposal.approved_at or proposal.requested_at
        latest[code] = (
            _decision_timestamp(changed_at),
            _override_from_proposal(proposal, line, code, status),
        )

    lifecycle_rows = db.scalars(
        select(ProcurementLifecycleTransitionProposal)
        .where(
            ProcurementLifecycleTransitionProposal.status.in_(APPROVED_PROPOSAL_STATUSES),
            ProcurementLifecycleTransitionProposal.action_kind == "manual_decision",
        )
        .order_by(ProcurementLifecycleTransitionProposal.id)
    ).all()
    for proposal in lifecycle_rows:
        manual = dict((proposal.payload or {}).get("manual_decision") or {})
        status = clean_string(manual.get("decision") or proposal.target_status)
        code = clean_string(proposal.nomenclature_code)
        if not code or status not in {"pension", "working"}:
            continue
        changed_at = proposal.approved_at or proposal.updated_at or proposal.created_at
        candidate = (
            _decision_timestamp(changed_at),
            _override_from_lifecycle(proposal, code, status, manual),
        )
        if code not in latest or candidate[0] >= latest[code][0]:
            latest[code] = candidate
    return [latest[code][1] for code in sorted(latest)]


def _override_from_proposal(
    proposal: ProcurementClassificationProposal,
    line: ProcurementOrderFormationLine,
    code: str,
    status: str,
) -> dict[str, Any]:
    approved_by = clean_string(proposal.approved_by_name) or clean_string(
        proposal.approved_by_actor
    )
    changed_at = proposal.approved_at or proposal.requested_at
    blockers: list[str] = []
    if not approved_by:
        blockers.append("manual_approved_by_required")
    if changed_at is None:
        blockers.append("manual_changed_at_required")

    override: dict[str, Any] = {
        "nomenclature_code": code,
        "approval_rule": SOURCE_RULE,
        "approval_rule_ru": SOURCE_RULE_RU,
        "approval_source": f"{SOURCE_PREFIX}{proposal.id}",
        "manual_approved_by": approved_by,
        "manual_changed_at": changed_at.date().isoformat() if changed_at else "",
        "manual_reason": clean_string(proposal.reason)
        or f"Ручное решение: {decision_label(status)}.",
        "source_order_line_id": proposal.line_id,
        "source_nomenclature_name": clean_string(line.nomenclature_name),
        "sync_blockers": blockers,
    }
    if status == "working":
        override["working_confirmed_by_folder_responsible"] = True
    else:
        override["manual_status"] = status
    replacement_code = clean_string(proposal.replacement_sku_code)
    if replacement_code:
        override["replacement_sku_code"] = replacement_code
        override["replacement_sku_name"] = clean_string(proposal.replacement_sku_name)
    return override


def _override_from_lifecycle(
    proposal: ProcurementLifecycleTransitionProposal,
    code: str,
    status: str,
    manual: dict[str, Any],
) -> dict[str, Any]:
    approved_by = clean_string(proposal.approved_by_name) or clean_string(
        proposal.approved_by_actor
    )
    changed_at = proposal.approved_at or proposal.updated_at or proposal.created_at
    blockers: list[str] = []
    if not approved_by:
        blockers.append("manual_approved_by_required")
    if changed_at is None:
        blockers.append("manual_changed_at_required")
    override: dict[str, Any] = {
        "nomenclature_code": code,
        "approval_rule": SOURCE_RULE,
        "approval_rule_ru": SOURCE_RULE_RU,
        "approval_source": f"{SOURCE_PREFIX}lifecycle:{proposal.id}",
        "manual_approved_by": approved_by,
        "manual_changed_at": changed_at.date().isoformat() if changed_at else "",
        "manual_reason": clean_string(manual.get("reason"))
        or clean_string(proposal.reason)
        or f"Ручное решение: {decision_label(status)}.",
        "source_lifecycle_proposal_id": proposal.id,
        "source_nomenclature_name": clean_string(proposal.product_name),
        "sync_blockers": blockers,
    }
    if status == "working":
        override["working_confirmed_by_folder_responsible"] = True
    else:
        override["manual_status"] = status
    replacement_code = clean_string(manual.get("replacement_sku_code"))
    if replacement_code:
        override["replacement_sku_code"] = replacement_code
    return override


def _decision_timestamp(value: datetime | None) -> float:
    if value is None:
        return 0.0
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return normalized.timestamp()


def export_manual_status_overrides(
    db: Session,
    overrides_path: str,
    *,
    dry_run: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Возвращает решения и строки слияния; при dry_run файл не переписывается."""
    decisions = collect_approved_overrides(db)
    payload = load_manual_overrides(overrides_path)
    merged, merge_rows = merge_manual_overrides(
        payload,
        decisions,
        source_prefix=SOURCE_PREFIX,
        synced_at_key=SYNCED_AT_KEY,
        source_rule_key=SOURCE_RULE_KEY,
        source_rule=SOURCE_RULE,
    )
    # Файл лежит в репозитории, а задача идёт ежечасно: без этой проверки каждый
    # прогон переписывал бы отметку времени и оставлял вечный diff. Сравниваем
    # сами решения, а не факт слияния: повторный прогон даёт те же записи.
    changed = merged.get("items") != payload.get("items")
    if not dry_run and changed:
        write_json(overrides_path, merged)
    return decisions, merge_rows


def blocked_rows(merge_rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in merge_rows if row.get("sync_blockers")]
