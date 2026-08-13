"""Build and audit the display lifecycle v2 historical replay.

The task is read-only for 1C and the application database.  It may append a
new immutable local dataset/trajectory and write local analytical artifacts;
it never changes production stages or creates an order.
"""

from __future__ import annotations

import argparse
import csv
import inspect
import itertools
import json
import os
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from app.core.config import get_settings
from app.infrastructure.db.engines import build_engine
from app.services.assortment_lifecycle import (
    AssortmentStatus,
    decide_demand_state,
    decide_target_assortment_status,
)
from app.services.assortment_lifecycle_facts import (
    fetch_first_sale_dates,
    fetch_first_supplier_order_dates,
    fetch_historical_sales_observations,
    fetch_receipt_date_bounds,
)
from app.services.assortment_lifecycle_replay_store import (
    DEFAULT_REPLAY_STORE_PATH,
    AssortmentLifecycleReplayStore,
    stable_hash,
)
from app.services.assortment_lifecycle_v2_policy import (
    DEFAULT_ASSORTMENT_LIFECYCLE_V2_POLICY_PATH,
    DemandStatePolicy,
    load_assortment_lifecycle_v2_policy,
)
from app.services.assortment_lifecycle_v2_replay import (
    V2_REPLAY_MODEL_VERSION,
    HistoricalReceipt,
    HistoricalSupplierOrder,
    build_assortment_lifecycle_v2_trajectory,
    sales_observations_from_facts,
)
from tasks.build_display_auto_order_dry_run import load_warehouse_policy
from tasks.report_display_auto_order_six_month_backtest import (
    DEFAULT_HISTORY_START,
    LEGACY_REPLAY_MODEL_VERSION,
    PurchaseLine,
    ReceiptLine,
    _date,
    historical_replay_facts,
    load_backtest_items,
    load_or_build_historical_lifecycle_trajectory,
    normalize_purchase_history,
    reconstruct_historical_stock,
)
from tasks.report_display_supplier_lead_time_history import (
    DEFAULT_RECEIPT_MAPPING_JSON,
    DEFAULT_SUPPLIER_ORDER_MAPPING_JSON,
    RECEIPT_MAPPING_UNRESOLVED,
    SUPPLIER_ORDER_MAPPING_UNRESOLVED,
    _load_document_line_mapping,
    fetch_display_supplier_lead_time_source_rows,
)

ZERO = Decimal("0")
DEFAULT_PREFLIGHT_DIR = Path(
    "reports/assortment_lifecycle/backtest-2026-02-01_2026-07-31/"
    "next-stage-model-preflight-acceleration-v6"
)
DEFAULT_OUTPUT_DIR = Path(
    "reports/assortment_lifecycle/backtest-2026-02-01_2026-07-31/"
    "assortment-lifecycle-v2-historical-backtest"
)
DEFAULT_WAREHOUSE_POLICY_PATH = Path("config/assortment/display-warehouse-policy.json")


def build_v2_replay_facts(
    *,
    legacy_facts: Sequence[Mapping[str, Any]],
    sale_observations: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    facts = [dict(row) for row in legacy_facts]
    facts.extend(
        {
            "business_date": row["business_date"],
            "nomenclature_code": row["nomenclature_code"],
            "fact_type": "sale_observation",
            "payload": {
                "quantity": row["quantity"],
                "document_id": row["document_id"],
                "customer_id": row["customer_id"],
                "sales_point_id": row["sales_point_id"],
            },
        }
        for row in sale_observations
    )
    return facts


def replay_inputs_from_facts(facts: Sequence[Mapping[str, Any]]):
    items: list[dict[str, Any]] = []
    availability: dict[str, set[date]] = defaultdict(set)
    orders: dict[str, list[HistoricalSupplierOrder]] = defaultdict(list)
    receipts: dict[str, list[HistoricalReceipt]] = defaultdict(list)
    for fact in facts:
        code = _clean(fact.get("nomenclature_code"))
        business_date = _date(fact.get("business_date"))
        fact_type = _clean(fact.get("fact_type"))
        payload = fact.get("payload")
        if not code or business_date is None or not isinstance(payload, Mapping):
            continue
        if fact_type == "item":
            items.append(
                {
                    "nomenclature_code": code,
                    "name": _clean(payload.get("name")),
                    "source_record": dict(payload),
                }
            )
        elif fact_type == "available" and payload.get("available") is True:
            availability[code].add(business_date)
        elif fact_type == "supplier_order":
            orders[code].append(
                HistoricalSupplierOrder(
                    created_at=business_date,
                    cargo_handoff_at=_date(payload.get("cargo_handoff_at")),
                )
            )
        elif fact_type == "receipt":
            receipts[code].append(HistoricalReceipt(received_at=business_date))
    return items, sales_observations_from_facts(facts), availability, orders, receipts


def demand_policy_grid(policy) -> list[DemandStatePolicy]:
    grid = policy.backtest_grid
    return [
        replace(
            policy.demand,
            growth_multiplier=growth,
            confirmation_days=confirmation,
            max_single_day_share=share,
            min_independent_sales=independent,
        )
        for growth, confirmation, share, independent in itertools.product(
            grid.growth_multipliers,
            grid.confirmation_days,
            grid.max_single_day_shares,
            grid.min_independent_sales,
        )
    ]


def demand_policy_parameters(policy: DemandStatePolicy) -> dict[str, Any]:
    return {
        "growth_multiplier": str(policy.growth_multiplier),
        "confirmation_days": policy.confirmation_days,
        "max_single_day_share": str(policy.max_single_day_share),
        "min_independent_sales": policy.min_independent_sales,
        "confirmed_sales_qty_180": str(policy.confirmed_sales_qty_180),
        "decline_multiplier": str(policy.decline_multiplier),
        "decline_min_days_in_sale_90": str(policy.decline_min_days_in_sale_90),
    }


def v2_replay_policy_hash(policy: DemandStatePolicy) -> str:
    return stable_hash(
        {
            "model_version": V2_REPLAY_MODEL_VERSION,
            "demand_policy": demand_policy_parameters(policy),
            "decide_demand_state": inspect.getsource(decide_demand_state),
            "decide_target_assortment_status": inspect.getsource(decide_target_assortment_status),
            "build_trajectory": inspect.getsource(build_assortment_lifecycle_v2_trajectory),
        }
    )


def load_or_build_v2_trajectory(
    *,
    store: AssortmentLifecycleReplayStore,
    dataset_hash: str,
    facts: Sequence[Mapping[str, Any]],
    demand_policy: DemandStatePolicy,
    history_start: date,
    date_from: date,
    date_to: date,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    policy_hash = v2_replay_policy_hash(demand_policy)
    cached = store.find_trajectory(
        dataset_hash=dataset_hash,
        model_version=V2_REPLAY_MODEL_VERSION,
        policy_hash=policy_hash,
        period_from=date_from,
        period_to=date_to,
    )
    if cached is not None:
        rows = store.load_trajectory_rows(cached.trajectory_hash)
        return rows, {
            "policy_hash": policy_hash,
            "trajectory_hash": cached.trajectory_hash,
            "trajectory_reused": True,
            "row_count": len(rows),
        }
    items, sales, availability, orders, receipts = replay_inputs_from_facts(facts)
    rows = build_assortment_lifecycle_v2_trajectory(
        items=items,
        sales_observations_by_code=sales,
        availability_by_code=availability,
        supplier_orders_by_code=orders,
        receipts_by_code=receipts,
        history_start=history_start,
        date_from=date_from,
        date_to=date_to,
        demand_policy=demand_policy,
    )
    stored = store.put_trajectory(
        dataset_hash=dataset_hash,
        model_version=V2_REPLAY_MODEL_VERSION,
        policy_hash=policy_hash,
        period_from=date_from,
        period_to=date_to,
        rows=rows,
        metadata={
            "scope": "Дисплеи",
            "look_ahead_free": True,
            "sale_identifiers": "sha256_prefix_16",
            "production_action": "none_read_only",
            "demand_policy": demand_policy_parameters(demand_policy),
        },
    )
    return store.load_trajectory_rows(stored.key), {
        "policy_hash": policy_hash,
        "trajectory_hash": stored.key,
        "trajectory_reused": stored.reused,
        "row_count": len(rows),
    }


def build_stage_diff(
    legacy_rows: Sequence[Mapping[str, Any]],
    target_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    legacy = {
        (_clean(row.get("business_date")), _clean(row.get("nomenclature_code"))): row
        for row in legacy_rows
    }
    result: list[dict[str, Any]] = []
    for row in target_rows:
        key = (_clean(row.get("business_date")), _clean(row.get("nomenclature_code")))
        old = legacy.get(key, {})
        old_status = _clean(old.get("status"))
        new_status = _clean(row.get("status"))
        result.append(
            {
                "business_date": key[0],
                "nomenclature_code": key[1],
                "name": _clean(row.get("name") or old.get("name")),
                "old_status": old_status,
                "new_status": new_status,
                "changed": int(old_status != new_status),
                "demand_state": _clean(row.get("demand_state")),
                "old_reason_codes": ",".join(_text_values(old.get("reason_codes"))),
                "new_reason_codes": ",".join(_text_values(row.get("reason_codes"))),
                "demand_reason_codes": ",".join(_text_values(row.get("demand_reason_codes"))),
                "sales_30": row.get("sales_30"),
                "sales_90": row.get("sales_90"),
                "sales_180": row.get("sales_180"),
                "available_days_30": row.get("available_days_30"),
                "available_days_90": row.get("available_days_90"),
                "available_days_180": row.get("available_days_180"),
                "first_receipt_at": row.get("first_receipt_at"),
                "last_receipt_at": row.get("last_receipt_at"),
                "history_age_days": row.get("history_age_days"),
                "manual_review_required": int(bool(row.get("manual_review_required"))),
                "blockers": ",".join(_text_values(row.get("blockers"))),
                "exited_growing": int(
                    old_status == AssortmentStatus.SALE.value
                    and new_status != AssortmentStatus.SALE.value
                ),
            }
        )
    return result


def summarize_diff(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    latest_date = max((_clean(row.get("business_date")) for row in rows), default="")
    latest = [row for row in rows if _clean(row.get("business_date")) == latest_date]
    return {
        "daily_row_count": len(rows),
        "changed_daily_row_count": sum(int(row.get("changed") or 0) for row in rows),
        "latest_date": latest_date,
        "latest_sku_count": len(latest),
        "latest_changed_sku_count": sum(int(row.get("changed") or 0) for row in latest),
        "latest_exits_from_growing": sum(int(row.get("exited_growing") or 0) for row in latest),
        "latest_demand_states": dict(
            sorted(Counter(_clean(row.get("demand_state")) for row in latest).items())
        ),
        "latest_blocked_sku_count": sum(bool(_clean(row.get("blockers"))) for row in latest),
        "latest_manual_review_sku_count": sum(
            bool(int(row.get("manual_review_required") or 0)) for row in latest
        ),
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight-dir", type=Path, default=DEFAULT_PREFLIGHT_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--replay-store-path", type=Path, default=DEFAULT_REPLAY_STORE_PATH)
    parser.add_argument(
        "--policy-json", type=Path, default=DEFAULT_ASSORTMENT_LIFECYCLE_V2_POLICY_PATH
    )
    parser.add_argument("--warehouse-policy-json", type=Path, default=DEFAULT_WAREHOUSE_POLICY_PATH)
    parser.add_argument("--folder", default="Дисплеи")
    parser.add_argument("--database-url")
    parser.add_argument("--onec-database-url")
    parser.add_argument("--dataset-hash")
    parser.add_argument("--build-full-grid", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    policy = load_assortment_lifecycle_v2_policy(args.policy_json)
    manifest = json.loads((args.preflight_dir / "run-manifest.json").read_text(encoding="utf-8"))
    if manifest.get("preflight_status") != "PASS":
        raise SystemExit("v2 backtest requires a PASS frozen preflight")
    history_start = date.fromisoformat(manifest.get("history_start") or str(DEFAULT_HISTORY_START))
    date_from = policy.periods.training_from
    date_to = policy.periods.holdout_to
    store = AssortmentLifecycleReplayStore(args.replay_store_path)

    if args.dataset_hash:
        dataset_hash = args.dataset_hash
        facts = store.load_dataset_facts(dataset_hash)
        dataset_reused = True
        source = {"mode": "immutable_store_reuse"}
    else:
        settings = get_settings()
        app_url = args.database_url or os.environ.get("DATABASE_URL") or settings.database_url
        onec_url = (
            args.onec_database_url
            or os.environ.get("ONEC_DATABASE_URL", "")
            or settings.onec_database_url
            or ""
        )
        if not onec_url:
            raise SystemExit("ONEC_DATABASE_URL is not configured")
        app_engine = build_engine(app_url, pool_pre_ping=True)
        try:
            items, run_id = load_backtest_items(app_engine, folder=args.folder)
        finally:
            app_engine.dispose()
        if not items:
            raise SystemExit("display cohort is empty")
        codes = sorted(
            _clean(row.get("nomenclature_code"))
            for row in items
            if _clean(row.get("nomenclature_code"))
        )
        warehouse_payload = json.loads(args.warehouse_policy_json.read_text(encoding="utf-8-sig"))
        warehouse_policy = load_warehouse_policy(args.warehouse_policy_json)
        network_codes = sorted(
            {
                _clean(row.get("warehouse_code") or row.get("code"))
                for row in warehouse_payload["warehouses"]
                if _clean(row.get("warehouse_code") or row.get("code"))
                and not row.get("is_defect_warehouse")
                and not row.get("is_non_systematic_sale")
            }
        )
        onec_engine = build_engine(onec_url, pool_pre_ping=True)
        try:
            sale_rows = fetch_historical_sales_observations(
                onec_engine,
                nomenclature_codes=codes,
                date_from=history_start,
                date_to=date_to,
                warehouse_codes=warehouse_policy.sellable_codes,
            )
            _stock, availability, _stock_counts = reconstruct_historical_stock(
                onec_engine,
                codes=codes,
                network_warehouse_codes=network_codes,
                physical_warehouse_codes=warehouse_policy.sellable_codes,
                date_from=history_start,
                date_to=date_to,
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
                history_start=history_start,
                as_of=date_to,
                supplier_mapping=supplier_mapping,
                receipt_mapping=receipt_mapping,
                limit=50000,
            )
            refs_by_code = {
                _clean(row.get("nomenclature_code")): _clean(row.get("nomenclature_ref"))
                for row in source_rows["nomenclature_rows"]
                if _clean(row.get("nomenclature_code")) and _clean(row.get("nomenclature_ref"))
            }
            first_supplier_orders = fetch_first_supplier_order_dates(
                onec_engine,
                nomenclature_refs_by_code=refs_by_code,
                supplier_mapping=supplier_mapping,
            )
            receipt_bounds = fetch_receipt_date_bounds(
                onec_engine,
                nomenclature_codes=codes,
                receipt_mapping=receipt_mapping,
            )
            sale_bounds = fetch_first_sale_dates(
                onec_engine,
                nomenclature_codes=codes,
            )
        finally:
            onec_engine.dispose()
        for item in items:
            code = _clean(item.get("nomenclature_code"))
            raw_source = item.get("source_record")
            if isinstance(raw_source, str) and raw_source.strip():
                try:
                    source_record = json.loads(raw_source)
                except json.JSONDecodeError:
                    source_record = {}
            elif isinstance(raw_source, Mapping):
                source_record = dict(raw_source)
            else:
                source_record = {}
            if code in first_supplier_orders:
                source_record["first_supplier_order_at"] = first_supplier_orders[code].isoformat()
            if code in receipt_bounds:
                source_record["first_receipt_at"] = receipt_bounds[code][0].isoformat()
                source_record["last_receipt_at"] = receipt_bounds[code][1].isoformat()
            if code in sale_bounds:
                source_record["first_sale_at"] = sale_bounds[code][0].isoformat()
                source_record["last_sale_at"] = sale_bounds[code][1].isoformat()
            item["source_record"] = source_record
        purchases, receipts = normalize_purchase_history(
            source_rows["supplier_order_rows"], source_rows["receipt_rows"]
        )
        daily_sales: dict[str, dict[date, Decimal]] = defaultdict(dict)
        for row in sale_rows:
            code = _clean(row.get("nomenclature_code"))
            business_date = _date(row.get("business_date"))
            if code and business_date is not None:
                daily_sales[code][business_date] = daily_sales[code].get(
                    business_date, ZERO
                ) + Decimal(str(row["quantity"]))
        legacy_facts = historical_replay_facts(
            items=items,
            sales_by_code=daily_sales,
            availability_by_code=availability,
            purchase_history=purchases,
            receipt_history=receipts,
            item_default_date=history_start,
        )
        facts = build_v2_replay_facts(legacy_facts=legacy_facts, sale_observations=sale_rows)
        dataset = store.put_dataset(
            scope=args.folder,
            observation_from=min(
                _date(row["business_date"])
                for row in facts
                if _date(row["business_date"]) is not None
            ),
            observation_to=max(
                date_to,
                max(
                    _date(row["business_date"])
                    for row in facts
                    if _date(row["business_date"]) is not None
                ),
            ),
            facts=facts,
            source_manifest={
                "classification_run_id": run_id,
                "preflight_manifest": manifest.get("files"),
                "sale_identifiers": "sha256_prefix_16",
                "production_action": "none_read_only",
            },
        )
        dataset_hash = dataset.key
        dataset_reused = dataset.reused
        source = {
            "mode": "read_only_1c_refresh",
            "classification_run_id": run_id,
            "sale_observation_count": len(sale_rows),
        }

    items, sales, availability, orders, receipts = replay_inputs_from_facts(facts)
    purchases = {
        code: [
            PurchaseLine(
                created_at=row.created_at,
                qty=ZERO,
                price=ZERO,
                supplier_name="",
                order_ref="",
                expected_receipt_at=None,
                cargo_handoff_at=row.cargo_handoff_at,
            )
            for row in values
        ]
        for code, values in orders.items()
    }
    legacy_receipts = {
        code: [ReceiptLine(received_at=row.received_at, qty=ZERO) for row in values]
        for code, values in receipts.items()
    }
    daily_sales = {
        code: {
            business_date: sum(
                (row.quantity for row in values if row.business_date == business_date), ZERO
            )
            for business_date in {row.business_date for row in values}
        }
        for code, values in sales.items()
    }
    legacy_rows, legacy_store = load_or_build_historical_lifecycle_trajectory(
        store=store,
        items=items,
        sales_by_code=daily_sales,
        availability_by_code=availability,
        purchase_history=purchases,
        receipt_history=legacy_receipts,
        history_start=history_start,
        date_from=date_from,
        date_to=date_to,
        scope=args.folder,
        source_manifest={"derived_from_v2_dataset": dataset_hash},
    )

    policies = demand_policy_grid(policy) if args.build_full_grid else [policy.demand]
    trajectory_runs: list[dict[str, Any]] = []
    selected_rows: list[dict[str, Any]] = []
    for index, demand_policy in enumerate(policies):
        rows, run = load_or_build_v2_trajectory(
            store=store,
            dataset_hash=dataset_hash,
            facts=facts,
            demand_policy=demand_policy,
            history_start=history_start,
            date_from=date_from,
            date_to=date_to,
        )
        run["parameters"] = demand_policy_parameters(demand_policy)
        trajectory_runs.append(run)
        if index == 0:
            selected_rows = rows

    diff = build_stage_diff(legacy_rows, selected_rows)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(args.output_dir / "v2-lifecycle-history.csv", selected_rows)
    _write_csv(args.output_dir / "stage-diff.csv", diff)
    payload = {
        "schema": "display_assortment_lifecycle_v2_historical_backtest.v1",
        "status": "shadow_replay_complete_economic_train_pending",
        "scope": args.folder,
        "period_from": date_from.isoformat(),
        "period_to": date_to.isoformat(),
        "history_start": history_start.isoformat(),
        "dataset_hash": dataset_hash,
        "dataset_reused": dataset_reused,
        "fact_count": len(facts),
        "source": source,
        "legacy": {**legacy_store, "model_version": LEGACY_REPLAY_MODEL_VERSION},
        "v2_model_version": V2_REPLAY_MODEL_VERSION,
        "v2_trajectories": trajectory_runs,
        "diff": summarize_diff(diff),
        "train_holdout": {
            "training_period": {
                "from": policy.periods.training_from.isoformat(),
                "to": policy.periods.training_to.isoformat(),
            },
            "holdout_period": {
                "from": policy.periods.holdout_from.isoformat(),
                "to": policy.periods.holdout_to.isoformat(),
            },
            "status": (
                "stage_grid_replayed_economic_evaluation_pending"
                if args.build_full_grid
                else "shadow_policy_only_economic_evaluation_pending"
            ),
            "holdout_consumed": False,
        },
        "limitations": [
            "Product grouping attributes come from the frozen classification snapshot; dated sales, availability, supplier orders and receipts are walk-forward.",
            "Economic candidate scoring and the single July holdout are intentionally not claimed by this replay-only artifact.",
        ],
        "production_authorized": False,
        "production_action": "none_read_only",
    }
    (args.output_dir / "summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8-sig")
        return
    columns = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _text_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item for item in (part.strip() for part in value.split(",")) if item]
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [_clean(item) for item in value if _clean(item)]
    return []


def _clean(value: Any) -> str:
    return str(value or "").strip()


if __name__ == "__main__":
    raise SystemExit(main())
