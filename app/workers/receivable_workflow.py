from __future__ import annotations

from datetime import date

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import ReceivableCase
from app.services.receivable_workflow import (
    build_bitrix_client_from_settings,
    sync_receivable_workflow,
)
from app.services.receivables import CASE_BUYERS, fetch_counterparty_phones_from_onec


def _get_app_engine():
    return create_engine(get_settings().database_url)


def _get_onec_engine():
    settings = get_settings()
    if not settings.onec_database_url:
        return None
    return create_engine(
        settings.onec_database_url,
        connect_args={
            "timeout": float(settings.onec_query_timeout_seconds),
            "login_timeout": float(settings.onec_login_timeout_seconds),
        },
    )


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


def run_receivable_workflow_sync(
    *,
    as_of: date | None = None,
    force: bool = False,
    dry_run_bitrix: bool = False,
) -> dict[str, object]:
    settings = get_settings()
    business_date = as_of or date.today()
    if not settings.receivable_workflow_enabled and not force:
        return {
            "status": "disabled",
            "business_date": business_date.isoformat(),
            "reason": "RECEIVABLE_WORKFLOW_ENABLED=false",
        }

    client = build_bitrix_client_from_settings(settings)
    with Session(_get_app_engine()) as session:
        phone_by_counterparty = _load_phone_by_counterparty(session, as_of=business_date)
        summary = sync_receivable_workflow(
            session,
            as_of=business_date,
            phone_by_counterparty=phone_by_counterparty,
            settings=settings,
            bitrix_client=client,
            dry_run_bitrix=dry_run_bitrix,
        )
        session.commit()
        return {
            "status": "ok",
            "business_date": business_date.isoformat(),
            **summary.as_dict(),
        }
