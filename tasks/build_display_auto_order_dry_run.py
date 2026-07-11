from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import bindparam, create_engine, func, select, text

from app.core.config import get_settings
from app.services.assortment_lifecycle_classification_store import (
    ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE,
)
from app.services.procurement_b2b_customer_demand import (
    B2BSkuDemandProfile,
    load_b2b_customer_demand_profiles,
)

DEFAULT_OUTPUT_CSV = (
    Path("reports/assortment_lifecycle")
    / date.today().isoformat()
    / "display-auto-order-dry-run.csv"
)
DEFAULT_OUTPUT_JSON = (
    Path("reports/assortment_lifecycle")
    / date.today().isoformat()
    / "display-auto-order-dry-run-summary.json"
)
DEFAULT_POLICY_JSON = Path("config/assortment/display-auto-order-policy.json")
ONEC_EMPTY_DATE = date(1753, 1, 1)
OPEN_SUPPLIER_ORDER_BALANCE_PERIOD = datetime.fromisoformat("3999-11-01T00:00:00")
MIN_PURCHASE_PRICE_FOR_ANALOG_SCORE = Decimal("5")
SUPPORTED_VARIANT_COLOR_RE = re.compile(
    r"(?:черн|бел|син|красн|зелен|зелён|сер|розов|фиолетов|голуб|золот|"
    r"black|white|blue|red|green|grey|gray|pink|purple|gold|silver)",
    re.IGNORECASE,
)

CSV_COLUMNS = [
    "nomenclature_code",
    "name",
    "status_label",
    "quality_raw",
    "quality_normalized",
    "price_segment",
    "latest_purchase_price",
    "latest_purchase_price_at",
    "analog_group_id",
    "analog_group_size",
    "analog_role",
    "preferred_replacement_code",
    "preferred_replacement_name",
    "analog_score",
    "analog_winner_score",
    "analog_group_net_sales_qty",
    "analog_group_free_stock_qty",
    "analog_group_incoming_qty",
    "analog_group_target_stock_qty",
    "analog_group_recommended_order_qty_raw",
    "analog_group_recommended_order_qty",
    "analog_model_tokens",
    "analog_decision_reason_ru",
    "supported_analog_min_stock_qty",
    "supported_analog_floor_need_qty",
    "supported_analog_rule_applied",
    "sellable_stock_qty",
    "reserved_qty",
    "free_stock_qty",
    "central_stock_qty",
    "total_stock_qty",
    "incoming_qty",
    "incoming_order_count",
    "sales_qty_window",
    "return_qty_window",
    "net_sales_qty_window",
    "sales_doc_count",
    "sales_warehouse_count",
    "last_sale_at",
    "base_avg_daily_sales_qty",
    "avg_daily_sales_qty",
    "speed_tier",
    "speed_group_avg_daily_sales_qty",
    "speed_max_effective_target_days",
    "speed_rule_safety_stock_days",
    "speed_rule_action",
    "adjusted_net_sales_qty_window",
    "demand_adjustment_rule_id",
    "demand_adjustment_multiplier",
    "demand_adjustment_reason_ru",
    "b2b_profile_as_of_exclusive",
    "b2b_profile_age_days",
    "b2b_demand_mode",
    "b2b_dependency_class",
    "b2b_active_customer_count",
    "b2b_passive_customer_count",
    "b2b_due_customer_count",
    "b2b_managed_sales_qty_window",
    "b2b_active_daily_rate",
    "b2b_client_forecast_qty",
    "b2b_ordinary_net_sales_qty_window",
    "b2b_replacement_target_stock_qty",
    "b2b_replacement_decision",
    "b2b_replacement_recommended_order_qty",
    "b2b_order_delta_qty",
    "b2b_reason_ru",
    "target_days",
    "order_cadence_days",
    "supplier_prepare_days",
    "supplier_assembly_days",
    "logistics_days",
    "delivery_days",
    "supplier_delay_buffer_days",
    "receiving_buffer_days",
    "safety_stock_days",
    "lead_time_days",
    "effective_target_days",
    "forecast_qty",
    "safety_stock_qty",
    "min_display_qty",
    "min_order_qty",
    "order_rounding_rule",
    "order_rounding_multiple",
    "price_batch_min_qty",
    "price_batch_excess_qty",
    "price_batch_excess_coverage_days",
    "price_batch_decision",
    "target_stock_qty",
    "recommended_order_qty_raw",
    "recommended_order_qty",
    "latest_expected_receipt_at",
    "incoming_latest_arrival_days",
    "pipeline_arriving_10_days_qty",
    "pipeline_arriving_20_days_qty",
    "pipeline_later_qty",
    "pipeline_no_date_qty",
    "pipeline_cargo_handoff_qty",
    "pipeline_supplier_processing_qty",
    "dry_run_decision",
    "reason_ru",
    "blockers",
    "warnings",
    "data_sources",
]


@dataclass(frozen=True)
class WarehousePolicy:
    usable_stock_quality_names: tuple[str, ...]
    sellable_codes: tuple[str, ...]
    central_codes: tuple[str, ...]
    defect_codes: tuple[str, ...]
    transit_codes: tuple[str, ...]
    non_systematic_codes: tuple[str, ...]

    @property
    def usable_codes(self) -> tuple[str, ...]:
        blocked = {*self.defect_codes, *self.transit_codes}
        return tuple(
            code for code in (*self.sellable_codes, *self.central_codes) if code not in blocked
        )


@dataclass(frozen=True)
class DemandUpliftRule:
    rule_id: str
    match_any_analog_model_tokens: tuple[str, ...]
    demand_multiplier: Decimal
    reason_ru: str = ""

    def validate(self) -> None:
        if not self.rule_id:
            raise SystemExit("demand uplift rule must have rule_id")
        if not self.match_any_analog_model_tokens:
            raise SystemExit(f"demand uplift rule {self.rule_id!r} must have model tokens")
        if self.demand_multiplier < Decimal("1"):
            raise SystemExit(
                f"demand uplift rule {self.rule_id!r} multiplier must be greater than or equal to 1"
            )


@dataclass(frozen=True)
class OrderRoundingRule:
    threshold_gt: Decimal
    round_to: int

    def validate(self) -> None:
        if self.threshold_gt < 0:
            raise SystemExit("order rounding threshold must be non-negative")
        if self.round_to <= 0:
            raise SystemExit("order rounding round_to must be positive")


@dataclass(frozen=True)
class SpeedHorizonRule:
    tier: str
    min_group_avg_daily_sales_qty: Decimal
    max_effective_target_days: int = 0
    safety_stock_days: int = 0
    review_only: bool = False
    label_ru: str = ""

    def validate(self) -> None:
        if not self.tier:
            raise SystemExit("speed horizon rule must have tier")
        if self.min_group_avg_daily_sales_qty < 0:
            raise SystemExit(
                "speed horizon rule min_group_avg_daily_sales_qty must be non-negative"
            )
        if self.review_only:
            return
        if self.max_effective_target_days <= 0:
            raise SystemExit("speed horizon rule max_effective_target_days must be positive")
        if self.safety_stock_days < 0:
            raise SystemExit("speed horizon rule safety_stock_days must be non-negative")
        if self.safety_stock_days > self.max_effective_target_days:
            raise SystemExit(
                "speed horizon rule safety_stock_days must be less than or equal to "
                "max_effective_target_days"
            )


@dataclass(frozen=True)
class PriceBatchRule:
    speed_tier: str
    price_segments: tuple[str, ...]
    minimum_batch_qty: int | None = None
    max_automatic_excess_coverage_days: int | None = None
    rounding_mode: str = ""

    def validate(self) -> None:
        if not self.speed_tier:
            raise SystemExit("price batch rule must have speed_tier")
        if not self.price_segments:
            raise SystemExit("price batch rule must have price_segments")
        if self.minimum_batch_qty is not None and self.minimum_batch_qty <= 0:
            raise SystemExit("price batch minimum_batch_qty must be positive")
        if (
            self.max_automatic_excess_coverage_days is not None
            and self.max_automatic_excess_coverage_days < 0
        ):
            raise SystemExit("price batch max_automatic_excess_coverage_days must be non-negative")


@dataclass(frozen=True)
class SupportedAnalogPolicy:
    enabled: bool = False
    applies_to_statuses: tuple[str, ...] = ()
    active_store_count: int = 0
    site_reserve_qty: int = 0
    min_network_stock_qty: int = 0
    min_recent_sales_pct_of_store_count: Decimal = Decimal("0")
    max_days_since_last_sale: int = 0

    @property
    def min_recent_sales_qty(self) -> Decimal:
        if self.active_store_count <= 0:
            return Decimal("0")
        return _ceil_decimal(
            Decimal(str(self.active_store_count))
            * self.min_recent_sales_pct_of_store_count
            / Decimal("100")
        )

    def validate(self) -> None:
        if not self.enabled:
            return
        if not self.applies_to_statuses:
            raise SystemExit("supported analog policy must have applies_to_statuses")
        if self.active_store_count <= 0:
            raise SystemExit("supported analog active_store_count must be positive")
        if self.site_reserve_qty < 0:
            raise SystemExit("supported analog site_reserve_qty must be non-negative")
        if self.min_network_stock_qty <= 0:
            raise SystemExit("supported analog min_network_stock_qty must be positive")
        if self.min_recent_sales_pct_of_store_count < 0:
            raise SystemExit(
                "supported analog min_recent_sales_pct_of_store_count must be non-negative"
            )
        if self.max_days_since_last_sale <= 0:
            raise SystemExit("supported analog max_days_since_last_sale must be positive")


@dataclass(frozen=True)
class AutoOrderPolicy:
    sales_window_days: int = 180
    target_days: int = 14
    order_cadence_days: int = 0
    supplier_prepare_days: int = 0
    logistics_days: int = 0
    supplier_delay_buffer_days: int = 0
    receiving_buffer_days: int = 0
    safety_stock_days: int = 0
    min_display_qty: int = 0
    min_order_qty: int = 1
    max_order_qty: int | None = None
    include_sale_review_candidates: bool = False
    order_rounding_rules: tuple[OrderRoundingRule, ...] = ()
    speed_horizon_rules: tuple[SpeedHorizonRule, ...] = ()
    onec_catalog_analog_candidate_model_tokens: tuple[str, ...] = ()
    demand_uplift_rules: tuple[DemandUpliftRule, ...] = ()
    price_batch_rules: tuple[PriceBatchRule, ...] = ()
    price_batch_applies_to_statuses: tuple[str, ...] = ()
    price_batch_applies_to_analog_roles: tuple[str, ...] = ()
    supported_analog_policy: SupportedAnalogPolicy = SupportedAnalogPolicy()

    @property
    def lead_time_days(self) -> int:
        return self.supplier_prepare_days + self.logistics_days

    @property
    def planning_horizon_days(self) -> int:
        return (
            self.target_days
            + self.order_cadence_days
            + self.lead_time_days
            + self.supplier_delay_buffer_days
            + self.receiving_buffer_days
        )

    @property
    def effective_target_days(self) -> int:
        return self.planning_horizon_days + self.safety_stock_days

    def with_overrides(self, **overrides: int | None) -> AutoOrderPolicy:
        values = {
            "sales_window_days": self.sales_window_days,
            "target_days": self.target_days,
            "order_cadence_days": self.order_cadence_days,
            "supplier_prepare_days": self.supplier_prepare_days,
            "logistics_days": self.logistics_days,
            "supplier_delay_buffer_days": self.supplier_delay_buffer_days,
            "receiving_buffer_days": self.receiving_buffer_days,
            "safety_stock_days": self.safety_stock_days,
            "min_display_qty": self.min_display_qty,
            "min_order_qty": self.min_order_qty,
            "max_order_qty": self.max_order_qty,
            "include_sale_review_candidates": self.include_sale_review_candidates,
            "order_rounding_rules": self.order_rounding_rules,
            "speed_horizon_rules": self.speed_horizon_rules,
            "onec_catalog_analog_candidate_model_tokens": (
                self.onec_catalog_analog_candidate_model_tokens
            ),
            "demand_uplift_rules": self.demand_uplift_rules,
            "price_batch_rules": self.price_batch_rules,
            "price_batch_applies_to_statuses": self.price_batch_applies_to_statuses,
            "price_batch_applies_to_analog_roles": self.price_batch_applies_to_analog_roles,
            "supported_analog_policy": self.supported_analog_policy,
        }
        values.update({key: value for key, value in overrides.items() if value is not None})
        return AutoOrderPolicy(**values)

    def validate(self) -> None:
        if self.sales_window_days <= 0:
            raise SystemExit("sales_window_days must be positive")
        if self.target_days <= 0:
            raise SystemExit("target_days must be positive")
        if self.min_order_qty <= 0:
            raise SystemExit("min_order_qty must be positive")
        if self.max_order_qty is not None and self.max_order_qty <= 0:
            raise SystemExit("max_order_qty must be positive or null")
        if self.max_order_qty is not None and self.min_order_qty > self.max_order_qty:
            raise SystemExit("min_order_qty must be less than or equal to max_order_qty")
        for rule in self.demand_uplift_rules:
            rule.validate()
        for rule in self.order_rounding_rules:
            rule.validate()
        for rule in self.speed_horizon_rules:
            rule.validate()
        for rule in self.price_batch_rules:
            rule.validate()
        self.supported_analog_policy.validate()
        for field_name in [
            "order_cadence_days",
            "supplier_prepare_days",
            "logistics_days",
            "supplier_delay_buffer_days",
            "receiving_buffer_days",
            "safety_stock_days",
            "min_display_qty",
        ]:
            if getattr(self, field_name) < 0:
                raise SystemExit(f"{field_name} must be non-negative")


def main() -> int:
    args = _parse_args()
    auto_order_policy = load_auto_order_policy(args.auto_order_policy_json).with_overrides(
        sales_window_days=args.sales_window_days,
        target_days=args.target_days,
        order_cadence_days=args.order_cadence_days,
        supplier_prepare_days=args.supplier_prepare_days,
        logistics_days=args.logistics_days,
        supplier_delay_buffer_days=args.supplier_delay_buffer_days,
        receiving_buffer_days=args.receiving_buffer_days,
        safety_stock_days=args.safety_stock_days,
        min_display_qty=args.min_display_qty,
        min_order_qty=args.min_order_qty,
        max_order_qty=args.max_order_qty,
    )
    auto_order_policy.validate()
    settings = get_settings()
    database_url = args.database_url or os.environ.get("DATABASE_URL") or settings.database_url
    onec_database_url = (
        args.onec_database_url
        or os.environ.get("ONEC_DATABASE_URL", "")
        or settings.onec_database_url
        or ""
    )
    app_engine = create_engine(database_url, pool_pre_ping=True)
    try:
        items, run_id = load_auto_order_items(
            app_engine,
            folder=args.folder,
            include_sale_review_candidates=(
                args.include_sale_review_candidates
                or auto_order_policy.include_sale_review_candidates
            ),
        )
    finally:
        app_engine.dispose()

    policy = load_warehouse_policy(args.warehouse_policy_json)
    source_errors: dict[str, str] = {}
    facts = {
        "stock": {},
        "reserve": {},
        "incoming": {},
        "sales": {},
        "returns": {},
        "purchase": {},
    }
    b2b_customer_demand_profiles: dict[str, B2BSkuDemandProfile] = {}
    b2b_customer_demand_error = ""
    if args.b2b_customer_demand_csv:
        try:
            b2b_customer_demand_profiles = load_b2b_customer_demand_profiles(
                args.b2b_customer_demand_csv,
                profile_as_of_exclusive=args.b2b_customer_demand_as_of,
            )
        except Exception as exc:  # noqa: BLE001 - optional advisory must not block base dry-run.
            b2b_customer_demand_error = f"{type(exc).__name__}: {exc}"
    if onec_database_url:
        onec_engine = create_engine(onec_database_url, pool_pre_ping=True)
        try:
            scoped_candidate_tokens = auto_order_policy.onec_catalog_analog_candidate_model_tokens
            include_catalog_analog_candidates = args.include_onec_catalog_analog_candidates or bool(
                scoped_candidate_tokens
            )
            if include_catalog_analog_candidates:
                items.extend(
                    fetch_onec_catalog_analog_candidates(
                        onec_engine,
                        base_items=items,
                        include_model_tokens=(
                            ()
                            if args.include_onec_catalog_analog_candidates
                            else scoped_candidate_tokens
                        ),
                    )
                )
            codes = tuple(str(item["nomenclature_code"]) for item in items)
            facts["stock"] = fetch_stock_totals(onec_engine, codes=codes, policy=policy)
            facts["reserve"] = fetch_reserved_totals(onec_engine, codes=codes, policy=policy)
            facts["incoming"] = fetch_incoming_totals(
                onec_engine,
                codes=codes,
                as_of=args.as_of,
            )
            facts["sales"] = fetch_sales_totals(
                onec_engine,
                codes=codes,
                sellable_codes=policy.sellable_codes,
                date_from=args.as_of - timedelta(days=auto_order_policy.sales_window_days),
                date_to=args.as_of + timedelta(days=1),
            )
            facts["returns"] = fetch_return_totals(
                onec_engine,
                codes=codes,
                sellable_codes=policy.sellable_codes,
                date_from=args.as_of - timedelta(days=auto_order_policy.sales_window_days),
                date_to=args.as_of + timedelta(days=1),
            )
            facts["purchase"] = fetch_latest_purchase_prices(onec_engine, codes=codes)
        except (
            Exception
        ) as exc:  # noqa: BLE001 - report must stay read-only and explain source gaps.
            source_errors["onec"] = f"{type(exc).__name__}: {exc}"
        finally:
            onec_engine.dispose()
    else:
        source_errors["onec"] = "ONEC_DATABASE_URL is not configured"

    rows = build_dry_run_rows(
        items,
        facts=facts,
        source_errors=source_errors,
        target_days=auto_order_policy.target_days,
        order_cadence_days=auto_order_policy.order_cadence_days,
        supplier_prepare_days=auto_order_policy.supplier_prepare_days,
        logistics_days=auto_order_policy.logistics_days,
        supplier_delay_buffer_days=auto_order_policy.supplier_delay_buffer_days,
        receiving_buffer_days=auto_order_policy.receiving_buffer_days,
        safety_stock_days=auto_order_policy.safety_stock_days,
        min_display_qty=auto_order_policy.min_display_qty,
        min_order_qty=auto_order_policy.min_order_qty,
        sales_window_days=auto_order_policy.sales_window_days,
        max_order_qty=auto_order_policy.max_order_qty,
        order_rounding_rules=auto_order_policy.order_rounding_rules,
        speed_horizon_rules=auto_order_policy.speed_horizon_rules,
        demand_uplift_rules=auto_order_policy.demand_uplift_rules,
        price_batch_rules=auto_order_policy.price_batch_rules,
        price_batch_applies_to_statuses=auto_order_policy.price_batch_applies_to_statuses,
        price_batch_applies_to_analog_roles=(auto_order_policy.price_batch_applies_to_analog_roles),
        supported_analog_policy=auto_order_policy.supported_analog_policy,
        b2b_customer_demand_profiles=b2b_customer_demand_profiles,
        b2b_customer_demand_error=b2b_customer_demand_error,
        as_of=args.as_of,
    )
    write_csv(args.output_csv, rows)
    summary = build_summary(rows, run_id=run_id, source_errors=source_errors)
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    payload = {"status": "ready", "output_csv": str(args.output_csv), **summary}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def load_auto_order_items(
    engine,
    *,
    folder: str,
    include_sale_review_candidates: bool = False,
) -> tuple[list[dict[str, Any]], int | None]:
    table = ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE
    with engine.connect() as conn:
        run_id = conn.execute(
            select(func.max(table.c.last_run_id)).where(table.c.folder.ilike(f"%{folder}%"))
        ).scalar()
        conditions = [
            table.c.folder.ilike(f"%{folder}%"),
            table.c.last_run_id == run_id,
            table.c.future_ka_mapping_status == "ready",
            table.c.demand_method_code == "available_days_average",
        ]
        if include_sale_review_candidates:
            conditions.extend(
                [
                    table.c.status.in_(("working", "sale")),
                    table.c.manual_review_required.is_(False),
                ]
            )
        else:
            conditions.extend(
                [
                    table.c.auto_order_allowed.is_(True),
                    table.c.status == "working",
                ]
            )
        rows = (
            conn.execute(select(table).where(*conditions).order_by(table.c.nomenclature_code.asc()))
            .mappings()
            .all()
        )
    return [dict(row) for row in rows], run_id


def load_warehouse_policy(path: Path) -> WarehousePolicy:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    usable_stock_quality_names = _string_tuple(payload.get("usable_stock_quality_names")) or (
        "Новый",
    )
    raw_warehouses = payload.get("warehouses")
    if not isinstance(raw_warehouses, list):
        raise SystemExit("warehouse policy must contain warehouses list")
    sellable: list[str] = []
    central: list[str] = []
    defect: list[str] = []
    transit: list[str] = []
    non_systematic: list[str] = []
    for raw in raw_warehouses:
        if not isinstance(raw, Mapping):
            continue
        code = _clean(raw.get("warehouse_code") or raw.get("code"))
        if not code:
            continue
        if _bool(raw.get("sells_systematically")):
            sellable.append(code)
        if _bool(raw.get("is_central")):
            central.append(code)
        if _bool(raw.get("is_defect_warehouse")):
            defect.append(code)
        if _bool(raw.get("is_transit")):
            transit.append(code)
        if _bool(raw.get("is_non_systematic_sale")):
            non_systematic.append(code)
    if not sellable:
        raise SystemExit("warehouse policy has no sellable warehouses")
    return WarehousePolicy(
        usable_stock_quality_names=usable_stock_quality_names,
        sellable_codes=tuple(sellable),
        central_codes=tuple(central),
        defect_codes=tuple(defect),
        transit_codes=tuple(transit),
        non_systematic_codes=tuple(non_systematic),
    )


def load_auto_order_policy(path: Path) -> AutoOrderPolicy:
    if not path.exists():
        return AutoOrderPolicy()
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, Mapping):
        return AutoOrderPolicy()
    price_policy = (
        payload.get("price_segment_schedule_policy")
        if isinstance(payload.get("price_segment_schedule_policy"), Mapping)
        else {}
    )
    supported_analog_raw = (
        payload.get("supported_analog_policy")
        if isinstance(payload.get("supported_analog_policy"), Mapping)
        else {}
    )
    raw = (
        payload.get("auto_order_policy")
        if isinstance(payload.get("auto_order_policy"), Mapping)
        else payload
    )
    if not isinstance(raw, Mapping):
        return AutoOrderPolicy()
    return AutoOrderPolicy(
        sales_window_days=_int_value(raw.get("sales_window_days"), 180),
        target_days=_int_value(raw.get("target_days"), 14),
        order_cadence_days=_int_value(raw.get("order_cadence_days"), 0),
        supplier_prepare_days=_int_value(
            raw.get("supplier_prepare_days") or raw.get("supplier_assembly_days"),
            0,
        ),
        logistics_days=_int_value(raw.get("logistics_days") or raw.get("delivery_days"), 0),
        supplier_delay_buffer_days=_int_value(raw.get("supplier_delay_buffer_days"), 0),
        receiving_buffer_days=_int_value(raw.get("receiving_buffer_days"), 0),
        safety_stock_days=_int_value(raw.get("safety_stock_days"), 0),
        min_display_qty=_int_value(raw.get("min_display_qty"), 0),
        min_order_qty=_int_value(raw.get("min_order_qty"), 1),
        max_order_qty=_optional_int_value(raw.get("max_order_qty")),
        include_sale_review_candidates=_bool(raw.get("include_sale_review_candidates")),
        order_rounding_rules=_order_rounding_rules(raw.get("order_rounding_rules")),
        speed_horizon_rules=_speed_horizon_rules(raw.get("speed_horizon_rules")),
        onec_catalog_analog_candidate_model_tokens=_string_tuple(
            raw.get("onec_catalog_analog_candidate_model_tokens")
        ),
        demand_uplift_rules=_demand_uplift_rules(raw.get("demand_uplift_rules")),
        price_batch_rules=(
            _price_batch_rules(price_policy.get("rules"))
            if _clean(price_policy.get("status")).casefold() == "approved"
            else ()
        ),
        price_batch_applies_to_statuses=_string_tuple(price_policy.get("applies_to_statuses")),
        price_batch_applies_to_analog_roles=_string_tuple(
            price_policy.get("applies_to_analog_roles")
        ),
        supported_analog_policy=_supported_analog_policy(supported_analog_raw),
    )


def fetch_stock_totals(
    engine, *, codes: Sequence[str], policy: WarehousePolicy
) -> dict[str, dict[str, Any]]:
    if not codes:
        return {}
    sql = _expanding_text(
        """
        SELECT
            NULLIF(LTRIM(RTRIM(product._Code)), N'') AS code,
            SUM(CASE WHEN NULLIF(LTRIM(RTRIM(quality._Description)), N'')
                    IN :usable_stock_quality_names
                THEN CAST(stock._Fld7743 AS decimal(18, 3)) ELSE 0 END) AS sellable_stock_qty,
            SUM(CASE WHEN NULLIF(LTRIM(RTRIM(quality._Description)), N'')
                    IN :usable_stock_quality_names
                    AND NULLIF(LTRIM(RTRIM(warehouse._Code)), N'') IN :central_codes
                THEN CAST(stock._Fld7743 AS decimal(18, 3)) ELSE 0 END) AS central_stock_qty,
            SUM(CAST(stock._Fld7743 AS decimal(18, 3))) AS total_stock_qty
        FROM dbo._AccumRgT7745 AS stock WITH (NOLOCK)
        JOIN dbo._Reference62 AS product WITH (NOLOCK)
            ON product._IDRRef = stock._Fld7738RRef
        JOIN dbo._Reference48 AS quality WITH (NOLOCK)
            ON quality._IDRRef = stock._Fld7741RRef
        JOIN dbo._Reference80 AS warehouse WITH (NOLOCK)
            ON warehouse._IDRRef = stock._Fld7742RRef
        WHERE stock._Fld7743 <> 0
          AND stock._Period = :balance_period
          AND NULLIF(LTRIM(RTRIM(product._Code)), N'') IN :codes
        GROUP BY NULLIF(LTRIM(RTRIM(product._Code)), N'')
        """,
        codes=codes,
        usable_stock_quality_names=policy.usable_stock_quality_names,
        central_codes=policy.central_codes or ("__none__",),
    ).bindparams(bindparam("balance_period", value=OPEN_SUPPLIER_ORDER_BALANCE_PERIOD))
    with engine.connect() as conn:
        return {_clean(row["code"]): dict(row) for row in conn.execute(sql).mappings()}


def fetch_reserved_totals(
    engine, *, codes: Sequence[str], policy: WarehousePolicy
) -> dict[str, dict[str, Any]]:
    if not codes:
        return {}
    sql = _expanding_text(
        """
        SELECT
            NULLIF(LTRIM(RTRIM(product._Code)), N'') AS code,
            SUM(CAST(reserve._Fld7659 AS decimal(18, 3))) AS reserved_qty
        FROM dbo._AccumRgT7662 AS reserve WITH (NOLOCK)
        JOIN dbo._Reference62 AS product WITH (NOLOCK)
            ON product._IDRRef = reserve._Fld7655RRef
        JOIN dbo._Reference80 AS warehouse WITH (NOLOCK)
            ON warehouse._IDRRef = reserve._Fld7654RRef
        WHERE reserve._Fld7659 > 0
          AND reserve._Period = :balance_period
          AND reserve._Fld7657_RTRef = 0x00000084
          AND NULLIF(LTRIM(RTRIM(product._Code)), N'') IN :codes
        GROUP BY NULLIF(LTRIM(RTRIM(product._Code)), N'')
        """,
        codes=codes,
    ).bindparams(bindparam("balance_period", value=OPEN_SUPPLIER_ORDER_BALANCE_PERIOD))
    with engine.connect() as conn:
        return {_clean(row["code"]): dict(row) for row in conn.execute(sql).mappings()}


def fetch_incoming_totals(
    engine,
    *,
    codes: Sequence[str],
    as_of: date,
) -> dict[str, dict[str, Any]]:
    if not codes:
        return {}
    empty_date = datetime.combine(ONEC_EMPTY_DATE, time.min)
    arriving_10_days_at = datetime.combine(as_of + timedelta(days=10), time.max)
    arriving_20_days_at = datetime.combine(as_of + timedelta(days=20), time.max)
    sql = _expanding_text(
        """
        SELECT
            NULLIF(LTRIM(RTRIM(product._Code)), N'') AS code,
            SUM(CAST(open_balance._Fld7156 AS decimal(18, 3))) AS incoming_qty,
            COUNT(DISTINCT CONVERT(varchar(34), supplier_order._IDRRef, 1)) AS incoming_order_count,
            MAX(CASE WHEN supplier_order._Fld2493 > :empty_onec_date
                THEN supplier_order._Fld2493 ELSE NULL END) AS latest_expected_receipt_at,
            SUM(CASE WHEN supplier_order._Fld2493 > :empty_onec_date
                    AND supplier_order._Fld2493 <= :arriving_10_days_at
                THEN CAST(open_balance._Fld7156 AS decimal(18, 3)) ELSE 0 END)
                AS pipeline_arriving_10_days_qty,
            SUM(CASE WHEN supplier_order._Fld2493 > :arriving_10_days_at
                    AND supplier_order._Fld2493 <= :arriving_20_days_at
                THEN CAST(open_balance._Fld7156 AS decimal(18, 3)) ELSE 0 END)
                AS pipeline_arriving_20_days_qty,
            SUM(CASE WHEN supplier_order._Fld2493 > :arriving_20_days_at
                THEN CAST(open_balance._Fld7156 AS decimal(18, 3)) ELSE 0 END)
                AS pipeline_later_qty,
            SUM(CASE WHEN supplier_order._Fld2493 IS NULL
                    OR supplier_order._Fld2493 <= :empty_onec_date
                THEN CAST(open_balance._Fld7156 AS decimal(18, 3)) ELSE 0 END)
                AS pipeline_no_date_qty,
            SUM(CASE WHEN supplier_order._Fld8852 > :empty_onec_date
                THEN CAST(open_balance._Fld7156 AS decimal(18, 3)) ELSE 0 END)
                AS pipeline_cargo_handoff_qty,
            SUM(CASE WHEN supplier_order._Fld8852 IS NULL
                    OR supplier_order._Fld8852 <= :empty_onec_date
                THEN CAST(open_balance._Fld7156 AS decimal(18, 3)) ELSE 0 END)
                AS pipeline_supplier_processing_qty
        FROM dbo._AccumRgT7160 AS open_balance WITH (NOLOCK)
        JOIN dbo._Reference62 AS product WITH (NOLOCK)
            ON product._IDRRef = open_balance._Fld7151RRef
        LEFT JOIN dbo._Document133 AS supplier_order WITH (NOLOCK)
            ON supplier_order._IDRRef = open_balance._Fld7149RRef
        WHERE open_balance._Period = :balance_period
          AND open_balance._Fld7156 > 0
          AND NULLIF(LTRIM(RTRIM(product._Code)), N'') IN :codes
        GROUP BY NULLIF(LTRIM(RTRIM(product._Code)), N'')
        """,
        codes=codes,
    ).bindparams(
        bindparam("balance_period", value=OPEN_SUPPLIER_ORDER_BALANCE_PERIOD),
        bindparam("empty_onec_date", value=empty_date),
        bindparam("arriving_10_days_at", value=arriving_10_days_at),
        bindparam("arriving_20_days_at", value=arriving_20_days_at),
    )
    with engine.connect() as conn:
        return {_clean(row["code"]): dict(row) for row in conn.execute(sql).mappings()}


def fetch_sales_totals(
    engine,
    *,
    codes: Sequence[str],
    sellable_codes: Sequence[str],
    date_from: date,
    date_to: date,
) -> dict[str, dict[str, Any]]:
    if not codes:
        return {}
    sql = _expanding_text(
        """
        SELECT
            NULLIF(LTRIM(RTRIM(product._Code)), N'') AS code,
            SUM(CAST(rtu_line._Fld4971 AS decimal(18, 3))) AS sales_qty_window,
            COUNT(DISTINCT CONVERT(varchar(34), rtu._IDRRef, 1)) AS sales_doc_count,
            COUNT(DISTINCT NULLIF(LTRIM(RTRIM(warehouse._Code)), N'')) AS sales_warehouse_count,
            MAX(rtu._Date_Time) AS last_sale_at
        FROM dbo._Document203 AS rtu WITH (NOLOCK)
        JOIN dbo._Document203_VT4966 AS rtu_line WITH (NOLOCK)
            ON rtu_line._Document203_IDRRef = rtu._IDRRef
        JOIN dbo._Reference62 AS product WITH (NOLOCK)
            ON product._IDRRef = rtu_line._Fld4974RRef
        JOIN dbo._Reference80 AS warehouse WITH (NOLOCK)
            ON warehouse._IDRRef = (
                CASE
                    WHEN rtu_line._Fld4983RRef <> 0x00000000000000000000000000000000
                    THEN rtu_line._Fld4983RRef
                    ELSE rtu._Fld4940RRef
                END
            )
        WHERE rtu._Marked = 0x00
          AND rtu._Posted = 0x01
          AND rtu._Date_Time >= :date_from
          AND rtu._Date_Time < :date_to
          AND rtu_line._Fld4971 > 0
          AND NULLIF(LTRIM(RTRIM(product._Code)), N'') IN :codes
          AND NULLIF(LTRIM(RTRIM(warehouse._Code)), N'') IN :sellable_codes
        GROUP BY NULLIF(LTRIM(RTRIM(product._Code)), N'')
        """,
        codes=codes,
        sellable_codes=sellable_codes,
    ).bindparams(
        bindparam("date_from", value=datetime.combine(date_from, time.min)),
        bindparam("date_to", value=datetime.combine(date_to, time.min)),
    )
    with engine.connect() as conn:
        return {_clean(row["code"]): dict(row) for row in conn.execute(sql).mappings()}


def fetch_return_totals(
    engine,
    *,
    codes: Sequence[str],
    sellable_codes: Sequence[str],
    date_from: date,
    date_to: date,
) -> dict[str, dict[str, Any]]:
    if not codes:
        return {}
    sql = _expanding_text(
        """
        SELECT
            NULLIF(LTRIM(RTRIM(product._Code)), N'') AS code,
            SUM(CAST(return_line._Fld1701 AS decimal(18, 3))) AS return_qty_window
        FROM dbo._Document109 AS customer_return WITH (NOLOCK)
        JOIN dbo._Document109_VT1698 AS return_line WITH (NOLOCK)
            ON return_line._Document109_IDRRef = customer_return._IDRRef
        JOIN dbo._Reference62 AS product WITH (NOLOCK)
            ON product._IDRRef = return_line._Fld1700RRef
        JOIN dbo._Reference80 AS warehouse WITH (NOLOCK)
            ON warehouse._IDRRef = return_line._Fld1716RRef
        WHERE customer_return._Marked = 0x00
          AND customer_return._Posted = 0x01
          AND customer_return._Date_Time >= :date_from
          AND customer_return._Date_Time < :date_to
          AND return_line._Fld1701 > 0
          AND NULLIF(LTRIM(RTRIM(product._Code)), N'') IN :codes
          AND NULLIF(LTRIM(RTRIM(warehouse._Code)), N'') IN :sellable_codes
        GROUP BY NULLIF(LTRIM(RTRIM(product._Code)), N'')
        """,
        codes=codes,
        sellable_codes=sellable_codes,
    ).bindparams(
        bindparam("date_from", value=datetime.combine(date_from, time.min)),
        bindparam("date_to", value=datetime.combine(date_to, time.min)),
    )
    with engine.connect() as conn:
        return {_clean(row["code"]): dict(row) for row in conn.execute(sql).mappings()}


def fetch_latest_purchase_prices(engine, *, codes: Sequence[str]) -> dict[str, dict[str, Any]]:
    if not codes:
        return {}
    sql = _expanding_text(
        """
        WITH latest_price AS (
            SELECT
                NULLIF(LTRIM(RTRIM(product._Code)), N'') AS code,
                CAST(supplier_line._Fld2529 AS decimal(18, 2)) AS latest_purchase_price,
                supplier_order._Date_Time AS latest_purchase_price_at,
                ROW_NUMBER() OVER (
                    PARTITION BY NULLIF(LTRIM(RTRIM(product._Code)), N'')
                    ORDER BY supplier_order._Date_Time DESC
                ) AS rn
            FROM dbo._Document133 AS supplier_order WITH (NOLOCK)
            JOIN dbo._Document133_VT2515 AS supplier_line WITH (NOLOCK)
                ON supplier_line._Document133_IDRRef = supplier_order._IDRRef
            JOIN dbo._Reference62 AS product WITH (NOLOCK)
                ON product._IDRRef = supplier_line._Fld2523RRef
            WHERE supplier_order._Marked = 0x00
              AND supplier_order._Posted = 0x01
              AND supplier_line._Fld2520 > 0
              AND supplier_line._Fld2529 >= :min_purchase_price
              AND NULLIF(LTRIM(RTRIM(product._Code)), N'') IN :codes
        )
        SELECT code, latest_purchase_price, latest_purchase_price_at
        FROM latest_price
        WHERE rn = 1
        """,
        codes=codes,
    ).bindparams(
        bindparam("min_purchase_price", value=MIN_PURCHASE_PRICE_FOR_ANALOG_SCORE),
    )
    with engine.connect() as conn:
        return {_clean(row["code"]): dict(row) for row in conn.execute(sql).mappings()}


def fetch_onec_catalog_analog_candidates(
    engine,
    *,
    base_items: Sequence[Mapping[str, Any]],
    include_model_tokens: Sequence[str] = (),
) -> list[dict[str, Any]]:
    base_codes = {_clean(item.get("nomenclature_code")) for item in base_items}
    base_tokens = {token for item in base_items for token in _analog_model_tokens(item)}
    if include_model_tokens:
        base_tokens &= {token for token in include_model_tokens if token}
    if not base_tokens:
        return []
    sql = text("""
        SELECT TOP 5000
            NULLIF(LTRIM(RTRIM(item._Code)), N'') AS nomenclature_code,
            NULLIF(LTRIM(RTRIM(item._Description)), N'') AS name,
            NULLIF(LTRIM(RTRIM(CAST(item._Fld836 AS nvarchar(max)))), N'') AS article
        FROM dbo._Reference62 AS item WITH (NOLOCK)
        WHERE item._Marked = 0x00
          AND item._Description LIKE N'Дисплей для %'
          AND (
              item._Description LIKE N'%+ тачскрин%'
              OR item._Description LIKE N'%(в сборе с тачскрином)%'
          )
        ORDER BY item._Code
        """)
    candidates: list[dict[str, Any]] = []
    with engine.connect() as conn:
        rows = conn.execute(sql).mappings().all()
    for row in rows:
        code = _clean(row.get("nomenclature_code"))
        if not code or code in base_codes:
            continue
        item = {
            "nomenclature_code": code,
            "name": _clean(row.get("name")),
            "article": _clean(row.get("article")),
            "folder": "Каталог 1С: кандидаты-аналоги",
            "status": "catalog_candidate",
            "status_label": "Каталог 1С",
            "auto_order_allowed": False,
            "manual_review_required": True,
            "future_ka_mapping_status": "catalog_only",
            "demand_method_code": "available_days_average",
            "brand_compatibility": "",
            "model_compatibility": "",
            "quality_raw": _quality_from_name(_clean(row.get("name"))),
            "quality_normalized": "",
            "price_segment": "",
            "characteristic_values": {},
        }
        if set(_analog_model_tokens(item)) & base_tokens:
            candidates.append(item)
    return candidates


def build_dry_run_rows(
    items: Sequence[Mapping[str, Any]],
    *,
    facts: Mapping[str, Mapping[str, Mapping[str, Any]]],
    source_errors: Mapping[str, str],
    target_days: int,
    sales_window_days: int,
    max_order_qty: int | None = None,
    order_cadence_days: int = 0,
    supplier_prepare_days: int = 0,
    logistics_days: int = 0,
    supplier_delay_buffer_days: int = 0,
    receiving_buffer_days: int = 0,
    safety_stock_days: int = 0,
    min_display_qty: int = 0,
    min_order_qty: int = 1,
    supplier_assembly_days: int = 0,
    delivery_days: int = 0,
    order_rounding_rules: Sequence[OrderRoundingRule] = (),
    speed_horizon_rules: Sequence[SpeedHorizonRule] = (),
    demand_uplift_rules: Sequence[DemandUpliftRule] = (),
    price_batch_rules: Sequence[PriceBatchRule] = (),
    price_batch_applies_to_statuses: Sequence[str] = (),
    price_batch_applies_to_analog_roles: Sequence[str] = (),
    supported_analog_policy: SupportedAnalogPolicy | None = None,
    b2b_customer_demand_profiles: Mapping[str, B2BSkuDemandProfile] | None = None,
    b2b_customer_demand_error: str = "",
    as_of: date | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    supported_analog_policy = supported_analog_policy or SupportedAnalogPolicy()
    if supplier_assembly_days:
        supplier_prepare_days = supplier_assembly_days
    if delivery_days:
        logistics_days = delivery_days
    lead_time_days = supplier_prepare_days + logistics_days
    planning_horizon_days = (
        target_days
        + order_cadence_days
        + lead_time_days
        + supplier_delay_buffer_days
        + receiving_buffer_days
    )
    effective_target_days = planning_horizon_days + safety_stock_days
    for item in items:
        code = _clean(item.get("nomenclature_code"))
        stock = facts.get("stock", {}).get(code, {})
        reserve = facts.get("reserve", {}).get(code, {})
        incoming = facts.get("incoming", {}).get(code, {})
        sales = facts.get("sales", {}).get(code, {})
        returns = facts.get("returns", {}).get(code, {})
        purchase = facts.get("purchase", {}).get(code, {})
        sellable_stock_qty = _decimal(stock.get("sellable_stock_qty"))
        reserved_qty = _decimal(reserve.get("reserved_qty"))
        free_stock_qty = sellable_stock_qty - reserved_qty
        central_stock_qty = _decimal(stock.get("central_stock_qty"))
        total_stock_qty = _decimal(stock.get("total_stock_qty"))
        incoming_qty = _decimal(incoming.get("incoming_qty"))
        pipeline_arriving_10_days_qty = _decimal(incoming.get("pipeline_arriving_10_days_qty"))
        pipeline_arriving_20_days_qty = _decimal(incoming.get("pipeline_arriving_20_days_qty"))
        pipeline_later_qty = _decimal(incoming.get("pipeline_later_qty"))
        pipeline_no_date_qty = _decimal(incoming.get("pipeline_no_date_qty"))
        pipeline_cargo_handoff_qty = _decimal(incoming.get("pipeline_cargo_handoff_qty"))
        pipeline_supplier_processing_qty = _decimal(
            incoming.get("pipeline_supplier_processing_qty")
        )
        sales_qty = _decimal(sales.get("sales_qty_window"))
        return_qty = _decimal(returns.get("return_qty_window"))
        latest_purchase_price = _decimal(purchase.get("latest_purchase_price"))
        net_sales_qty = max(Decimal("0"), sales_qty - return_qty)
        base_avg_daily_sales_qty = (
            net_sales_qty / Decimal(str(sales_window_days))
            if sales_window_days > 0
            else Decimal("0")
        )
        demand_rule = _demand_uplift_rule_for_item(item, demand_uplift_rules)
        demand_multiplier = demand_rule.demand_multiplier if demand_rule else Decimal("1")
        adjusted_net_sales_qty = net_sales_qty * demand_multiplier
        avg_daily_sales_qty = (
            adjusted_net_sales_qty / Decimal(str(sales_window_days))
            if sales_window_days > 0
            else Decimal("0")
        )
        forecast_qty = (
            _ceil_decimal(avg_daily_sales_qty * Decimal(str(planning_horizon_days)))
            if net_sales_qty > 0
            else Decimal("0")
        )
        safety_stock_qty = (
            _ceil_decimal(avg_daily_sales_qty * Decimal(str(safety_stock_days)))
            if net_sales_qty > 0
            else Decimal("0")
        )
        target_stock_qty = forecast_qty + safety_stock_qty
        if min_display_qty and net_sales_qty > 0:
            target_stock_qty = max(target_stock_qty, Decimal(str(min_display_qty)))
        raw_order_qty = max(Decimal("0"), target_stock_qty - free_stock_qty - incoming_qty)
        recommended_order_qty_raw = _ceil_decimal(raw_order_qty)
        recommended_order_qty = rounded_order_qty(
            recommended_order_qty_raw,
            min_order_qty=min_order_qty,
            max_order_qty=max_order_qty,
            order_rounding_rules=order_rounding_rules,
        )
        order_rounding_rule = _order_rounding_rule_for_qty(
            recommended_order_qty_raw,
            order_rounding_rules,
        )
        blockers: list[str] = []
        warnings: list[str] = []
        if source_errors:
            blockers.append("source_error")
        if free_stock_qty < 0:
            warnings.append("reserve_more_than_sellable_stock")
        if net_sales_qty <= 0:
            warnings.append("no_recent_net_sales")
        if recommended_order_qty_raw > recommended_order_qty:
            warnings.append("order_qty_capped")
        if recommended_order_qty > recommended_order_qty_raw:
            warnings.append("order_qty_rounded_to_multiple")
        if incoming_qty > 0:
            warnings.append("incoming_deducted_from_need")
        if demand_rule and net_sales_qty > 0 and demand_multiplier > Decimal("1"):
            warnings.append("stockout_demand_uplift_applied")
        decision = (
            "manual_review"
            if blockers
            else "order" if recommended_order_qty > 0 else "do_not_order"
        )
        reason = _reason(
            decision=decision,
            recommended_order_qty=recommended_order_qty,
            recommended_order_qty_raw=recommended_order_qty_raw,
            target_stock_qty=target_stock_qty,
            free_stock_qty=free_stock_qty,
            incoming_qty=incoming_qty,
            net_sales_qty=net_sales_qty,
            target_days=target_days,
            order_cadence_days=order_cadence_days,
            supplier_prepare_days=supplier_prepare_days,
            logistics_days=logistics_days,
            supplier_delay_buffer_days=supplier_delay_buffer_days,
            receiving_buffer_days=receiving_buffer_days,
            safety_stock_days=safety_stock_days,
            effective_target_days=effective_target_days,
            sales_window_days=sales_window_days,
            demand_adjustment_reason_ru=demand_rule.reason_ru if demand_rule else "",
            demand_adjustment_multiplier=demand_multiplier,
            order_rounding_rule=order_rounding_rule,
            blockers=blockers,
            warnings=warnings,
        )
        rows.append(
            {
                "nomenclature_code": code,
                "name": _clean(item.get("name")),
                "status_label": _clean(item.get("status_label")),
                "_assortment_status": _clean(item.get("status")),
                "_auto_order_allowed": bool(item.get("auto_order_allowed", True)),
                "quality_raw": _clean(item.get("quality_raw")),
                "quality_normalized": _clean(item.get("quality_normalized")),
                "price_segment": _clean(item.get("price_segment")),
                "latest_purchase_price": _out_decimal(latest_purchase_price, places=2),
                "latest_purchase_price_at": _date_text(purchase.get("latest_purchase_price_at")),
                "analog_group_id": "",
                "analog_group_size": 1,
                "analog_role": "single_sku",
                "preferred_replacement_code": "",
                "preferred_replacement_name": "",
                "analog_score": "",
                "analog_winner_score": "",
                "analog_group_net_sales_qty": "",
                "analog_group_free_stock_qty": "",
                "analog_group_incoming_qty": "",
                "analog_group_target_stock_qty": "",
                "analog_group_recommended_order_qty_raw": "",
                "analog_group_recommended_order_qty": "",
                "analog_model_tokens": "; ".join(_analog_model_tokens(item)),
                "analog_decision_reason_ru": "Одиночная расчетная строка: надежной группы аналогов не найдено.",
                "supported_analog_min_stock_qty": "",
                "supported_analog_floor_need_qty": "",
                "supported_analog_rule_applied": "",
                "sellable_stock_qty": _out_decimal(sellable_stock_qty),
                "reserved_qty": _out_decimal(reserved_qty),
                "free_stock_qty": _out_decimal(free_stock_qty),
                "central_stock_qty": _out_decimal(central_stock_qty),
                "total_stock_qty": _out_decimal(total_stock_qty),
                "incoming_qty": _out_decimal(incoming_qty),
                "incoming_order_count": int(incoming.get("incoming_order_count") or 0),
                "sales_qty_window": _out_decimal(sales_qty),
                "return_qty_window": _out_decimal(return_qty),
                "net_sales_qty_window": _out_decimal(net_sales_qty),
                "sales_doc_count": int(sales.get("sales_doc_count") or 0),
                "sales_warehouse_count": int(sales.get("sales_warehouse_count") or 0),
                "last_sale_at": _date_text(sales.get("last_sale_at")),
                "base_avg_daily_sales_qty": _out_decimal(
                    base_avg_daily_sales_qty,
                    places=4,
                ),
                "avg_daily_sales_qty": _out_decimal(avg_daily_sales_qty, places=4),
                "speed_tier": "",
                "speed_group_avg_daily_sales_qty": "",
                "speed_max_effective_target_days": "",
                "speed_rule_safety_stock_days": "",
                "speed_rule_action": "",
                "adjusted_net_sales_qty_window": _out_decimal(adjusted_net_sales_qty),
                "demand_adjustment_rule_id": demand_rule.rule_id if demand_rule else "",
                "demand_adjustment_multiplier": _out_decimal(demand_multiplier, places=4),
                "demand_adjustment_reason_ru": demand_rule.reason_ru if demand_rule else "",
                "target_days": target_days,
                "order_cadence_days": order_cadence_days,
                "supplier_prepare_days": supplier_prepare_days,
                "supplier_assembly_days": supplier_prepare_days,
                "logistics_days": logistics_days,
                "delivery_days": logistics_days,
                "supplier_delay_buffer_days": supplier_delay_buffer_days,
                "receiving_buffer_days": receiving_buffer_days,
                "safety_stock_days": safety_stock_days,
                "lead_time_days": lead_time_days,
                "effective_target_days": effective_target_days,
                "forecast_qty": _out_decimal(forecast_qty),
                "safety_stock_qty": _out_decimal(safety_stock_qty),
                "min_display_qty": min_display_qty,
                "min_order_qty": min_order_qty,
                "order_rounding_rule": _order_rounding_rule_text(order_rounding_rule),
                "order_rounding_multiple": (
                    order_rounding_rule.round_to if order_rounding_rule else ""
                ),
                "price_batch_min_qty": "",
                "price_batch_excess_qty": "",
                "price_batch_excess_coverage_days": "",
                "price_batch_decision": "",
                "target_stock_qty": _out_decimal(target_stock_qty),
                "recommended_order_qty_raw": _out_decimal(recommended_order_qty_raw),
                "recommended_order_qty": _out_decimal(recommended_order_qty),
                "latest_expected_receipt_at": _date_text(
                    incoming.get("latest_expected_receipt_at")
                ),
                "incoming_latest_arrival_days": _days_until(
                    incoming.get("latest_expected_receipt_at"),
                    as_of=as_of,
                ),
                "pipeline_arriving_10_days_qty": _out_decimal(pipeline_arriving_10_days_qty),
                "pipeline_arriving_20_days_qty": _out_decimal(pipeline_arriving_20_days_qty),
                "pipeline_later_qty": _out_decimal(pipeline_later_qty),
                "pipeline_no_date_qty": _out_decimal(pipeline_no_date_qty),
                "pipeline_cargo_handoff_qty": _out_decimal(pipeline_cargo_handoff_qty),
                "pipeline_supplier_processing_qty": _out_decimal(pipeline_supplier_processing_qty),
                "dry_run_decision": decision,
                "reason_ru": reason,
                "blockers": "; ".join(blockers),
                "warnings": "; ".join(warnings),
                "data_sources": _data_sources(demand_rule=demand_rule),
            }
        )
    apply_analog_group_decisions(
        rows,
        min_order_qty=min_order_qty,
        max_order_qty=max_order_qty,
        order_rounding_rules=order_rounding_rules,
        speed_horizon_rules=speed_horizon_rules,
    )
    apply_supported_analog_and_price_batch_policies(
        rows,
        supported_analog_policy=supported_analog_policy,
        price_batch_rules=price_batch_rules,
        price_batch_applies_to_statuses=price_batch_applies_to_statuses,
        price_batch_applies_to_analog_roles=price_batch_applies_to_analog_roles,
        as_of=as_of,
        min_order_qty=min_order_qty,
        max_order_qty=max_order_qty,
        order_rounding_rules=order_rounding_rules,
    )
    apply_b2b_customer_demand_advisory(
        rows,
        profiles=b2b_customer_demand_profiles or {},
        profile_error=b2b_customer_demand_error,
        as_of=as_of,
        sales_window_days=sales_window_days,
        min_order_qty=min_order_qty,
        max_order_qty=max_order_qty,
        order_rounding_rules=order_rounding_rules,
    )
    apply_b2b_final_order_policies(
        rows,
        price_batch_rules=price_batch_rules,
        price_batch_applies_to_statuses=price_batch_applies_to_statuses,
        price_batch_applies_to_analog_roles=price_batch_applies_to_analog_roles,
        min_order_qty=min_order_qty,
        max_order_qty=max_order_qty,
        order_rounding_rules=order_rounding_rules,
    )
    return rows


def apply_supported_analog_and_price_batch_policies(
    rows: list[dict[str, Any]],
    *,
    supported_analog_policy: SupportedAnalogPolicy,
    price_batch_rules: Sequence[PriceBatchRule],
    price_batch_applies_to_statuses: Sequence[str],
    price_batch_applies_to_analog_roles: Sequence[str],
    as_of: date | None,
    min_order_qty: int,
    max_order_qty: int | None,
    order_rounding_rules: Sequence[OrderRoundingRule],
) -> None:
    if not supported_analog_policy.enabled and not price_batch_rules:
        return

    grouped_row_ids: set[int] = set()
    for group_rows in _analog_groups(rows):
        if len(group_rows) < 2:
            continue
        grouped_row_ids.update(id(row) for row in group_rows)
        primary = next(
            (row for row in group_rows if _clean(row.get("analog_role")) == "primary_analog"),
            None,
        )
        if primary is None:
            continue

        original_group_raw = max(
            (_decimal(row.get("analog_group_recommended_order_qty_raw")) for row in group_rows),
            default=Decimal("0"),
        )
        supported_rows: list[dict[str, Any]] = []
        support_floor_by_id: dict[int, Decimal] = {}
        if supported_analog_policy.enabled:
            for row in group_rows:
                if row is primary or not _is_supported_analog_candidate(
                    row,
                    primary=primary,
                    policy=supported_analog_policy,
                    as_of=as_of,
                ):
                    continue
                floor_need = _ceil_decimal(
                    max(
                        Decimal("0"),
                        Decimal(str(supported_analog_policy.min_network_stock_qty))
                        - _decimal(row.get("free_stock_qty"))
                        - _decimal(row.get("incoming_qty")),
                    )
                )
                row["analog_role"] = "supported_analog"
                row["supported_analog_min_stock_qty"] = (
                    supported_analog_policy.min_network_stock_qty
                )
                row["supported_analog_floor_need_qty"] = _out_decimal(floor_need)
                row["supported_analog_rule_applied"] = "yes"
                _remove_warning(row, "analog_transition_to_better_item")
                _append_warning(row, "supported_analog_network_minimum")
                _append_data_source(row, "config:supported_analog_policy")
                supported_rows.append(row)
                support_floor_by_id[id(row)] = floor_need

        total_support_floor = sum(support_floor_by_id.values(), Decimal("0"))
        group_raw = max(original_group_raw, total_support_floor)
        allocations: dict[int, Decimal] = {
            id(row): support_floor_by_id.get(id(row), Decimal("0")) for row in group_rows
        }
        allocations[id(primary)] = max(Decimal("0"), group_raw - total_support_floor)

        _apply_allocations_and_price_batch(
            group_rows,
            allocations=allocations,
            supported_rows=supported_rows,
            group_target_stock_qty=_decimal(primary.get("analog_group_target_stock_qty")),
            group_free_stock_qty=_decimal(primary.get("analog_group_free_stock_qty")),
            group_incoming_qty=_decimal(primary.get("analog_group_incoming_qty")),
            group_avg_daily_sales_qty=_decimal(primary.get("speed_group_avg_daily_sales_qty")),
            price_batch_rules=price_batch_rules,
            price_batch_applies_to_statuses=price_batch_applies_to_statuses,
            price_batch_applies_to_analog_roles=price_batch_applies_to_analog_roles,
            min_order_qty=min_order_qty,
            max_order_qty=max_order_qty,
            order_rounding_rules=order_rounding_rules,
        )

    for row in rows:
        if id(row) in grouped_row_ids:
            continue
        if _clean(row.get("dry_run_decision")) != "order":
            continue
        raw_qty = _decimal(row.get("recommended_order_qty_raw"))
        if raw_qty <= 0:
            continue
        _apply_allocations_and_price_batch(
            [row],
            allocations={id(row): raw_qty},
            supported_rows=[],
            group_target_stock_qty=_decimal(row.get("target_stock_qty")),
            group_free_stock_qty=_decimal(row.get("free_stock_qty")),
            group_incoming_qty=_decimal(row.get("incoming_qty")),
            group_avg_daily_sales_qty=_decimal(row.get("avg_daily_sales_qty")),
            price_batch_rules=price_batch_rules,
            price_batch_applies_to_statuses=price_batch_applies_to_statuses,
            price_batch_applies_to_analog_roles=price_batch_applies_to_analog_roles,
            min_order_qty=min_order_qty,
            max_order_qty=max_order_qty,
            order_rounding_rules=order_rounding_rules,
        )


def _is_supported_analog_candidate(
    row: Mapping[str, Any],
    *,
    primary: Mapping[str, Any],
    policy: SupportedAnalogPolicy,
    as_of: date | None,
) -> bool:
    configured_statuses = {status.casefold() for status in policy.applies_to_statuses}
    row_statuses = {
        _clean(row.get("_assortment_status")).casefold(),
        _clean(row.get("status_label")).casefold(),
    }
    if not configured_statuses.intersection(row_statuses):
        return False
    if _clean(row.get("blockers")):
        return False
    row_quality = _clean(row.get("quality_normalized") or row.get("quality_raw")).casefold()
    primary_quality = _clean(
        primary.get("quality_normalized") or primary.get("quality_raw")
    ).casefold()
    if row_quality != primary_quality:
        return False
    row_variant_key = _supported_variant_key(row.get("name"))
    if not row_variant_key or row_variant_key != _supported_variant_key(primary.get("name")):
        return False
    if _decimal(row.get("net_sales_qty_window")) < policy.min_recent_sales_qty:
        return False
    last_sale_at = _date_value(row.get("last_sale_at"))
    if as_of is not None:
        if last_sale_at is None:
            return False
        days_since_last_sale = (as_of - last_sale_at).days
        if days_since_last_sale < 0 or days_since_last_sale > policy.max_days_since_last_sale:
            return False
    return True


def _supported_variant_key(value: Any) -> str:
    text_value = _clean(value).casefold().replace("ё", "е")

    def remove_color_parentheses(match: re.Match[str]) -> str:
        content = match.group(1)
        return " " if SUPPORTED_VARIANT_COLOR_RE.search(content) else match.group(0)

    text_value = re.sub(r"\(([^()]*)\)", remove_color_parentheses, text_value)
    text_value = SUPPORTED_VARIANT_COLOR_RE.sub(" ", text_value)
    return re.sub(r"[^a-zа-я0-9]+", " ", text_value).strip()


def _apply_allocations_and_price_batch(
    group_rows: Sequence[dict[str, Any]],
    *,
    allocations: Mapping[int, Decimal],
    supported_rows: Sequence[dict[str, Any]],
    group_target_stock_qty: Decimal,
    group_free_stock_qty: Decimal,
    group_incoming_qty: Decimal,
    group_avg_daily_sales_qty: Decimal,
    price_batch_rules: Sequence[PriceBatchRule],
    price_batch_applies_to_statuses: Sequence[str],
    price_batch_applies_to_analog_roles: Sequence[str],
    min_order_qty: int,
    max_order_qty: int | None,
    order_rounding_rules: Sequence[OrderRoundingRule],
) -> None:
    supported_ids = {id(row) for row in supported_rows}
    final_by_id: dict[int, Decimal] = {}
    review_by_id: set[int] = set()
    for row in group_rows:
        raw_qty = allocations.get(id(row), Decimal("0"))
        final_by_id[id(row)] = rounded_order_qty(
            raw_qty,
            min_order_qty=min_order_qty,
            max_order_qty=max_order_qty,
            order_rounding_rules=order_rounding_rules,
        )

    for row in group_rows:
        raw_qty = allocations.get(id(row), Decimal("0"))
        if raw_qty <= 0:
            continue
        price_rule = _price_batch_rule_for_row(
            row,
            rules=price_batch_rules,
            applies_to_statuses=price_batch_applies_to_statuses,
            applies_to_analog_roles=price_batch_applies_to_analog_roles,
        )
        if price_rule is None:
            continue
        _append_data_source(row, "config:price_segment_schedule_policy")
        if price_rule.minimum_batch_qty is None:
            row["price_batch_decision"] = "exact_need"
            continue
        row["price_batch_min_qty"] = price_rule.minimum_batch_qty
        candidate_qty = rounded_order_qty(
            max(raw_qty, Decimal(str(price_rule.minimum_batch_qty))),
            min_order_qty=min_order_qty,
            max_order_qty=max_order_qty,
            order_rounding_rules=order_rounding_rules,
        )
        candidate_total = (
            sum(final_by_id.values(), Decimal("0")) - final_by_id[id(row)] + candidate_qty
        )
        candidate_excess = max(
            Decimal("0"),
            group_free_stock_qty + group_incoming_qty + candidate_total - group_target_stock_qty,
        )
        candidate_excess_days = (
            candidate_excess / group_avg_daily_sales_qty
            if group_avg_daily_sales_qty > 0
            else Decimal("999999") if candidate_excess > 0 else Decimal("0")
        )
        row["price_batch_excess_qty"] = _out_decimal(candidate_excess)
        row["price_batch_excess_coverage_days"] = _out_decimal(
            candidate_excess_days,
            places=2,
        )
        max_excess_days = price_rule.max_automatic_excess_coverage_days
        if max_excess_days is not None and candidate_excess_days > Decimal(str(max_excess_days)):
            row["price_batch_decision"] = "manual_review_excess"
            review_by_id.add(id(row))
            _append_warning(row, "price_batch_excess_manual_review")
            continue
        final_by_id[id(row)] = candidate_qty
        row["price_batch_decision"] = "rounded_to_price_minimum"
        if candidate_qty > raw_qty:
            _append_warning(row, "price_batch_minimum_applied")

    final_group_qty = sum(final_by_id.values(), Decimal("0"))
    final_group_excess = max(
        Decimal("0"),
        group_free_stock_qty + group_incoming_qty + final_group_qty - group_target_stock_qty,
    )
    final_group_excess_days = (
        final_group_excess / group_avg_daily_sales_qty
        if group_avg_daily_sales_qty > 0
        else Decimal("999999") if final_group_excess > 0 else Decimal("0")
    )
    raw_group_qty = sum(allocations.values(), Decimal("0"))

    for row in group_rows:
        raw_qty = allocations.get(id(row), Decimal("0"))
        final_qty = final_by_id[id(row)]
        if len(group_rows) > 1:
            row["analog_group_recommended_order_qty_raw"] = _out_decimal(raw_group_qty)
            row["analog_group_recommended_order_qty"] = _out_decimal(final_group_qty)
        if raw_qty <= 0:
            row["recommended_order_qty_raw"] = "0"
            row["recommended_order_qty"] = "0"
            if not _clean(row.get("blockers")) and _clean(row.get("speed_tier")) != "slow":
                row["dry_run_decision"] = "do_not_order"
            if id(row) in supported_ids:
                row["analog_decision_reason_ru"] = (
                    "Поддерживаемый аналог уже закрывает сетевой минимум; отдельный заказ "
                    "сейчас не нужен."
                )
                row["reason_ru"] = row["analog_decision_reason_ru"]
            elif (
                _clean(row.get("analog_role")) == "primary_analog"
                and supported_rows
                and raw_group_qty > 0
            ):
                row["analog_decision_reason_ru"] = (
                    "Потребность группы полностью направлена поддерживаемому аналогу, "
                    "которому не хватает товара до сетевого минимума. Основной аналог "
                    "отдельно сейчас не заказываем."
                )
                row["reason_ru"] = row["analog_decision_reason_ru"]
            continue

        row["recommended_order_qty_raw"] = _out_decimal(raw_qty)
        row["recommended_order_qty"] = _out_decimal(final_qty)
        rounding_rule = _order_rounding_rule_for_qty(
            max(raw_qty, _decimal(row.get("price_batch_min_qty"))),
            order_rounding_rules,
        )
        row["order_rounding_rule"] = _order_rounding_rule_text(rounding_rule)
        row["order_rounding_multiple"] = rounding_rule.round_to if rounding_rule else ""
        row["price_batch_excess_qty"] = _out_decimal(final_group_excess)
        row["price_batch_excess_coverage_days"] = _out_decimal(
            final_group_excess_days,
            places=2,
        )
        if id(row) in review_by_id:
            row["dry_run_decision"] = "manual_review"
        elif _clean(row.get("speed_tier")) == "slow":
            row["dry_run_decision"] = "manual_review"
            row["price_batch_decision"] = "manual_review_slow"
            _append_warning(row, "speed_tier_manual_review")
        elif id(row) in supported_ids or bool(row.get("_auto_order_allowed")):
            row["dry_run_decision"] = "order"
        if id(row) in supported_ids:
            row["analog_decision_reason_ru"] = (
                f"Поддерживаемый продающийся аналог: до сетевого минимума "
                f"{row['supported_analog_min_stock_qty']} шт. не хватает {raw_qty} шт.; "
                f"после ценового правила рекомендация {final_qty} шт."
            )
            row["reason_ru"] = row["analog_decision_reason_ru"]
        elif _clean(row.get("price_batch_decision")) == "rounded_to_price_minimum":
            row["reason_ru"] = (
                f"Расчетная потребность {raw_qty} шт.; по классу "
                f"{_clean(row.get('speed_tier'))} + {_clean(row.get('price_segment'))} "
                f"заказ округлен до {final_qty} шт. Излишек группы "
                f"{final_group_excess} шт. ({_out_decimal(final_group_excess_days, places=2)} "
                "дня спроса)."
            )


def _price_batch_rule_for_row(
    row: Mapping[str, Any],
    *,
    rules: Sequence[PriceBatchRule],
    applies_to_statuses: Sequence[str],
    applies_to_analog_roles: Sequence[str],
) -> PriceBatchRule | None:
    configured_statuses = {status.casefold() for status in applies_to_statuses}
    row_statuses = {
        _clean(row.get("_assortment_status")).casefold(),
        _clean(row.get("status_label")).casefold(),
    }
    if configured_statuses and not configured_statuses.intersection(row_statuses):
        return None
    configured_roles = {role.casefold() for role in applies_to_analog_roles}
    if configured_roles and _clean(row.get("analog_role")).casefold() not in configured_roles:
        return None
    speed_tier = _clean(row.get("speed_tier")).casefold()
    price_segment = _clean(row.get("price_segment")).casefold()
    return next(
        (
            rule
            for rule in rules
            if rule.speed_tier.casefold() == speed_tier
            and price_segment in {segment.casefold() for segment in rule.price_segments}
        ),
        None,
    )


def apply_b2b_final_order_policies(
    rows: list[dict[str, Any]],
    *,
    price_batch_rules: Sequence[PriceBatchRule],
    price_batch_applies_to_statuses: Sequence[str],
    price_batch_applies_to_analog_roles: Sequence[str],
    min_order_qty: int,
    max_order_qty: int | None,
    order_rounding_rules: Sequence[OrderRoundingRule],
) -> None:
    if not any(_clean(row.get("b2b_replacement_decision")) for row in rows):
        return

    grouped_row_ids: set[int] = set()
    for group_rows in _analog_groups(rows):
        if len(group_rows) < 2:
            continue
        if not any(_clean(row.get("b2b_replacement_decision")) for row in group_rows):
            continue
        grouped_row_ids.update(id(row) for row in group_rows)
        primary = next(
            (row for row in group_rows if _clean(row.get("analog_role")) == "primary_analog"),
            None,
        )
        if primary is None:
            continue
        group_target = max(
            (_decimal(row.get("b2b_replacement_target_stock_qty")) for row in group_rows),
            default=Decimal("0"),
        )
        group_free = sum((_decimal(row.get("free_stock_qty")) for row in group_rows), Decimal("0"))
        group_incoming = sum(
            (_decimal(row.get("incoming_qty")) for row in group_rows), Decimal("0")
        )
        group_raw = _ceil_decimal(max(Decimal("0"), group_target - group_free - group_incoming))
        supported_rows = [
            row for row in group_rows if _clean(row.get("analog_role")) == "supported_analog"
        ]
        support_floor = {
            _clean(row.get("nomenclature_code")): _decimal(
                row.get("supported_analog_floor_need_qty")
            )
            for row in supported_rows
        }
        total_support_floor = sum(support_floor.values(), Decimal("0"))
        total_raw = max(group_raw, total_support_floor)

        temp_rows = [dict(row) for row in group_rows]
        temp_by_code = {_clean(row.get("nomenclature_code")): row for row in temp_rows}
        temp_primary = temp_by_code[_clean(primary.get("nomenclature_code"))]
        temp_supported = [
            temp_by_code[_clean(row.get("nomenclature_code"))] for row in supported_rows
        ]
        allocations = {
            id(temp): support_floor.get(_clean(temp.get("nomenclature_code")), Decimal("0"))
            for temp in temp_rows
        }
        allocations[id(temp_primary)] = max(Decimal("0"), total_raw - total_support_floor)
        _apply_allocations_and_price_batch(
            temp_rows,
            allocations=allocations,
            supported_rows=temp_supported,
            group_target_stock_qty=group_target,
            group_free_stock_qty=group_free,
            group_incoming_qty=group_incoming,
            group_avg_daily_sales_qty=_decimal(primary.get("speed_group_avg_daily_sales_qty")),
            price_batch_rules=price_batch_rules,
            price_batch_applies_to_statuses=price_batch_applies_to_statuses,
            price_batch_applies_to_analog_roles=price_batch_applies_to_analog_roles,
            min_order_qty=min_order_qty,
            max_order_qty=max_order_qty,
            order_rounding_rules=order_rounding_rules,
        )
        for row in group_rows:
            temp = temp_by_code[_clean(row.get("nomenclature_code"))]
            qty = _decimal(temp.get("recommended_order_qty"))
            decision = _clean(temp.get("dry_run_decision"))
            row["b2b_replacement_recommended_order_qty"] = _out_decimal(qty)
            row["b2b_replacement_decision"] = decision
            row["b2b_order_delta_qty"] = _out_decimal(
                qty - _decimal(row.get("recommended_order_qty"))
            )
            row["b2b_reason_ru"] = (
                _clean(row.get("b2b_reason_ru"))
                + " После клиентского прогноза повторно применены сетевой минимум "
                "поддерживаемого аналога и ценовое округление."
            ).strip()

    for row in rows:
        if id(row) in grouped_row_ids:
            continue
        if _clean(row.get("b2b_replacement_decision")) != "order":
            continue
        raw_qty = _ceil_decimal(
            max(
                Decimal("0"),
                _decimal(row.get("b2b_replacement_target_stock_qty"))
                - _decimal(row.get("free_stock_qty"))
                - _decimal(row.get("incoming_qty")),
            )
        )
        if raw_qty <= 0:
            continue
        temp = dict(row)
        _apply_allocations_and_price_batch(
            [temp],
            allocations={id(temp): raw_qty},
            supported_rows=[],
            group_target_stock_qty=_decimal(row.get("b2b_replacement_target_stock_qty")),
            group_free_stock_qty=_decimal(row.get("free_stock_qty")),
            group_incoming_qty=_decimal(row.get("incoming_qty")),
            group_avg_daily_sales_qty=_decimal(row.get("avg_daily_sales_qty")),
            price_batch_rules=price_batch_rules,
            price_batch_applies_to_statuses=price_batch_applies_to_statuses,
            price_batch_applies_to_analog_roles=price_batch_applies_to_analog_roles,
            min_order_qty=min_order_qty,
            max_order_qty=max_order_qty,
            order_rounding_rules=order_rounding_rules,
        )
        qty = _decimal(temp.get("recommended_order_qty"))
        row["b2b_replacement_recommended_order_qty"] = _out_decimal(qty)
        row["b2b_replacement_decision"] = _clean(temp.get("dry_run_decision"))
        row["b2b_order_delta_qty"] = _out_decimal(qty - _decimal(row.get("recommended_order_qty")))
        row["b2b_reason_ru"] = (
            _clean(row.get("b2b_reason_ru"))
            + " После клиентского прогноза повторно применено ценовое округление."
        ).strip()


def apply_b2b_customer_demand_advisory(
    rows: list[dict[str, Any]],
    *,
    profiles: Mapping[str, B2BSkuDemandProfile],
    profile_error: str,
    as_of: date | None,
    sales_window_days: int,
    min_order_qty: int,
    max_order_qty: int | None,
    order_rounding_rules: Sequence[OrderRoundingRule],
) -> None:
    if profile_error:
        for row in rows:
            row["b2b_demand_mode"] = "source_error"
            row["b2b_reason_ru"] = (
                "Клиентский B2B-профиль не применен: источник недоступен или имеет "
                f"неверный формат ({profile_error}). Базовый dry-run не заблокирован."
            )
            _append_warning(row, "b2b_customer_demand_source_error")
        return
    if not profiles:
        return

    for row in rows:
        code = _clean(row.get("nomenclature_code"))
        profile = profiles.get(code)
        if profile is None:
            continue
        calculation_date = as_of or profile.profile_as_of_exclusive
        profile_age_days = (calculation_date - profile.profile_as_of_exclusive).days
        effective_target_days = int(_decimal(row.get("effective_target_days")))
        safety_stock_days = int(_decimal(row.get("safety_stock_days")))
        planning_horizon_days = max(0, effective_target_days - safety_stock_days)
        due_customer_count = profile.due_customer_count(
            as_of=calculation_date,
            horizon_days=planning_horizon_days,
        )
        active_daily_rate = profile.active_daily_rate_due(
            as_of=calculation_date,
            horizon_days=planning_horizon_days,
        )
        client_forecast_qty = _ceil_decimal(active_daily_rate * Decimal(str(planning_horizon_days)))

        row.update(
            {
                "b2b_profile_as_of_exclusive": profile.profile_as_of_exclusive.isoformat(),
                "b2b_profile_age_days": profile_age_days,
                "b2b_demand_mode": "advisory_only",
                "b2b_dependency_class": profile.dependency_class,
                "b2b_active_customer_count": profile.active_customer_count,
                "b2b_passive_customer_count": profile.passive_customer_count,
                "b2b_due_customer_count": due_customer_count,
                "b2b_active_daily_rate": _out_decimal(active_daily_rate, places=5),
                "b2b_client_forecast_qty": _out_decimal(client_forecast_qty),
            }
        )
        _append_warning(row, "b2b_customer_demand_advisory")
        _append_data_source(row, "report:b2b_customer_demand_profile")
        if profile.dependency_class == "Только активные клиенты 3/4/5":
            _append_warning(row, "b2b_client_only_sku")
        if profile.passive_customer_count:
            _append_warning(row, "b2b_passive_reactivation_not_calibrated")
        if profile_age_days < 0 or profile_age_days > 2:
            _append_warning(row, "b2b_customer_demand_profile_stale")

        if sales_window_days == 180:
            managed_sales_qty = profile.managed_sales_qty_180
        elif sales_window_days == 270:
            managed_sales_qty = profile.managed_sales_qty_270
        else:
            row["b2b_managed_sales_qty_window"] = ""
            row["b2b_reason_ru"] = (
                "Клиентский B2B-профиль показан справочно, но альтернативное количество "
                f"не рассчитано: окно базового спроса {sales_window_days} дней не совпадает "
                "с сохраненными окнами 180/270 дней."
            )
            _append_warning(row, "b2b_sales_window_not_supported")
            continue

        net_sales_qty = _decimal(row.get("net_sales_qty_window"))
        ordinary_net_sales_qty = max(
            Decimal("0"),
            net_sales_qty - managed_sales_qty,
        )
        demand_multiplier = max(
            Decimal("1"),
            _decimal(row.get("demand_adjustment_multiplier")),
        )
        adjusted_ordinary_sales_qty = ordinary_net_sales_qty * demand_multiplier
        ordinary_daily_rate = (
            adjusted_ordinary_sales_qty / Decimal(str(sales_window_days))
            if sales_window_days > 0
            else Decimal("0")
        )
        ordinary_forecast_qty = _ceil_decimal(
            ordinary_daily_rate * Decimal(str(planning_horizon_days))
        )
        ordinary_safety_qty = _ceil_decimal(ordinary_daily_rate * Decimal(str(safety_stock_days)))
        replacement_target_stock_qty = (
            ordinary_forecast_qty + ordinary_safety_qty + client_forecast_qty
        )
        min_display_qty = int(_decimal(row.get("min_display_qty")))
        if min_display_qty and (ordinary_net_sales_qty > 0 or client_forecast_qty > 0):
            replacement_target_stock_qty = max(
                replacement_target_stock_qty,
                Decimal(str(min_display_qty)),
            )
        free_stock_qty = _decimal(row.get("free_stock_qty"))
        incoming_qty = _decimal(row.get("incoming_qty"))
        replacement_order_qty_raw = _ceil_decimal(
            max(
                Decimal("0"),
                replacement_target_stock_qty - free_stock_qty - incoming_qty,
            )
        )
        replacement_order_qty = rounded_order_qty(
            replacement_order_qty_raw,
            min_order_qty=min_order_qty,
            max_order_qty=max_order_qty,
            order_rounding_rules=order_rounding_rules,
        )
        replacement_decision = (
            "manual_review"
            if _clean(row.get("dry_run_decision")) == "manual_review"
            else "order" if replacement_order_qty > 0 else "do_not_order"
        )
        if replacement_decision == "manual_review":
            replacement_order_qty = Decimal("0")
        current_order_qty = _decimal(row.get("recommended_order_qty"))
        order_delta_qty = replacement_order_qty - current_order_qty
        row.update(
            {
                "b2b_managed_sales_qty_window": _out_decimal(managed_sales_qty),
                "b2b_ordinary_net_sales_qty_window": _out_decimal(ordinary_net_sales_qty),
                "b2b_replacement_target_stock_qty": _out_decimal(replacement_target_stock_qty),
                "b2b_replacement_decision": replacement_decision,
                "b2b_replacement_recommended_order_qty": _out_decimal(replacement_order_qty),
                "b2b_order_delta_qty": _out_decimal(order_delta_qty),
                "b2b_reason_ru": (
                    f"Пилот без двойного счета: из чистого спроса {net_sales_qty} шт. "
                    f"выделено {managed_sales_qty} шт. продаж управляемых клиентов 3/4/5; "
                    f"обычный спрос {ordinary_net_sales_qty} шт. Активных клиентов "
                    f"{profile.active_customer_count}, в горизонт {planning_horizon_days} дней "
                    f"попадают {due_customer_count}; их клиентский прогноз "
                    f"{client_forecast_qty} шт. Альтернативная цель "
                    f"{replacement_target_stock_qty} шт., заказ {replacement_order_qty} шт. "
                    f"вместо текущих {current_order_qty} шт., разница "
                    f"{order_delta_qty} шт., решение {replacement_decision}. "
                    "Режим advisory_only: основное количество не изменено."
                ),
            }
        )

    grouped_rows: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        group_id = _clean(row.get("analog_group_id"))
        if group_id:
            grouped_rows.setdefault(group_id, []).append(row)
    for group_id, group_rows in grouped_rows.items():
        profiled_rows = [
            row for row in group_rows if _clean(row.get("b2b_demand_mode")) == "advisory_only"
        ]
        if not profiled_rows:
            continue
        winner = next(
            (row for row in group_rows if _clean(row.get("analog_role")) == "primary_analog"),
            None,
        )
        if winner is None:
            continue
        group_target_stock_qty = sum(
            (
                (
                    _decimal(row.get("b2b_replacement_target_stock_qty"))
                    if _clean(row.get("b2b_demand_mode")) == "advisory_only"
                    else _decimal(row.get("target_stock_qty"))
                )
                for row in group_rows
            ),
            Decimal("0"),
        )
        group_free_stock_qty = sum(
            (_decimal(row.get("free_stock_qty")) for row in group_rows),
            Decimal("0"),
        )
        group_incoming_qty = sum(
            (_decimal(row.get("incoming_qty")) for row in group_rows),
            Decimal("0"),
        )
        group_order_qty_raw = _ceil_decimal(
            max(
                Decimal("0"),
                group_target_stock_qty - group_free_stock_qty - group_incoming_qty,
            )
        )
        group_order_qty = rounded_order_qty(
            group_order_qty_raw,
            min_order_qty=min_order_qty,
            max_order_qty=max_order_qty,
            order_rounding_rules=order_rounding_rules,
        )
        group_replacement_decision = (
            "manual_review"
            if _clean(winner.get("dry_run_decision")) == "manual_review"
            else "order" if group_order_qty > 0 else "do_not_order"
        )
        if group_replacement_decision == "manual_review":
            group_order_qty = Decimal("0")
        winner_current_order_qty = _decimal(winner.get("recommended_order_qty"))
        if not _clean(winner.get("b2b_demand_mode")):
            winner["b2b_demand_mode"] = "advisory_only_group_context"
            winner["b2b_profile_as_of_exclusive"] = profiled_rows[0].get(
                "b2b_profile_as_of_exclusive", ""
            )
            winner["b2b_profile_age_days"] = profiled_rows[0].get("b2b_profile_age_days", "")
            _append_warning(winner, "b2b_customer_demand_advisory")
            _append_data_source(winner, "report:b2b_customer_demand_profile")
        winner["b2b_replacement_target_stock_qty"] = _out_decimal(group_target_stock_qty)
        winner["b2b_replacement_decision"] = group_replacement_decision
        winner["b2b_replacement_recommended_order_qty"] = _out_decimal(group_order_qty)
        winner["b2b_order_delta_qty"] = _out_decimal(group_order_qty - winner_current_order_qty)
        winner["b2b_reason_ru"] = (
            f"B2B-потребность группы аналогов {group_id} собрана в SKU-победитель "
            f"{_clean(winner.get('nomenclature_code'))}: цель группы "
            f"{group_target_stock_qty} шт., свободно {group_free_stock_qty} шт., "
            f"в пути {group_incoming_qty} шт., альтернативный заказ "
            f"{group_order_qty} шт. вместо {winner_current_order_qty} шт., решение "
            f"{group_replacement_decision}. "
            "Режим advisory_only: основное количество не изменено."
        )
        for row in profiled_rows:
            if row is winner:
                continue
            row["b2b_replacement_target_stock_qty"] = "0"
            row["b2b_replacement_decision"] = "manual_review"
            row["b2b_replacement_recommended_order_qty"] = "0"
            row["b2b_order_delta_qty"] = _out_decimal(-_decimal(row.get("recommended_order_qty")))
            row["b2b_reason_ru"] = (
                f"B2B-потребность строки перенесена в SKU-победитель "
                f"{_clean(winner.get('nomenclature_code'))} группы аналогов {group_id}; "
                "отдельный заказ этой строки равен 0. Режим advisory_only: "
                "основное количество не изменено."
            )


def apply_analog_group_decisions(
    rows: list[dict[str, Any]],
    *,
    min_order_qty: int,
    max_order_qty: int | None,
    order_rounding_rules: Sequence[OrderRoundingRule],
    speed_horizon_rules: Sequence[SpeedHorizonRule],
) -> None:
    for group_rows in _analog_groups(rows):
        group_avg_daily_sales_qty = sum(
            (_decimal(row.get("avg_daily_sales_qty")) for row in group_rows),
            Decimal("0"),
        )
        speed_rule = _speed_horizon_rule_for_group(
            group_avg_daily_sales_qty,
            speed_horizon_rules,
        )
        _apply_speed_horizon_rule(
            group_rows,
            speed_rule=speed_rule,
            group_avg_daily_sales_qty=group_avg_daily_sales_qty,
            min_order_qty=min_order_qty,
            max_order_qty=max_order_qty,
            order_rounding_rules=order_rounding_rules,
        )
        min_price = _min_positive_decimal(row.get("latest_purchase_price") for row in group_rows)
        for row in group_rows:
            row["analog_score"] = _out_decimal(_analog_score(row, min_price), places=2)
        if len(group_rows) < 2:
            _mark_single_sku_review_only_if_needed(group_rows[0])
            continue

        winner = max(group_rows, key=lambda row: _analog_sort_key(row, min_price))
        group_id = _analog_group_id(group_rows)
        group_net_sales_qty = sum(
            (_decimal(row.get("net_sales_qty_window")) for row in group_rows), Decimal("0")
        )
        group_free_stock_qty = sum(
            (_decimal(row.get("free_stock_qty")) for row in group_rows), Decimal("0")
        )
        group_incoming_qty = sum(
            (_decimal(row.get("incoming_qty")) for row in group_rows), Decimal("0")
        )
        group_target_stock_qty = sum(
            (_decimal(row.get("target_stock_qty")) for row in group_rows), Decimal("0")
        )
        if speed_rule is not None and speed_rule.review_only:
            group_order_qty_raw = Decimal("0")
            group_order_qty = Decimal("0")
            group_order_rounding_rule = None
        else:
            group_order_qty_raw = _ceil_decimal(
                max(
                    Decimal("0"),
                    group_target_stock_qty - group_free_stock_qty - group_incoming_qty,
                )
            )
            group_order_qty = rounded_order_qty(
                group_order_qty_raw,
                min_order_qty=min_order_qty,
                max_order_qty=max_order_qty,
                order_rounding_rules=order_rounding_rules,
            )
            group_order_rounding_rule = _order_rounding_rule_for_qty(
                group_order_qty_raw,
                order_rounding_rules,
            )
        winner_score = _clean(winner.get("analog_score"))
        demand_adjustment_note = _analog_group_demand_adjustment_note(group_rows)

        for row in group_rows:
            row["analog_group_id"] = group_id
            row["analog_group_size"] = len(group_rows)
            row["preferred_replacement_code"] = winner["nomenclature_code"]
            row["preferred_replacement_name"] = winner["name"]
            row["analog_winner_score"] = winner_score
            row["analog_group_net_sales_qty"] = _out_decimal(group_net_sales_qty)
            row["analog_group_free_stock_qty"] = _out_decimal(group_free_stock_qty)
            row["analog_group_incoming_qty"] = _out_decimal(group_incoming_qty)
            row["analog_group_target_stock_qty"] = _out_decimal(group_target_stock_qty)
            row["analog_group_recommended_order_qty_raw"] = _out_decimal(group_order_qty_raw)
            row["analog_group_recommended_order_qty"] = _out_decimal(group_order_qty)
            row["order_rounding_rule"] = _order_rounding_rule_text(group_order_rounding_rule)
            row["order_rounding_multiple"] = (
                group_order_rounding_rule.round_to if group_order_rounding_rule else ""
            )

        for row in group_rows:
            if row is winner:
                _mark_analog_winner(
                    row,
                    group_size=len(group_rows),
                    group_order_qty_raw=group_order_qty_raw,
                    group_order_qty=group_order_qty,
                    group_order_rounding_rule=group_order_rounding_rule,
                    speed_horizon_rule=speed_rule,
                    group_target_stock_qty=group_target_stock_qty,
                    group_free_stock_qty=group_free_stock_qty,
                    group_incoming_qty=group_incoming_qty,
                    demand_adjustment_note=demand_adjustment_note,
                )
            else:
                _mark_analog_loser(row, winner=winner)


def _mark_analog_winner(
    row: dict[str, Any],
    *,
    group_size: int,
    group_order_qty_raw: Decimal,
    group_order_qty: Decimal,
    group_order_rounding_rule: OrderRoundingRule | None,
    speed_horizon_rule: SpeedHorizonRule | None,
    group_target_stock_qty: Decimal,
    group_free_stock_qty: Decimal,
    group_incoming_qty: Decimal,
    demand_adjustment_note: str,
) -> None:
    row["analog_role"] = "primary_analog"
    _remove_warning(row, "order_qty_capped")
    _append_warning(row, "analog_group_consolidated")
    if group_order_qty_raw > group_order_qty:
        _append_warning(row, "order_qty_capped")
    if group_order_qty > group_order_qty_raw:
        _append_warning(row, "order_qty_rounded_to_multiple")
    if _clean(row.get("blockers")):
        row["recommended_order_qty_raw"] = "0"
        row["recommended_order_qty"] = "0"
        row["dry_run_decision"] = "manual_review"
        row["analog_decision_reason_ru"] = (
            "Основной аналог группы, но есть блокер источника данных; нужен ручной разбор."
        )
        row["reason_ru"] = row["analog_decision_reason_ru"]
        return
    if speed_horizon_rule is not None and speed_horizon_rule.review_only:
        row["recommended_order_qty_raw"] = "0"
        row["recommended_order_qty"] = "0"
        row["dry_run_decision"] = "manual_review"
        _append_warning(row, "speed_tier_manual_review")
        row["analog_decision_reason_ru"] = _speed_review_reason(speed_horizon_rule, group_size)
        row["reason_ru"] = row["analog_decision_reason_ru"]
        return
    if group_order_qty <= 0:
        row["recommended_order_qty_raw"] = "0"
        row["recommended_order_qty"] = "0"
        row["dry_run_decision"] = "do_not_order"
        row["analog_decision_reason_ru"] = (
            f"Основной аналог группы из {group_size} SKU, но заказ не нужен: цель группы "
            f"{group_target_stock_qty} шт. закрыта свободным остатком {group_free_stock_qty} шт. "
            f"и товаром в пути {group_incoming_qty} шт."
        )
        row["reason_ru"] = row["analog_decision_reason_ru"]
        return
    if not bool(row.get("_auto_order_allowed")):
        row["recommended_order_qty_raw"] = "0"
        row["recommended_order_qty"] = "0"
        row["dry_run_decision"] = "manual_review"
        _append_warning(row, "analog_winner_not_auto_order_allowed")
        row["analog_decision_reason_ru"] = (
            f"Лучший аналог группы из {group_size} SKU, расчетная потребность "
            f"{group_order_qty} шт., но автозаказ по карточке еще не разрешен."
        )
        row["reason_ru"] = row["analog_decision_reason_ru"]
        return

    row["recommended_order_qty_raw"] = _out_decimal(group_order_qty_raw)
    row["recommended_order_qty"] = _out_decimal(group_order_qty)
    row["dry_run_decision"] = "order"
    row["analog_decision_reason_ru"] = (
        f"Основной аналог группы из {group_size} SKU: заказ переносим сюда. "
        f"Цель группы {group_target_stock_qty} шт., свободно {group_free_stock_qty} шт., "
        f"в пути {group_incoming_qty} шт."
    )
    if demand_adjustment_note:
        row["analog_decision_reason_ru"] += f" {demand_adjustment_note}"
    rounding_note = _order_rounding_note(group_order_rounding_rule)
    if rounding_note:
        row["analog_decision_reason_ru"] += f" {rounding_note}"
    row["reason_ru"] = (
        f"Рекомендуем {group_order_qty} шт. по группе аналогов: цель "
        f"{group_target_stock_qty} шт., свободно {group_free_stock_qty} шт., "
        f"в пути {group_incoming_qty} шт.; основной аналог выбран по продажам, "
        "качеству, возвратам, наличию и закупочной цене."
    )
    if demand_adjustment_note:
        row["reason_ru"] += f" {demand_adjustment_note}"
    if rounding_note:
        row["reason_ru"] += f" {rounding_note}"


def _mark_single_sku_review_only_if_needed(row: dict[str, Any]) -> None:
    if _clean(row.get("dry_run_decision")) != "order" or bool(row.get("_auto_order_allowed")):
        return
    row["analog_group_recommended_order_qty_raw"] = row["recommended_order_qty_raw"]
    row["analog_group_recommended_order_qty"] = row["recommended_order_qty"]
    row["recommended_order_qty_raw"] = "0"
    row["recommended_order_qty"] = "0"
    row["dry_run_decision"] = "manual_review"
    _append_warning(row, "not_auto_order_allowed")
    row["analog_decision_reason_ru"] = (
        "Расчет показывает потребность, но карточка не входит в разрешенное ядро "
        "автозаказа; нужна ручная проверка или перевод в рабочий статус."
    )
    row["reason_ru"] = row["analog_decision_reason_ru"]


def _mark_analog_loser(row: dict[str, Any], *, winner: Mapping[str, Any]) -> None:
    row["analog_role"] = "transition_to_better_analog"
    _remove_warning(row, "order_qty_capped")
    row["recommended_order_qty_raw"] = "0"
    row["recommended_order_qty"] = "0"
    if not _clean(row.get("blockers")):
        row["dry_run_decision"] = "do_not_order"
    _append_warning(row, "analog_transition_to_better_item")
    row["analog_decision_reason_ru"] = (
        f"Переход на лучший аналог {winner['nomenclature_code']}: "
        f"{winner['name']}. Текущую карточку не заказывать, остаток допродавать."
    )
    row["reason_ru"] = row["analog_decision_reason_ru"]


def _analog_group_demand_adjustment_note(group_rows: Sequence[Mapping[str, Any]]) -> str:
    rule_rows = [
        row
        for row in group_rows
        if _clean(row.get("demand_adjustment_rule_id"))
        and _decimal(row.get("demand_adjustment_multiplier")) > Decimal("1")
    ]
    if not rule_rows:
        return ""
    strongest = max(
        rule_rows,
        key=lambda row: _decimal(row.get("demand_adjustment_multiplier")),
    )
    multiplier = _decimal(strongest.get("demand_adjustment_multiplier"))
    reason = _clean(strongest.get("demand_adjustment_reason_ru"))
    note = f"Для группы применена поправка скрытого спроса x{_out_decimal(multiplier, places=2)}"
    if reason:
        note += f": {reason}"
    return note + "."


def _apply_speed_horizon_rule(
    group_rows: Sequence[dict[str, Any]],
    *,
    speed_rule: SpeedHorizonRule | None,
    group_avg_daily_sales_qty: Decimal,
    min_order_qty: int,
    max_order_qty: int | None,
    order_rounding_rules: Sequence[OrderRoundingRule],
) -> None:
    if speed_rule is None:
        return
    action = "manual_review" if speed_rule.review_only else "cap_coverage_days"
    for row in group_rows:
        row["speed_tier"] = speed_rule.tier
        row["speed_group_avg_daily_sales_qty"] = _out_decimal(
            group_avg_daily_sales_qty,
            places=4,
        )
        row["speed_max_effective_target_days"] = (
            speed_rule.max_effective_target_days if speed_rule.max_effective_target_days else ""
        )
        row["speed_rule_safety_stock_days"] = (
            speed_rule.safety_stock_days if not speed_rule.review_only else ""
        )
        row["speed_rule_action"] = action
        _append_warning(row, "speed_horizon_rule_applied")

    if speed_rule.review_only:
        for row in group_rows:
            row["recommended_order_qty_raw"] = "0"
            row["recommended_order_qty"] = "0"
            row["dry_run_decision"] = "manual_review"
            _append_warning(row, "speed_tier_manual_review")
            row["reason_ru"] = _speed_review_reason(speed_rule, len(group_rows))
        return

    forecast_days = max(0, speed_rule.max_effective_target_days - speed_rule.safety_stock_days)
    for row in group_rows:
        avg_daily_sales_qty = _decimal(row.get("avg_daily_sales_qty"))
        net_sales_qty = _decimal(row.get("net_sales_qty_window"))
        forecast_qty = (
            _ceil_decimal(avg_daily_sales_qty * Decimal(str(forecast_days)))
            if net_sales_qty > 0
            else Decimal("0")
        )
        safety_stock_qty = (
            _ceil_decimal(avg_daily_sales_qty * Decimal(str(speed_rule.safety_stock_days)))
            if net_sales_qty > 0
            else Decimal("0")
        )
        target_stock_qty = forecast_qty + safety_stock_qty
        min_display_qty = int(_decimal(row.get("min_display_qty")))
        if min_display_qty and net_sales_qty > 0:
            target_stock_qty = max(target_stock_qty, Decimal(str(min_display_qty)))
        free_stock_qty = _decimal(row.get("free_stock_qty"))
        incoming_qty = _decimal(row.get("incoming_qty"))
        recommended_order_qty_raw = _ceil_decimal(
            max(Decimal("0"), target_stock_qty - free_stock_qty - incoming_qty)
        )
        recommended_order_qty = rounded_order_qty(
            recommended_order_qty_raw,
            min_order_qty=min_order_qty,
            max_order_qty=max_order_qty,
            order_rounding_rules=order_rounding_rules,
        )
        order_rounding_rule = _order_rounding_rule_for_qty(
            recommended_order_qty_raw,
            order_rounding_rules,
        )

        row["safety_stock_days"] = speed_rule.safety_stock_days
        row["effective_target_days"] = speed_rule.max_effective_target_days
        row["forecast_qty"] = _out_decimal(forecast_qty)
        row["safety_stock_qty"] = _out_decimal(safety_stock_qty)
        row["target_stock_qty"] = _out_decimal(target_stock_qty)
        row["recommended_order_qty_raw"] = _out_decimal(recommended_order_qty_raw)
        row["recommended_order_qty"] = _out_decimal(recommended_order_qty)
        row["order_rounding_rule"] = _order_rounding_rule_text(order_rounding_rule)
        row["order_rounding_multiple"] = order_rounding_rule.round_to if order_rounding_rule else ""

        _remove_warning(row, "order_qty_capped")
        _remove_warning(row, "order_qty_rounded_to_multiple")
        if recommended_order_qty_raw > recommended_order_qty:
            _append_warning(row, "order_qty_capped")
        if recommended_order_qty > recommended_order_qty_raw:
            _append_warning(row, "order_qty_rounded_to_multiple")
        if _clean(row.get("blockers")):
            row["dry_run_decision"] = "manual_review"
        else:
            row["dry_run_decision"] = "order" if recommended_order_qty > 0 else "do_not_order"
        row["reason_ru"] = _speed_horizon_reason(
            row,
            speed_rule=speed_rule,
            recommended_order_qty=recommended_order_qty,
            recommended_order_qty_raw=recommended_order_qty_raw,
            target_stock_qty=target_stock_qty,
            free_stock_qty=free_stock_qty,
            incoming_qty=incoming_qty,
            order_rounding_rule=order_rounding_rule,
        )


def _speed_horizon_rule_for_group(
    group_avg_daily_sales_qty: Decimal,
    rules: Sequence[SpeedHorizonRule],
) -> SpeedHorizonRule | None:
    matching = [
        rule for rule in rules if group_avg_daily_sales_qty >= rule.min_group_avg_daily_sales_qty
    ]
    if not matching:
        return None
    return max(matching, key=lambda rule: rule.min_group_avg_daily_sales_qty)


def _speed_horizon_reason(
    row: Mapping[str, Any],
    *,
    speed_rule: SpeedHorizonRule,
    recommended_order_qty: Decimal,
    recommended_order_qty_raw: Decimal,
    target_stock_qty: Decimal,
    free_stock_qty: Decimal,
    incoming_qty: Decimal,
    order_rounding_rule: OrderRoundingRule | None,
) -> str:
    label = speed_rule.label_ru or speed_rule.tier
    base = (
        f"Правило скорости {label}: максимум {speed_rule.max_effective_target_days} дней "
        f"покрытия, страховой запас {speed_rule.safety_stock_days} дней. "
    )
    if _clean(row.get("blockers")):
        return base + "Не считаем заказ: есть ошибка источника данных, нужна ручная проверка."
    if recommended_order_qty > 0:
        reason = (
            f"{base}Рекомендуем {recommended_order_qty} шт.: цель {target_stock_qty} шт., "
            f"свободно {free_stock_qty} шт., в пути {incoming_qty} шт."
        )
        rounding_note = _order_rounding_note(order_rounding_rule)
        if rounding_note and recommended_order_qty > recommended_order_qty_raw:
            reason += f" {rounding_note}"
        return reason
    return (
        f"{base}Не заказывать: цель {target_stock_qty} шт. закрыта свободным остатком "
        f"{free_stock_qty} шт. и товаром в пути {incoming_qty} шт."
    )


def _speed_review_reason(speed_rule: SpeedHorizonRule, group_size: int) -> str:
    label = speed_rule.label_ru or speed_rule.tier
    return (
        f"Правило скорости {label}: группа из {group_size} SKU медленная, автозаказ не "
        "раздуваем; нужен ручной review."
    )


def build_summary(
    rows: Sequence[Mapping[str, Any]],
    *,
    run_id: int | None,
    source_errors: Mapping[str, str],
) -> dict[str, Any]:
    decisions = Counter(_clean(row.get("dry_run_decision")) for row in rows)
    warnings = Counter(
        warning for row in rows for warning in _clean(row.get("warnings")).split("; ") if warning
    )
    blockers = Counter(
        blocker for row in rows for blocker in _clean(row.get("blockers")).split("; ") if blocker
    )
    demand_rules = Counter(
        _clean(row.get("demand_adjustment_rule_id"))
        for row in rows
        if _clean(row.get("demand_adjustment_rule_id"))
    )
    speed_tiers = Counter(
        _clean(row.get("speed_tier")) for row in rows if _clean(row.get("speed_tier"))
    )
    b2b_demand_modes = Counter(
        _clean(row.get("b2b_demand_mode")) for row in rows if _clean(row.get("b2b_demand_mode"))
    )
    b2b_dependency_classes = Counter(
        _clean(row.get("b2b_dependency_class"))
        for row in rows
        if _clean(row.get("b2b_dependency_class"))
    )
    b2b_replacement_decisions = Counter(
        _clean(row.get("b2b_replacement_decision"))
        for row in rows
        if _clean(row.get("b2b_replacement_decision"))
    )
    analog_roles = Counter(_clean(row.get("analog_role")) for row in rows)
    analog_roles.pop("", None)
    price_batch_decisions = Counter(
        _clean(row.get("price_batch_decision"))
        for row in rows
        if _clean(row.get("price_batch_decision"))
    )
    analog_group_ids = {
        _clean(row.get("analog_group_id"))
        for row in rows
        if _decimal(row.get("analog_group_size")) > 1 and _clean(row.get("analog_group_id"))
    }
    total_recommended = sum(
        (_decimal(row.get("recommended_order_qty")) for row in rows), Decimal("0")
    )
    total_raw_recommended = sum(
        (_decimal(row.get("recommended_order_qty_raw")) for row in rows), Decimal("0")
    )
    total_automatic_order = sum(
        (
            _decimal(row.get("recommended_order_qty"))
            for row in rows
            if _clean(row.get("dry_run_decision")) == "order"
        ),
        Decimal("0"),
    )
    total_manual_review_qty = sum(
        (
            _decimal(row.get("recommended_order_qty"))
            for row in rows
            if _clean(row.get("dry_run_decision")) == "manual_review"
        ),
        Decimal("0"),
    )
    total_b2b_replacement_recommended = sum(
        (_decimal(row.get("b2b_replacement_recommended_order_qty")) for row in rows),
        Decimal("0"),
    )
    total_b2b_order_delta = sum(
        (_decimal(row.get("b2b_order_delta_qty")) for row in rows),
        Decimal("0"),
    )
    horizon_row = rows[0] if rows else {}
    return {
        "classification_run_id": run_id,
        "items": len(rows),
        "target_days": int(_decimal(horizon_row.get("target_days"))),
        "order_cadence_days": int(_decimal(horizon_row.get("order_cadence_days"))),
        "supplier_prepare_days": int(_decimal(horizon_row.get("supplier_prepare_days"))),
        "supplier_assembly_days": int(_decimal(horizon_row.get("supplier_assembly_days"))),
        "logistics_days": int(_decimal(horizon_row.get("logistics_days"))),
        "delivery_days": int(_decimal(horizon_row.get("delivery_days"))),
        "supplier_delay_buffer_days": int(_decimal(horizon_row.get("supplier_delay_buffer_days"))),
        "receiving_buffer_days": int(_decimal(horizon_row.get("receiving_buffer_days"))),
        "safety_stock_days": int(_decimal(horizon_row.get("safety_stock_days"))),
        "min_order_qty": int(_decimal(horizon_row.get("min_order_qty"))),
        "lead_time_days": int(_decimal(horizon_row.get("lead_time_days"))),
        "effective_target_days": int(_decimal(horizon_row.get("effective_target_days"))),
        "decision_counts": dict(sorted(decisions.items())),
        "analog_role_counts": dict(sorted(analog_roles.items())),
        "price_batch_decision_counts": dict(sorted(price_batch_decisions.items())),
        "analog_group_count": len(analog_group_ids),
        "demand_adjustment_rule_counts": dict(sorted(demand_rules.items())),
        "speed_tier_counts": dict(sorted(speed_tiers.items())),
        "b2b_demand_mode_counts": dict(sorted(b2b_demand_modes.items())),
        "b2b_dependency_class_counts": dict(sorted(b2b_dependency_classes.items())),
        "b2b_replacement_decision_counts": dict(sorted(b2b_replacement_decisions.items())),
        "warning_counts": dict(sorted(warnings.items())),
        "blocker_counts": dict(sorted(blockers.items())),
        "total_recommended_order_qty": _out_decimal(total_recommended),
        "total_recommended_order_qty_raw": _out_decimal(total_raw_recommended),
        "total_rounding_delta_qty": _out_decimal(total_recommended - total_raw_recommended),
        "total_automatic_order_qty": _out_decimal(total_automatic_order),
        "total_manual_review_qty": _out_decimal(total_manual_review_qty),
        "total_b2b_replacement_recommended_order_qty": _out_decimal(
            total_b2b_replacement_recommended
        ),
        "total_b2b_order_delta_qty": _out_decimal(total_b2b_order_delta),
        "source_errors": dict(source_errors),
    }


def write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in CSV_COLUMNS})
    return path


def _reason(
    *,
    decision: str,
    recommended_order_qty: Decimal,
    recommended_order_qty_raw: Decimal,
    target_stock_qty: Decimal,
    free_stock_qty: Decimal,
    incoming_qty: Decimal,
    net_sales_qty: Decimal,
    target_days: int,
    order_cadence_days: int,
    supplier_prepare_days: int,
    logistics_days: int,
    supplier_delay_buffer_days: int,
    receiving_buffer_days: int,
    safety_stock_days: int,
    effective_target_days: int,
    sales_window_days: int,
    demand_adjustment_reason_ru: str,
    demand_adjustment_multiplier: Decimal,
    order_rounding_rule: OrderRoundingRule | None,
    blockers: Sequence[str],
    warnings: Sequence[str],
) -> str:
    horizon = _horizon_text(
        target_days=target_days,
        order_cadence_days=order_cadence_days,
        supplier_prepare_days=supplier_prepare_days,
        logistics_days=logistics_days,
        supplier_delay_buffer_days=supplier_delay_buffer_days,
        receiving_buffer_days=receiving_buffer_days,
        safety_stock_days=safety_stock_days,
        effective_target_days=effective_target_days,
    )
    if blockers:
        return "Не считаем заказ: есть ошибка источника данных, нужна ручная проверка."
    if decision == "order":
        reason = (
            f"Рекомендуем {recommended_order_qty} шт.: цель {target_stock_qty} шт. "
            f"{horizon}, свободно {free_stock_qty} шт., в пути {incoming_qty} шт."
        )
        if demand_adjustment_multiplier > Decimal("1"):
            reason += (
                f" Поправка скрытого спроса x{_out_decimal(demand_adjustment_multiplier, places=2)}"
            )
            if demand_adjustment_reason_ru:
                reason += f": {demand_adjustment_reason_ru}"
            reason += "."
        rounding_note = _order_rounding_note(order_rounding_rule)
        if rounding_note and recommended_order_qty > recommended_order_qty_raw:
            reason += f" {rounding_note}"
        return reason
    if net_sales_qty <= 0:
        return f"Не заказывать: за последние {sales_window_days} дней нет чистых продаж."
    if "incoming_deducted_from_need" in warnings:
        return (
            f"Не заказывать: цель {target_stock_qty} шт. {horizon} закрыта свободным остатком "
            f"{free_stock_qty} шт. и товаром в пути {incoming_qty} шт."
        )
    return (
        f"Не заказывать: свободный остаток {free_stock_qty} шт. "
        f"покрывает цель {target_stock_qty} шт. {horizon}."
    )


def _horizon_text(
    *,
    target_days: int,
    order_cadence_days: int,
    supplier_prepare_days: int,
    logistics_days: int,
    supplier_delay_buffer_days: int,
    receiving_buffer_days: int,
    safety_stock_days: int,
    effective_target_days: int,
) -> str:
    components = [f"покрытие {target_days}"]
    if order_cadence_days:
        components.append(f"график {order_cadence_days}")
    if supplier_prepare_days:
        components.append(f"сборка {supplier_prepare_days}")
    if logistics_days:
        components.append(f"доставка {logistics_days}")
    if supplier_delay_buffer_days:
        components.append(f"буфер {supplier_delay_buffer_days}")
    if receiving_buffer_days:
        components.append(f"приемка {receiving_buffer_days}")
    if safety_stock_days:
        components.append(f"страховой запас {safety_stock_days}")
    return f"на {effective_target_days} дней ({' + '.join(components)})"


KNOWN_DEVICE_BRANDS = (
    "apple",
    "samsung",
    "xiaomi",
    "redmi",
    "poco",
    "huawei",
    "honor",
    "oppo",
    "realme",
    "vivo",
    "meizu",
    "nokia",
    "lenovo",
    "zte",
    "sony",
    "motorola",
    "asus",
    "tecno",
    "infinix",
    "oneplus",
)
MODEL_CODE_RE = re.compile(
    r"(?<![a-z0-9])("
    r"sm-[a-z]\d{3}[a-z]?|"
    r"rmx\d{4}|"
    r"cph\d{4}|"
    r"m\d{4}[a-z]\d{2}[a-z]?|"
    r"[a-z]{1,5}\d{1,5}[a-z0-9-]*|"
    r"\d{6,}[a-z]{0,4}"
    r")(?![a-z0-9])",
    re.IGNORECASE,
)
MODEL_STOP_WORDS = {
    "display",
    "screen",
    "lcd",
    "oled",
    "amoled",
    "тачскрин",
    "дисплей",
    "черный",
    "черн",
    "black",
    "white",
    "orig",
    "orig100",
    "or100",
    "original",
    "oem",
    "medium",
    "premium",
    "optima",
    "copy",
    "high",
    "low",
    "incell",
    "in-cell",
    "with",
    "touch",
}


def _analog_model_tokens(item: Mapping[str, Any]) -> tuple[str, ...]:
    default_brand = _normalize_brand(_clean(item.get("brand_compatibility")))
    tokens: set[str] = set()
    for value in (item.get("model_compatibility"), item.get("name")):
        tokens.update(_model_tokens_from_text(_clean(value), default_brand=default_brand))
    return tuple(sorted(tokens))


def _model_tokens_from_text(value: str, *, default_brand: str) -> set[str]:
    if not value:
        return set()
    text = value.casefold().replace("ё", "е")
    segment = text
    if "для " in segment:
        segment = segment.split("для ", 1)[1]
    if "+" in segment:
        segment = segment.split("+", 1)[0]
    segment = segment.replace("\\", "/")
    parts = [part.strip() for part in re.split(r"\s*/\s*|,\s*|\s+и\s+др\.?", segment)]
    tokens: set[str] = set()
    current_brand = default_brand
    for part in parts:
        if not part:
            continue
        brand = _detect_brand(part) or current_brand
        if brand:
            current_brand = brand
        if not brand:
            continue
        for code in _model_code_tokens(part):
            tokens.add(f"{brand}:code:{code}")
        phrase = _model_phrase(part, brand=brand)
        if phrase and any(ch.isdigit() for ch in phrase):
            tokens.add(f"{brand}:model:{phrase}")
    return tokens


def _model_code_tokens(value: str) -> set[str]:
    tokens: set[str] = set()
    for match in MODEL_CODE_RE.finditer(value):
        token = match.group(1).casefold()
        if token in MODEL_STOP_WORDS:
            continue
        if token in {"4g", "5g", "3g", "2g"}:
            continue
        if re.fullmatch(r"[a-z]\d{1,2}", token):
            continue
        if len(token) < 3:
            continue
        tokens.add(token)
    return tokens


def _model_phrase(value: str, *, brand: str) -> str:
    text = re.sub(r"\([^)]*\)", " ", value.casefold())
    text = re.sub(r"[^a-zа-я0-9+-]+", " ", text)
    words = []
    for word in text.split():
        clean_word = word.strip("+-")
        if not clean_word or clean_word == brand or clean_word in MODEL_STOP_WORDS:
            continue
        if clean_word in KNOWN_DEVICE_BRANDS and clean_word != brand:
            continue
        words.append(clean_word)
    return " ".join(words[:8])


def _detect_brand(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", value.casefold())
    for brand in KNOWN_DEVICE_BRANDS:
        if re.search(rf"(?<![a-z0-9]){re.escape(brand)}(?![a-z0-9])", normalized):
            if brand == "redmi":
                return "xiaomi"
            if brand == "poco":
                return "xiaomi"
            return brand
    return ""


def _normalize_brand(value: str) -> str:
    detected = _detect_brand(value)
    if detected:
        return detected
    normalized = re.sub(r"[^a-z0-9]+", "", value.casefold())
    return normalized


def _analog_groups(rows: Sequence[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    parent = list(range(len(rows)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    token_owner: dict[str, int] = {}
    for index, row in enumerate(rows):
        tokens = _row_analog_tokens(row)
        for token in tokens:
            previous = token_owner.get(token)
            if previous is None:
                token_owner[token] = index
            else:
                union(previous, index)

    groups: dict[int, list[dict[str, Any]]] = {}
    for index, row in enumerate(rows):
        groups.setdefault(find(index), []).append(row)
    return list(groups.values())


def _row_analog_tokens(row: Mapping[str, Any]) -> set[str]:
    return {
        token.strip()
        for token in _clean(row.get("analog_model_tokens")).split(";")
        if token.strip()
    }


def _demand_uplift_rule_for_item(
    item: Mapping[str, Any],
    rules: Sequence[DemandUpliftRule],
) -> DemandUpliftRule | None:
    if not rules:
        return None
    item_tokens = set(_analog_model_tokens(item))
    if not item_tokens:
        return None
    matching_rules = [
        rule for rule in rules if item_tokens & set(rule.match_any_analog_model_tokens)
    ]
    if not matching_rules:
        return None
    return max(matching_rules, key=lambda rule: rule.demand_multiplier)


def _data_sources(*, demand_rule: DemandUpliftRule | None = None) -> str:
    sources = [
        "1c:stock_totals",
        "1c:reserved_stock_totals",
        "1c:open_supplier_order_balance_pipeline",
        "1c:rtu_lines",
        "1c:return_lines",
        "1c:supplier_order_latest_price",
        "config:display-auto-order-policy",
        "local:analog_group_scoring",
    ]
    if demand_rule is not None:
        sources.append("config:demand_uplift_rules")
    return "; ".join(sources)


def _analog_group_id(group_rows: Sequence[Mapping[str, Any]]) -> str:
    tokens = sorted({token for row in group_rows for token in _row_analog_tokens(row)})
    digest = hashlib.sha1("||".join(tokens).encode("utf-8")).hexdigest()[:12]
    return f"analog-{digest}"


def _analog_sort_key(row: Mapping[str, Any], min_price: Decimal) -> tuple[Any, ...]:
    sales_qty = _decimal(row.get("net_sales_qty_window"))
    quality_rank = Decimal(str(_quality_rank(row)))
    return_rate = _return_rate(row)
    availability = max(Decimal("0"), _decimal(row.get("free_stock_qty"))) + max(
        Decimal("0"), _decimal(row.get("incoming_qty"))
    )
    price = _decimal(row.get("latest_purchase_price"))
    price_sort = price if price > 0 else Decimal("999999")
    return (
        sales_qty,
        quality_rank,
        -return_rate,
        availability,
        -price_sort,
        _clean(row.get("nomenclature_code")),
    )


def _analog_score(row: Mapping[str, Any], min_price: Decimal) -> Decimal:
    sales_qty = _decimal(row.get("net_sales_qty_window"))
    doc_count = _decimal(row.get("sales_doc_count"))
    quality_rank = Decimal(str(_quality_rank(row)))
    availability = max(Decimal("0"), _decimal(row.get("free_stock_qty"))) + max(
        Decimal("0"), _decimal(row.get("incoming_qty"))
    )
    price = _decimal(row.get("latest_purchase_price"))
    price_penalty = Decimal("0")
    if price > 0 and min_price > 0 and price > min_price:
        price_penalty = ((price - min_price) / min_price) * Decimal("10")
    return (
        sales_qty
        + (doc_count * Decimal("2"))
        + quality_rank
        + (availability / Decimal("10"))
        - (_return_rate(row) * Decimal("100"))
        - price_penalty
    )


def _quality_rank(row: Mapping[str, Any]) -> int:
    text = " ".join(
        [
            _clean(row.get("quality_raw")),
            _clean(row.get("quality_normalized")),
            _clean(row.get("name")),
        ]
    ).casefold()
    compact = re.sub(r"[^a-zа-я0-9]+", "", text)
    if any(token in compact for token in ("orig100", "or100", "original", "ориг100")):
        return 100
    if any(token in compact for token in ("orig", "ориг", "oem")):
        return 90
    if any(token in compact for token in ("premium", "high", "aaa", "премиум")):
        return 80
    if any(token in compact for token in ("optima", "standard", "std", "средн")):
        return 65
    if any(token in compact for token in ("medium", "analog", "аналог")):
        return 55
    if any(token in compact for token in ("low", "econom", "cheap", "эконом")):
        return 25
    return 50


def _quality_from_name(value: str) -> str:
    text = value.casefold()
    compact = re.sub(r"[^a-zа-я0-9]+", "", text)
    if any(token in compact for token in ("orig100", "or100", "ориг100")):
        return "ORIG100"
    if any(token in compact for token in ("orig", "original", "ориг")):
        return "ORIG"
    if any(token in compact for token in ("premium", "премиум", "aaa")):
        return "Premium"
    if any(token in compact for token in ("optima", "оптима")):
        return "Optima"
    if any(token in compact for token in ("medium", "аналог")):
        return "Medium"
    if any(token in compact for token in ("low", "econom", "эконом")):
        return "Low"
    return ""


def _return_rate(row: Mapping[str, Any]) -> Decimal:
    sales_qty = _decimal(row.get("sales_qty_window"))
    if sales_qty <= 0:
        return Decimal("0")
    return _decimal(row.get("return_qty_window")) / sales_qty


def _min_positive_decimal(values: Iterable[Any]) -> Decimal:
    positive = [_decimal(value) for value in values if _decimal(value) > 0]
    return min(positive) if positive else Decimal("0")


def _append_warning(row: dict[str, Any], warning: str) -> None:
    warnings = [item for item in _clean(row.get("warnings")).split("; ") if item]
    if warning not in warnings:
        warnings.append(warning)
    row["warnings"] = "; ".join(warnings)


def _append_data_source(row: dict[str, Any], source: str) -> None:
    sources = [item for item in _clean(row.get("data_sources")).split("; ") if item]
    if source not in sources:
        sources.append(source)
    row["data_sources"] = "; ".join(sources)


def _remove_warning(row: dict[str, Any], warning: str) -> None:
    row["warnings"] = "; ".join(
        item for item in _clean(row.get("warnings")).split("; ") if item and item != warning
    )


def _days_until(value: Any, *, as_of: date | None) -> str:
    if as_of is None:
        return ""
    value_date = _date_value(value)
    if value_date is None:
        return ""
    return str((value_date - as_of).days)


def _date_value(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = _clean(value)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).date()
    except ValueError:
        try:
            return date.fromisoformat(raw)
        except ValueError:
            return None


def _expanding_text(sql: str, **expanding_values: Sequence[str]):
    statement = text(sql)
    for name, values in expanding_values.items():
        statement = statement.bindparams(bindparam(name, value=tuple(values), expanding=True))
    return statement


def _ceil_decimal(value: Decimal) -> Decimal:
    if value <= 0:
        return Decimal("0")
    return value.to_integral_value(rounding=ROUND_CEILING)


def rounded_order_qty(
    raw_qty: Decimal,
    *,
    min_order_qty: int,
    max_order_qty: int | None,
    order_rounding_rules: Sequence[OrderRoundingRule] = (),
) -> Decimal:
    if raw_qty <= 0:
        return Decimal("0")
    min_qty = Decimal(str(min_order_qty))
    rounded_qty = max(raw_qty, min_qty)
    rule = _order_rounding_rule_for_qty(rounded_qty, order_rounding_rules)
    if rule is not None:
        rounded_qty = _round_up_to_multiple(rounded_qty, Decimal(str(rule.round_to)))
    if max_order_qty is None:
        return rounded_qty
    max_qty = Decimal(str(max_order_qty))
    return min(rounded_qty, max_qty)


def _round_up_to_multiple(value: Decimal, multiple: Decimal) -> Decimal:
    if value <= 0 or multiple <= 0:
        return Decimal("0")
    return (value / multiple).to_integral_value(rounding=ROUND_CEILING) * multiple


def _order_rounding_rule_for_qty(
    raw_qty: Decimal,
    rules: Sequence[OrderRoundingRule],
) -> OrderRoundingRule | None:
    matching = [rule for rule in rules if raw_qty > rule.threshold_gt]
    if not matching:
        return None
    return max(matching, key=lambda rule: rule.threshold_gt)


def _order_rounding_rule_text(rule: OrderRoundingRule | None) -> str:
    if rule is None:
        return ""
    return f">{_out_decimal(rule.threshold_gt)} -> {rule.round_to}"


def _order_rounding_note(rule: OrderRoundingRule | None) -> str:
    if rule is None:
        return ""
    return (
        f"Округлено вверх по правилу: больше {_out_decimal(rule.threshold_gt)} шт. "
        f"- до кратности {rule.round_to}."
    )


def _decimal(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _out_decimal(value: Decimal, *, places: int = 3) -> str:
    if not isinstance(value, Decimal):
        value = _decimal(value)
    if places <= 0:
        quant = Decimal("1")
    else:
        quant = Decimal("1").scaleb(-places)
    text_value = format(value.quantize(quant), "f")
    if "." in text_value:
        text_value = text_value.rstrip("0").rstrip(".")
    return "0" if text_value in {"", "-0"} else text_value


def _date_text(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    return _clean(value)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().casefold() in {"1", "true", "yes", "y", "да"}


def _int_value(value: Any, default: int) -> int:
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise SystemExit(f"integer value expected, got: {value!r}") from exc


def _optional_int_value(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise SystemExit(f"integer value expected, got: {value!r}") from exc


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise SystemExit(f"list of strings expected, got: {value!r}")
    return tuple(str(item).strip() for item in value if str(item).strip())


def _decimal_value(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    if value in (None, ""):
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise SystemExit(f"decimal value expected, got: {value!r}") from exc


def _demand_uplift_rules(value: Any) -> tuple[DemandUpliftRule, ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise SystemExit(f"demand_uplift_rules must be a list, got: {value!r}")
    rules: list[DemandUpliftRule] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            raise SystemExit(f"demand uplift rule must be an object, got: {raw!r}")
        rules.append(
            DemandUpliftRule(
                rule_id=_clean(raw.get("rule_id") or raw.get("id")),
                match_any_analog_model_tokens=_string_tuple(
                    raw.get("match_any_analog_model_tokens") or raw.get("analog_model_tokens")
                ),
                demand_multiplier=_decimal_value(
                    raw.get("demand_multiplier") or raw.get("multiplier"),
                    Decimal("1"),
                ),
                reason_ru=_clean(raw.get("reason_ru") or raw.get("reason")),
            )
        )
    return tuple(rules)


def _order_rounding_rules(value: Any) -> tuple[OrderRoundingRule, ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise SystemExit(f"order_rounding_rules must be a list, got: {value!r}")
    rules: list[OrderRoundingRule] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            raise SystemExit(f"order rounding rule must be an object, got: {raw!r}")
        rules.append(
            OrderRoundingRule(
                threshold_gt=_decimal_value(raw.get("threshold_gt")),
                round_to=_int_value(raw.get("round_to"), 1),
            )
        )
    return tuple(sorted(rules, key=lambda rule: rule.threshold_gt, reverse=True))


def _speed_horizon_rules(value: Any) -> tuple[SpeedHorizonRule, ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise SystemExit(f"speed_horizon_rules must be a list, got: {value!r}")
    rules: list[SpeedHorizonRule] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            raise SystemExit(f"speed horizon rule must be an object, got: {raw!r}")
        rules.append(
            SpeedHorizonRule(
                tier=_clean(raw.get("tier") or raw.get("name")),
                min_group_avg_daily_sales_qty=_decimal_value(
                    raw.get("min_group_avg_daily_sales_qty")
                    or raw.get("min_avg_daily_sales_qty")
                    or raw.get("min_daily_sales"),
                ),
                max_effective_target_days=_int_value(raw.get("max_effective_target_days"), 0),
                safety_stock_days=_int_value(raw.get("safety_stock_days"), 0),
                review_only=_bool(raw.get("review_only")),
                label_ru=_clean(raw.get("label_ru") or raw.get("label")),
            )
        )
    return tuple(
        sorted(
            rules,
            key=lambda rule: rule.min_group_avg_daily_sales_qty,
            reverse=True,
        )
    )


def _price_batch_rules(value: Any) -> tuple[PriceBatchRule, ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise SystemExit(f"price batch rules must be a list, got: {value!r}")
    rules: list[PriceBatchRule] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            raise SystemExit(f"price batch rule must be an object, got: {raw!r}")
        rules.append(
            PriceBatchRule(
                speed_tier=_clean(raw.get("speed_tier")),
                price_segments=_string_tuple(raw.get("price_segments")),
                minimum_batch_qty=_optional_int_value(raw.get("minimum_batch_qty")),
                max_automatic_excess_coverage_days=_optional_int_value(
                    raw.get("max_automatic_excess_coverage_days")
                ),
                rounding_mode=_clean(raw.get("rounding_mode")),
            )
        )
    return tuple(rules)


def _supported_analog_policy(value: Any) -> SupportedAnalogPolicy:
    if not isinstance(value, Mapping):
        return SupportedAnalogPolicy()
    enabled = _clean(value.get("status")).casefold() == "approved" and _clean(
        value.get("implementation_status")
    ).casefold() not in {"disabled", "documented_not_wired"}
    active_store_count = _int_value(value.get("active_store_count"), 0)
    site_reserve_qty = _int_value(value.get("site_reserve_qty"), 0)
    return SupportedAnalogPolicy(
        enabled=enabled,
        applies_to_statuses=_string_tuple(value.get("applies_to_statuses")),
        active_store_count=active_store_count,
        site_reserve_qty=site_reserve_qty,
        min_network_stock_qty=_int_value(
            value.get("min_network_stock_qty"),
            active_store_count + site_reserve_qty,
        ),
        min_recent_sales_pct_of_store_count=_decimal_value(
            value.get("min_recent_sales_pct_of_store_count"),
            Decimal("10"),
        ),
        max_days_since_last_sale=_int_value(value.get("max_days_since_last_sale"), 180),
    )


def _optional_env_int(*names: str) -> int | None:
    for name in names:
        if name in os.environ and os.environ[name] != "":
            return _int_value(os.environ[name], 0)
    return None


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build read-only auto-order dry-run for confirmed working display SKUs."
    )
    parser.add_argument("--database-url", default="")
    parser.add_argument("--onec-database-url", default="")
    parser.add_argument("--folder", default="дисплеи")
    parser.add_argument(
        "--include-sale-review-candidates",
        action="store_true",
        help=(
            "Include sale-status display rows as review-only analog candidates. "
            "Rows without auto_order_allowed are not sent to Bitrix as auto orders."
        ),
    )
    parser.add_argument(
        "--include-onec-catalog-analog-candidates",
        action="store_true",
        help=(
            "For review reports, enrich analog groups with matching display catalog items "
            "from 1C Reference62. Catalog-only rows are review-only."
        ),
    )
    parser.add_argument(
        "--auto-order-policy-json",
        type=Path,
        default=DEFAULT_POLICY_JSON,
    )
    parser.add_argument(
        "--warehouse-policy-json",
        type=Path,
        default=Path("config/assortment/display-warehouse-policy.json"),
    )
    parser.add_argument(
        "--b2b-customer-demand-csv",
        type=Path,
        default=(
            Path(os.environ["DISPLAY_AUTO_ORDER_B2B_CUSTOMER_DEMAND_CSV"])
            if os.environ.get("DISPLAY_AUTO_ORDER_B2B_CUSTOMER_DEMAND_CSV")
            else None
        ),
        help=(
            "Optional customer-SKU profile for price types 3/4/5. The profile adds "
            "an advisory replacement calculation and never changes the base order quantity."
        ),
    )
    parser.add_argument(
        "--b2b-customer-demand-as-of",
        type=_parse_date,
        default=None,
        help=("Exclusive profile date. If omitted, it is inferred from the profile filename."),
    )
    parser.add_argument("--as-of", type=_parse_date, default=date.today())
    parser.add_argument(
        "--sales-window-days",
        type=int,
        default=_optional_env_int("DISPLAY_AUTO_ORDER_SALES_WINDOW_DAYS"),
    )
    parser.add_argument(
        "--target-days",
        type=int,
        default=_optional_env_int("DISPLAY_AUTO_ORDER_TARGET_DAYS"),
    )
    parser.add_argument(
        "--order-cadence-days",
        type=int,
        default=_optional_env_int("DISPLAY_AUTO_ORDER_ORDER_CADENCE_DAYS"),
    )
    parser.add_argument(
        "--supplier-prepare-days",
        "--supplier-assembly-days",
        dest="supplier_prepare_days",
        type=int,
        default=_optional_env_int(
            "DISPLAY_AUTO_ORDER_SUPPLIER_PREPARE_DAYS",
            "DISPLAY_AUTO_ORDER_SUPPLIER_ASSEMBLY_DAYS",
        ),
    )
    parser.add_argument(
        "--logistics-days",
        "--delivery-days",
        dest="logistics_days",
        type=int,
        default=_optional_env_int(
            "DISPLAY_AUTO_ORDER_LOGISTICS_DAYS",
            "DISPLAY_AUTO_ORDER_DELIVERY_DAYS",
        ),
    )
    parser.add_argument(
        "--supplier-delay-buffer-days",
        type=int,
        default=_optional_env_int("DISPLAY_AUTO_ORDER_SUPPLIER_DELAY_BUFFER_DAYS"),
    )
    parser.add_argument(
        "--receiving-buffer-days",
        type=int,
        default=_optional_env_int("DISPLAY_AUTO_ORDER_RECEIVING_BUFFER_DAYS"),
    )
    parser.add_argument(
        "--safety-stock-days",
        type=int,
        default=_optional_env_int("DISPLAY_AUTO_ORDER_SAFETY_STOCK_DAYS"),
    )
    parser.add_argument(
        "--min-display-qty",
        type=int,
        default=_optional_env_int("DISPLAY_AUTO_ORDER_MIN_DISPLAY_QTY"),
    )
    parser.add_argument(
        "--min-order-qty",
        type=int,
        default=_optional_env_int("DISPLAY_AUTO_ORDER_MIN_ORDER_QTY"),
    )
    parser.add_argument(
        "--max-order-qty",
        type=int,
        default=_optional_env_int("DISPLAY_AUTO_ORDER_MAX_ORDER_QTY"),
    )
    parser.add_argument("--output-csv", type=Path, default=DEFAULT_OUTPUT_CSV)
    parser.add_argument("--output-json", type=Path, default=DEFAULT_OUTPUT_JSON)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"date must be YYYY-MM-DD, got: {value}") from exc


if __name__ == "__main__":
    raise SystemExit(main())
