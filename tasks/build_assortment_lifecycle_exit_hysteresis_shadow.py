"""Build a read-only exit-hysteresis shadow from an immutable v2 trajectory.

The task never rebuilds source facts. It streams an existing look-ahead-free
trajectory by bounded SKU partitions and delays only ``sale -> working`` until
the base trajectory has requested that exit for N consecutive calendar days.
The derived trajectory is stored separately in the append-only replay store.
"""

from __future__ import annotations

import argparse
import csv
import inspect
import json
from collections import Counter, defaultdict
from datetime import date
from itertools import zip_longest
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from app.services.assortment_lifecycle_replay_store import (
    DEFAULT_REPLAY_STORE_PATH,
    AssortmentLifecycleReplayStore,
    StoredTrajectory,
    stable_hash,
)

MODEL_VERSION = "assortment-lifecycle-v2-exit-hysteresis.v1"
DEFAULT_BASE_TRAJECTORY_HASH = "1e744d385bdf04fa2066bcc7ed6590e2b4e2a98ea7c1743266a31ea03e7a7a49"
DEFAULT_EXIT_CONFIRMATION_DAYS = 7
DEFAULT_BATCH_SIZE = 80
DEFAULT_OUTPUT_DIR = Path(
    "reports/assortment_lifecycle/backtest-2026-01-01_2026-07-31/"
    "assortment-lifecycle-v2-exit-hysteresis-d7-2026-08-15-v1"
)
ACTIVE_STATUSES = {"sale", "working"}
ROUNDTRIP_WINDOWS = (1, 3, 7, 14)
CHANGED_COLUMNS = (
    "business_date",
    "nomenclature_code",
    "name",
    "base_status",
    "hysteresis_status",
    "demand_state",
    "exit_non_growth_streak",
    "reason_codes",
)


def apply_exit_hysteresis(
    rows: Iterable[Mapping[str, Any]],
    *,
    exit_confirmation_days: int,
) -> list[dict[str, Any]]:
    """Delay a base ``sale -> working`` until N consecutive requested days."""

    if exit_confirmation_days < 1:
        raise ValueError("exit_hysteresis_confirmation_days_positive_required")
    result: list[dict[str, Any]] = []
    previous_code = ""
    previous_adjusted_status = ""
    previous_date: date | None = None
    non_growth_streak = 0

    for source in rows:
        row = dict(source)
        code = _clean(row.get("nomenclature_code"))
        business_date = date.fromisoformat(_clean(row.get("business_date")))
        if not code:
            raise ValueError("exit_hysteresis_sku_required")
        if code != previous_code:
            previous_code = code
            previous_adjusted_status = _clean(row.get("previous_status"))
            previous_date = None
            non_growth_streak = 0
        if previous_date is not None and (business_date - previous_date).days != 1:
            raise ValueError(f"exit_hysteresis_non_contiguous_sku:{code}:{business_date}")

        base_status = _clean(row.get("status"))
        adjusted_status = base_status
        pending = False
        confirmed = False
        if (
            row.get("historical_manual_status_replayed") is True
            or base_status not in ACTIVE_STATUSES
        ):
            non_growth_streak = 0
        elif previous_adjusted_status == "sale" and base_status == "working":
            non_growth_streak += 1
            if non_growth_streak < exit_confirmation_days:
                adjusted_status = "sale"
                pending = True
            else:
                adjusted_status = "working"
                confirmed = True
        else:
            non_growth_streak = 0

        reason_codes = _text_list(row.get("reason_codes"))
        reason_text = _clean(row.get("reason_text"))
        if pending:
            reason_codes.append("sale_exit_hysteresis_pending")
            reason_text = (
                f"{reason_text} Выход из «Растим» пока не подтверждён: "
                f"{non_growth_streak}/{exit_confirmation_days} последовательных дней."
            ).strip()
        elif confirmed:
            reason_codes.append("sale_exit_hysteresis_confirmed")
            reason_text = (
                f"{reason_text} Выход из «Растим» подтверждён за "
                f"{exit_confirmation_days} последовательных дней."
            ).strip()

        row.update(
            {
                "previous_status": previous_adjusted_status,
                "base_status": base_status,
                "status": adjusted_status,
                "status_label": (
                    "Растим" if adjusted_status == "sale" else row.get("status_label")
                ),
                "reason_codes": reason_codes,
                "reason_text": reason_text,
                "exit_hysteresis_confirmation_days": exit_confirmation_days,
                "exit_non_growth_streak": non_growth_streak,
                "exit_hysteresis_pending": pending,
                "exit_hysteresis_confirmed": confirmed,
                "hysteresis_target_status": "working" if pending else None,
            }
        )
        result.append(row)
        previous_adjusted_status = adjusted_status
        previous_date = business_date
    return result


def exit_hysteresis_policy_hash(
    *,
    base_trajectory_hash: str,
    exit_confirmation_days: int,
) -> str:
    return stable_hash(
        {
            "model_version": MODEL_VERSION,
            "base_trajectory_hash": base_trajectory_hash,
            "entry_policy": "inherited_immutable_base_trajectory",
            "exit_rule": "consecutive_base_working_days",
            "exit_confirmation_days": exit_confirmation_days,
            "transform_source": inspect.getsource(apply_exit_hysteresis),
        }
    )


def build_exit_hysteresis_trajectory(
    *,
    store: AssortmentLifecycleReplayStore,
    base: StoredTrajectory,
    exit_confirmation_days: int,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> tuple[StoredTrajectory, dict[str, Any]]:
    if batch_size < 1 or batch_size > 500:
        raise ValueError("exit_hysteresis_batch_size_invalid")
    policy_hash = exit_hysteresis_policy_hash(
        base_trajectory_hash=base.trajectory_hash,
        exit_confirmation_days=exit_confirmation_days,
    )
    cached = store.find_trajectory(
        dataset_hash=base.dataset_hash,
        model_version=MODEL_VERSION,
        policy_hash=policy_hash,
        period_from=base.period_from,
        period_to=base.period_to,
    )
    if cached is not None:
        return cached, {
            "trajectory_reused": True,
            "resumed": False,
            "completed_sku_count": len(store.dataset_codes(base.dataset_hash)),
        }

    metadata = {
        "scope": "Дисплеи",
        "base_trajectory_hash": base.trajectory_hash,
        "entry_policy": "inherited_immutable_base_trajectory",
        "exit_rule": "sale_to_working_after_consecutive_base_working_days",
        "exit_confirmation_days": exit_confirmation_days,
        "look_ahead_free": True,
        "memory_safe": True,
        "partition": "nomenclature_code",
        "production_authorized": False,
        "production_action": "none_read_only",
    }
    codes = store.dataset_codes(base.dataset_hash)
    build = store.begin_trajectory_build(
        dataset_hash=base.dataset_hash,
        model_version=MODEL_VERSION,
        policy_hash=policy_hash,
        period_from=base.period_from,
        period_to=base.period_to,
        metadata=metadata,
    )
    start = build.completed_sku_count
    if start and build.last_completed_sku != codes[start - 1]:
        raise ValueError("exit_hysteresis_checkpoint_dataset_mismatch")
    resumed = start > 0
    for offset in range(start, len(codes), batch_size):
        batch = codes[offset : offset + batch_size]
        source_rows = store.iter_trajectory_rows_for_codes(base.trajectory_hash, batch)
        rows = apply_exit_hysteresis(
            source_rows,
            exit_confirmation_days=exit_confirmation_days,
        )
        build = store.append_trajectory_partition(
            trajectory_hash=build.trajectory_hash,
            checkpoint_sku=batch[-1],
            completed_sku_count=offset + len(batch),
            rows=rows,
        )
        print(
            json.dumps(
                {
                    "completed_sku_count": build.completed_sku_count,
                    "row_count": build.row_count,
                    "trajectory_hash": build.trajectory_hash,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            flush=True,
        )
    store.finalize_trajectory_build(build.trajectory_hash, expected_sku_count=len(codes))
    ready = store.find_trajectory(
        dataset_hash=base.dataset_hash,
        model_version=MODEL_VERSION,
        policy_hash=policy_hash,
        period_from=base.period_from,
        period_to=base.period_to,
    )
    if ready is None:
        raise ValueError("exit_hysteresis_trajectory_finalize_readback_failed")
    return ready, {
        "trajectory_reused": False,
        "resumed": resumed,
        "completed_sku_count": len(codes),
    }


def compare_trajectories(
    *,
    store: AssortmentLifecycleReplayStore,
    base_trajectory_hash: str,
    hysteresis_trajectory_hash: str,
    output_dir: Path,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    policy_state = {
        "base": _PolicyState(),
        "hysteresis": _PolicyState(),
    }
    changed_rows = 0
    changed_skus: set[str] = set()
    latest_date = ""
    latest_matrix: Counter[tuple[str, str]] = Counter()
    requested_sku_rows: list[dict[str, Any]] = []
    with (output_dir / "changed-rows.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CHANGED_COLUMNS)
        writer.writeheader()
        for base, target in zip_longest(
            store.iter_trajectory_rows(base_trajectory_hash),
            store.iter_trajectory_rows(hysteresis_trajectory_hash),
        ):
            if base is None or target is None:
                raise ValueError("exit_hysteresis_trajectory_row_count_mismatch")
            key = (_clean(base.get("business_date")), _clean(base.get("nomenclature_code")))
            target_key = (
                _clean(target.get("business_date")),
                _clean(target.get("nomenclature_code")),
            )
            if key != target_key:
                raise ValueError(f"exit_hysteresis_trajectory_key_mismatch:{key}:{target_key}")
            policy_state["base"].accept(base)
            policy_state["hysteresis"].accept(target)
            business_date, code = key
            if business_date != latest_date:
                latest_date = business_date
                latest_matrix = Counter()
            latest_matrix[(_clean(base.get("status")), _clean(target.get("status")))] += 1
            if _clean(base.get("status")) != _clean(target.get("status")):
                changed_rows += 1
                changed_skus.add(code)
                writer.writerow(
                    {
                        "business_date": business_date,
                        "nomenclature_code": code,
                        "name": _clean(target.get("name")),
                        "base_status": _clean(base.get("status")),
                        "hysteresis_status": _clean(target.get("status")),
                        "demand_state": _clean(target.get("demand_state")),
                        "exit_non_growth_streak": target.get("exit_non_growth_streak"),
                        "reason_codes": json.dumps(
                            _text_list(target.get("reason_codes")), ensure_ascii=False
                        ),
                    }
                )
            if code == "РБ000069123" and (
                _clean(base.get("status")) != _clean(target.get("status"))
                or bool(target.get("exit_hysteresis_confirmed"))
            ):
                requested_sku_rows.append(
                    {
                        "business_date": business_date,
                        "base_status": _clean(base.get("status")),
                        "hysteresis_status": _clean(target.get("status")),
                        "demand_state": _clean(target.get("demand_state")),
                        "exit_non_growth_streak": target.get("exit_non_growth_streak"),
                    }
                )

    payload = {
        "schema": "assortment_lifecycle_exit_hysteresis_comparison.v1",
        "status": "shadow_trajectory_complete_economic_train_pending",
        "base_trajectory_hash": base_trajectory_hash,
        "hysteresis_trajectory_hash": hysteresis_trajectory_hash,
        "changed_daily_row_count": changed_rows,
        "changed_sku_count": len(changed_skus),
        "latest_date": latest_date,
        "latest_status_matrix": [
            {
                "base_status": base_status,
                "hysteresis_status": target_status,
                "sku_count": count,
            }
            for (base_status, target_status), count in sorted(latest_matrix.items())
        ],
        "policies": {name: state.summary() for name, state in policy_state.items()},
        "requested_sku": {
            "nomenclature_code": "РБ000069123",
            "changed_or_confirmed_rows": requested_sku_rows,
        },
        "production_authorized": False,
        "production_action": "none_read_only",
    }
    (output_dir / "stability-comparison.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return payload


class _PolicyState:
    def __init__(self) -> None:
        self.previous: dict[str, tuple[date, str]] = {}
        self.transitions: Counter[tuple[str, str]] = Counter()
        self.transition_skus: defaultdict[tuple[str, str], set[str]] = defaultdict(set)
        self.events: defaultdict[str, list[tuple[date, str, str]]] = defaultdict(list)

    def accept(self, row: Mapping[str, Any]) -> None:
        business_date = date.fromisoformat(_clean(row.get("business_date")))
        code = _clean(row.get("nomenclature_code"))
        status = _clean(row.get("status"))
        previous = self.previous.get(code)
        if previous is not None and status != previous[1]:
            pair = (previous[1], status)
            self.transitions[pair] += 1
            self.transition_skus[pair].add(code)
            if set(pair) == ACTIVE_STATUSES:
                self.events[code].append((business_date, *pair))
        self.previous[code] = (business_date, status)

    def summary(self) -> dict[str, Any]:
        reverse = {
            "working_to_sale": Counter(),
            "sale_to_working": Counter(),
        }
        for events in self.events.values():
            for index, (event_date, from_status, to_status) in enumerate(events):
                direction = "working_to_sale" if from_status == "working" else "sale_to_working"
                reverse_days = None
                for later_date, later_from, later_to in events[index + 1 :]:
                    if later_from == to_status and later_to == from_status:
                        reverse_days = (later_date - event_date).days
                        break
                for window in ROUNDTRIP_WINDOWS:
                    if reverse_days is not None and reverse_days <= window:
                        reverse[direction][window] += 1
        return {
            "active_transitions": {
                f"{from_status}_to_{to_status}": {
                    "event_count": self.transitions[(from_status, to_status)],
                    "sku_count": len(self.transition_skus[(from_status, to_status)]),
                }
                for from_status, to_status in (("working", "sale"), ("sale", "working"))
            },
            "short_reverse_proxies": {
                direction: {str(window): counts[window] for window in ROUNDTRIP_WINDOWS}
                for direction, counts in reverse.items()
            },
        }


def _stored_trajectory(
    store: AssortmentLifecycleReplayStore, trajectory_hash: str
) -> StoredTrajectory:
    for row in store.manifest().get("trajectories", []):
        if row.get("trajectory_hash") == trajectory_hash:
            return StoredTrajectory(
                trajectory_hash=str(row["trajectory_hash"]),
                dataset_hash=str(row["dataset_hash"]),
                model_version=str(row["model_version"]),
                policy_hash=str(row["policy_hash"]),
                period_from=date.fromisoformat(str(row["period_from"])),
                period_to=date.fromisoformat(str(row["period_to"])),
                content_sha256=str(row["content_sha256"]),
                row_count=int(row["row_count"]),
                metadata=dict(row.get("metadata") or {}),
            )
    raise ValueError(f"replay_trajectory_not_found:{trajectory_hash}")


def _text_list(value: Any) -> list[str]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [text for item in value if (text := _clean(item))]
    return []


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store-path", type=Path, default=DEFAULT_REPLAY_STORE_PATH)
    parser.add_argument("--base-trajectory-hash", default=DEFAULT_BASE_TRAJECTORY_HASH)
    parser.add_argument(
        "--exit-confirmation-days", type=int, default=DEFAULT_EXIT_CONFIRMATION_DAYS
    )
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    store = AssortmentLifecycleReplayStore(args.store_path)
    base = _stored_trajectory(store, args.base_trajectory_hash)
    ready, build = build_exit_hysteresis_trajectory(
        store=store,
        base=base,
        exit_confirmation_days=args.exit_confirmation_days,
        batch_size=args.batch_size,
    )
    comparison = compare_trajectories(
        store=store,
        base_trajectory_hash=base.trajectory_hash,
        hysteresis_trajectory_hash=ready.trajectory_hash,
        output_dir=args.output_dir,
    )
    payload = {
        "schema": "assortment_lifecycle_exit_hysteresis_shadow_run.v1",
        "status": "complete",
        "dataset_hash": base.dataset_hash,
        "base_trajectory_hash": base.trajectory_hash,
        "hysteresis_trajectory_hash": ready.trajectory_hash,
        "hysteresis_content_sha256": ready.content_sha256,
        "policy_hash": ready.policy_hash,
        "model_version": ready.model_version,
        "period_from": ready.period_from.isoformat(),
        "period_to": ready.period_to.isoformat(),
        "row_count": ready.row_count,
        "exit_confirmation_days": args.exit_confirmation_days,
        "build": build,
        "comparison_status": comparison["status"],
        "production_authorized": False,
        "production_action": "none_read_only",
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "run-manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
