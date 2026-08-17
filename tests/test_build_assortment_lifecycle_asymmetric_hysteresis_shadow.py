from datetime import date, timedelta
from pathlib import Path

from app.services.assortment_lifecycle_replay_store import (
    AssortmentLifecycleReplayStore,
    StoredTrajectory,
)
from tasks.build_assortment_lifecycle_asymmetric_hysteresis_shadow import (
    apply_asymmetric_hysteresis,
    build_asymmetric_hysteresis_trajectory,
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


def test_e2_d3_confirms_entry_and_exit_independently() -> None:
    rows = apply_asymmetric_hysteresis(
        _rows(["sale", "sale", "working", "working", "working"]),
        entry_confirmation_days=2,
        exit_confirmation_days=3,
    )

    assert [row["status"] for row in rows] == [
        "working",
        "sale",
        "sale",
        "sale",
        "working",
    ]
    assert rows[0]["entry_hysteresis_pending"] is True
    assert rows[1]["entry_hysteresis_confirmed"] is True
    assert rows[3]["exit_hysteresis_pending"] is True
    assert rows[4]["exit_hysteresis_confirmed"] is True


def test_working_day_interrupts_entry_confirmation() -> None:
    rows = apply_asymmetric_hysteresis(
        _rows(["sale", "working", "sale", "sale"]),
        entry_confirmation_days=2,
        exit_confirmation_days=3,
    )

    assert [row["status"] for row in rows] == ["working", "working", "working", "sale"]
    assert [row["entry_growth_streak"] for row in rows] == [1, 0, 1, 2]


def test_manual_status_is_never_delayed() -> None:
    source = _rows(["sale"])[0]
    source["historical_manual_status_replayed"] = True

    row = apply_asymmetric_hysteresis(
        [source], entry_confirmation_days=2, exit_confirmation_days=3
    )[0]

    assert row["status"] == "sale"
    assert row["entry_growth_streak"] == 0
    assert row["entry_hysteresis_pending"] is False


def test_memory_safe_build_creates_separate_immutable_trajectory(tmp_path: Path) -> None:
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
    base_write = store.put_trajectory(
        dataset_hash=dataset.key,
        model_version="base-v2",
        policy_hash="base-policy",
        period_from=date(2026, 1, 1),
        period_to=date(2026, 1, 5),
        rows=_rows(["sale", "sale", "working", "working", "working"]),
    )
    manifest = next(
        row for row in store.manifest()["trajectories"] if row["trajectory_hash"] == base_write.key
    )
    ready, metadata = build_asymmetric_hysteresis_trajectory(
        store=store,
        base=StoredTrajectory(
            trajectory_hash=manifest["trajectory_hash"],
            dataset_hash=manifest["dataset_hash"],
            model_version=manifest["model_version"],
            policy_hash=manifest["policy_hash"],
            period_from=date.fromisoformat(manifest["period_from"]),
            period_to=date.fromisoformat(manifest["period_to"]),
            content_sha256=manifest["content_sha256"],
            row_count=manifest["row_count"],
            metadata={},
        ),
        entry_confirmation_days=2,
        exit_confirmation_days=3,
        batch_size=1,
    )

    assert ready.trajectory_hash != base_write.key
    assert [row["status"] for row in store.load_trajectory_rows(ready.trajectory_hash)] == [
        "working",
        "sale",
        "sale",
        "sale",
        "working",
    ]
    assert metadata["completed_sku_count"] == 1
    assert store.manifest()["trajectory_builds"] == []
