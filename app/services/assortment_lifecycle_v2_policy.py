from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping


DEFAULT_ASSORTMENT_LIFECYCLE_V2_POLICY_PATH = Path(
    "config/assortment/display-assortment-lifecycle-v2.json"
)


@dataclass(frozen=True)
class DemandStatePolicy:
    growth_multiplier: Decimal = Decimal("1.5")
    confirmation_days: int = 14
    max_single_day_share: Decimal = Decimal("0.70")
    min_independent_sales: int = 2
    confirmed_sales_qty_180: Decimal = Decimal("12")
    decline_multiplier: Decimal = Decimal("1.2")
    decline_min_days_in_sale_90: Decimal = Decimal("15")


@dataclass(frozen=True)
class AssortmentLifecycleV2Policy:
    schema: str
    policy_id: str
    live_enabled: bool
    demand: DemandStatePolicy
    comparable_group_min_size: int
    reliable_incoming_basis: str


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
    policy = AssortmentLifecycleV2Policy(
        schema=str(payload["schema"]),
        policy_id=_required_text(shadow, "policy_id"),
        live_enabled=bool(payload.get("live_enabled", False)),
        demand=DemandStatePolicy(
            growth_multiplier=_positive_decimal(demand, "growth_multiplier"),
            confirmation_days=_positive_int(demand, "confirmation_days"),
            max_single_day_share=_fraction(demand, "max_single_day_share"),
            min_independent_sales=_positive_int(demand, "min_independent_sales"),
            confirmed_sales_qty_180=_positive_decimal(
                demand, "confirmed_sales_qty_180"
            ),
            decline_multiplier=_positive_decimal(demand, "decline_multiplier"),
            decline_min_days_in_sale_90=_positive_decimal(
                demand, "decline_min_days_in_sale_90"
            ),
        ),
        comparable_group_min_size=_positive_int(
            shadow, "comparable_group_min_size"
        ),
        reliable_incoming_basis=_required_text(shadow, "reliable_incoming_basis"),
    )
    if policy.reliable_incoming_basis not in {"cargo_handoff"}:
        raise ValueError("unsupported_reliable_incoming_basis")
    _validate_grid(payload)
    return policy


def _validate_grid(payload: Mapping[str, Any]) -> None:
    grid = _mapping(payload.get("backtest_grid"), "backtest_grid_required")
    for name in (
        "growth_multipliers",
        "confirmation_days",
        "max_single_day_shares",
        "min_independent_sales",
        "comparable_group_min_sizes",
    ):
        values = grid.get(name)
        if not isinstance(values, list) or not values:
            raise ValueError(f"backtest_grid_values_required:{name}")
    periods = _mapping(payload.get("periods"), "backtest_periods_required")
    for name in ("training_from", "training_to", "holdout_from", "holdout_to"):
        _required_text(periods, name)
    acceptance = _mapping(payload.get("acceptance"), "backtest_acceptance_required")
    for name in (
        "served_sales_not_worse",
        "gross_profit_not_worse",
        "economic_effect_non_negative",
        "gmroi_not_worse",
        "ending_excess_stock_not_worse",
    ):
        if acceptance.get(name) is not True:
            raise ValueError(f"backtest_acceptance_must_be_true:{name}")


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
