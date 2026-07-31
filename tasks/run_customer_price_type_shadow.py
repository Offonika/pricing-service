"""Run the customer price-type calculation without external side effects.

The command reads 1C through the read-only engine. With ``--apply`` it persists
only pricing-service runs, snapshots, cases and the optional reviewed batch. It
does not call Bitrix24 and does not create or apply a 1C export package.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

from app.domains.customer_price_types import CustomerPriceTypeAccessScope
from app.infrastructure.customer_price_type_sources import CustomerPriceTypeBulkSource
from app.infrastructure.customer_price_types import SqlAlchemyCustomerPriceTypeRepository
from app.infrastructure.db import get_application_session_factory, get_onec_engine
from app.services.customer_price_type_review_batches import (
    DEFAULT_BATCH_KEY,
    ReviewBatchSourceRow,
    import_review_batch,
    load_review_batch_sources,
)
from app.services.customer_price_types import CustomerPriceTypeRunService

_BUSINESS_CONFLICT_REASONS = {
    "conflicting_price_levels",
    "conflicting_price_type_variants",
}


def _month(value: str) -> date:
    try:
        result = date.fromisoformat(f"{value}-01")
    except ValueError as exc:
        raise argparse.ArgumentTypeError("snapshot month must use YYYY-MM") from exc
    if result.strftime("%Y-%m") != value:
        raise argparse.ArgumentTypeError("snapshot month must use YYYY-MM")
    return result


def _normalized_code(value: str | None) -> str:
    return " ".join(str(value or "").split()).casefold()


def _aggregate_source_statuses(
    facts: list[Any], required_sources: tuple[str, ...]
) -> dict[str, str]:
    return {
        source: (
            "ready"
            if all(item.source_statuses.get(source, "missing") == "ready" for item in facts)
            else "partial"
        )
        for source in required_sources
    }


def _decision_review_status(fact: Any, decision: Any, required_sources: tuple[str, ...]) -> str:
    if any(fact.source_statuses.get(source, "missing") != "ready" for source in required_sources):
        return "technical_incomplete"
    if decision.recommendation != "data_check":
        return "ready"
    if set(decision.reasons) & _BUSINESS_CONFLICT_REASONS:
        return "business_conflict"
    return "technical_incomplete"


def _review_preview(
    *,
    facts: list[Any],
    decisions: list[Any],
    rows: list[ReviewBatchSourceRow],
    required_sources: tuple[str, ...],
) -> dict[str, Any]:
    by_code: dict[str, list[tuple[Any, Any]]] = defaultdict(list)
    for fact, decision in zip(facts, decisions, strict=True):
        by_code[_normalized_code(fact.counterparty_code)].append((fact, decision))

    counts = Counter()
    status_counts = Counter()
    mismatches: list[dict[str, str | None]] = []
    for row in rows:
        matches = by_code.get(_normalized_code(row.counterparty_code), [])
        if len(matches) != 1:
            status = "missing_fact" if not matches else "ambiguous_fact"
            status_counts[status] += 1
            mismatches.append(
                {
                    "counterparty_code": row.counterparty_code,
                    "status": status,
                    "expected_price_type": row.expected_price_type,
                    "actual_price_type": None,
                }
            )
            continue
        fact, decision = matches[0]
        actual_bucket = (
            "working_bronze" if decision.current_price_type == "2.Бронзовый" else "review_queue"
        )
        counts[actual_bucket] += 1
        review_status = _decision_review_status(fact, decision, required_sources)
        status_counts[review_status] += 1
        if row.expected_price_type:
            matched = (
                actual_bucket == row.expected_bucket
                and decision.current_price_type == row.expected_price_type
                and review_status == "ready"
            )
        else:
            matched = (
                actual_bucket == row.expected_bucket
                and decision.current_price_type is None
                and review_status == "business_conflict"
            )
        if not matched:
            mismatches.append(
                {
                    "counterparty_code": row.counterparty_code,
                    "status": review_status,
                    "expected_price_type": row.expected_price_type,
                    "actual_price_type": decision.current_price_type,
                }
            )
    return {
        "total": len(rows),
        "counts": dict(counts),
        "review_status_counts": dict(status_counts),
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-month", required=True, type=_month)
    parser.add_argument("--run-key")
    parser.add_argument("--working-bronze-csv", type=Path)
    parser.add_argument("--review-queue-csv", type=Path)
    parser.add_argument("--batch-key", default=DEFAULT_BATCH_KEY)
    parser.add_argument(
        "--apply",
        action="store_true",
        help=("Persist only pricing-service shadow state. This never writes to " "Bitrix24 or 1C."),
    )
    args = parser.parse_args()
    if bool(args.working_bronze_csv) != bool(args.review_queue_csv):
        parser.error("both reviewed CSV paths must be provided together")
    return args


def main() -> int:
    args = _parse_args()
    session_factory = get_application_session_factory()
    service = CustomerPriceTypeRunService(session_factory)
    with session_factory() as session:
        source = CustomerPriceTypeBulkSource(
            onec_engine=get_onec_engine(),
            application_session=session,
            buyers_root_group_ref=service.ruleset.buyers_root_group_ref,
            contract_kind_ref=service.ruleset.contract_kind_ref,
            key_account_price_type_prefixes=service.ruleset.key_account_prefixes,
        )
        facts = source.collect(snapshot_month=args.snapshot_month)
    if not facts:
        raise SystemExit("customer price-type source returned no facts")

    source_statuses = _aggregate_source_statuses(facts, service.ruleset.required_sources)
    decisions = [service.engine.evaluate(item) for item in facts]
    output: dict[str, Any] = {
        "mode": "apply" if args.apply else "preview",
        "snapshot_month": args.snapshot_month.strftime("%Y-%m"),
        "ruleset_version": service.ruleset.version,
        "fact_count": len(facts),
        "source_statuses": source_statuses,
        "recommendations": dict(Counter(item.recommendation for item in decisions)),
        "actionable_count": sum(item.action_required for item in decisions),
    }

    batch_rows: list[ReviewBatchSourceRow] | None = None
    if args.working_bronze_csv and args.review_queue_csv:
        batch_rows, checksum = load_review_batch_sources(
            working_bronze_csv=args.working_bronze_csv,
            review_queue_csv=args.review_queue_csv,
        )
        output["review_batch_preview"] = {
            "batch_key": args.batch_key,
            "source_sha256": checksum,
            **_review_preview(
                facts=facts,
                decisions=decisions,
                rows=batch_rows,
                required_sources=service.ruleset.required_sources,
            ),
        }

    if args.apply:
        run = service.execute(facts, source_statuses=source_statuses, run_key=args.run_key)
        output["run"] = asdict(run)
        if batch_rows is not None:
            with session_factory() as session:
                batch_result = import_review_batch(
                    session,
                    working_bronze_csv=args.working_bronze_csv,
                    review_queue_csv=args.review_queue_csv,
                    batch_key=args.batch_key,
                    apply=True,
                )
            output["batch_import"] = asdict(batch_result)
            with session_factory() as session:
                repository = SqlAlchemyCustomerPriceTypeRepository(session)
                batch = repository.get_review_batch(args.batch_key)
                if batch is None:
                    raise RuntimeError("review batch disappeared after import")
                _, total, counts, review_status_counts, mismatch_count = repository.list_portfolio(
                    batch=batch,
                    access=CustomerPriceTypeAccessScope(
                        actor="shadow-run",
                        role="internal",
                        can_view_money=True,
                    ),
                    run_id=run.run_id,
                    bucket="all",
                    current_price_type=None,
                    action_required=None,
                    search=None,
                    limit=500,
                    offset=0,
                )
            output["persisted_portfolio"] = {
                "total": total,
                "counts": counts,
                "review_status_counts": review_status_counts,
                "mismatch_count": mismatch_count,
            }

    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
