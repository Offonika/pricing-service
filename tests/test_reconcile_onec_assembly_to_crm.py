from __future__ import annotations

from datetime import datetime

from tasks import reconcile_onec_assembly_to_crm as task


class FakeResponse:
    status_code = 200
    text = '{"ok": true}'

    def json(self) -> dict[str, bool]:
        return {"ok": True}


def test_send_to_crm_uses_issued_payload_for_printed_scanned_pickup(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_post(url: str, *, data: dict[str, str], timeout: int) -> FakeResponse:
        captured["url"] = url
        captured["data"] = data
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(task.requests, "post", fake_post)

    event = task.AssemblyEvent(
        event_key="issued-scan:0x01",
        crm_status="issued",
        event_at=datetime(2026, 5, 1, 19, 0, 0),
        rtu_external_id="0xrtu",
        rtu_number="РБГУ0197082",
        rtu_date=datetime(2026, 5, 1, 18, 57, 28),
        onec_order_number="РБГУ0033819",
        site_order_number="214577",
        is_posted=True,
        document_amount="1560.00",
    )

    result = task.send_to_crm(
        event,
        crm_url="https://example.test/hook",
        token="secret",
        dry_run=True,
    )

    payload = captured["data"]
    assert result["ok"] is True
    assert payload["status"] == "issued"
    assert payload["order"] == "214577"
    assert payload["rtu"] == "РБГУ0197082"
    assert payload["issued_at"] == "2026-05-01 19:00:00"
    assert payload["document_amount"] == "1560.00"
    assert payload["dry_run"] == "1"
    assert "assembled_at" not in payload


def test_send_to_crm_keeps_assembled_payload_for_assembly_event(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_post(url: str, *, data: dict[str, str], timeout: int) -> FakeResponse:
        captured["data"] = data
        return FakeResponse()

    monkeypatch.setattr(task.requests, "post", fake_post)

    event = task.AssemblyEvent(
        event_key="assembled:0x01",
        crm_status="assembled",
        event_at=datetime(2026, 5, 1, 18, 59, 0),
        rtu_external_id="0xrtu",
        rtu_number="РБГУ0197082",
        rtu_date=datetime(2026, 5, 1, 18, 57, 28),
        onec_order_number="РБГУ0033819",
        site_order_number="214577",
        is_posted=True,
    )

    task.send_to_crm(
        event,
        crm_url="https://example.test/hook",
        token="secret",
        dry_run=True,
    )

    payload = captured["data"]
    assert payload["status"] == "assembled"
    assert payload["assembled_at"] == "2026-05-01 18:59:00"
    assert "issued_at" not in payload
