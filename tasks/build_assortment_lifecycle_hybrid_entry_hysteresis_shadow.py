"""Build a read-only hybrid entry / d3 exit lifecycle trajectory.

The transform reuses two immutable, look-ahead-free lifecycle trajectories on
the same frozen dataset.  A working SKU enters ``sale`` immediately when the
x1.5 trajectory also requests ``sale``.  A boundary x1.2-only request must be
present for two consecutive calendar days.  Exits use three consecutive x1.2
``working`` requests.  No source facts are replayed and no external writes are
performed.
"""

from __future__ import annotations

import argparse
import inspect
import json
from datetime import date
from itertools import zip_longest
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

MODEL_VERSION = "assortment-lifecycle-v2-hybrid-entry-hysteresis.v1"
DEFAULT_X1_2_BASE_TRAJECTORY_HASH = (
    "1e744d385bdf04fa2066bcc7ed6590e2b4e2a98ea7c1743266a31ea03e7a7a49"
)
DEFAULT_X1_5_STRONG_TRAJECTORY_HASH = (
    "f981f8ec5f72fd42f475b05ab025c66f7979909bedce53c78f64083969a08c4c"
)
DEFAULT_COMPARISON_TRAJECTORY_HASH = (
    "8beccc1d06b43e87fadd2bcba9662794ea3fee2650ec33faa000e631b6f91cea"
)
DEFAULT_BOUNDARY_ENTRY_CONFIRMATION_DAYS = 2
DEFAULT_EXIT_CONFIRMATION_DAYS = 3
DEFAULT_OUTPUT_DIR = Path(
    "reports/assortment_lifecycle/backtest-2026-01-01_2026-07-31/"
    "hybrid-entry-e1-e2-exit-d3-x1.2-2026-08-15-v1"
)
ACTIVE_STATUSES = {"sale", "working"}
STATUS_LABELS = {"sale": "Растим", "working": "Поддерживаем"}


def apply_hybrid_entry_hysteresis(
    x1_2_rows: Iterable[Mapping[str, Any]],
    x1_5_rows: Iterable[Mapping[str, Any]],
    *,
    boundary_entry_confirmation_days: int,
    exit_confirmation_days: int,
) -> list[dict[str, Any]]:
    """Apply immediate strong entry, confirmed boundary entry, and d3 exit."""

    if boundary_entry_confirmation_days < 1:
        raise ValueError("hybrid_boundary_entry_confirmation_days_positive_required")
    if exit_confirmation_days < 1:
        raise ValueError("hybrid_exit_confirmation_days_positive_required")

    result: list[dict[str, Any]] = []
    previous_code = ""
    previous_adjusted_status = ""
    previous_date: date | None = None
    boundary_entry_streak = 0
    exit_streak = 0

    for x1_2_source, x1_5_source in zip_longest(x1_2_rows, x1_5_rows):
        if x1_2_source is None or x1_5_source is None:
            raise ValueError("hybrid_entry_source_row_count_mismatch")
        row = dict(x1_2_source)
        x1_2_key = (
            _clean(row.get("nomenclature_code")),
            _clean(row.get("business_date")),
        )
        x1_5_key = (
            _clean(x1_5_source.get("nomenclature_code")),
            _clean(x1_5_source.get("business_date")),
        )
        if x1_2_key != x1_5_key:
            raise ValueError(f"hybrid_entry_source_key_mismatch:{x1_2_key}:{x1_5_key}")
        code, business_date_text = x1_2_key
        if not code:
            raise ValueError("hybrid_entry_sku_required")
        business_date = date.fromisoformat(business_date_text)
        if code != previous_code:
            previous_code = code
            previous_adjusted_status = _clean(row.get("previous_status"))
            previous_date = None
            boundary_entry_streak = 0
            exit_streak = 0
        if previous_date is not None and (business_date - previous_date).days != 1:
            raise ValueError(f"hybrid_entry_non_contiguous_sku:{code}:{business_date}")

        base_status = _clean(row.get("status"))
        strong_status = _clean(x1_5_source.get("status"))
        if strong_status == "sale" and base_status != "sale":
            raise ValueError(
                f"hybrid_entry_non_monotonic_threshold_status:{code}:{business_date_text}"
            )

        adjusted_status = base_status
        entry_signal = "none"
        entry_pending = False
        entry_confirmed = False
        entry_immediate = False
        exit_pending = False
        exit_confirmed = False
        if (
            row.get("historical_manual_status_replayed") is True
            or base_status not in ACTIVE_STATUSES
        ):
            boundary_entry_streak = 0
            exit_streak = 0
        elif previous_adjusted_status == "working" and base_status == "sale":
            exit_streak = 0
            if strong_status == "sale":
                entry_signal = "strong_x1.5"
                boundary_entry_streak = 0
                adjusted_status = "sale"
                entry_confirmed = True
                entry_immediate = True
            else:
                entry_signal = "boundary_x1.2_to_x1.5"
                boundary_entry_streak += 1
                if boundary_entry_streak < boundary_entry_confirmation_days:
                    adjusted_status = "working"
                    entry_pending = True
                else:
                    adjusted_status = "sale"
                    entry_confirmed = True
        elif previous_adjusted_status == "sale" and base_status == "working":
            boundary_entry_streak = 0
            exit_streak += 1
            if exit_streak < exit_confirmation_days:
                adjusted_status = "sale"
                exit_pending = True
            else:
                adjusted_status = "working"
                exit_confirmed = True
        else:
            boundary_entry_streak = 0
            exit_streak = 0

        reason_codes = _text_list(row.get("reason_codes"))
        reason_text = _clean(row.get("reason_text"))
        if entry_immediate:
            reason_codes.append("sale_entry_hybrid_strong_immediate")
            reason_text = (
                f"{reason_text} Сильный сигнал ≥×1,5: вход в «Растим» без задержки."
            ).strip()
        elif entry_pending:
            reason_codes.append("sale_entry_hybrid_boundary_pending")
            reason_text = (
                f"{reason_text} Пограничный сигнал ×1,2…×1,5 пока не подтверждён: "
                f"{boundary_entry_streak}/{boundary_entry_confirmation_days} дней."
            ).strip()
        elif entry_confirmed:
            reason_codes.append("sale_entry_hybrid_boundary_confirmed")
            reason_text = (
                f"{reason_text} Пограничный сигнал ×1,2…×1,5 подтверждён за "
                f"{boundary_entry_confirmation_days} дня."
            ).strip()
        if exit_pending:
            reason_codes.append("sale_exit_hysteresis_pending")
            reason_text = (
                f"{reason_text} Выход из «Растим» пока не подтверждён: "
                f"{exit_streak}/{exit_confirmation_days} дней."
            ).strip()
        elif exit_confirmed:
            reason_codes.append("sale_exit_hysteresis_confirmed")
            reason_text = (
                f"{reason_text} Выход из «Растим» подтверждён за " f"{exit_confirmation_days} дня."
            ).strip()

        row.update(
            {
                "previous_status": previous_adjusted_status,
                "base_status": base_status,
                "strong_threshold_status": strong_status,
                "status": adjusted_status,
                "status_label": STATUS_LABELS.get(adjusted_status, _clean(row.get("status_label"))),
                "reason_codes": reason_codes,
                "reason_text": reason_text,
                "hybrid_entry_signal": entry_signal,
                "boundary_entry_confirmation_days": boundary_entry_confirmation_days,
                "exit_hysteresis_confirmation_days": exit_confirmation_days,
                "boundary_entry_streak": boundary_entry_streak,
                "exit_non_growth_streak": exit_streak,
                "entry_hysteresis_pending": entry_pending,
                "entry_hysteresis_confirmed": entry_confirmed,
                "entry_hysteresis_immediate": entry_immediate,
                "exit_hysteresis_pending": exit_pending,
                "exit_hysteresis_confirmed": exit_confirmed,
            }
        )
        result.append(row)
        previous_adjusted_status = adjusted_status
        previous_date = business_date

    return result


def hybrid_entry_policy_hash(
    *,
    x1_2_trajectory_hash: str,
    x1_5_trajectory_hash: str,
    boundary_entry_confirmation_days: int,
    exit_confirmation_days: int,
) -> str:
    return stable_hash(
        {
            "model_version": MODEL_VERSION,
            "x1_2_trajectory_hash": x1_2_trajectory_hash,
            "x1_5_trajectory_hash": x1_5_trajectory_hash,
            "strong_entry_rule": "x1.5_status_sale_enters_immediately",
            "boundary_entry_rule": "x1.2_only_sale_after_consecutive_days",
            "boundary_entry_confirmation_days": boundary_entry_confirmation_days,
            "exit_rule": "consecutive_x1.2_working_days",
            "exit_confirmation_days": exit_confirmation_days,
            "transform_source": inspect.getsource(apply_hybrid_entry_hysteresis),
        }
    )


def build_hybrid_entry_trajectory(
    *,
    store: AssortmentLifecycleReplayStore,
    x1_2_base: StoredTrajectory,
    x1_5_strong: StoredTrajectory,
    boundary_entry_confirmation_days: int,
    exit_confirmation_days: int,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> tuple[StoredTrajectory, dict[str, Any]]:
    if batch_size < 1 or batch_size > 500:
        raise ValueError("hybrid_entry_batch_size_invalid")
    if x1_2_base.dataset_hash != x1_5_strong.dataset_hash:
        raise ValueError("hybrid_entry_source_dataset_mismatch")
    if (x1_2_base.period_from, x1_2_base.period_to) != (
        x1_5_strong.period_from,
        x1_5_strong.period_to,
    ):
        raise ValueError("hybrid_entry_source_period_mismatch")

    policy_hash = hybrid_entry_policy_hash(
        x1_2_trajectory_hash=x1_2_base.trajectory_hash,
        x1_5_trajectory_hash=x1_5_strong.trajectory_hash,
        boundary_entry_confirmation_days=boundary_entry_confirmation_days,
        exit_confirmation_days=exit_confirmation_days,
    )
    cached = store.find_trajectory(
        dataset_hash=x1_2_base.dataset_hash,
        model_version=MODEL_VERSION,
        policy_hash=policy_hash,
        period_from=x1_2_base.period_from,
        period_to=x1_2_base.period_to,
    )
    codes = store.dataset_codes(x1_2_base.dataset_hash)
    if cached is not None:
        return cached, {
            "trajectory_reused": True,
            "resumed": False,
            "completed_sku_count": len(codes),
        }

    build = store.begin_trajectory_build(
        dataset_hash=x1_2_base.dataset_hash,
        model_version=MODEL_VERSION,
        policy_hash=policy_hash,
        period_from=x1_2_base.period_from,
        period_to=x1_2_base.period_to,
        metadata={
            "scope": "Дисплеи",
            "x1_2_base_trajectory_hash": x1_2_base.trajectory_hash,
            "x1_5_strong_trajectory_hash": x1_5_strong.trajectory_hash,
            "strong_entry_rule": "x1.5_status_sale_enters_immediately",
            "boundary_entry_rule": "x1.2_only_sale_after_consecutive_days",
            "boundary_entry_confirmation_days": boundary_entry_confirmation_days,
            "exit_rule": "sale_to_working_after_consecutive_x1.2_working_days",
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
        raise ValueError("hybrid_entry_checkpoint_dataset_mismatch")
    resumed = start > 0
    for offset in range(start, len(codes), batch_size):
        batch = codes[offset : offset + batch_size]
        rows = apply_hybrid_entry_hysteresis(
            store.iter_trajectory_rows_for_codes(x1_2_base.trajectory_hash, batch),
            store.iter_trajectory_rows_for_codes(x1_5_strong.trajectory_hash, batch),
            boundary_entry_confirmation_days=boundary_entry_confirmation_days,
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
        dataset_hash=x1_2_base.dataset_hash,
        model_version=MODEL_VERSION,
        policy_hash=policy_hash,
        period_from=x1_2_base.period_from,
        period_to=x1_2_base.period_to,
    )
    if ready is None:
        raise ValueError("hybrid_entry_trajectory_finalize_readback_failed")
    return ready, {
        "trajectory_reused": False,
        "resumed": resumed,
        "completed_sku_count": len(codes),
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store-path", type=Path, default=DEFAULT_REPLAY_STORE_PATH)
    parser.add_argument("--x1-2-base-trajectory-hash", default=DEFAULT_X1_2_BASE_TRAJECTORY_HASH)
    parser.add_argument(
        "--x1-5-strong-trajectory-hash", default=DEFAULT_X1_5_STRONG_TRAJECTORY_HASH
    )
    parser.add_argument("--comparison-trajectory-hash", default=DEFAULT_COMPARISON_TRAJECTORY_HASH)
    parser.add_argument(
        "--boundary-entry-confirmation-days",
        type=int,
        default=DEFAULT_BOUNDARY_ENTRY_CONFIRMATION_DAYS,
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
    x1_2_base = _stored_trajectory(store, args.x1_2_base_trajectory_hash)
    x1_5_strong = _stored_trajectory(store, args.x1_5_strong_trajectory_hash)
    comparison = _stored_trajectory(store, args.comparison_trajectory_hash)
    if comparison.dataset_hash != x1_2_base.dataset_hash:
        raise ValueError("hybrid_entry_comparison_dataset_mismatch")
    ready, build = build_hybrid_entry_trajectory(
        store=store,
        x1_2_base=x1_2_base,
        x1_5_strong=x1_5_strong,
        boundary_entry_confirmation_days=args.boundary_entry_confirmation_days,
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
        "schema": "assortment_lifecycle_hybrid_entry_shadow_run.v1",
        "status": "complete",
        "dataset_hash": x1_2_base.dataset_hash,
        "x1_2_base_trajectory_hash": x1_2_base.trajectory_hash,
        "x1_5_strong_trajectory_hash": x1_5_strong.trajectory_hash,
        "comparison_trajectory_hash": comparison.trajectory_hash,
        "candidate_trajectory_hash": ready.trajectory_hash,
        "candidate_content_sha256": ready.content_sha256,
        "policy_hash": ready.policy_hash,
        "model_version": ready.model_version,
        "period_from": ready.period_from.isoformat(),
        "period_to": ready.period_to.isoformat(),
        "row_count": ready.row_count,
        "boundary_entry_confirmation_days": args.boundary_entry_confirmation_days,
        "exit_confirmation_days": args.exit_confirmation_days,
        "build": build,
        "comparison_status": stability["status"],
        "production_authorized": False,
        "production_action": "none_read_only",
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "run-manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
