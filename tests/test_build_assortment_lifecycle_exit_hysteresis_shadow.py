from datetime import date, timedelta
from pathlib import Path

from app.services.assortment_lifecycle_replay_store import (
    AssortmentLifecycleReplayStore,
    StoredTrajectory,
)
from tasks.build_assortment_lifecycle_exit_hysteresis_shadow import (
    apply_exit_hysteresis,
    build_exit_hysteresis_trajectory,
)


def _rows(statuses: list[str], *, code: str = "SKU-1") -> list[dict[str, object]]:
    start = date(2026, 1, 1)
    rows = []
    previous = "sale"
    for offset, status in enumerate(statuses):
        business_date = start + timedelta(days=offset)
        rows.append(
            {
                "business_date": business_date.isoformat(),
                "nomenclature_code": code,
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


def test_exit_requires_seven_consecutive_base_working_days() -> None:
    rows = apply_exit_hysteresis(
        _rows(["working"] * 7 + ["sale"]),
        exit_confirmation_days=7,
    )

    assert [row["status"] for row in rows] == ["sale"] * 6 + ["working", "sale"]
    assert [row["exit_non_growth_streak"] for row in rows[:7]] == list(range(1, 8))
    assert rows[5]["exit_hysteresis_pending"] is True
    assert rows[6]["exit_hysteresis_confirmed"] is True
    assert rows[7]["previous_status"] == "working"


def test_growth_interrupts_exit_confirmation() -> None:
    rows = apply_exit_hysteresis(
        _rows(["working", "working", "sale", "working", "working"]),
        exit_confirmation_days=3,
    )

    assert [row["status"] for row in rows] == ["sale"] * 5
    assert [row["exit_non_growth_streak"] for row in rows] == [1, 2, 0, 1, 2]
    assert all(row["exit_hysteresis_confirmed"] is False for row in rows)


def test_manual_status_is_not_delayed() -> None:
    source = _rows(["working"])[0]
    source["historical_manual_status_replayed"] = True

    row = apply_exit_hysteresis([source], exit_confirmation_days=7)[0]

    assert row["status"] == "working"
    assert row["exit_non_growth_streak"] == 0
    assert row["exit_hysteresis_pending"] is False


def test_memory_safe_build_stores_a_separate_immutable_trajectory(tmp_path: Path) -> None:
    store = AssortmentLifecycleReplayStore(tmp_path / "replay.sqlite3")
    dataset = store.put_dataset(
        scope="Дисплеи",
        observation_from=date(2026, 1, 1),
        observation_to=date(2026, 1, 7),
        facts=[
            {
                "business_date": "2026-01-01",
                "nomenclature_code": "SKU-1",
                "fact_type": "item",
                "payload": {"name": "Тестовый дисплей"},
            }
        ],
    )
    base_write = store.put_trajectory(
        dataset_hash=dataset.key,
        model_version="base-v2",
        policy_hash="base-policy",
        period_from=date(2026, 1, 1),
        period_to=date(2026, 1, 7),
        rows=_rows(["working"] * 7),
    )
    base = next(
        row for row in store.manifest()["trajectories"] if row["trajectory_hash"] == base_write.key
    )
    ready, metadata = build_exit_hysteresis_trajectory(
        store=store,
        base=StoredTrajectory(
            trajectory_hash=base["trajectory_hash"],
            dataset_hash=base["dataset_hash"],
            model_version=base["model_version"],
            policy_hash=base["policy_hash"],
            period_from=date.fromisoformat(base["period_from"]),
            period_to=date.fromisoformat(base["period_to"]),
            content_sha256=base["content_sha256"],
            row_count=base["row_count"],
            metadata={},
        ),
        exit_confirmation_days=7,
        batch_size=1,
    )

    assert ready.trajectory_hash != base_write.key
    assert [row["status"] for row in store.load_trajectory_rows(ready.trajectory_hash)] == [
        "sale",
        "sale",
        "sale",
        "sale",
        "sale",
        "sale",
        "working",
    ]
    assert metadata["completed_sku_count"] == 1
    assert store.manifest()["trajectory_builds"] == []
