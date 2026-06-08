from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from infra.cron.counterparty_folder_recommendations_from_a import (
    REPORT_ENDPOINT,
    STATUS_MOVE_RECOMMENDED,
    sync_counterparty_folder_recommendations,
)


def _report() -> dict[str, Any]:
    return {
        "as_of": "2026-05-29",
        "freshness_status": "fresh",
        "source_status": "ready",
        "report_revision": "abc123",
        "summary": {
            "total_count": 1,
            "source_snapshot_count": 5,
            "move_recommended_count": 1,
            "needs_review_count": 0,
        },
        "payload": [
            {
                "counterparty_ref": "cp-site",
                "counterparty_name": "Контрагент из папки Сайт",
                "current_balance": "12000.00",
                "current_folder_name": "08. Сайт",
                "recommended_folder_name": "02. СПБ",
                "debt_department_name": "СПБ",
                "origin_document_ref": "doc-old-spb",
                "origin_document_number": "РТУ-1",
                "origin_document_date": "2026-05-01T10:00:00",
                "overdue_days": 21,
                "credit_depth_days": 7,
                "due_date": "2026-05-08T10:00:00",
                "status": STATUS_MOVE_RECOMMENDED,
                "review_reason": None,
            }
        ],
    }


def test_counterparty_folder_wrapper_exports_csv_and_dedupes(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    def fetch_json(path: str, params: dict[str, str]) -> dict[str, Any]:
        calls.append((path, params))
        return _report()

    state_path = tmp_path / "state.json"
    artifact_dir = tmp_path / "artifacts"
    summary = sync_counterparty_folder_recommendations(
        fetch_json=fetch_json,
        snapshot_date=date(2026, 5, 29),
        state_path=state_path,
        artifact_dir=artifact_dir,
    )

    assert calls == [
        (
            REPORT_ENDPOINT,
            {"date": "2026-05-29", "status": STATUS_MOVE_RECOMMENDED},
        )
    ]
    assert summary["action"] == "export"
    assert summary["exported"] == 1
    artifact_path = Path(summary["artifact_path"])
    assert artifact_path.exists()
    assert "Контрагент из папки Сайт" in artifact_path.read_text(encoding="utf-8-sig")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["reports"]["2026-05-29|abc123"]["export_status"] == "exported"

    second_summary = sync_counterparty_folder_recommendations(
        fetch_json=fetch_json,
        snapshot_date=date(2026, 5, 29),
        state_path=state_path,
        artifact_dir=artifact_dir,
    )
    assert second_summary["action"] == "noop"
    assert second_summary["reason"] == "already_exported"


def test_counterparty_folder_wrapper_dry_run_has_no_side_effects(tmp_path: Path) -> None:
    def fetch_json(path: str, params: dict[str, str]) -> dict[str, Any]:
        assert path == REPORT_ENDPOINT
        assert params["status"] == STATUS_MOVE_RECOMMENDED
        return _report()

    state_path = tmp_path / "state.json"
    artifact_dir = tmp_path / "artifacts"
    summary = sync_counterparty_folder_recommendations(
        fetch_json=fetch_json,
        snapshot_date=date(2026, 5, 29),
        state_path=state_path,
        artifact_dir=artifact_dir,
        dry_run=True,
    )

    assert summary["action"] == "dry_run"
    assert summary["exported"] == 0
    assert not state_path.exists()
    assert not Path(summary["artifact_path"]).exists()
