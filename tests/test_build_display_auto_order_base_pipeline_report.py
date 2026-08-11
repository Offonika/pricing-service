from decimal import Decimal

from tasks.build_display_auto_order_base_pipeline_report import (
    build_artifact,
    build_lot_risk_artifact,
    economic_concentration,
)


def test_economic_concentration_uses_only_affected_skus() -> None:
    rows = [
        {
            "pipeline_profile_affected": 1,
            "served_observed_delta_to_control_qty": "2",
            "economic_contribution_delta_to_control_rub": "80",
        },
        {
            "pipeline_profile_affected": 1,
            "served_observed_delta_to_control_qty": "0",
            "economic_contribution_delta_to_control_rub": "20",
        },
        {
            "pipeline_profile_affected": 1,
            "served_observed_delta_to_control_qty": "0",
            "economic_contribution_delta_to_control_rub": "-10",
        },
        {
            "pipeline_profile_affected": 0,
            "served_observed_delta_to_control_qty": "100",
            "economic_contribution_delta_to_control_rub": "1000",
        },
    ]

    result = economic_concentration(rows)

    assert result["affected_sku_count"] == 3
    assert result["sales_gain_sku_count"] == 1
    assert result["positive_economic_sku_count"] == 2
    assert result["negative_economic_sku_count"] == 1
    assert Decimal(result["top_1_positive_share"]) == Decimal("0.8")
    assert result["gross_negative_economic_rub"] == "-10"


def test_report_artifact_has_required_stakeholder_reading_path() -> None:
    analysis = {
        "generated_at": "2026-08-11T20:00:00Z",
        "headline": {
            "sales_delta_qty": "97",
            "economic_delta_rub": "83133",
            "capital_delta_rub": "197786",
            "strict_sales_gap_qty": "9717.25",
            "strict_capital_advantage_rub": "7529880",
            "control_gap_recovered_share": "0.00988",
            "affected_sku_count": 365,
        },
        "scenario_rows": [],
        "period_rows": [],
        "stage_rows": [],
        "acceptance_rows": [],
    }

    artifact = build_artifact(analysis)

    assert artifact["surface"] == "report"
    assert artifact["manifest"]["blocks"][0]["body"].startswith("# ")
    assert artifact["manifest"]["blocks"][1]["body"].startswith("## Executive Summary")
    assert len(artifact["manifest"]["charts"]) == 3
    assert artifact["snapshot"]["status"] == "ready"
    assert artifact["package_info"]["originUrl"].startswith("artifact://")


def test_lot_risk_report_leads_with_answer_and_keeps_three_evidence_charts() -> None:
    analysis = {
        "generated_at": "2026-08-11T21:00:00Z",
        "headline": {
            "incremental_economic_vs_v15_rub": "5440.99",
            "incremental_gross_profit_vs_v15_rub": "5674.54",
            "incremental_capital_vs_v15_rub": "724.57",
            "strict_sales_gap_qty": "9717.25",
            "strict_profit_gap_rub": "9583000",
            "strict_capital_advantage_rub": "7529000",
            "control_gap_recovered_share": "0.00988",
        },
        "scenario_rows": [],
        "lot_risk_rows": [],
        "period_rows": [],
        "stage_rows": [],
        "acceptance_rows": [],
        "source_paths": {
            "v15_summary": "reports/v15/frozen-summary.json",
            "v16_summary": "reports/v16/frozen-summary.json",
            "v16_replay": "reports/v16-replay/analysis-summary.json",
            "v16_segments": "reports/v16-replay/segment-effects.csv",
            "report_analysis": "reports/v16-report/report-analysis.json",
            "method_policy": "docs/specs/assortment-lifecycle-policy.md",
        },
    }

    artifact = build_lot_risk_artifact(analysis)

    blocks = artifact["manifest"]["blocks"]
    assert blocks[0]["body"].startswith("# ")
    assert blocks[1]["body"].startswith("## Executive Summary")
    assert "Новых продаж сверх v15 нет" in blocks[1]["body"]
    assert artifact["manifest"]["cards"] == []
    assert len(artifact["manifest"]["charts"]) == 3
    assert artifact["package_info"]["originUrl"].endswith("lot-risk-v16")
