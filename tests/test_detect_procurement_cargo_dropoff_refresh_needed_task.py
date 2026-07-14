from __future__ import annotations

import json
from pathlib import Path

from tasks import detect_procurement_cargo_dropoff_refresh_needed as task


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_new_cargo_dropoff_date_requires_refresh(tmp_path: Path) -> None:
    input_path = tmp_path / "input.json"
    result_path = tmp_path / "result.json"
    state_path = tmp_path / "state.json"
    _write_json(
        input_path,
        {
            "orders": [
                {
                    "number": "РБГУ0000300",
                    "onec_ref": "0xorder",
                    "supplier": {"onec_ref": "0xsupplier", "title": "855 Android SP"},
                    "cargo_dropoff_date": "2026-06-08T00:00:00",
                }
            ]
        },
    )
    _write_json(
        result_path,
        {
            "input_json": str(input_path),
            "rows": [{"source_number": "РБГУ0000300", "action": "noop"}],
        },
    )

    payload = task.build_detection_payload(
        state_path=state_path,
        result_paths=[result_path],
    )

    assert payload["refresh_needed"] is True
    assert payload["cargo_event_count"] == 1
    assert payload["new_event_count"] == 1
    assert payload["new_events"] == [
        {
            "source_number": "РБГУ0000300",
            "supplier_title": "855 Android SP",
            "cargo_dropoff_date": "2026-06-08",
        }
    ]


def test_known_cargo_dropoff_date_does_not_require_refresh(tmp_path: Path) -> None:
    input_path = tmp_path / "input.json"
    state_path = tmp_path / "state.json"
    _write_json(
        input_path,
        {
            "orders": [
                {
                    "number": "РБГУ0000300",
                    "onec_ref": "0xorder",
                    "supplier": {"onec_ref": "0xsupplier", "title": "855 Android SP"},
                    "cargo_dropoff_date": "2026-06-08T00:00:00",
                }
            ]
        },
    )
    task.apply_state(state_path=state_path, result_paths=[], input_paths=[input_path])

    payload = task.build_detection_payload(
        state_path=state_path,
        result_paths=[],
        input_paths=[input_path],
    )

    assert payload["refresh_needed"] is False
    assert payload["cargo_event_count"] == 1
    assert payload["new_event_count"] == 0


def test_empty_onec_cargo_dropoff_date_is_ignored(tmp_path: Path) -> None:
    input_path = tmp_path / "input.json"
    state_path = tmp_path / "state.json"
    _write_json(
        input_path,
        {
            "orders": [
                {
                    "number": "РБГУ0000300",
                    "supplier": {"title": "855 Android SP"},
                    "cargo_dropoff_date": "1753-01-01T00:00:00",
                }
            ]
        },
    )

    payload = task.build_detection_payload(
        state_path=state_path,
        result_paths=[],
        input_paths=[input_path],
    )

    assert payload["refresh_needed"] is False
    assert payload["cargo_event_count"] == 0
