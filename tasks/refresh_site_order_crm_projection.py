#!/usr/bin/env python3
"""Refresh the read-only CRM projection used by KMP4 without changing CRM stages."""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.infrastructure.db import session_scope
from app.services import site_order_fulfillment as fulfillment
from app.services import site_order_state_projection as projection
from infra.cron import order_fulfillment_sync as fulfillment_sync


def _parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _load_cursor(path: Path) -> datetime | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return _parse_datetime(payload.get("modified_at")) if isinstance(payload, dict) else None


def _save_cursor(path: Path, modified_at: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps({"modified_at": modified_at.isoformat()}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def fetch_projection_facts(
    client: fulfillment.BitrixChatClient,
    *,
    modified_since: datetime | None,
    limit: int | None,
) -> list[projection.CrmProjectionFact]:
    result: list[projection.CrmProjectionFact] = []
    start: int | None = 0
    while start is not None and (limit is None or len(result) < limit):
        filter_payload: dict[str, Any] = {f"!{fulfillment.CRM_ORDER_NUMBER_FIELD}": False}
        if modified_since is not None:
            filter_payload[">=DATE_MODIFY"] = modified_since.isoformat()
        response = client.call(
            "crm.deal.list",
            {
                "filter": filter_payload,
                "select": [*fulfillment.CRM_REVIEW_SELECT_FIELDS, "DATE_MODIFY"],
                "order": {"DATE_MODIFY": "ASC", "ID": "ASC"},
                "start": start,
            },
        )
        for item in response.get("result") or []:
            if not isinstance(item, dict):
                continue
            order_number = str(item.get(fulfillment.CRM_ORDER_NUMBER_FIELD) or "").strip()
            deal_id = str(item.get("ID") or "").strip()
            if not order_number or not deal_id.isdigit():
                continue
            raw_delivery = str(item.get(fulfillment.CRM_DELIVERY_FIELD) or "").strip() or None
            payment_raw = str(item.get(fulfillment.CRM_PAYMENT_FIELD) or "").strip().lower()
            result.append(
                projection.CrmProjectionFact(
                    site_order_number=order_number,
                    bitrix_deal_id=int(deal_id),
                    crm_stage=str(item.get("STAGE_ID") or "").strip() or None,
                    delivery_method=fulfillment.classify_delivery_method(raw_delivery),
                    raw_delivery_method=raw_delivery,
                    payment_state=(
                        "paid" if payment_raw in {"1", "y", "yes", "true", "да"} else "unconfirmed"
                    ),
                    modified_at=_parse_datetime(item.get("DATE_MODIFY")),
                )
            )
            if limit is not None and len(result) >= limit:
                break
        next_value = response.get("next")
        start = int(next_value) if next_value is not None else None
    return result


def enrich_projection_facts(
    facts: list[projection.CrmProjectionFact],
) -> list[projection.CrmProjectionFact]:
    order_numbers = list(dict.fromkeys(item.site_order_number for item in facts))
    if not order_numbers:
        return []
    site_statuses = fulfillment_sync.fetch_sale_order_statuses(order_numbers)
    settlements = fulfillment_sync.fetch_onec_order_settlements(order_numbers)
    enriched: list[projection.CrmProjectionFact] = []
    for fact in facts:
        site_status = site_statuses.get(fact.site_order_number)
        settlement = settlements.get(fact.site_order_number)
        payment_confirmed = bool(
            (site_status is not None and site_status.payed is True)
            or (settlement is not None and settlement.payment_confirmed)
        )
        enriched.append(
            replace(
                fact,
                payment_state="paid" if payment_confirmed else fact.payment_state,
                payment_amount=(settlement.payment_amount if settlement is not None else None),
                debt_amount=settlement.debt_amount if settlement is not None else None,
                site_status=site_status.status_id if site_status is not None else None,
                site_paid=site_status.payed if site_status is not None else None,
                site_canceled=site_status.canceled if site_status is not None else None,
            )
        )
    return enriched


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = get_settings()
    if not settings.order_fulfillment_bitrix_webhook_url:
        raise SystemExit("ORDER_FULFILLMENT_BITRIX_WEBHOOK_URL is not configured")
    cursor_path = Path(settings.order_fulfillment_crm_projection_cursor_path)
    cursor = None if args.full else _load_cursor(cursor_path)
    modified_since = (
        cursor - timedelta(minutes=settings.order_fulfillment_crm_projection_overlap_minutes)
        if cursor is not None
        else None
    )
    client = fulfillment.BitrixChatClient(settings.order_fulfillment_bitrix_webhook_url)
    facts = enrich_projection_facts(
        fetch_projection_facts(
            client,
            modified_since=modified_since,
            limit=(None if args.full else settings.order_fulfillment_crm_projection_batch_limit),
        )
    )
    observed_at = datetime.now()
    with session_scope() as session:
        summary = projection.upsert_crm_projection(session, facts, observed_at=observed_at)
    newest = max(
        (fact.modified_at for fact in facts if fact.modified_at is not None),
        default=cursor or observed_at,
    )
    _save_cursor(cursor_path, newest)
    print(
        json.dumps(
            {"mode": "full" if args.full else "incremental", "fetched": len(facts), **summary},
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
