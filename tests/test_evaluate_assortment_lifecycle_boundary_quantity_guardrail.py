from datetime import date
from decimal import Decimal

from tasks.evaluate_assortment_lifecycle_boundary_quantity_guardrail import (
    GuardedRepresentationMinimumLookup,
    candidate_guardrails,
    representation_minimum_reach_by_level,
    select_screening_winner,
)


class _BaseLookup:
    def get(self, key: tuple[date, str], default: object = None) -> object:
        return Decimal("13") if key[1] == "SKU-1" else default


def test_guarded_representation_minimum_caps_only_selected_keys() -> None:
    selected = (date(2026, 2, 1), "SKU-1")
    lookup = GuardedRepresentationMinimumLookup(
        base=_BaseLookup(),  # type: ignore[arg-type]
        guarded_keys={selected},
        cap_qty=Decimal("7"),
    )

    assert lookup.get(selected) == Decimal("7")
    assert lookup.get((date(2026, 2, 2), "SKU-1")) == Decimal("13")
    assert lookup.get((date(2026, 2, 1), "SKU-2")) is None


def _metrics(*, effect: str, served: str = "10") -> dict[str, str]:
    return {
        "served_sales_qty": served,
        "gross_profit_rub": "10",
        "average_inventory_value_rub": "10",
        "carrying_cost_rub": "10",
        "economic_effect_rub": effect,
        "gmroi": "10",
        "ending_inventory_qty": "10",
        "ending_target_stock_qty": "10",
        "ending_excess_stock_qty": "10",
    }


def test_screening_selects_best_effect_only_among_all_gate_passers() -> None:
    baseline = _metrics(effect="10")
    candidates = {
        "failed_service": _metrics(effect="20", served="9"),
        "passed_low": _metrics(effect="11"),
        "passed_high": _metrics(effect="12"),
    }

    assert (
        candidate_guardrails(candidates["failed_service"], baseline)["served_sales_not_worse"]
        is False
    )
    assert select_screening_winner(candidates, baseline=baseline) == "passed_high"


def test_screening_tie_selects_least_restrictive_representation_cap() -> None:
    baseline = _metrics(effect="10")
    candidates = {
        "boundary-risk-ratio-lt1p30-representation-cap-0": _metrics(effect="11"),
        "boundary-risk-ratio-lt1p30-representation-cap-7": _metrics(effect="11"),
        "boundary-risk-ratio-lt1p30-representation-cap-10": _metrics(effect="11"),
    }

    assert select_screening_winner(candidates, baseline=baseline).endswith("cap-10")


def test_representation_minimum_reach_is_reported_for_every_level() -> None:
    guarded_keys = {
        (date(2026, 2, 1), "A"),
        (date(2026, 2, 1), "B"),
    }
    masks = {
        (date(2026, 2, 1), "A"): 0b1111,
        (date(2026, 2, 1), "B"): 0b0011,
    }
    bit_by_variant = {
        (8, "brand_quality_construction"): 0,
        (8, "quality_construction"): 1,
        (8, "quality"): 2,
        (8, "all_displays"): 3,
    }

    result = representation_minimum_reach_by_level(
        guarded_keys=guarded_keys,
        masks=masks,
        bit_by_variant=bit_by_variant,
        spike_keys={(date(2026, 2, 1), "B")},
    )

    assert result["brand_quality_construction"] == {
        "guarded_key_count": 2,
        "representation_minimum_13_key_count": 1,
        "representation_minimum_13_key_share": 0.5,
    }
    assert result["quality_construction"] == result["brand_quality_construction"]
    assert result["quality"]["representation_minimum_13_key_count"] == 1
    assert result["all_displays"]["representation_minimum_13_key_count"] == 1
