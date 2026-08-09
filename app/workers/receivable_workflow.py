from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.infrastructure.db import build_onec_engine_from_settings, get_application_engine
from app.models import ReceivableCase, ReceivableWorkItem
from app.services.receivable_workflow import (
    STATUS_CLOSED,
    ReceivableBitrixClient,
    ReceivableWorkflowSummary,
    build_bitrix_client_from_settings,
    close_stale_receivable_work_items,
    stable_key_for_counterparty,
    sync_receivable_workflow,
)
from app.services.receivable_workplace import build_receivable_workplace
from app.services.receivables import CASE_BUYERS, fetch_counterparty_phones_from_onec


def _get_app_engine():
    return get_application_engine()


def _get_onec_engine():
    settings = get_settings()
    if not settings.onec_database_url:
        return None
    return build_onec_engine_from_settings()


def _load_workflow_counterparty_refs(session: Session, *, as_of: date) -> tuple[str, ...]:
    refs = (
        session.execute(
            select(ReceivableCase.counterparty_ref)
            .where(
                ReceivableCase.snapshot_date == as_of,
                ReceivableCase.segment == CASE_BUYERS,
            )
            .distinct()
            .order_by(ReceivableCase.counterparty_ref)
        )
        .scalars()
        .all()
    )
    return tuple(ref for ref in refs if ref)


def _load_phone_by_counterparty(session: Session, *, as_of: date) -> dict[str, str]:
    onec_engine = _get_onec_engine()
    if onec_engine is None:
        return {}
    try:
        return fetch_counterparty_phones_from_onec(
            onec_engine,
            counterparty_refs=_load_workflow_counterparty_refs(session, as_of=as_of),
        )
    finally:
        onec_engine.dispose()


def _normalized_department_name(value: str | None) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _load_workplace_counterparty_refs(
    session: Session,
    *,
    as_of: date,
    settings: Settings,
) -> tuple[str, ...]:
    workplace = build_receivable_workplace(
        session,
        snapshot_date=as_of,
        limit=10_000,
    )
    allowed_refs = {
        str(value).strip() for value in settings.receivable_workflow_department_refs if value
    }
    allowed_names = {
        _normalized_department_name(value)
        for value in settings.receivable_workflow_department_names
        if value
    }
    if not allowed_refs and not allowed_names:
        return tuple(item.counterparty_ref for item in workplace.payload)
    return tuple(
        item.counterparty_ref
        for item in workplace.payload
        if (item.department_ref and str(item.department_ref).strip() in allowed_refs)
        or _normalized_department_name(item.department_name) in allowed_names
    )


def _load_previous_workplace_stable_keys(
    session: Session,
    *,
    as_of: date,
    settings: Settings,
) -> set[str] | None:
    previous_date = session.scalar(
        select(func.max(ReceivableCase.snapshot_date)).where(
            ReceivableCase.snapshot_date < as_of,
            ReceivableCase.segment == CASE_BUYERS,
        )
    )
    if previous_date is None:
        return None
    return {
        stable_key_for_counterparty(counterparty_ref)
        for counterparty_ref in _load_workplace_counterparty_refs(
            session,
            as_of=previous_date,
            settings=settings,
        )
    }


class PlanningReceivableBitrixClient:
    def __init__(self, delegate: ReceivableBitrixClient, session: Session) -> None:
        self.delegate = delegate
        self.session = session
        self.actions: list[dict[str, Any]] = []
        self._next_fake_id = -1

    @staticmethod
    def _stable_key(fields: dict[str, Any]) -> str | None:
        return next(
            (
                str(value)
                for value in fields.values()
                if str(value).startswith("receivables|buyers|")
            ),
            None,
        )

    def list_items_by_ref(
        self,
        *,
        entity_type_id: int,
        ref_field: str,
        ref_value: str,
    ) -> list[dict[str, Any]]:
        matches = self.delegate.list_items_by_ref(
            entity_type_id=entity_type_id,
            ref_field=ref_field,
            ref_value=ref_value,
        )
        if len(matches) > 1:
            self.actions.append(
                {
                    "action": "conflict",
                    "stable_key": ref_value,
                    "matching_item_ids": [
                        str(item.get("id") or item.get("ID") or "") for item in matches
                    ],
                }
            )
        return matches

    def add_smart_process_item(
        self,
        *,
        entity_type_id: int,
        fields: dict[str, Any],
    ) -> tuple[str, str | None]:
        fake_id = str(self._next_fake_id)
        self._next_fake_id -= 1
        self.actions.append(
            {
                "action": "create",
                "stable_key": self._stable_key(fields),
                "entity_type_id": entity_type_id,
            }
        )
        return fake_id, None

    def update_smart_process_item(
        self,
        *,
        entity_type_id: int,
        item_id: str,
        fields: dict[str, Any],
    ) -> None:
        stable_key = self._stable_key(fields)
        work_item = (
            self.session.scalar(
                select(ReceivableWorkItem).where(ReceivableWorkItem.stable_key == stable_key)
            )
            if stable_key
            else None
        )
        self.actions.append(
            {
                "action": "close" if work_item and work_item.status == STATUS_CLOSED else "update",
                "stable_key": stable_key,
                "entity_type_id": entity_type_id,
                "item_id": str(item_id),
            }
        )


def _sync_closures(
    session: Session,
    *,
    as_of: date,
    settings: Settings,
    bitrix_client: ReceivableBitrixClient | None,
    current_counterparty_refs: tuple[str, ...],
    dry_run_bitrix: bool,
) -> ReceivableWorkflowSummary:
    summary = ReceivableWorkflowSummary()
    close_stale_receivable_work_items(
        session,
        as_of=as_of,
        settings=settings,
        bitrix_client=bitrix_client,
        summary=summary,
        current_active_stable_keys={
            stable_key_for_counterparty(counterparty_ref)
            for counterparty_ref in current_counterparty_refs
        },
        previous_active_stable_keys=_load_previous_workplace_stable_keys(
            session,
            as_of=as_of,
            settings=settings,
        ),
        dry_run_bitrix=dry_run_bitrix,
    )
    return summary


def run_receivable_workflow_sync(
    *,
    as_of: date | None = None,
    force: bool = False,
    dry_run_bitrix: bool = False,
    plan: bool = False,
    bitrix_only: bool = False,
    allow_closure: bool = True,
    counterparty_refs: tuple[str, ...] = (),
    limit: int | None = None,
    offset: int = 0,
    all_departments: bool = False,
    batch_size: int | None = None,
) -> dict[str, object]:
    settings = get_settings()
    if all_departments:
        settings = settings.model_copy(
            update={
                "receivable_workflow_department_refs": [],
                "receivable_workflow_department_names": [],
            }
        )
    business_date = as_of or date.today()
    if not settings.receivable_workflow_enabled and not force:
        return {
            "status": "disabled",
            "business_date": business_date.isoformat(),
            "reason": "RECEIVABLE_WORKFLOW_ENABLED=false",
        }

    real_client = build_bitrix_client_from_settings(settings)
    if (plan or bitrix_only) and real_client is None:
        return {
            "status": "error",
            "business_date": business_date.isoformat(),
            "reason": "Bitrix receivables client is not configured",
        }

    with Session(_get_app_engine()) as session:
        planning_client = (
            PlanningReceivableBitrixClient(real_client, session) if plan and real_client else None
        )
        client = planning_client or real_client
        phone_by_counterparty = (
            {} if bitrix_only or plan else _load_phone_by_counterparty(session, as_of=business_date)
        )
        full_counterparty_refs = counterparty_refs or _load_workplace_counterparty_refs(
            session,
            as_of=business_date,
            settings=settings,
        )
        selected_counterparty_refs = full_counterparty_refs[
            max(offset, 0) : None if limit is None else max(offset, 0) + limit
        ]
        full_selection = not counterparty_refs and limit is None and offset == 0
        if full_selection and not full_counterparty_refs:
            session.rollback()
            return {
                "status": "error",
                "business_date": business_date.isoformat(),
                "reason": "Receivables workplace is empty; refusing a full card sync",
            }

        summary = ReceivableWorkflowSummary()
        if batch_size and not plan:
            for start in range(0, len(selected_counterparty_refs), batch_size):
                batch_refs = selected_counterparty_refs[start : start + batch_size]
                batch_summary = sync_receivable_workflow(
                    session,
                    as_of=business_date,
                    phone_by_counterparty=phone_by_counterparty,
                    settings=settings,
                    bitrix_client=client,
                    dry_run_bitrix=dry_run_bitrix,
                    sync_sms=False,
                    allow_closure=False,
                    only_counterparty_refs=batch_refs,
                )
                summary.merge(batch_summary)
                session.commit()
                if batch_summary.bitrix_errors:
                    break
        else:
            summary.merge(
                sync_receivable_workflow(
                    session,
                    as_of=business_date,
                    phone_by_counterparty=phone_by_counterparty,
                    settings=settings,
                    bitrix_client=client,
                    dry_run_bitrix=dry_run_bitrix,
                    sync_sms=not (bitrix_only or plan),
                    allow_closure=False,
                    only_counterparty_refs=selected_counterparty_refs,
                )
            )

        closure_enabled = bool(allow_closure and full_selection and not summary.bitrix_errors)
        if closure_enabled:
            closure_summary = _sync_closures(
                session,
                as_of=business_date,
                settings=settings,
                bitrix_client=client,
                current_counterparty_refs=full_counterparty_refs,
                dry_run_bitrix=dry_run_bitrix,
            )
            summary.merge(closure_summary)

        if plan:
            session.rollback()
        elif not batch_size or closure_enabled:
            session.commit()
        status = "error" if summary.bitrix_errors else "ok"
        return {
            "status": status,
            "mode": "plan" if plan else "apply",
            "business_date": business_date.isoformat(),
            "bitrix_only": bitrix_only,
            "closure_enabled": closure_enabled,
            "all_departments": all_departments,
            "offset": offset,
            "limit": limit,
            "batch_size": batch_size,
            "selected_counterparty_count": len(selected_counterparty_refs),
            "plan_actions": planning_client.actions if planning_client else [],
            **summary.as_dict(),
        }
