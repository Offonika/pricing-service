"""Stream a saved legacy/v2 replay into the auditable stage diff artifacts."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import date
from itertools import zip_longest
from pathlib import Path
from typing import Any, Mapping

from app.services.assortment_lifecycle import AssortmentStatus
from app.services.assortment_lifecycle_replay_store import (
    DEFAULT_REPLAY_STORE_PATH,
    AssortmentLifecycleReplayStore,
)

DEFAULT_LEGACY_TRAJECTORY_HASH = "8759f62279284d62e36bb47df39634227a48a4e959c56acb3434d1532d3827d6"
DEFAULT_V2_TRAJECTORY_HASH = "2c6e28ab7906097658176d78ecc509c55abe4814d1b067775810af074b534e82"
DEFAULT_OUTPUT_DIR = Path(
    "reports/assortment_lifecycle/backtest-2026-02-01_2026-07-31/"
    "assortment-lifecycle-v2-historical-backtest"
)
DIFF_COLUMNS = (
    "business_date",
    "nomenclature_code",
    "name",
    "old_status",
    "new_status",
    "changed",
    "demand_state",
    "old_reason_codes",
    "new_reason_codes",
    "demand_reason_codes",
    "sales_30",
    "sales_90",
    "sales_180",
    "available_days_30",
    "available_days_90",
    "available_days_180",
    "first_receipt_at",
    "last_receipt_at",
    "history_age_days",
    "manual_review_required",
    "blockers",
    "exited_growing",
)
ACTIVE_STAGES = {AssortmentStatus.SALE.value, AssortmentStatus.WORKING.value}
DATE_EVIDENCE_FIELDS = (
    "first_supplier_order_at",
    "first_cargo_at",
    "first_receipt_at",
    "last_receipt_at",
    "first_sale_at",
    "last_sale_at",
    "demand_state_since",
)


def finalize_replay_audit(
    *,
    store: AssortmentLifecycleReplayStore,
    legacy_trajectory_hash: str,
    v2_trajectory_hash: str,
    output_dir: Path,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    legacy_rows = store.iter_trajectory_rows(legacy_trajectory_hash)
    v2_rows = store.iter_trajectory_rows(v2_trajectory_hash)
    total_rows = 0
    changed_rows = 0
    transitions: Counter[str] = Counter()
    date_stats: dict[str, Any] = {}
    history_audit = _HistoricalTrajectoryAudit()

    with (
        (output_dir / "v2-lifecycle-history.csv").open(
            "w", encoding="utf-8-sig", newline=""
        ) as lifecycle_handle,
        (output_dir / "stage-diff.csv").open("w", encoding="utf-8-sig", newline="") as diff_handle,
    ):
        lifecycle_writer: csv.DictWriter[str] | None = None
        diff_writer = csv.DictWriter(diff_handle, fieldnames=DIFF_COLUMNS)
        diff_writer.writeheader()
        for legacy, target in zip_longest(legacy_rows, v2_rows):
            if legacy is None or target is None:
                raise ValueError("replay_trajectory_row_count_mismatch")
            legacy_key = _row_key(legacy)
            target_key = _row_key(target)
            if legacy_key != target_key:
                raise ValueError(f"replay_trajectory_key_mismatch:{legacy_key}:{target_key}")
            if lifecycle_writer is None:
                lifecycle_writer = csv.DictWriter(
                    lifecycle_handle, fieldnames=list(target), extrasaction="ignore"
                )
                lifecycle_writer.writeheader()
            lifecycle_writer.writerow({key: _cell(value) for key, value in target.items()})
            row = _diff_row(legacy, target)
            diff_writer.writerow(row)
            history_audit.accept(target)
            total_rows += 1
            changed_rows += int(row["changed"])
            transitions[f"{row['old_status']} -> {row['new_status']}"] += 1
            business_date = str(row["business_date"])
            if date_stats.get("date") != business_date:
                date_stats = {
                    "date": business_date,
                    "sku_count": 0,
                    "changed_sku_count": 0,
                    "exits_from_growing": 0,
                    "demand_states": Counter(),
                    "blocked_sku_count": 0,
                    "manual_review_sku_count": 0,
                }
            date_stats["sku_count"] += 1
            date_stats["changed_sku_count"] += int(row["changed"])
            date_stats["exits_from_growing"] += int(row["exited_growing"])
            date_stats["demand_states"][str(row["demand_state"])] += 1
            date_stats["blocked_sku_count"] += int(bool(row["blockers"]))
            date_stats["manual_review_sku_count"] += int(bool(row["manual_review_required"]))

    summary = {
        "schema": "display_assortment_lifecycle_v2_replay_audit.v1",
        "status": "shadow_replay_complete_economic_train_pending",
        "legacy_trajectory_hash": legacy_trajectory_hash,
        "v2_trajectory_hash": v2_trajectory_hash,
        "daily_row_count": total_rows,
        "changed_daily_row_count": changed_rows,
        "status_transitions": dict(sorted(transitions.items())),
        "historical_validation": history_audit.summary(),
        "latest": {
            **{key: value for key, value in date_stats.items() if key != "demand_states"},
            "demand_states": dict(sorted(date_stats.get("demand_states", {}).items())),
        },
        "train_holdout": {
            "status": "economic_evaluation_pending",
            "holdout_consumed": False,
        },
        "production_authorized": False,
        "production_action": "none_read_only",
        "files": {
            "v2_lifecycle_history": "v2-lifecycle-history.csv",
            "stage_diff": "stage-diff.csv",
        },
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return summary


class _HistoricalTrajectoryAudit:
    """Validate one saved v2 trajectory in a single date-ordered pass."""

    def __init__(self) -> None:
        self.row_count = 0
        self.first_date: date | None = None
        self.last_date: date | None = None
        self.sku_codes: set[str] = set()
        self.previous: dict[str, tuple[date, str, str]] = {}
        self.previous_previous: dict[str, tuple[date, str]] = {}
        self.stage_transitions: Counter[str] = Counter()
        self.transition_skus: set[str] = set()
        self.active_regression_count = 0
        self.active_regression_skus: set[str] = set()
        self.one_day_roundtrips: Counter[str] = Counter()
        self.data_issue_skus: defaultdict[str, set[str]] = defaultdict(set)
        self.chronology_issue_skus: set[str] = set()
        self.chronology_without_blocker_skus: set[str] = set()
        self.stage_fact_conflict_skus: set[str] = set()
        self.future_evidence_skus: set[str] = set()
        self.history_age_mismatch_skus: set[str] = set()
        self.sales_window_issue_skus: set[str] = set()
        self.availability_window_issue_skus: set[str] = set()
        self.non_contiguous_skus: set[str] = set()
        self.previous_status_mismatch_skus: set[str] = set()
        self.growing_rule_violation_skus: set[str] = set()
        self.spike_preservation_checks = 0
        self.spike_preservation_violations = 0
        self.shortage_preservation_checks = 0
        self.shortage_preservation_violations = 0
        self.manual_skus: set[str] = set()
        self.manual_status_by_code: dict[str, str] = {}
        self.manual_overwrite_skus: set[str] = set()
        self.blocked_skus: set[str] = set()
        self.current_snapshot_date: date | None = None
        self.current_stage_counts: Counter[str] = Counter()
        self.current_demand_counts: Counter[str] = Counter()
        self.month_end_snapshots: list[dict[str, Any]] = []

    def accept(self, row: Mapping[str, Any]) -> None:
        business_date = date.fromisoformat(_clean(row.get("business_date")))
        code = _clean(row.get("nomenclature_code"))
        status = _clean(row.get("status"))
        demand_state = _clean(row.get("demand_state"))
        blockers = _text_values(row.get("blockers"))
        self._accept_snapshot(business_date, status, demand_state)
        self.row_count += 1
        self.first_date = min(self.first_date or business_date, business_date)
        self.last_date = max(self.last_date or business_date, business_date)
        self.sku_codes.add(code)
        if blockers:
            self.blocked_skus.add(code)

        previous = self.previous.get(code)
        if previous is not None:
            previous_date, previous_status, _previous_demand = previous
            if (business_date - previous_date).days != 1:
                self.non_contiguous_skus.add(code)
            if _clean(row.get("previous_status")) != previous_status:
                self.previous_status_mismatch_skus.add(code)
            if status != previous_status:
                transition = f"{previous_status} -> {status}"
                self.stage_transitions[transition] += 1
                self.transition_skus.add(code)
                if (
                    previous_status in ACTIVE_STAGES
                    and status == AssortmentStatus.SALES_START.value
                ):
                    self.active_regression_count += 1
                    self.active_regression_skus.add(code)
            previous_previous = self.previous_previous.get(code)
            if (
                previous_previous is not None
                and (business_date - previous_previous[0]).days == 2
                and previous_previous[1] == status
                and previous_status != status
            ):
                self.one_day_roundtrips[f"{status} -> {previous_status} -> {status}"] += 1
            self.previous_previous[code] = (previous_date, previous_status)
            if demand_state == "spike" and previous_status in ACTIVE_STAGES:
                self.spike_preservation_checks += 1
                self.spike_preservation_violations += int(status != previous_status)
            if demand_state == "shortage_limited" and previous_status in ACTIVE_STAGES:
                self.shortage_preservation_checks += 1
                self.shortage_preservation_violations += int(status != previous_status)
        self.previous[code] = (business_date, status, demand_state)

        evidence = {field: _optional_date(row.get(field)) for field in DATE_EVIDENCE_FIELDS}
        self._audit_evidence(
            code=code,
            business_date=business_date,
            status=status,
            demand_state=demand_state,
            row=row,
            evidence=evidence,
            blockers=blockers,
        )
        if bool(row.get("historical_manual_status_replayed")):
            self.manual_skus.add(code)
            prior_manual_status = self.manual_status_by_code.get(code)
            if prior_manual_status is not None and status != prior_manual_status:
                self.manual_overwrite_skus.add(code)
            self.manual_status_by_code[code] = status

    def summary(self) -> dict[str, Any]:
        self._close_snapshot()
        hard_issue_count = (
            len(self.active_regression_skus)
            + len(self.chronology_without_blocker_skus)
            + len(self.stage_fact_conflict_skus)
            + len(self.future_evidence_skus)
            + len(self.history_age_mismatch_skus)
            + len(self.sales_window_issue_skus)
            + len(self.availability_window_issue_skus)
            + len(self.non_contiguous_skus)
            + len(self.previous_status_mismatch_skus)
            + len(self.growing_rule_violation_skus)
            + self.spike_preservation_violations
            + self.shortage_preservation_violations
            + len(self.manual_overwrite_skus)
        )
        return {
            "status": "needs_revision" if hard_issue_count else "passed",
            "period_from": self.first_date.isoformat() if self.first_date else None,
            "period_to": self.last_date.isoformat() if self.last_date else None,
            "daily_row_count": self.row_count,
            "sku_count": len(self.sku_codes),
            "transition_count": sum(self.stage_transitions.values()),
            "transitioning_sku_count": len(self.transition_skus),
            "stage_transitions": dict(sorted(self.stage_transitions.items())),
            "active_to_sales_start": {
                "transition_count": self.active_regression_count,
                "sku_count": len(self.active_regression_skus),
            },
            "one_day_roundtrips": dict(sorted(self.one_day_roundtrips.items())),
            "data_quality": {
                "affected_sku_count": len(set().union(*self.data_issue_skus.values())),
                "by_issue_sku_count": {
                    key: len(values) for key, values in sorted(self.data_issue_skus.items())
                },
                "chronology_issue_sku_count": len(self.chronology_issue_skus),
                "chronology_without_blocker_sku_count": len(self.chronology_without_blocker_skus),
                "stage_fact_conflict_sku_count": len(self.stage_fact_conflict_skus),
            },
            "invariants": {
                "future_evidence_sku_count": len(self.future_evidence_skus),
                "history_age_mismatch_sku_count": len(self.history_age_mismatch_skus),
                "sales_window_issue_sku_count": len(self.sales_window_issue_skus),
                "availability_window_issue_sku_count": len(self.availability_window_issue_skus),
                "non_contiguous_sku_count": len(self.non_contiguous_skus),
                "previous_status_mismatch_sku_count": len(self.previous_status_mismatch_skus),
                "growing_rule_violation_sku_count": len(self.growing_rule_violation_skus),
                "spike_preservation_checks": self.spike_preservation_checks,
                "spike_preservation_violations": self.spike_preservation_violations,
                "shortage_preservation_checks": self.shortage_preservation_checks,
                "shortage_preservation_violations": self.shortage_preservation_violations,
                "manual_sku_count": len(self.manual_skus),
                "manual_overwrite_sku_count": len(self.manual_overwrite_skus),
                "blocked_sku_count": len(self.blocked_skus),
            },
            "month_end_snapshots": self.month_end_snapshots,
        }

    def _audit_evidence(
        self,
        *,
        code: str,
        business_date: date,
        status: str,
        demand_state: str,
        row: Mapping[str, Any],
        evidence: Mapping[str, date | None],
        blockers: list[str],
    ) -> None:
        order_at = evidence["first_supplier_order_at"]
        cargo_at = evidence["first_cargo_at"]
        receipt_at = evidence["first_receipt_at"]
        last_receipt_at = evidence["last_receipt_at"]
        sale_at = evidence["first_sale_at"]
        chronology_issue = False
        for field, event_date in evidence.items():
            if event_date is not None and event_date > business_date:
                self.future_evidence_skus.add(code)
                self.data_issue_skus[f"future_{field}"].add(code)
        if cargo_at is not None and cargo_at < date(2000, 1, 1):
            self.data_issue_skus["cargo_before_2000"].add(code)
        for issue, failed in (
            ("receipt_before_first_order", bool(order_at and receipt_at and receipt_at < order_at)),
            ("sale_before_first_receipt", bool(receipt_at and sale_at and sale_at < receipt_at)),
            ("receipt_without_order", bool(receipt_at and not order_at)),
            ("sale_without_order", bool(sale_at and not order_at)),
            ("sale_without_receipt", bool(sale_at and not receipt_at)),
            (
                "last_receipt_before_first_receipt",
                bool(receipt_at and last_receipt_at and last_receipt_at < receipt_at),
            ),
        ):
            if failed:
                chronology_issue = True
                self.data_issue_skus[issue].add(code)
        if chronology_issue:
            self.chronology_issue_skus.add(code)
            if not blockers:
                self.chronology_without_blocker_skus.add(code)

        manual = bool(row.get("historical_manual_status_replayed"))
        if not manual:
            if status == AssortmentStatus.FRUIT.value and (receipt_at or sale_at):
                self.stage_fact_conflict_skus.add(code)
            elif status == AssortmentStatus.NEWBORN.value and (
                not order_at or receipt_at or sale_at
            ):
                self.stage_fact_conflict_skus.add(code)
            elif status == AssortmentStatus.NEW_ITEM.value and (not receipt_at or sale_at):
                self.stage_fact_conflict_skus.add(code)
            elif status in {
                AssortmentStatus.SALES_START.value,
                AssortmentStatus.SALE.value,
                AssortmentStatus.WORKING.value,
            } and (not receipt_at or not sale_at):
                self.stage_fact_conflict_skus.add(code)

        if receipt_at is not None and row.get("history_age_days") is not None:
            try:
                age_days = int(row["history_age_days"])
            except (TypeError, ValueError):
                self.history_age_mismatch_skus.add(code)
            else:
                if age_days != (business_date - receipt_at).days:
                    self.history_age_mismatch_skus.add(code)
        sales_windows = tuple(
            _optional_float(row.get(field)) for field in ("sales_30", "sales_90", "sales_180")
        )
        if (
            any(value is None or value < 0 for value in sales_windows)
            or not sales_windows[0] <= sales_windows[1] <= sales_windows[2]
        ):
            self.sales_window_issue_skus.add(code)
        availability_windows = tuple(
            _optional_float(row.get(field))
            for field in ("available_days_30", "available_days_90", "available_days_180")
        )
        if any(value is None for value in availability_windows) or not (
            0 <= availability_windows[0] <= 30
            and availability_windows[0] <= availability_windows[1] <= 90
            and availability_windows[1] <= availability_windows[2] <= 180
        ):
            self.availability_window_issue_skus.add(code)
        if demand_state == "growing":
            since = evidence["demand_state_since"]
            independent_fields = (
                "sales_active_days_30",
                "sales_document_count_30",
                "sales_customer_count_30",
                "sales_point_count_30",
            )
            if (
                since is None
                or (business_date - since).days < 14
                or _float(row.get("sales_180")) < 12
                or any(_float(row.get(field)) < 2 for field in independent_fields)
                or _float(row.get("sales_max_day_share_30"), default=1) > 0.7
            ):
                self.growing_rule_violation_skus.add(code)

    def _accept_snapshot(self, business_date: date, status: str, demand_state: str) -> None:
        if self.current_snapshot_date is not None and business_date != self.current_snapshot_date:
            if business_date.month != self.current_snapshot_date.month:
                self._append_month_end_snapshot()
            self.current_stage_counts = Counter()
            self.current_demand_counts = Counter()
        self.current_snapshot_date = business_date
        self.current_stage_counts[status] += 1
        self.current_demand_counts[demand_state] += 1

    def _append_month_end_snapshot(self) -> None:
        if self.current_snapshot_date is None:
            return
        self.month_end_snapshots.append(
            {
                "date": self.current_snapshot_date.isoformat(),
                "sku_count": sum(self.current_stage_counts.values()),
                "stages": dict(sorted(self.current_stage_counts.items())),
                "demand_states": dict(sorted(self.current_demand_counts.items())),
            }
        )

    def _close_snapshot(self) -> None:
        if self.current_snapshot_date is None:
            return
        if (
            not self.month_end_snapshots
            or self.month_end_snapshots[-1]["date"] != self.current_snapshot_date.isoformat()
        ):
            self._append_month_end_snapshot()


def _optional_date(value: Any) -> date | None:
    rendered = _clean(value)
    if not rendered:
        return None
    try:
        return date.fromisoformat(rendered[:10])
    except ValueError:
        return None


def _float(value: Any, *, default: float = 0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _optional_float(value: Any) -> float | None:
    if value is None or isinstance(value, str) and not value.strip():
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _diff_row(legacy: Mapping[str, Any], target: Mapping[str, Any]) -> dict[str, Any]:
    old_status = _clean(legacy.get("status"))
    new_status = _clean(target.get("status"))
    return {
        "business_date": target["business_date"],
        "nomenclature_code": target["nomenclature_code"],
        "name": _clean(target.get("name") or legacy.get("name")),
        "old_status": old_status,
        "new_status": new_status,
        "changed": int(old_status != new_status),
        "demand_state": _clean(target.get("demand_state")),
        "old_reason_codes": ",".join(_text_values(legacy.get("reason_codes"))),
        "new_reason_codes": ",".join(_text_values(target.get("reason_codes"))),
        "demand_reason_codes": ",".join(_text_values(target.get("demand_reason_codes"))),
        "sales_30": target.get("sales_30"),
        "sales_90": target.get("sales_90"),
        "sales_180": target.get("sales_180"),
        "available_days_30": target.get("available_days_30"),
        "available_days_90": target.get("available_days_90"),
        "available_days_180": target.get("available_days_180"),
        "first_receipt_at": target.get("first_receipt_at"),
        "last_receipt_at": target.get("last_receipt_at"),
        "history_age_days": target.get("history_age_days"),
        "manual_review_required": int(bool(target.get("manual_review_required"))),
        "blockers": ",".join(_text_values(target.get("blockers"))),
        "exited_growing": int(
            old_status == AssortmentStatus.SALE.value and new_status != AssortmentStatus.SALE.value
        ),
    }


def _row_key(row: Mapping[str, Any]) -> tuple[str, str]:
    return _clean(row.get("business_date")), _clean(row.get("nomenclature_code"))


def _cell(value: Any) -> Any:
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if value is None:
        return ""
    return value


def _text_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item for item in (part.strip() for part in value.split(",")) if item]
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value if _clean(item)]
    return []


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store-path", type=Path, default=DEFAULT_REPLAY_STORE_PATH)
    parser.add_argument("--legacy-trajectory-hash", default=DEFAULT_LEGACY_TRAJECTORY_HASH)
    parser.add_argument("--v2-trajectory-hash", default=DEFAULT_V2_TRAJECTORY_HASH)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    summary = finalize_replay_audit(
        store=AssortmentLifecycleReplayStore(args.store_path),
        legacy_trajectory_hash=args.legacy_trajectory_hash,
        v2_trajectory_hash=args.v2_trajectory_hash,
        output_dir=args.output_dir,
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
