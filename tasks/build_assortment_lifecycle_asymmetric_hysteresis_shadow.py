"""Build a read-only asymmetric entry/exit hysteresis shadow trajectory.

The task streams an immutable lifecycle-v2 base trajectory by bounded SKU
partitions. It delays ``working -> sale`` until the base requests ``sale`` for
E consecutive calendar days and delays ``sale -> working`` until the base
requests ``working`` for D consecutive calendar days. Source facts are never
rebuilt and production/external writes are never performed.
"""

from __future__ import annotations

import argparse
import inspect
import json
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping

from app.services.assortment_lifecycle_replay_store import (
    DEFAULT_REPLAY_STORE_PATH,
    AssortmentLifecycleReplayStore,
    StoredTrajectory,
    stable_hash,
)
from tasks.build_assortment_lifecycle_exit_hysteresis_shadow import (
    DEFAULT_BATCH_SIZE,
    _clean,
    _stored_trajectory,
    _text_list,
    compare_trajectories,
)

MODEL_VERSION = "assortment-lifecycle-v2-asymmetric-hysteresis.v1"
DEFAULT_BASE_TRAJECTORY_HASH = "1e744d385bdf04fa2066bcc7ed6590e2b4e2a98ea7c1743266a31ea03e7a7a49"
DEFAULT_COMPARISON_TRAJECTORY_HASH = (
    "8beccc1d06b43e87fadd2bcba9662794ea3fee2650ec33faa000e631b6f91cea"
)
DEFAULT_ENTRY_CONFIRMATION_DAYS = 2
DEFAULT_EXIT_CONFIRMATION_DAYS = 3
DEFAULT_OUTPUT_DIR = Path(
    "reports/assortment_lifecycle/backtest-2026-01-01_2026-07-31/"
    "entry-confirmation-e2-exit-d3-x1.2-2026-08-15-v1"
)
ACTIVE_STATUSES = {"sale", "working"}
STATUS_LABELS = {"sale": "Растим", "working": "Поддерживаем"}


def apply_asymmetric_hysteresis(
    rows: Iterable[Mapping[str, Any]],
    *,
    entry_confirmation_days: int,
    exit_confirmation_days: int,
) -> list[dict[str, Any]]:
    """Apply independent consecutive-day confirmation to both active transitions."""

    if entry_confirmation_days < 1:
        raise ValueError("entry_hysteresis_confirmation_days_positive_required")
    if exit_confirmation_days < 1:
        raise ValueError("exit_hysteresis_confirmation_days_positive_required")
    result: list[dict[str, Any]] = []
    previous_code = ""
    previous_adjusted_status = ""
    previous_date: date | None = None
    entry_streak = 0
    exit_streak = 0

    for source in rows:
        row = dict(source)
        code = _clean(row.get("nomenclature_code"))
        business_date = date.fromisoformat(_clean(row.get("business_date")))
        if not code:
            raise ValueError("asymmetric_hysteresis_sku_required")
        if code != previous_code:
            previous_code = code
            previous_adjusted_status = _clean(row.get("previous_status"))
            previous_date = None
            entry_streak = 0
            exit_streak = 0
        if previous_date is not None and (business_date - previous_date).days != 1:
            raise ValueError(f"asymmetric_hysteresis_non_contiguous_sku:{code}:{business_date}")

        base_status = _clean(row.get("status"))
        adjusted_status = base_status
        entry_pending = False
        entry_confirmed = False
        exit_pending = False
        exit_confirmed = False
        if (
            row.get("historical_manual_status_replayed") is True
            or base_status not in ACTIVE_STATUSES
        ):
            entry_streak = 0
            exit_streak = 0
        elif previous_adjusted_status == "working" and base_status == "sale":
            entry_streak += 1
            exit_streak = 0
            if entry_streak < entry_confirmation_days:
                adjusted_status = "working"
                entry_pending = True
            else:
                adjusted_status = "sale"
                entry_confirmed = True
        elif previous_adjusted_status == "sale" and base_status == "working":
            exit_streak += 1
            entry_streak = 0
            if exit_streak < exit_confirmation_days:
                adjusted_status = "sale"
                exit_pending = True
            else:
                adjusted_status = "working"
                exit_confirmed = True
        else:
            entry_streak = 0
            exit_streak = 0

        reason_codes = _text_list(row.get("reason_codes"))
        reason_text = _clean(row.get("reason_text"))
        if entry_pending:
            reason_codes.append("sale_entry_hysteresis_pending")
            reason_text = (
                f"{reason_text} Вход в «Растим» пока не подтверждён: "
                f"{entry_streak}/{entry_confirmation_days} последовательных дней."
            ).strip()
        elif entry_confirmed:
            reason_codes.append("sale_entry_hysteresis_confirmed")
            reason_text = (
                f"{reason_text} Вход в «Растим» подтверждён за "
                f"{entry_confirmation_days} последовательных дней."
            ).strip()
        if exit_pending:
            reason_codes.append("sale_exit_hysteresis_pending")
            reason_text = (
                f"{reason_text} Выход из «Растим» пока не подтверждён: "
                f"{exit_streak}/{exit_confirmation_days} последовательных дней."
            ).strip()
        elif exit_confirmed:
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
                "status_label": STATUS_LABELS.get(adjusted_status, _clean(row.get("status_label"))),
                "reason_codes": reason_codes,
                "reason_text": reason_text,
                "entry_hysteresis_confirmation_days": entry_confirmation_days,
                "exit_hysteresis_confirmation_days": exit_confirmation_days,
                "entry_growth_streak": entry_streak,
                "exit_non_growth_streak": exit_streak,
                "entry_hysteresis_pending": entry_pending,
                "entry_hysteresis_confirmed": entry_confirmed,
                "exit_hysteresis_pending": exit_pending,
                "exit_hysteresis_confirmed": exit_confirmed,
            }
        )
        result.append(row)
        previous_adjusted_status = adjusted_status
        previous_date = business_date
    return result


def asymmetric_hysteresis_policy_hash(
    *,
    base_trajectory_hash: str,
    entry_confirmation_days: int,
    exit_confirmation_days: int,
) -> str:
    return stable_hash(
        {
            "model_version": MODEL_VERSION,
            "base_trajectory_hash": base_trajectory_hash,
            "entry_rule": "consecutive_base_sale_days",
            "entry_confirmation_days": entry_confirmation_days,
            "exit_rule": "consecutive_base_working_days",
            "exit_confirmation_days": exit_confirmation_days,
            "transform_source": inspect.getsource(apply_asymmetric_hysteresis),
        }
    )


def build_asymmetric_hysteresis_trajectory(
    *,
    store: AssortmentLifecycleReplayStore,
    base: StoredTrajectory,
    entry_confirmation_days: int,
    exit_confirmation_days: int,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> tuple[StoredTrajectory, dict[str, Any]]:
    if batch_size < 1 or batch_size > 500:
        raise ValueError("asymmetric_hysteresis_batch_size_invalid")
    policy_hash = asymmetric_hysteresis_policy_hash(
        base_trajectory_hash=base.trajectory_hash,
        entry_confirmation_days=entry_confirmation_days,
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

    codes = store.dataset_codes(base.dataset_hash)
    build = store.begin_trajectory_build(
        dataset_hash=base.dataset_hash,
        model_version=MODEL_VERSION,
        policy_hash=policy_hash,
        period_from=base.period_from,
        period_to=base.period_to,
        metadata={
            "scope": "Дисплеи",
            "base_trajectory_hash": base.trajectory_hash,
            "entry_rule": "working_to_sale_after_consecutive_base_sale_days",
            "entry_confirmation_days": entry_confirmation_days,
            "exit_rule": "sale_to_working_after_consecutive_base_working_days",
            "exit_confirmation_days": exit_confirmation_days,
            "look_ahead_free": True,
            "memory_safe": True,
            "partition": "nomenclature_code",
            "production_authorized": False,
            "production_action": "none_read_only",
        },
    )
    start = build.completed_sku_count
    if start and build.last_completed_sku != codes[start - 1]:
        raise ValueError("asymmetric_hysteresis_checkpoint_dataset_mismatch")
    resumed = start > 0
    for offset in range(start, len(codes), batch_size):
        batch = codes[offset : offset + batch_size]
        rows = apply_asymmetric_hysteresis(
            store.iter_trajectory_rows_for_codes(base.trajectory_hash, batch),
            entry_confirmation_days=entry_confirmation_days,
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
        raise ValueError("asymmetric_hysteresis_trajectory_finalize_readback_failed")
    return ready, {
        "trajectory_reused": False,
        "resumed": resumed,
        "completed_sku_count": len(codes),
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store-path", type=Path, default=DEFAULT_REPLAY_STORE_PATH)
    parser.add_argument("--base-trajectory-hash", default=DEFAULT_BASE_TRAJECTORY_HASH)
    parser.add_argument("--comparison-trajectory-hash", default=DEFAULT_COMPARISON_TRAJECTORY_HASH)
    parser.add_argument(
        "--entry-confirmation-days", type=int, default=DEFAULT_ENTRY_CONFIRMATION_DAYS
    )
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
    comparison = _stored_trajectory(store, args.comparison_trajectory_hash)
    if comparison.dataset_hash != base.dataset_hash:
        raise ValueError("asymmetric_hysteresis_comparison_dataset_mismatch")
    ready, build = build_asymmetric_hysteresis_trajectory(
        store=store,
        base=base,
        entry_confirmation_days=args.entry_confirmation_days,
        exit_confirmation_days=args.exit_confirmation_days,
        batch_size=args.batch_size,
    )
    stability = compare_trajectories(
        store=store,
        base_trajectory_hash=comparison.trajectory_hash,
        hysteresis_trajectory_hash=ready.trajectory_hash,
        output_dir=args.output_dir,
    )
    payload = {
        "schema": "assortment_lifecycle_asymmetric_hysteresis_shadow_run.v1",
        "status": "complete",
        "dataset_hash": base.dataset_hash,
        "base_trajectory_hash": base.trajectory_hash,
        "comparison_trajectory_hash": comparison.trajectory_hash,
        "candidate_trajectory_hash": ready.trajectory_hash,
        "candidate_content_sha256": ready.content_sha256,
        "policy_hash": ready.policy_hash,
        "model_version": ready.model_version,
        "period_from": ready.period_from.isoformat(),
        "period_to": ready.period_to.isoformat(),
        "row_count": ready.row_count,
        "entry_confirmation_days": args.entry_confirmation_days,
        "exit_confirmation_days": args.exit_confirmation_days,
        "build": build,
        "comparison_status": stability["status"],
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
