from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from sqlalchemy import text

from app.infrastructure.db.engines import (
    build_onec_engine_from_settings,
    get_application_engine,
)
from app.services.assortment_lifecycle_facts import (
    validate_minimum_representation_policy,
    validate_warehouse_policy,
)
from app.services.display_demand_location_shadow import (
    build_display_demand_location_shadow,
    build_display_stock_location_shadow,
    fetch_display_demand_grain,
    fetch_display_stock_grain,
)

DEFAULT_WAREHOUSE_POLICY_PATH = Path("config/assortment/display-warehouse-policy.json")
DEFAULT_OUTPUT_PATH = Path("build/assortment/display-demand-location-shadow.json")
WINDOWS_DAYS = (30, 90, 180)
CLOSED_LYUBLINO_CODE = "РБ0000045"


def main() -> int:
    args = _parse_args()
    warehouse_payload = _load_json_object(args.warehouse_policy_json)
    warehouses = validate_warehouse_policy(warehouse_payload)
    minimum_policy = validate_minimum_representation_policy(warehouse_payload)
    active_store_codes = sorted(
        {
            str(row["warehouse_code"])
            for row in warehouses
            if row.get("role") == "physical_sales_point"
            and row.get("sells_systematically")
            and not row.get("is_central")
            and not row.get("is_defect_warehouse")
            and not row.get("is_transit")
            and not row.get("is_non_systematic_sale")
        }
    )
    usable_stock_codes = sorted({*active_store_codes, minimum_policy.central_warehouse_code})
    usable_quality_names = tuple(
        str(value).strip()
        for value in warehouse_payload.get("usable_stock_quality_names", [])
        if str(value).strip()
    )
    if not usable_quality_names:
        raise SystemExit("warehouse policy has no usable_stock_quality_names")

    if args.input_json:
        input_payload = _load_json_object(args.input_json)
        registry = dict(input_payload.get("registry") or {})
        display_codes = sorted(
            {
                str(value).strip()
                for value in input_payload.get("display_codes", [])
                if str(value).strip()
            }
        )
        sale_rows = _mapping_rows(input_payload.get("sale_rows"))
        return_rows = _mapping_rows(input_payload.get("return_rows"))
        stock_rows = _mapping_rows(input_payload.get("stock_rows"))
        reserve_rows = _mapping_rows(input_payload.get("reserve_rows"))
        source_mode = "frozen_input"
    else:
        registry, display_codes = _active_display_registry_scope()
        date_from = args.as_of - timedelta(days=max(WINDOWS_DAYS))
        onec_engine = build_onec_engine_from_settings()
        try:
            sale_rows, return_rows = fetch_display_demand_grain(
                onec_engine,
                product_codes=display_codes,
                date_from=date_from,
                date_to_exclusive=args.as_of + timedelta(days=1),
            )
            stock_rows, reserve_rows = fetch_display_stock_grain(
                onec_engine,
                product_codes=display_codes,
                warehouse_codes=usable_stock_codes,
                quality_names=usable_quality_names,
            )
        finally:
            onec_engine.dispose()
        source_mode = "live_read_only"

    demand = build_display_demand_location_shadow(
        sale_rows,
        return_rows,
        date_to=args.as_of,
        windows_days=WINDOWS_DAYS,
    )
    stock = build_display_stock_location_shadow(stock_rows, reserve_rows)
    active_store_count = len(active_store_codes)
    minimum_representation_qty = active_store_count + minimum_policy.central_reserve_qty
    largest_window = str(max(WINDOWS_DAYS))
    demand_totals = demand["totals_by_window"][largest_window]
    stock_totals = stock["network"]
    quality = demand["quality"]
    gates = {
        "active_physical_store_count_is_11": active_store_count == 11,
        "closed_lyublino_excluded": CLOSED_LYUBLINO_CODE not in active_store_codes,
        "central_reserve_qty_is_2": minimum_policy.central_reserve_qty == 2,
        "minimum_representation_qty_is_13": minimum_representation_qty == 13,
        "sale_canonical_grain_unique": quality["sale_duplicate_canonical_key_count"] == 0,
        "return_causal_grain_unique": quality["return_duplicate_causal_key_count"] == 0,
        "return_source_sale_ref_complete": quality["return_missing_source_sale_ref_row_count"] == 0,
    }
    comparison = {
        "window_days": int(largest_window),
        "current_gross_demand_qty": demand_totals["gross_sale_qty"],
        "candidate_net_fulfilled_demand_qty": demand_totals["net_fulfilled_sale_qty"],
        "return_qty": demand_totals["return_qty"],
        "candidate_minus_current_demand_qty": demand_totals["net_fulfilled_sale_qty"]
        - demand_totals["gross_sale_qty"],
        "naive_network_free_qty": stock_totals["naive_net_qty"],
        "point_safe_network_free_qty": stock_totals["point_safe_free_qty"],
        "point_safe_minus_naive_free_qty": stock_totals["point_safe_free_qty"]
        - stock_totals["naive_net_qty"],
        "order_quantity_change_authorized": False,
        "next_decision": (
            "После проверки shadow бизнес выбирает источник скорости: gross_sale, "
            "net_fulfilled_sale или ограниченную комбинацию."
        ),
    }
    generated_at = datetime.now(UTC)
    payload: dict[str, Any] = {
        "schema": "display_demand_location_frozen_comparison.v1",
        "status": "ready" if all(gates.values()) else "blocked",
        "generated_at": generated_at,
        "as_of": args.as_of,
        "source_mode": source_mode,
        "source_identity": {
            "active_display_registry": registry,
            "display_code_count": len(display_codes),
            "display_code_sha256": _sha256_text("\n".join(display_codes)),
        },
        "policy": {
            "version": minimum_policy.version,
            "active_physical_store_codes": active_store_codes,
            "active_physical_store_count": active_store_count,
            "central_warehouse_code": minimum_policy.central_warehouse_code,
            "central_reserve_qty": minimum_policy.central_reserve_qty,
            "minimum_representation_qty": minimum_representation_qty,
            "usable_stock_warehouse_codes": usable_stock_codes,
            "usable_stock_quality_names": list(usable_quality_names),
        },
        "gates": gates,
        "comparison": comparison,
        "demand": demand,
        "stock": stock,
        "safety": {
            "read_only": True,
            "writes_to_onec": False,
            "writes_to_application_db": False,
            "creates_orders": False,
            "changes_statuses": False,
            "changes_production_formula": False,
        },
    }
    payload["artifact_sha256"] = _sha256_json(payload)
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default) + "\n",
        encoding="utf-8",
    )
    output = {
        "status": payload["status"],
        "output_json": str(args.output_json),
        "artifact_sha256": payload["artifact_sha256"],
        "comparison": comparison,
        "gates": gates,
    }
    print(
        json.dumps(
            output,
            ensure_ascii=False,
            indent=None if args.json else 2,
            default=_json_default,
        )
    )
    return 0 if payload["status"] == "ready" else 2


def _active_display_registry_scope() -> tuple[dict[str, Any], list[str]]:
    engine = get_application_engine()
    try:
        with engine.connect() as connection:
            version = dict(connection.execute(text("""
                        SELECT id, version_number, status, effective_from,
                               actual_family_count, actual_member_count,
                               inventory_checksum, membership_checksum
                        FROM display_family_registry_version
                        WHERE status = 'active'
                        """)).mappings().one())
            rows = connection.execute(
                text("""
                    SELECT p.code_1c AS product_code
                    FROM display_family_member AS member
                    JOIN product AS p ON p.id = member.product_id
                    WHERE member.registry_version_id = :version_id
                      AND p.code_1c IS NOT NULL
                      AND btrim(p.code_1c) <> ''
                    ORDER BY p.code_1c
                    """),
                {"version_id": version["id"]},
            ).mappings()
            codes = [str(row["product_code"]).strip() for row in rows]
    finally:
        engine.dispose()
    if len(codes) != len(set(codes)):
        raise SystemExit("active display registry contains duplicate product codes")
    version.pop("id", None)
    return version, codes


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a read-only display demand/location frozen comparison."
    )
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    parser.add_argument(
        "--warehouse-policy-json",
        type=Path,
        default=DEFAULT_WAREHOUSE_POLICY_PATH,
    )
    parser.add_argument("--input-json", type=Path)
    parser.add_argument("--output-json", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def _load_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, Mapping):
        raise SystemExit(f"JSON object required: {path}")
    return dict(payload)


def _mapping_rows(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise SystemExit("shadow input rows must be arrays")
    if not all(isinstance(row, Mapping) for row in value):
        raise SystemExit("shadow input row must be an object")
    return [dict(row) for row in value]


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_json(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )
    return _sha256_text(canonical)


def _json_default(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


if __name__ == "__main__":
    raise SystemExit(main())
