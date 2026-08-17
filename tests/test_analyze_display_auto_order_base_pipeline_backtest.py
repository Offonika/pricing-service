from tasks.analyze_display_auto_order_base_pipeline_backtest import (
    candidate_rule_rows,
    enrich_sku_rows,
    finalize_existing_analysis,
)


def _sku_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "nomenclature_code": "SKU-1",
        "stage_at_period_start": "sale",
        "demand_pattern_preperiod": "intermittent",
        "inventory_cost_per_unit_rub_start": "1000",
        "gross_margin_per_unit_rub_start": "600",
        "lead_time_p75_days_start": 70,
        "lead_time_confidence_start": "medium",
        "served_observed_delta_to_control_qty": "2",
        "gross_profit_delta_to_control_rub": "1200",
        "capital_delta_to_control_rub": "1000",
        "ending_inventory_delta_to_control_qty": "1",
        "economic_contribution_delta_to_control_rub": "50",
        "order_delta_to_control_qty": "3",
    }
    row.update(overrides)
    return row


def test_enrich_sku_rows_calculates_predecision_margin_cost_ratio() -> None:
    result = enrich_sku_rows([_sku_row()])[0]

    assert result["gross_margin_to_cost_ratio_start"] == "0.6"
    assert result["gross_margin_to_cost_ratio_band_start"] == "0.50-0.99"
    assert result["pipeline_profile_affected"] == 1


def test_candidate_rules_use_medium_confidence_and_preperiod_features() -> None:
    rows = enrich_sku_rows(
        [
            _sku_row(),
            _sku_row(
                nomenclature_code="SKU-HIGH",
                lead_time_confidence_start="high",
                served_observed_delta_to_control_qty="100",
                economic_contribution_delta_to_control_rub="10000",
            ),
        ]
    )

    result = {row["rule_id"]: row for row in candidate_rule_rows(rows)}[
        "medium95_grow_sparse_cost1500_ratio0.5_p7590"
    ]

    assert result["sku_count"] == 1
    assert result["served_observed_delta_qty"] == "2"
    assert result["economic_contribution_delta_rub"] == "50"


def test_finalize_existing_analysis_hashes_completed_artifacts(tmp_path) -> None:
    for name in (
        "analysis-summary.json",
        "SEGMENT-DIAGNOSTIC.md",
        "sku-effects.csv",
        "segment-effects.csv",
        "candidate-rules.csv",
        "period-effects.csv",
        "segment-analysis.ipynb",
    ):
        (tmp_path / name).write_text(name, encoding="utf-8")

    manifest = finalize_existing_analysis(tmp_path)

    assert manifest["production_authorized"] is False
    assert set(manifest["files"]) == {
        "analysis-summary.json",
        "SEGMENT-DIAGNOSTIC.md",
        "sku-effects.csv",
        "segment-effects.csv",
        "candidate-rules.csv",
        "period-effects.csv",
        "segment-analysis.ipynb",
    }
    assert (tmp_path / "analysis-manifest.json").is_file()
