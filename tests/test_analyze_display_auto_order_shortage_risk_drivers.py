from tasks.analyze_display_auto_order_shortage_risk_drivers import (
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
