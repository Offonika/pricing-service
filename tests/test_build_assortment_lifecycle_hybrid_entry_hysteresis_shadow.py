from datetime import date, timedelta
from pathlib import Path

import pytest

from app.services.assortment_lifecycle_replay_store import (
    AssortmentLifecycleReplayStore,
    StoredTrajectory,
)
from tasks.build_assortment_lifecycle_hybrid_entry_hysteresis_shadow import (
    apply_hybrid_entry_hysteresis,
    build_hybrid_entry_trajectory,
)


def _rows(statuses: list[str], *, previous: str = "working") -> list[dict[str, object]]:
    start = date(2026, 1, 1)
    rows = []
    for offset, status in enumerate(statuses):
        rows.append(
            {
                "business_date": (start + timedelta(days=offset)).isoformat(),
                "nomenclature_code": "SKU-1",
                "name": "Тестовый дисплей",
                "previous_status": previous,
                "status": status,
                "status_label": "Растим" if status == "sale" else "Поддерживаем",
                "demand_state": "growing" if status == "sale" else "stable",
                "reason_codes": ["base"],
                "reason_text": "Базовое решение.",
                "historical_manual_status_replayed": False,
            }
        )
        previous = status
    return rows


def test_strong_entry_is_immediate_and_boundary_entry_waits_two_days() -> None:
    rows = apply_hybrid_entry_hysteresis(
        _rows(["sale", "working", "working", "working", "sale", "sale"]),
        _rows(["sale", "working", "working", "working", "working", "working"]),
        boundary_entry_confirmation_days=2,
        exit_confirmation_days=3,
    )

    assert [row["status"] for row in rows] == [
        "sale",
        "sale",
        "sale",
        "working",
        "working",
        "sale",
    ]
    assert rows[0]["entry_hysteresis_immediate"] is True
    assert rows[4]["entry_hysteresis_pending"] is True
    assert rows[5]["entry_hysteresis_confirmed"] is True


def test_boundary_entry_streak_resets_when_x1_2_sale_is_interrupted() -> None:
    rows = apply_hybrid_entry_hysteresis(
        _rows(["sale", "working", "sale", "sale"]),
        _rows(["working", "working", "working", "working"]),
        boundary_entry_confirmation_days=2,
        exit_confirmation_days=3,
    )

    assert [row["status"] for row in rows] == ["working", "working", "working", "sale"]
    assert [row["boundary_entry_streak"] for row in rows] == [1, 0, 1, 2]


def test_non_monotonic_threshold_status_fails_closed() -> None:
    with pytest.raises(ValueError, match="hybrid_entry_non_monotonic_threshold_status"):
        apply_hybrid_entry_hysteresis(
            _rows(["working"]),
            _rows(["sale"]),
            boundary_entry_confirmation_days=2,
            exit_confirmation_days=3,
        )


def test_source_key_mismatch_fails_closed() -> None:
    strong = _rows(["working"])
    strong[0]["nomenclature_code"] = "SKU-2"
    with pytest.raises(ValueError, match="hybrid_entry_source_key_mismatch"):
        apply_hybrid_entry_hysteresis(
            _rows(["working"]),
            strong,
            boundary_entry_confirmation_days=2,
            exit_confirmation_days=3,
        )


def _stored(store: AssortmentLifecycleReplayStore, trajectory_hash: str) -> StoredTrajectory:
    manifest = next(
        row for row in store.manifest()["trajectories"] if row["trajectory_hash"] == trajectory_hash
    )
    return StoredTrajectory(
        trajectory_hash=manifest["trajectory_hash"],
        dataset_hash=manifest["dataset_hash"],
        model_version=manifest["model_version"],
        policy_hash=manifest["policy_hash"],
        period_from=date.fromisoformat(manifest["period_from"]),
        period_to=date.fromisoformat(manifest["period_to"]),
        content_sha256=manifest["content_sha256"],
        row_count=manifest["row_count"],
        metadata={},
    )


def test_memory_safe_build_uses_two_immutable_trajectories(tmp_path: Path) -> None:
    store = AssortmentLifecycleReplayStore(tmp_path / "replay.sqlite3")
    dataset = store.put_dataset(
        scope="Дисплеи",
        observation_from=date(2026, 1, 1),
        observation_to=date(2026, 1, 5),
        facts=[
            {
                "business_date": "2026-01-01",
                "nomenclature_code": "SKU-1",
                "fact_type": "item",
                "payload": {"name": "Тестовый дисплей"},
            }
        ],
    )
    x1_2 = store.put_trajectory(
        dataset_hash=dataset.key,
        model_version="base-v2",
        policy_hash="x1.2-policy",
        period_from=date(2026, 1, 1),
        period_to=date(2026, 1, 5),
        rows=_rows(["sale", "working", "working", "working", "sale"]),
    )
    x1_5 = store.put_trajectory(
        dataset_hash=dataset.key,
        model_version="base-v2",
        policy_hash="x1.5-policy",
        period_from=date(2026, 1, 1),
        period_to=date(2026, 1, 5),
        rows=_rows(["sale", "working", "working", "working", "working"]),
    )

    ready, metadata = build_hybrid_entry_trajectory(
        store=store,
        x1_2_base=_stored(store, x1_2.key),
        x1_5_strong=_stored(store, x1_5.key),
        boundary_entry_confirmation_days=2,
        exit_confirmation_days=3,
        batch_size=1,
    )

    assert ready.trajectory_hash not in {x1_2.key, x1_5.key}
    assert [row["status"] for row in store.load_trajectory_rows(ready.trajectory_hash)] == [
        "sale",
        "sale",
        "sale",
        "working",
        "working",
    ]
    assert metadata["completed_sku_count"] == 1
    assert store.manifest()["trajectory_builds"] == []
