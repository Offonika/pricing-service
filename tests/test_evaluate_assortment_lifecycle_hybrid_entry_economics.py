from decimal import Decimal

from tasks.evaluate_assortment_lifecycle_exit_hysteresis_economics import (
    MONEY_AND_QUANTITY_METRICS,
)
from tasks.evaluate_assortment_lifecycle_hybrid_entry_economics import (
    BASELINE_POLICY,
    CANDIDATE_POLICY,
    combine_hybrid_entry_results,
)


def _metrics(value: str) -> dict[str, str]:
    return {metric: value for metric in MONEY_AND_QUANTITY_METRICS}


def test_combine_hybrid_entry_results_pairs_every_group_level() -> None:
    levels = (
        "brand_quality_construction",
        "quality_construction",
        "quality",
        "all_displays",
    )
    reused = [
        {"policy": BASELINE_POLICY, "comparable_group_level": level, **_metrics("10")}
        for level in levels
    ]
    simulated = {
        level: {metric: Decimal("11") for metric in MONEY_AND_QUANTITY_METRICS} for level in levels
    }

    rows = combine_hybrid_entry_results(reused_rows=reused, simulated=simulated)

    assert len(rows) == 8
    assert [row["policy"] for row in rows[:2]] == [BASELINE_POLICY, CANDIDATE_POLICY]
    assert rows[0]["vs_entry_e1_served_sales_qty_delta"] == "0"
    assert rows[1]["vs_entry_e1_served_sales_qty_delta"] == "1"
