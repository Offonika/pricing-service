from __future__ import annotations

import json
from pathlib import Path

from infra.cron.counterparty_duplicates_pull_from_a import (
    _fingerprint_case,
    sync_counterparty_duplicate_cases,
)


def _payload(
    case_id: int = 1, dedupe_key: str = "dup-1", summary_text: str = "summary"
) -> dict[str, object]:
    return {
        "case_id": case_id,
        "dedupe_key": dedupe_key,
        "detected_at": "2026-03-24T12:00:00",
        "risk_level": "P1",
        "reason_codes": ["phone"],
        "records": [
            {"counterparty_ref": "cp-1", "counterparty_name": "A", "phone": "+77771234567"},
            {"counterparty_ref": "cp-2", "counterparty_name": "B", "phone": "+77771234567"},
        ],
        "responsible_code": "finance",
        "status": "new",
        "sla_deadline_at": "2026-03-25T12:00:00",
        "summary_text": summary_text,
        "source_hash": "source-hash-1",
    }


def test_sync_counterparty_duplicate_cases_creates_and_acks(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    acked: list[tuple[int, str | None]] = []

    def fetch_json(path: str, params: dict[str, str]) -> dict[str, object]:
        assert path == "/api/internal/counterparty-duplicates/pending"
        assert params == {}
        return {"items": [_payload()]}

    def ack_case(**payload):
        acked.append((payload["case_id"], payload.get("external_case_id")))
        return payload

    def create_case(**kwargs):
        return "sp-101", "https://bitrix.example/items/101"

    summary = sync_counterparty_duplicate_cases(
        fetch_json=fetch_json,
        ack_case=ack_case,
        state_path=state_path,
        create_case=create_case,
        field_map={},
    )

    assert summary == {"created": 1, "updated": 0, "noop": 0}
    assert acked == [(1, "sp-101")]

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["cases"]["dup-1"]["external_case_id"] == "sp-101"


def test_sync_counterparty_duplicate_cases_noops_on_same_fingerprint(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    payload = _payload()
    state_path.write_text(
        json.dumps(
            {
                "cases": {
                    "dup-1": {
                        "external_case_id": "sp-101",
                        "fingerprint": _fingerprint_case(payload),
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    summary = sync_counterparty_duplicate_cases(
        fetch_json=lambda path, params: {"items": [payload]},
        ack_case=lambda **payload: payload,
        state_path=state_path,
        field_map={},
    )

    assert summary == {"created": 0, "updated": 0, "noop": 1}
