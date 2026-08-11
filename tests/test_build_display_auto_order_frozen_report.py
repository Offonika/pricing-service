from tasks.build_display_auto_order_frozen_report import build_artifact


def test_frozen_report_metric_cards_render_ruble_values_without_usd_format() -> None:
    analysis = {
        "generated_at": "2026-08-11T00:00:00Z",
        "cohort": {"sku_count": 1, "classification_run_id": 1},
        "headline": {
            "observed_fill_rate": 0.93,
            "observed_fill_delta": -0.07,
            "hidden_fill_rate": 0.58,
            "hidden_fill_delta": -0.03,
            "gross_profit_delta_rub": -10_071_815.62,
            "capital_delta_rub": -9_675_285.62,
            "economic_contribution_delta_rub": -6_953_192.73,
            "manual_order_share": 0.84,
            "extra_lost_total_qty": 10_549,
            "sale_extra_lost_qty": 9_884,
            "sale_extra_lost_share": 0.937,
            "top10_negative_gp_share": 0.159,
        },
        "actual": {"hidden_fill_rate": "0.6174"},
        "model": {"observed_fill_rate": "0.9334", "hidden_fill_rate": "0.5883"},
        "stages": [],
        "monthly_chart": [],
        "scenario_chart": [],
        "source_quality": {
            "negative_register_balances": {"value": "0"},
            "unit_economics_coverage": {"value": "0"},
        },
    }

    artifact = build_artifact(analysis)

    headline = artifact["snapshot"]["datasets"]["headline"][0]
    assert headline["gross_profit_delta_million_rub"] == -10.07181562
    assert headline["capital_delta_million_rub"] == -9.67528562
    cards = {card["id"]: card for card in artifact["manifest"]["cards"]}
    assert cards["profit_delta_card"]["metrics"][0] == {
        "label": "Δ валовая прибыль, млн ₽",
        "field": "gross_profit_delta_million_rub",
        "format": "number",
        "signed": True,
    }
    assert cards["capital_delta_card"]["metrics"][0] == {
        "label": "Δ складской капитал, млн ₽",
        "field": "capital_delta_million_rub",
        "format": "number",
        "signed": True,
    }
