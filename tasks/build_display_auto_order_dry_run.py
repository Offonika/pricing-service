from __future__ import annotations

import argparse
import csv
import json
import os
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence

from sqlalchemy import bindparam, func, select, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.infrastructure.db.engines import build_engine
from app.services.assortment_lifecycle_classification_store import (
    ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE,
)
from app.services.display_family_order_recommendation import (
    FAMILY_RECOMMENDATION_COLUMNS,
    apply_display_family_order_recommendations,
    display_family_order_recommendation_summary,
)
from app.services.display_family_registry import load_active_display_family_member_contexts
from app.services.display_margin_flow import (
    MarginFlowPolicy,
    build_margin_flow_facts,
    fetch_current_party_costs,
    fetch_point_availability_days,
    fetch_point_gross_sales,
    fetch_point_safe_free_stock,
    fetch_rolling_unit_revenue,
    qualifies_for_margin_flow,
)
from app.services.display_scope_policy import (
    empty_display_scope_audit,
    filter_display_scope_records,
    merge_display_scope_audits,
)
from app.services.onec_stock_availability import merged_interval_days
from app.services.procurement_b2b_customer_demand import (
    B2BSkuDemandProfile,
    load_b2b_customer_demand_profiles,
)
from app.services.query_batching import (
    load_text_mapping_in_batches,
    normalized_text_batches,
)

DEFAULT_SCOPE_EXCLUSIONS_CSV_NAME = "display-auto-order-scope-exclusions.csv"
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
ACTIVE_CUSTOMER_ORDER_STATUS_CODES = frozenset({"sale", "working"})
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
    *FAMILY_RECOMMENDATION_COLUMNS,
    "latest_purchase_price",
    "latest_purchase_price_at",
    "order_rounding_price_group",
    "order_rounding_group_median_price",
    "order_rounding_price_gate",
    "order_rounding_price_gate_ru",
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
    "active_customer_order_qty",
    "active_customer_order_count",
    "order_available_stock_qty",
    "central_stock_qty",
    "total_stock_qty",
    "incoming_qty",
    "incoming_order_count",
    "sales_qty_window",
    "sales_qty_window_medium",
    "sales_qty_window_short",
    "return_qty_window",
    "batch_error_return_qty",
    "batch_error_share_pct",
    "batch_error_suspected",
    "defect_return_qty",
    "defect_share_pct",
    "defect_rate_suspected",
    "net_sales_qty_window",
    "non_marketplace_net_sales_qty",
    "marketplace_net_sales_qty",
    "marketplace_share_pct",
    "sales_doc_count_marketplace",
    "marketplace_order_impact_qty",
    "marketplace_risk_code",
    "marketplace_risk_ru",
    "sales_doc_count",
    "sales_warehouse_count",
    "last_sale_at",
    "sales_speed_trend",
    "days_in_sale_short",
    "days_in_sale_medium",
    "days_in_sale_long",
    "base_avg_daily_sales_qty",
    "avg_daily_sales_qty",
    "margin_flow_qualifies",
    "margin_flow_rule_applied",
    "margin_flow_point_rate_sum",
    "margin_flow_profitability_pct",
    "margin_flow_party_cost_per_unit",
    "margin_flow_gross_sale_qty_180",
    "margin_flow_net_revenue_rub_180",
    "margin_flow_minimum_representation_qty",
    "margin_flow_reliable_incoming_qty",
    "margin_flow_free_stock_qty",
    "margin_flow_data_status",
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
    "distribution_to_shelf_days",
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
    "stockout_guard_triggered",
    "stockout_guard_days_remaining",
    "stockout_guard_required_days",
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
    physical_sales_point_codes: tuple[str, ...] = ()

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
class OrderRoundingPriceGatePolicy:
    """Ценовой гейт округления (решение 2026-08-19).

    Округлять количество разрешено только карточке, чья закупочная цена не выше
    медианы своей ценовой группы. Группа - бренд плюс класс устройства, и
    определяется она по свойствам и названию карточки, а НЕ по папке каталога
    1С: папка это ручная раскладка, её меняют без следа в свойствах. Проверка
    2026-08-19 показала, что папка сегодня чистая (бренд из имени папки есть в
    названии у 78 из 78 спорных карточек), но привязываться к ней всё равно
    нельзя - решение пользователя.
    """

    enabled: bool = False
    min_group_size: int = 10
    small_group_round_to: int = 10

    def validate(self) -> None:
        if self.min_group_size <= 0:
            raise SystemExit("order rounding price gate min_group_size must be positive")
        if self.small_group_round_to <= 0:
            raise SystemExit("order rounding price gate small_group_round_to must be positive")


@dataclass(frozen=True)
class OrderRoundingGate:
    group_label: str = ""
    median_price: Decimal | None = None
    allowed: bool = True
    reason_code: str = ""
    forced_round_to: int | None = None


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
    active_customer_order_max_age_days: int = 30
    target_days: int = 14
    order_cadence_days: int = 0
    supplier_prepare_days: int = 0
    logistics_days: int = 0
    supplier_delay_buffer_days: int = 0
    receiving_buffer_days: int = 0
    distribution_to_shelf_days: int = 0
    safety_stock_days: int = 0
    min_display_qty: int = 0
    min_order_qty: int = 1
    max_order_qty: int | None = None
    include_sale_review_candidates: bool = False
    order_rounding_rules: tuple[OrderRoundingRule, ...] = ()
    order_rounding_price_gate: OrderRoundingPriceGatePolicy = OrderRoundingPriceGatePolicy()
    speed_horizon_rules: tuple[SpeedHorizonRule, ...] = ()
    onec_catalog_analog_candidate_model_tokens: tuple[str, ...] = ()
    demand_uplift_rules: tuple[DemandUpliftRule, ...] = ()
    price_batch_rules: tuple[PriceBatchRule, ...] = ()
    price_batch_applies_to_statuses: tuple[str, ...] = ()
    price_batch_applies_to_analog_roles: tuple[str, ...] = ()
    supported_analog_policy: SupportedAnalogPolicy = SupportedAnalogPolicy()
    margin_flow_policy: MarginFlowPolicy = MarginFlowPolicy()

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
            + self.distribution_to_shelf_days
        )

    @property
    def effective_target_days(self) -> int:
        return self.planning_horizon_days + self.safety_stock_days

    def with_overrides(self, **overrides: int | None) -> AutoOrderPolicy:
        values = {
            "sales_window_days": self.sales_window_days,
            "active_customer_order_max_age_days": self.active_customer_order_max_age_days,
            "target_days": self.target_days,
            "order_cadence_days": self.order_cadence_days,
            "supplier_prepare_days": self.supplier_prepare_days,
            "logistics_days": self.logistics_days,
            "supplier_delay_buffer_days": self.supplier_delay_buffer_days,
            "receiving_buffer_days": self.receiving_buffer_days,
            "distribution_to_shelf_days": self.distribution_to_shelf_days,
            "safety_stock_days": self.safety_stock_days,
            "min_display_qty": self.min_display_qty,
            "min_order_qty": self.min_order_qty,
            "max_order_qty": self.max_order_qty,
            "include_sale_review_candidates": self.include_sale_review_candidates,
            "order_rounding_rules": self.order_rounding_rules,
            "order_rounding_price_gate": self.order_rounding_price_gate,
            "speed_horizon_rules": self.speed_horizon_rules,
            "onec_catalog_analog_candidate_model_tokens": (
                self.onec_catalog_analog_candidate_model_tokens
            ),
            "demand_uplift_rules": self.demand_uplift_rules,
            "price_batch_rules": self.price_batch_rules,
            "price_batch_applies_to_statuses": self.price_batch_applies_to_statuses,
            "price_batch_applies_to_analog_roles": self.price_batch_applies_to_analog_roles,
            "supported_analog_policy": self.supported_analog_policy,
            "margin_flow_policy": self.margin_flow_policy,
        }
        values.update({key: value for key, value in overrides.items() if value is not None})
        return AutoOrderPolicy(**values)

    def validate(self) -> None:
        if self.sales_window_days <= 0:
            raise SystemExit("sales_window_days must be positive")
        if self.active_customer_order_max_age_days <= 0:
            raise SystemExit("active_customer_order_max_age_days must be positive")
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
        self.order_rounding_price_gate.validate()
        for rule in self.speed_horizon_rules:
            rule.validate()
        for rule in self.price_batch_rules:
            rule.validate()
        self.supported_analog_policy.validate()
        self.margin_flow_policy.validate()
        for field_name in [
            "order_cadence_days",
            "supplier_prepare_days",
            "logistics_days",
            "supplier_delay_buffer_days",
            "receiving_buffer_days",
            "distribution_to_shelf_days",
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
        distribution_to_shelf_days=args.distribution_to_shelf_days,
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
    app_engine = build_engine(database_url, pool_pre_ping=True)
    try:
        items, run_id, scope_policy_audit, scope_gate_audit = (
            load_auto_order_items_with_scope_audit(
                app_engine,
                folder=args.folder,
                include_sale_review_candidates=(
                    args.include_sale_review_candidates
                    or auto_order_policy.include_sale_review_candidates
                ),
            )
        )
    finally:
        app_engine.dispose()

    policy = load_warehouse_policy(args.warehouse_policy_json)
    if auto_order_policy.margin_flow_policy.enabled:
        if (
            len(policy.physical_sales_point_codes)
            != auto_order_policy.margin_flow_policy.physical_store_count
        ):
            raise SystemExit("margin flow physical store count does not match warehouse policy")
        if (
            auto_order_policy.margin_flow_policy.central_warehouse_code
            in policy.physical_sales_point_codes
        ):
            raise SystemExit("margin flow central warehouse must not be a physical store")
    source_errors: dict[str, str] = {}
    facts = {
        "stock": {},
        "reserve": {},
        "customer_orders": {},
        "incoming": {},
        "sales": {},
        "returns": {},
        "purchase": {},
        "margin_flow": {},
    }
    margin_flow_point_sales: dict[str, dict[str, dict[int, Decimal]]] = {}
    margin_flow_party_costs: dict[str, Decimal] = {}
    margin_flow_revenue: dict[str, dict[str, Decimal]] = {}
    margin_flow_free_stock: dict[str, dict[str, Any]] = {}
    margin_flow_codes: tuple[str, ...] = ()
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
        onec_engine = build_engine(onec_database_url, pool_pre_ping=True)
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
            expanded_scope_result = filter_display_scope_records(items)
            items = list(expanded_scope_result.included)
            scope_policy_audit = merge_display_scope_audits(
                scope_policy_audit,
                expanded_scope_result.audit,
            )
            codes = tuple(str(item["nomenclature_code"]) for item in items)
            margin_flow_codes = tuple(
                str(item["nomenclature_code"])
                for item in items
                if _clean(item.get("status")).casefold()
                == auto_order_policy.margin_flow_policy.status_code.casefold()
            )
            facts["stock"] = fetch_stock_totals(onec_engine, codes=codes, policy=policy)
            facts["reserve"] = fetch_reserved_totals(onec_engine, codes=codes, policy=policy)
            facts["customer_orders"] = fetch_active_customer_order_totals(
                onec_engine,
                codes=codes,
                as_of=args.as_of,
                max_age_days=auto_order_policy.active_customer_order_max_age_days,
            )
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
            margin_locations = (
                *policy.physical_sales_point_codes,
                auto_order_policy.margin_flow_policy.central_warehouse_code,
            )
            if auto_order_policy.margin_flow_policy.enabled:
                try:
                    margin_flow_point_sales = fetch_point_gross_sales(
                        onec_engine,
                        codes=margin_flow_codes,
                        warehouse_codes=margin_locations,
                        as_of=args.as_of,
                    )
                    margin_flow_party_costs = fetch_current_party_costs(
                        onec_engine,
                        codes=margin_flow_codes,
                    )
                    margin_flow_revenue = fetch_rolling_unit_revenue(
                        onec_engine,
                        codes=margin_flow_codes,
                        as_of=args.as_of,
                        history_days=auto_order_policy.sales_window_days,
                    )
                    margin_flow_free_stock = fetch_point_safe_free_stock(
                        onec_engine,
                        codes=margin_flow_codes,
                        warehouse_codes=margin_locations,
                        quality_names=policy.usable_stock_quality_names,
                    )
                except Exception as exc:  # noqa: BLE001 - fail closed for this rule only.
                    source_errors["margin_flow_onec"] = f"{type(exc).__name__}: {exc}"
        except (
            Exception
        ) as exc:  # noqa: BLE001 - report must stay read-only and explain source gaps.
            source_errors["onec"] = f"{type(exc).__name__}: {exc}"
        finally:
            onec_engine.dispose()

        days_in_sale_engine = build_engine(database_url, pool_pre_ping=True)
        try:
            facts["days_in_sale"] = fetch_days_in_sale_totals(
                days_in_sale_engine,
                codes=codes,
                physical_sales_point_codes=policy.sellable_codes,
                date_to=args.as_of,
                windows_days=(
                    TREND_WINDOW_SHORT_DAYS,
                    TREND_WINDOW_MEDIUM_DAYS,
                    auto_order_policy.sales_window_days,
                ),
            )
            if auto_order_policy.margin_flow_policy.enabled:
                margin_locations = (
                    *policy.physical_sales_point_codes,
                    auto_order_policy.margin_flow_policy.central_warehouse_code,
                )
                margin_flow_availability = fetch_point_availability_days(
                    days_in_sale_engine,
                    codes=margin_flow_codes,
                    warehouse_codes=margin_locations,
                    as_of=args.as_of,
                )
                facts["margin_flow"] = build_margin_flow_facts(
                    codes=margin_flow_codes,
                    warehouse_codes=margin_locations,
                    point_sales=margin_flow_point_sales,
                    point_availability=margin_flow_availability,
                    party_costs=margin_flow_party_costs,
                    rolling_revenue=margin_flow_revenue,
                    point_free_stock=margin_flow_free_stock,
                )
        except (
            Exception
        ) as exc:  # noqa: BLE001 - days-in-sale correction is best-effort, must not block dry-run.
            source_errors["days_in_sale"] = f"{type(exc).__name__}: {exc}"
        finally:
            days_in_sale_engine.dispose()
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
        distribution_to_shelf_days=auto_order_policy.distribution_to_shelf_days,
        safety_stock_days=auto_order_policy.safety_stock_days,
        min_display_qty=auto_order_policy.min_display_qty,
        min_order_qty=auto_order_policy.min_order_qty,
        sales_window_days=auto_order_policy.sales_window_days,
        max_order_qty=auto_order_policy.max_order_qty,
        order_rounding_rules=auto_order_policy.order_rounding_rules,
        order_rounding_price_gate=auto_order_policy.order_rounding_price_gate,
        speed_horizon_rules=auto_order_policy.speed_horizon_rules,
        margin_flow_policy=auto_order_policy.margin_flow_policy,
        demand_uplift_rules=auto_order_policy.demand_uplift_rules,
        price_batch_rules=auto_order_policy.price_batch_rules,
        price_batch_applies_to_statuses=auto_order_policy.price_batch_applies_to_statuses,
        price_batch_applies_to_analog_roles=(auto_order_policy.price_batch_applies_to_analog_roles),
        supported_analog_policy=auto_order_policy.supported_analog_policy,
        b2b_customer_demand_profiles=b2b_customer_demand_profiles,
        b2b_customer_demand_error=b2b_customer_demand_error,
        as_of=args.as_of,
    )
    if args.use_active_display_family_registry:
        family_registry_error = ""
        membership_by_code = {}
        family_engine = build_engine(database_url, pool_pre_ping=True)
        try:
            with Session(family_engine) as family_session:
                membership_by_code = load_active_display_family_member_contexts(
                    family_session,
                    nomenclature_codes=[str(row.get("nomenclature_code") or "") for row in rows],
                )
        except Exception as exc:  # noqa: BLE001 - shadow output must explain registry gaps.
            family_registry_error = f"{type(exc).__name__}: {exc}"
            source_errors["display_family_registry"] = family_registry_error
        finally:
            family_engine.dispose()
        apply_display_family_order_recommendations(
            rows,
            membership_by_code=membership_by_code,
            registry_error=family_registry_error,
        )
    write_csv(args.output_csv, rows)
    scope_gate_csv = write_scope_gate_csv(
        args.scope_exclusions_csv or args.output_csv.parent / DEFAULT_SCOPE_EXCLUSIONS_CSV_NAME,
        scope_gate_audit.get("exclusions") or (),
    )
    summary = build_summary(
        rows,
        run_id=run_id,
        source_errors=source_errors,
        scope_policy_audit=scope_policy_audit,
        scope_gate_audit=scope_gate_audit,
    )
    summary["auto_order_scope_gate"]["output_csv"] = str(scope_gate_csv)
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


# Журнал выпавших карточек (раздел "Границы и риск-гейты",
# docs/specs/assortment-status-contour-plan.md): условия скоупа раньше стояли в
# SQL WHERE, поэтому живая карточка с продажами исчезала из расчёта молча - её
# не было ни в CSV, ни в summary. Гейт вычисляется в Python, а каждая отсечённая
# карточка попадает в аудит с причиной. Набор условий и их строгость (`is False`
# / `is True`) повторяют прежний SQL один в один: расчёт заказа не меняется.
AUTO_ORDER_SCOPE_GATE_VERSION = "display_auto_order_scope_gate.v1"
AUTO_ORDER_SCOPE_GATE_ELIGIBLE_STATUSES = ("working", "sale")
AUTO_ORDER_SCOPE_GATE_DEMAND_METHOD_CODE = "available_days_average"
AUTO_ORDER_SCOPE_GATE_KA_MAPPING_STATUS = "ready"

GATE_STATUS_NOT_ELIGIBLE = "gate_status_not_eligible"
GATE_DEMAND_METHOD_NOT_SUPPORTED = "gate_demand_method_not_supported"
GATE_FUTURE_KA_MAPPING_NOT_READY = "gate_future_ka_mapping_not_ready"
GATE_MANUAL_REVIEW_REQUIRED = "gate_manual_review_required"
GATE_AUTO_ORDER_NOT_ALLOWED = "gate_auto_order_not_allowed"
GATE_MISSING_IN_LATEST_RUN = "gate_missing_in_latest_classification_run"

# Порядок задаёт основную причину для карточки, нарушившей несколько условий:
# сначала то, что лечится настройкой карточки, потом технические признаки.
AUTO_ORDER_SCOPE_GATE_PRIORITY = (
    GATE_STATUS_NOT_ELIGIBLE,
    GATE_DEMAND_METHOD_NOT_SUPPORTED,
    GATE_FUTURE_KA_MAPPING_NOT_READY,
    GATE_MANUAL_REVIEW_REQUIRED,
    GATE_AUTO_ORDER_NOT_ALLOWED,
    GATE_MISSING_IN_LATEST_RUN,
)

AUTO_ORDER_SCOPE_GATE_LABELS_RU = {
    GATE_STATUS_NOT_ELIGIBLE: "статус карточки вне автозаказа",
    GATE_DEMAND_METHOD_NOT_SUPPORTED: "способ расчёта спроса не поддержан автозаказом",
    GATE_FUTURE_KA_MAPPING_NOT_READY: "карточка не готова к переносу в КА 2",
    GATE_MANUAL_REVIEW_REQUIRED: "карточка помечена ручной проверкой",
    GATE_AUTO_ORDER_NOT_ALLOWED: "автозаказ по карточке запрещён классификацией",
    GATE_MISSING_IN_LATEST_RUN: "карточки нет в последнем прогоне классификации",
}

AUTO_ORDER_SCOPE_GATE_CSV_COLUMNS = (
    "nomenclature_code",
    "article",
    "name",
    "gate_reason_code",
    "gate_reason_label_ru",
    "gate_reason_codes",
    "status",
    "status_label",
    "demand_method_code",
    "demand_method_reason",
    "future_ka_mapping_status",
    "missing_required_attributes",
    "manual_review_required",
    "auto_order_allowed",
    "last_run_id",
)


def auto_order_scope_gate_reasons(
    record: Mapping[str, Any],
    *,
    include_sale_review_candidates: bool = False,
) -> tuple[str, ...]:
    """Причины, по которым карточка не попадает в расчёт автозаказа."""

    reasons: set[str] = set()
    status = str(record.get("status") or "")
    if include_sale_review_candidates:
        if status not in AUTO_ORDER_SCOPE_GATE_ELIGIBLE_STATUSES:
            reasons.add(GATE_STATUS_NOT_ELIGIBLE)
        if record.get("manual_review_required") is not False:
            reasons.add(GATE_MANUAL_REVIEW_REQUIRED)
    else:
        if status != "working":
            reasons.add(GATE_STATUS_NOT_ELIGIBLE)
        if record.get("auto_order_allowed") is not True:
            reasons.add(GATE_AUTO_ORDER_NOT_ALLOWED)
    if str(record.get("future_ka_mapping_status") or "") != AUTO_ORDER_SCOPE_GATE_KA_MAPPING_STATUS:
        reasons.add(GATE_FUTURE_KA_MAPPING_NOT_READY)
    if str(record.get("demand_method_code") or "") != AUTO_ORDER_SCOPE_GATE_DEMAND_METHOD_CODE:
        reasons.add(GATE_DEMAND_METHOD_NOT_SUPPORTED)
    return tuple(code for code in AUTO_ORDER_SCOPE_GATE_PRIORITY if code in reasons)


def _auto_order_scope_gate_exclusion(
    record: Mapping[str, Any],
    reasons: Sequence[str],
) -> dict[str, Any]:
    missing_attributes = record.get("missing_required_attributes")
    if isinstance(missing_attributes, (list, tuple)):
        missing_attributes = ", ".join(str(value) for value in missing_attributes)
    return {
        "nomenclature_code": str(record.get("nomenclature_code") or ""),
        "article": str(record.get("article") or ""),
        "name": str(record.get("name") or ""),
        "gate_reason_code": reasons[0],
        "gate_reason_label_ru": AUTO_ORDER_SCOPE_GATE_LABELS_RU.get(reasons[0], reasons[0]),
        "gate_reason_codes": ", ".join(reasons),
        "status": str(record.get("status") or ""),
        "status_label": str(record.get("status_label") or ""),
        "demand_method_code": str(record.get("demand_method_code") or ""),
        "demand_method_reason": str(record.get("demand_method_reason") or ""),
        "future_ka_mapping_status": str(record.get("future_ka_mapping_status") or ""),
        "missing_required_attributes": str(missing_attributes or ""),
        "manual_review_required": bool(record.get("manual_review_required")),
        "auto_order_allowed": bool(record.get("auto_order_allowed")),
        "last_run_id": record.get("last_run_id"),
    }


def build_auto_order_scope_gate_audit(
    exclusions: Sequence[Mapping[str, Any]],
    *,
    source_item_count: int,
    included_item_count: int,
    run_id: int | None,
    previous_run_id: int | None = None,
) -> dict[str, Any]:
    reason_counts = Counter(str(row.get("gate_reason_code") or "") for row in exclusions)
    status_counts = Counter(
        str(row.get("status") or "")
        for row in exclusions
        if str(row.get("gate_reason_code") or "") == GATE_STATUS_NOT_ELIGIBLE
    )
    return {
        "scope_gate_version": AUTO_ORDER_SCOPE_GATE_VERSION,
        "run_id": run_id,
        "previous_run_id": previous_run_id,
        "source_item_count": source_item_count,
        "included_item_count": included_item_count,
        "excluded_item_count": len(exclusions),
        "excluded_reason_counts": dict(sorted(reason_counts.items())),
        "excluded_status_counts": dict(sorted(status_counts.items())),
        "exclusions": [dict(row) for row in exclusions],
    }


def _load_rows_missing_from_latest_run(
    conn,
    table,
    *,
    folder: str,
    run_id: int | None,
    present_codes: set[str],
) -> tuple[list[dict[str, Any]], int | None]:
    """Карточки предыдущего прогона, которых больше нет в последнем."""

    if run_id is None:
        return [], None
    previous_run_id = conn.execute(
        select(func.max(table.c.last_run_id)).where(
            table.c.folder.ilike(f"%{folder}%"),
            table.c.last_run_id < run_id,
        )
    ).scalar()
    if previous_run_id is None:
        return [], None
    rows = (
        conn.execute(
            select(table)
            .where(
                table.c.folder.ilike(f"%{folder}%"),
                table.c.last_run_id == previous_run_id,
            )
            .order_by(table.c.nomenclature_code.asc())
        )
        .mappings()
        .all()
    )
    missing_by_code: dict[str, dict[str, Any]] = {}
    for row in rows:
        record = dict(row)
        code = str(record.get("nomenclature_code") or "")
        if not code or code in present_codes or code in missing_by_code:
            continue
        missing_by_code[code] = record
    return list(missing_by_code.values()), previous_run_id


def load_auto_order_items_with_scope_audit(
    engine,
    *,
    folder: str,
    include_sale_review_candidates: bool = False,
) -> tuple[list[dict[str, Any]], int | None, dict[str, Any], dict[str, Any]]:
    table = ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE
    with engine.connect() as conn:
        run_id = conn.execute(
            select(func.max(table.c.last_run_id)).where(table.c.folder.ilike(f"%{folder}%"))
        ).scalar()
        rows = (
            conn.execute(
                select(table)
                .where(
                    table.c.folder.ilike(f"%{folder}%"),
                    table.c.last_run_id == run_id,
                )
                .order_by(table.c.nomenclature_code.asc())
            )
            .mappings()
            .all()
        )
        source_records = [dict(row) for row in rows]
        missing_records, previous_run_id = _load_rows_missing_from_latest_run(
            conn,
            table,
            folder=folder,
            run_id=run_id,
            present_codes={str(record.get("nomenclature_code") or "") for record in source_records},
        )
    eligible_records: list[dict[str, Any]] = []
    gate_exclusions: list[dict[str, Any]] = []
    for record in source_records:
        reasons = auto_order_scope_gate_reasons(
            record,
            include_sale_review_candidates=include_sale_review_candidates,
        )
        if reasons:
            gate_exclusions.append(_auto_order_scope_gate_exclusion(record, reasons))
        else:
            eligible_records.append(record)
    for record in missing_records:
        gate_exclusions.append(
            _auto_order_scope_gate_exclusion(record, (GATE_MISSING_IN_LATEST_RUN,))
        )
    gate_exclusions.sort(key=lambda row: str(row.get("nomenclature_code") or ""))
    scope_result = filter_display_scope_records(eligible_records)
    gate_audit = build_auto_order_scope_gate_audit(
        gate_exclusions,
        source_item_count=len(source_records) + len(missing_records),
        included_item_count=len(scope_result.included),
        run_id=run_id,
        previous_run_id=previous_run_id,
    )
    return list(scope_result.included), run_id, scope_result.audit, gate_audit


def load_auto_order_items(
    engine,
    *,
    folder: str,
    include_sale_review_candidates: bool = False,
) -> tuple[list[dict[str, Any]], int | None]:
    items, run_id, _scope_audit, _gate_audit = load_auto_order_items_with_scope_audit(
        engine,
        folder=folder,
        include_sale_review_candidates=include_sale_review_candidates,
    )
    return items, run_id


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
    physical_sales_points: list[str] = []
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
        if (
            _clean(raw.get("role")) == "physical_sales_point"
            and _bool(raw.get("sells_systematically"))
            and not _bool(raw.get("is_transit"))
            and not _bool(raw.get("is_defect_warehouse"))
            and not _bool(raw.get("is_non_systematic_sale"))
        ):
            physical_sales_points.append(code)
    if not sellable:
        raise SystemExit("warehouse policy has no sellable warehouses")
    return WarehousePolicy(
        usable_stock_quality_names=usable_stock_quality_names,
        sellable_codes=tuple(sellable),
        central_codes=tuple(central),
        defect_codes=tuple(defect),
        transit_codes=tuple(transit),
        non_systematic_codes=tuple(non_systematic),
        physical_sales_point_codes=tuple(physical_sales_points),
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
    active_customer_order_raw = (
        raw.get("active_customer_order_policy")
        if isinstance(raw.get("active_customer_order_policy"), Mapping)
        else {}
    )
    return AutoOrderPolicy(
        sales_window_days=_int_value(raw.get("sales_window_days"), 180),
        active_customer_order_max_age_days=_int_value(
            active_customer_order_raw.get("active_order_max_age_days"), 30
        ),
        target_days=_int_value(raw.get("target_days"), 14),
        order_cadence_days=_int_value(raw.get("order_cadence_days"), 0),
        supplier_prepare_days=_int_value(
            raw.get("supplier_prepare_days") or raw.get("supplier_assembly_days"),
            0,
        ),
        logistics_days=_int_value(raw.get("logistics_days") or raw.get("delivery_days"), 0),
        supplier_delay_buffer_days=_int_value(raw.get("supplier_delay_buffer_days"), 0),
        receiving_buffer_days=_int_value(raw.get("receiving_buffer_days"), 0),
        distribution_to_shelf_days=_int_value(raw.get("distribution_to_shelf_days"), 0),
        safety_stock_days=_int_value(raw.get("safety_stock_days"), 0),
        min_display_qty=_int_value(raw.get("min_display_qty"), 0),
        min_order_qty=_int_value(raw.get("min_order_qty"), 1),
        max_order_qty=_optional_int_value(raw.get("max_order_qty")),
        include_sale_review_candidates=_bool(raw.get("include_sale_review_candidates")),
        order_rounding_rules=_order_rounding_rules(raw.get("order_rounding_rules")),
        order_rounding_price_gate=_order_rounding_price_gate_policy(
            raw.get("order_rounding_price_gate")
        ),
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
        margin_flow_policy=_margin_flow_policy(raw.get("margin_flow_policy")),
    )


# Найдено и подтверждено пользователем 2026-07-31: реквизит "Роль склада"
# (тот же генерик-механизм _InfoRg6309+_Chrc401+_Reference42, что уже
# используется для маркетплейса выше) явно расставлен на ВСЕХ 36 складах в
# базе. Остаток качества "Новый" на складах с ролью "Резерв"/"Производства"/
# "Брак" (Уценка, утерянный карго, оборудование для переклейки и т.п.) не
# готов к продаже, хотя формально проходил старый фильтр только по качеству.
# "Точка продаж"/"Транзит"/"Центральный" остаются в свободном остатке как и
# раньше (подтверждено ранее: товар в пути с центрального склада уже
# практически в наличии).
WAREHOUSE_ROLE_PROPERTY_REF = "0xb55d002590803daf11f182171ce63dc8"
SELLABLE_WAREHOUSE_ROLE_NAMES = ("Точка продаж", "Транзит", "Центральный")


def fetch_stock_totals(
    engine, *, codes: Sequence[str], policy: WarehousePolicy
) -> dict[str, dict[str, Any]]:
    code_batches = normalized_text_batches(codes)
    if not code_batches:
        return {}
    if len(code_batches) > 1:
        return load_text_mapping_in_batches(
            codes,
            lambda batch: fetch_stock_totals(engine, codes=batch, policy=policy),
        )
    codes = code_batches[0]
    sql = _expanding_text(
        f"""
        SELECT
            NULLIF(LTRIM(RTRIM(product._Code)), N'') AS code,
            SUM(CASE WHEN NULLIF(LTRIM(RTRIM(quality._Description)), N'')
                    IN :usable_stock_quality_names
                    AND sellable_role.warehouse_ref IS NOT NULL
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
        LEFT JOIN (
            SELECT DISTINCT role_reg._Fld6310_RRRef AS warehouse_ref
            FROM dbo._InfoRg6309 AS role_reg WITH (NOLOCK)
            JOIN dbo._Reference42 AS role_value WITH (NOLOCK)
                ON role_value._IDRRef = role_reg._Fld6312_RRRef
            WHERE role_reg._Fld6311RRef = {WAREHOUSE_ROLE_PROPERTY_REF}
              AND NULLIF(LTRIM(RTRIM(role_value._Description)), N'')
                  IN :sellable_warehouse_role_names
        ) AS sellable_role
            ON sellable_role.warehouse_ref = warehouse._IDRRef
        WHERE stock._Fld7743 <> 0
          AND stock._Period = :balance_period
          AND NULLIF(LTRIM(RTRIM(product._Code)), N'') IN :codes
        GROUP BY NULLIF(LTRIM(RTRIM(product._Code)), N'')
        """,
        codes=codes,
        usable_stock_quality_names=policy.usable_stock_quality_names,
        sellable_warehouse_role_names=SELLABLE_WAREHOUSE_ROLE_NAMES,
        central_codes=policy.central_codes or ("__none__",),
    ).bindparams(bindparam("balance_period", value=OPEN_SUPPLIER_ORDER_BALANCE_PERIOD))
    with engine.connect() as conn:
        return {_clean(row["code"]): dict(row) for row in conn.execute(sql).mappings()}


def fetch_reserved_totals(
    engine, *, codes: Sequence[str], policy: WarehousePolicy
) -> dict[str, dict[str, Any]]:
    code_batches = normalized_text_batches(codes)
    if not code_batches:
        return {}
    if len(code_batches) > 1:
        return load_text_mapping_in_batches(
            codes,
            lambda batch: fetch_reserved_totals(engine, codes=batch, policy=policy),
        )
    codes = code_batches[0]
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


def fetch_active_customer_order_totals(
    engine,
    *,
    codes: Sequence[str],
    as_of: date,
    max_age_days: int,
) -> dict[str, dict[str, Any]]:
    """Return positive unfulfilled customer-order balances per SKU.

    The 1C register also contains internal store requests represented as
    ``Потребности <магазин>`` customer orders. They are intentionally included:
    the procurement formula treats them as demand commitments and never filters
    counterparties by name.

    Only orders dated within ``max_age_days`` of ``as_of`` count. 1C keeps the
    register clean only inside the transfer-assistant correction window; older
    orders are never closed and accumulate stale balances (58M+ units observed
    on 2026-08-18), so an unbounded read would poison the demand formula.
    """

    code_batches = normalized_text_batches(codes)
    if not code_batches:
        return {}
    if len(code_batches) > 1:
        return load_text_mapping_in_batches(
            codes,
            lambda batch: fetch_active_customer_order_totals(
                engine,
                codes=batch,
                as_of=as_of,
                max_age_days=max_age_days,
            ),
        )
    codes = code_batches[0]
    min_order_at = datetime.combine(as_of - timedelta(days=max_age_days), time.min)
    sql = _expanding_text(
        """
        WITH active_order_balances AS (
            SELECT
                NULLIF(LTRIM(RTRIM(product._Code)), N'') AS code,
                customer_order._IDRRef AS customer_order_ref,
                SUM(CAST(balance._Fld7140 AS decimal(18, 3))) AS open_qty
            FROM dbo._AccumRgT7145 AS balance WITH (NOLOCK)
            JOIN dbo._Reference62 AS product WITH (NOLOCK)
                ON product._IDRRef = balance._Fld7131RRef
            JOIN dbo._Document132 AS customer_order WITH (NOLOCK)
                ON customer_order._IDRRef = balance._Fld7129RRef
            WHERE balance._Period = :balance_period
              AND customer_order._Posted = 0x01
              AND customer_order._Marked = 0x00
              AND customer_order._Date_Time >= :active_order_min_date
              AND NULLIF(LTRIM(RTRIM(product._Code)), N'') IN :codes
            GROUP BY
                NULLIF(LTRIM(RTRIM(product._Code)), N''),
                customer_order._IDRRef
            HAVING SUM(CAST(balance._Fld7140 AS decimal(18, 3))) > 0
        )
        SELECT
            code,
            SUM(open_qty) AS active_customer_order_qty,
            COUNT(*) AS active_customer_order_count
        FROM active_order_balances
        GROUP BY code
        """,
        codes=codes,
    ).bindparams(
        bindparam("balance_period", value=OPEN_SUPPLIER_ORDER_BALANCE_PERIOD),
        bindparam("active_order_min_date", value=min_order_at),
    )
    with engine.connect() as conn:
        return {_clean(row["code"]): dict(row) for row in conn.execute(sql).mappings()}


def fetch_incoming_totals(
    engine,
    *,
    codes: Sequence[str],
    as_of: date,
) -> dict[str, dict[str, Any]]:
    code_batches = normalized_text_batches(codes)
    if not code_batches:
        return {}
    if len(code_batches) > 1:
        return load_text_mapping_in_batches(
            codes,
            lambda batch: fetch_incoming_totals(engine, codes=batch, as_of=as_of),
        )
    codes = code_batches[0]
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


TREND_WINDOW_MEDIUM_DAYS = 90
TREND_WINDOW_SHORT_DAYS = 30

# Решение 2026-07-31 (карточка РБ000064147): строгое "каждое окно чуть
# больше предыдущего" ловило шум на маленьких числах (0.1485/0.1341/0.1161 -
# формально по возрастанию, реально плоско, пользователь свою систему сверил
# - там ускорения не видно). Порог значимости - 20% роста на каждом шаге,
# не любая формальная разница.
ACCELERATING_MIN_GROWTH_MULTIPLIER = Decimal("1.2")

# Раздел 2 assortment-status-legacy-rule-inventory.md +
# procurement-order-auto-order-unified-contour.md ("Разрезы спроса по типу
# покупателя"). Реквизит найден и подтвержден на реальных данных 2026-07-30:
# _Reference54._Fld619RRef (Основной вид деятельности контрагента) ->
# _Reference23, значение "Маркетплейс" = РБ0000021. Продажи через маркетплейс
# идут через ТЕ ЖЕ физические магазины (проверено), поэтому фильтр по
# sellable_codes здесь не убирается - маркетплейс считается внутри того же
# среза точек продаж, не отдельным каналом склада.
MARKETPLACE_ACTIVITY_REF = "0x9e78002590803daf11efe0a59c93966e"

# Пороги из procurement-order-auto-order-unified-contour.md ("Разрезы спроса
# по типу покупателя") и уточнения 2026-07-25 (assortment-status-legacy-
# rule-inventory.md, раздел 2): 30-50% - складываем магазинную и
# маркетплейсную потребность (не выбор), 50-70% и 70%+ - строго ручное
# решение, автосумма не действует.
MARKETPLACE_WATCH_SHARE_PCT = Decimal("10")
MARKETPLACE_MEDIUM_SHARE_PCT = Decimal("30")
MARKETPLACE_HIGH_SHARE_PCT = Decimal("50")
MARKETPLACE_CRITICAL_SHARE_PCT = Decimal("70")
MARKETPLACE_MEDIUM_MIN_DOC_COUNT = 7
MARKETPLACE_WATCH_MIN_ORDER_IMPACT_QTY = Decimal("10")


def fetch_sales_totals(
    engine,
    *,
    codes: Sequence[str],
    sellable_codes: Sequence[str],
    date_from: date,
    date_to: date,
    trend_window_medium_days: int = TREND_WINDOW_MEDIUM_DAYS,
    trend_window_short_days: int = TREND_WINDOW_SHORT_DAYS,
    marketplace_activity_ref: str = MARKETPLACE_ACTIVITY_REF,
) -> dict[str, dict[str, Any]]:
    code_batches = normalized_text_batches(codes)
    if not code_batches:
        return {}
    if len(code_batches) > 1:
        return load_text_mapping_in_batches(
            codes,
            lambda batch: fetch_sales_totals(
                engine,
                codes=batch,
                sellable_codes=sellable_codes,
                date_from=date_from,
                date_to=date_to,
                trend_window_medium_days=trend_window_medium_days,
                trend_window_short_days=trend_window_short_days,
                marketplace_activity_ref=marketplace_activity_ref,
            ),
        )
    codes = code_batches[0]
    window_medium_from = datetime.combine(date_to, time.min) - timedelta(
        days=trend_window_medium_days
    )
    window_short_from = datetime.combine(date_to, time.min) - timedelta(
        days=trend_window_short_days
    )
    sql = _expanding_text(
        f"""
        SELECT
            NULLIF(LTRIM(RTRIM(product._Code)), N'') AS code,
            SUM(CAST(rtu_line._Fld4971 AS decimal(18, 3))) AS sales_qty_window,
            SUM(CASE WHEN rtu._Date_Time >= :window_medium_from
                THEN CAST(rtu_line._Fld4971 AS decimal(18, 3)) ELSE 0 END) AS sales_qty_window_medium,
            SUM(CASE WHEN rtu._Date_Time >= :window_short_from
                THEN CAST(rtu_line._Fld4971 AS decimal(18, 3)) ELSE 0 END) AS sales_qty_window_short,
            SUM(CASE WHEN counterparty._Fld619RRef = {marketplace_activity_ref}
                THEN CAST(rtu_line._Fld4971 AS decimal(18, 3)) ELSE 0 END) AS sales_qty_window_marketplace,
            COUNT(DISTINCT CASE WHEN counterparty._Fld619RRef = {marketplace_activity_ref}
                THEN CONVERT(varchar(34), rtu._IDRRef, 1) END) AS sales_doc_count_marketplace,
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
        JOIN dbo._Reference54 AS counterparty WITH (NOLOCK)
            ON counterparty._IDRRef = rtu._Fld4942RRef
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
        bindparam("window_medium_from", value=window_medium_from),
        bindparam("window_short_from", value=window_short_from),
    )
    with engine.connect() as conn:
        return {_clean(row["code"]): dict(row) for row in conn.execute(sql).mappings()}


# Раздел 5.1 assortment-status-legacy-rule-inventory.md, "ранний триггер
# партийной ошибки" (пересорт/ревизия/версия детали): возвратов качества
# "Новый" >=5 шт за скользящее окно 90 дней И доля этих возвратов от продаж
# за то же окно >=40% -> продажа/автозаказ карточки останавливается
# немедленно. Качество возврата - _Document109_VT1698._Fld1715RRef ->
# _Reference48 (подтверждено на реальных данных: 582505 "Новый" / 166087
# "Брак" по всей базе). Окно для новых карточек без 90 дней истории должно
# быть открытым от даты первой продажи - это упрощение пока считает всех
# по фиксированному 90-дневному окну, открытый вариант не реализован.
BATCH_ERROR_RETURN_QUALITY_NAME = "Новый"
BATCH_ERROR_WINDOW_DAYS = TREND_WINDOW_MEDIUM_DAYS
BATCH_ERROR_MIN_RETURN_QTY = Decimal("5")
BATCH_ERROR_MIN_SHARE_PCT = Decimal("40")

# Настоящий брак - отдельный показатель от раннего триггера пересорта выше.
# Правило 5.1 сознательно смотрит на качество "Новый" (возврат "не подошло" ->
# признак пересорта/неверной ревизии детали). Возвраты качества "Брак" до
# 2026-08-01 не считались нигде вообще: карточка с 11 бракованными из 51
# проданной (РБ000059304) показывала batch_error_share_pct = 0%, то есть
# "претензий нет". Здесь считается именно доля возвратов качества "Брак".
#
# Окно намеренно объявлено отдельной константой, а не ссылкой на
# TREND_WINDOW_MEDIUM_DAYS: окно тренда скорости и окно контроля качества -
# разные по смыслу величины, их изменение не должно тянуть друг друга.
#
# Пороги откалиброваны на реальных данных 2026-08-01 по ВСЕМУ каталогу
# дисплеев (1313 карточек прогона, окно 90 дней): средний уровень брака ~3.6%.
# Матрица срабатываний считалась по сетке порогов, выбран вариант 5% + 5 шт
# (29 карточек из 1313). Связка не произвольная: чем ниже порог доли, тем
# больше должен быть минимум штук, иначе процент считается на слишком малой
# базе. При 5% минимум 5 шт означает базу от 100 продаж - 5 возвратов против
# ожидаемых 3.6 уже осмысленная разница. Вариант 5% + 3 шт отклонён: давал 66
# карточек, из них заметная часть - шум на 60 продажах.
DEFECT_RETURN_QUALITY_NAME = "Брак"
DEFECT_RATE_WINDOW_DAYS = 90
DEFECT_RATE_MIN_RETURN_QTY = Decimal("5")
DEFECT_RATE_MIN_SHARE_PCT = Decimal("5")

# Решение 2026-07-31 (карточки РБ000064721/РБ000057817, раздел 9 assortment-
# status-legacy-rule-inventory.md): у тира "slow" (review_only) заказ
# зануляется БЕЗУСЛОВНО для всей группы, даже если у карточки остаток по
# сети равен нулю - цель (target_stock_qty) при этом всё равно честно
# считается выше по конвейеру, просто результат выбрасывается. Структурный
# пол - 11 активных точек продаж (active_store_count,
# display-warehouse-policy.json) + Сайт (тоже "Точка продаж" в 1С,
# подтверждено скринами) + 1 буфер на СДЭК складе = 13. Карточка ниже этого
# порога получает стартовый заказ по уже посчитанной цели вместо занулени,
# независимо от тира. Второе исключение: если тренд скорости "accelerating"
# (растёт, см. sales_speed_trend) - тоже не зануляем, даже если остаток
# выше 13, карточка не должна тормозиться ручным review, пока спрос реально
# растёт.
STRUCTURAL_FLOOR_QTY = Decimal("13")

# Решение 2026-07-31 (карточка РБ000029831, "гейт Пенсии"): структурный пол
# выше не должен слепо давать стартовый заказ любой медленной карточке ниже
# 13 шт - нужно сначала проверить, БЫЛ ли у неё честный шанс продаваться.
# Если товар реально стоял на полке достаточно дней (days_in_sale_medium >=
# порог) и даже тогда скорость не растёт (не accelerating) - это не
# голодание, это угасающий спрос: карточка не получает автозаказ, уходит на
# ручную проверку как кандидат на статус "Пенсия" ("вывод из активной
# работы", задокументирован в спеках, не реализован как отдельный код-
# статус - см. Changelog). Если честных дней в продаже почти не было
# (карточка реально голодала, как РБ000064721 до этой сессии) - природа
# низкой скорости неизвестна, действует исключение, стартовый заказ
# сохраняется. Порог 15 дней из 90 - примерно две недели реального
# присутствия на полке, достаточно для честного суждения о тренде, не
# закреплён строгим анализом, при необходимости можно скорректировать.
PENSION_CANDIDATE_MIN_DAYS_IN_SALE = Decimal("15")

# Решение 2026-08-09 (возврат предохранителя, который сняли 2026-07-25):
# если за длинное окно товар был на полке меньше этого числа дней, база для
# скорости ненадёжна - поправку наличия не применяем вообще (считаем по
# календарю), а строку с положительным заказом отдаём в ручную проверку.
# Для мягкой поправки наличия такой порог обязателен:
# нормализация по нескольким дням наличия ведёт к заказу неликвида
# ("шапки пролежали год, разошлись за два дня"). Обсуждение - чат
# 2026-08-04; фиксация - assortment-status-legacy-rule-inventory.md, раздел 1.
MIN_RELIABLE_AVAILABILITY_DAYS = Decimal("15")

# off_schedule_signal_policy.stockout_guard, display-auto-order-policy.json:
# "дней свободного остатка меньше ожидаемого оставшегося срока поставки плюс
# 10 дней запаса -> создать внеплановую Потребность на заказ или ручную
# задачу до обычного дня графика". Раньше это было только в JSON, в коде не
# читалось вообще (0 упоминаний off_schedule_signal_policy в этом файле до
# 2026-07-31). Реализовано КАК СИГНАЛ (v1): помечает карточки, где текущий
# расчёт решил "заказ не нужен", а честный остаток запаса времени
# (свободный остаток / скорость) меньше срока полного цикла довоза + буфер -
# то есть решение "не заказывать" рискует обернуться пустой полкой раньше,
# чем придёт следующий заказ. Сознательно НЕ меняет recommended_order_qty
# в этой версии - только явный флаг и текст тревоги, чтобы не повторить
# сценарий двух утечек этой же сессии (правило molча меняющее количество в
# ещё одном месте конвейера). Вопрос "должен ли сигнал ещё и создавать заказ
# сам" - открытый, требует отдельного qty-diff гейта на реальных данных
# перед включением.
STOCKOUT_GUARD_BUFFER_DAYS = 10


# Раздел 1 assortment-status-legacy-rule-inventory.md ("Дни эффективного
# наличия") - методология (честное среднее по реальным точкам продаж,
# onec_stock_availability_interval, НЕ "сеть-любая точка") уже реализована
# и работает в app/services/onec_stock_availability.py, но там report_only -
# используется только assortment_lifecycle_facts.py для отчётности, в сам
# автозаказ (этот файл) никогда не подключалась. Подтверждено вручную на
# РБ000064721 (0/30, 14.2/90, 51.1/180 дней в продаже) и РБ000057817
# 2026-07-31 - без этой поправки скорость занижается для карточек, которые
# просто были без остатка часть окна, а не потеряли реальный спрос. Здесь -
# первое подключение к формуле скорости.
def fetch_days_in_sale_totals(
    engine: Any,
    *,
    codes: Sequence[str],
    physical_sales_point_codes: Sequence[str],
    date_to: date,
    windows_days: Sequence[int],
) -> dict[str, dict[int, Decimal]]:
    # Дни наличия = число дней, когда товар был в продаже хотя бы в одной
    # физической точке. Интервалы точек объединяются (union), а не
    # суммируются.
    #
    # Исправление 2026-08-09. Прежняя версия суммировала точко-дни и делила
    # на число точек ("средние дни по сети"). В одном числе смешивались
    # "сколько дней товар был" и "в скольких точках он лежал": товар, весь
    # период пролежавший в одной точке из одиннадцати, получал W/11 дней и
    # выглядел отсутствующим. На прогоне 2026-08-09 это было видно прямо в
    # данных - ни одна из 1517 карточек не набирала полного окна 180 дней,
    # максимум по каталогу составлял 141 день.
    #
    # Расчёт скорости и потребности отдельно по каждой точке (решение
    # 2026-07-20) этой правкой не выполняется и остаётся отдельной задачей:
    # он требует продаж в разрезе точек, распределения товара в пути и
    # правила "сначала перемещение, затем закупка".
    code_batches = normalized_text_batches(codes)
    if not code_batches or not physical_sales_point_codes:
        return {}
    if len(code_batches) > 1:
        return load_text_mapping_in_batches(
            codes,
            lambda batch: fetch_days_in_sale_totals(
                engine,
                codes=batch,
                physical_sales_point_codes=physical_sales_point_codes,
                date_to=date_to,
                windows_days=windows_days,
            ),
        )
    codes = code_batches[0]
    windows = sorted({int(window) for window in windows_days if int(window) > 0})
    if not windows:
        return {}
    result: dict[str, dict[int, Decimal]] = {code: {} for code in codes}
    earliest_from = date_to - timedelta(days=max(windows) - 1)
    intervals: dict[str, list[tuple[date, date]]] = {}
    sql = text("""
        SELECT product_code, available_from, available_to
        FROM onec_stock_availability_interval
        WHERE product_code IN :codes
          AND warehouse_code IN :warehouse_codes
          AND available_from <= :window_to
          AND available_to >= :window_from
        """).bindparams(
        bindparam("codes", value=tuple(codes), expanding=True),
        bindparam("warehouse_codes", value=tuple(physical_sales_point_codes), expanding=True),
        bindparam("window_from", value=earliest_from),
        bindparam("window_to", value=date_to),
    )
    with engine.connect() as conn:
        for row in conn.execute(sql).mappings():
            code = str(row["product_code"])
            if code not in result:
                continue
            intervals.setdefault(code, []).append((row["available_from"], row["available_to"]))
    for window_days in windows:
        window_from = date_to - timedelta(days=window_days - 1)
        for code, code_intervals in intervals.items():
            clipped = [
                (max(start, window_from), min(end, date_to))
                for start, end in code_intervals
                if end >= window_from and start <= date_to
            ]
            if not clipped:
                continue
            result[code][window_days] = Decimal(str(merged_interval_days(clipped)))
    return result


def fetch_return_totals(
    engine,
    *,
    codes: Sequence[str],
    sellable_codes: Sequence[str],
    date_from: date,
    date_to: date,
    batch_error_window_days: int = BATCH_ERROR_WINDOW_DAYS,
    defect_rate_window_days: int = DEFECT_RATE_WINDOW_DAYS,
) -> dict[str, dict[str, Any]]:
    code_batches = normalized_text_batches(codes)
    if not code_batches:
        return {}
    if len(code_batches) > 1:
        return load_text_mapping_in_batches(
            codes,
            lambda batch: fetch_return_totals(
                engine,
                codes=batch,
                sellable_codes=sellable_codes,
                date_from=date_from,
                date_to=date_to,
                batch_error_window_days=batch_error_window_days,
                defect_rate_window_days=defect_rate_window_days,
            ),
        )
    codes = code_batches[0]
    batch_error_window_from = datetime.combine(date_to, time.min) - timedelta(
        days=batch_error_window_days
    )
    defect_rate_window_from = datetime.combine(date_to, time.min) - timedelta(
        days=defect_rate_window_days
    )
    sql = _expanding_text(
        """
        SELECT
            NULLIF(LTRIM(RTRIM(product._Code)), N'') AS code,
            SUM(CAST(return_line._Fld1701 AS decimal(18, 3))) AS return_qty_window,
            SUM(CASE WHEN customer_return._Date_Time >= :batch_error_window_from
                    AND NULLIF(LTRIM(RTRIM(quality._Description)), N'') = :batch_error_return_quality_name
                THEN CAST(return_line._Fld1701 AS decimal(18, 3)) ELSE 0 END)
                AS batch_error_return_qty,
            SUM(CASE WHEN customer_return._Date_Time >= :defect_rate_window_from
                    AND NULLIF(LTRIM(RTRIM(quality._Description)), N'') = :defect_return_quality_name
                THEN CAST(return_line._Fld1701 AS decimal(18, 3)) ELSE 0 END)
                AS defect_return_qty
        FROM dbo._Document109 AS customer_return WITH (NOLOCK)
        JOIN dbo._Document109_VT1698 AS return_line WITH (NOLOCK)
            ON return_line._Document109_IDRRef = customer_return._IDRRef
        JOIN dbo._Reference62 AS product WITH (NOLOCK)
            ON product._IDRRef = return_line._Fld1700RRef
        JOIN dbo._Reference80 AS warehouse WITH (NOLOCK)
            ON warehouse._IDRRef = return_line._Fld1716RRef
        LEFT JOIN dbo._Reference48 AS quality WITH (NOLOCK)
            ON quality._IDRRef = return_line._Fld1715RRef
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
        bindparam("batch_error_window_from", value=batch_error_window_from),
        bindparam("batch_error_return_quality_name", value=BATCH_ERROR_RETURN_QUALITY_NAME),
        bindparam("defect_rate_window_from", value=defect_rate_window_from),
        bindparam("defect_return_quality_name", value=DEFECT_RETURN_QUALITY_NAME),
    )
    with engine.connect() as conn:
        return {_clean(row["code"]): dict(row) for row in conn.execute(sql).mappings()}


def fetch_latest_purchase_prices(engine, *, codes: Sequence[str]) -> dict[str, dict[str, Any]]:
    code_batches = normalized_text_batches(codes)
    if not code_batches:
        return {}
    if len(code_batches) > 1:
        return load_text_mapping_in_batches(
            codes,
            lambda batch: fetch_latest_purchase_prices(engine, codes=batch),
        )
    codes = code_batches[0]
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
    distribution_to_shelf_days: int = 0,
    safety_stock_days: int = 0,
    min_display_qty: int = 0,
    min_order_qty: int = 1,
    supplier_assembly_days: int = 0,
    delivery_days: int = 0,
    order_rounding_rules: Sequence[OrderRoundingRule] = (),
    order_rounding_price_gate: OrderRoundingPriceGatePolicy | None = None,
    speed_horizon_rules: Sequence[SpeedHorizonRule] = (),
    margin_flow_policy: MarginFlowPolicy | None = None,
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
    scoped_items = filter_display_scope_records(items).included
    # Ценовой гейт округления считается ДО цикла: медиана группы нужна каждой
    # строке, а собрать её можно только по всем карточкам прогона сразу.
    rounding_gates = order_rounding_price_gates(
        scoped_items,
        purchase_facts=facts.get("purchase", {}),
        policy=order_rounding_price_gate or OrderRoundingPriceGatePolicy(),
    )
    supported_analog_policy = supported_analog_policy or SupportedAnalogPolicy()
    margin_flow_policy = margin_flow_policy or MarginFlowPolicy()
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
        + distribution_to_shelf_days
    )
    effective_target_days = planning_horizon_days + safety_stock_days
    for item in scoped_items:
        code = _clean(item.get("nomenclature_code"))
        assortment_status = _clean(item.get("status")).casefold()
        stock = facts.get("stock", {}).get(code, {})
        reserve = facts.get("reserve", {}).get(code, {})
        customer_orders = facts.get("customer_orders", {}).get(code, {})
        incoming = facts.get("incoming", {}).get(code, {})
        sales = facts.get("sales", {}).get(code, {})
        returns = facts.get("returns", {}).get(code, {})
        purchase = facts.get("purchase", {}).get(code, {})
        sellable_stock_qty = _decimal(stock.get("sellable_stock_qty"))
        reserved_qty = _decimal(reserve.get("reserved_qty"))
        free_stock_qty = sellable_stock_qty - reserved_qty
        active_customer_order_qty = (
            _decimal(customer_orders.get("active_customer_order_qty"))
            if assortment_status in ACTIVE_CUSTOMER_ORDER_STATUS_CODES
            else Decimal("0")
        )
        active_customer_order_count = (
            int(customer_orders.get("active_customer_order_count") or 0)
            if assortment_status in ACTIVE_CUSTOMER_ORDER_STATUS_CODES
            else 0
        )
        order_available_stock_qty = (
            sellable_stock_qty - active_customer_order_qty
            if assortment_status in ACTIVE_CUSTOMER_ORDER_STATUS_CODES
            else free_stock_qty
        )
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
        sales_qty_medium = _decimal(sales.get("sales_qty_window_medium"))
        sales_qty_short = _decimal(sales.get("sales_qty_window_short"))
        sales_qty_marketplace = _decimal(sales.get("sales_qty_window_marketplace"))
        sales_doc_count_marketplace = int(sales.get("sales_doc_count_marketplace") or 0)
        return_qty = _decimal(returns.get("return_qty_window"))
        batch_error_return_qty = _decimal(returns.get("batch_error_return_qty"))
        defect_return_qty = _decimal(returns.get("defect_return_qty"))
        latest_purchase_price = _decimal(purchase.get("latest_purchase_price"))
        rounding_gate = rounding_gates.get(code) or OrderRoundingGate()
        row_order_rounding_rules = _gate_order_rounding_rules(rounding_gate, order_rounding_rules)
        # Спрос брутто: возвраты не вычитаются из базы расчёта количества.
        # 82.6% возвратов дисплеев — причина "Не понадобился" (качество "Новый"),
        # не брак; вычитание всех возвратов занижало реальный спрос почти на
        # порядок. Подтверждено диффом на 2144 SKU (2026-07-21): 130213 -> 155835
        # (+19.7%). return_qty остаётся в выводе как отдельная информационная
        # колонка (return_qty_window), просто больше не уменьшает net_sales_qty.
        net_sales_qty = max(Decimal("0"), sales_qty)
        net_sales_qty_medium = max(Decimal("0"), sales_qty_medium)
        net_sales_qty_short = max(Decimal("0"), sales_qty_short)
        # Раздел 5.1: доля возвратов качества "Новый" от продаж за то же
        # 90-дневное окно (BATCH_ERROR_WINDOW_DAYS). Без вычитания возвратов
        # из net_sales_qty (спрос брутто) - это отдельная, самостоятельная
        # проверка, не связана с формулой количества.
        batch_error_share_pct = (
            (batch_error_return_qty / net_sales_qty_medium * Decimal("100"))
            if net_sales_qty_medium > 0
            else Decimal("0")
        )
        batch_error_suspected = (
            batch_error_return_qty >= BATCH_ERROR_MIN_RETURN_QTY
            and batch_error_share_pct >= BATCH_ERROR_MIN_SHARE_PCT
        )
        # Доля возвратов качества "Брак" от продаж за то же окно. Отдельно от
        # batch_error выше: тот считает "Новый" (пересорт), этот - настоящие
        # претензии к качеству товара. Раздел 5.1 спеки требует, чтобы
        # статистически значимый брак уводил строку в Review, а не изображал
        # отсутствие проблемы - с 2026-08-01 сигнал блокирует автозаказ
        # (порог утверждён пользователем на матрице по всему каталогу).
        defect_share_pct = (
            (defect_return_qty / net_sales_qty_medium * Decimal("100"))
            if net_sales_qty_medium > 0
            else Decimal("0")
        )
        defect_rate_suspected = (
            defect_return_qty >= DEFECT_RATE_MIN_RETURN_QTY
            and defect_share_pct >= DEFECT_RATE_MIN_SHARE_PCT
        )
        # Раздел 2 + procurement-order-auto-order-unified-contour.md
        # ("Разрезы спроса по типу покупателя"): маркетплейс-спрос не должен
        # молча доказывать нужность в матрице магазинов. min(...) на случай,
        # если сумма по маркетплейсу почему-то превысит общую (не должно
        # происходить при текущем SQL, но не должно и падать, если произойдёт).
        marketplace_net_sales_qty = min(max(Decimal("0"), sales_qty_marketplace), net_sales_qty)
        non_marketplace_net_sales_qty = net_sales_qty - marketplace_net_sales_qty
        marketplace_share_pct = (
            (marketplace_net_sales_qty / net_sales_qty * Decimal("100"))
            if net_sales_qty > 0
            else Decimal("0")
        )
        # Раздел 1 ("Дни эффективного наличия"): скорость по календарным дням
        # окна занижает спрос, если товара часть окна не было на полке -
        # карточка выглядит "медленной", хотя реально просто голодала
        # (подтверждено на РБ000064721: 0.1222 шт/день по календарю против
        # честных 14.2/90 дней в продаже).
        #
        # Решение 2026-08-09: используется МЯГКАЯ поправка - та же формула,
        # что в app/services/assortment_lifecycle.py и в каноне
        # docs/specs/assortment-lifecycle-policy.md. Дни без товара
        # достраиваются по базовой (календарной) скорости:
        #
        #   base_rate = продажи / окно
        #   virtual   = дни_без_товара * base_rate
        #   rate      = (продажи + virtual) / окно
        #
        # Потолок поправки - x2 к календарной скорости. Прежняя ЖЁСТКАЯ
        # формула (продажи / дни_наличия) отменена: на текущем знаменателе
        # она не компенсирует дефицит, а умножает продажи на число точек, где
        # товара не было (days_in_sale = сумма точко-дней / число точек, см.
        # fetch_days_in_sale_totals) - прогон 2026-08-01 дал рост дневного
        # заказа с 2183 до 39359 шт, медиана x2.06, максимум x180.
        #
        # Знаменатель по-прежнему неточный: расчёт по каждой точке отдельно
        # (решение 2026-07-20) не реализован - отдельная задача, см. раздел 1
        # инвентаризации. Если данных нет (старый вызов/фикстура теста) -
        # откат на календарную скорость, прежнее поведение.
        days_in_sale = facts.get("days_in_sale", {}).get(code, {})
        observed_days_long = days_in_sale.get(sales_window_days)
        availability_history_too_short = (
            observed_days_long is not None and observed_days_long < MIN_RELIABLE_AVAILABILITY_DAYS
        )

        def _availability_rate(
            net_qty: Decimal,
            calendar_days: int,
            *,
            days_in_sale: dict[int, Decimal] = days_in_sale,
            history_too_short: bool = availability_history_too_short,
        ) -> Decimal:
            if calendar_days <= 0:
                return Decimal("0")
            window = Decimal(str(calendar_days))
            base_rate = net_qty / window
            available_days = days_in_sale.get(calendar_days)
            if available_days is None or available_days <= 0:
                return base_rate
            if history_too_short:
                # Меньше MIN_RELIABLE_AVAILABILITY_DAYS дней наблюдения за
                # длинное окно: по такой базе нельзя судить о скорости,
                # виртуальные продажи не достраиваем ни в одном окне.
                return base_rate
            days_without_stock = max(Decimal("0"), window - min(available_days, window))
            virtual_qty = days_without_stock * base_rate
            return (net_qty + virtual_qty) / window

        rate_long = _availability_rate(net_sales_qty, sales_window_days)
        # Раздел 9.1 (п.2) спеки "Дорогие/маржинальные медленные карточки":
        # скорость считаем на трёх окнах 180/90/30 дней и сравниваем тренд.
        # Карточка разгоняется (30д быстрее 90д быстрее 180д) -> берём
        # максимум из трёх (последний месяц). Иначе (тормозит/плоско) ->
        # берём среднее из трёх. Раньше здесь было одно плоское окно
        # sales_window_days (180) без сравнения тренда — занижало скорость
        # для растущих карточек. Найдено и исправлено 2026-07-30 на реальном
        # примере РБ000064965 (май 5 -> июнь 6 -> июль 11 шт).
        # trend_data_available=False (окна 90/30 не переданы источником,
        # например старый вызов/фикстура теста) -> откат к плоскому окну
        # sales_window_days, чтобы не путать "нет данных" с "0 продаж".
        trend_data_available = (
            "sales_qty_window_medium" in sales and "sales_qty_window_short" in sales
        )
        if trend_data_available:
            rate_medium = _availability_rate(net_sales_qty_medium, TREND_WINDOW_MEDIUM_DAYS)
            rate_short = _availability_rate(net_sales_qty_short, TREND_WINDOW_SHORT_DAYS)
            accelerating = (
                rate_short >= rate_medium * ACCELERATING_MIN_GROWTH_MULTIPLIER
                and rate_medium >= rate_long * ACCELERATING_MIN_GROWTH_MULTIPLIER
            )
            base_avg_daily_sales_qty = (
                max(rate_short, rate_medium, rate_long)
                if accelerating
                else (rate_short + rate_medium + rate_long) / 3
            )
        else:
            accelerating = False
            base_avg_daily_sales_qty = rate_long
        demand_rule = _demand_uplift_rule_for_item(item, demand_uplift_rules)
        demand_multiplier = demand_rule.demand_multiplier if demand_rule else Decimal("1")
        adjusted_net_sales_qty = net_sales_qty * demand_multiplier
        avg_daily_sales_qty = base_avg_daily_sales_qty * demand_multiplier
        margin_flow = facts.get("margin_flow", {}).get(code, {})
        margin_flow_rate = _decimal(margin_flow.get("point_rate_sum"))
        margin_flow_profitability = _optional_decimal(margin_flow.get("profitability_pct"))
        margin_flow_qualifies = qualifies_for_margin_flow(
            status_code=_clean(item.get("status")),
            point_rate_sum=margin_flow_rate,
            profitability_pct=margin_flow_profitability,
            policy=margin_flow_policy,
        )
        margin_flow_data_status = (
            "ready"
            if margin_flow_profitability is not None
            else "missing_party_cost_or_revenue" if margin_flow_policy.enabled else "disabled"
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
        raw_order_qty = max(
            Decimal("0"),
            target_stock_qty - order_available_stock_qty - incoming_qty,
        )
        recommended_order_qty_raw = _ceil_decimal(raw_order_qty)
        recommended_order_qty = rounded_order_qty(
            recommended_order_qty_raw,
            min_order_qty=min_order_qty,
            max_order_qty=max_order_qty,
            order_rounding_rules=row_order_rounding_rules,
        )
        order_rounding_rule = _order_rounding_rule_for_qty(
            recommended_order_qty_raw,
            row_order_rounding_rules,
        )
        marketplace_has_exposure = total_stock_qty > 0 or incoming_qty > 0
        non_marketplace_target_stock_qty = (
            target_stock_qty * (non_marketplace_net_sales_qty / net_sales_qty)
            if net_sales_qty > 0
            else target_stock_qty
        )
        marketplace_order_impact_qty = recommended_order_qty_raw - _ceil_decimal(
            max(
                Decimal("0"),
                non_marketplace_target_stock_qty - order_available_stock_qty - incoming_qty,
            )
        )
        marketplace_risk_code, marketplace_risk_ru = _classify_marketplace_risk(
            net_sales_qty=net_sales_qty,
            marketplace_net_sales_qty=marketplace_net_sales_qty,
            marketplace_share_pct=marketplace_share_pct,
            marketplace_doc_count=sales_doc_count_marketplace,
            has_exposure=marketplace_has_exposure,
            order_impact_qty=marketplace_order_impact_qty,
        )
        blockers: list[str] = []
        warnings: list[str] = []
        if source_errors:
            blockers.append("source_error")
        if free_stock_qty < 0:
            warnings.append("reserve_more_than_sellable_stock")
        if active_customer_order_qty > 0:
            warnings.append("active_customer_orders_added_to_need")
        if order_available_stock_qty < 0:
            warnings.append("active_customer_orders_exceed_sellable_stock")
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
        if marketplace_risk_code:
            warnings.append(marketplace_risk_code)
        if marketplace_risk_code in (
            "critical_marketplace_refusal_nonliquid_risk",
            "high_marketplace_refusal_risk",
        ):
            # Раздел 2 / procurement-order-auto-order-unified-contour.md: при
            # доле маркетплейса 50%+ (или обычного спроса нет) автозаказ
            # останавливается - решение только ручное, не автосумма.
            recommended_order_qty_raw = Decimal("0")
            recommended_order_qty = Decimal("0")
        if batch_error_suspected:
            # Раздел 5.1: ранний триггер партийной ошибки (пересорт/ревизия/
            # версия детали) - >=5 шт возвратов качества "Новый" за 90 дней И
            # доля этих возвратов от продаж за то же окно >=40%. Продажа/
            # автозаказ карточки останавливается немедленно, задача в Bitrix
            # закупщику - срочная (создание самой Bitrix-задачи - отдельный
            # sync-шаг, не этот dry-run).
            blockers.append("batch_error_suspected")
            recommended_order_qty_raw = Decimal("0")
            recommended_order_qty = Decimal("0")
        if defect_rate_suspected:
            # Раздел 5.1: подтверждённый брак выше порога снимает карточку с
            # автозаказа и уводит в ручную проверку - как и пересорт выше.
            # Отдельный блокер, не сливать с batch_error: причины разные
            # (качество товара против неверной ревизии), решения тоже.
            blockers.append("defect_rate_suspected")
            recommended_order_qty_raw = Decimal("0")
            recommended_order_qty = Decimal("0")
        decision = (
            "manual_review"
            if blockers
            or marketplace_risk_code
            in (
                "critical_marketplace_refusal_nonliquid_risk",
                "high_marketplace_refusal_risk",
            )
            else "order" if recommended_order_qty > 0 else "do_not_order"
        )
        reason = _reason(
            decision=decision,
            recommended_order_qty=recommended_order_qty,
            recommended_order_qty_raw=recommended_order_qty_raw,
            target_stock_qty=target_stock_qty,
            free_stock_qty=order_available_stock_qty,
            incoming_qty=incoming_qty,
            net_sales_qty=net_sales_qty,
            target_days=target_days,
            order_cadence_days=order_cadence_days,
            supplier_prepare_days=supplier_prepare_days,
            logistics_days=logistics_days,
            supplier_delay_buffer_days=supplier_delay_buffer_days,
            receiving_buffer_days=receiving_buffer_days,
            distribution_to_shelf_days=distribution_to_shelf_days,
            safety_stock_days=safety_stock_days,
            effective_target_days=effective_target_days,
            sales_window_days=sales_window_days,
            demand_adjustment_reason_ru=demand_rule.reason_ru if demand_rule else "",
            demand_adjustment_multiplier=demand_multiplier,
            order_rounding_rule=order_rounding_rule,
            blockers=blockers,
            warnings=warnings,
        )
        if marketplace_risk_code in (
            "critical_marketplace_refusal_nonliquid_risk",
            "high_marketplace_refusal_risk",
        ):
            reason = marketplace_risk_ru
        if batch_error_suspected:
            reason = (
                f"ТРЕВОГА: подозрение на партийную ошибку (пересорт/ревизия/версия "
                f"детали) - {_out_decimal(batch_error_return_qty)} шт возвратов "
                f"качества «Новый» за {BATCH_ERROR_WINDOW_DAYS} дней, "
                f"{_out_decimal(batch_error_share_pct, places=1)}% от продаж за то "
                f"же окно. Автозаказ остановлен, нужна срочная проверка поставщика."
            )
        if defect_rate_suspected:
            warnings.append("defect_rate_above_threshold")
            reason = (
                f"ТРЕВОГА (качество): {_out_decimal(defect_return_qty)} шт возвратов "
                f"качества «Брак» за {DEFECT_RATE_WINDOW_DAYS} дней, "
                f"{_out_decimal(defect_share_pct, places=1)}% от продаж за то же окно "
                f"(порог {_out_decimal(DEFECT_RATE_MIN_SHARE_PCT)}% и "
                f"{_out_decimal(DEFECT_RATE_MIN_RETURN_QTY)} шт). Автозаказ остановлен, "
                f"нужна проверка партии и поставщика."
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
                "order_rounding_price_group": rounding_gate.group_label,
                "order_rounding_group_median_price": (
                    _out_decimal(rounding_gate.median_price, places=2)
                    if rounding_gate.median_price is not None
                    else ""
                ),
                "order_rounding_price_gate": rounding_gate.reason_code,
                "order_rounding_price_gate_ru": ORDER_ROUNDING_GATE_LABELS_RU.get(
                    rounding_gate.reason_code, ""
                ),
                "_order_rounding_allowed": rounding_gate.allowed,
                "_order_rounding_forced_round_to": rounding_gate.forced_round_to,
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
                "active_customer_order_qty": _out_decimal(active_customer_order_qty),
                "active_customer_order_count": active_customer_order_count,
                "order_available_stock_qty": _out_decimal(order_available_stock_qty),
                "central_stock_qty": _out_decimal(central_stock_qty),
                "total_stock_qty": _out_decimal(total_stock_qty),
                "incoming_qty": _out_decimal(incoming_qty),
                "incoming_order_count": int(incoming.get("incoming_order_count") or 0),
                "sales_qty_window": _out_decimal(sales_qty),
                "sales_qty_window_medium": _out_decimal(sales_qty_medium),
                "sales_qty_window_short": _out_decimal(sales_qty_short),
                "return_qty_window": _out_decimal(return_qty),
                "batch_error_return_qty": _out_decimal(batch_error_return_qty),
                "batch_error_share_pct": _out_decimal(batch_error_share_pct, places=1),
                "batch_error_suspected": "yes" if batch_error_suspected else "",
                "defect_return_qty": _out_decimal(defect_return_qty),
                "defect_share_pct": _out_decimal(defect_share_pct, places=1),
                "defect_rate_suspected": "yes" if defect_rate_suspected else "",
                "net_sales_qty_window": _out_decimal(net_sales_qty),
                "non_marketplace_net_sales_qty": _out_decimal(non_marketplace_net_sales_qty),
                "marketplace_net_sales_qty": _out_decimal(marketplace_net_sales_qty),
                "marketplace_share_pct": _out_decimal(marketplace_share_pct, places=1),
                "sales_doc_count_marketplace": sales_doc_count_marketplace,
                "marketplace_order_impact_qty": _out_decimal(marketplace_order_impact_qty),
                "marketplace_risk_code": marketplace_risk_code,
                "marketplace_risk_ru": marketplace_risk_ru,
                "sales_doc_count": int(sales.get("sales_doc_count") or 0),
                "sales_warehouse_count": int(sales.get("sales_warehouse_count") or 0),
                "last_sale_at": _date_text(sales.get("last_sale_at")),
                "sales_speed_trend": (
                    "accelerating"
                    if accelerating
                    else "flat_or_slowing" if trend_data_available else "n/a_flat_window_fallback"
                ),
                "days_in_sale_short": (
                    _out_decimal(days_in_sale[TREND_WINDOW_SHORT_DAYS], places=1)
                    if TREND_WINDOW_SHORT_DAYS in days_in_sale
                    else ""
                ),
                "days_in_sale_medium": (
                    _out_decimal(days_in_sale[TREND_WINDOW_MEDIUM_DAYS], places=1)
                    if TREND_WINDOW_MEDIUM_DAYS in days_in_sale
                    else ""
                ),
                "days_in_sale_long": (
                    _out_decimal(days_in_sale[sales_window_days], places=1)
                    if sales_window_days in days_in_sale
                    else ""
                ),
                "base_avg_daily_sales_qty": _out_decimal(
                    base_avg_daily_sales_qty,
                    places=4,
                ),
                "avg_daily_sales_qty": _out_decimal(avg_daily_sales_qty, places=4),
                "margin_flow_qualifies": "yes" if margin_flow_qualifies else "",
                "margin_flow_rule_applied": "",
                "margin_flow_point_rate_sum": _out_decimal(margin_flow_rate, places=6),
                "margin_flow_profitability_pct": (
                    _out_decimal(margin_flow_profitability, places=4)
                    if margin_flow_profitability is not None
                    else ""
                ),
                "margin_flow_party_cost_per_unit": (
                    _out_decimal(_decimal(margin_flow.get("party_cost_per_unit")), places=4)
                    if margin_flow.get("party_cost_per_unit") is not None
                    else ""
                ),
                "margin_flow_gross_sale_qty_180": _out_decimal(
                    _decimal(margin_flow.get("gross_sale_qty_180"))
                ),
                "margin_flow_net_revenue_rub_180": _out_decimal(
                    _decimal(margin_flow.get("net_revenue_rub_180")), places=2
                ),
                "margin_flow_minimum_representation_qty": (
                    margin_flow_policy.minimum_representation_qty
                    if margin_flow_policy.enabled
                    else ""
                ),
                # Решение 2026-08-17: весь открытый остаток заказа поставщику
                # засчитывается на 100% независимо от стадии. Отменяет прежнее
                # cargo_handoff_only, где вычиталось только pipeline_cargo_handoff_qty,
                # а товар на согласовании и сборке у поставщика заказывался повторно.
                "margin_flow_reliable_incoming_qty": _out_decimal(incoming_qty),
                "margin_flow_free_stock_qty": _out_decimal(
                    _decimal(margin_flow.get("point_safe_free_stock_qty"))
                ),
                "margin_flow_data_status": margin_flow_data_status,
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
                "distribution_to_shelf_days": distribution_to_shelf_days,
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
                "_availability_history_too_short": availability_history_too_short,
            }
        )
    apply_independent_speed_tier(
        rows,
        min_order_qty=min_order_qty,
        max_order_qty=max_order_qty,
        order_rounding_rules=order_rounding_rules,
        speed_horizon_rules=speed_horizon_rules,
        distribution_to_shelf_days=distribution_to_shelf_days,
    )
    apply_price_batch_policy(
        rows,
        price_batch_rules=price_batch_rules,
        price_batch_applies_to_statuses=price_batch_applies_to_statuses,
        price_batch_applies_to_analog_roles=price_batch_applies_to_analog_roles,
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
    # Финальный барьер, не обойти ни одним из шагов выше (apply_analog_group_
    # decisions/_mark_analog_winner уже проверяли blockers по отдельности, но
    # на реальном прогоне 2026-07-31 нашлось ЕЩЁ ДВЕ утечки в других шагах
    # конвейера - апстрим-фиксы легко пропустить при следующей правке.
    # Инвариант "есть blockers -> заказ 0, ручная проверка" гарантируется
    # здесь один раз, для всех строк, независимо от того, что случилось выше.
    for row in rows:
        if _clean(row.get("blockers")):
            row["recommended_order_qty_raw"] = "0"
            row["recommended_order_qty"] = "0"
            row["dry_run_decision"] = "manual_review"

    # Предохранитель короткой истории наличия (решение 2026-08-09, см.
    # MIN_RELIABLE_AVAILABILITY_DAYS): скорость по нескольким дням наблюдения
    # не доказана, автозаказ по ней запрещён - строка уходит человеку.
    # Здесь, в самом конце, чтобы ни один шаг конвейера не вернул заказ
    # такой строке (тот же принцип, что и барьер blockers выше).
    for row in rows:
        if not row.get("_availability_history_too_short"):
            continue
        if _clean(row.get("dry_run_decision")) != "order":
            continue
        computed_qty = _clean(row.get("recommended_order_qty")) or "0"
        row["recommended_order_qty_raw"] = "0"
        row["recommended_order_qty"] = "0"
        row["dry_run_decision"] = "manual_review"
        _append_warning(row, "availability_history_too_short")
        observed = _clean(row.get("days_in_sale_long")) or "0"
        row["reason_ru"] = (
            f"Мало данных о наличии: товар был на полке {observed} дн. из "
            f"{sales_window_days} (порог {MIN_RELIABLE_AVAILABILITY_DAYS}). Поправку "
            f"наличия не применяли, расчёт по календарю давал {computed_qty} шт - "
            "по такой короткой базе автозаказ запрещён, нужна ручная проверка."
        )

    # stockout_guard (off_schedule_signal_policy) - см. константу выше.
    # Считается здесь, в самом конце, на уже окончательно устоявшемся
    # решении - только для строк, где итог "заказ не нужен" и блокеров нет
    # (блокер уже сам по себе объясняет заказ=0, тревога здесь не добавляет
    # смысла).
    for row in rows:
        if _clean(row.get("blockers")) or _clean(row.get("dry_run_decision")) != "do_not_order":
            continue
        avg_daily_sales_qty = _decimal(row.get("avg_daily_sales_qty"))
        if avg_daily_sales_qty <= 0:
            continue
        order_available_stock_qty = max(
            Decimal("0"),
            _decimal(row.get("order_available_stock_qty")),
        )
        days_of_stock_remaining = order_available_stock_qty / avg_daily_sales_qty
        required_days = (
            _decimal(row.get("lead_time_days"))
            + _decimal(row.get("distribution_to_shelf_days"))
            + Decimal(str(STOCKOUT_GUARD_BUFFER_DAYS))
        )
        if days_of_stock_remaining >= required_days:
            continue
        row["stockout_guard_triggered"] = "true"
        row["stockout_guard_days_remaining"] = _out_decimal(days_of_stock_remaining, places=1)
        row["stockout_guard_required_days"] = _out_decimal(required_days)
        _append_warning(row, "stockout_guard_triggered")
        row["reason_ru"] = (
            "ТРЕВОГА (stockout_guard): расчёт решил, что заказ сейчас не нужен, но при "
            f"текущей скорости остатка хватит на {_out_decimal(days_of_stock_remaining, places=1)} "
            f"дней, а полный цикл довоза (путь + буфер) занимает {_out_decimal(required_days)} "
            "дней - есть риск пустой полки раньше следующего планового пересмотра. "
            f"{row.get('reason_ru', '')}"
        ).strip()
    for row in rows:
        active_customer_order_qty = _decimal(row.get("active_customer_order_qty"))
        if active_customer_order_qty <= 0:
            continue
        row["reason_ru"] = (
            f"{row.get('reason_ru', '')} Активный невыполненный остаток Заказов "
            f"покупателей {_out_decimal(active_customer_order_qty)} шт. включён в "
            "потребность; Зарезервировано и Под заказ в этой формуле не используются."
        ).strip()
    return rows


def apply_price_batch_policy(
    rows: list[dict[str, Any]],
    *,
    price_batch_rules: Sequence[PriceBatchRule],
    price_batch_applies_to_statuses: Sequence[str],
    price_batch_applies_to_analog_roles: Sequence[str],
    min_order_qty: int,
    max_order_qty: int | None,
    order_rounding_rules: Sequence[OrderRoundingRule],
) -> None:
    # Раздел 4: раньше эта функция сначала сводила группу аналогов к
    # "победителю" (через уже удалённый apply_independent_speed_tier-
    # предшественник) и только внутри победителя/поддерживаемых аналогов
    # применяла минимальную партию по цене (price_batch_rules). "Поддержи-
    # ваемые аналоги" (supported_analog_policy, одобрено 2026-07-11) решали
    # проблему "проигравший цветовой вариант обнуляется навсегда, держим ему
    # сетевой минимум" - раз теперь каждая карточка (включая любой цветовой
    # вариант) считается независимо и никогда не обнуляется группировкой,
    # самой проблемы больше нет, поддержка стала не нужна. Убрано 2026-07-31
    # вместе с консолидацией по аналогам (тот же qty-diff гейт: раньше
    # карточки внутри группы аналогов молча пропускали ценовое округление
    # целиком - реальный найденный побочный баг, не только упрощение).
    # Ценовое округление (price_segment × speed_tier) теперь применяется к
    # каждой карточке по отдельности - price_segment уже независимая
    # per-SKU характеристика (квартиль цены внутри своей группы сравнения,
    # см. _price_segment в assortment_lifecycle_facts.py), группировка по
    # аналогам ей не требовалась и раньше.
    if not price_batch_rules:
        return
    for row in rows:
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
            group_free_stock_qty=_decimal(row.get("order_available_stock_qty")),
            group_incoming_qty=_decimal(row.get("incoming_qty")),
            group_avg_daily_sales_qty=_decimal(row.get("avg_daily_sales_qty")),
            price_batch_rules=price_batch_rules,
            price_batch_applies_to_statuses=price_batch_applies_to_statuses,
            price_batch_applies_to_analog_roles=price_batch_applies_to_analog_roles,
            min_order_qty=min_order_qty,
            max_order_qty=max_order_qty,
            order_rounding_rules=order_rounding_rules,
        )


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
            order_rounding_rules=_row_order_rounding_rules(row, order_rounding_rules),
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
        minimum_batch_qty = Decimal(str(price_rule.minimum_batch_qty))
        # Минимальная партия финальна (решение 2026-08-19): поднятые до партии
        # 12 шт остаются 12 шт. Раньше округление считалось от
        # max(raw_qty, minimum_batch_qty) и молча раздувало сами партии.
        candidate_qty = rounded_order_qty(
            raw_qty if raw_qty >= minimum_batch_qty else minimum_batch_qty,
            min_order_qty=min_order_qty,
            max_order_qty=max_order_qty,
            order_rounding_rules=(
                _row_order_rounding_rules(row, order_rounding_rules)
                if raw_qty >= minimum_batch_qty
                else ()
            ),
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
        rounding_rule = (
            _order_rounding_rule_for_qty(
                raw_qty,
                _row_order_rounding_rules(row, order_rounding_rules),
            )
            if raw_qty >= _decimal(row.get("price_batch_min_qty"))
            else None
        )
        row["order_rounding_rule"] = _order_rounding_rule_text(rounding_rule)
        row["order_rounding_multiple"] = rounding_rule.round_to if rounding_rule else ""
        row["price_batch_excess_qty"] = _out_decimal(final_group_excess)
        row["price_batch_excess_coverage_days"] = _out_decimal(
            final_group_excess_days,
            places=2,
        )
        # Раньше здесь стояла безусловная ветка "speed_tier == slow ->
        # manual_review" (price_batch_decision="manual_review_slow"). До
        # структурного пола/растущей скорости (2026-07-31) это было мёртвым
        # кодом по построению - медленная карточка никогда не попадала сюда
        # с dry_run_decision=="order" (apply_price_batch_policy фильтрует
        # только "order" на входе), review_only зануляла её раньше. После
        # фикса slow-карточки ЛЕГИТИМНО доходят досюда через новое
        # исключение - вторая утечка (тот же класс бага, что и блокеры):
        # эта ветка молча откатывала их обратно в manual_review, обнуляя
        # только что выданный стартовый заказ. Убрано - слово "slow" тут
        # больше не должно ничего решать, только review_by_id (реальный
        # избыток при округлении) и _auto_order_allowed.
        if id(row) in review_by_id:
            row["dry_run_decision"] = "manual_review"
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
    row_status = _clean(row.get("_assortment_status")).casefold()
    if configured_statuses and row_status not in configured_statuses:
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
    # Раздел 4: раньше эта функция сначала пыталась свести b2b-прогноз
    # ГРУППЫ аналогов к "победителю" (primary_analog) - тот же принцип
    # консолидации, что отменён и удалён 2026-07-31. Раз победителя больше
    # не существует (см. apply_independent_speed_tier), групповая ветка
    # всегда находила primary=None и молча пропускала карточки, попавшие в
    # группу по токенам - реальный побочный баг, тот же класс, что уже
    # нашли и исправили в apply_price_batch_policy. Теперь каждая карточка
    # с client-прогнозом обрабатывается независимо, без группировки.
    if not any(_clean(row.get("b2b_replacement_decision")) for row in rows):
        return

    for row in rows:
        if _clean(row.get("b2b_replacement_decision")) != "order":
            continue
        raw_qty = _ceil_decimal(
            max(
                Decimal("0"),
                _decimal(row.get("b2b_replacement_target_stock_qty"))
                - _decimal(row.get("order_available_stock_qty"))
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
            group_free_stock_qty=_decimal(row.get("order_available_stock_qty")),
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
        free_stock_qty = _decimal(row.get("order_available_stock_qty"))
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
            order_rounding_rules=_row_order_rounding_rules(row, order_rounding_rules),
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
            (_decimal(row.get("order_available_stock_qty")) for row in group_rows),
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
            order_rounding_rules=_row_order_rounding_rules(winner, order_rounding_rules),
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


def apply_independent_speed_tier(
    # Раздел 4 (assortment-status-legacy-rule-inventory.md): консолидация по
    # аналогам была ОТМЕНЕНА решением пользователя 2026-07-26 ("не нужен
    # вообще никакой механизм консолидации по аналогам, ни автоматический,
    # ни ручной... каждая карточка (SKU) рассчитывается и заказывается
    # независимо") и УДАЛЕНА из кода 2026-07-31 после qty-diff гейта
    # (реальный прогон по каталогу: +2765 шт, 236 карточек изменили заказ -
    # см. Changelog). Было apply_analog_group_decisions: сводило группу
    # аналогов к одному "победителю" (_mark_analog_winner/_mark_analog_
    # loser), суммируя спрос группы и обнуляя заказ у "проигравших". Причина
    # отмены - карточки внутри группы аналогов почти всегда разное КАЧЕСТВО
    # одного товара для разных покупателей, а не дубли с общим спросом.
    #
    # Осталось только то, что и было задумано изначально - тир скорости
    # (super_fast/fast/normal/slow) назначается КАЖДОЙ карточке по ЕЁ
    # СОБСТВЕННОЙ скорости, не по сумме группы. _analog_groups/
    # _analog_model_tokens не удалены - от них по-прежнему зависят
    # apply_supported_analog_and_price_batch_policies и apply_b2b_final_
    # order_policies (отдельные, НЕ отменённые механизмы).
    rows: list[dict[str, Any]],
    *,
    min_order_qty: int,
    max_order_qty: int | None,
    order_rounding_rules: Sequence[OrderRoundingRule],
    speed_horizon_rules: Sequence[SpeedHorizonRule],
    distribution_to_shelf_days: int = 0,
) -> None:
    for row in rows:
        own_speed = _decimal(row.get("avg_daily_sales_qty"))
        speed_rule = _speed_horizon_rule_for_group(own_speed, speed_horizon_rules)
        _apply_speed_horizon_rule(
            [row],
            speed_rule=speed_rule,
            group_avg_daily_sales_qty=own_speed,
            min_order_qty=min_order_qty,
            max_order_qty=max_order_qty,
            order_rounding_rules=order_rounding_rules,
            distribution_to_shelf_days=distribution_to_shelf_days,
        )
        _mark_single_sku_review_only_if_needed(row)


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


def _apply_speed_horizon_rule(
    group_rows: Sequence[dict[str, Any]],
    *,
    speed_rule: SpeedHorizonRule | None,
    group_avg_daily_sales_qty: Decimal,
    min_order_qty: int,
    max_order_qty: int | None,
    order_rounding_rules: Sequence[OrderRoundingRule],
    distribution_to_shelf_days: int = 0,
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
            if _clean(row.get("blockers")):
                row["recommended_order_qty_raw"] = "0"
                row["recommended_order_qty"] = "0"
                row["dry_run_decision"] = "manual_review"
                _append_warning(row, "speed_tier_manual_review")
                row["reason_ru"] = _speed_review_reason(speed_rule, len(group_rows))
                continue
            free_stock_qty = _decimal(row.get("order_available_stock_qty"))
            below_structural_floor = free_stock_qty < STRUCTURAL_FLOOR_QTY
            accelerating_speed = row.get("sales_speed_trend") == "accelerating"
            days_in_sale_medium = _decimal(row.get("days_in_sale_medium"))
            had_genuine_availability = days_in_sale_medium >= PENSION_CANDIDATE_MIN_DAYS_IN_SALE
            pension_candidate = (
                below_structural_floor and not accelerating_speed and had_genuine_availability
            )
            if pension_candidate:
                row["recommended_order_qty_raw"] = "0"
                row["recommended_order_qty"] = "0"
                row["dry_run_decision"] = "manual_review"
                _append_warning(row, "pension_candidate_flat_despite_availability")
                row["reason_ru"] = (
                    f"Кандидат на 'Пенсию': товар реально был на полке "
                    f"{_out_decimal(days_in_sale_medium, places=1)} из {TREND_WINDOW_MEDIUM_DAYS} "
                    "дней, но скорость не растёт даже с честной поправкой на наличие - это не "
                    "голодание, это угасающий спрос. Структурный пол не применяем, нужна ручная "
                    "проверка."
                )
                continue
            if below_structural_floor or accelerating_speed:
                target_stock_qty = _decimal(row.get("target_stock_qty"))
                incoming_qty = _decimal(row.get("incoming_qty"))
                recommended_order_qty_raw = _ceil_decimal(
                    max(Decimal("0"), target_stock_qty - free_stock_qty - incoming_qty)
                )
                recommended_order_qty = rounded_order_qty(
                    recommended_order_qty_raw,
                    min_order_qty=min_order_qty,
                    max_order_qty=max_order_qty,
                    order_rounding_rules=_row_order_rounding_rules(row, order_rounding_rules),
                )
                row["recommended_order_qty_raw"] = _out_decimal(recommended_order_qty_raw)
                row["recommended_order_qty"] = _out_decimal(recommended_order_qty)
                row["dry_run_decision"] = "order" if recommended_order_qty > 0 else "do_not_order"
                if below_structural_floor:
                    _append_warning(row, "structural_floor_starter_order")
                    reason = (
                        f"Структурный пол: остаток по сети {_out_decimal(free_stock_qty)} шт "
                        f"ниже {_out_decimal(STRUCTURAL_FLOOR_QTY)} шт (11 точек продаж + Сайт + "
                        "буфер СДЭК). Медленная группа не блокирует стартовый заказ: "
                        f"рекомендуем {_out_decimal(recommended_order_qty)} шт по уже посчитанной цели "
                        f"{_out_decimal(target_stock_qty)} шт."
                    )
                else:
                    _append_warning(row, "speed_tier_accelerating_override")
                    reason = (
                        "Медленная группа, но скорость растёт (accelerating) - не зануляем: "
                        f"рекомендуем {_out_decimal(recommended_order_qty)} шт по цели "
                        f"{_out_decimal(target_stock_qty)} шт."
                    )
                row["reason_ru"] = reason
                continue
            row["recommended_order_qty_raw"] = "0"
            row["recommended_order_qty"] = "0"
            row["dry_run_decision"] = "manual_review"
            _append_warning(row, "speed_tier_manual_review")
            row["reason_ru"] = _speed_review_reason(speed_rule, len(group_rows))
        return

    # distribution_to_shelf_days (найдено 2026-07-30: дата поступления заказа
    # поставщику - это дата приёмки на центральном узле, не дата на полке)
    # добавляется к тирам той же логикой, что и к базовой формуле - см.
    # planning_horizon_days выше и Changelog.
    forecast_days = max(
        0,
        speed_rule.max_effective_target_days
        - speed_rule.safety_stock_days
        + distribution_to_shelf_days,
    )
    effective_target_days = speed_rule.max_effective_target_days + distribution_to_shelf_days
    for row in group_rows:
        # Найдено на реальном прогоне 2026-07-31: карточка с уже
        # сработавшим блокером (partийная ошибка, маркетплейс-риск) молча
        # получала здесь свежий ненулевой заказ - тир пересчитывал
        # recommended_order_qty с нуля, не глядя на уже выставленный
        # blockers/qty=0 выше по конвейеру. dry_run_decision оставался
        # "manual_review" правильно, но само число заказа утекало обратно.
        # Раз блокер уже стоит - тир только помечает метаданные, количество
        # не трогает.
        if _clean(row.get("blockers")):
            continue
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
        free_stock_qty = _decimal(row.get("order_available_stock_qty"))
        incoming_qty = _decimal(row.get("incoming_qty"))
        recommended_order_qty_raw = _ceil_decimal(
            max(Decimal("0"), target_stock_qty - free_stock_qty - incoming_qty)
        )
        recommended_order_qty = rounded_order_qty(
            recommended_order_qty_raw,
            min_order_qty=min_order_qty,
            max_order_qty=max_order_qty,
            order_rounding_rules=_row_order_rounding_rules(row, order_rounding_rules),
        )
        order_rounding_rule = _order_rounding_rule_for_qty(
            recommended_order_qty_raw,
            _row_order_rounding_rules(row, order_rounding_rules),
        )

        row["safety_stock_days"] = speed_rule.safety_stock_days
        row["distribution_to_shelf_days"] = distribution_to_shelf_days
        row["effective_target_days"] = effective_target_days
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
            effective_target_days=effective_target_days,
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
    effective_target_days: int,
    recommended_order_qty: Decimal,
    recommended_order_qty_raw: Decimal,
    target_stock_qty: Decimal,
    free_stock_qty: Decimal,
    incoming_qty: Decimal,
    order_rounding_rule: OrderRoundingRule | None,
) -> str:
    label = speed_rule.label_ru or speed_rule.tier
    base = (
        f"Правило скорости {label}: максимум {effective_target_days} дней "
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
    scope_policy_audit: Mapping[str, Any] | None = None,
    scope_gate_audit: Mapping[str, Any] | None = None,
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
    order_rounding_gates = Counter(
        _clean(row.get("order_rounding_price_gate"))
        for row in rows
        if _clean(row.get("order_rounding_price_gate"))
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
        "order_rounding_price_gate_counts": dict(sorted(order_rounding_gates.items())),
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
        "scope_policy": dict(
            scope_policy_audit or empty_display_scope_audit(source_item_count=len(rows))
        ),
        "auto_order_scope_gate": dict(
            scope_gate_audit
            or build_auto_order_scope_gate_audit(
                (),
                source_item_count=len(rows),
                included_item_count=len(rows),
                run_id=run_id,
            )
        ),
        "display_family_order_recommendation": display_family_order_recommendation_summary(rows),
    }


def write_scope_gate_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> Path:
    """Журнал карточек, которые не дошли до расчёта, с причиной по каждой."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as csv_file:
        writer = csv.DictWriter(
            csv_file,
            fieldnames=AUTO_ORDER_SCOPE_GATE_CSV_COLUMNS,
            extrasaction="ignore",
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {column: row.get(column, "") for column in AUTO_ORDER_SCOPE_GATE_CSV_COLUMNS}
            )
    return path


def write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in CSV_COLUMNS})
    return path


def _classify_marketplace_risk(
    *,
    net_sales_qty: Decimal,
    marketplace_net_sales_qty: Decimal,
    marketplace_share_pct: Decimal,
    marketplace_doc_count: int,
    has_exposure: bool,
    order_impact_qty: Decimal,
) -> tuple[str, str]:
    if marketplace_net_sales_qty <= 0:
        # Без единой маркетплейс-продажи это не маркетплейс-риск - карточки с
        # нулевым спросом вообще (не только без маркетплейса) уже покрыты
        # отдельным предупреждением no_recent_net_sales, метку сюда не ставим.
        return "", ""
    non_marketplace_qty = net_sales_qty - marketplace_net_sales_qty
    if has_exposure and (
        non_marketplace_qty <= 0 or marketplace_share_pct >= MARKETPLACE_CRITICAL_SHARE_PCT
    ):
        return (
            "critical_marketplace_refusal_nonliquid_risk",
            "Критично: обычного спроса (без маркетплейса) нет, или маркетплейс "
            "занимает 70%+ продаж, а остаток/путь товара есть - риск неликвида "
            "при отказе маркетплейс-покупателя. Автозаказ остановлен, нужно "
            "ручное решение.",
        )
    if has_exposure and marketplace_share_pct >= MARKETPLACE_HIGH_SHARE_PCT:
        return (
            "high_marketplace_refusal_risk",
            "Высокий риск: маркетплейс 50-70% продаж, а остаток/путь товара "
            "есть. Автозаказ остановлен, нужно вручную разделить магазинную и "
            "маркетплейсную потребность.",
        )
    if (
        has_exposure
        and marketplace_share_pct >= MARKETPLACE_MEDIUM_SHARE_PCT
        and marketplace_doc_count >= MARKETPLACE_MEDIUM_MIN_DOC_COUNT
    ):
        return (
            "medium_channel_split_required",
            "Маркетплейс 30-50% продаж (минимум 7 продаж маркетплейса). "
            "Магазинная и маркетплейсная потребность складываются "
            "автоматически, показ раздельно - только для прозрачности риска.",
        )
    if (
        marketplace_share_pct >= MARKETPLACE_WATCH_SHARE_PCT
        and order_impact_qty >= MARKETPLACE_WATCH_MIN_ORDER_IMPACT_QTY
    ):
        return (
            "watch_order_impact",
            f"Маркетплейс 10-30% продаж и заметно влияет на заказ (оценка "
            f"без маркетплейса ниже минимум на {_out_decimal(order_impact_qty)} шт).",
        )
    return "", ""


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
    distribution_to_shelf_days: int,
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
        distribution_to_shelf_days=distribution_to_shelf_days,
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
    distribution_to_shelf_days: int,
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
    if distribution_to_shelf_days:
        components.append(f"на полку {distribution_to_shelf_days}")
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


# Группировка по аналогам сама по себе НЕ отменена - используется
# apply_supported_analog_and_price_batch_policies и apply_b2b_final_order_
# policies (отдельные, не связанные с уже удалённой 2026-07-31 винер-
# консолидацией функции). Не путать с apply_independent_speed_tier выше,
# которая больше НЕ группирует карточки для расчёта заказа.
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
        "1c:active_customer_order_balance",
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


ORDER_ROUNDING_TABLET_RE = re.compile(
    r"(?<![a-z])(ipad|matepad|mediapad|tab|pad)(?![a-z])",
    re.IGNORECASE,
)
ORDER_ROUNDING_WATCH_RE = re.compile(
    r"(?<![a-z])(watch|band|forerunner|fenix|gt\s*\d)(?![a-z])",
    re.IGNORECASE,
)
ORDER_ROUNDING_BRAND_ALIASES = {
    "redmi": "xiaomi",
    "poco": "xiaomi",
}
ORDER_ROUNDING_GATE_LABELS_RU = {
    "at_or_below_median": "цена не выше медианы группы - округление разрешено",
    "above_median": "цена выше медианы группы - округление не применяется",
    "small_group": "в группе меньше карточек с ценой, чем нужно для медианы - шаг 10",
    "no_purchase_price": "нет закупочной цены - округление не применяется",
}


def _order_rounding_device_class(name: str) -> str:
    """Класс устройства по названию карточки (решение 2026-08-19).

    Берём из названия, а не из папки: папка выделяет планшеты только у Apple и
    Samsung, поэтому дисплеи Huawei MediaPad/MatePad, Lenovo Tab и Acer Iconia
    сидят в общих папках бренда. По названию их находится 170 против 86.
    """

    if ORDER_ROUNDING_TABLET_RE.search(name):
        return "планшет"
    if ORDER_ROUNDING_WATCH_RE.search(name):
        return "часы"
    return "телефон"


def _order_rounding_brand_token(value: Any) -> str:
    text = re.sub(r"[^a-z0-9]+", " ", _clean(value).casefold()).strip()
    if not text:
        return ""
    token = text.split(" ", 1)[0]
    return ORDER_ROUNDING_BRAND_ALIASES.get(token, token)


def _order_rounding_brand_vocabulary(items: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    """Словарь брендов собирается из самих данных прогона, а не из константы.

    Реквизит бренда заполнен у всех карточек дисплеев, поэтому список брендов
    получается самоподдерживающимся: новый бренд в номенклатуре попадает в
    словарь сам, править код не нужно.
    """

    brands = set(ORDER_ROUNDING_BRAND_ALIASES)
    brands.update(KNOWN_DEVICE_BRANDS)
    for item in items:
        token = _order_rounding_brand_token(item.get("brand_compatibility"))
        if token:
            brands.add(token)
    return tuple(sorted(brands))


def _first_brand_in_text(value: str, brands: Sequence[str]) -> str:
    """Первый бренд по позиции в названии.

    Мультибрендовый дисплей `Дисплей для Tecno Spark 7 / Infinix Hot 10i`
    относится к группе Tecno: реквизит `Бренд` у таких карточек называет любой
    из совместимых брендов (на 2026-08-19 таких карточек 78), поэтому сам по
    себе он группу задавать не может.
    """

    normalized = re.sub(r"[^a-z0-9]+", " ", value.casefold())
    best_position: int | None = None
    best_brand = ""
    for brand in brands:
        match = re.search(rf"(?<![a-z0-9]){re.escape(brand)}(?![a-z0-9])", normalized)
        if match is None:
            continue
        if best_position is None or match.start() < best_position:
            best_position = match.start()
            best_brand = brand
    return ORDER_ROUNDING_BRAND_ALIASES.get(best_brand, best_brand)


def _order_rounding_group_key(
    item: Mapping[str, Any],
    *,
    brands: Sequence[str],
) -> tuple[str, str]:
    name = _clean(item.get("name"))
    brand = _first_brand_in_text(name, brands) or _order_rounding_brand_token(
        item.get("brand_compatibility")
    )
    return brand or "бренд не определён", _order_rounding_device_class(name)


def _median_decimal(values: Sequence[Decimal]) -> Decimal:
    ordered = sorted(values)
    count = len(ordered)
    middle = count // 2
    if count % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal("2")


def order_rounding_price_gates(
    items: Sequence[Mapping[str, Any]],
    *,
    purchase_facts: Mapping[str, Mapping[str, Any]],
    policy: OrderRoundingPriceGatePolicy,
) -> dict[str, OrderRoundingGate]:
    if not policy.enabled:
        return {}
    brands = _order_rounding_brand_vocabulary(items)
    group_by_code: dict[str, tuple[str, str]] = {}
    price_by_code: dict[str, Decimal] = {}
    prices_by_group: dict[tuple[str, str], list[Decimal]] = {}
    for item in items:
        code = _clean(item.get("nomenclature_code"))
        if not code:
            continue
        key = _order_rounding_group_key(item, brands=brands)
        group_by_code[code] = key
        price = _decimal(purchase_facts.get(code, {}).get("latest_purchase_price"))
        if price > 0:
            price_by_code[code] = price
            prices_by_group.setdefault(key, []).append(price)
    gates: dict[str, OrderRoundingGate] = {}
    for code, key in group_by_code.items():
        label = f"{key[0]} / {key[1]}"
        price = price_by_code.get(code)
        if price is None:
            # Нет цены - подтверждать условие нечем, вслепую заказ не раздуваем.
            gates[code] = OrderRoundingGate(label, None, False, "no_purchase_price")
            continue
        group_prices = prices_by_group.get(key, [])
        if len(group_prices) < policy.min_group_size:
            # Медиану считать не на чем: округляем фиксированным шагом, лестница
            # по количеству здесь не применяется (решение 2026-08-19).
            gates[code] = OrderRoundingGate(
                label,
                None,
                True,
                "small_group",
                policy.small_group_round_to,
            )
            continue
        median_price = _median_decimal(group_prices)
        allowed = price <= median_price
        gates[code] = OrderRoundingGate(
            label,
            median_price,
            allowed,
            "at_or_below_median" if allowed else "above_median",
        )
    return gates


def _gate_order_rounding_rules(
    gate: OrderRoundingGate,
    order_rounding_rules: Sequence[OrderRoundingRule],
) -> Sequence[OrderRoundingRule]:
    if not gate.allowed:
        return ()
    if gate.forced_round_to:
        return (OrderRoundingRule(threshold_gt=Decimal("0"), round_to=gate.forced_round_to),)
    return order_rounding_rules


def _row_order_rounding_rules(
    row: Mapping[str, Any],
    order_rounding_rules: Sequence[OrderRoundingRule],
) -> Sequence[OrderRoundingRule]:
    if row.get("_order_rounding_allowed") is False:
        return ()
    forced_round_to = row.get("_order_rounding_forced_round_to")
    if forced_round_to:
        return (OrderRoundingRule(threshold_gt=Decimal("0"), round_to=int(forced_round_to)),)
    return order_rounding_rules


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


def _optional_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


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


def _order_rounding_price_gate_policy(value: Any) -> OrderRoundingPriceGatePolicy:
    if not isinstance(value, Mapping):
        return OrderRoundingPriceGatePolicy()
    enabled = _clean(value.get("status")).casefold() == "approved" and _clean(
        value.get("implementation_status")
    ).casefold() not in {"disabled", "documented_not_wired"}
    return OrderRoundingPriceGatePolicy(
        enabled=enabled,
        min_group_size=_int_value(value.get("min_group_size"), 10),
        small_group_round_to=_int_value(value.get("small_group_round_to"), 10),
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


def _margin_flow_policy(value: Any) -> MarginFlowPolicy:
    if not isinstance(value, Mapping):
        return MarginFlowPolicy()
    return MarginFlowPolicy(
        enabled=_bool(value.get("enabled")),
        status_code=_clean(value.get("status_code")) or "sale",
        speed_min_inclusive=_decimal_value(value.get("speed_min_inclusive"), Decimal("0.1")),
        speed_max_inclusive=_decimal_value(value.get("speed_max_inclusive"), Decimal("0.25")),
        profitability_min_exclusive=_decimal_value(
            value.get("profitability_min_exclusive"), Decimal("31")
        ),
        safety_stock_days=_int_value(value.get("safety_stock_days"), 25),
        minimum_representation_qty=_int_value(value.get("minimum_representation_qty"), 13),
        physical_store_count=_int_value(value.get("physical_store_count"), 11),
        central_reserve_qty=_int_value(value.get("central_reserve_qty"), 2),
        central_warehouse_code=(_clean(value.get("central_warehouse_code")) or "РБ0000010"),
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
        "--use-active-display-family-registry",
        action="store_true",
        help=(
            "Attach a read-only family order-pool recommendation using only the "
            "verified active registry. Base SKU quantities remain in separate columns."
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
        "--distribution-to-shelf-days",
        type=int,
        default=_optional_env_int("DISPLAY_AUTO_ORDER_DISTRIBUTION_TO_SHELF_DAYS"),
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
    parser.add_argument(
        "--scope-exclusions-csv",
        type=Path,
        default=None,
        help=(
            "Журнал карточек, не дошедших до расчёта, с причиной. "
            "По умолчанию пишется рядом с --output-csv."
        ),
    )
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
