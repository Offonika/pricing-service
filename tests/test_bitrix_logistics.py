from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.api import bitrix_logistics as bitrix_api
from app.api.dependencies import get_db
from app.core.config import Settings, get_settings
from app.main import app
from app.models import (
    Base,
    LogisticsDraft,
    LogisticsDriver,
    LogisticsManualReview,
    LogisticsTransfer,
    LogisticsTransferEvent,
    LogisticsTransferState,
    LogisticsUser,
    LogisticsWarehouse,
    LogisticsWebLaunchToken,
)
from app.services.bitrix_logistics_auth import (
    BitrixUser,
    create_logistics_bitrix_session_token,
    verify_logistics_bitrix_session_token,
)


def _override_db(engine):
    def override():
        with Session(engine) as session:
            yield session

    return override


def test_bitrix_logistics_session_roles_and_one_time_fallback(monkeypatch, tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'bitrix-logistics.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        source = LogisticsWarehouse(
            external_id="central",
            name="Центральный склад",
            kind="central",
        )
        target = LogisticsWarehouse(
            external_id="teply-stan",
            name="Тёплый Стан",
            kind="store",
        )
        session.add_all([source, target])
        session.flush()
        session.add_all(
            [
                LogisticsUser(
                    external_id="sender",
                    bitrix_user_id="10",
                    full_name="Отправитель",
                    role="sender",
                    default_warehouse_id=source.id,
                ),
                LogisticsUser(
                    external_id="admin",
                    bitrix_user_id="30",
                    full_name="Администратор",
                    role="admin",
                ),
                LogisticsDriver(external_id="driver", full_name="Водитель"),
            ]
        )
        session.add(
            LogisticsTransfer(
                external_id="bitrix-removable-transfer",
                document_number="РТУ-BITRIX-1",
                document_date=datetime(2026, 8, 28, 9, 0),
                source_warehouse_id=source.id,
                target_warehouse_id=target.id,
                barcode="BC-BITRIX-1",
                lookup_code="MMLOG1|rtu|bitrix-removable-transfer|220028",
                onec_status="posted",
            )
        )
        session.add_all(
            [
                LogisticsManualReview(
                    review_type="rtu_target_warehouse_unresolved",
                    source_document_type="rtu",
                    source_external_id="rtu-review-1",
                    reason="RTU address did not match any warehouse",
                    payload={
                        "rtu_number": "РБГУ0408001",
                        "onec_order_number": "РБГУ0067001",
                        "site_order_number": "220001",
                        "source_warehouse_external_id": "source",
                        "source_warehouse_name": "Сайт",
                        "site_delivery_method": "Самовывоз",
                        "site_delivery_address": "secret customer address",
                    },
                ),
                LogisticsManualReview(
                    review_type="rtu_external_carrier_unmapped",
                    source_document_type="rtu",
                    source_external_id="rtu-review-2",
                    reason="external carrier",
                    payload={
                        "rtu_number": "РБГУ0408002",
                        "source_warehouse_external_id": "source",
                    },
                ),
                LogisticsManualReview(
                    review_type="site_order_execution_conflict",
                    source_document_type="site_order",
                    source_external_id="220003",
                    reason="execution reconciliation conflict",
                    payload={"site_order_number": "220003"},
                ),
            ]
        )
        session.commit()
        source_id = source.id
        target_id = target.id
        admin_id = session.scalar(
            select(LogisticsUser.id).where(LogisticsUser.bitrix_user_id == "30")
        )
        driver_id = session.scalar(select(LogisticsDriver.id))

    settings = get_settings()
    monkeypatch.setattr(settings, "logistics_bitrix_app_enabled", True)
    monkeypatch.setattr(settings, "logistics_bitrix_allowed_domains", ["portal.example"])
    monkeypatch.setattr(settings, "logistics_bitrix_allowed_member_ids", ["member-1"])
    monkeypatch.setattr(settings, "logistics_bitrix_session_secret", "test-secret-long-enough")
    monkeypatch.setattr(settings, "logistics_web_session_secret", "fallback-secret-long-enough")
    monkeypatch.setattr(
        settings,
        "logistics_stage_pilot_warehouse_external_ids",
        ["central", "teply-stan", "source", "target"],
    )
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(
        bitrix_api,
        "load_bitrix_current_user",
        lambda **_kwargs: BitrixUser(user_id="10", name="Отправитель"),
    )
    app.dependency_overrides[get_db] = _override_db(engine)
    try:
        client = TestClient(app, base_url="https://testserver")
        response = client.post(
            "/api/bitrix/logistics/session",
            json={
                "access_token": "oauth-token",
                "domain": "portal.example",
                "member_id": "member-1",
            },
        )
        assert response.status_code == 200
        token = response.json()["session_token"]
        cookie = response.headers["set-cookie"]
        assert f"{bitrix_api.BITRIX_SESSION_COOKIE_NAME}=" in cookie
        assert "HttpOnly" in cookie
        assert "Secure" in cookie
        assert "SameSite=none" in cookie
        assert "Path=/api/bitrix/logistics" in cookie

        resumed = client.get("/api/bitrix/logistics/session/resume")
        assert resumed.status_code == 200
        assert resumed.json()["session_token"] == token
        assert resumed.json()["profile"]["full_name"] == "Отправитель"
        assert resumed.json()["expires_in"] > 0

        without_cookie = TestClient(app, base_url="https://testserver").get(
            "/api/bitrix/logistics/session/resume"
        )
        assert without_cookie.status_code == 401
        assert without_cookie.json()["detail"] == "logistics session cookie is missing"
        headers = {"Authorization": f"Bearer {token}"}

        bootstrap = client.get("/api/bitrix/logistics/bootstrap", headers=headers)
        assert bootstrap.status_code == 200
        assert bootstrap.json()["capabilities"] == ["handoff", "monitor", "history"]

        forbidden_receipt = client.post(
            "/api/bitrix/logistics/receipts/draft",
            headers=headers,
            json={"warehouse_id": source_id},
        )
        assert forbidden_receipt.status_code == 403
        foreign_warehouse = client.get(
            "/api/bitrix/logistics/monitor",
            headers=headers,
            params={"warehouse_id": target_id},
        )
        assert foreign_warehouse.status_code == 403

        oversized_comment = client.post(
            "/api/bitrix/logistics/handoffs/draft",
            headers=headers,
            json={
                "warehouse_id": source_id,
                "driver_id": driver_id,
                "default_dropoff_warehouse_id": target_id,
                "comment": "x" * 1001,
            },
        )
        assert oversized_comment.status_code == 422

        handoff_draft = client.post(
            "/api/bitrix/logistics/handoffs/draft",
            headers=headers,
            json={
                "warehouse_id": source_id,
                "driver_id": driver_id,
                "default_dropoff_warehouse_id": 999999,
            },
        )
        assert handoff_draft.status_code == 200
        assert handoff_draft.json()["default_dropoff_warehouse_id"] is None
        restored_bootstrap = client.get("/api/bitrix/logistics/bootstrap", headers=headers)
        assert restored_bootstrap.status_code == 200
        assert restored_bootstrap.json()["open_draft"]["id"] == handoff_draft.json()["id"]

        draft_id = handoff_draft.json()["id"]
        bitrix_scan = client.post(
            f"/api/bitrix/logistics/handoffs/draft/{draft_id}/scan",
            headers=headers,
            json={
                "lookup_code": "MMLOG1|rtu|bitrix-removable-transfer|220028",
                "dropoff_warehouse_id": source_id,
            },
        )
        assert bitrix_scan.status_code == 200
        assert bitrix_scan.json()["items"][0]["dropoff_warehouse_id"] == target_id
        bitrix_item_id = bitrix_scan.json()["items"][0]["id"]
        bitrix_remove = client.post(
            f"/api/bitrix/logistics/handoffs/draft/{draft_id}/items/{bitrix_item_id}/remove",
            headers=headers,
        )
        assert bitrix_remove.status_code == 200
        assert bitrix_remove.json()["item_count"] == 0
        bitrix_cancel = client.post(
            f"/api/bitrix/logistics/handoffs/draft/{draft_id}/cancel",
            headers=headers,
            json={"reason": "Исправление ошибочного черновика Bitrix"},
        )
        assert bitrix_cancel.status_code == 200
        assert bitrix_cancel.json()["status"] == "cancelled"

        replacement_draft = client.post(
            "/api/bitrix/logistics/handoffs/draft",
            headers=headers,
            json={
                "warehouse_id": source_id,
                "driver_id": driver_id,
                "default_dropoff_warehouse_id": target_id,
            },
        )
        assert replacement_draft.status_code == 200
        replacement_draft_id = replacement_draft.json()["id"]
        assert replacement_draft_id != draft_id

        fallback = client.post("/api/bitrix/logistics/fallback-link", headers=headers)
        assert fallback.status_code == 200
        launch_token = parse_qs(urlparse(fallback.json()["url"]).query)["launch"][0]
        exchanged = client.post(
            "/api/bitrix/logistics/fallback-session",
            json={"token": launch_token},
        )
        assert exchanged.status_code == 200
        assert "mm_logistics_session=" in exchanged.headers["set-cookie"]
        repeated = client.post(
            "/api/bitrix/logistics/fallback-session",
            json={"token": launch_token},
        )
        assert repeated.status_code == 401

        web_scan = client.post(
            f"/api/logistics/web/handoffs/draft/{replacement_draft_id}/scan",
            json={"lookup_code": "BC-BITRIX-1"},
        )
        assert web_scan.status_code == 200
        web_item_id = web_scan.json()["items"][0]["id"]
        web_remove = client.post(
            f"/api/logistics/web/handoffs/draft/{replacement_draft_id}/items/{web_item_id}/remove"
        )
        assert web_remove.status_code == 200
        assert web_remove.json()["item_count"] == 0
        web_cancel = client.post(
            f"/api/logistics/web/handoffs/draft/{replacement_draft_id}/cancel",
            json={"reason": "Исправление ошибочного fallback-черновика"},
        )
        assert web_cancel.status_code == 200
        assert web_cancel.json()["status"] == "cancelled"
        with Session(engine) as audit_session:
            launch_audit = audit_session.scalar(
                select(LogisticsWebLaunchToken).where(
                    LogisticsWebLaunchToken.token_hash
                    == hashlib.sha256(launch_token.encode()).hexdigest()
                )
            )
            assert launch_audit is not None
            assert launch_audit.consumed_at is not None
            assert launch_audit.created_at <= launch_audit.consumed_at

        admin_token, _expires_at = create_logistics_bitrix_session_token(
            actor_user_id=admin_id,
            domain="portal.example",
            member_id="member-1",
            bitrix_user_id="30",
            settings=settings,
        )
        admin_expected = client.get(
            "/api/bitrix/logistics/expected-deliveries",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert admin_expected.status_code == 200
        assert admin_expected.json() == []

        review_page = client.get(
            "/api/bitrix/logistics/errors",
            headers={"Authorization": f"Bearer {admin_token}"},
            params={"limit": 1, "offset": 0},
        )
        assert review_page.status_code == 200
        review_payload = review_page.json()
        assert review_payload["total"] == 1
        assert review_payload["limit"] == 1
        assert len(review_payload["items"]) == 1
        assert review_payload["counts"] == {"rtu_target_warehouse_unresolved": 1}
        assert "site_order_execution_conflict" not in review_payload["counts"]
        assert "payload" not in review_payload["items"][0]
        assert "source_external_id" not in review_payload["items"][0]
        assert "secret customer address" not in review_page.text
        assert "RTU address did not match any warehouse" not in review_page.text

        filtered_reviews = client.get(
            "/api/bitrix/logistics/errors",
            headers={"Authorization": f"Bearer {admin_token}"},
            params={"review_type": "rtu_target_warehouse_unresolved"},
        )
        assert filtered_reviews.status_code == 200
        assert filtered_reviews.json()["total"] == 1
        assert filtered_reviews.json()["items"][0]["document_number"] == "РБГУ0408001"

        external_reviews = client.get(
            "/api/bitrix/logistics/errors",
            headers={"Authorization": f"Bearer {admin_token}"},
            params={"review_type": "rtu_external_carrier_unmapped"},
        )
        assert external_reviews.status_code == 200
        assert external_reviews.json()["total"] == 1
        assert external_reviews.json()["items"][0]["document_number"] == "РБГУ0408002"

        foreign_reviews = client.get(
            "/api/bitrix/logistics/errors",
            headers={"Authorization": f"Bearer {admin_token}"},
            params={"review_type": "site_order_execution_conflict"},
        )
        assert foreign_reviews.status_code == 200
        assert foreign_reviews.json()["total"] == 0
        assert foreign_reviews.json()["items"] == []
    finally:
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()


def test_bitrix_logistics_session_expires_at_ttl_boundary() -> None:
    settings = Settings(
        logistics_bitrix_app_enabled=True,
        logistics_bitrix_allowed_domains=["portal.example"],
        logistics_bitrix_allowed_member_ids=["member-1"],
        logistics_bitrix_session_secret="test-secret-long-enough",
        logistics_bitrix_session_ttl_seconds=60,
    )
    token, _expires_at = create_logistics_bitrix_session_token(
        actor_user_id=1,
        domain="portal.example",
        member_id="member-1",
        bitrix_user_id="10",
        settings=settings,
        now=100,
    )

    with pytest.raises(HTTPException) as exc_info:
        verify_logistics_bitrix_session_token(token, settings=settings, now=160)

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "logistics session expired"


def test_admin_can_handoff_and_receive_at_selected_warehouses(
    monkeypatch,
    tmp_path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'bitrix-admin-logistics.db'}")
    Base.metadata.create_all(engine)
    now = datetime(2026, 8, 28, 9, 0)
    with Session(engine) as session:
        source = LogisticsWarehouse(
            external_id="central",
            name="Центральный склад",
            kind="central",
        )
        target = LogisticsWarehouse(
            external_id="teply-stan",
            name="Тёплый Стан",
            kind="store",
        )
        foreign = LogisticsWarehouse(
            external_id="foreign-store",
            name="Вне пилота",
            kind="store",
        )
        admin = LogisticsUser(
            external_id="admin",
            bitrix_user_id="30",
            full_name="Администратор",
            role="admin",
        )
        driver = LogisticsDriver(external_id="driver", full_name="Водитель")
        session.add_all([source, target, foreign, admin, driver])
        session.flush()
        transfer = LogisticsTransfer(
            external_id="admin-transfer",
            document_number="РТУ-ADMIN-1",
            document_date=now,
            source_warehouse_id=source.id,
            target_warehouse_id=target.id,
            barcode="BC-ADMIN-1",
            lookup_code="MMLOG1|rtu|admin-transfer|220030",
            onec_status="posted",
        )
        session.add(transfer)
        outside_transfer = LogisticsTransfer(
            external_id="outside-pilot-transfer",
            document_number="РТУ-OUTSIDE-1",
            document_date=datetime(2026, 8, 28, 8, 30),
            source_warehouse_id=foreign.id,
            target_warehouse_id=foreign.id,
            barcode="BC-OUTSIDE-1",
            lookup_code="MMLOG1|rtu|outside-pilot-transfer|220099",
            onec_status="posted",
        )
        session.add(outside_transfer)
        session.flush()
        session.add(
            LogisticsTransferState(
                transfer_id=transfer.id,
                status="at_warehouse",
                current_warehouse_id=source.id,
                last_event_type="synced",
                last_event_at=now,
                version=1,
            )
        )
        session.commit()
        source_id = source.id
        target_id = target.id
        foreign_id = foreign.id
        admin_id = admin.id
        driver_id = driver.id
        transfer_id = transfer.id
        outside_transfer_id = outside_transfer.id

    settings = get_settings()
    monkeypatch.setattr(settings, "logistics_bitrix_app_enabled", True)
    monkeypatch.setattr(settings, "logistics_bitrix_allowed_domains", ["portal.example"])
    monkeypatch.setattr(settings, "logistics_bitrix_allowed_member_ids", ["member-1"])
    monkeypatch.setattr(settings, "logistics_bitrix_session_secret", "test-secret-long-enough")
    monkeypatch.setattr(settings, "logistics_web_session_secret", "fallback-secret-long-enough")
    monkeypatch.setattr(
        settings,
        "logistics_stage_pilot_warehouse_external_ids",
        ["central", "teply-stan"],
    )
    admin_token, _expires_at = create_logistics_bitrix_session_token(
        actor_user_id=admin_id,
        domain="portal.example",
        member_id="member-1",
        bitrix_user_id="30",
        settings=settings,
    )
    headers = {"Authorization": f"Bearer {admin_token}"}

    app.dependency_overrides[get_db] = _override_db(engine)
    try:
        client = TestClient(app, base_url="https://testserver")
        bootstrap = client.get("/api/bitrix/logistics/bootstrap", headers=headers)
        assert bootstrap.status_code == 200
        assert bootstrap.json()["capabilities"] == [
            "handoff",
            "receipt",
            "expected",
            "monitor",
            "history",
            "errors",
            "customer_returns",
        ]
        assert {warehouse["external_id"] for warehouse in bootstrap.json()["warehouses"]} == {
            "central",
            "teply-stan",
        }
        pilot_monitor = client.get(
            "/api/bitrix/logistics/monitor",
            headers=headers,
        )
        assert pilot_monitor.status_code == 200
        assert {item["transfer_id"] for item in pilot_monitor.json()} == {transfer_id}
        outside_history = client.get(
            f"/api/bitrix/logistics/transfers/{outside_transfer_id}/history",
            headers=headers,
        )
        assert outside_history.status_code == 403
        outside_pilot = client.post(
            "/api/bitrix/logistics/handoffs/draft",
            headers=headers,
            json={"warehouse_id": foreign_id, "driver_id": driver_id},
        )
        assert outside_pilot.status_code == 403
        assert outside_pilot.json()["detail"] == "warehouse is outside logistics pilot"

        handoff = client.post(
            "/api/bitrix/logistics/handoffs/draft",
            headers=headers,
            json={
                "warehouse_id": source_id,
                "driver_id": driver_id,
                "default_dropoff_warehouse_id": target_id,
            },
        )
        assert handoff.status_code == 200
        handoff_id = handoff.json()["id"]
        assert (
            client.post(
                f"/api/bitrix/logistics/handoffs/draft/{handoff_id}/scan",
                headers=headers,
                json={"lookup_code": "MMLOG1|rtu|admin-transfer|220030"},
            ).status_code
            == 200
        )
        handoff_confirm = client.post(
            f"/api/bitrix/logistics/handoffs/draft/{handoff_id}/confirm",
            headers=headers,
            json={"idempotency_key": "admin-handoff-1"},
        )
        assert handoff_confirm.status_code == 200

        receipt = client.post(
            "/api/bitrix/logistics/receipts/draft",
            headers=headers,
            json={"warehouse_id": target_id},
        )
        assert receipt.status_code == 200
        receipt_id = receipt.json()["id"]
        assert (
            client.post(
                f"/api/bitrix/logistics/receipts/draft/{receipt_id}/scan",
                headers=headers,
                json={"lookup_code": "BC-ADMIN-1"},
            ).status_code
            == 200
        )
        receipt_confirm = client.post(
            f"/api/bitrix/logistics/receipts/draft/{receipt_id}/confirm",
            headers=headers,
            json={"idempotency_key": "admin-receipt-1"},
        )
        assert receipt_confirm.status_code == 200

        fallback = client.post("/api/bitrix/logistics/fallback-link", headers=headers)
        launch_token = parse_qs(urlparse(fallback.json()["url"]).query)["launch"][0]
        assert (
            client.post(
                "/api/bitrix/logistics/fallback-session",
                json={"token": launch_token},
            ).status_code
            == 200
        )
        outside_fallback = client.post(
            "/api/logistics/web/handoffs/draft",
            json={"warehouse_id": foreign_id, "driver_id": driver_id},
        )
        assert outside_fallback.status_code == 403
        assert outside_fallback.json()["detail"] == "warehouse is outside logistics pilot"
        web_handoff = client.post(
            "/api/logistics/web/handoffs/draft",
            json={
                "warehouse_id": target_id,
                "driver_id": driver_id,
                "default_dropoff_warehouse_id": source_id,
            },
        )
        assert web_handoff.status_code == 200
        assert (
            client.post(
                f"/api/logistics/web/handoffs/draft/{web_handoff.json()['id']}/cancel",
                json={"reason": "admin fallback smoke"},
            ).status_code
            == 200
        )

        with Session(engine) as session:
            outside_draft = LogisticsDraft(
                draft_type="handoff",
                warehouse_id=foreign_id,
                actor_user_id=admin_id,
                driver_id=driver_id,
            )
            session.add(outside_draft)
            session.commit()
            outside_draft_id = outside_draft.id
        blocked_old_draft = client.post(
            f"/api/bitrix/logistics/handoffs/draft/{outside_draft_id}/scan",
            headers=headers,
            json={"lookup_code": "BC-OUTSIDE-1"},
        )
        assert blocked_old_draft.status_code == 403
        cancellable_old_draft = client.post(
            f"/api/bitrix/logistics/handoffs/draft/{outside_draft_id}/cancel",
            headers=headers,
            json={"reason": "Черновик вне текущего пилота"},
        )
        assert cancellable_old_draft.status_code == 200
        assert cancellable_old_draft.json()["status"] == "cancelled"

        monkeypatch.setattr(settings, "logistics_stage_pilot_warehouse_external_ids", [])
        empty_pilot = client.get("/api/bitrix/logistics/bootstrap", headers=headers)
        assert empty_pilot.status_code == 200
        assert empty_pilot.json()["warehouses"] == []
        empty_monitor = client.get("/api/bitrix/logistics/monitor", headers=headers)
        assert empty_monitor.status_code == 200
        assert empty_monitor.json() == []
        fail_closed = client.post(
            "/api/bitrix/logistics/handoffs/draft",
            headers=headers,
            json={"warehouse_id": source_id, "driver_id": driver_id},
        )
        assert fail_closed.status_code == 403

        with Session(engine) as session:
            state = session.get(LogisticsTransferState, transfer_id)
            assert state is not None
            assert state.status == "at_warehouse"
            assert state.current_warehouse_id == target_id
            events = session.scalars(
                select(LogisticsTransferEvent)
                .where(LogisticsTransferEvent.transfer_id == transfer_id)
                .order_by(LogisticsTransferEvent.id)
            ).all()
            assert [event.event_type for event in events] == [
                "handed_to_driver",
                "accepted_at_point",
            ]
            assert {event.source for event in events} == {"bitrix"}
    finally:
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()


def test_bitrix_logistics_fallback_link_rejects_expired_token(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'expired-fallback.db'}")
    Base.metadata.create_all(engine)
    raw_token = "expired-launch-token"
    with Session(engine) as session:
        warehouse = LogisticsWarehouse(
            external_id="central",
            name="Центральный склад",
            kind="central",
        )
        session.add(warehouse)
        session.flush()
        actor = LogisticsUser(
            external_id="sender",
            bitrix_user_id="10",
            full_name="Отправитель",
            role="sender",
            default_warehouse_id=warehouse.id,
        )
        session.add(actor)
        session.flush()
        session.add(
            LogisticsWebLaunchToken(
                token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
                actor_user_id=actor.id,
                expires_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1),
            )
        )
        session.commit()

    app.dependency_overrides[get_db] = _override_db(engine)
    try:
        response = TestClient(app).post(
            "/api/bitrix/logistics/fallback-session",
            json={"token": raw_token},
        )
        assert response.status_code == 401
        assert response.json()["detail"] == "fallback link is invalid or expired"
    finally:
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()


def test_receiver_can_read_only_history_for_assigned_warehouse(
    monkeypatch,
    tmp_path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'receiver-history.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        source = LogisticsWarehouse(external_id="central", name="ЦС", kind="central")
        target = LogisticsWarehouse(external_id="target", name="Целевой", kind="store")
        foreign = LogisticsWarehouse(external_id="foreign", name="Чужой", kind="store")
        session.add_all([source, target, foreign])
        session.flush()
        receiver = LogisticsUser(
            external_id="receiver",
            bitrix_user_id="20",
            full_name="Получатель",
            role="receiver",
            default_warehouse_id=target.id,
        )
        session.add(receiver)
        session.flush()
        transfers = []
        for suffix, dropoff in (("visible", target), ("foreign", foreign)):
            transfer = LogisticsTransfer(
                external_id=f"transfer-{suffix}",
                document_number=f"РТУ-{suffix}",
                document_date=datetime(2026, 8, 26, 9, 0),
                source_warehouse_id=source.id,
                target_warehouse_id=dropoff.id,
                barcode=f"BC-{suffix}",
                onec_status="posted",
            )
            session.add(transfer)
            session.flush()
            session.add_all(
                [
                    LogisticsTransferState(
                        transfer_id=transfer.id,
                        status="in_transit",
                        current_warehouse_id=None,
                        dropoff_warehouse_id=dropoff.id,
                        last_event_type="handed_to_driver",
                        last_event_at=datetime(2026, 8, 26, 9, 5),
                        version=1,
                    ),
                    LogisticsTransferEvent(
                        transfer_id=transfer.id,
                        event_type="handed_to_driver",
                        event_at=datetime(2026, 8, 26, 9, 5),
                        warehouse_id=source.id,
                        dropoff_warehouse_id=dropoff.id,
                        user_id=receiver.id,
                        source="bitrix",
                    ),
                ]
            )
            transfers.append(transfer)
        session.commit()
        receiver_id = receiver.id
        visible_id = transfers[0].id
        foreign_id = transfers[1].id

    settings = get_settings()
    monkeypatch.setattr(settings, "logistics_bitrix_app_enabled", True)
    monkeypatch.setattr(settings, "logistics_bitrix_allowed_domains", ["portal.example"])
    monkeypatch.setattr(settings, "logistics_bitrix_allowed_member_ids", ["member-1"])
    monkeypatch.setattr(settings, "logistics_bitrix_session_secret", "test-secret-long-enough")
    token, _expires_at = create_logistics_bitrix_session_token(
        actor_user_id=receiver_id,
        domain="portal.example",
        member_id="member-1",
        bitrix_user_id="20",
        settings=settings,
    )
    headers = {"Authorization": f"Bearer {token}"}

    app.dependency_overrides[get_db] = _override_db(engine)
    try:
        client = TestClient(app)
        visible = client.get(
            f"/api/bitrix/logistics/transfers/{visible_id}/history",
            headers=headers,
        )
        assert visible.status_code == 200
        assert [item["source"] for item in visible.json()] == ["bitrix"]

        forbidden = client.get(
            f"/api/bitrix/logistics/transfers/{foreign_id}/history",
            headers=headers,
        )
        assert forbidden.status_code == 403
    finally:
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()
