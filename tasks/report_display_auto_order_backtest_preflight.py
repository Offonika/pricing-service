"""Build the auditable input table for the next display auto-order backtest."""

from __future__ import annotations

import argparse
import json
import os
from datetime import date, timedelta
from pathlib import Path

from app.core.config import get_settings
from app.infrastructure.db.engines import build_engine
from tasks.build_display_auto_order_dry_run import (
    WarehousePolicy,
    load_auto_order_policy,
    load_warehouse_policy,
)
from tasks.display_auto_order_backtest_preflight import (
    build_historical_incoming_by_day,
    build_preflight_tables,
    fetch_daily_unit_economics,
    fetch_kmp4_demand,
    load_scenario_config,
    reconstruct_historical_placements,
    reconstruct_historical_reserves,
    reconstruct_historical_stock,
    write_preflight_artifacts,
)
from tasks.report_display_auto_order_six_month_backtest import (
    DEFAULT_DATE_FROM,
    DEFAULT_DATE_TO,
    DEFAULT_HISTORY_START,
    DEFAULT_LAUNCH_PROFILE_MIN_SAMPLES,
    DEFAULT_RECEIPT_MAPPING_JSON,
    DEFAULT_SUPPLIER_ORDER_MAPPING_JSON,
    _clean,
    build_launch_observations,
    fetch_daily_sales,
    load_backtest_items,
    normalize_purchase_history,
)
from tasks.report_display_supplier_lead_time_history import (
    RECEIPT_MAPPING_UNRESOLVED,
    SUPPLIER_ORDER_MAPPING_UNRESOLVED,
    _load_document_line_mapping,
    build_lead_time_detail_rows,
    fetch_display_supplier_lead_time_source_rows,
)

DEFAULT_SCENARIO_CONFIG = Path(
    "config/assortment/display-auto-order-backtest-scenarios.json"
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date-from", type=date.fromisoformat, default=DEFAULT_DATE_FROM)
    parser.add_argument("--date-to", type=date.fromisoformat, default=DEFAULT_DATE_TO)
    parser.add_argument(
        "--history-start", type=date.fromisoformat, default=DEFAULT_HISTORY_START
    )
    parser.add_argument("--folder", default="дисплеи")
    parser.add_argument("--database-url", default="")
    parser.add_argument("--onec-database-url", default="")
    parser.add_argument(
        "--auto-order-policy-json",
        type=Path,
        default=Path("config/assortment/display-auto-order-policy.json"),
    )
    parser.add_argument(
        "--warehouse-policy-json",
        type=Path,
        default=Path("config/assortment/display-warehouse-policy.json"),
    )
    parser.add_argument(
        "--scenario-config-json", type=Path, default=DEFAULT_SCENARIO_CONFIG
    )
    parser.add_argument(
        "--launch-profile-min-samples",
        type=int,
        default=DEFAULT_LAUNCH_PROFILE_MIN_SAMPLES,
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.date_from > args.date_to:
        raise SystemExit("date-from must not exceed date-to")
    if (args.date_from - args.history_start).days < 365:
        raise SystemExit("history-start must provide at least 365 days of warm-up")
    if args.launch_profile_min_samples <= 0:
        raise SystemExit("launch-profile-min-samples must be positive")
    return args


def main() -> int:
    args = _parse_args()
    settings = get_settings()
    application_url = (
        args.database_url or os.environ.get("DATABASE_URL") or settings.database_url
    )
    onec_url = (
        args.onec_database_url
        or os.environ.get("ONEC_DATABASE_URL", "")
        or settings.onec_database_url
        or ""
    )
    if not onec_url:
        raise SystemExit("ONEC_DATABASE_URL is not configured")
    scenario_config = load_scenario_config(args.scenario_config_json)
    policy = load_auto_order_policy(args.auto_order_policy_json)
    warehouse_policy: WarehousePolicy = load_warehouse_policy(
        args.warehouse_policy_json
    )

    application_engine = build_engine(application_url, pool_pre_ping=True)
    try:
        items, run_id = load_backtest_items(application_engine, folder=args.folder)
    finally:
        application_engine.dispose()
    if not items:
        raise SystemExit("display auto-order cohort is empty")
    codes = sorted(
        {
            _clean(item.get("nomenclature_code"))
            for item in items
            if _clean(item.get("nomenclature_code"))
        }
    )
    warehouse_config = json.loads(
        args.warehouse_policy_json.read_text(encoding="utf-8-sig")
    )
    network_codes = sorted(
        {
            _clean(row.get("warehouse_code") or row.get("code"))
            for row in warehouse_config["warehouses"]
            if _clean(row.get("warehouse_code") or row.get("code"))
            and not row.get("is_defect_warehouse")
            and not row.get("is_non_systematic_sale")
        }
    )

    onec_engine = build_engine(onec_url, pool_pre_ping=True)
    try:
        sales = fetch_daily_sales(
            onec_engine,
            codes=codes,
            warehouse_codes=warehouse_policy.sellable_codes,
            date_from=args.history_start,
            date_to=args.date_to,
        )
        stock_by_day, availability, stock_counts = reconstruct_historical_stock(
            onec_engine,
            codes=codes,
            network_warehouse_codes=network_codes,
            physical_warehouse_codes=warehouse_policy.sellable_codes,
            date_from=args.history_start,
            date_to=args.date_to,
        )
        reserves = reconstruct_historical_reserves(
            onec_engine,
            codes=codes,
            date_from=args.history_start,
            date_to=args.date_to,
            tolerance=scenario_config.quantity_tolerance,
        )
        placements = reconstruct_historical_placements(
            onec_engine,
            codes=codes,
            date_from=args.history_start,
            date_to=args.date_to,
            tolerance=scenario_config.quantity_tolerance,
        )
        kmp4_raw, kmp4_counts = fetch_kmp4_demand(
            onec_engine,
            codes=codes,
            date_from=max(
                args.history_start,
                args.date_from - timedelta(days=scenario_config.kmp4_queue_days),
            ),
            date_to=args.date_to,
        )
        supplier_mapping = _load_document_line_mapping(
            DEFAULT_SUPPLIER_ORDER_MAPPING_JSON,
            error_code=SUPPLIER_ORDER_MAPPING_UNRESOLVED,
        )
        receipt_mapping = _load_document_line_mapping(
            DEFAULT_RECEIPT_MAPPING_JSON,
            error_code=RECEIPT_MAPPING_UNRESOLVED,
        )
        source_rows = fetch_display_supplier_lead_time_source_rows(
            onec_engine,
            folder=args.folder,
            history_start=args.history_start,
            as_of=args.date_to,
            supplier_mapping=supplier_mapping,
            receipt_mapping=receipt_mapping,
            limit=50000,
        )
        economics = fetch_daily_unit_economics(
            onec_engine,
            codes=codes,
            date_from=args.history_start,
            date_to=args.date_to,
        )
    finally:
        onec_engine.dispose()

    purchases, receipts = normalize_purchase_history(
        source_rows["supplier_order_rows"], source_rows["receipt_rows"]
    )
    lead_time_detail = build_lead_time_detail_rows(
        source_rows["supplier_order_rows"], source_rows["receipt_rows"]
    )
    incoming_by_day = build_historical_incoming_by_day(
        codes=codes,
        purchases=purchases,
        receipts=receipts,
        date_from=args.history_start,
        date_to=args.date_to,
    )
    launch_observations = build_launch_observations(
        items=items,
        sales_by_code=sales,
        availability_by_code=availability,
        receipt_history=receipts,
        history_start=args.history_start,
    )
    source_metadata = {
        "cohort_sku_count": len(codes),
        "sales_sku_count": len(sales),
        "kmp4_document_count": kmp4_counts["document_count"],
        "kmp4_line_count": kmp4_counts["line_count"],
        "kmp4_active_date_count": kmp4_counts["active_date_count"],
        "supplier_order_rows": len(source_rows["supplier_order_rows"]),
        "receipt_rows": len(source_rows["receipt_rows"]),
        "lead_time_detail_rows": len(lead_time_detail),
        "launch_observation_count": len(launch_observations),
        "economics_sku_count": len(economics),
        "reserve_opening_rows": reserves.source_counts["opening_rows"],
        "reserve_movement_rows": reserves.source_counts["movement_rows"],
        "placement_opening_rows": placements.source_counts["opening_rows"],
        "placement_movement_rows": placements.source_counts["movement_rows"],
        **{f"stock_{key}": value for key, value in stock_counts.items()},
    }
    tables = build_preflight_tables(
        items=items,
        sales_by_code=sales,
        availability_by_code=availability,
        stock_by_day=stock_by_day,
        reserves=reserves,
        placements=placements,
        incoming_by_day=incoming_by_day,
        kmp4_raw_by_code=kmp4_raw,
        purchases=purchases,
        receipts=receipts,
        launch_observations=launch_observations,
        lead_time_detail_rows=lead_time_detail,
        economics=economics,
        policy=policy,
        config=scenario_config,
        history_start=args.history_start,
        date_from=args.date_from,
        date_to=args.date_to,
        source_metadata=source_metadata,
        launch_profile_min_samples=args.launch_profile_min_samples,
    )
    manifest = write_preflight_artifacts(
        args.output_dir,
        tables=tables,
        date_from=args.date_from,
        date_to=args.date_to,
        history_start=args.history_start,
        config_path=args.scenario_config_json,
        cohort_run_id=run_id,
    )
    print(json.dumps(manifest, ensure_ascii=False))
    return 0 if tables.status == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
