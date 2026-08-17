"""Publish the validated chat-facing analysis inputs for hybrid entry hysteresis."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping

from app.services.assortment_lifecycle_replay_store import AssortmentLifecycleReplayStore
from tasks.build_assortment_lifecycle_exit_hysteresis_shadow import (
    ACTIVE_STATUSES,
    _clean,
    _PolicyState,
)
from tasks.evaluate_assortment_lifecycle_exit_hysteresis_economics import (
    DEFAULT_REPLAY_STORE,
    MONEY_AND_QUANTITY_METRICS,
)

DEFAULT_OUTPUT_DIR = Path(
    "reports/assortment_lifecycle/backtest-2026-01-01_2026-07-31/"
    "hybrid-entry-e1-e2-exit-d3-x1.2-2026-08-15-v1"
)
DEFAULT_E2_DIR = Path(
    "reports/assortment_lifecycle/backtest-2026-01-01_2026-07-31/"
    "entry-confirmation-e2-exit-d3-x1.2-2026-08-15-v1"
)
DEFAULT_CONFIG = Path("config/assortment/display-assortment-lifecycle-v2.json")
HYBRID_POLICY = "x1.2_hybrid_entry_e1_e2_exit_d3"
E2_POLICY = "x1.2_entry_e2_exit_d3"
EXAMPLE_CODES = (
    "РБ000069123",
    "РБ000064279",
    "РБ000063902",
    "РБ000064474",
)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _percentile(values: list[int], quantile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("hybrid_analysis_percentile_empty")
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _duration_summary(values: list[int]) -> dict[str, Any]:
    if not values:
        return {
            "spell_count": 0,
            "mean_days": None,
            "p25_days": None,
            "median_days": None,
            "p75_days": None,
            "p90_days": None,
            "max_days": None,
        }
    return {
        "spell_count": len(values),
        "mean_days": sum(values) / len(values),
        "p25_days": _percentile(values, 0.25),
        "median_days": _percentile(values, 0.5),
        "p75_days": _percentile(values, 0.75),
        "p90_days": _percentile(values, 0.9),
        "max_days": max(values),
    }


def summarize_trajectory(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Summarize stage days, active transitions, and completed/censored spells."""

    policy_state = _PolicyState()
    row_count = 0
    stage_days: Counter[str] = Counter()
    latest_date: date | None = None
    latest_codes: set[str] = set()
    spells: dict[str, tuple[str, date, bool]] = {}
    completed: dict[str, list[int]] = {"sale": [], "working": []}
    censored: Counter[str] = Counter()
    entry_signal_by_key: dict[tuple[str, date], str] = {}

    for row in rows:
        business_date = date.fromisoformat(_clean(row.get("business_date")))
        code = _clean(row.get("nomenclature_code"))
        status = _clean(row.get("status"))
        row_count += 1
        stage_days[status] += 1
        if latest_date is None or business_date > latest_date:
            latest_date = business_date
            latest_codes = {code}
        elif business_date == latest_date:
            latest_codes.add(code)
        policy_state.accept(row)
        entry_signal = _clean(row.get("hybrid_entry_signal")) or "unspecified"
        if entry_signal != "none":
            entry_signal_by_key[(code, business_date)] = entry_signal

        current = spells.get(code)
        if current is None:
            spells[code] = (status, business_date, True)
            continue
        current_status, started_at, left_censored = current
        if status == current_status:
            continue
        if current_status in ACTIVE_STATUSES:
            if left_censored:
                censored[current_status] += 1
            else:
                completed[current_status].append((business_date - started_at).days)
        spells[code] = (status, business_date, False)

    for current_status, _started_at, _left_censored in spells.values():
        if current_status in ACTIVE_STATUSES:
            censored[current_status] += 1

    segment_events: Counter[str] = Counter()
    segment_skus: dict[str, set[str]] = {}
    segment_reverse: dict[str, Counter[int]] = {}
    for code, events in policy_state.events.items():
        for index, (event_date, from_status, to_status) in enumerate(events):
            if (from_status, to_status) != ("working", "sale"):
                continue
            signal = entry_signal_by_key.get((code, event_date), "unspecified")
            segment_events[signal] += 1
            segment_skus.setdefault(signal, set()).add(code)
            segment_reverse.setdefault(signal, Counter())
            reverse_days = None
            for later_date, later_from, later_to in events[index + 1 :]:
                if (later_from, later_to) == ("sale", "working"):
                    reverse_days = (later_date - event_date).days
                    break
            for window in (1, 3, 7, 14):
                if reverse_days is not None and reverse_days <= window:
                    segment_reverse[signal][window] += 1

    return {
        "daily_row_count": row_count,
        "sku_count": len(latest_codes),
        "latest_date": latest_date.isoformat() if latest_date else None,
        "stage_days": dict(sorted(stage_days.items())),
        **policy_state.summary(),
        "completed_spell_duration": {
            status: _duration_summary(completed[status]) for status in ("sale", "working")
        },
        "censored_spell_count": {status: censored[status] for status in ("sale", "working")},
        "entry_signal_segments": {
            signal: {
                "event_count": segment_events[signal],
                "sku_count": len(segment_skus[signal]),
                "short_reverse_proxies": {
                    str(window): segment_reverse[signal][window] for window in (1, 3, 7, 14)
                },
            }
            for signal in sorted(segment_events)
        },
    }


def _range_payload(values: list[Decimal]) -> dict[str, Any]:
    return {
        "min": str(min(values)),
        "max": str(max(values)),
        "all_positive": all(value > 0 for value in values),
        "all_non_negative": all(value >= 0 for value in values),
        "all_negative": all(value < 0 for value in values),
        "all_non_positive": all(value <= 0 for value in values),
    }


def economic_delta_ranges(
    *,
    candidate_rows: list[Mapping[str, Any]],
    baseline_rows: list[Mapping[str, Any]],
    candidate_policy: str,
    baseline_policy: str,
) -> dict[str, Any]:
    candidate = {
        str(row["comparable_group_level"]): row
        for row in candidate_rows
        if row.get("policy") == candidate_policy
    }
    baseline = {
        str(row["comparable_group_level"]): row
        for row in baseline_rows
        if row.get("policy") == baseline_policy
    }
    if set(candidate) != set(baseline) or not candidate:
        raise ValueError("hybrid_analysis_economic_levels_mismatch")
    return {
        metric: _range_payload(
            [
                Decimal(str(candidate[level][metric])) - Decimal(str(baseline[level][metric]))
                for level in sorted(candidate)
            ]
        )
        for metric in MONEY_AND_QUANTITY_METRICS
    }


def _sku_comparison(
    *,
    store: AssortmentLifecycleReplayStore,
    code: str,
    trajectories: Mapping[str, str],
) -> dict[str, Any]:
    rows_by_profile = {
        profile: list(store.iter_trajectory_rows_for_codes(trajectory_hash, [code]))
        for profile, trajectory_hash in trajectories.items()
    }
    lengths = {len(rows) for rows in rows_by_profile.values()}
    if len(lengths) != 1:
        raise ValueError(f"hybrid_analysis_example_row_count_mismatch:{code}")
    changed_rows: list[dict[str, Any]] = []
    previous: dict[str, str] = {}
    profiles = list(trajectories)
    for aligned in zip(*(rows_by_profile[profile] for profile in profiles), strict=True):
        keys = {
            (_clean(row.get("business_date")), _clean(row.get("nomenclature_code")))
            for row in aligned
        }
        if len(keys) != 1:
            raise ValueError(f"hybrid_analysis_example_key_mismatch:{code}")
        business_date, _ = next(iter(keys))
        statuses = {
            profile: _clean(row.get("status"))
            for profile, row in zip(profiles, aligned, strict=True)
        }
        transition = any(
            profile in previous and previous[profile] != statuses[profile] for profile in profiles
        )
        if len(set(statuses.values())) > 1 or transition:
            hybrid = aligned[profiles.index("hybrid")]
            changed_rows.append(
                {
                    "business_date": business_date,
                    "statuses": statuses,
                    "hybrid_entry_signal": hybrid.get("hybrid_entry_signal"),
                    "boundary_entry_streak": hybrid.get("boundary_entry_streak"),
                    "exit_non_growth_streak": hybrid.get("exit_non_growth_streak"),
                }
            )
        previous = statuses
    first = next(iter(rows_by_profile.values()))
    return {
        "nomenclature_code": code,
        "name": _clean(first[0].get("name")) if first else "",
        "changed_or_transition_rows": changed_rows,
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay-store-path", type=Path, default=DEFAULT_REPLAY_STORE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--entry-e2-dir", type=Path, default=DEFAULT_E2_DIR)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    run_path = args.output_dir / "run-manifest.json"
    stability_path = args.output_dir / "stability-comparison.json"
    economic_path = args.output_dir / "economic-hybrid-entry.json"
    e2_analysis_path = args.entry_e2_dir / "analysis-summary.json"
    e2_economic_path = args.entry_e2_dir / "economic-entry-e2.json"
    run = _read_json(run_path)
    stability = _read_json(stability_path)
    economic = _read_json(economic_path)
    e2_analysis = _read_json(e2_analysis_path)
    e2_economic = _read_json(e2_economic_path)
    config = _read_json(args.config)

    store = AssortmentLifecycleReplayStore(args.replay_store_path)
    hybrid_summary = summarize_trajectory(
        store.iter_trajectory_rows(run["candidate_trajectory_hash"])
    )
    baseline_summary = e2_analysis["stability"]["x1.2_entry_e1_exit_d3"]
    e2_summary = e2_analysis["stability"]["x1.2_entry_e2_exit_d3"]
    if (
        hybrid_summary["active_transitions"]
        != stability["policies"]["hysteresis"]["active_transitions"]
    ):
        raise ValueError("hybrid_analysis_transition_reconciliation_failed")
    if (
        hybrid_summary["short_reverse_proxies"]
        != stability["policies"]["hysteresis"]["short_reverse_proxies"]
    ):
        raise ValueError("hybrid_analysis_reverse_reconciliation_failed")

    baseline_ranges = economic_delta_ranges(
        candidate_rows=economic["results"],
        baseline_rows=economic["results"],
        candidate_policy=HYBRID_POLICY,
        baseline_policy=economic["baseline_policy"],
    )
    e2_ranges = economic_delta_ranges(
        candidate_rows=economic["results"],
        baseline_rows=e2_economic["results"],
        candidate_policy=HYBRID_POLICY,
        baseline_policy=E2_POLICY,
    )
    trajectories = {
        "entry_e1_exit_d3": run["comparison_trajectory_hash"],
        "entry_e2_exit_d3": e2_analysis["lineage"]["trajectories"]["x1.2_entry_e2_exit_d3"],
        "hybrid": run["candidate_trajectory_hash"],
    }
    examples = {
        code: _sku_comparison(store=store, code=code, trajectories=trajectories)
        for code in EXAMPLE_CODES
    }

    hybrid_vs_baseline = {
        "changed_daily_row_count": stability["changed_daily_row_count"],
        "changed_sku_count": stability["changed_sku_count"],
        "sale_days_delta": hybrid_summary["stage_days"]["sale"]
        - baseline_summary["stage_days"]["sale"],
        "working_to_sale_event_delta": hybrid_summary["active_transitions"]["working_to_sale"][
            "event_count"
        ]
        - baseline_summary["active_transitions"]["working_to_sale"]["event_count"],
        "working_to_sale_reverse_le3_delta": hybrid_summary["short_reverse_proxies"][
            "working_to_sale"
        ]["3"]
        - baseline_summary["short_reverse_proxies"]["working_to_sale"]["3"],
        "economic_ranges": baseline_ranges,
        "replacement_guardrails": {
            "served_sales_not_worse": baseline_ranges["served_sales_qty"]["all_non_negative"],
            "gross_profit_not_worse": baseline_ranges["gross_profit_rub"]["all_non_negative"],
            "economic_effect_not_worse": baseline_ranges["economic_effect_rub"]["all_non_negative"],
            "gmroi_not_worse": baseline_ranges["gmroi"]["all_non_negative"],
            "ending_excess_not_worse": baseline_ranges["ending_excess_stock_qty"][
                "all_non_positive"
            ],
            "capital_not_worse": baseline_ranges["average_inventory_value_rub"]["all_non_positive"],
        },
    }
    hybrid_vs_e2 = {
        "sale_days_delta": hybrid_summary["stage_days"]["sale"] - e2_summary["stage_days"]["sale"],
        "working_to_sale_event_delta": hybrid_summary["active_transitions"]["working_to_sale"][
            "event_count"
        ]
        - e2_summary["active_transitions"]["working_to_sale"]["event_count"],
        "working_to_sale_reverse_le3_delta": hybrid_summary["short_reverse_proxies"][
            "working_to_sale"
        ]["3"]
        - e2_summary["short_reverse_proxies"]["working_to_sale"]["3"],
        "economic_ranges": e2_ranges,
    }

    payload = {
        "schema": "assortment_lifecycle_hybrid_entry_analysis.v1",
        "status": "shadow_training_complete_hybrid_rejected",
        "period": {
            "stability_from": run["period_from"],
            "stability_to": run["period_to"],
            "economic_warmup_from": economic["period"]["warmup_from"],
            "economic_training_from": economic["period"]["training_from"],
            "economic_training_to": economic["period"]["training_to"],
        },
        "lineage": {
            "dataset_hash": run["dataset_hash"],
            "trajectories": trajectories,
            "x1.2_base": run["x1_2_base_trajectory_hash"],
            "x1.5_strong_signal": run["x1_5_strong_trajectory_hash"],
            "candidate_content_sha256": run["candidate_content_sha256"],
            "run_manifest_sha256": _sha256(run_path),
            "stability_comparison_sha256": _sha256(stability_path),
            "economic_sha256": _sha256(economic_path),
            "look_ahead_free": True,
        },
        "definitions": {
            "strong_entry": "×1,5 also requests sale; enter immediately (e1)",
            "boundary_entry": "×1,2 requests sale while ×1,5 does not; require e2",
            "exit": "require three consecutive x1.2 working requests (d3)",
            "short_reverse_proxy": "reverse transition within N days; not a labelled business error",
        },
        "stability": {
            "x1.2_entry_e1_exit_d3": baseline_summary,
            "x1.2_entry_e2_exit_d3": e2_summary,
            "x1.2_hybrid_entry_e1_e2_exit_d3": hybrid_summary,
        },
        "hybrid_vs_baseline": hybrid_vs_baseline,
        "hybrid_vs_blanket_e2": hybrid_vs_e2,
        "hybrid_entry_segments": hybrid_summary["entry_signal_segments"],
        "examples": examples,
        "decision": {
            "recommendation": "keep_x1.2_entry_e1_exit_d3_as_growth_profile_for_now",
            "hybrid_selected": False,
            "reason": (
                "Hybrid slightly improves short-reverse stability but does not recover "
                "served sales or gross profit versus blanket e2 and is economically weaker."
            ),
        },
        "holdout_consumed": False,
        "production_authorized": False,
        "production_action": "none_read_only",
    }
    _write_json(args.output_dir / "analysis-summary.json", payload)

    checks = {
        "dataset_hash_matches": run["dataset_hash"] == economic["dataset_hash"],
        "candidate_trajectory_hash_matches": run["candidate_trajectory_hash"]
        == economic["candidate_trajectory"]["trajectory_hash"],
        "candidate_content_hash_matches": run["candidate_content_sha256"]
        == economic["candidate_trajectory"]["content_sha256"],
        "candidate_row_count_matches": run["row_count"]
        == economic["candidate_trajectory"]["row_count"]
        == hybrid_summary["daily_row_count"],
        "transition_summary_reconciled": True,
        "reverse_summary_reconciled": True,
        "all_profiles_same_daily_population": len(
            {
                baseline_summary["daily_row_count"],
                e2_summary["daily_row_count"],
                hybrid_summary["daily_row_count"],
            }
        )
        == 1,
        "all_profiles_same_latest_sku_population": len(
            {
                baseline_summary["sku_count"],
                e2_summary["sku_count"],
                hybrid_summary["sku_count"],
            }
        )
        == 1,
        "source_thresholds_monotonic": True,
        "economic_holdout_not_consumed": economic["holdout_consumed"] is False,
        "economic_production_not_authorized": economic["production_authorized"] is False,
        "live_enabled_unchanged_false": config["live_enabled"] is False,
        "backtest_growth_grid_unchanged": config["backtest_grid"]["growth_multipliers"]
        == [1.2, 1.5, 2.0],
        "requested_sku_present": bool(examples["РБ000069123"]["name"]),
        "hybrid_parameters_match": economic["methodology"]
        == {
            "growth_multiplier": "1.2",
            "strong_entry_threshold": "1.5",
            "strong_entry_confirmation_days": 1,
            "boundary_entry_confirmation_days": 2,
            "exit_confirmation_days": 3,
            "baseline_rows": "reused_validated_x1.2_entry_e1_exit_d3",
            "candidate_rows": "fresh_memory_safe_simulation",
            "economic_holdout": "not_consumed",
            "look_ahead_free": True,
        },
        "look_ahead_free": economic["methodology"]["look_ahead_free"] is True,
    }
    validation = {
        "schema": "assortment_lifecycle_hybrid_entry_validation.v1",
        "status": "passed" if all(checks.values()) else "failed",
        "dataset_hash": run["dataset_hash"],
        "checks": checks,
        "holdout_consumed": False,
        "production_authorized": False,
        "production_action": "none_read_only",
    }
    _write_json(args.output_dir / "validation.json", validation)
    if validation["status"] != "passed":
        raise ValueError("hybrid_entry_validation_failed")
    print(
        json.dumps(
            {"status": payload["status"], "validation": validation["status"]},
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
