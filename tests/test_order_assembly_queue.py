from __future__ import annotations

from collections.abc import Generator
from datetime import datetime, timezone
from xml.etree import ElementTree

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.dependencies import get_db
from app.core.config import get_settings
from app.main import app
from app.models.order_assembly_queue import (
    OrderAssemblyQueueItem,
    OrderAssemblyQueueSyncState,
)
from app.services import order_assembly_queue as service
from app.services import site_order_fulfillment

TABLES = [
    OrderAssemblyQueueItem.__table__,
    OrderAssemblyQueueSyncState.__table__,
]


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer order-token"}


def _engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    for table in TABLES:
        table.create(engine)
    return engine


def _override_db(engine):
    def override() -> Generator[Session, None, None]:
        session = Session(engine)
        try:
            yield session
        finally:
            session.close()

    return override


def _configure(monkeypatch, *, webhook: str = "https://bitrix.example/rest/1/token") -> None:
    monkeypatch.setenv("ORDER_FULFILLMENT_INTERNAL_API_TOKEN", "order-token")
    monkeypatch.setenv("ORDER_FULFILLMENT_BITRIX_WEBHOOK_URL", webhook)
    monkeypatch.delenv("LOGISTICS_INTERNAL_API_TOKEN", raising=False)
    monkeypatch.delenv("MANAGEMENT_INTERNAL_API_TOKEN", raising=False)
    get_settings.cache_clear()


def _deal(
    deal_id: int,
    order_number: str,
    *,
    moved_time: str | None = "2026-08-31T07:00:00+00:00",
    due_at: str | None = None,
    urgent: str = "0",
    urgent_reason: str = "",
    urgent_until: str | None = None,
) -> dict[str, str | None]:
    return {
        "ID": str(deal_id),
        "STAGE_ID": "EXECUTING",
        "MOVED_TIME": moved_time,
        "DATE_MODIFY": "2026-08-31T08:00:00+00:00",
        service.CRM_ORDER_NUMBER_FIELD: order_number,
        service.CRM_DELIVERY_FIELD: "Самовывоз",
        service.CRM_PAYMENT_FIELD: "Оплачен",
        service.CRM_ASSEMBLY_DUE_AT_FIELD: due_at,
        service.CRM_ASSEMBLY_URGENT_FIELD: urgent,
        service.CRM_ASSEMBLY_URGENT_REASON_FIELD: urgent_reason,
        service.CRM_ASSEMBLY_URGENT_UNTIL_FIELD: urgent_until,
    }


def test_assembly_queue_endpoint_returns_fresh_xml_in_priority_order(monkeypatch) -> None:
    _configure(monkeypatch)
    engine = _engine()
    calls: list[tuple[str, dict]] = []

    def fake_call(self, method: str, params: dict | None = None):
        calls.append((method, params or {}))
        return {
            "result": [
                _deal(
                    10,
                    "218010",
                    due_at="2026-08-31T09:00:00+00:00",
                ),
                _deal(
                    11,
                    "218011",
                    due_at="2026-09-01T09:00:00+00:00",
                    urgent="1",
                    urgent_reason="Клиент выезжает",
                    urgent_until="2099-09-01T12:00:00+00:00",
                ),
            ]
        }

    monkeypatch.setattr(site_order_fulfillment.BitrixChatClient, "call", fake_call)
    app.dependency_overrides = {get_db: _override_db(engine)}
    client = TestClient(app)
    try:
        response = client.get(
            "/api/order-fulfillment/assembly-queue?format=xml&limit=500",
            headers=_headers(),
        )
    finally:
        app.dependency_overrides = {}

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    root = ElementTree.fromstring(response.content)
    assert root.tag == "assembly_queue"
    assert root.attrib["stale"] == "false"
    assert root.attrib["truncated"] == "false"
    assert root.attrib["count"] == "2"
    orders = root.findall("order")
    assert [node.findtext("order_number") for node in orders] == ["218011", "218010"]
    assert orders[0].findtext("urgent") == "true"
    assert len(orders[0].findtext("evidence_id") or "") == 64
    assert calls[0][0] == "crm.deal.list"
    assert calls[0][1]["filter"] == {"=STAGE_ID": "EXECUTING"}


def test_stage_entered_fallback_is_first_persisted_observation() -> None:
    engine = _engine()

    class Client:
        def __init__(self) -> None:
            self.payload = _deal(20, "218020", moved_time=None)

        def call(self, method: str, params: dict | None = None):
            return {"result": [self.payload]}

    client = Client()
    first_seen = datetime(2026, 8, 31, 8, 0, tzinfo=timezone.utc)
    second_seen = datetime(2026, 8, 31, 8, 5, tzinfo=timezone.utc)
    with Session(engine) as session:
        first = service.sync_assembly_queue(session, client=client, now=first_seen)
        first_stage_entered_at = first.items[0].stage_entered_at
        session.commit()
        second = service.sync_assembly_queue(session, client=client, now=second_seen)
        second_stage_entered_at = second.items[0].stage_entered_at
        second_synced_at = second.items[0].synced_at
        session.commit()

    assert service._ensure_aware(first_stage_entered_at) == first_seen
    assert service._ensure_aware(second_stage_entered_at) == first_seen
    assert service._ensure_aware(second_synced_at) == second_seen


def test_failed_refresh_returns_503_and_keeps_previous_queue(monkeypatch) -> None:
    _configure(monkeypatch)
    engine = _engine()
    with Session(engine) as session:
        service.sync_assembly_queue(
            session,
            client=type(
                "SeedClient",
                (),
                {"call": lambda self, method, params=None: {"result": [_deal(30, "218030")]}},
            )(),
            now=datetime(2026, 8, 31, 8, 0, tzinfo=timezone.utc),
        )
        session.commit()

    def failed_call(self, method: str, params: dict | None = None):
        raise RuntimeError("upstream secret must not be reflected")

    monkeypatch.setattr(site_order_fulfillment.BitrixChatClient, "call", failed_call)
    app.dependency_overrides = {get_db: _override_db(engine)}
    client = TestClient(app)
    try:
        response = client.get(
            "/api/order-fulfillment/assembly-queue",
            headers=_headers(),
        )
    finally:
        app.dependency_overrides = {}

    assert response.status_code == 503
    root = ElementTree.fromstring(response.content)
    assert root.tag == "assembly_queue_error"
    assert root.attrib["code"] == "bitrix_unavailable"
    assert root.attrib["stale"] == "true"
    assert "secret" not in response.text
    with Session(engine) as session:
        rows = session.scalars(select(OrderAssemblyQueueItem)).all()
        state = service.get_sync_state(session)
    assert [row.order_number for row in rows] == ["218030"]
    assert state is not None
    assert state.last_success_at is not None
    assert state.last_error_code == "bitrix_unavailable"


def test_limit_overflow_does_not_publish_partial_queue(monkeypatch) -> None:
    _configure(monkeypatch)
    engine = _engine()

    def fake_call(self, method: str, params: dict | None = None):
        return {"result": [_deal(index + 1, f"{218000 + index}") for index in range(3)]}

    monkeypatch.setattr(site_order_fulfillment.BitrixChatClient, "call", fake_call)
    app.dependency_overrides = {get_db: _override_db(engine)}
    client = TestClient(app)
    try:
        response = client.get(
            "/api/order-fulfillment/assembly-queue?limit=2",
            headers=_headers(),
        )
    finally:
        app.dependency_overrides = {}

    assert response.status_code == 503
    assert ElementTree.fromstring(response.content).attrib["code"] == (
        "assembly_queue_limit_exceeded"
    )
    with Session(engine) as session:
        assert session.scalars(select(OrderAssemblyQueueItem)).all() == []


def test_missing_bitrix_config_is_fail_closed_xml(monkeypatch) -> None:
    _configure(monkeypatch, webhook="")
    engine = _engine()
    app.dependency_overrides = {get_db: _override_db(engine)}
    client = TestClient(app)
    try:
        response = client.get(
            "/api/order-fulfillment/assembly-queue",
            headers=_headers(),
        )
    finally:
        app.dependency_overrides = {}

    assert response.status_code == 503
    root = ElementTree.fromstring(response.content)
    assert root.attrib["code"] == "bitrix_not_configured"
