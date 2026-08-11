from tasks.build_display_auto_order_frozen_report import build_artifact, build_source_notes


def test_frozen_report_metric_cards_render_ruble_values_without_usd_format() -> None:
    analysis = {
        "generated_at": "2026-08-11T00:00:00Z",
        "cohort": {"sku_count": 1, "classification_run_id": 1},
        "acceptance": {
            "gross_profit_not_lower": False,
            "fill_rate_not_lower": False,
            "capital_lower_or_gmroi_higher": True,
            "passed": False,
        },
        "protective_scenario_acceptance": {"passed_count": 0, "evaluated_count": 18},
        "headline": {
            "observed_fill_rate": 0.93,
            "observed_fill_delta": -0.07,
            "hidden_fill_rate": 0.58,
            "hidden_fill_delta": -0.03,
            "gross_profit_delta_rub": -10_071_815.62,
            "capital_delta_rub": -9_675_285.62,
            "economic_contribution_delta_rub": -6_953_192.73,
            "observed_fill_improvement_vs_control": 0.003,
            "gross_profit_improvement_vs_control_rub": 500_000,
            "capital_increase_vs_control_rub": 2_000_000,
            "best_service_fill_improvement_vs_control": 0.004,
            "best_service_profit_improvement_vs_control_rub": 700_000,
            "best_service_capital_increase_vs_control_rub": 3_000_000,
            "manual_order_share": 0.84,
            "manual_review_created": 1_900,
            "manual_review_updated": 10_000,
            "manual_review_creation_reduction": 0.84,
            "extra_lost_total_qty": 10_549,
            "sale_extra_lost_qty": 9_884,
            "sale_extra_lost_share": 0.937,
            "top10_negative_gp_share": 0.159,
            "hidden_kmp4_qty": 1_000,
            "hidden_site_order_qty": 500,
            "hidden_site_cart_qty": 10,
            "hidden_reserve_backlog_qty": 0,
            "entered_sale_demand_share": 0.34,
            "sale_sku_count": 1_700,
            "entered_sale_sku_count": 1_200,
            "economic_safety_binding_share": 0.98,
        },
        "actual": {
            "observed_fill_rate": "1.0000",
            "hidden_fill_rate": "0.6174",
        },
        "model": {
            "observed_fill_rate": "0.9334",
            "hidden_fill_rate": "0.5883",
            "served_observed_qty": "100",
            "gross_profit_rub": "1000000",
            "average_inventory_value_rub": "2000000",
            "manual_order_lines": 10_000,
        },
        "control_model": {
            "observed_fill_rate": "0.9304",
            "served_observed_qty": "90",
            "gross_profit_rub": "500000",
            "average_inventory_value_rub": "0",
        },
        "control_scenario": {"economic_contribution_rub": 600_000},
        "best_service_scenario": {
            "observed_fill_rate": 0.9344,
            "gross_profit_delta_vs_control_rub": 700_000,
            "capital_delta_vs_control_rub": 3_000_000,
        },
        "best_economic_scenario": {"economic_contribution_rub": 604_000},
        "stages": [],
        "monthly_chart": [],
        "scenario_chart": [],
        "scenario_comparison": [],
        "factor_effects": [],
        "period_sensitivity": [],
        "site_export": {
            "mapping_stats": {
                "mapped_row_count": 1,
                "out_of_cohort_count": 0,
            }
        },
        "source_quality": {
            "negative_register_balances": {"value": "0"},
            "unit_economics_coverage": {"value": "0"},
        },
    }

    artifact = build_artifact(analysis)

    assert artifact["manifest"]["title"] == (
        "Автозаказ дисплеев: защита «Растим» улучшила результат, но не закрыла дефицит"
    )
    sources = {source["id"]: source for source in artifact["manifest"]["sources"]}
    assert sources["preflight_manifest"]["path"] == (
        "next-stage-model-preflight-grow-protection-v3/run-manifest.json"
    )
    headline = artifact["snapshot"]["datasets"]["headline"][0]
    assert headline["gross_profit_delta_million_rub"] == -10.07181562
    assert headline["capital_delta_million_rub"] == -9.67528562
    cards = {card["id"]: card for card in artifact["manifest"]["cards"]}
    assert cards["profit_delta_card"]["metrics"][0] == {
        "label": "Δ прибыль к факту, млн ₽",
        "field": "gross_profit_delta_million_rub",
        "format": "number",
        "signed": True,
    }
    assert cards["acceptance_card"]["metrics"][0]["field"] == "passed_scenario_count"
    source_notes = build_source_notes(analysis)
    assert "| Обмен сервиса на капитал |" in source_notes
    assert "Site demand is excluded" not in source_notes
