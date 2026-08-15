from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

DEFAULT_ASSORTMENT_LIFECYCLE_V2_POLICY_PATH = Path(
    "config/assortment/display-assortment-lifecycle-v2.json"
)


@dataclass(frozen=True)
class DemandStatePolicy:
    growth_multiplier: Decimal = Decimal("1.2")
    confirmation_days: int = 14
    max_single_day_share: Decimal = Decimal("0.70")
    min_independent_sales: int = 2
    confirmed_sales_qty_180: Decimal = Decimal("12")
    decline_multiplier: Decimal = Decimal("1.2")
    decline_min_days_in_sale_90: Decimal = Decimal("15")


@dataclass(frozen=True)
class AssortmentLifecycleV2BacktestGrid:
    growth_multipliers: tuple[Decimal, ...]
    confirmation_days: tuple[int, ...]
    max_single_day_shares: tuple[Decimal, ...]
    min_independent_sales: tuple[int, ...]
    spike_quantity_policies: tuple[str, ...]
    comparable_group_min_sizes: tuple[int, ...]
    comparable_group_levels: tuple[str, ...]


@dataclass(frozen=True)
class AssortmentLifecycleV2BacktestPeriods:
    training_from: date
    training_to: date
    holdout_from: date
    holdout_to: date


@dataclass(frozen=True)
class AssortmentLifecycleV2Policy:
    schema: str
    policy_id: str
    live_enabled: bool
    demand: DemandStatePolicy
    comparable_group_min_size: int
    reliable_incoming_basis: str
    spike_quantity_policy: str
    backtest_grid: AssortmentLifecycleV2BacktestGrid
    periods: AssortmentLifecycleV2BacktestPeriods
    acceptance_criteria: tuple[str, ...]


DEFAULT_DEMAND_STATE_POLICY = DemandStatePolicy()


def load_assortment_lifecycle_v2_policy(
    path: Path | str = DEFAULT_ASSORTMENT_LIFECYCLE_V2_POLICY_PATH,
) -> AssortmentLifecycleV2Policy:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("assortment_lifecycle_v2_policy_must_be_object")
    if payload.get("schema") != "display_assortment_lifecycle_v2_policy.v1":
        raise ValueError("unsupported_assortment_lifecycle_v2_policy_schema")
    shadow = _mapping(payload.get("shadow_policy"), "shadow_policy_required")
    demand = _mapping(shadow.get("demand"), "shadow_demand_policy_required")
    grid = _mapping(payload.get("backtest_grid"), "backtest_grid_required")
    periods = _mapping(payload.get("periods"), "backtest_periods_required")
    acceptance = _mapping(payload.get("acceptance"), "backtest_acceptance_required")
    policy = AssortmentLifecycleV2Policy(
        schema=str(payload["schema"]),
        policy_id=_required_text(shadow, "policy_id"),
        live_enabled=bool(payload.get("live_enabled", False)),
        demand=DemandStatePolicy(
            growth_multiplier=_positive_decimal(demand, "growth_multiplier"),
            confirmation_days=_positive_int(demand, "confirmation_days"),
            max_single_day_share=_fraction(demand, "max_single_day_share"),
            min_independent_sales=_positive_int(demand, "min_independent_sales"),
            confirmed_sales_qty_180=_positive_decimal(demand, "confirmed_sales_qty_180"),
            decline_multiplier=_positive_decimal(demand, "decline_multiplier"),
            decline_min_days_in_sale_90=_positive_decimal(demand, "decline_min_days_in_sale_90"),
        ),
        comparable_group_min_size=_positive_int(shadow, "comparable_group_min_size"),
        reliable_incoming_basis=_required_text(shadow, "reliable_incoming_basis"),
        spike_quantity_policy=_required_text(shadow, "spike_quantity_policy"),
        backtest_grid=AssortmentLifecycleV2BacktestGrid(
            growth_multipliers=_decimal_list(grid, "growth_multipliers"),
            confirmation_days=_integer_list(grid, "confirmation_days"),
            max_single_day_shares=_fraction_list(grid, "max_single_day_shares"),
            min_independent_sales=_integer_list(grid, "min_independent_sales"),
            spike_quantity_policies=_text_list(grid, "spike_quantity_policies"),
            comparable_group_min_sizes=_integer_list(grid, "comparable_group_min_sizes"),
            comparable_group_levels=_text_list(grid, "comparable_group_levels"),
        ),
        periods=AssortmentLifecycleV2BacktestPeriods(
            training_from=_date_value(periods, "training_from"),
            training_to=_date_value(periods, "training_to"),
            holdout_from=_date_value(periods, "holdout_from"),
            holdout_to=_date_value(periods, "holdout_to"),
        ),
        acceptance_criteria=tuple(
            name
            for name in (
                "served_sales_not_worse",
                "gross_profit_not_worse",
                "economic_effect_non_negative",
                "gmroi_not_worse",
                "ending_excess_stock_not_worse",
            )
            if acceptance.get(name) is True
        ),
    )
    if policy.reliable_incoming_basis not in {"cargo_handoff"}:
        raise ValueError("unsupported_reliable_incoming_basis")
    if policy.spike_quantity_policy not in policy.backtest_grid.spike_quantity_policies:
        raise ValueError("unsupported_spike_quantity_policy")
    _validate_periods(policy.periods)
    _validate_acceptance(policy.acceptance_criteria)
    return policy


def _validate_acceptance(criteria: tuple[str, ...]) -> None:
    required = (
        "served_sales_not_worse",
        "gross_profit_not_worse",
        "economic_effect_non_negative",
        "gmroi_not_worse",
        "ending_excess_stock_not_worse",
    )
    for name in required:
        if name not in criteria:
            raise ValueError(f"backtest_acceptance_must_be_true:{name}")


def _validate_periods(periods: AssortmentLifecycleV2BacktestPeriods) -> None:
    if periods.training_from > periods.training_to:
        raise ValueError("backtest_training_period_invalid")
    if periods.holdout_from > periods.holdout_to:
        raise ValueError("backtest_holdout_period_invalid")
    if periods.training_to >= periods.holdout_from:
        raise ValueError("backtest_holdout_must_follow_training")


def _mapping(value: Any, error: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(error)
    return value


def _required_text(payload: Mapping[str, Any], name: str) -> str:
    value = str(payload.get(name) or "").strip()
    if not value:
        raise ValueError(f"assortment_lifecycle_v2_policy_value_required:{name}")
    return value


def _positive_int(payload: Mapping[str, Any], name: str) -> int:
    try:
        value = int(payload.get(name))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"assortment_lifecycle_v2_policy_integer_required:{name}") from exc
    if value < 1:
        raise ValueError(f"assortment_lifecycle_v2_policy_positive_required:{name}")
    return value


def _positive_decimal(payload: Mapping[str, Any], name: str) -> Decimal:
    try:
        value = Decimal(str(payload.get(name)))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"assortment_lifecycle_v2_policy_decimal_required:{name}") from exc
    if value <= 0:
        raise ValueError(f"assortment_lifecycle_v2_policy_positive_required:{name}")
    return value


def _fraction(payload: Mapping[str, Any], name: str) -> Decimal:
    value = _positive_decimal(payload, name)
    if value > 1:
        raise ValueError(f"assortment_lifecycle_v2_policy_fraction_required:{name}")
    return value


def _list(payload: Mapping[str, Any], name: str) -> list[Any]:
    values = payload.get(name)
    if not isinstance(values, list) or not values:
        raise ValueError(f"backtest_grid_values_required:{name}")
    return values


def _decimal_list(payload: Mapping[str, Any], name: str) -> tuple[Decimal, ...]:
    values: list[Decimal] = []
    for raw_value in _list(payload, name):
        try:
            value = Decimal(str(raw_value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError(f"backtest_grid_decimal_required:{name}") from exc
        if not value.is_finite() or value <= 0:
            raise ValueError(f"backtest_grid_positive_required:{name}")
        values.append(value)
    return tuple(dict.fromkeys(values))


def _fraction_list(payload: Mapping[str, Any], name: str) -> tuple[Decimal, ...]:
    values = _decimal_list(payload, name)
    if any(value > 1 for value in values):
        raise ValueError(f"backtest_grid_fraction_required:{name}")
    return values


def _integer_list(payload: Mapping[str, Any], name: str) -> tuple[int, ...]:
    values: list[int] = []
    for raw_value in _list(payload, name):
        try:
            value = int(raw_value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"backtest_grid_integer_required:{name}") from exc
        if value < 1 or isinstance(raw_value, float) and not raw_value.is_integer():
            raise ValueError(f"backtest_grid_positive_integer_required:{name}")
        values.append(value)
    return tuple(dict.fromkeys(values))


def _text_list(payload: Mapping[str, Any], name: str) -> tuple[str, ...]:
    values = tuple(dict.fromkeys(str(value).strip() for value in _list(payload, name)))
    if any(not value for value in values):
        raise ValueError(f"backtest_grid_text_required:{name}")
    return values


def _date_value(payload: Mapping[str, Any], name: str) -> date:
    value = _required_text(payload, name)
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"backtest_period_date_required:{name}") from exc
