from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_db
from app.core.config import get_settings
from app.main import app
from app.models import Base, LogisticsUser
from app.models.customer_return import CustomerReturnShipment
from app.services import customer_return_deals as customer_return_deal_service
from app.services import customer_returns as customer_return_service
from app.services.bitrix_logistics_auth import create_logistics_bitrix_session_token
from app.services.customer_returns import CustomerReturnDealLink


def _override_db(engine):
    def override():
        with Session(engine) as session:
            yield session

    return override


def _token(*, actor_id: int, bitrix_user_id: str) -> str:
    token, _ = create_logistics_bitrix_session_token(
        actor_user_id=actor_id,
        domain="portal.example",
        member_id="member-1",
        bitrix_user_id=bitrix_user_id,
    )
    return token


def test_bitrix_customer_returns_role_registers_tracks_and_confirms_pickup(
    monkeypatch,
    tmp_path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'bitrix-customer-returns.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        returns_actor = LogisticsUser(
            external_id="online-returns",
            bitrix_user_id="6357",
            full_name="Андрей Платонов",
            role="returns",
        )
        sender_actor = LogisticsUser(
            external_id="store-sender",
            bitrix_user_id="10",
            full_name="Отправитель",
            role="sender",
        )
        session.add_all([returns_actor, sender_actor])
        session.commit()
        returns_actor_id = returns_actor.id
        sender_actor_id = sender_actor.id

    settings = get_settings()
    monkeypatch.setattr(settings, "logistics_bitrix_app_enabled", True)
    monkeypatch.setattr(settings, "logistics_bitrix_allowed_domains", ["portal.example"])
    monkeypatch.setattr(settings, "logistics_bitrix_allowed_member_ids", ["member-1"])
    monkeypatch.setattr(settings, "logistics_bitrix_session_secret", "test-secret-long-enough")
    monkeypatch.setattr(settings, "logistics_stage_pilot_warehouse_external_ids", [])
    app.dependency_overrides[get_db] = _override_db(engine)
    try:
        client = TestClient(app)
        headers = {
            "Authorization": f"Bearer {_token(actor_id=returns_actor_id, bitrix_user_id='6357')}"
        }

        bootstrap = client.get("/api/bitrix/logistics/bootstrap", headers=headers)
        assert bootstrap.status_code == 200
        assert bootstrap.json()["profile"]["role"] == "returns"
        assert bootstrap.json()["capabilities"] == ["customer_returns"]
        assert bootstrap.json()["warehouses"] == []

        selected_deals = {
            3507: CustomerReturnDealLink(
                deal_id=3507,
                title="Интернет-заказ 241094",
                order_ref="241094",
                stage_id="NEW",
                stage_name="Новая",
                contact_id=77,
                contact_name="Иван Петров",
                responsible_user_id=88,
                responsible_name="Анна Смирнова",
            ),
            3508: CustomerReturnDealLink(
                deal_id=3508,
                title="Интернет-заказ 241095",
                order_ref="241095",
                stage_id="WON",
                stage_name="Завершена",
                closed=True,
                company_id=99,
                company_name="ООО Клиент",
                responsible_user_id=89,
                responsible_name="Олег Сидоров",
            ),
        }
        monkeypatch.setattr(
            customer_return_deal_service,
            "search_customer_return_deals",
            lambda **_kwargs: list(selected_deals.values()),
        )
        monkeypatch.setattr(
            customer_return_deal_service,
            "get_customer_return_deal",
            lambda *, deal_id, **_kwargs: selected_deals[deal_id],
        )

        deal_search = client.get(
            "/api/bitrix/logistics/customer-return-deals",
            headers=headers,
            params={"search": "241094"},
        )
        assert deal_search.status_code == 200
        assert [item["deal_id"] for item in deal_search.json()] == [3507, 3508]
        assert deal_search.json()[1]["closed"] is True

        linked_registration = client.post(
            "/api/bitrix/logistics/customer-returns",
            headers=headers,
            json={
                "carrier": "cdek",
                "tracking_number": "CDEK-DEAL-3507",
                "bitrix_deal_id": 3507,
            },
        )
        assert linked_registration.status_code == 200
        linked_id = linked_registration.json()["shipment"]["id"]
        assert linked_registration.json()["shipment"]["bitrix_deal_id"] == 3507
        assert linked_registration.json()["shipment"]["onec_order_ref"] == "241094"
        assert linked_registration.json()["shipment"]["bitrix_contact_name"] == "Иван Петров"

        relinked = client.put(
            f"/api/bitrix/logistics/customer-returns/{linked_id}/deal-link",
            headers=headers,
            json={"bitrix_deal_id": 3508},
        )
        assert relinked.status_code == 200
        assert relinked.json()["bitrix_deal_id"] == 3508
        assert relinked.json()["bitrix_company_name"] == "ООО Клиент"
        assert relinked.json()["events"][-1]["event_type"] == "deal_link_changed"
        assert relinked.json()["events"][-1]["payload"]["old"]["deal_id"] == 3507

        repeated = client.put(
            f"/api/bitrix/logistics/customer-returns/{linked_id}/deal-link",
            headers=headers,
            json={"bitrix_deal_id": 3508},
        )
        assert repeated.status_code == 200
        assert [event["event_type"] for event in repeated.json()["events"]].count(
            "deal_link_changed"
        ) == 1

        unlinked = client.put(
            f"/api/bitrix/logistics/customer-returns/{linked_id}/deal-link",
            headers=headers,
            json={"bitrix_deal_id": None},
        )
        assert unlinked.status_code == 200
        assert unlinked.json()["bitrix_deal_id"] is None
        assert [event["event_type"] for event in unlinked.json()["events"]].count(
            "deal_link_changed"
        ) == 2

        registered = client.post(
            "/api/bitrix/logistics/customer-returns",
            headers=headers,
            json={
                "carrier": "russian_post",
                "tracking_number": "12345678901234",
                "onec_order_ref": "ЗАКАЗ-3507",
            },
        )
        assert registered.status_code == 200
        assert registered.json()["created"] is True
        shipment_id = registered.json()["shipment"]["id"]
        assert registered.json()["shipment"]["created_by_bitrix_user_id"] == "6357"

        duplicate = client.post(
            "/api/bitrix/logistics/customer-returns",
            headers=headers,
            json={"carrier": "russian_post", "tracking_number": "1234 5678 9012 34"},
        )
        assert duplicate.status_code == 200
        assert duplicate.json()["created"] is False
        assert duplicate.json()["shipment"]["id"] == shipment_id

        spoofed_actor = client.post(
            "/api/bitrix/logistics/customer-returns",
            headers=headers,
            json={
                "carrier": "cdek",
                "tracking_number": "CDEK-3507",
                "created_by_bitrix_user_id": "999",
            },
        )
        assert spoofed_actor.status_code == 422

        with Session(engine) as session:
            stored = session.scalar(
                select(CustomerReturnShipment).where(CustomerReturnShipment.id == shipment_id)
            )
            assert stored is not None
            assert stored.source == "bitrix_ui"
            assert stored.created_by_bitrix_user_id == "6357"
            customer_return_service.record_carrier_event(
                session,
                shipment_id,
                status_code="READY_FOR_PICKUP",
                status_text="Прибыло в отделение",
                occurred_at=datetime.now(UTC),
                storage_deadline_at=datetime.now(UTC) + timedelta(days=5),
                external_event_id="arrival-3507",
            )

        listed = client.get(
            "/api/bitrix/logistics/customer-returns",
            headers=headers,
            params={"carrier": "russian_post", "status": "arrived_at_pickup_point"},
        )
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()] == [shipment_id]

        detail = client.get(
            f"/api/bitrix/logistics/customer-returns/{shipment_id}",
            headers=headers,
        )
        assert detail.status_code == 200
        assert [event["event_type"] for event in detail.json()["events"]] == [
            "registered",
            "carrier_status",
        ]

        picked_up = client.post(
            f"/api/bitrix/logistics/customer-returns/{shipment_id}/pickup",
            headers=headers,
            json={
                "idempotency_key": f"bitrix-ui-pickup-{shipment_id}",
                "comment": "Получено онлайн-отделом",
            },
        )
        assert picked_up.status_code == 200
        assert picked_up.json()["status"] == "picked_up"
        assert picked_up.json()["picked_up_by_bitrix_user_id"] == "6357"
        assert [action["action_type"] for action in picked_up.json()["actions"]].count(
            "onec_return_control"
        ) == 1

        sender_headers = {
            "Authorization": f"Bearer {_token(actor_id=sender_actor_id, bitrix_user_id='10')}"
        }
        forbidden = client.get(
            "/api/bitrix/logistics/customer-returns",
            headers=sender_headers,
        )
        assert forbidden.status_code == 403
    finally:
        app.dependency_overrides.clear()
