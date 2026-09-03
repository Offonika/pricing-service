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
        assembly_source="rtu",
        assembly_ref="0xrtu",
        rtu_external_id="0xrtu",
        rtu_number="РБГУ0197082",
        rtu_date=datetime(2026, 5, 1, 18, 57, 28),
        onec_order_number="РБГУ0033819",
        site_order_number="214577",
        is_posted=True,
        execution_status="07",
        delivery_code="PICKUP",
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
    assert payload["assembly_source"] == "rtu"
    assert payload["assembly_ref"] == "0xrtu"
    assert payload["idempotency_key"] == "issued-scan:0x01"
    assert payload["execution_status"] == "07"
    assert payload["delivery_code"] == "PICKUP"
    assert payload["dry_run"] == "1"
    assert "assembled_at" not in payload


def test_send_to_crm_keeps_assembled_payload_for_assembly_event(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_post(url: str, *, data: dict[str, str], timeout: int) -> FakeResponse:
        captured["data"] = data
        return FakeResponse()

    monkeypatch.setattr(task.requests, "post", fake_post)

    event = task.AssemblyEvent(
        event_key="assembled-order:0x01",
        crm_status="assembled",
        event_at=datetime(2026, 5, 1, 18, 59, 0),
        assembly_source="customer_order",
        assembly_ref="0xorder",
        rtu_external_id=None,
        rtu_number=None,
        rtu_date=None,
        onec_order_number="РБГУ0033819",
        site_order_number="214577",
        is_posted=True,
        execution_status="06",
        delivery_code="MM_COURIER",
        payment_mode="by_agreement",
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
    assert payload["assembly_source"] == "customer_order"
    assert payload["assembly_ref"] == "0xorder"
    assert payload["idempotency_key"] == "assembled-order:0x01"
    assert payload["execution_status"] == "06"
    assert payload["delivery_code"] == "MM_COURIER"
    assert payload["payment_mode"] == "by_agreement"
    assert "rtu" not in payload
    assert "issued_at" not in payload


def test_issued_query_is_not_restricted_to_ready_statuses(monkeypatch) -> None:
    captured: dict[str, str] = {}

    class FakeResult:
        def __iter__(self):
            return iter(())

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, statement, _params):
            captured["sql"] = str(statement)
            return FakeResult()

    class FakeEngine:
        def connect(self):
            return FakeConnection()

    monkeypatch.setattr(task, "build_engine", lambda *_args, **_kwargs: FakeEngine())

    assert (
        task.fetch_assembly_events(
            "mssql://example",
            since=datetime(2026, 5, 1),
            limit=10,
        )
        == []
    )

    assembled_sql, issued_sql = captured["sql"].split("UNION ALL", maxsplit=1)
    assert "IN (N'05', N'06')" in assembled_sql
    assert "IN (N'05', N'06')" not in issued_sql


def test_delivery_code_column_accepts_only_physical_field_name() -> None:
    expression = task._delivery_code_expression("_Fld12345")
    assert "ord._Fld12345" in expression
    assert "COALESCE(NULLIF(" in expression
    assert "ord._Fld9266" in expression

    try:
        task._delivery_code_expression("_Fld12345; DROP TABLE orders")
    except ValueError as exc:
        assert "must look like" in str(exc)
    else:
        raise AssertionError("unsafe delivery code column was accepted")
