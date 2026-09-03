from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.dependencies import get_db
from app.core.config import get_settings
from app.main import app
from app.models import Base, LogisticsUser
from app.models.customer_return import CustomerReturnShipment
from app.models.expertise import ExpertiseCase
from app.services import customer_return_deals as customer_return_deal_service
from app.services import (
    customer_return_service_requests as customer_return_request_service,
)
from app.services.bitrix_logistics_auth import create_logistics_bitrix_session_token
from app.services.customer_returns import (
    CustomerReturnDealLink,
    CustomerReturnServiceRequestLink,
)


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


def _request(
    item_id: int,
    *,
    deal_id: int | None = 3507,
    order_ref: str | None = "241094",
) -> CustomerReturnServiceRequestLink:
    return CustomerReturnServiceRequestLink(
        item_id=item_id,
        title=f"Обращение {item_id}",
        stage_id="DT1134_55:NEW",
        stage_name="Новое",
        deal_id=deal_id,
        order_ref=order_ref,
        responsible_user_id=88,
        responsible_name="Анна Смирнова",
        site_ticket_id=str(item_id + 7000),
    )


def _deal(deal_id: int) -> CustomerReturnDealLink:
    order_ref = "241094" if deal_id == 3507 else "999999"
    return CustomerReturnDealLink(
        deal_id=deal_id,
        title=f"Интернет-заказ {order_ref}",
        order_ref=order_ref,
        stage_id="NEW",
        stage_name="Новая",
        responsible_user_id=88,
        responsible_name="Анна Смирнова",
    )


def test_feature_flag_rolls_out_to_admin_before_returns(monkeypatch, tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'service-link-roles.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        admin = LogisticsUser(
            external_id="admin",
            bitrix_user_id="1",
            full_name="Администратор",
            role="admin",
        )
        returns = LogisticsUser(
            external_id="returns",
            bitrix_user_id="2",
            full_name="Сотрудник возвратов",
            role="returns",
        )
        session.add_all([admin, returns])
        session.commit()
        admin_id = admin.id
        returns_id = returns.id

    settings = get_settings()
    monkeypatch.setattr(settings, "logistics_bitrix_app_enabled", True)
    monkeypatch.setattr(settings, "logistics_bitrix_allowed_domains", ["portal.example"])
    monkeypatch.setattr(settings, "logistics_bitrix_allowed_member_ids", ["member-1"])
    monkeypatch.setattr(settings, "logistics_bitrix_session_secret", "test-secret-long-enough")
    monkeypatch.setattr(settings, "logistics_stage_pilot_warehouse_external_ids", [])
    monkeypatch.setattr(settings, "customer_return_service_links_enabled", True)
    monkeypatch.setattr(settings, "customer_return_service_links_roles", ["admin"])
    app.dependency_overrides[get_db] = _override_db(engine)
    try:
        client = TestClient(app)
        admin_headers = {"Authorization": f"Bearer {_token(actor_id=admin_id, bitrix_user_id='1')}"}
        returns_headers = {
            "Authorization": f"Bearer {_token(actor_id=returns_id, bitrix_user_id='2')}"
        }

        assert (
            "customer_return_service_links"
            in client.get("/api/bitrix/logistics/bootstrap", headers=admin_headers).json()[
                "capabilities"
            ]
        )
        assert (
            "customer_return_service_links"
            not in client.get("/api/bitrix/logistics/bootstrap", headers=returns_headers).json()[
                "capabilities"
            ]
        )
        assert (
            client.get(
                "/api/bitrix/logistics/customer-return-expertise",
                headers=returns_headers,
            ).status_code
            == 404
        )

        monkeypatch.setattr(
            settings,
            "customer_return_service_links_roles",
            ["admin", "returns"],
        )
        assert (
            "customer_return_service_links"
            in client.get("/api/bitrix/logistics/bootstrap", headers=returns_headers).json()[
                "capabilities"
            ]
        )
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_bff_links_relinks_unlinks_and_preserves_state_on_bitrix_failure(
    monkeypatch,
    tmp_path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'service-link-bff.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        actor = LogisticsUser(
            external_id="admin",
            bitrix_user_id="6357",
            full_name="Андрей Платонов",
            role="admin",
        )
        session.add(actor)
        session.commit()
        actor_id = actor.id

    settings = get_settings()
    monkeypatch.setattr(settings, "logistics_bitrix_app_enabled", True)
    monkeypatch.setattr(settings, "logistics_bitrix_allowed_domains", ["portal.example"])
    monkeypatch.setattr(settings, "logistics_bitrix_allowed_member_ids", ["member-1"])
    monkeypatch.setattr(settings, "logistics_bitrix_session_secret", "test-secret-long-enough")
    monkeypatch.setattr(settings, "logistics_stage_pilot_warehouse_external_ids", [])
    monkeypatch.setattr(settings, "customer_return_service_links_enabled", True)
    monkeypatch.setattr(settings, "customer_return_service_links_roles", ["admin"])
    monkeypatch.setattr(
        customer_return_deal_service,
        "get_customer_return_deal",
        lambda *, deal_id, **_kwargs: _deal(deal_id),
    )
    requests = {
        113401: _request(113401),
        113402: _request(113402),
        113499: _request(113499, deal_id=9999, order_ref="999999"),
    }

    def get_request(*, item_id: int, **_kwargs):
        return requests[item_id]

    monkeypatch.setattr(
        customer_return_request_service,
        "get_customer_return_service_request",
        get_request,
    )
    app.dependency_overrides[get_db] = _override_db(engine)
    try:
        client = TestClient(app)
        headers = {"Authorization": f"Bearer {_token(actor_id=actor_id, bitrix_user_id='6357')}"}
        registered = client.post(
            "/api/bitrix/logistics/customer-returns",
            headers=headers,
            json={"carrier": "cdek", "tracking_number": "CDEK-BFF-SERVICE"},
        )
        assert registered.status_code == 200
        shipment_id = registered.json()["shipment"]["id"]

        linked = client.put(
            f"/api/bitrix/logistics/customer-returns/{shipment_id}/service-request-link",
            headers=headers,
            json={"serviceRequestItemId": 113401},
        )
        assert linked.status_code == 200
        assert linked.json()["bitrix_deal_id"] == 3507
        assert linked.json()["serviceRequest"]["item_id"] == 113401
        assert linked.json()["serviceRequest"]["responsible_name"] == "Анна Смирнова"
        assert linked.json()["events"][-1]["actor_bitrix_user_id"] == "6357"

        relinked = client.put(
            f"/api/bitrix/logistics/customer-returns/{shipment_id}/service-request-link",
            headers=headers,
            json={"serviceRequestItemId": 113402},
        )
        assert relinked.status_code == 200
        assert relinked.json()["serviceRequest"]["item_id"] == 113402
        assert relinked.json()["events"][-1]["payload"]["old"]["item_id"] == 113401

        conflict = client.put(
            f"/api/bitrix/logistics/customer-returns/{shipment_id}/service-request-link",
            headers=headers,
            json={"serviceRequestItemId": 113499},
        )
        assert conflict.status_code == 409
        assert "another Bitrix24 deal" in conflict.json()["detail"]

        def unavailable_request(**_kwargs):
            raise customer_return_request_service.CustomerReturnServiceRequestUnavailable(
                "Bitrix24 service requests are temporarily unavailable"
            )

        monkeypatch.setattr(
            customer_return_request_service,
            "get_customer_return_service_request",
            unavailable_request,
        )
        unavailable = client.put(
            f"/api/bitrix/logistics/customer-returns/{shipment_id}/service-request-link",
            headers=headers,
            json={"serviceRequestItemId": 113401},
        )
        assert unavailable.status_code == 503
        with Session(engine) as session:
            stored = session.get(CustomerReturnShipment, shipment_id)
            assert stored is not None
            assert stored.service_request_item_id == 113402

        monkeypatch.setattr(
            customer_return_request_service,
            "get_customer_return_service_request",
            get_request,
        )
        unlinked = client.put(
            f"/api/bitrix/logistics/customer-returns/{shipment_id}/service-request-link",
            headers=headers,
            json={"serviceRequestItemId": None},
        )
        assert unlinked.status_code == 200
        assert unlinked.json()["serviceRequest"] is None
        assert unlinked.json()["bitrix_deal_id"] == 3507

        missing = client.get(
            "/api/bitrix/logistics/customer-returns",
            headers=headers,
            params={"without_service_request": "true"},
        )
        assert missing.status_code == 200
        assert [item["id"] for item in missing.json()] == [shipment_id]
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_bff_expertise_link_and_search_unavailable_contract(monkeypatch, tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'expertise-link-bff.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        actor = LogisticsUser(
            external_id="admin",
            bitrix_user_id="6357",
            full_name="Андрей Платонов",
            role="admin",
        )
        expertise = ExpertiseCase(
            external_id="expertise-bff",
            onec_expertise_number="ЭКС-3507",
            linked_customer_order_number="241094",
            current_status="registered",
        )
        session.add_all([actor, expertise])
        session.commit()
        actor_id = actor.id
        expertise_id = expertise.id

    settings = get_settings()
    monkeypatch.setattr(settings, "logistics_bitrix_app_enabled", True)
    monkeypatch.setattr(settings, "logistics_bitrix_allowed_domains", ["portal.example"])
    monkeypatch.setattr(settings, "logistics_bitrix_allowed_member_ids", ["member-1"])
    monkeypatch.setattr(settings, "logistics_bitrix_session_secret", "test-secret-long-enough")
    monkeypatch.setattr(settings, "logistics_stage_pilot_warehouse_external_ids", [])
    monkeypatch.setattr(settings, "customer_return_service_links_enabled", True)
    monkeypatch.setattr(settings, "customer_return_service_links_roles", ["admin"])
    monkeypatch.setattr(
        customer_return_request_service,
        "get_customer_return_service_request",
        lambda **_kwargs: _request(113401),
    )
    app.dependency_overrides[get_db] = _override_db(engine)
    try:
        client = TestClient(app)
        headers = {"Authorization": f"Bearer {_token(actor_id=actor_id, bitrix_user_id='6357')}"}
        linked = client.put(
            f"/api/bitrix/logistics/expertise/{expertise_id}/service-request-link",
            headers=headers,
            json={"serviceRequestItemId": 113401},
        )
        assert linked.status_code == 200
        assert linked.json()["service_request_item_id"] == 113401

        found = client.get(
            "/api/bitrix/logistics/customer-return-expertise",
            headers=headers,
            params={"search": "ЭКС-3507"},
        )
        assert found.status_code == 200
        assert [item["id"] for item in found.json()] == [expertise_id]

        def unavailable_search(**_kwargs):
            raise customer_return_request_service.CustomerReturnServiceRequestUnavailable(
                "Bitrix24 service requests are temporarily unavailable"
            )

        monkeypatch.setattr(
            customer_return_request_service,
            "search_customer_return_service_requests",
            unavailable_search,
        )
        unavailable = client.get(
            "/api/bitrix/logistics/customer-return-service-requests",
            headers=headers,
            params={"deal_id": 3507},
        )
        assert unavailable.status_code == 503
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
