from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from app.services.assortment_lifecycle_replay_store import AssortmentLifecycleReplayStore
from tasks.manage_assortment_lifecycle_replay_store import main


def test_cli_manifest_and_export_trajectory(tmp_path: Path, capsys) -> None:
    store_path = tmp_path / "replay.sqlite3"
    store = AssortmentLifecycleReplayStore(store_path)
    dataset = store.put_dataset(
        scope="дисплеи",
        observation_from=date(2026, 1, 1),
        observation_to=date(2026, 7, 31),
        facts=[
            {
                "business_date": "2026-01-01",
                "nomenclature_code": "SKU-1",
                "fact_type": "item",
                "payload": {"name": "Тест"},
            }
        ],
    )
    trajectory = store.put_trajectory(
        dataset_hash=dataset.key,
        model_version="v2-shadow",
        policy_hash="policy-hash",
        period_from=date(2026, 2, 1),
        period_to=date(2026, 7, 31),
        rows=[
            {
                "business_date": "2026-02-01",
                "nomenclature_code": "SKU-1",
                "status": "fruit",
            }
        ],
    )
    manifest_path = tmp_path / "manifest.json"
    assert (
        main(
            [
                "--store-path",
                str(store_path),
                "manifest",
                "--output-json",
                str(manifest_path),
            ]
        )
        == 0
    )
    assert (
        json.loads(manifest_path.read_text(encoding="utf-8"))["datasets"][0]["dataset_hash"]
        == dataset.key
    )

    export_path = tmp_path / "trajectory.json"
    assert (
        main(
            [
                "--store-path",
                str(store_path),
                "export-trajectory",
                "--trajectory-hash",
                trajectory.key,
                "--output-path",
                str(export_path),
            ]
        )
        == 0
    )
    assert json.loads(export_path.read_text(encoding="utf-8"))["rows"][0]["status"] == "fruit"
    assert "exported" in capsys.readouterr().out
