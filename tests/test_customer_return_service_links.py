from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models import Base
from app.models.expertise import ExpertiseCase, ExpertiseCaseEvent
from app.services.customer_returns import (
    CustomerReturnConflict,
    CustomerReturnDealLink,
    CustomerReturnServiceRequestLink,
    attach_expertise_cases,
    list_returns,
    register_return,
    update_expertise_service_request_link,
    update_return_deal_link,
    update_return_service_request_link,
)


def _request(
    item_id: int,
    *,
    deal_id: int | None = 3507,
    order_ref: str | None = "241094",
    title: str | None = None,
) -> CustomerReturnServiceRequestLink:
    return CustomerReturnServiceRequestLink(
        item_id=item_id,
        title=title or f"Обращение {item_id}",
        stage_id="DT1134_55:NEW",
        stage_name="Новое",
        deal_id=deal_id,
        order_ref=order_ref,
        responsible_user_id=88,
        responsible_name="Анна Смирнова",
        site_ticket_id=str(7000 + item_id),
    )


def _deal(deal_id: int = 3507, order_ref: str = "241094") -> CustomerReturnDealLink:
    return CustomerReturnDealLink(
        deal_id=deal_id,
        title=f"Интернет-заказ {order_ref}",
        order_ref=order_ref,
        stage_id="NEW",
        stage_name="Новая",
        responsible_user_id=88,
        responsible_name="Анна Смирнова",
    )


def test_service_request_link_relink_unlink_and_multiple_shipments(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'service-links.db'}")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        first, _ = register_return(
            db,
            carrier="cdek",
            tracking_number="CDEK-SERVICE-1",
            source="bitrix_ui",
            created_by_bitrix_user_id="6357",
        )
        second, _ = register_return(
            db,
            carrier="russian_post",
            tracking_number="12345678901234",
            source="bitrix_ui",
            created_by_bitrix_user_id="6357",
            deal_link=_deal(),
        )

        first = update_return_service_request_link(
            db,
            first.id,
            service_request_link=_request(113401),
            actor_bitrix_user_id="6357",
            deal_link_if_missing=_deal(),
        )
        second = update_return_service_request_link(
            db,
            second.id,
            service_request_link=_request(113401),
            actor_bitrix_user_id="6357",
        )

        assert first.bitrix_deal_id == 3507
        assert first.service_request_item_id == 113401
        assert first.service_request_deal_id == 3507
        assert first.service_request_title == "Обращение 113401"
        assert first.service_request_linked_by_user_id == "6357"
        assert first.bitrix_case_id == "113401"
        assert first.site_ticket_id == str(7000 + 113401)
        assert second.service_request_item_id == 113401
        assert len(list_returns(db, without_service_request=False)) == 2
        assert list_returns(db, without_service_request=True) == []

        first = update_return_service_request_link(
            db,
            first.id,
            service_request_link=_request(113402),
            actor_bitrix_user_id="6357",
        )
        assert first.service_request_item_id == 113402
        assert first.events[-1].payload["old"]["item_id"] == 113401
        assert first.events[-1].payload["new"]["item_id"] == 113402

        first = update_return_service_request_link(
            db,
            first.id,
            service_request_link=None,
            actor_bitrix_user_id="6357",
        )
        assert first.service_request_item_id is None
        assert first.bitrix_deal_id == 3507
        assert first.bitrix_case_id is None
        assert first.events[-1].payload["old"]["item_id"] == 113402
        assert first.events[-1].payload["new"] is None

    engine.dispose()


def test_service_request_and_deal_conflicts_do_not_change_existing_link(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'service-link-conflict.db'}")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        shipment, _ = register_return(
            db,
            carrier="cdek",
            tracking_number="CDEK-SERVICE-CONFLICT",
            source="bitrix_ui",
            created_by_bitrix_user_id="6357",
            deal_link=_deal(),
        )
        shipment = update_return_service_request_link(
            db,
            shipment.id,
            service_request_link=_request(113401),
            actor_bitrix_user_id="6357",
        )

        with pytest.raises(CustomerReturnConflict, match="another Bitrix24 deal"):
            update_return_service_request_link(
                db,
                shipment.id,
                service_request_link=_request(113499, deal_id=9999, order_ref="999999"),
                actor_bitrix_user_id="6357",
            )
        db.expire_all()
        assert db.get(type(shipment), shipment.id).service_request_item_id == 113401

        with pytest.raises(CustomerReturnConflict, match="remove or replace"):
            update_return_deal_link(
                db,
                shipment.id,
                deal_link=_deal(9999, "999999"),
                actor_bitrix_user_id="6357",
            )
        db.expire_all()
        stored = db.get(type(shipment), shipment.id)
        assert stored is not None
        assert stored.bitrix_deal_id == 3507
        assert stored.service_request_item_id == 113401

    engine.dispose()


def test_expertise_links_via_request_with_order_guard_and_audit(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'expertise-service-links.db'}")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        shipment, _ = register_return(
            db,
            carrier="cdek",
            tracking_number="CDEK-EXPERTISE-LINK",
            source="bitrix_ui",
            created_by_bitrix_user_id="6357",
            deal_link=_deal(),
        )
        shipment = update_return_service_request_link(
            db,
            shipment.id,
            service_request_link=_request(113401),
            actor_bitrix_user_id="6357",
        )
        matching = ExpertiseCase(
            external_id="expertise-matching",
            onec_expertise_number="ЭКС-101",
            linked_customer_order_number="241094",
            current_status="registered",
        )
        no_order = ExpertiseCase(
            external_id="expertise-no-order",
            onec_expertise_number="ЭКС-102",
            current_status="registered",
        )
        mismatch = ExpertiseCase(
            external_id="expertise-mismatch",
            onec_expertise_number="ЭКС-103",
            linked_customer_order_number="999999",
            current_status="registered",
        )
        db.add_all([matching, no_order, mismatch])
        db.commit()

        matching = update_expertise_service_request_link(
            db,
            matching.id,
            service_request_link=_request(113401),
            actor_bitrix_user_id="6357",
        )
        no_order = update_expertise_service_request_link(
            db,
            no_order.id,
            service_request_link=_request(113401, order_ref=None),
            actor_bitrix_user_id="6357",
        )
        with pytest.raises(CustomerReturnConflict, match="different order numbers"):
            update_expertise_service_request_link(
                db,
                mismatch.id,
                service_request_link=_request(113401, order_ref=None),
                actor_bitrix_user_id="6357",
            )

        assert matching.service_request_item_id == 113401
        assert no_order.service_request_item_id == 113401
        db.expire_all()
        assert db.get(ExpertiseCase, mismatch.id).service_request_item_id is None
        events = list(
            db.scalars(
                select(ExpertiseCaseEvent).order_by(
                    ExpertiseCaseEvent.expertise_case_id,
                    ExpertiseCaseEvent.id,
                )
            )
        )
        assert len(events) == 2
        assert events[0].actor_external_id == "6357"
        assert events[0].meta["old_service_request_item_id"] is None
        assert events[0].meta["new_service_request_item_id"] == 113401
        assert events[1].meta["order_match_checked"] is False

        shipment = attach_expertise_cases(db, shipment)
        assert {item.id for item in shipment.expertise_cases} == {matching.id, no_order.id}

        matching = update_expertise_service_request_link(
            db,
            matching.id,
            service_request_link=None,
            actor_bitrix_user_id="6357",
        )
        assert matching.service_request_item_id is None
        assert matching.events[0].meta["new_service_request_item_id"] is None

    engine.dispose()
