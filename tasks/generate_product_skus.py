from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, selectinload

from app.models import Product
from app.services.exporters.ut103_exchange import load_ut103_env_file, resolve_ut103_exchange_root
from app.services.exporters.ut103_nomenclature_properties import (
    DEFAULT_SOURCE,
    NomenclaturePropertyUpdateMessage,
    NomenclaturePropertyUpdateRow,
    build_nomenclature_property_updates_xml,
    write_nomenclature_property_updates_message,
)
from app.services.sku import generate_sku_batch

DEFAULT_SKU_PROPERTY_NAME = "SKU"
DEFAULT_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate planned SKU for products from DB attributes."
    )
    parser.add_argument("--write", action="store_true", help="Persist planned SKU values to DB.")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Process all products, not only products with missing SKU.",
    )
    parser.add_argument(
        "--include-inactive",
        action="store_true",
        help="Include inactive or deletion-marked products.",
    )
    parser.add_argument(
        "--export-existing",
        action="store_true",
        help=(
            "Add active products with already planned SKU waiting for 1C "
            "to the UT103 update package without regenerating them."
        ),
    )
    parser.add_argument(
        "--product-id",
        action="append",
        dest="product_ids",
        type=int,
        default=None,
        help="Restrict generation to specific product IDs.",
    )
    parser.add_argument("--message-id", help="Stable idempotency key for the 1C update package")
    parser.add_argument("--mode", choices=("dry_run", "apply"), default="dry_run")
    parser.add_argument("--approved-by", default="", help="Required for 1C apply mode")
    parser.add_argument("--source", default=None)
    parser.add_argument("--changed-at", type=_parse_date, default=None)
    parser.add_argument("--exchange-root", help="UT103 exchange root for --write-ready")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument(
        "--sku-property-name",
        default=DEFAULT_SKU_PROPERTY_NAME,
        help="1C nomenclature property name that stores the generated SKU.",
    )
    parser.add_argument("--print-xml", action="store_true", help="Print 1C update XML")
    parser.add_argument(
        "--write-ready",
        action="store_true",
        help="Write ready XML with generated SKU updates into the UT103 exchange folder.",
    )
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="Exit successfully without writing XML when no SKU rows are ready for 1C.",
    )
    return parser.parse_args()


def main() -> None:
    load_ut103_env_file()
    _load_database_env_file()
    args = parse_args()
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL is not set", file=sys.stderr)
        raise SystemExit(1)

    engine = create_engine(db_url)
    with Session(engine) as session:
        result = generate_sku_batch(
            session,
            product_ids=args.product_ids,
            dry_run=not args.write,
            only_missing=not args.all,
            active_only=not args.include_inactive,
        )
        existing_items = (
            _build_existing_sku_export_items(
                session,
                product_ids=args.product_ids,
                active_only=not args.include_inactive,
                exclude_product_ids={int(item["product_id"]) for item in result["items"]},
            )
            if args.export_existing
            else []
        )
    export_items = [*result["items"], *existing_items]
    result["existing_sku_export_items"] = len(existing_items)
    result["ut103_export_candidates"] = len(export_items)
    rows, skipped = _build_sku_property_rows(
        export_items,
        property_name=args.sku_property_name,
        changed_at=args.changed_at,
    )
    result["ut103_property_rows"] = len(rows)
    result["ut103_property_skipped"] = skipped

    output_path: Path | None = None
    if args.print_xml or args.write_ready:
        if not rows:
            if args.allow_empty:
                result["ut103_property_path"] = None
                print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
                return
            raise SystemExit("No generated SKU rows can be exported to 1C")
        message = NomenclaturePropertyUpdateMessage(
            message_id=args.message_id
            or f"sku-properties-{datetime.now().strftime('%Y%m%d%H%M%S')}",
            rows=tuple(rows),
            mode=args.mode,
            approved_by=args.approved_by,
            source=args.source
            or os.environ.get("UT103_NOMENCLATURE_PROPERTIES_SOURCE", DEFAULT_SOURCE),
        )
        if args.print_xml:
            print(build_nomenclature_property_updates_xml(message).decode("windows-1251"))
            return
        try:
            exchange_root = resolve_ut103_exchange_root(args.exchange_root)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        output_path = write_nomenclature_property_updates_message(
            exchange_root,
            message,
            overwrite=args.overwrite,
        )

    result["ut103_property_path"] = str(output_path) if output_path else None
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


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


def _build_existing_sku_export_items(
    session: Session,
    *,
    product_ids: list[int] | None,
    active_only: bool,
    exclude_product_ids: set[int],
) -> list[dict[str, Any]]:
    query = (
        select(Product)
        .options(selectinload(Product.sku_plans))
        .where(Product.planned_sku.is_not(None))
        .order_by(Product.id)
    )
    if product_ids:
        query = query.where(Product.id.in_(product_ids))
    if exclude_product_ids:
        query = query.where(Product.id.not_in(exclude_product_ids))
    if active_only:
        query = query.where(
            Product.is_active.is_(True),
            Product.is_marked_for_deletion.is_(False),
        )

    items: list[dict[str, Any]] = []
    for product in session.execute(query).scalars():
        planned_sku = str(product.planned_sku or "").strip()
        if not planned_sku:
            continue
        active_plan = next((plan for plan in product.sku_plans if plan.is_active), None)
        plan_status = active_plan.status if active_plan else "generated"
        sync_status = _existing_sku_sync_status(product.fact_sku, planned_sku)
        if plan_status != "generated" or sync_status not in {"missing_in_1c", "mismatch"}:
            continue
        items.append(
            {
                "product_id": product.id,
                "article": product.article,
                "code_1c": product.code_1c,
                "fact_sku": product.fact_sku,
                "planned_sku": planned_sku,
                "status": plan_status,
                "sync_status": sync_status,
                "reasons": [],
            }
        )
    return items


def _existing_sku_sync_status(fact_sku: str | None, planned_sku: str) -> str:
    clean_fact = str(fact_sku or "").strip()
    clean_planned = planned_sku.strip()
    if clean_fact and clean_fact == clean_planned:
        return "match"
    if clean_fact and clean_planned:
        return "mismatch"
    if clean_planned:
        return "missing_in_1c"
    return "manual_review"


def _build_sku_property_rows(
    items: list[dict[str, Any]],
    *,
    property_name: str = DEFAULT_SKU_PROPERTY_NAME,
    changed_at: date | None = None,
) -> tuple[list[NomenclaturePropertyUpdateRow], list[dict[str, Any]]]:
    rows: list[NomenclaturePropertyUpdateRow] = []
    skipped: list[dict[str, Any]] = []
    change_date = changed_at or date.today()
    clean_property_name = property_name.strip()
    if not clean_property_name:
        raise SystemExit("--sku-property-name must not be empty")

    for item in items:
        if item.get("status") != "generated":
            _skip_sku_property_row(skipped, item, "not_generated")
            continue
        planned_sku = str(item.get("planned_sku") or "").strip()
        if not planned_sku:
            _skip_sku_property_row(skipped, item, "missing_planned_sku")
            continue
        sync_status = str(item.get("sync_status") or "").strip()
        if sync_status not in {"missing_in_1c", "mismatch"}:
            _skip_sku_property_row(skipped, item, f"sync_status_{sync_status or 'empty'}")
            continue
        nomenclature_code = str(item.get("code_1c") or "").strip()
        if not nomenclature_code:
            _skip_sku_property_row(skipped, item, "missing_code_1c")
            continue
        fact_sku = str(item.get("fact_sku") or "").strip()
        rows.append(
            NomenclaturePropertyUpdateRow(
                idempotency_key=(
                    f"nom-prop:{nomenclature_code}:{clean_property_name}:"
                    f"{planned_sku}:{change_date.isoformat()}:r1"
                ),
                nomenclature_code=nomenclature_code,
                property_name=clean_property_name,
                value_type="string",
                target_kind="requisite",
                new_value=planned_sku,
                expected_current_value_name=fact_sku,
                reason="Автоматически сгенерированный SKU из pricing-service",
            )
        )
    return rows, skipped


def _skip_sku_property_row(
    skipped: list[dict[str, Any]], item: dict[str, Any], reason: str
) -> None:
    skipped.append(
        {
            "product_id": item.get("product_id"),
            "article": item.get("article"),
            "code_1c": item.get("code_1c"),
            "planned_sku": item.get("planned_sku"),
            "sync_status": item.get("sync_status"),
            "reason": reason,
        }
    )


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from exc


def _strip_env_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


if __name__ == "__main__":
    main()
