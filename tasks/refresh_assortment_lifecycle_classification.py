from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.infrastructure.db.engines import build_engine
from app.services.assortment_lifecycle_classification_store import (
    build_classification_rows,
    persist_classification_rows,
    result_to_mapping,
    utcnow_naive,
)
from app.services.assortment_lifecycle_facts import (
    DEFAULT_HISTORY_MONTHS,
    RECEIPT_MAPPING_UNRESOLVED,
    SUPPLIER_ORDER_MAPPING_UNRESOLVED,
    DocumentLineMapping,
    build_assortment_lifecycle_fact_records,
    default_history_start,
    enrich_nomenclature_rows_with_product_snapshot,
    fetch_onec_lifecycle_source_rows,
    normalize_manager_signals,
    normalize_manual_overrides,
    validate_warehouse_policy,
)
from app.services.exporters.ut103_exchange import load_ut103_env_file, resolve_ut103_exchange_root
from app.services.exporters.ut103_nomenclature_properties import (
    NomenclaturePropertyUpdateMessage,
    NomenclaturePropertyUpdateRow,
    build_nomenclature_property_updates_xml,
    write_nomenclature_property_updates_message,
)
from app.services.onec_stock_availability import (
    DEFAULT_HISTORY_DAYS as DEFAULT_AVAILABILITY_HISTORY_DAYS,
)
from app.services.onec_stock_availability import (
    attach_effective_availability_shadow_to_facts,
)
from app.services.procurement_order_formation_workspace import (
    sync_lifecycle_transition_proposals,
)
from tasks.build_assortment_lifecycle_updates import build_updates_from_records

DEFAULT_SOURCE = "assortment_lifecycle_postgres_v1"
DEFAULT_FACT_STATUS_DECISIONS_JSON = Path("config/assortment/display-fact-status-decisions.json")


def main() -> int:
    load_ut103_env_file()
    args = _parse_args()
    started_at = utcnow_naive()
    classified_at = args.classified_at or started_at
    settings = get_settings()
    database_url = args.database_url or os.environ.get("DATABASE_URL") or settings.database_url

    try:
        records = _load_or_build_fact_records(
            args,
            database_url=database_url,
            settings_onec_database_url=settings.onec_database_url or "",
        )
        update_rows, summaries = build_updates_from_records(
            records,
            folder_filter=args.folder,
            changed_at=classified_at.date(),
            source=args.source,
            suspicious_quantity_threshold=args.suspicious_quantity_threshold,
        )
        rows = build_classification_rows(
            records=records,
            summaries=summaries,
            source=args.source,
            classified_at=classified_at,
        )
        engine = build_engine(database_url, pool_pre_ping=True)
        try:
            result = persist_classification_rows(
                engine,
                rows=rows,
                run_key=args.run_key or _default_run_key(classified_at, args.folder),
                folder=args.folder,
                source=args.source,
                started_at=started_at,
                finished_at=utcnow_naive(),
                dry_run=args.dry_run,
            )
            transition_sync = {
                "created": 0,
                "updated": 0,
                "automatic": 0,
                "stale": 0,
                "run_id": 0,
            }
            if (
                result.run_id is not None
                and not args.dry_run
                and inspect(engine).has_table("procurement_lifecycle_transition_proposal")
            ):
                with Session(engine) as db:
                    transition_sync = sync_lifecycle_transition_proposals(
                        db,
                        folder=args.folder,
                        run_id=result.run_id,
                    )
        finally:
            engine.dispose()
    except ValueError as exc:
        if args.json:
            print(
                json.dumps(
                    {"status": "blocked", "error": str(exc)},
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        raise SystemExit(str(exc)) from exc

    property_update_path = _export_property_updates(
        args,
        message_id=args.message_id or _default_message_id(classified_at, args.folder),
        rows=update_rows,
    )

    payload = {"status": "ready", **result_to_mapping(result)}
    payload.update(
        {
            "transition_sync": transition_sync,
            "property_update_message_id": args.message_id
            or _default_message_id(classified_at, args.folder),
            "property_update_mode": args.export_mode,
            "property_update_rows": len(update_rows),
            "property_update_path": str(property_update_path) if property_update_path else None,
        }
    )
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(
            "assortment lifecycle classification refreshed:",
            json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
        )
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Refresh current assortment lifecycle classification in Postgres. "
            "The task reads Ekama/1C facts or a prepared assortment_lifecycle_facts.v1 file, "
            "calculates statuses, and upserts the current classification snapshot."
        )
    )
    parser.add_argument("--folder", default=os.getenv("ASSORTMENT_LIFECYCLE_FOLDER", "дисплеи"))
    parser.add_argument("--history-months", type=int, default=_default_history_months())
    parser.add_argument("--today", type=_parse_date, default=None)
    parser.add_argument("--limit", type=int, default=_default_limit())
    parser.add_argument("--database-url", default="")
    parser.add_argument("--onec-database-url", default="")
    parser.add_argument("--facts-json", type=Path, help="Prepared assortment_lifecycle_facts.v1")
    parser.add_argument(
        "--source-rows-json",
        type=Path,
        help="Offline source rows fixture: {nomenclature_rows, supplier_order_rows, receipt_rows}",
    )
    parser.add_argument(
        "--warehouse-policy-json",
        type=Path,
        default=_env_path("ASSORTMENT_WAREHOUSE_POLICY_JSON"),
    )
    parser.add_argument(
        "--supplier-order-mapping-json",
        type=Path,
        default=_env_path("ASSORTMENT_SUPPLIER_ORDER_MAPPING_JSON"),
    )
    parser.add_argument(
        "--receipt-mapping-json",
        type=Path,
        default=_env_path("ASSORTMENT_RECEIPT_MAPPING_JSON"),
    )
    parser.add_argument(
        "--manual-overrides-json",
        type=Path,
        default=_env_path("ASSORTMENT_MANUAL_OVERRIDES_JSON"),
    )
    parser.add_argument(
        "--manager-signals-json",
        type=Path,
        default=_env_path("ASSORTMENT_MANAGER_SIGNALS_JSON"),
    )
    parser.add_argument(
        "--fact-status-decisions-json",
        type=Path,
        default=_default_fact_status_decisions_json(),
        help=(
            "Optional JSON registry with fact-based target statuses. "
            "Affects Postgres classification only; UT103 export is blocked until approval."
        ),
    )
    parser.add_argument("--source", default=DEFAULT_SOURCE)
    parser.add_argument("--run-key", help="Optional idempotency key for the refresh run")
    parser.add_argument("--message-id", help="Stable message id for the UT103 export package")
    parser.add_argument("--classified-at", type=_parse_datetime, default=None)
    parser.add_argument("--suspicious-quantity-threshold", default=None)
    parser.add_argument("--dry-run", action="store_true", help="Calculate without DB writes")
    parser.add_argument("--output-json", type=Path)
    parser.add_argument(
        "--export-mode",
        choices=("dry_run", "apply"),
        default=os.getenv("ASSORTMENT_LIFECYCLE_UT103_MODE", "dry_run"),
        help="UT103 property update package mode",
    )
    parser.add_argument(
        "--approved-by",
        default=os.getenv("ASSORTMENT_LIFECYCLE_UT103_APPROVED_BY", ""),
        help="Required for UT103 apply mode",
    )
    parser.add_argument("--exchange-root", help="UT103 exchange root for --write-ready")
    parser.add_argument("--print-xml", action="store_true", help="Print UT103 XML package")
    parser.add_argument(
        "--write-ready",
        action="store_true",
        help="Write ready UT103 XML with assortment property updates",
    )
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="Exit successfully without writing XML when no assortment property rows are ready.",
    )
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing ready XML")
    parser.add_argument("--json", action="store_true", help="Print machine-readable summary")
    args = parser.parse_args()
    if args.history_months <= 0:
        raise SystemExit("--history-months must be positive")
    if args.limit <= 0:
        raise SystemExit("--limit must be positive")
    return args


def _export_property_updates(
    args: argparse.Namespace,
    *,
    message_id: str,
    rows: list[NomenclaturePropertyUpdateRow],
) -> Path | None:
    if not (args.print_xml or args.write_ready):
        return None
    if not rows:
        if args.allow_empty:
            return None
        raise SystemExit("No property update rows built; nothing to export")

    message = NomenclaturePropertyUpdateMessage(
        message_id=message_id,
        rows=tuple(rows),
        mode=args.export_mode,
        approved_by=args.approved_by,
        source=args.source,
    )
    if args.print_xml:
        print(build_nomenclature_property_updates_xml(message).decode("windows-1251"))
    if not args.write_ready:
        return None
    try:
        exchange_root = resolve_ut103_exchange_root(args.exchange_root)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    return write_nomenclature_property_updates_message(
        exchange_root,
        message,
        overwrite=args.overwrite,
    )


def _default_history_months() -> int:
    raw_value = os.getenv("ASSORTMENT_LIFECYCLE_HISTORY_MONTHS")
    if raw_value is None or raw_value == "":
        return DEFAULT_HISTORY_MONTHS
    try:
        return int(raw_value)
    except ValueError as exc:
        raise SystemExit("ASSORTMENT_LIFECYCLE_HISTORY_MONTHS must be an integer") from exc


def _default_limit() -> int:
    raw_value = os.getenv("ASSORTMENT_LIFECYCLE_LIMIT")
    if raw_value is None or raw_value == "":
        return 3000
    try:
        return int(raw_value)
    except ValueError as exc:
        raise SystemExit("ASSORTMENT_LIFECYCLE_LIMIT must be an integer") from exc


def _load_or_build_fact_records(
    args: argparse.Namespace,
    *,
    database_url: str,
    settings_onec_database_url: str = "",
) -> list[dict[str, Any]]:
    if args.facts_json:
        facts = _load_fact_records(args.facts_json)
    else:
        if args.warehouse_policy_json is None:
            raise ValueError("warehouse_policy_required: set --warehouse-policy-json")
        warehouse_policy = validate_warehouse_policy(_load_json_object(args.warehouse_policy_json))
        manual_overrides = normalize_manual_overrides(
            _load_optional_json(args.manual_overrides_json)
        )
        manager_signals = normalize_manager_signals(_load_optional_json(args.manager_signals_json))
        history_start = default_history_start(args.today, history_months=args.history_months)

        if args.source_rows_json:
            raw_payload = _load_json_object(args.source_rows_json)
            nomenclature_rows = _list_field(raw_payload, "nomenclature_rows")
            supplier_order_rows = _list_field(raw_payload, "supplier_order_rows")
            receipt_rows = _list_field(raw_payload, "receipt_rows")
        else:
            if args.supplier_order_mapping_json is None:
                raise ValueError(
                    f"{SUPPLIER_ORDER_MAPPING_UNRESOLVED}: set --supplier-order-mapping-json"
                )
            if args.receipt_mapping_json is None:
                raise ValueError(f"{RECEIPT_MAPPING_UNRESOLVED}: set --receipt-mapping-json")
            onec_database_url = (
                args.onec_database_url
                or os.environ.get("ONEC_DATABASE_URL", "")
                or settings_onec_database_url
            )
            if not onec_database_url:
                raise ValueError(
                    "ONEC_DATABASE_URL is required unless --source-rows-json is passed"
                )
            supplier_mapping = DocumentLineMapping.from_mapping(
                _load_json_object(args.supplier_order_mapping_json)
            )
            receipt_mapping = DocumentLineMapping.from_mapping(
                _load_json_object(args.receipt_mapping_json)
            )
            onec_engine = build_engine(onec_database_url, pool_pre_ping=True)
            try:
                (
                    nomenclature_rows,
                    supplier_order_rows,
                    receipt_rows,
                ) = fetch_onec_lifecycle_source_rows(
                    onec_engine,
                    folder=args.folder,
                    history_start=history_start,
                    supplier_mapping=supplier_mapping,
                    receipt_mapping=receipt_mapping,
                    limit=args.limit,
                )
            finally:
                onec_engine.dispose()
            product_engine = build_engine(database_url, pool_pre_ping=True)
            try:
                nomenclature_rows = enrich_nomenclature_rows_with_product_snapshot(
                    product_engine,
                    nomenclature_rows,
                )
            finally:
                product_engine.dispose()

        facts, _ = build_assortment_lifecycle_fact_records(
            nomenclature_rows=nomenclature_rows,
            supplier_order_rows=supplier_order_rows,
            receipt_rows=receipt_rows,
            warehouse_policy=warehouse_policy,
            manual_overrides=manual_overrides,
            manager_signals=manager_signals,
            history_start=history_start,
        )
    fact_status_decisions = _load_fact_status_decisions(args.fact_status_decisions_json)
    facts = _attach_fact_status_decisions(facts, fact_status_decisions)
    product_engine = create_engine(database_url, pool_pre_ping=True)
    try:
        return attach_effective_availability_shadow_to_facts(
            product_engine,
            facts,
            date_to=(args.today or date.today()) - timedelta(days=1),
            history_days=DEFAULT_AVAILABILITY_HISTORY_DAYS,
        )
    finally:
        product_engine.dispose()


def _load_fact_records(path: Path) -> list[dict[str, Any]]:
    payload = _load_json(path)
    records = payload.get("items") if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        raise SystemExit("facts JSON must be a list or an object with an items list")
    if not all(isinstance(item, dict) for item in records):
        raise SystemExit("Every fact item must be an object")
    return records


def _load_fact_status_decisions(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None or not path.exists():
        return {}
    payload = _load_json(path)
    raw_items = payload.get("items") if isinstance(payload, dict) else payload
    if not isinstance(raw_items, list):
        raise SystemExit("fact status decisions JSON must be a list or an object with items")
    result: dict[str, dict[str, Any]] = {}
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise SystemExit("fact status decision item must be an object")
        code = str(raw.get("nomenclature_code") or raw.get("NomenclatureCode") or "").strip()
        if not code:
            raise SystemExit("fact status decision nomenclature_code is required")
        target_status = str(raw.get("target_status") or raw.get("TargetStatus") or "").strip()
        if not target_status:
            continue
        result[code] = dict(raw)
    return result


def _attach_fact_status_decisions(
    facts: list[dict[str, Any]],
    decisions: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    if not decisions:
        return facts
    result: list[dict[str, Any]] = []
    for fact in facts:
        code = str(fact.get("nomenclature_code") or fact.get("NomenclatureCode") or "").strip()
        decision = decisions.get(code)
        if decision is None:
            result.append(fact)
        else:
            result.append({**fact, "fact_status_decision": decision})
    return result


def _default_fact_status_decisions_json() -> Path | None:
    raw_value = os.getenv("ASSORTMENT_FACT_STATUS_DECISIONS_JSON")
    if raw_value is not None:
        if raw_value.strip() == "":
            return None
        return Path(raw_value)
    return DEFAULT_FACT_STATUS_DECISIONS_JSON


def _load_optional_json(path: Path | None) -> Any:
    if path is None:
        return None
    return _load_json(path)


def _load_json_object(path: Path) -> dict[str, Any]:
    payload = _load_json(path)
    if not isinstance(payload, dict):
        raise SystemExit(f"{path} must contain a JSON object")
    return payload


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _list_field(payload: dict[str, Any], field_name: str) -> list[dict[str, Any]]:
    raw = payload.get(field_name)
    if not isinstance(raw, list):
        raise SystemExit(f"{field_name} must be a list")
    if not all(isinstance(item, dict) for item in raw):
        raise SystemExit(f"{field_name} items must be objects")
    return raw


def _env_path(name: str) -> Path | None:
    value = os.getenv(name)
    return Path(value) if value else None


def _default_run_key(classified_at: datetime, folder: str) -> str:
    safe_folder = "".join(char if char.isalnum() else "-" for char in folder.casefold()).strip("-")
    return f"assortment-lifecycle:{safe_folder or 'all'}:{classified_at:%Y%m%d%H%M%S}"


def _default_message_id(classified_at: datetime, folder: str) -> str:
    safe_folder = "".join(char if char.isalnum() else "-" for char in folder.casefold()).strip("-")
    return f"assortment-lifecycle-{safe_folder or 'all'}-{classified_at:%Y%m%d%H%M%S}"


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"date must be YYYY-MM-DD, got: {value}") from exc


def _parse_datetime(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"datetime must be ISO-8601, got: {value}") from exc


if __name__ == "__main__":
    raise SystemExit(main())
