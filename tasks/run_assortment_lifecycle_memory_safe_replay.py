"""Build legacy and v2 lifecycle trajectories with bounded memory and resume.

The command reads an already frozen immutable dataset, processes it by ordered
SKU partitions, checkpoints every completed partition and atomically publishes
only complete trajectories.  It never writes application data, 1C or orders.
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import resource
import shutil
from bisect import bisect_right
from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from app.services.assortment_lifecycle import decide_legacy_assortment_status
from app.services.assortment_lifecycle_replay_store import (
    DEFAULT_REPLAY_STORE_PATH,
    AssortmentLifecycleReplayStore,
    StoredTrajectory,
    stable_hash,
)
from app.services.assortment_lifecycle_v2_policy import (
    DEFAULT_ASSORTMENT_LIFECYCLE_V2_POLICY_PATH,
    DemandStatePolicy,
    load_assortment_lifecycle_v2_policy,
)
from app.services.assortment_lifecycle_v2_replay import (
    V2_REPLAY_MODEL_VERSION,
    build_assortment_lifecycle_v2_trajectory,
)
from tasks.finalize_assortment_lifecycle_v2_replay_audit import finalize_replay_audit
from tasks.report_assortment_lifecycle_v2_historical_backtest import (
    demand_policy_parameters,
    replay_inputs_from_facts,
    v2_replay_policy_hash,
)
from tasks.report_display_auto_order_six_month_backtest import (
    DEFAULT_HISTORY_START,
    LEGACY_REPLAY_MODEL_VERSION,
    PurchaseLine,
    ReceiptLine,
    build_historical_lifecycle_trajectory,
    historical_lifecycle_decision,
    item_active_as_of,
    warmup_lifecycle_statuses,
)

ZERO = Decimal("0")
DEFAULT_DATE_FROM = date(2026, 1, 1)
DEFAULT_DATE_TO = date(2026, 7, 31)
DEFAULT_BATCH_SIZE = 40
DEFAULT_MEMORY_BUDGET_MB = 3072
DEFAULT_MIN_FREE_DISK_GB = 5
DEFAULT_MIN_SWAP_FREE_MB = 256
DEFAULT_OUTPUT_DIR = Path(
    "reports/assortment_lifecycle/backtest-2026-01-01_2026-07-31/"
    "assortment-lifecycle-v2-memory-safe-replay"
)
ProgressCallback = Callable[[Mapping[str, Any]], None]


def resource_preflight(
    *,
    store_path: Path,
    memory_budget_mb: int,
    min_free_disk_gb: int,
    min_swap_free_mb: int,
) -> dict[str, Any]:
    if memory_budget_mb <= 0 or min_free_disk_gb <= 0 or min_swap_free_mb < 0:
        raise ValueError("replay_resource_budget_invalid")
    memory = _meminfo_kb()
    available_mb = int(memory.get("MemAvailable", 0) // 1024)
    swap_free_mb = int(memory.get("SwapFree", 0) // 1024)
    free_disk_bytes = shutil.disk_usage(store_path.parent.resolve()).free
    free_disk_gb = free_disk_bytes // (1024**3)
    blockers: list[str] = []
    if available_mb < memory_budget_mb:
        blockers.append(f"available_memory_below_budget:{available_mb}:{memory_budget_mb}")
    if swap_free_mb < min_swap_free_mb:
        blockers.append(f"swap_free_below_minimum:{swap_free_mb}:{min_swap_free_mb}")
    if free_disk_gb < min_free_disk_gb:
        blockers.append(f"free_disk_below_minimum:{free_disk_gb}:{min_free_disk_gb}")
    payload = {
        "status": "blocked" if blockers else "passed",
        "memory_budget_mb": memory_budget_mb,
        "available_memory_mb": available_mb,
        "swap_free_mb": swap_free_mb,
        "min_swap_free_mb": min_swap_free_mb,
        "free_disk_gb": free_disk_gb,
        "min_free_disk_gb": min_free_disk_gb,
        "blockers": blockers,
    }
    if blockers:
        raise ValueError("replay_resource_preflight_blocked:" + ",".join(blockers))
    return payload


def apply_memory_budget(memory_budget_mb: int) -> None:
    budget_bytes = memory_budget_mb * 1024 * 1024
    current_soft, current_hard = resource.getrlimit(resource.RLIMIT_AS)
    if current_hard not in (-1, resource.RLIM_INFINITY):
        budget_bytes = min(budget_bytes, current_hard)
    resource.setrlimit(resource.RLIMIT_AS, (budget_bytes, budget_bytes))


def legacy_replay_policy_hash() -> str:
    lifecycle_path = Path(inspect.getsourcefile(decide_legacy_assortment_status) or "")
    return stable_hash(
        {
            "model_version": LEGACY_REPLAY_MODEL_VERSION,
            "lifecycle_module_source": lifecycle_path.read_text(encoding="utf-8"),
            "historical_decision": inspect.getsource(historical_lifecycle_decision),
            "warmup": inspect.getsource(warmup_lifecycle_statuses),
            "build_trajectory": inspect.getsource(build_historical_lifecycle_trajectory),
            "item_active_as_of": inspect.getsource(item_active_as_of),
            "memory_safe_partitioning": "ordered_sku_checkpoint.v1",
        }
    )


def build_memory_safe_trajectory(
    *,
    store: AssortmentLifecycleReplayStore,
    dataset_hash: str,
    model: str,
    demand_policy: DemandStatePolicy,
    history_start: date,
    date_from: date,
    date_to: date,
    batch_size: int = DEFAULT_BATCH_SIZE,
    progress: ProgressCallback | None = None,
) -> tuple[StoredTrajectory, dict[str, Any]]:
    if history_start > date_from or date_from > date_to:
        raise ValueError("assortment_lifecycle_memory_safe_replay_period_invalid")
    if batch_size <= 0 or batch_size > 500:
        raise ValueError("assortment_lifecycle_memory_safe_batch_size_invalid")
    if model == "legacy":
        model_version = LEGACY_REPLAY_MODEL_VERSION
        policy_hash = legacy_replay_policy_hash()
        metadata = {
            "source": "reconstructed_legacy",
            "look_ahead_free": True,
            "memory_safe": True,
            "partition": "nomenclature_code",
            "production_action": "none_read_only",
        }
    elif model == "v2":
        model_version = V2_REPLAY_MODEL_VERSION
        policy_hash = v2_replay_policy_hash(demand_policy)
        metadata = {
            "scope": "Дисплеи",
            "look_ahead_free": True,
            "sale_identifiers": "sha256_prefix_16",
            "memory_safe": True,
            "partition": "nomenclature_code",
            "production_action": "none_read_only",
            "demand_policy": demand_policy_parameters(demand_policy),
        }
    else:
        raise ValueError(f"unknown_replay_model:{model}")
    cached = store.find_trajectory(
        dataset_hash=dataset_hash,
        model_version=model_version,
        policy_hash=policy_hash,
        period_from=date_from,
        period_to=date_to,
    )
    if cached is not None:
        return cached, {
            "trajectory_hash": cached.trajectory_hash,
            "policy_hash": policy_hash,
            "row_count": cached.row_count,
            "trajectory_reused": True,
            "resumed": False,
        }

    codes = store.dataset_codes(dataset_hash)
    build = store.begin_trajectory_build(
        dataset_hash=dataset_hash,
        model_version=model_version,
        policy_hash=policy_hash,
        period_from=date_from,
        period_to=date_to,
        metadata=metadata,
    )
    start = bisect_right(codes, build.last_completed_sku or "")
    if build.completed_sku_count != start:
        raise ValueError(
            "replay_checkpoint_dataset_mismatch:"
            f"{build.completed_sku_count}:{start}:{build.last_completed_sku}"
        )
    resumed = start > 0
    for offset in range(start, len(codes), batch_size):
        batch = codes[offset : offset + batch_size]
        facts = store.load_dataset_facts_for_codes(dataset_hash, batch)
        rows = _build_partition(
            model=model,
            facts=facts,
            demand_policy=demand_policy,
            history_start=history_start,
            date_from=date_from,
            date_to=date_to,
        )
        build = store.append_trajectory_partition(
            trajectory_hash=build.trajectory_hash,
            checkpoint_sku=batch[-1],
            completed_sku_count=offset + len(batch),
            rows=rows,
        )
        if progress is not None:
            progress(
                {
                    "event": "checkpoint",
                    "model": model,
                    "trajectory_hash": build.trajectory_hash,
                    "completed_sku_count": build.completed_sku_count,
                    "total_sku_count": len(codes),
                    "row_count": build.row_count,
                    "last_completed_sku": build.last_completed_sku,
                    "rss_mb": _rss_mb(),
                }
            )
    stored = store.finalize_trajectory_build(
        build.trajectory_hash,
        expected_sku_count=len(codes),
    )
    ready = store.find_trajectory(
        dataset_hash=dataset_hash,
        model_version=model_version,
        policy_hash=policy_hash,
        period_from=date_from,
        period_to=date_to,
    )
    if ready is None:
        raise ValueError(f"replay_trajectory_finalize_readback_failed:{stored.key}")
    return ready, {
        "trajectory_hash": ready.trajectory_hash,
        "policy_hash": policy_hash,
        "row_count": ready.row_count,
        "trajectory_reused": stored.reused,
        "resumed": resumed,
        "completed_sku_count": len(codes),
    }


def _build_partition(
    *,
    model: str,
    facts: Sequence[Mapping[str, Any]],
    demand_policy: DemandStatePolicy,
    history_start: date,
    date_from: date,
    date_to: date,
) -> list[dict[str, Any]]:
    items, sales, availability, orders, receipts = replay_inputs_from_facts(facts)
    if model == "v2":
        return build_assortment_lifecycle_v2_trajectory(
            items=items,
            sales_observations_by_code=sales,
            availability_by_code=availability,
            supplier_orders_by_code=orders,
            receipts_by_code=receipts,
            history_start=history_start,
            date_from=date_from,
            date_to=date_to,
            demand_policy=demand_policy,
        )
    purchases = {
        code: [
            PurchaseLine(
                created_at=row.created_at,
                qty=ZERO,
                price=ZERO,
                supplier_name="",
                order_ref="",
                expected_receipt_at=None,
                cargo_handoff_at=row.cargo_handoff_at,
            )
            for row in values
        ]
        for code, values in orders.items()
    }
    legacy_receipts = {
        code: [ReceiptLine(received_at=row.received_at, qty=ZERO) for row in values]
        for code, values in receipts.items()
    }
    daily_sales: dict[str, dict[date, Decimal]] = defaultdict(dict)
    for code, observations in sales.items():
        for row in observations:
            daily_sales[code][row.business_date] = (
                daily_sales[code].get(row.business_date, ZERO) + row.quantity
            )
    return build_historical_lifecycle_trajectory(
        items=items,
        sales_by_code=daily_sales,
        availability_by_code=availability,
        purchase_history=purchases,
        receipt_history=legacy_receipts,
        history_start=history_start,
        date_from=date_from,
        date_to=date_to,
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-hash", required=True)
    parser.add_argument("--replay-store-path", type=Path, default=DEFAULT_REPLAY_STORE_PATH)
    parser.add_argument(
        "--policy-json", type=Path, default=DEFAULT_ASSORTMENT_LIFECYCLE_V2_POLICY_PATH
    )
    parser.add_argument("--history-start", type=date.fromisoformat, default=DEFAULT_HISTORY_START)
    parser.add_argument("--date-from", type=date.fromisoformat, default=DEFAULT_DATE_FROM)
    parser.add_argument("--date-to", type=date.fromisoformat, default=DEFAULT_DATE_TO)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--memory-budget-mb", type=int, default=DEFAULT_MEMORY_BUDGET_MB)
    parser.add_argument("--min-free-disk-gb", type=int, default=DEFAULT_MIN_FREE_DISK_GB)
    parser.add_argument("--min-swap-free-mb", type=int, default=DEFAULT_MIN_SWAP_FREE_MB)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--skip-audit", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    preflight = resource_preflight(
        store_path=args.replay_store_path,
        memory_budget_mb=args.memory_budget_mb,
        min_free_disk_gb=args.min_free_disk_gb,
        min_swap_free_mb=args.min_swap_free_mb,
    )
    apply_memory_budget(args.memory_budget_mb)
    store = AssortmentLifecycleReplayStore(args.replay_store_path)
    fact_count = store.dataset_fact_count(args.dataset_hash)
    policy = load_assortment_lifecycle_v2_policy(args.policy_json)

    def progress(payload: Mapping[str, Any]) -> None:
        rss_mb = _rss_mb()
        free_disk_gb = shutil.disk_usage(args.replay_store_path.parent.resolve()).free // (1024**3)
        if rss_mb >= int(args.memory_budget_mb * Decimal("0.9")):
            raise RuntimeError(f"replay_memory_guard_blocked:{rss_mb}:{args.memory_budget_mb}")
        if free_disk_gb < args.min_free_disk_gb:
            raise RuntimeError(f"replay_disk_guard_blocked:{free_disk_gb}:{args.min_free_disk_gb}")
        print(json.dumps(dict(payload), ensure_ascii=False, sort_keys=True), flush=True)

    legacy, legacy_meta = build_memory_safe_trajectory(
        store=store,
        dataset_hash=args.dataset_hash,
        model="legacy",
        demand_policy=policy.demand,
        history_start=args.history_start,
        date_from=args.date_from,
        date_to=args.date_to,
        batch_size=args.batch_size,
        progress=progress,
    )
    target, v2_meta = build_memory_safe_trajectory(
        store=store,
        dataset_hash=args.dataset_hash,
        model="v2",
        demand_policy=policy.demand,
        history_start=args.history_start,
        date_from=args.date_from,
        date_to=args.date_to,
        batch_size=args.batch_size,
        progress=progress,
    )
    audit = None
    if not args.skip_audit:
        audit = finalize_replay_audit(
            store=store,
            legacy_trajectory_hash=legacy.trajectory_hash,
            v2_trajectory_hash=target.trajectory_hash,
            output_dir=args.output_dir,
        )
    payload = {
        "status": "complete",
        "mode": "memory_safe_resumable",
        "dataset_hash": args.dataset_hash,
        "fact_count": fact_count,
        "period_from": args.date_from.isoformat(),
        "period_to": args.date_to.isoformat(),
        "history_start": args.history_start.isoformat(),
        "resource_preflight": preflight,
        "legacy": legacy_meta,
        "v2": v2_meta,
        "audit": audit,
        "peak_rss_mb": _peak_rss_mb(),
        "production_authorized": False,
        "production_action": "none_read_only",
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "memory-safe-run.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


def _meminfo_kb() -> dict[str, int]:
    result: dict[str, int] = {}
    with Path("/proc/meminfo").open(encoding="utf-8") as handle:
        for line in handle:
            key, raw = line.split(":", 1)
            value = raw.strip().split()[0]
            result[key] = int(value)
    return result


def _rss_mb() -> int:
    with Path("/proc/self/status").open(encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) // 1024
    return 0


def _peak_rss_mb() -> int:
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value // 1024 if os.name == "posix" else value // (1024 * 1024)


if __name__ == "__main__":
    raise SystemExit(main())
