from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from app.services.assortment_lifecycle_replay_store import (
    AssortmentLifecycleReplayStore,
    stable_hash,
)


def _facts(quantity: str = "1") -> list[dict[str, object]]:
    return [
        {
            "business_date": "2026-01-01",
            "nomenclature_code": "SKU-1",
            "fact_type": "sale",
            "payload": {"quantity": quantity},
        },
        {
            "business_date": "2026-01-02",
            "nomenclature_code": "SKU-1",
            "fact_type": "available",
            "payload": {"available": True},
        },
    ]


def _trajectory(status: str = "fruit") -> list[dict[str, object]]:
    return [
        {
            "business_date": "2026-02-01",
            "nomenclature_code": "SKU-1",
            "status": status,
            "reason_codes": ["test"],
        }
    ]


def _dataset(store: AssortmentLifecycleReplayStore):
    return store.put_dataset(
        scope="дисплеи",
        observation_from=date(2026, 1, 1),
        observation_to=date(2026, 7, 31),
        facts=_facts(),
        source_manifest={"source": "test"},
    )


def test_dataset_hash_is_order_independent_and_reused(tmp_path: Path) -> None:
    store = AssortmentLifecycleReplayStore(tmp_path / "replay.sqlite3")
    first = _dataset(store)
    second = store.put_dataset(
        scope="дисплеи",
        observation_from=date(2026, 1, 1),
        observation_to=date(2026, 7, 31),
        facts=list(reversed(_facts())),
        source_manifest={"source": "different-audit-metadata"},
    )

    assert first.key == second.key
    assert first.reused is False
    assert second.reused is True
    assert store.load_dataset_facts(first.key) == _facts()


def test_changed_fact_creates_new_dataset(tmp_path: Path) -> None:
    store = AssortmentLifecycleReplayStore(tmp_path / "replay.sqlite3")
    first = _dataset(store)
    changed = store.put_dataset(
        scope="дисплеи",
        observation_from=date(2026, 1, 1),
        observation_to=date(2026, 7, 31),
        facts=_facts("2"),
    )

    assert first.key != changed.key
    assert len(store.manifest()["datasets"]) == 2


def test_trajectory_reuses_exact_key_and_refuses_changed_content(tmp_path: Path) -> None:
    store = AssortmentLifecycleReplayStore(tmp_path / "replay.sqlite3")
    dataset = _dataset(store)
    common = {
        "dataset_hash": dataset.key,
        "model_version": "legacy-v1-reconstructed",
        "policy_hash": stable_hash({"policy": "v1"}),
        "period_from": date(2026, 2, 1),
        "period_to": date(2026, 7, 31),
        "metadata": {"look_ahead_free": True},
    }

    first = store.put_trajectory(rows=_trajectory(), **common)
    second = store.put_trajectory(rows=_trajectory(), **common)

    assert first.key == second.key
    assert second.reused is True
    assert store.load_trajectory_rows(first.key) == _trajectory()
    assert list(store.iter_trajectory_rows(first.key)) == _trajectory()
    with pytest.raises(ValueError, match="replay_trajectory_key_conflict"):
        store.put_trajectory(rows=_trajectory("working"), **common)


def test_v2_policy_hashes_get_separate_trajectories(tmp_path: Path) -> None:
    store = AssortmentLifecycleReplayStore(tmp_path / "replay.sqlite3")
    dataset = _dataset(store)
    common = {
        "dataset_hash": dataset.key,
        "model_version": "v2-shadow",
        "period_from": date(2026, 2, 1),
        "period_to": date(2026, 7, 31),
    }
    first = store.put_trajectory(
        policy_hash=stable_hash({"growth": "1.2"}), rows=_trajectory("sale"), **common
    )
    second = store.put_trajectory(
        policy_hash=stable_hash({"growth": "1.5"}), rows=_trajectory("working"), **common
    )

    assert first.key != second.key
    assert len(store.manifest()["trajectories"]) == 2


def test_sqlite_tables_are_append_only(tmp_path: Path) -> None:
    path = tmp_path / "replay.sqlite3"
    store = AssortmentLifecycleReplayStore(path)
    dataset = _dataset(store)

    with sqlite3.connect(path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable_replay_store"):
            connection.execute(
                "UPDATE replay_dataset SET scope = 'changed' WHERE dataset_hash = ?",
                (dataset.key,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable_replay_store"):
            connection.execute(
                "DELETE FROM replay_dataset_fact WHERE dataset_hash = ?", (dataset.key,)
            )


def test_checksum_detects_out_of_band_corruption(tmp_path: Path) -> None:
    path = tmp_path / "replay.sqlite3"
    store = AssortmentLifecycleReplayStore(path)
    dataset = _dataset(store)
    trajectory = store.put_trajectory(
        dataset_hash=dataset.key,
        model_version="legacy-v1-reconstructed",
        policy_hash="policy-v1",
        period_from=date(2026, 2, 1),
        period_to=date(2026, 7, 31),
        rows=_trajectory(),
    )
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER trg_replay_trajectory_row_update_immutable")
        connection.execute(
            "UPDATE replay_trajectory_row SET payload_json = '{}' WHERE trajectory_hash = ?",
            (trajectory.key,),
        )

    with pytest.raises(ValueError, match="checksum_mismatch"):
        store.load_trajectory_rows(trajectory.key)


def test_rows_outside_declared_period_are_rejected(tmp_path: Path) -> None:
    store = AssortmentLifecycleReplayStore(tmp_path / "replay.sqlite3")
    with pytest.raises(ValueError, match="replay_fact_outside_dataset_period"):
        store.put_dataset(
            scope="дисплеи",
            observation_from=date(2026, 2, 1),
            observation_to=date(2026, 7, 31),
            facts=_facts(),
        )


def test_trajectory_build_checkpoints_and_publishes_atomically(tmp_path: Path) -> None:
    store = AssortmentLifecycleReplayStore(tmp_path / "replay.sqlite3")
    dataset = _dataset(store)
    build = store.begin_trajectory_build(
        dataset_hash=dataset.key,
        model_version="v2-memory-safe",
        policy_hash="policy-v1",
        period_from=date(2026, 2, 1),
        period_to=date(2026, 2, 2),
        metadata={"memory_safe": True},
    )

    checkpoint = store.append_trajectory_partition(
        trajectory_hash=build.trajectory_hash,
        checkpoint_sku="SKU-1",
        completed_sku_count=1,
        rows=[
            {
                "business_date": "2026-02-01",
                "nomenclature_code": "SKU-1",
                "status": "working",
            }
        ],
    )

    assert checkpoint.last_completed_sku == "SKU-1"
    assert checkpoint.completed_sku_count == 1
    assert checkpoint.row_count == 1
    assert (
        store.find_trajectory(
            dataset_hash=dataset.key,
            model_version="v2-memory-safe",
            policy_hash="policy-v1",
            period_from=date(2026, 2, 1),
            period_to=date(2026, 2, 2),
        )
        is None
    )

    result = store.finalize_trajectory_build(build.trajectory_hash, expected_sku_count=1)

    assert result.reused is False
    assert store.get_trajectory_build(build.trajectory_hash) is None
    assert store.load_trajectory_rows(result.key)[0]["status"] == "working"


def test_trajectory_build_rejects_incomplete_finalize_and_resumes(tmp_path: Path) -> None:
    store = AssortmentLifecycleReplayStore(tmp_path / "replay.sqlite3")
    dataset = _dataset(store)
    common = {
        "dataset_hash": dataset.key,
        "model_version": "v2-memory-safe",
        "policy_hash": "policy-v1",
        "period_from": date(2026, 2, 1),
        "period_to": date(2026, 2, 2),
        "metadata": {"memory_safe": True},
    }
    first = store.begin_trajectory_build(**common)
    store.append_trajectory_partition(
        trajectory_hash=first.trajectory_hash,
        checkpoint_sku="SKU-1",
        completed_sku_count=1,
        rows=[],
    )

    resumed = store.begin_trajectory_build(**common)

    assert resumed.last_completed_sku == "SKU-1"
    assert resumed.completed_sku_count == 1
    with pytest.raises(ValueError, match="replay_trajectory_build_incomplete"):
        store.finalize_trajectory_build(first.trajectory_hash, expected_sku_count=2)
