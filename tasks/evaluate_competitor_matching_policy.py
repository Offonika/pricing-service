"""Evaluate category auto-accept policy on chronological decision snapshots."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.infrastructure.db import session_scope
from app.models import ProductCompetitorItemDecision
from app.models.competitor_item_match import CompetitorItemMatch, CompetitorItemMatchStatus
from app.services.competitor_auto_accept_policy import load_auto_accept_policy
from app.services.competitor_matching_replay import (
    build_auto_accept_audit_sample,
    evaluate_snapshot_decisions,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", help="Policy JSON path")
    parser.add_argument("--artifact-file", type=Path, help="Optional JSON report path")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    policy = load_auto_accept_policy(args.policy)
    with session_scope(read_only=True) as session:
        decisions = list(
            session.execute(
                select(ProductCompetitorItemDecision).order_by(
                    ProductCompetitorItemDecision.created_at,
                    ProductCompetitorItemDecision.id,
                )
            ).scalars()
        )
        accepted_matches = list(
            session.execute(
                select(CompetitorItemMatch)
                .options(
                    joinedload(CompetitorItemMatch.competitor_item),
                    joinedload(CompetitorItemMatch.product),
                )
                .where(CompetitorItemMatch.status == CompetitorItemMatchStatus.ACCEPTED)
            ).scalars()
        )
    report = evaluate_snapshot_decisions(
        decisions,
        target_precision=policy.target_precision,
        minimum_examples=policy.minimum_validation_examples,
        audit_sample_rate=policy.audit_sample_rate,
        rollback_error_rate=policy.rollback_error_rate,
    )
    report["auto_accept_audit_sample"] = build_auto_accept_audit_sample(
        accepted_matches,
        sample_rate=policy.audit_sample_rate,
    )
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.artifact_file:
        args.artifact_file.parent.mkdir(parents=True, exist_ok=True)
        args.artifact_file.write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
