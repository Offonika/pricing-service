from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.infrastructure.db.session import get_application_session_factory
from app.models.procurement_order_formation import ProcurementOrderFormation
from app.services.procurement_order_formation import PROCUREMENT_PROCESS_ENTITY_TYPE_ID
from app.services.procurement_order_product_rows import (
    list_procurement_product_rows,
    preflight_procurement_product_rows,
    summarize_product_row_sync,
    sync_procurement_order_product_rows,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RESULT_PATH = REPO_ROOT / "build/bitrix/procurement_product_rows_sync.json"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reconcile canonical 1C order lines with Smart Process product rows"
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--order-id", action="append", type=int, default=[])
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--backup-path", type=Path)
    parser.add_argument("--result-path", type=Path, default=DEFAULT_RESULT_PATH)
    return parser.parse_args(argv)


def _webhook(settings) -> str:
    return str(
        settings.procurement_bitrix_webhook_url or settings.bitrix_box_webhook_base or ""
    ).strip()


def _orders(session, *, order_ids: list[int], limit: int) -> list[ProcurementOrderFormation]:
    statement = (
        select(ProcurementOrderFormation)
        .options(selectinload(ProcurementOrderFormation.lines))
        .where(
            ProcurementOrderFormation.bitrix_entity_type_id == PROCUREMENT_PROCESS_ENTITY_TYPE_ID,
            ProcurementOrderFormation.bitrix_item_id.is_not(None),
        )
        .order_by(ProcurementOrderFormation.id)
        .limit(limit)
    )
    if order_ids:
        statement = statement.where(ProcurementOrderFormation.id.in_(order_ids))
    return list(session.scalars(statement).all())


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.apply and not args.all and not args.order_id:
        raise ValueError("--apply requires --all or at least one --order-id")
    if args.apply and args.backup_path is None:
        raise ValueError("--apply requires --backup-path")
    settings = get_settings()
    webhook = _webhook(settings)
    if not webhook:
        raise RuntimeError("PROCUREMENT_BITRIX_WEBHOOK_URL is not configured")
    session = get_application_session_factory()()
    try:
        orders = _orders(session, order_ids=list(args.order_id), limit=args.limit)
        preflight = None
        if args.preflight:
            if not orders:
                raise RuntimeError("No linked Smart Process order is available for preflight")
            preflight = preflight_procurement_product_rows(
                item_id=str(orders[0].bitrix_item_id),
                settings=settings,
                webhook_base=webhook,
            )

        backup: list[dict[str, Any]] = []
        if args.apply:
            for order in orders:
                backup.append(
                    {
                        "order_id": order.id,
                        "item_id": order.bitrix_item_id,
                        "onec_document_number": order.onec_document_number,
                        "rows": list_procurement_product_rows(
                            item_id=str(order.bitrix_item_id),
                            settings=settings,
                            webhook_base=webhook,
                        ),
                    }
                )
            _write_json(
                args.backup_path,
                {
                    "created_at": datetime.now(UTC).isoformat(),
                    "entity_type_id": PROCUREMENT_PROCESS_ENTITY_TYPE_ID,
                    "orders": backup,
                },
            )

        results: list[dict[str, Any]] = []
        for order in orders:
            result = sync_procurement_order_product_rows(
                session,
                order,
                apply=bool(args.apply),
                settings=settings,
                webhook_base=webhook,
                actor="system:procurement-product-rows-backfill",
            )
            results.append(result)
            if args.apply:
                session.commit()
        if not args.apply:
            session.rollback()
        return {
            "mode": "apply" if args.apply else "dry-run",
            "preflight": preflight,
            "orders": len(orders),
            "summary": summarize_product_row_sync(results),
            "results": results,
            "backup_path": str(args.backup_path) if args.backup_path else None,
        }
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = run(args)
    _write_json(args.result_path, result)
    print(
        json.dumps({key: result[key] for key in ("mode", "orders", "summary")}, ensure_ascii=False)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
