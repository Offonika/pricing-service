"""Normalize legacy paid receivable rows that still have an active manager comment."""

from __future__ import annotations

import argparse
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.infrastructure.db import session_scope
from app.models import ReceivableWorkEvent, ReceivableWorkItem
from app.services.receivable_workflow import STATUS_PAID, utcnow

NORMALIZATION_VERSION = "20260805_paid_manager_comments_v1"
EVENT_MANAGER_COMMENT_CLEARED = "manager_comment_cleared"
NORMALIZATION_SOURCE = "release_normalization"


def normalize_paid_manager_comments(session: Session, *, apply: bool) -> dict[str, object]:
    rows = (
        session.execute(
            select(ReceivableWorkItem)
            .where(
                ReceivableWorkItem.status == STATUS_PAID,
                ReceivableWorkItem.last_contact_comment.is_not(None),
            )
            .order_by(ReceivableWorkItem.id)
        )
        .scalars()
        .all()
    )
    candidates = [
        {
            "work_item_id": item.id,
            "counterparty_ref": item.counterparty_ref,
            "comment_length": len(item.last_contact_comment or ""),
        }
        for item in rows
    ]
    applied = 0
    if apply:
        for item in rows:
            idempotency_key = f"{NORMALIZATION_VERSION}|{item.id}"
            existing = session.scalar(
                select(ReceivableWorkEvent).where(
                    ReceivableWorkEvent.idempotency_key == idempotency_key
                )
            )
            if existing is not None:
                raise RuntimeError(
                    "Normalization audit event already exists while the active comment remains: "
                    f"work_item_id={item.id}"
                )
            previous_comment = item.last_contact_comment
            session.add(
                ReceivableWorkEvent(
                    work_item=item,
                    event_type=EVENT_MANAGER_COMMENT_CLEARED,
                    event_at=utcnow(),
                    source=NORMALIZATION_SOURCE,
                    comment=previous_comment,
                    payload={
                        "normalization_version": NORMALIZATION_VERSION,
                        "previous_status": item.status,
                        "manager_comment_cleared": True,
                    },
                    idempotency_key=idempotency_key,
                )
            )
            item.last_contact_comment = None
            applied += 1
        session.flush()
    return {
        "normalization_version": NORMALIZATION_VERSION,
        "mode": "apply" if apply else "dry_run",
        "candidate_count": len(candidates),
        "applied_count": applied,
        "candidates": candidates,
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Show rows without DB changes")
    mode.add_argument("--apply", action="store_true", help="Audit and clear active comments")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    with session_scope(read_only=not args.apply) as session:
        result = normalize_paid_manager_comments(session, apply=args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
