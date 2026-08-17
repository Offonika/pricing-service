"""Diagnose look-ahead-free risk rules for first-day x1.2 boundary entries.

The task reads immutable x1.2+d3 and hybrid trajectories from the replay store.
It calibrates a small predeclared rule set on February-April and evaluates only
the calibration winner on May-June.  July is never used and no source replay or
external write is performed.
"""

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from datetime import date
from itertools import zip_longest
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from app.services.assortment_lifecycle_replay_store import AssortmentLifecycleReplayStore
from tasks.build_assortment_lifecycle_exit_hysteresis_shadow import (
    DEFAULT_BATCH_SIZE,
    _clean,
)
from tasks.evaluate_assortment_lifecycle_exit_hysteresis_economics import (
    DEFAULT_DATASET_HASH,
    DEFAULT_REPLAY_STORE,
)
from tasks.run_assortment_lifecycle_v2_economic_backtest import _soft_rate

DEFAULT_BASELINE_TRAJECTORY_HASH = (
    "8beccc1d06b43e87fadd2bcba9662794ea3fee2650ec33faa000e631b6f91cea"
)
DEFAULT_HYBRID_TRAJECTORY_HASH = "8d2abd30c81e1c4934b5d67a4a231b6664ee13958b9819e575c259d8391f72ab"
DEFAULT_OUTPUT_DIR = Path(
    "reports/assortment_lifecycle/backtest-2026-01-01_2026-07-31/"
    "boundary-entry-quantity-guardrail-x1.2-2026-08-15-v1"
)
CALIBRATION_FROM = date(2026, 2, 1)
CALIBRATION_TO = date(2026, 4, 30)
VALIDATION_FROM = date(2026, 5, 1)
VALIDATION_TO = date(2026, 6, 16)
PRIMARY_REVERSE_DAYS = 7
SECONDARY_REVERSE_DAYS = 14
MIN_CALIBRATION_FLAGGED = 30
MIN_CALIBRATION_CAPTURE = 0.20
MIN_VALIDATION_FLAGGED = 20
MIN_VALIDATION_LIFT = 1.25
MIN_VALIDATION_CAPTURE = 0.15


@dataclass(frozen=True)
class BoundaryEpisode:
    business_date: date
    nomenclature_code: str
    name: str
    reverse_days: int | None
    sales_30: int
    sales_90: int
    sales_180: int
    available_days_30: int | None
    available_days_90: int | None
    active_days_30: int
    document_count_30: int
    customer_count_30: int
    point_count_30: int
    max_day_share_30: float
    adjusted_growth_ratio_30_to_90: float

    @property
    def reverse_within_7(self) -> bool:
        return self.reverse_days is not None and self.reverse_days <= PRIMARY_REVERSE_DAYS

    @property
    def reverse_within_14(self) -> bool:
        return self.reverse_days is not None and self.reverse_days <= SECONDARY_REVERSE_DAYS

    @property
    def minimum_breadth_30(self) -> int:
        return min(
            self.active_days_30,
            self.document_count_30,
            self.customer_count_30,
            self.point_count_30,
        )


@dataclass(frozen=True)
class RiskRule:
    rule_id: str
    description: str
    predicate: Callable[[BoundaryEpisode], bool]


def _rules() -> tuple[RiskRule, ...]:
    return (
        RiskRule(
            "growth_ratio_lt_1p30",
            "adjusted growth ratio 30/90 < 1.30",
            lambda row: row.adjusted_growth_ratio_30_to_90 < 1.30,
        ),
        RiskRule(
            "growth_ratio_lt_1p35",
            "adjusted growth ratio 30/90 < 1.35",
            lambda row: row.adjusted_growth_ratio_30_to_90 < 1.35,
        ),
        RiskRule(
            "growth_ratio_lt_1p40",
            "adjusted growth ratio 30/90 < 1.40",
            lambda row: row.adjusted_growth_ratio_30_to_90 < 1.40,
        ),
        RiskRule(
            "growth_ratio_lt_1p45",
            "adjusted growth ratio 30/90 < 1.45",
            lambda row: row.adjusted_growth_ratio_30_to_90 < 1.45,
        ),
        RiskRule(
            "sales_30_lte_3",
            "sales_30 <= 3",
            lambda row: row.sales_30 <= 3,
        ),
        RiskRule(
            "sales_30_lte_4",
            "sales_30 <= 4",
            lambda row: row.sales_30 <= 4,
        ),
        RiskRule(
            "minimum_breadth_lte_2",
            "minimum independent 30-day breadth <= 2",
            lambda row: row.minimum_breadth_30 <= 2,
        ),
        RiskRule(
            "max_day_share_gte_0p5",
            "maximum single-day share >= 0.5",
            lambda row: row.max_day_share_30 >= 0.5,
        ),
        RiskRule(
            "ratio_lt_1p35_or_breadth_lte_2",
            "growth ratio < 1.35 or minimum breadth <= 2",
            lambda row: (row.adjusted_growth_ratio_30_to_90 < 1.35 or row.minimum_breadth_30 <= 2),
        ),
    )


def _optional_int(value: Any) -> int | None:
    text = _clean(value)
    return int(float(text)) if text else None


def _int(value: Any) -> int:
    return int(float(_clean(value) or 0))


def _float(value: Any) -> float:
    return float(_clean(value) or 0)


def _growth_ratio(row: Mapping[str, Any]) -> float:
    sales_30 = _float(row.get("sales_30"))
    sales_90 = _float(row.get("sales_90"))
    short = _soft_rate(sales_30, 30, _optional_int(row.get("available_days_30")))
    medium = _soft_rate(sales_90, 90, _optional_int(row.get("available_days_90")))
    return short / medium if medium > 0 else float("inf")


def _episode_from_row(row: Mapping[str, Any], *, reverse_days: int | None) -> BoundaryEpisode:
    return BoundaryEpisode(
        business_date=date.fromisoformat(_clean(row.get("business_date"))),
        nomenclature_code=_clean(row.get("nomenclature_code")),
        name=_clean(row.get("name")),
        reverse_days=reverse_days,
        sales_30=_int(row.get("sales_30")),
        sales_90=_int(row.get("sales_90")),
        sales_180=_int(row.get("sales_180")),
        available_days_30=_optional_int(row.get("available_days_30")),
        available_days_90=_optional_int(row.get("available_days_90")),
        active_days_30=_int(row.get("sales_active_days_30")),
        document_count_30=_int(row.get("sales_document_count_30")),
        customer_count_30=_int(row.get("sales_customer_count_30")),
        point_count_30=_int(row.get("sales_point_count_30")),
        max_day_share_30=_float(row.get("sales_max_day_share_30")),
        adjusted_growth_ratio_30_to_90=_growth_ratio(row),
    )


def extract_boundary_episodes(
    *,
    store: AssortmentLifecycleReplayStore,
    baseline_trajectory_hash: str,
    hybrid_trajectory_hash: str,
    dataset_hash: str,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> list[BoundaryEpisode]:
    if batch_size < 1 or batch_size > 500:
        raise ValueError("boundary_risk_batch_size_invalid")
    episodes: list[BoundaryEpisode] = []
    codes = store.dataset_codes(dataset_hash)
    for offset in range(0, len(codes), batch_size):
        batch = codes[offset : offset + batch_size]
        baseline_rows = list(store.iter_trajectory_rows_for_codes(baseline_trajectory_hash, batch))
        hybrid_rows = list(store.iter_trajectory_rows_for_codes(hybrid_trajectory_hash, batch))
        baseline_by_code: dict[str, list[Mapping[str, Any]]] = {}
        candidate_rows: list[Mapping[str, Any]] = []
        for baseline, hybrid in zip_longest(baseline_rows, hybrid_rows):
            if baseline is None or hybrid is None:
                raise ValueError("boundary_risk_trajectory_row_count_mismatch")
            baseline_key = (
                _clean(baseline.get("nomenclature_code")),
                _clean(baseline.get("business_date")),
            )
            hybrid_key = (
                _clean(hybrid.get("nomenclature_code")),
                _clean(hybrid.get("business_date")),
            )
            if baseline_key != hybrid_key:
                raise ValueError(
                    f"boundary_risk_trajectory_key_mismatch:{baseline_key}:{hybrid_key}"
                )
            baseline_by_code.setdefault(baseline_key[0], []).append(baseline)
            if (
                hybrid.get("hybrid_entry_signal") == "boundary_x1.2_to_x1.5"
                and _int(hybrid.get("boundary_entry_streak")) == 1
                and _clean(baseline.get("status")) == "sale"
                and _clean(baseline.get("previous_status")) == "working"
            ):
                candidate_rows.append(hybrid)

        for row in candidate_rows:
            code = _clean(row.get("nomenclature_code"))
            entry_date = date.fromisoformat(_clean(row.get("business_date")))
            reverse_days = None
            for baseline in baseline_by_code[code]:
                business_date = date.fromisoformat(_clean(baseline.get("business_date")))
                if business_date <= entry_date:
                    continue
                if (
                    _clean(baseline.get("previous_status")) == "sale"
                    and _clean(baseline.get("status")) == "working"
                ):
                    reverse_days = (business_date - entry_date).days
                    break
            episodes.append(_episode_from_row(row, reverse_days=reverse_days))
        print(
            json.dumps(
                {
                    "completed_sku_count": min(offset + len(batch), len(codes)),
                    "episode_count": len(episodes),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    return episodes


def rule_metrics(rows: Iterable[BoundaryEpisode], *, rule: RiskRule) -> dict[str, Any]:
    population = list(rows)
    flagged = [row for row in population if rule.predicate(row)]
    unflagged = [row for row in population if not rule.predicate(row)]
    total_risk = sum(row.reverse_within_7 for row in population)
    flagged_risk = sum(row.reverse_within_7 for row in flagged)
    unflagged_risk = sum(row.reverse_within_7 for row in unflagged)
    overall_rate = total_risk / len(population) if population else 0.0
    flagged_rate = flagged_risk / len(flagged) if flagged else 0.0
    unflagged_rate = unflagged_risk / len(unflagged) if unflagged else 0.0
    return {
        "rule_id": rule.rule_id,
        "description": rule.description,
        "episode_count": len(population),
        "flagged_count": len(flagged),
        "flagged_share": len(flagged) / len(population) if population else 0.0,
        "reverse_within_7_count": total_risk,
        "overall_reverse_within_7_rate": overall_rate,
        "flagged_reverse_within_7_count": flagged_risk,
        "flagged_reverse_within_7_rate": flagged_rate,
        "unflagged_reverse_within_7_count": unflagged_risk,
        "unflagged_reverse_within_7_rate": unflagged_rate,
        "risk_lift_vs_overall": flagged_rate / overall_rate if overall_rate else 0.0,
        "risk_lift_vs_unflagged": (flagged_rate / unflagged_rate if unflagged_rate else None),
        "reverse_within_7_capture": flagged_risk / total_risk if total_risk else 0.0,
        "flagged_reverse_within_14_count": sum(row.reverse_within_14 for row in flagged),
        "flagged_reverse_within_14_rate": (
            sum(row.reverse_within_14 for row in flagged) / len(flagged) if flagged else 0.0
        ),
    }


def select_calibration_rule(metrics: Iterable[Mapping[str, Any]]) -> str | None:
    eligible = [
        row
        for row in metrics
        if int(row["flagged_count"]) >= MIN_CALIBRATION_FLAGGED
        and float(row["reverse_within_7_capture"]) >= MIN_CALIBRATION_CAPTURE
    ]
    if not eligible:
        return None
    winner = max(
        eligible,
        key=lambda row: (
            float(row["flagged_reverse_within_7_rate"]),
            float(row["reverse_within_7_capture"]),
            str(row["rule_id"]),
        ),
    )
    return str(winner["rule_id"])


def validation_passes(metrics: Mapping[str, Any]) -> bool:
    return bool(
        int(metrics["flagged_count"]) >= MIN_VALIDATION_FLAGGED
        and float(metrics["risk_lift_vs_overall"]) >= MIN_VALIDATION_LIFT
        and float(metrics["reverse_within_7_capture"]) >= MIN_VALIDATION_CAPTURE
        and float(metrics["flagged_reverse_within_7_rate"])
        > float(metrics["unflagged_reverse_within_7_rate"])
    )


def _episode_row(row: BoundaryEpisode, selected_rule: RiskRule | None) -> dict[str, Any]:
    return {
        "business_date": row.business_date.isoformat(),
        "nomenclature_code": row.nomenclature_code,
        "name": row.name,
        "reverse_days": row.reverse_days if row.reverse_days is not None else "",
        "reverse_within_7": int(row.reverse_within_7),
        "reverse_within_14": int(row.reverse_within_14),
        "selected_rule_flagged": (
            int(selected_rule.predicate(row)) if selected_rule is not None else 0
        ),
        "sales_30": row.sales_30,
        "sales_90": row.sales_90,
        "sales_180": row.sales_180,
        "available_days_30": row.available_days_30,
        "available_days_90": row.available_days_90,
        "minimum_breadth_30": row.minimum_breadth_30,
        "max_day_share_30": row.max_day_share_30,
        "adjusted_growth_ratio_30_to_90": row.adjusted_growth_ratio_30_to_90,
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay-store-path", type=Path, default=DEFAULT_REPLAY_STORE)
    parser.add_argument("--dataset-hash", default=DEFAULT_DATASET_HASH)
    parser.add_argument("--baseline-trajectory-hash", default=DEFAULT_BASELINE_TRAJECTORY_HASH)
    parser.add_argument("--hybrid-trajectory-hash", default=DEFAULT_HYBRID_TRAJECTORY_HASH)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    store = AssortmentLifecycleReplayStore(args.replay_store_path)
    episodes = extract_boundary_episodes(
        store=store,
        baseline_trajectory_hash=args.baseline_trajectory_hash,
        hybrid_trajectory_hash=args.hybrid_trajectory_hash,
        dataset_hash=args.dataset_hash,
        batch_size=args.batch_size,
    )
    calibration = [
        row for row in episodes if CALIBRATION_FROM <= row.business_date <= CALIBRATION_TO
    ]
    validation = [row for row in episodes if VALIDATION_FROM <= row.business_date <= VALIDATION_TO]
    rules = _rules()
    calibration_metrics = [rule_metrics(calibration, rule=rule) for rule in rules]
    selected_rule_id = select_calibration_rule(calibration_metrics)
    selected_rule = next((rule for rule in rules if rule.rule_id == selected_rule_id), None)
    validation_metrics = (
        rule_metrics(validation, rule=selected_rule) if selected_rule is not None else None
    )
    passed = validation_metrics is not None and validation_passes(validation_metrics)
    selected_keys = (
        sorted(
            (row.business_date.isoformat(), row.nomenclature_code)
            for row in episodes
            if CALIBRATION_FROM <= row.business_date <= date(2026, 6, 30)
            and selected_rule is not None
            and selected_rule.predicate(row)
        )
        if passed
        else []
    )
    payload = {
        "schema": "assortment_lifecycle_boundary_entry_risk_diagnostic.v1",
        "status": (
            "temporal_validation_passed_quantity_test_allowed"
            if passed
            else "temporal_validation_failed_no_quantity_test"
        ),
        "dataset_hash": args.dataset_hash,
        "baseline_trajectory_hash": args.baseline_trajectory_hash,
        "hybrid_trajectory_hash": args.hybrid_trajectory_hash,
        "periods": {
            "calibration_from": CALIBRATION_FROM.isoformat(),
            "calibration_to": CALIBRATION_TO.isoformat(),
            "validation_from": VALIDATION_FROM.isoformat(),
            "validation_to": VALIDATION_TO.isoformat(),
            "economic_training_to": "2026-06-30",
        },
        "primary_outcome": "baseline_x1.2_e1_d3_sale_to_working_reverse_within_7_days",
        "secondary_outcome": "reverse_within_14_days",
        "selection": {
            "rule_set_predeclared": True,
            "selected_on": "calibration_only",
            "selected_rule_id": selected_rule_id,
            "selected_rule_description": selected_rule.description if selected_rule else None,
            "calibration_min_flagged": MIN_CALIBRATION_FLAGGED,
            "calibration_min_capture": MIN_CALIBRATION_CAPTURE,
        },
        "calibration_rule_metrics": calibration_metrics,
        "validation_selected_rule_metrics": validation_metrics,
        "validation_gates": {
            "min_flagged": MIN_VALIDATION_FLAGGED,
            "min_lift_vs_overall": MIN_VALIDATION_LIFT,
            "min_capture": MIN_VALIDATION_CAPTURE,
            "flagged_rate_above_unflagged": True,
            "passed": passed,
        },
        "selected_training_key_count": len(selected_keys),
        "selected_training_keys": [
            {"business_date": business_date, "nomenclature_code": code}
            for business_date, code in selected_keys
        ],
        "look_ahead_free_features": True,
        "july_holdout_consumed": False,
        "production_authorized": False,
        "production_action": "none_read_only",
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "boundary-risk-diagnostic.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with (args.output_dir / "boundary-entry-episodes.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as handle:
        fieldnames = list(_episode_row(episodes[0], selected_rule)) if episodes else []
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if fieldnames:
            writer.writeheader()
            writer.writerows(_episode_row(row, selected_rule) for row in episodes)
    print(
        json.dumps(
            {
                "status": payload["status"],
                "episode_count": len(episodes),
                "calibration_count": len(calibration),
                "validation_count": len(validation),
                "selected_rule_id": selected_rule_id,
                "selected_training_key_count": len(selected_keys),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
