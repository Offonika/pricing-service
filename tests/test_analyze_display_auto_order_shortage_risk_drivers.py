from tasks.analyze_display_auto_order_shortage_risk_drivers import (
    _report_artifact,
    detection_status,
    episode_driver_flags,
    false_alarm_reason,
    map_episodes_to_case_windows,
)


def test_episode_driver_flags_keep_pipeline_and_demand_shock_separate() -> None:
    both = episode_driver_flags(
        {
            "mechanism": "pipeline_counted_before_arrival",
            "recoverability": "pipeline_blocked_at_last_chance",
            "observed_above_forecast_to_first_loss": "1",
            "forecast_shortfall_to_first_loss_qty": "3",
        }
    )
    shock_only = episode_driver_flags(
        {
            "mechanism": "base_target_underforecast",
            "observed_above_forecast_to_first_loss": "1",
            "forecast_shortfall_to_first_loss_qty": "2",
        }
    )

    assert both["driver"] == "pipeline_and_demand_shock"
    assert shock_only["driver"] == "demand_shock_only"


def test_episode_mapping_uses_latest_case_window_covering_loss_start() -> None:
    episodes = [
        {
            "episode_id": "SKU-1:2026-03-15:1",
            "nomenclature_code": "SKU-1",
            "status": "sale",
            "episode_start": "2026-03-15",
            "mechanism": "base_target_underforecast",
        }
    ]
    windows = [
        {
            "opportunity_id": "SKU-1:2026-03-01",
            "nomenclature_code": "SKU-1",
            "decision_date": "2026-03-01",
            "outcome_end": "2026-03-20",
        },
        {
            "opportunity_id": "SKU-1:2026-03-08",
            "nomenclature_code": "SKU-1",
            "decision_date": "2026-03-08",
            "outcome_end": "2026-03-27",
        },
    ]

    mapped, unmapped = map_episodes_to_case_windows(episodes, windows)

    assert not unmapped
    assert mapped[0]["case_opportunity_id"] == "SKU-1:2026-03-08"


def test_detection_status_distinguishes_unscored_from_miss() -> None:
    assert (
        detection_status({"case_expected_shortage_qty": "0", "control_expected_shortage_qty": "0"})
        == "unscored"
    )
    assert (
        detection_status({"case_expected_shortage_qty": "1", "control_expected_shortage_qty": "2"})
        == "missed"
    )
    assert (
        detection_status({"case_expected_shortage_qty": "3", "control_expected_shortage_qty": "2"})
        == "detected"
    )


def test_false_alarm_reason_uses_realized_demand_before_inventory_proxy() -> None:
    assert (
        false_alarm_reason(
            {
                "actual_demand_qty": "8",
                "forecast_demand_qty": "10",
                "as_of_inventory_position_qty": "1",
            }
        )
        == "demand_not_above_forecast"
    )
    assert (
        false_alarm_reason(
            {
                "actual_demand_qty": "12",
                "forecast_demand_qty": "10",
                "as_of_inventory_position_qty": "15",
            }
        )
        == "starting_position_absorbed_demand"
    )


def test_report_artifact_has_reproducible_report_contract() -> None:
    detection_rows = []
    for period in ("pre_final_month", "final_month_exposed"):
        for status, label, loss in (
            ("detected", "Риск распознан", "6"),
            ("missed", "Риск пропущен", "4"),
            ("unscored", "Не хватило истории", "0"),
        ):
            detection_rows.append(
                {
                    "period": period,
                    "detection_status": status,
                    "detection_label": label,
                    "pair_count": "1",
                    "lost_observed_qty": loss,
                    "loss_share": str(float(loss) / 10),
                }
            )
    driver_rows = []
    for period in ("pre_final_month", "final_month_exposed"):
        for driver, label, loss in (
            ("pipeline_and_demand_shock", "Pipeline и скачок спроса", "3"),
            ("pipeline_only", "Только pipeline", "1"),
        ):
            driver_rows.append(
                {
                    "period": period,
                    "detection_status": "missed",
                    "driver": driver,
                    "driver_label": label,
                    "episode_count": "1",
                    "pair_count": "1",
                    "lost_observed_qty": loss,
                    "share_within_detection_status": str(float(loss) / 4),
                }
            )
    summary = {
        "headline": {
            "matched_loss_qty": "20",
            "same_period_pair_count": 6,
            "false_alarm_pair_count": 2,
        },
        "detection_performance": detection_rows,
        "driver_breakdown": driver_rows,
        "feature_gaps": [
            {
                "period": "final_month_exposed",
                "detection_status": "missed",
                "feature": feature,
                "case_minus_control": value,
            }
            for feature, value in (
                ("position_cover", "0.7"),
                ("open_signal_qty", "0.1"),
                ("acceleration_30_forecast", "0.06"),
            )
        ],
    }

    artifact = _report_artifact(summary, [])

    assert artifact["surface"] == "report"
    assert artifact["manifest"]["blocks"][0]["body"].startswith("# ")
    assert len(artifact["manifest"]["charts"]) == 2
    assert artifact["manifest"]["tables"][0]["defaultSort"] == {
        "field": "quantity",
        "direction": "desc",
    }
    assert artifact["sources"][0]["query"]["sql"].startswith("WITH source_rows")
    assert set(artifact["snapshot"]["datasets"]) == {
        "headline",
        "detection_performance",
        "missed_drivers",
        "manual_review",
    }
