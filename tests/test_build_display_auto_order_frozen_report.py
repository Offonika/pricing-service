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
            "hidden_kmp4_qty": 1_000,
            "hidden_site_order_qty": 500,
            "hidden_site_cart_qty": 10,
            "hidden_reserve_backlog_qty": 0,
        },
        "actual": {
            "observed_fill_rate": "1.0000",
            "hidden_fill_rate": "0.6174",
        },
        "model": {"observed_fill_rate": "0.9334", "hidden_fill_rate": "0.5883"},
        "stages": [],
        "monthly_chart": [],
        "scenario_chart": [],
        "site_sensitivity": [],
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
        "Автозаказ дисплеев: почему модель не прошла проверку на истории"
    )
    sources = {source["id"]: source for source in artifact["manifest"]["sources"]}
    assert sources["preflight_manifest"]["path"] == (
        "next-stage-model-preflight-site-reserve-v2/run-manifest.json"
    )
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
    source_notes = build_source_notes(analysis)
    assert "| Профили сайта |" in source_notes
    assert "Site demand is excluded" not in source_notes
