"""Safely correct a transmitted procurement order that has no 1C document number."""

from __future__ import annotations

import argparse
import json
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.infrastructure.db.engines import build_engine
from app.services.procurement_order_formation import (
    mark_transmitted_order_for_number_reconciliation,
)
from app.services.procurement_order_formation_workspace import record_event


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Mark one transmitted procurement order without a 1C number for reconciliation."
    )
    parser.add_argument("--order-id", type=int, required=True)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def reconcile_order(db: Session, *, order_id: int, apply: bool) -> dict[str, Any]:
    order, before, after, changed = mark_transmitted_order_for_number_reconciliation(db, order_id)
    if apply and changed:
        record_event(
            db,
            entity_type="procurement_order_formation",
            entity_id=order.id,
            event_type="onec_number_reconciliation_required",
            order_id=order.id,
            before=before,
            after=after,
            idempotency_key=f"procurement-order:{order.id}:missing-onec-number-reconciliation:v1",
            actor="system:label-stabilization-reconciliation",
        )
        db.commit()
    elif apply:
        db.rollback()
    else:
        db.rollback()
    return {
        "mode": "apply" if apply else "dry-run",
        "order_id": order_id,
        "changed": changed,
        "before": before,
        "after": after,
    }


def main() -> int:
    args = parse_args()
    settings = get_settings()
    engine = build_engine(settings.database_url)
    try:
        with Session(engine) as db:
            result = reconcile_order(db, order_id=args.order_id, apply=args.apply)
    finally:
        engine.dispose()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
