from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import LogisticsManualReview


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Report open RTU logistics manual reviews.")
    parser.add_argument(
        "--review-type",
        default=None,
        help="Filter by review_type, for example rtu_target_warehouse_unresolved.",
    )
    parser.add_argument(
        "--examples",
        type=int,
        default=5,
        help="Max examples per group.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    with Session(engine) as session:
        report = build_report(
            session,
            review_type=args.review_type,
            examples_per_group=max(1, args.examples),
        )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


def build_report(
    session: Session,
    *,
    review_type: str | None = None,
    examples_per_group: int = 5,
) -> dict[str, Any]:
    stmt = (
        select(LogisticsManualReview)
        .where(
            LogisticsManualReview.source_document_type == "rtu",
            LogisticsManualReview.status == "open",
        )
        .order_by(LogisticsManualReview.id.asc())
    )
    if review_type:
        stmt = stmt.where(LogisticsManualReview.review_type == review_type)
    rows = session.scalars(stmt).all()

    by_type = Counter(row.review_type for row in rows)
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    source_warehouses: defaultdict[tuple[str, str, str], Counter[str]] = defaultdict(Counter)
    source_warehouse_ids: defaultdict[tuple[str, str, str], Counter[str]] = defaultdict(Counter)
    for row in rows:
        payload = row.payload if isinstance(row.payload, dict) else {}
        address = _address_from_payload(payload)
        key = (
            row.review_type,
            payload.get("site_delivery_method") or "",
            address,
        )
        if key not in groups:
            groups[key] = {
                "review_type": row.review_type,
                "delivery_method": payload.get("site_delivery_method"),
                "address": address,
                "count": 0,
                "source_warehouses": {},
                "examples": [],
            }
        group = groups[key]
        group["count"] += 1
        source_warehouse = payload.get("source_warehouse_name") or ""
        source_warehouse_id = payload.get("source_warehouse_external_id") or ""
        if source_warehouse:
            source_warehouses[key][source_warehouse] += 1
        if source_warehouse_id:
            source_warehouse_ids[key][source_warehouse_id] += 1
        if len(group["examples"]) < examples_per_group:
            group["examples"].append(
                {
                    "manual_review_id": row.id,
                    "rtu_number": payload.get("rtu_number"),
                    "site_order_number": payload.get("site_order_number"),
                    "source_warehouse_name": source_warehouse or None,
                    "source_warehouse_external_id": source_warehouse_id or None,
                    "source_external_id": row.source_external_id,
                }
            )

    result_groups = []
    for key, group in groups.items():
        group["source_warehouses"] = dict(source_warehouses[key])
        group["source_warehouse_external_ids"] = dict(source_warehouse_ids[key])
        group["source_based_candidate"] = _source_based_candidate(
            source_warehouse_ids[key],
            int(group["count"]),
        )
        result_groups.append(group)

    result_groups.sort(
        key=lambda item: (
            item["review_type"],
            item["delivery_method"] or "",
            -int(item["count"]),
            item["address"],
        )
    )
    return {
        "open_count": len(rows),
        "by_type": dict(by_type),
        "groups": result_groups,
    }


def _source_based_candidate(counter: Counter[str], total_count: int) -> dict[str, Any] | None:
    if not counter:
        return None
    warehouse_external_id, count = counter.most_common(1)[0]
    if count < max(2, total_count):
        return None
    return {
        "warehouse_external_id": warehouse_external_id,
        "confidence": "source_warehouse_exact_group",
        "note": "Needs human confirmation before applying as target warehouse alias.",
    }


def _address_from_payload(payload: dict[str, Any]) -> str:
    return (
        payload.get("site_delivery_addition")
        or payload.get("site_delivery_address")
        or payload.get("rtu_delivery_addition")
        or payload.get("rtu_delivery_address")
        or "<empty>"
    )


if __name__ == "__main__":
    raise SystemExit(main())
