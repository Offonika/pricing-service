from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models import Product
from app.services.exporters.ut103_exchange import load_ut103_env_file, resolve_ut103_exchange_root
from app.services.exporters.ut103_nomenclature_properties import (
    PropertyUpdateExchangeResult,
    PropertyUpdateItemResult,
    list_property_update_exchange_results,
)
from app.services.sku import sync_product_sku_status

DEFAULT_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
DEFAULT_SKU_PROPERTY_NAME = "SKU"
SUCCESS_RESULTS = frozenset({"applied", "already_actual"})
ERROR_RESULTS = frozenset({"failed", "needs_review", "error"})
IGNORED_RESULTS = frozenset({"validated", "duplicate"})
MAX_DETAILS = 100


def main() -> int:
    load_ut103_env_file()
    _load_database_env_file()
    args = _parse_args()
    database_url = args.database_url or os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 1

    try:
        exchange_root = resolve_ut103_exchange_root(args.exchange_root)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    results = list_property_update_exchange_results(exchange_root)
    if args.message_id:
        wanted = set(args.message_id)
        results = [result for result in results if result.message_id in wanted]

    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with Session(engine) as session:
            summary = apply_sku_results(
                session,
                results,
                property_name=args.property_name,
                dry_run=args.dry_run,
            )
            if args.dry_run:
                session.rollback()
            else:
                session.commit()
    finally:
        engine.dispose()

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    else:
        print(_human_summary(summary))
    return 0


def apply_sku_results(
    session: Session,
    results: Iterable[PropertyUpdateExchangeResult],
    *,
    property_name: str = DEFAULT_SKU_PROPERTY_NAME,
    dry_run: bool = False,
) -> dict[str, Any]:
    clean_property_name = property_name.strip()
    if not clean_property_name:
        raise ValueError("property_name must not be empty")

    summary: dict[str, Any] = {
        "dry_run": dry_run,
        "files": 0,
        "sku_items": 0,
        "success_items": 0,
        "error_items": 0,
        "ignored_items": 0,
        "missing_products": 0,
        "ambiguous_products": 0,
        "duplicate_fact_sku": 0,
        "updated_products": 0,
        "already_synced_products": 0,
        "details": [],
    }
    details: list[dict[str, Any]] = summary["details"]

    for result in results:
        summary["files"] += 1
        for item in result.item_results:
            if item.property_name.strip().casefold() != clean_property_name.casefold():
                continue
            summary["sku_items"] += 1
            item_result = item.result.strip().casefold()
            if item_result in SUCCESS_RESULTS:
                _apply_success_item(session, item, result, summary, details, dry_run=dry_run)
            elif item_result in ERROR_RESULTS:
                _apply_error_item(session, item, result, summary, details, dry_run=dry_run)
            else:
                summary["ignored_items"] += 1
                if item_result not in IGNORED_RESULTS:
                    _append_detail(
                        details,
                        {
                            "type": "ignored_unknown_result",
                            "message_id": result.message_id,
                            "nomenclature_code": item.nomenclature_code,
                            "result": item.result,
                            "message": item.message,
                        },
                    )
    return summary


def _apply_success_item(
    session: Session,
    item: PropertyUpdateItemResult,
    result: PropertyUpdateExchangeResult,
    summary: dict[str, Any],
    details: list[dict[str, Any]],
    *,
    dry_run: bool,
) -> None:
    summary["success_items"] += 1
    new_fact_sku = _clean(item.new_value) or _clean(item.current_value)
    if not new_fact_sku:
        summary["error_items"] += 1
        _append_detail(
            details,
            {
                "type": "missing_result_value",
                "message_id": result.message_id,
                "nomenclature_code": item.nomenclature_code,
                "result": item.result,
            },
        )
        return

    product = _single_product_by_code(session, item, result, summary, details)
    if product is None:
        return

    duplicate = session.execute(
        select(Product).where(
            Product.fact_sku == new_fact_sku,
            Product.id != product.id,
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        summary["duplicate_fact_sku"] += 1
        if not dry_run:
            product.sku_sync_status = "error"
            product.sku_sync_error = _truncate_error(
                f"duplicate_fact_sku:{new_fact_sku};product_id:{duplicate.id}"
            )
        _append_detail(
            details,
            {
                "type": "duplicate_fact_sku",
                "message_id": result.message_id,
                "nomenclature_code": item.nomenclature_code,
                "new_value": new_fact_sku,
                "other_product_id": duplicate.id,
            },
        )
        return

    if _clean(product.fact_sku) == new_fact_sku and product.sku_sync_status == "match":
        summary["already_synced_products"] += 1
        return

    summary["updated_products"] += 1
    if dry_run:
        return
    product.fact_sku = new_fact_sku
    product.sku_sync_error = None
    sync_product_sku_status(product, plan_status="generated")


def _apply_error_item(
    session: Session,
    item: PropertyUpdateItemResult,
    result: PropertyUpdateExchangeResult,
    summary: dict[str, Any],
    details: list[dict[str, Any]],
    *,
    dry_run: bool,
) -> None:
    summary["error_items"] += 1
    product = _single_product_by_code(session, item, result, summary, details)
    error_text = item.message or result.errors or item.result or "1c_result_error"
    if product is not None and not dry_run:
        product.sku_sync_status = "error"
        product.sku_sync_error = _truncate_error(error_text)
    _append_detail(
        details,
        {
            "type": "item_error",
            "message_id": result.message_id,
            "nomenclature_code": item.nomenclature_code,
            "result": item.result,
            "message": error_text,
        },
    )


def _single_product_by_code(
    session: Session,
    item: PropertyUpdateItemResult,
    result: PropertyUpdateExchangeResult,
    summary: dict[str, Any],
    details: list[dict[str, Any]],
) -> Product | None:
    nomenclature_code = _clean(item.nomenclature_code)
    if not nomenclature_code:
        summary["missing_products"] += 1
        _append_detail(
            details,
            {
                "type": "missing_nomenclature_code",
                "message_id": result.message_id,
                "result": item.result,
            },
        )
        return None

    products = (
        session.execute(
            select(Product).where(Product.code_1c == nomenclature_code).order_by(Product.id)
        )
        .scalars()
        .all()
    )
    if not products:
        summary["missing_products"] += 1
        _append_detail(
            details,
            {
                "type": "missing_product",
                "message_id": result.message_id,
                "nomenclature_code": nomenclature_code,
                "result": item.result,
            },
        )
        return None
    if len(products) > 1:
        summary["ambiguous_products"] += 1
        _append_detail(
            details,
            {
                "type": "ambiguous_product",
                "message_id": result.message_id,
                "nomenclature_code": nomenclature_code,
                "product_ids": [product.id for product in products[:10]],
                "result": item.result,
            },
        )
        return None
    return products[0]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Apply UT103 nomenclature_property_updates.v1 result files to SKU fields."
    )
    parser.add_argument("--exchange-root", help="UT103 exchange root")
    parser.add_argument("--database-url", default="")
    parser.add_argument("--property-name", default=DEFAULT_SKU_PROPERTY_NAME)
    parser.add_argument(
        "--message-id",
        action="append",
        help="Restrict processing to a specific result message id; can be repeated.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Do not write DB changes")
    parser.add_argument("--json", action="store_true", help="Print machine-readable summary")
    return parser.parse_args()


def _human_summary(summary: dict[str, Any]) -> str:
    ordered_keys = [
        "dry_run",
        "files",
        "sku_items",
        "success_items",
        "error_items",
        "ignored_items",
        "missing_products",
        "ambiguous_products",
        "duplicate_fact_sku",
        "updated_products",
        "already_synced_products",
    ]
    lines = [f"{key}: {summary[key]}" for key in ordered_keys]
    details = summary.get("details") or []
    if details:
        lines.append("details:")
        lines.extend(json.dumps(item, ensure_ascii=False, sort_keys=True) for item in details[:10])
    return "\n".join(lines)


def _append_detail(details: list[dict[str, Any]], item: dict[str, Any]) -> None:
    if len(details) < MAX_DETAILS:
        details.append(item)


def _load_database_env_file(env_file: str | Path | None = None) -> None:
    path = Path(env_file) if env_file is not None else DEFAULT_ENV_FILE
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() == "DATABASE_URL":
            os.environ.setdefault("DATABASE_URL", _strip_env_quotes(value.strip()))
            return


def _strip_env_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _clean(value: str | None) -> str:
    return str(value or "").strip()


def _truncate_error(value: str) -> str:
    return value[:255]


if __name__ == "__main__":
    raise SystemExit(main())
