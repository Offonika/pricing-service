"""Economic counterfactual for the display auto-order policy.

The comparison treats historical purchasing as one strategy, not as ground
truth.  Both strategies are evaluated on served demand, estimated lost demand,
gross profit and inventory capital.  All source-system reads are read-only; the
task writes only local analytical artifacts.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from sqlalchemy import bindparam

from app.core.config import get_settings
from app.infrastructure.db.engines import build_engine
from tasks.build_display_auto_order_dry_run import (
    AutoOrderPolicy,
    WarehousePolicy,
    load_auto_order_policy,
    load_warehouse_policy,
)
from tasks.report_display_auto_order_six_month_backtest import (
    DEFAULT_DATE_FROM,
    DEFAULT_DATE_TO,
    DEFAULT_HISTORY_START,
    DEFAULT_LAUNCH_PROFILE_MIN_SAMPLES,
    DEFAULT_LEAD_TIME_DAYS,
    DEFAULT_RECEIPT_MAPPING_JSON,
    DEFAULT_SUPPLIER_ORDER_MAPPING_JSON,
    LAUNCH_PROFILE_MIN_AVAILABILITY_DAYS,
    LAUNCH_PROFILE_OBSERVATION_DAYS,
    STAGE_MODEL_SCENARIO_NAMES,
    LaunchObservation,
    PipelineLot,
    PurchaseLine,
    ReceiptLine,
    StageModelScenario,
    _chunks,
    _clean,
    _demand_multiplier,
    _expanding_text,
    _latest_purchase,
    build_launch_observations,
    build_launch_profile_snapshot,
    fetch_daily_sales,
    fetch_historical_open_supplier_pipeline,
    forecast_rate,
    historical_lifecycle_decision,
    item_active_as_of,
    load_backtest_items,
    normalize_purchase_history,
    reconstruct_historical_stock,
    select_launch_profile,
    stage_model_scenario,
    stage_recommendation,
    warmup_lifecycle_statuses,
)
from tasks.report_display_supplier_lead_time_history import (
    RECEIPT_MAPPING_UNRESOLVED,
    SUPPLIER_ORDER_MAPPING_UNRESOLVED,
    _load_document_line_mapping,
    fetch_display_supplier_lead_time_source_rows,
)

ZERO = Decimal("0")
ONE = Decimal("1")
PERIOD_DAYS_IN_YEAR = Decimal("365")
DEFAULT_DEMAND_FACTORS = (Decimal("0"), Decimal("1"), Decimal("1.5"))
DEFAULT_REVIEW_MODES = ("auto_only", "all_recommendations")


@dataclass(frozen=True)
class SkuEconomics:
    gross_sale_qty: Decimal = ZERO
    return_qty: Decimal = ZERO
    net_revenue_rub: Decimal = ZERO
    net_cost_rub: Decimal = ZERO
    gross_sale_cost_rub: Decimal = ZERO

    @property
    def net_revenue_per_gross_unit(self) -> Decimal:
        return self.net_revenue_rub / self.gross_sale_qty if self.gross_sale_qty > ZERO else ZERO

    @property
    def net_cost_per_gross_unit(self) -> Decimal:
        return self.net_cost_rub / self.gross_sale_qty if self.gross_sale_qty > ZERO else ZERO

    @property
    def gross_profit_per_gross_unit(self) -> Decimal:
        return self.net_revenue_per_gross_unit - self.net_cost_per_gross_unit

    @property
    def inventory_cost_per_unit(self) -> Decimal:
        return (
            self.gross_sale_cost_rub / self.gross_sale_qty if self.gross_sale_qty > ZERO else ZERO
        )


@dataclass
class SkuAccumulator:
    observed_demand_qty: Decimal = ZERO
    hidden_demand_qty: Decimal = ZERO
    potential_demand_qty: Decimal = ZERO
    served_qty: Decimal = ZERO
    served_observed_qty: Decimal = ZERO
    served_hidden_qty: Decimal = ZERO
    lost_qty: Decimal = ZERO
    lost_observed_qty: Decimal = ZERO
    lost_hidden_qty: Decimal = ZERO
    stockout_demand_days: int = 0
    inventory_qty_days: Decimal = ZERO
    inventory_value_days_rub: Decimal = ZERO
    ending_stock_qty: Decimal = ZERO
    order_qty: Decimal = ZERO
    order_value_rub: Decimal = ZERO
    order_lines: int = 0


@dataclass
class StrategyResult:
    strategy: str
    demand_factor: Decimal
    review_mode: str = "actual"
    stage_model_scenario: str = "actual"
    sku: dict[str, SkuAccumulator] = field(default_factory=dict)
    daily_rows: list[dict[str, Any]] = field(default_factory=list)
    decision_rows: list[dict[str, Any]] = field(default_factory=list)
    lifecycle_rows: list[dict[str, Any]] = field(default_factory=list)


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0").strip() or "0")
    except (ArithmeticError, ValueError):
        return ZERO


def _daterange(date_from: date, date_to: date):
    cursor = date_from
    while cursor <= date_to:
        yield cursor
        cursor += timedelta(days=1)


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8-sig")
        return
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def fetch_sku_economics(
    engine: Any,
    *,
    codes: Sequence[str],
    date_from: date,
    date_to: date,
) -> dict[str, SkuEconomics]:
    """Read return-adjusted revenue and cost by SKU from canonical 1C registers."""

    result: dict[str, SkuEconomics] = {}
    for code_chunk in _chunks(sorted(set(codes))):
        statement = _expanding_text(
            """
            WITH target_organization AS (
                SELECT _IDRRef
                FROM dbo._Reference66 WITH (NOLOCK)
                WHERE _Description = N'MASTER MOBILE'
            ),
            revenue_rows AS (
                SELECT
                    product._Code AS code,
                    reg._RecorderTRef AS recorder_tref,
                    reg._RecorderRRef AS recorder_rref,
                    reg._Fld7551RRef AS product_ref,
                    SUM(CAST(reg._Fld7560 AS decimal(28, 3))) AS qty,
                    SUM(CAST(reg._Fld7561 AS decimal(28, 2))) AS revenue
                FROM dbo._AccumRg7550 AS reg WITH (NOLOCK)
                JOIN dbo._Reference62 AS product WITH (NOLOCK)
                  ON product._IDRRef = reg._Fld7551RRef
                WHERE reg._Active = 0x01
                  AND reg._RecorderTRef IN (0x000000CB, 0x0000006D)
                  AND reg._Fld7558RRef IN (SELECT _IDRRef FROM target_organization)
                  AND reg._Period >= :date_from
                  AND reg._Period < :date_to
                  AND product._Code IN :codes
                GROUP BY
                    product._Code,
                    reg._RecorderTRef,
                    reg._RecorderRRef,
                    reg._Fld7551RRef
            ),
            eligible_cost_keys AS (
                SELECT DISTINCT recorder_tref, recorder_rref, product_ref
                FROM revenue_rows
            ),
            cost_rows AS (
                SELECT
                    product._Code AS code,
                    reg._RecorderTRef AS recorder_tref,
                    SUM(CAST(reg._Fld7587 AS decimal(28, 3))) AS qty,
                    SUM(CAST(reg._Fld7588 AS decimal(28, 2))) AS cost
                FROM dbo._AccumRg7580 AS reg WITH (NOLOCK)
                JOIN eligible_cost_keys AS eligible
                  ON eligible.recorder_tref = reg._RecorderTRef
                 AND eligible.recorder_rref = reg._RecorderRRef
                 AND eligible.product_ref = reg._Fld7581RRef
                JOIN dbo._Reference62 AS product WITH (NOLOCK)
                  ON product._IDRRef = reg._Fld7581RRef
                WHERE reg._Active = 0x01
                GROUP BY product._Code, reg._RecorderTRef
            ),
            revenue_agg AS (
                SELECT
                    code,
                    SUM(CASE WHEN recorder_tref = 0x000000CB THEN qty ELSE 0 END) AS gross_sale_qty,
                    SUM(CASE WHEN recorder_tref = 0x0000006D THEN qty ELSE 0 END) AS return_qty,
                    SUM(revenue) AS net_revenue
                FROM revenue_rows
                GROUP BY code
            ),
            cost_agg AS (
                SELECT
                    code,
                    SUM(CASE WHEN recorder_tref = 0x000000CB THEN cost ELSE 0 END) AS gross_sale_cost,
                    SUM(cost) AS net_cost
                FROM cost_rows
                GROUP BY code
            )
            SELECT
                revenue_agg.code,
                revenue_agg.gross_sale_qty,
                revenue_agg.return_qty,
                revenue_agg.net_revenue,
                COALESCE(cost_agg.net_cost, 0) AS net_cost,
                COALESCE(cost_agg.gross_sale_cost, 0) AS gross_sale_cost
            FROM revenue_agg
            LEFT JOIN cost_agg ON cost_agg.code = revenue_agg.code
            """,
            codes=code_chunk,
        ).bindparams(
            bindparam("date_from", value=datetime.combine(date_from, time.min)),
            bindparam("date_to", value=datetime.combine(date_to + timedelta(days=1), time.min)),
        )
        with engine.connect() as connection:
            rows = connection.execute(statement).mappings()
            for row in rows:
                code = _clean(row["code"])
                result[code] = SkuEconomics(
                    gross_sale_qty=_decimal(row["gross_sale_qty"]),
                    return_qty=_decimal(row["return_qty"]),
                    net_revenue_rub=_decimal(row["net_revenue"]),
                    net_cost_rub=_decimal(row["net_cost"]),
                    gross_sale_cost_rub=_decimal(row["gross_sale_cost"]),
                )
    return result


def build_inventory_unit_costs(
    *,
    codes: Sequence[str],
    economics: Mapping[str, SkuEconomics],
    purchases: Mapping[str, Sequence[PurchaseLine]],
    as_of: date,
) -> tuple[dict[str, Decimal], dict[str, str]]:
    costs: dict[str, Decimal] = {}
    sources: dict[str, str] = {}
    for code in codes:
        financial = economics.get(code, SkuEconomics())
        if financial.inventory_cost_per_unit > ZERO:
            costs[code] = financial.inventory_cost_per_unit
            sources[code] = "1c_gross_cost_of_sales_per_unit"
            continue
        purchase = _latest_purchase(purchases.get(code, ()), as_of=as_of)
        if purchase is not None and purchase.price > ZERO:
            costs[code] = purchase.price
            sources[code] = "latest_purchase_price"
            continue
        costs[code] = ZERO
        sources[code] = "unpriced"
    return costs, sources


def build_hidden_demand_base(
    *,
    codes: Sequence[str],
    sales_by_code: Mapping[str, Mapping[date, Decimal]],
    availability_by_code: Mapping[str, set[date]],
    actual_stock_by_day: Mapping[date, Mapping[str, Decimal]],
    date_from: date,
    date_to: date,
) -> dict[str, dict[date, Decimal]]:
    """Estimate latent demand only on actual end-of-day stockout dates.

    The estimate uses the same no-look-ahead 30/90/180-day rate as the order
    policy but without SKU-specific commercial uplifts.  It is a scenario, not
    a restatement of fact, and is varied later through demand factors.
    """

    hidden: dict[str, dict[date, Decimal]] = defaultdict(dict)
    for code in codes:
        sales = sales_by_code.get(code, {})
        availability = availability_by_code.get(code, set())
        for business_date in _daterange(date_from, date_to):
            actual_stock = _decimal(actual_stock_by_day.get(business_date, {}).get(code))
            if actual_stock > ZERO:
                continue
            rate, _trend, _evidence = forecast_rate(
                sales,
                availability,
                as_of=business_date - timedelta(days=1),
                demand_multiplier=ONE,
            )
            observed = _decimal(sales.get(business_date))
            hidden_qty = max(ZERO, rate - observed)
            if hidden_qty > ZERO:
                hidden[code][business_date] = hidden_qty
    return dict(hidden)


def _potential_demand(
    *,
    observed: Decimal,
    hidden_base: Decimal,
    demand_factor: Decimal,
) -> Decimal:
    return max(ZERO, observed) + max(ZERO, hidden_base) * max(ZERO, demand_factor)


def evaluate_actual_strategy(
    *,
    codes: Sequence[str],
    sales_by_code: Mapping[str, Mapping[date, Decimal]],
    hidden_demand_base: Mapping[str, Mapping[date, Decimal]],
    actual_stock_by_day: Mapping[date, Mapping[str, Decimal]],
    inventory_unit_costs: Mapping[str, Decimal],
    date_from: date,
    date_to: date,
    demand_factor: Decimal,
) -> StrategyResult:
    result = StrategyResult(strategy="actual", demand_factor=demand_factor, review_mode="actual")
    result.sku = {code: SkuAccumulator() for code in codes}
    for business_date in _daterange(date_from, date_to):
        daily = {
            "strategy": "actual",
            "demand_factor": str(demand_factor),
            "business_date": business_date.isoformat(),
            "potential_demand_qty": ZERO,
            "served_qty": ZERO,
            "lost_qty": ZERO,
            "ending_stock_qty": ZERO,
            "ending_stock_value_rub": ZERO,
            "stockout_demand_sku_days": 0,
        }
        stock_row = actual_stock_by_day.get(business_date, {})
        for code in codes:
            accumulator = result.sku[code]
            observed = _decimal(sales_by_code.get(code, {}).get(business_date))
            hidden = _decimal(hidden_demand_base.get(code, {}).get(business_date)) * demand_factor
            potential = _potential_demand(
                observed=observed,
                hidden_base=_decimal(hidden_demand_base.get(code, {}).get(business_date)),
                demand_factor=demand_factor,
            )
            served = observed
            lost = max(ZERO, potential - served)
            stock = max(ZERO, _decimal(stock_row.get(code)))
            stock_value = stock * _decimal(inventory_unit_costs.get(code))
            accumulator.observed_demand_qty += observed
            accumulator.hidden_demand_qty += hidden
            accumulator.potential_demand_qty += potential
            accumulator.served_qty += served
            accumulator.served_observed_qty += served
            accumulator.lost_qty += lost
            accumulator.lost_hidden_qty += lost
            accumulator.stockout_demand_days += int(lost > ZERO)
            accumulator.inventory_qty_days += stock
            accumulator.inventory_value_days_rub += stock_value
            accumulator.ending_stock_qty = stock
            daily["potential_demand_qty"] += potential
            daily["served_qty"] += served
            daily["lost_qty"] += lost
            daily["ending_stock_qty"] += stock
            daily["ending_stock_value_rub"] += stock_value
            daily["stockout_demand_sku_days"] += int(lost > ZERO)
        result.daily_rows.append(_json_value(daily))
    return result


def simulate_model_strategy(
    *,
    items: Sequence[Mapping[str, Any]],
    sales_by_code: Mapping[str, Mapping[date, Decimal]],
    hidden_demand_base: Mapping[str, Mapping[date, Decimal]],
    actual_availability_by_code: Mapping[str, set[date]],
    actual_stock_by_day: Mapping[date, Mapping[str, Decimal]],
    initial_pipeline_by_code: Mapping[str, Sequence[PipelineLot]],
    purchase_history: Mapping[str, Sequence[PurchaseLine]],
    receipt_history: Mapping[str, Sequence[ReceiptLine]],
    inventory_unit_costs: Mapping[str, Decimal],
    policy: AutoOrderPolicy,
    date_from: date,
    date_to: date,
    lead_time_days: int,
    demand_factor: Decimal,
    review_mode: str,
    keep_daily_rows: bool,
    history_start: date,
    initial_previous_statuses: Mapping[str, str] | None = None,
    stage_scenario: StageModelScenario | None = None,
    launch_observations: Sequence[LaunchObservation] = (),
    launch_profile_min_samples: int = DEFAULT_LAUNCH_PROFILE_MIN_SAMPLES,
) -> StrategyResult:
    codes = [_clean(item.get("nomenclature_code")) for item in items]
    item_by_code = {_clean(item.get("nomenclature_code")): item for item in items}
    prior_date = date_from - timedelta(days=1)
    prior_stock = actual_stock_by_day.get(prior_date, actual_stock_by_day.get(date_from, {}))
    stock = {code: max(ZERO, _decimal(prior_stock.get(code))) for code in codes}
    pipeline: dict[str, list[PipelineLot]] = {
        code: [PipelineLot(lot.arrival_at, lot.qty, lot.source) for lot in lots]
        for code, lots in initial_pipeline_by_code.items()
    }
    model_sales: dict[str, dict[date, Decimal]] = defaultdict(dict)
    model_availability: dict[str, set[date]] = defaultdict(set)
    for code in codes:
        model_sales[code].update(
            {
                business_date: qty
                for business_date, qty in sales_by_code.get(code, {}).items()
                if business_date < date_from
            }
        )
        model_availability[code].update(
            business_date
            for business_date in actual_availability_by_code.get(code, set())
            if business_date < date_from
        )
    active_stage_scenario = stage_scenario or stage_model_scenario("legacy")
    result = StrategyResult(
        strategy="model",
        demand_factor=demand_factor,
        review_mode=review_mode,
        stage_model_scenario=active_stage_scenario.name,
    )
    result.sku = {code: SkuAccumulator() for code in codes}
    previous_statuses = dict(initial_previous_statuses or {})
    if initial_previous_statuses is None:
        previous_statuses = warmup_lifecycle_statuses(
            items=items,
            sales_by_code=model_sales,
            availability_by_code=model_availability,
            purchase_history=purchase_history,
            receipt_history=receipt_history,
            date_from=history_start,
            date_to=date_from - timedelta(days=1),
        )
    lifecycle_by_code = {}
    lifecycle_evidence_by_code: dict[str, dict[str, Any]] = {}
    decision_dates = {
        date_from + timedelta(days=offset)
        for offset in range(0, (date_to - date_from).days + 1, max(1, policy.order_cadence_days))
    }

    for business_date in _daterange(date_from, date_to):
        for code in codes:
            lots = pipeline.get(code, [])
            arrived = sum((lot.qty for lot in lots if lot.arrival_at <= business_date), ZERO)
            if arrived > ZERO:
                stock[code] += arrived
                pipeline[code] = [lot for lot in lots if lot.arrival_at > business_date]

        daily = {
            "strategy": "model",
            "demand_factor": str(demand_factor),
            "business_date": business_date.isoformat(),
            "potential_demand_qty": ZERO,
            "served_qty": ZERO,
            "lost_qty": ZERO,
            "ending_stock_qty": ZERO,
            "ending_stock_value_rub": ZERO,
            "stockout_demand_sku_days": 0,
        }
        for code in codes:
            accumulator = result.sku[code]
            observed = _decimal(sales_by_code.get(code, {}).get(business_date))
            hidden = _decimal(hidden_demand_base.get(code, {}).get(business_date)) * demand_factor
            potential = _potential_demand(
                observed=observed,
                hidden_base=_decimal(hidden_demand_base.get(code, {}).get(business_date)),
                demand_factor=demand_factor,
            )
            served_observed = min(stock[code], observed)
            remaining_stock = stock[code] - served_observed
            served_hidden = min(remaining_stock, hidden)
            served = served_observed + served_hidden
            lost_observed = observed - served_observed
            lost_hidden = hidden - served_hidden
            lost = lost_observed + lost_hidden
            stock[code] -= served
            if served > ZERO:
                model_sales[code][business_date] = served
            if stock[code] > ZERO:
                model_availability[code].add(business_date)
            stock_value = stock[code] * _decimal(inventory_unit_costs.get(code))
            accumulator.observed_demand_qty += observed
            accumulator.hidden_demand_qty += hidden
            accumulator.potential_demand_qty += potential
            accumulator.served_qty += served
            accumulator.served_observed_qty += served_observed
            accumulator.served_hidden_qty += served_hidden
            accumulator.lost_qty += lost
            accumulator.lost_observed_qty += lost_observed
            accumulator.lost_hidden_qty += lost_hidden
            accumulator.stockout_demand_days += int(lost > ZERO)
            accumulator.inventory_qty_days += stock[code]
            accumulator.inventory_value_days_rub += stock_value
            accumulator.ending_stock_qty = stock[code]
            daily["potential_demand_qty"] += potential
            daily["served_qty"] += served
            daily["lost_qty"] += lost
            daily["ending_stock_qty"] += stock[code]
            daily["ending_stock_value_rub"] += stock_value
            daily["stockout_demand_sku_days"] += int(lost > ZERO)

        for code in codes:
            item = item_by_code[code]
            if not item_active_as_of(item, as_of=business_date):
                continue
            previous_status = previous_statuses.get(code)
            lifecycle, lifecycle_evidence = historical_lifecycle_decision(
                item=item,
                sales=model_sales.get(code, {}),
                availability_dates=model_availability.get(code, set()),
                purchases=purchase_history.get(code, ()),
                receipts=receipt_history.get(code, ()),
                as_of=business_date,
                previous_status=previous_status,
            )
            previous_statuses[code] = lifecycle.status.value
            lifecycle_by_code[code] = lifecycle
            lifecycle_evidence_by_code[code] = lifecycle_evidence
            if keep_daily_rows:
                result.lifecycle_rows.append(
                    {
                        "business_date": business_date.isoformat(),
                        "nomenclature_code": code,
                        "name": _clean(item.get("name")),
                        "previous_status": previous_status or "",
                        "status": lifecycle.status.value,
                        "status_label": lifecycle.status_label,
                        "reason_codes": "|".join(lifecycle.reason_codes),
                        "auto_order_allowed": int(lifecycle.auto_order_allowed),
                        "manual_review_required": int(lifecycle.manual_review_required),
                        "sales_30": str(lifecycle_evidence["sales_30"]),
                        "sales_90": str(lifecycle_evidence["sales_90"]),
                        "sales_180": str(lifecycle_evidence["sales_180"]),
                        "available_days_30": lifecycle_evidence["available_30"],
                        "available_days_90": lifecycle_evidence["available_90"],
                        "available_days_180": lifecycle_evidence["available_180"],
                    }
                )

        if keep_daily_rows:
            result.daily_rows.append(_json_value(daily))
        if business_date not in decision_dates:
            continue
        launch_snapshot = build_launch_profile_snapshot(
            launch_observations,
            as_of=business_date,
        )
        for code in codes:
            item = item_by_code[code]
            if not item_active_as_of(item, as_of=business_date):
                continue
            rate, trend, evidence = forecast_rate(
                model_sales.get(code, {}),
                model_availability.get(code, set()),
                as_of=business_date,
                demand_multiplier=_demand_multiplier(item, policy),
            )
            incoming = sum((lot.qty for lot in pipeline.get(code, ())), ZERO)
            lifecycle = lifecycle_by_code[code]
            launch_profile = select_launch_profile(
                item=item,
                snapshot=launch_snapshot,
                scenario=active_stage_scenario,
                policy=policy,
                min_samples=launch_profile_min_samples,
            )
            recommended, target, decision, speed_tier, raw = stage_recommendation(
                lifecycle=lifecycle,
                rate=rate,
                trend=trend,
                evidence=lifecycle_evidence_by_code[code],
                free_stock=stock[code],
                incoming_qty=incoming,
                policy=policy,
                item=item,
                stage_scenario=active_stage_scenario,
                launch_profile=launch_profile,
            )
            scheduled = ZERO
            price = ZERO
            supplier = "Поставщик не определён"
            should_schedule = decision == "order" or (
                review_mode == "all_recommendations" and decision == "manual_review"
            )
            if should_schedule and recommended > ZERO:
                scheduled = recommended
                purchase = _latest_purchase(purchase_history.get(code, ()), as_of=business_date)
                if purchase is not None:
                    price = purchase.price
                    supplier = purchase.supplier_name
                pipeline.setdefault(code, []).append(
                    PipelineLot(
                        arrival_at=business_date + timedelta(days=lead_time_days),
                        qty=scheduled,
                        source="simulated_order",
                    )
                )
                accumulator = result.sku[code]
                accumulator.order_qty += scheduled
                accumulator.order_value_rub += scheduled * price
                accumulator.order_lines += 1
            if recommended > ZERO or rate > ZERO:
                result.decision_rows.append(
                    {
                        "demand_factor": str(demand_factor),
                        "review_mode": review_mode,
                        "stage_model_scenario": active_stage_scenario.name,
                        "decision_date": business_date.isoformat(),
                        "nomenclature_code": code,
                        "name": _clean(item.get("name")),
                        "status": lifecycle.status.value,
                        "status_label": lifecycle.status_label,
                        "status_reason_codes": "|".join(lifecycle.reason_codes),
                        "forecast_rate": str(rate),
                        "free_stock_qty": str(stock[code]),
                        "incoming_qty": str(incoming),
                        "target_stock_qty": str(target),
                        "recommended_order_qty_raw": str(raw),
                        "scheduled_order_qty": str(scheduled),
                        "decision": decision,
                        "speed_tier": speed_tier,
                        "launch_profile_group_level": (
                            launch_profile.group_level if launch_profile else ""
                        ),
                        "launch_profile_group_key": (
                            launch_profile.group_key if launch_profile else ""
                        ),
                        "launch_profile_sample_count": (
                            launch_profile.sample_count if launch_profile else 0
                        ),
                        "launch_profile_confidence": (
                            launch_profile.confidence if launch_profile else ""
                        ),
                        "launch_profile_demand_qty_30d": (
                            str(launch_profile.demand_qty_30d) if launch_profile else ""
                        ),
                        "launch_profile_min_qty": (
                            str(launch_profile.min_qty) if launch_profile else ""
                        ),
                        "launch_profile_max_qty": (
                            str(launch_profile.max_qty) if launch_profile else ""
                        ),
                        "supplier": supplier,
                        "purchase_price": str(price),
                    }
                )
    return result


def _strategy_summary(
    *,
    result: StrategyResult,
    economics: Mapping[str, SkuEconomics],
    period_days: int,
) -> dict[str, Any]:
    total = SkuAccumulator()
    revenue = ZERO
    cost = ZERO
    lost_revenue = ZERO
    lost_gross_profit = ZERO
    served_hidden_revenue = ZERO
    served_hidden_gross_profit = ZERO
    financial_covered_served_qty = ZERO
    valued_inventory_qty_days = ZERO
    for code, row in result.sku.items():
        total.observed_demand_qty += row.observed_demand_qty
        total.hidden_demand_qty += row.hidden_demand_qty
        total.potential_demand_qty += row.potential_demand_qty
        total.served_qty += row.served_qty
        total.served_observed_qty += row.served_observed_qty
        total.served_hidden_qty += row.served_hidden_qty
        total.lost_qty += row.lost_qty
        total.lost_observed_qty += row.lost_observed_qty
        total.lost_hidden_qty += row.lost_hidden_qty
        total.stockout_demand_days += row.stockout_demand_days
        total.inventory_qty_days += row.inventory_qty_days
        total.inventory_value_days_rub += row.inventory_value_days_rub
        total.ending_stock_qty += row.ending_stock_qty
        total.order_qty += row.order_qty
        total.order_value_rub += row.order_value_rub
        total.order_lines += row.order_lines
        financial = economics.get(code, SkuEconomics())
        revenue += row.served_qty * financial.net_revenue_per_gross_unit
        cost += row.served_qty * financial.net_cost_per_gross_unit
        lost_revenue += row.lost_qty * financial.net_revenue_per_gross_unit
        lost_gross_profit += row.lost_qty * financial.gross_profit_per_gross_unit
        served_hidden_revenue += row.served_hidden_qty * financial.net_revenue_per_gross_unit
        served_hidden_gross_profit += row.served_hidden_qty * financial.gross_profit_per_gross_unit
        if financial.gross_sale_qty > ZERO:
            financial_covered_served_qty += row.served_qty
        if financial.inventory_cost_per_unit > ZERO:
            valued_inventory_qty_days += row.inventory_qty_days
    average_inventory_qty = total.inventory_qty_days / Decimal(period_days)
    average_inventory_value = total.inventory_value_days_rub / Decimal(period_days)
    gross_profit = revenue - cost
    annualization = PERIOD_DAYS_IN_YEAR / Decimal(period_days)
    fill_rate = total.served_qty / total.potential_demand_qty if total.potential_demand_qty else ONE
    turnover = cost * annualization / average_inventory_value if average_inventory_value else ZERO
    gmroi = (
        gross_profit * annualization / average_inventory_value if average_inventory_value else ZERO
    )
    days_inventory = average_inventory_value / cost * Decimal(period_days) if cost else ZERO
    return {
        "strategy": result.strategy,
        "review_mode": result.review_mode,
        "stage_model_scenario": result.stage_model_scenario,
        "demand_factor": str(result.demand_factor),
        "observed_demand_qty": str(total.observed_demand_qty),
        "estimated_hidden_demand_qty": str(total.hidden_demand_qty),
        "potential_demand_qty": str(total.potential_demand_qty),
        "served_qty": str(total.served_qty),
        "served_observed_qty": str(total.served_observed_qty),
        "served_hidden_qty": str(total.served_hidden_qty),
        "lost_sales_qty": str(total.lost_qty),
        "lost_observed_sales_qty": str(total.lost_observed_qty),
        "lost_hidden_sales_qty": str(total.lost_hidden_qty),
        "fill_rate": str(fill_rate),
        "stockout_demand_sku_days": total.stockout_demand_days,
        "net_revenue_rub": str(revenue),
        "cost_of_sales_rub": str(cost),
        "gross_profit_rub": str(gross_profit),
        "estimated_lost_revenue_rub": str(lost_revenue),
        "estimated_lost_gross_profit_rub": str(lost_gross_profit),
        "served_hidden_revenue_rub": str(served_hidden_revenue),
        "served_hidden_gross_profit_rub": str(served_hidden_gross_profit),
        "gross_margin_pct": str(gross_profit / revenue if revenue else ZERO),
        "average_inventory_qty": str(average_inventory_qty),
        "average_inventory_value_rub": str(average_inventory_value),
        "ending_inventory_qty": str(total.ending_stock_qty),
        "inventory_turnover_annualized": str(turnover),
        "gmroi_annualized": str(gmroi),
        "days_inventory": str(days_inventory),
        "order_qty": str(total.order_qty),
        "order_value_rub": str(total.order_value_rub),
        "order_lines": total.order_lines,
        "financial_served_qty_coverage": str(
            financial_covered_served_qty / total.served_qty if total.served_qty else ONE
        ),
        "inventory_qty_day_valuation_coverage": str(
            valued_inventory_qty_days / total.inventory_qty_days
            if total.inventory_qty_days
            else ONE
        ),
    }


def _actual_summary_with_exact_financials(
    *,
    summary: Mapping[str, Any],
    economics: Mapping[str, SkuEconomics],
    observed_qty_by_code: Mapping[str, Decimal],
    period_days: int,
) -> dict[str, Any]:
    exact_revenue = sum((row.net_revenue_rub for row in economics.values()), ZERO)
    exact_cost = sum((row.net_cost_rub for row in economics.values()), ZERO)
    gross_profit = exact_revenue - exact_cost
    adjusted = dict(summary)
    adjusted["net_revenue_rub"] = str(exact_revenue)
    adjusted["cost_of_sales_rub"] = str(exact_cost)
    adjusted["gross_profit_rub"] = str(gross_profit)
    adjusted["gross_margin_pct"] = str(gross_profit / exact_revenue if exact_revenue else ZERO)
    average_inventory_value = _decimal(adjusted["average_inventory_value_rub"])
    annualization = PERIOD_DAYS_IN_YEAR / Decimal(period_days)
    adjusted["inventory_turnover_annualized"] = str(
        exact_cost * annualization / average_inventory_value if average_inventory_value else ZERO
    )
    adjusted["gmroi_annualized"] = str(
        gross_profit * annualization / average_inventory_value if average_inventory_value else ZERO
    )
    adjusted["days_inventory"] = str(
        average_inventory_value / exact_cost * Decimal(period_days) if exact_cost else ZERO
    )
    observed_total = sum(observed_qty_by_code.values(), ZERO)
    covered_observed = sum(
        (
            qty
            for code, qty in observed_qty_by_code.items()
            if economics.get(code, SkuEconomics()).gross_sale_qty > ZERO
        ),
        ZERO,
    )
    adjusted["financial_served_qty_coverage"] = str(
        covered_observed / observed_total if observed_total else ONE
    )
    return adjusted


def _winner(actual: Mapping[str, Any], model: Mapping[str, Any]) -> str:
    actual_profit = _decimal(actual["gross_profit_rub"])
    model_profit = _decimal(model["gross_profit_rub"])
    actual_capital = _decimal(actual["average_inventory_value_rub"])
    model_capital = _decimal(model["average_inventory_value_rub"])
    if model_profit >= actual_profit and model_capital <= actual_capital:
        return "model_dominates"
    if model_profit < actual_profit and model_capital < actual_capital:
        if _decimal(model["gmroi_annualized"]) > _decimal(actual["gmroi_annualized"]):
            return "model_more_capital_efficient_but_lower_profit"
        return "actual_better_profit_model_uses_less_capital"
    if model_profit >= actual_profit and model_capital > actual_capital:
        return "model_more_profit_but_more_capital"
    return "actual_dominates"


def _classify_sku(
    *,
    actual: SkuAccumulator,
    model: SkuAccumulator,
    actual_profit: Decimal,
    model_profit: Decimal,
    period_days: int,
) -> str:
    actual_capital = actual.inventory_value_days_rub / Decimal(period_days)
    model_capital = model.inventory_value_days_rub / Decimal(period_days)
    tolerance = Decimal("0.5")
    if model.lost_qty <= actual.lost_qty + tolerance and model_capital < actual_capital:
        return "model_releases_capital_without_worse_service"
    if model.lost_qty > actual.lost_qty + tolerance:
        return "model_service_worse"
    if model_profit > actual_profit and model_capital > actual_capital:
        return "model_more_profit_more_capital"
    if model_capital > actual_capital and model_profit <= actual_profit:
        return "model_excess_capital"
    return "similar_or_mixed"


def build_sku_rows(
    *,
    items: Sequence[Mapping[str, Any]],
    actual: StrategyResult,
    model: StrategyResult,
    economics: Mapping[str, SkuEconomics],
    inventory_cost_sources: Mapping[str, str],
    period_days: int,
    sales_by_code: Mapping[str, Mapping[date, Decimal]],
    date_from: date,
    lifecycle_rows: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    item_by_code = {_clean(item.get("nomenclature_code")): item for item in items}
    first_stage_by_code: dict[str, str] = {}
    final_stage_by_code: dict[str, str] = {}
    for lifecycle_row in lifecycle_rows:
        code = _clean(lifecycle_row.get("nomenclature_code"))
        status = _clean(lifecycle_row.get("status"))
        if code and status:
            first_stage_by_code.setdefault(code, status)
            final_stage_by_code[code] = status
    rows = []
    for code in sorted(actual.sku):
        actual_row = actual.sku[code]
        model_row = model.sku[code]
        financial = economics.get(code, SkuEconomics())
        actual_profit = actual_row.served_qty * financial.gross_profit_per_gross_unit
        model_profit = model_row.served_qty * financial.gross_profit_per_gross_unit
        preperiod_sales = sum(
            (
                qty
                for business_date, qty in sales_by_code.get(code, {}).items()
                if date_from - timedelta(days=180) <= business_date < date_from
            ),
            ZERO,
        )
        observed_dates = sorted(
            business_date
            for business_date, qty in sales_by_code.get(code, {}).items()
            if qty > ZERO
        )
        rows.append(
            {
                "nomenclature_code": code,
                "name": _clean(item_by_code.get(code, {}).get("name")),
                "status_at_period_start": first_stage_by_code.get(code, ""),
                "status_at_period_end": final_stage_by_code.get(code, ""),
                "status_current_reference": _clean(item_by_code.get(code, {}).get("status")),
                "actual_served_qty": str(actual_row.served_qty),
                "model_served_qty": str(model_row.served_qty),
                "incremental_sales_qty": str(model_row.served_qty - actual_row.served_qty),
                "model_served_hidden_qty": str(model_row.served_hidden_qty),
                "model_lost_observed_qty": str(model_row.lost_observed_qty),
                "model_lost_hidden_qty": str(model_row.lost_hidden_qty),
                "actual_lost_sales_qty": str(actual_row.lost_qty),
                "model_lost_sales_qty": str(model_row.lost_qty),
                "lost_sales_delta_model_minus_actual": str(
                    model_row.lost_qty - actual_row.lost_qty
                ),
                "actual_average_inventory_qty": str(
                    actual_row.inventory_qty_days / Decimal(period_days)
                ),
                "model_average_inventory_qty": str(
                    model_row.inventory_qty_days / Decimal(period_days)
                ),
                "actual_average_inventory_value_rub": str(
                    actual_row.inventory_value_days_rub / Decimal(period_days)
                ),
                "model_average_inventory_value_rub": str(
                    model_row.inventory_value_days_rub / Decimal(period_days)
                ),
                "capital_delta_model_minus_actual_rub": str(
                    (model_row.inventory_value_days_rub - actual_row.inventory_value_days_rub)
                    / Decimal(period_days)
                ),
                "actual_gross_profit_rub_estimated_by_unit": str(actual_profit),
                "model_gross_profit_rub": str(model_profit),
                "gross_profit_delta_model_minus_actual_rub": str(model_profit - actual_profit),
                "net_revenue_per_gross_unit_rub": str(financial.net_revenue_per_gross_unit),
                "gross_profit_per_gross_unit_rub": str(financial.gross_profit_per_gross_unit),
                "inventory_cost_per_unit_rub": str(financial.inventory_cost_per_unit),
                "inventory_cost_source": inventory_cost_sources.get(code, "unpriced"),
                "model_order_qty": str(model_row.order_qty),
                "model_order_value_rub": str(model_row.order_value_rub),
                "model_order_lines": model_row.order_lines,
                "preperiod_sales_qty_180d": str(preperiod_sales),
                "first_observed_sale_date": (
                    observed_dates[0].isoformat() if observed_dates else ""
                ),
                "new_during_period": int(not observed_dates or observed_dates[0] >= date_from),
                "classification": _classify_sku(
                    actual=actual_row,
                    model=model_row,
                    actual_profit=actual_profit,
                    model_profit=model_profit,
                    period_days=period_days,
                ),
            }
        )
    rows.sort(
        key=lambda row: abs(_decimal(row["gross_profit_delta_model_minus_actual_rub"])),
        reverse=True,
    )
    return rows


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date-from", type=date.fromisoformat, default=DEFAULT_DATE_FROM)
    parser.add_argument("--date-to", type=date.fromisoformat, default=DEFAULT_DATE_TO)
    parser.add_argument("--history-start", type=date.fromisoformat, default=DEFAULT_HISTORY_START)
    parser.add_argument("--folder", default="дисплеи")
    parser.add_argument("--database-url", default="")
    parser.add_argument("--onec-database-url", default="")
    parser.add_argument("--lead-time-days", type=int, default=DEFAULT_LEAD_TIME_DAYS)
    parser.add_argument(
        "--demand-factors",
        type=Decimal,
        nargs="+",
        default=list(DEFAULT_DEMAND_FACTORS),
    )
    parser.add_argument(
        "--review-modes",
        choices=DEFAULT_REVIEW_MODES,
        nargs="+",
        default=list(DEFAULT_REVIEW_MODES),
    )
    parser.add_argument(
        "--stage-model-scenarios",
        choices=STAGE_MODEL_SCENARIO_NAMES,
        nargs="+",
        default=list(STAGE_MODEL_SCENARIO_NAMES),
    )
    parser.add_argument(
        "--launch-profile-min-samples",
        type=int,
        default=DEFAULT_LAUNCH_PROFILE_MIN_SAMPLES,
    )
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
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--preflight-dir",
        type=Path,
        required=True,
        help="Каталог проверенной предварительной таблицы со статусом PASS",
    )
    args = parser.parse_args()
    if args.date_from > args.date_to:
        raise SystemExit("date-from must not exceed date-to")
    if (args.date_from - args.history_start).days < 365:
        raise SystemExit("history-start must provide at least 365 days of warm-up")
    if args.lead_time_days <= 0:
        raise SystemExit("lead-time-days must be positive")
    if any(value < ZERO for value in args.demand_factors):
        raise SystemExit("demand-factors must be non-negative")
    if args.launch_profile_min_samples <= 0:
        raise SystemExit("launch-profile-min-samples must be positive")
    return args


def main() -> int:
    args = _parse_args()
    from tasks.display_auto_order_backtest_preflight import validate_preflight_directory

    try:
        preflight_manifest = validate_preflight_directory(args.preflight_dir)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if (
        preflight_manifest.get("date_from") != args.date_from.isoformat()
        or preflight_manifest.get("date_to") != args.date_to.isoformat()
    ):
        raise SystemExit("preflight period does not match backtest period")
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
    policy = load_auto_order_policy(args.auto_order_policy_json)
    warehouse_policy: WarehousePolicy = load_warehouse_policy(args.warehouse_policy_json)

    app_engine = build_engine(app_url, pool_pre_ping=True)
    try:
        items, run_id = load_backtest_items(app_engine, folder=args.folder)
    finally:
        app_engine.dispose()
    if not items:
        raise SystemExit("display auto-order cohort is empty")
    codes = sorted(
        {
            _clean(item.get("nomenclature_code"))
            for item in items
            if _clean(item.get("nomenclature_code"))
        }
    )
    warehouse_config = json.loads(args.warehouse_policy_json.read_text(encoding="utf-8-sig"))
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
        starting_pipeline = fetch_historical_open_supplier_pipeline(
            onec_engine,
            codes=codes,
            as_of=args.date_from - timedelta(days=1),
            fallback_lead_time_days=args.lead_time_days,
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
        economics = fetch_sku_economics(
            onec_engine,
            codes=codes,
            date_from=args.date_from,
            date_to=args.date_to,
        )
    finally:
        onec_engine.dispose()

    purchases, receipts = normalize_purchase_history(
        source_rows["supplier_order_rows"],
        source_rows["receipt_rows"],
    )
    launch_observations = build_launch_observations(
        items=items,
        sales_by_code=sales,
        availability_by_code=availability,
        receipt_history=receipts,
        history_start=args.history_start,
    )
    inventory_costs, inventory_cost_sources = build_inventory_unit_costs(
        codes=codes,
        economics=economics,
        purchases=purchases,
        as_of=args.date_to,
    )
    hidden_base = build_hidden_demand_base(
        codes=codes,
        sales_by_code=sales,
        availability_by_code=availability,
        actual_stock_by_day=stock_by_day,
        date_from=args.date_from,
        date_to=args.date_to,
    )
    period_days = (args.date_to - args.date_from).days + 1
    observed_qty_by_code = {
        code: sum(
            (
                qty
                for business_date, qty in sales.get(code, {}).items()
                if args.date_from <= business_date <= args.date_to
            ),
            ZERO,
        )
        for code in codes
    }
    warmup_sales = {
        code: {
            business_date: qty
            for business_date, qty in sales.get(code, {}).items()
            if business_date < args.date_from
        }
        for code in codes
    }
    warmup_availability = {
        code: {
            business_date
            for business_date in availability.get(code, set())
            if business_date < args.date_from
        }
        for code in codes
    }
    initial_previous_statuses = warmup_lifecycle_statuses(
        items=items,
        sales_by_code=warmup_sales,
        availability_by_code=warmup_availability,
        purchase_history=purchases,
        receipt_history=receipts,
        date_from=args.history_start,
        date_to=args.date_from - timedelta(days=1),
    )
    scenario_rows: list[dict[str, Any]] = []
    base_actual: StrategyResult | None = None
    base_model: StrategyResult | None = None
    base_review_mode = "all_recommendations"
    base_factor = Decimal("1")
    base_stage_scenario = (
        "typical" if "typical" in args.stage_model_scenarios else args.stage_model_scenarios[0]
    )
    all_decisions: list[dict[str, Any]] = []
    daily_rows: list[dict[str, Any]] = []

    for demand_factor in sorted(set(args.demand_factors)):
        actual = evaluate_actual_strategy(
            codes=codes,
            sales_by_code=sales,
            hidden_demand_base=hidden_base,
            actual_stock_by_day=stock_by_day,
            inventory_unit_costs=inventory_costs,
            date_from=args.date_from,
            date_to=args.date_to,
            demand_factor=demand_factor,
        )
        actual_summary = _actual_summary_with_exact_financials(
            summary=_strategy_summary(
                result=actual,
                economics=economics,
                period_days=period_days,
            ),
            economics=economics,
            observed_qty_by_code=observed_qty_by_code,
            period_days=period_days,
        )
        scenario_rows.append({**actual_summary, "comparison_winner": "baseline"})
        for stage_scenario_name in dict.fromkeys(args.stage_model_scenarios):
            active_stage_scenario = stage_model_scenario(stage_scenario_name)
            for review_mode in args.review_modes:
                is_base = (
                    demand_factor == base_factor
                    and review_mode == base_review_mode
                    and stage_scenario_name == base_stage_scenario
                )
                model = simulate_model_strategy(
                    items=items,
                    sales_by_code=sales,
                    hidden_demand_base=hidden_base,
                    actual_availability_by_code=availability,
                    actual_stock_by_day=stock_by_day,
                    initial_pipeline_by_code=starting_pipeline,
                    purchase_history=purchases,
                    receipt_history=receipts,
                    inventory_unit_costs=inventory_costs,
                    policy=policy,
                    date_from=args.date_from,
                    date_to=args.date_to,
                    lead_time_days=args.lead_time_days,
                    demand_factor=demand_factor,
                    review_mode=review_mode,
                    keep_daily_rows=is_base,
                    history_start=args.history_start,
                    initial_previous_statuses=initial_previous_statuses,
                    stage_scenario=active_stage_scenario,
                    launch_observations=launch_observations,
                    launch_profile_min_samples=args.launch_profile_min_samples,
                )
                model_summary = _strategy_summary(
                    result=model,
                    economics=economics,
                    period_days=period_days,
                )
                winner = _winner(actual_summary, model_summary)
                scenario_rows.append({**model_summary, "comparison_winner": winner})
                all_decisions.extend(model.decision_rows)
                if is_base:
                    base_actual = actual
                    base_model = model
                    daily_rows = actual.daily_rows + model.daily_rows

    if base_actual is None or base_model is None:
        raise SystemExit("demand-factors must include 1 for the base scenario")
    sku_rows = build_sku_rows(
        items=items,
        actual=base_actual,
        model=base_model,
        economics=economics,
        inventory_cost_sources=inventory_cost_sources,
        period_days=period_days,
        sales_by_code=sales,
        date_from=args.date_from,
        lifecycle_rows=base_model.lifecycle_rows,
    )
    output = args.output_dir
    output.mkdir(parents=True, exist_ok=True)
    _write_csv(output / "economic-scenario-comparison.csv", scenario_rows)
    _write_csv(output / "economic-sku-outcomes.csv", sku_rows)
    _write_csv(output / "economic-daily-summary.csv", daily_rows)
    _write_csv(output / "economic-decision-detail.csv", all_decisions)
    _write_csv(output / "economic-lifecycle-history.csv", base_model.lifecycle_rows)
    _write_csv(
        output / "economic-launch-observation-history.csv",
        [row.as_dict() for row in launch_observations],
    )

    lifecycle_day_counts = Counter(row["status"] for row in base_model.lifecycle_rows)
    final_lifecycle_by_code = {
        row["nomenclature_code"]: row["status"] for row in base_model.lifecycle_rows
    }

    base_actual_summary = next(
        row
        for row in scenario_rows
        if row["strategy"] == "actual" and _decimal(row["demand_factor"]) == base_factor
    )
    base_model_summary = next(
        row
        for row in scenario_rows
        if row["strategy"] == "model"
        and _decimal(row["demand_factor"]) == base_factor
        and row["review_mode"] == base_review_mode
        and row["stage_model_scenario"] == base_stage_scenario
    )
    summary = {
        "schema": "display_auto_order_economic_backtest.v2",
        "status": "share_with_caveats",
        "date_from": args.date_from,
        "date_to": args.date_to,
        "history_start": args.history_start,
        "preflight": {
            "directory": str(args.preflight_dir),
            "schema": preflight_manifest.get("schema"),
            "status": preflight_manifest.get("preflight_status"),
            "files": preflight_manifest.get("files"),
        },
        "lead_time_days": args.lead_time_days,
        "stage_model": {
            "scenarios": list(dict.fromkeys(args.stage_model_scenarios)),
            "scenario_parameters": [
                {
                    "name": scenario_name,
                    "launch_quantile": (
                        str(stage_model_scenario(scenario_name).launch_quantile)
                        if stage_model_scenario(scenario_name).launch_quantile is not None
                        else None
                    ),
                    "use_sales_start_min_max": stage_model_scenario(
                        scenario_name
                    ).use_sales_start_min_max,
                    "protected_working_safety_days": stage_model_scenario(
                        scenario_name
                    ).protected_working_safety_days,
                }
                for scenario_name in dict.fromkeys(args.stage_model_scenarios)
            ],
            "base_scenario": base_stage_scenario,
            "launch_profile_observation_days": LAUNCH_PROFILE_OBSERVATION_DAYS,
            "launch_profile_min_availability_days": LAUNCH_PROFILE_MIN_AVAILABILITY_DAYS,
            "launch_profile_min_samples": args.launch_profile_min_samples,
            "launch_observation_count": len(launch_observations),
            "phone_sales_used": False,
        },
        "cohort": {
            "classification_run_id": run_id,
            "sku_count": len(codes),
            "definition": "all display rows from one classification run; daily eligibility is reconstructed from historical events",
            "current_status_filter": False,
            "historical_folder_membership_warning": True,
        },
        "lifecycle": {
            "method": "daily walk-forward full ladder with previous-day hysteresis",
            "stage_sku_days": dict(sorted(lifecycle_day_counts.items())),
            "final_stage_sku_count": dict(
                sorted(Counter(final_lifecycle_by_code.values()).items())
            ),
        },
        "metric_definitions": {
            "winner": "higher gross profit with lower average inventory capital dominates; otherwise GMROI and profit trade-off is reported",
            "lost_sales": "observed sales not served plus scenario-estimated hidden demand on actual end-of-day stockout dates",
            "gross_profit": "net 1C revenue less net 1C cost of sales; model uses observed SKU unit economics including proportional historical returns",
            "average_inventory_capital": "mean daily network stock valued at 1C gross cost of sales per unit, with latest purchase price fallback",
            "inventory_turnover": "annualized cost of sales divided by average inventory capital",
            "gmroi": "annualized gross profit divided by average inventory capital",
        },
        "base_scenario": {
            "demand_factor": base_factor,
            "review_mode": base_review_mode,
            "stage_model_scenario": base_stage_scenario,
            "actual": base_actual_summary,
            "model": base_model_summary,
            "winner": base_model_summary["comparison_winner"],
            "delta": {
                "served_qty": _decimal(base_model_summary["served_qty"])
                - _decimal(base_actual_summary["served_qty"]),
                "lost_sales_qty": _decimal(base_model_summary["lost_sales_qty"])
                - _decimal(base_actual_summary["lost_sales_qty"]),
                "net_revenue_rub": _decimal(base_model_summary["net_revenue_rub"])
                - _decimal(base_actual_summary["net_revenue_rub"]),
                "gross_profit_rub": _decimal(base_model_summary["gross_profit_rub"])
                - _decimal(base_actual_summary["gross_profit_rub"]),
                "average_inventory_value_rub": _decimal(
                    base_model_summary["average_inventory_value_rub"]
                )
                - _decimal(base_actual_summary["average_inventory_value_rub"]),
                "ending_inventory_qty": _decimal(base_model_summary["ending_inventory_qty"])
                - _decimal(base_actual_summary["ending_inventory_qty"]),
                "gmroi_annualized": _decimal(base_model_summary["gmroi_annualized"])
                - _decimal(base_actual_summary["gmroi_annualized"]),
            },
        },
        "sensitivity": scenario_rows,
        "data_quality": {
            "daily_sales_sku_count": len(sales),
            "financial_sku_count": len(economics),
            "inventory_valued_sku_count": sum(cost > ZERO for cost in inventory_costs.values()),
            "hidden_demand_sku_count": len(hidden_base),
            "hidden_demand_sku_days": sum(len(rows) for rows in hidden_base.values()),
            "stock_source_counts": stock_counts,
        },
        "limitations": [
            "Hidden demand is estimated only on actual end-of-day zero-stock dates; it is not an accounting fact.",
            "A zero end-of-day balance may mean the last unit was sold that day, so hidden demand is shown with 0x/1x/1.5x sensitivity.",
            "Model revenue and margin use historical net unit economics by SKU and assume the historical return/margin rate remains proportional.",
            "Historical reserves are unavailable; model free stock may be overstated and model orders understated.",
            "Current display subject defines SKU identity because historical folder membership changes are unavailable; current lifecycle status is not used.",
            "Historical manual statuses are replayed only when source evidence includes an effective date; undated blockers are not projected backward.",
            "The model uses a fixed 52-day receipt assumption; supplier-specific realized lead times are not replayed.",
            "Inventory capital excludes financing cost, warehouse handling, obsolescence and taxes.",
            "Launch profiles exclude left-censored launches and rows with fewer than seven proven availability days; brand, quality and price-segment grouping comes from the classification snapshot.",
            "Phone sales and installed-base estimates are not used because the company does not sell phones.",
        ],
        "outputs": {
            "scenario_comparison_csv": "economic-scenario-comparison.csv",
            "sku_outcomes_csv": "economic-sku-outcomes.csv",
            "daily_summary_csv": "economic-daily-summary.csv",
            "decision_detail_csv": "economic-decision-detail.csv",
            "lifecycle_history_csv": "economic-lifecycle-history.csv",
            "launch_observation_history_csv": "economic-launch-observation-history.csv",
        },
    }
    (output / "economic-summary.json").write_text(
        json.dumps(_json_value(summary), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(_json_value(summary), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
