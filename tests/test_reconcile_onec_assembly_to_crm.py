from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace

from sqlalchemy import select

from app.core.config import Settings
from app.models import SiteOrderExecutionCase, SiteOrderExecutionEvent
from app.services import site_order_execution_reconciliation as reconciliation
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


def test_service_db_transport_persists_append_only_onec_event(
    db_session,
    monkeypatch,
) -> None:
    @contextmanager
    def fake_session_scope():
        try:
            yield db_session
            db_session.commit()
        except BaseException:
            db_session.rollback()
            raise

    monkeypatch.setattr(task, "session_scope", fake_session_scope)
    event = task.AssemblyEvent(
        event_key="assembled:0x01",
        crm_status="assembled",
        event_at=datetime(2026, 8, 26, 10, 0),
        rtu_external_id="0xrtu",
        rtu_number="РБГУ0001001",
        rtu_date=datetime(2026, 8, 26, 9, 55),
        onec_order_number="РБГУ0002001",
        site_order_number="242901",
        is_posted=True,
    )

    first = task.persist_event_to_service_db(event)
    second = task.persist_event_to_service_db(event)

    assert first["ok"] is True
    assert first["duplicate"] is False
    assert second["duplicate"] is True
    events = db_session.scalars(select(SiteOrderExecutionEvent)).all()
    case = db_session.scalar(select(SiteOrderExecutionCase))
    assert len(events) == 1
    assert events[0].source == "onec"
    assert events[0].event_type == "execution_assembled_raw"
    assert case is not None
    assert case.onec_order_external_id == "РБГУ0002001"


def test_service_db_reconciliation_is_scoped_to_affected_orders(
    db_session,
    monkeypatch,
) -> None:
    @contextmanager
    def fake_session_scope():
        yield db_session

    class FakeClient:
        def __init__(self, url: str) -> None:
            assert url == "https://bitrix.example/rest"

        def call(self, method: str, params: dict):
            assert method == "crm.deal.list"
            assert params["filter"] == {"@UF_CRM_1772784329053": ["245383"]}
            return {"result": []}

    snapshot = reconciliation.ExecutionEvidenceSnapshot(
        site_order_number="245383",
        bitrix_deal_id=90383,
        current_stage="EXECUTING",
        delivery_class="pickup",
        duplicate_deal_ids=(90383,),
        rtu_count=1,
        assembled_rtu_count=1,
        line_coverage_status="complete",
    )
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        task,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            order_fulfillment_bitrix_webhook_url="https://bitrix.example/rest",
        ),
    )
    monkeypatch.setattr(task.fulfillment, "BitrixChatClient", FakeClient)
    monkeypatch.setattr(task.fulfillment_sync, "fetch_sale_order_statuses", lambda orders: {})
    monkeypatch.setattr(
        task.fulfillment_sync,
        "fetch_onec_order_settlements",
        lambda orders, strict: {},
    )
    monkeypatch.setattr(task.fulfillment_sync, "query_rtu_signal_by_orders", lambda orders: {})
    monkeypatch.setattr(
        task.fulfillment_sync,
        "query_onec_order_states_by_orders",
        lambda orders: {},
    )
    monkeypatch.setattr(
        task.fulfillment_sync,
        "_dismantling_started_at_for_deals",
        lambda client, deals: {},
    )
    monkeypatch.setattr(
        task.fulfillment_sync,
        "build_execution_snapshots",
        lambda *args, **kwargs: [snapshot],
    )
    monkeypatch.setattr(
        task.fulfillment_sync,
        "persist_execution_snapshots",
        lambda snapshots, decisions: [SimpleNamespace(outbox_id=17)],
    )

    def fake_worker(session, **kwargs):
        captured.update(kwargs)
        return [SimpleNamespace(result="dismantling_auto_apply_disabled")]

    monkeypatch.setattr(task.stage_outbox_service, "process_stage_outbox", fake_worker)
    monkeypatch.setattr(task, "session_scope", fake_session_scope)

    result = task.reconcile_service_db_orders(["245383", "245383"], apply=False)

    assert result == {"orders": 1, "snapshots": 1, "outbox": 1, "worker": 1}
    assert captured["site_order_numbers"] == ["245383"]
    assert captured["apply"] is False
