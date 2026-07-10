from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import func, select

import app.services.bitrix_order_formation as bitrix_order_service
from app.core.config import Settings
from app.models.procurement_order_formation import (
    ProcurementLifecycleTransitionProposal,
    ProcurementOrderFormationEvent,
)
from app.services.assortment_lifecycle_classification_store import (
    ASSORTMENT_LIFECYCLE_METADATA,
    build_classification_rows,
    persist_classification_rows,
)
from app.services.bitrix_order_formation import BitrixCatalogProduct
from app.services.bitrix_procurement_order_formation_auth import (
    ProcurementOrderFormationSession,
)
from app.services.procurement_order_formation_workspace import (
    LIFECYCLE_ORDER,
    approve_lifecycle_transitions,
    build_dashboard,
    list_lifecycle_transitions,
    sync_lifecycle_transition_proposals,
)

DISPLAY_GUID = "2685293e-967c-11e1-bdb9-0025901e48ef"


@pytest.fixture()
def lifecycle_db(sqlite_engine, db_session):
    ASSORTMENT_LIFECYCLE_METADATA.create_all(sqlite_engine)
    try:
        yield db_session
    finally:
        db_session.rollback()
        ASSORTMENT_LIFECYCLE_METADATA.drop_all(sqlite_engine)


def _session(user_id: str, name: str) -> ProcurementOrderFormationSession:
    return ProcurementOrderFormationSession(
        actor=f"bitrix:member:{user_id}",
        domain="crm.example.test",
        member_id="member",
        user_id=user_id,
        expires_at=datetime.now(UTC),
        user_name=name,
    )


def _settings() -> Settings:
    return Settings(
        procurement_order_formation_lifecycle_approver_user_ids=["130757", "4241"],
        procurement_order_formation_display_responsible_user_id="130757",
        procurement_order_formation_property_apply_enabled=False,
    )


def _seed_lifecycle(sqlite_engine) -> int:
    now = datetime(2026, 7, 10, 9, 20)
    records = [
        {
            "nomenclature_code": "FRUIT-1",
            "product_ref": DISPLAY_GUID.replace("-", ""),
            "manual_status": "fruit",
            "created_at": date(2026, 7, 1).isoformat(),
            "first_supplier_order_at": date(2026, 7, 10).isoformat(),
        },
        {
            "nomenclature_code": "NEWBORN-NEED-1",
            "product_ref": DISPLAY_GUID,
            "created_at": date(2026, 7, 1).isoformat(),
            "first_supplier_order_at": date(2026, 7, 8).isoformat(),
            "has_need_signal": True,
        },
        {
            "nomenclature_code": "NEWBORN-1",
            "product_ref": DISPLAY_GUID,
            "manual_status": "newborn",
        },
        {
            "nomenclature_code": "WORKING-1",
            "product_ref": DISPLAY_GUID,
            "manual_status": "working",
        },
    ]
    summaries = [
        {
            "nomenclature_code": "FRUIT-1",
            "name": "Дисплей Fruit",
            "folder": "дисплеи",
            "status": "newborn",
            "status_label": "Новорожденный",
            "recommended_status": "newborn",
            "reason_text": "Создан первый заказ поставщику",
            "export_blockers": ["fact_status_decision_requires_1c_approval"],
        },
        {
            "nomenclature_code": "NEWBORN-NEED-1",
            "name": "Дисплей ДН",
            "folder": "дисплеи",
            "status": "newborn_need",
            "status_label": "ДН / Добор новорождённого",
            "reason_text": "Есть явная потребность",
            "manual_review_required": True,
        },
        {
            "nomenclature_code": "NEWBORN-1",
            "name": "Дисплей к Новинке",
            "folder": "дисплеи",
            "status": "new_item",
            "status_label": "Новинка",
            "recommended_status": "new_item",
            "reason_text": "Первый груз передан перевозчику",
        },
        {
            "nomenclature_code": "WORKING-1",
            "name": "Дисплей рабочий",
            "folder": "дисплеи",
            "status": "working",
            "status_label": "Рабочий",
            "reason_text": "Нужен пересмотр",
            "manual_review_required": True,
        },
    ]
    rows = build_classification_rows(
        records=records,
        summaries=summaries,
        source="test",
        classified_at=now,
    )
    result = persist_classification_rows(
        sqlite_engine,
        rows=rows,
        run_key="display-test-run-1",
        folder="дисплеи",
        source="test",
        started_at=now,
        finished_at=now,
    )
    assert result.run_id is not None
    return result.run_id


def _approval_item(proposal: ProcurementLifecycleTransitionProposal) -> dict[str, object]:
    return {
        "proposal_id": proposal.id,
        "expected_run_id": proposal.run_id,
        "expected_current_status": proposal.current_status,
        "facts_hash": proposal.facts_hash,
    }


def test_dashboard_keeps_lifecycle_order_and_nests_newborn_need(
    lifecycle_db,
    sqlite_engine,
) -> None:
    run_id = _seed_lifecycle(sqlite_engine)
    summary = sync_lifecycle_transition_proposals(
        lifecycle_db,
        run_id=run_id,
        settings=_settings(),
    )

    dashboard = build_dashboard(lifecycle_db, settings=_settings())

    assert [card["status"] for card in dashboard["cards"]] == list(LIFECYCLE_ORDER)
    newborn = next(card for card in dashboard["cards"] if card["status"] == "newborn")
    fruit = next(card for card in dashboard["cards"] if card["status"] == "fruit")
    working = next(card for card in dashboard["cards"] if card["status"] == "working")
    assert newborn["total_count"] == 2
    assert newborn["action_count"] == 2
    assert fruit["action_count"] == 0
    assert working["action_label"] == "На пересмотр"
    assert working["action_count"] == 1
    assert summary == {
        "created": 4,
        "updated": 0,
        "automatic": 1,
        "stale": 0,
        "run_id": run_id,
    }


def test_queue_starts_unselected_and_only_ready_rows_are_selectable(
    lifecycle_db,
    sqlite_engine,
) -> None:
    run_id = _seed_lifecycle(sqlite_engine)
    sync_lifecycle_transition_proposals(
        lifecycle_db,
        run_id=run_id,
        settings=_settings(),
    )

    queue = list_lifecycle_transitions(lifecycle_db, status="fruit", scope="action")
    newborn_queue = list_lifecycle_transitions(
        lifecycle_db,
        status="newborn",
        scope="action",
    )

    assert queue["total"] == 0
    assert newborn_queue["total"] == 2
    assert newborn_queue["ready_count"] == 1
    assert sum(1 for item in newborn_queue["items"] if item["selectable"]) == 1
    assert all("selected" not in item for item in newborn_queue["items"])
    review = next(item for item in newborn_queue["items"] if item["action_kind"] == "review")
    assert review["current_status"] == "newborn"
    assert review["target_status"] is None


def test_first_supplier_order_moves_fruit_to_newborn_without_approval(
    lifecycle_db,
    sqlite_engine,
) -> None:
    run_id = _seed_lifecycle(sqlite_engine)

    sync_lifecycle_transition_proposals(
        lifecycle_db,
        run_id=run_id,
        settings=_settings(),
    )

    proposal = lifecycle_db.scalar(
        select(ProcurementLifecycleTransitionProposal).where(
            ProcurementLifecycleTransitionProposal.nomenclature_code == "FRUIT-1"
        )
    )
    assert proposal is not None
    assert proposal.current_status == "fruit"
    assert proposal.target_status == "newborn"
    assert proposal.status == "auto_applied"
    assert proposal.approved_by_actor == "system:onec-facts"


def test_batch_approval_returns_partial_result_and_is_idempotent(
    lifecycle_db,
    sqlite_engine,
) -> None:
    run_id = _seed_lifecycle(sqlite_engine)
    sync_lifecycle_transition_proposals(
        lifecycle_db,
        run_id=run_id,
        settings=_settings(),
    )
    proposals = lifecycle_db.scalars(
        select(ProcurementLifecycleTransitionProposal).order_by(
            ProcurementLifecycleTransitionProposal.id
        )
    ).all()
    ready = next(
        item
        for item in proposals
        if item.action_kind == "transition" and item.status == "pending"
    )
    review = next(
        item
        for item in proposals
        if item.action_kind == "review" and item.status == "pending"
    )
    request_items = [_approval_item(ready), _approval_item(review)]

    first = approve_lifecycle_transitions(
        lifecycle_db,
        items=request_items,
        idempotency_key="test-lifecycle-batch-1",
        session=_session("4241", "Эльдар"),
        settings=_settings(),
    )
    second = approve_lifecycle_transitions(
        lifecycle_db,
        items=request_items,
        idempotency_key="test-lifecycle-batch-1",
        session=_session("4241", "Эльдар"),
        settings=_settings(),
    )

    assert first["mode"] == "dry_run"
    assert first["summary"]["approved"] == 1
    assert first["summary"]["blocked"] == 1
    assert second == first
    assert lifecycle_db.scalar(select(func.count(ProcurementOrderFormationEvent.id))) == 1


def test_sale_to_working_is_omar_only(lifecycle_db, sqlite_engine) -> None:
    run_id = _seed_lifecycle(sqlite_engine)
    proposal = ProcurementLifecycleTransitionProposal(
        nomenclature_code="SALE-1",
        nomenclature_ref=DISPLAY_GUID,
        product_guid=DISPLAY_GUID,
        product_name="Дисплей к рабочему",
        folder="Дисплеи",
        action_kind="transition",
        current_status="sale",
        target_status="working",
        status="pending",
        reason="Подтверждение ответственного",
        facts={},
        blockers=[],
        risk_codes=[],
        run_id=run_id,
        run_key="display-test-run-1",
        facts_hash="a" * 64,
        idempotency_key="sale-to-working-test",
        responsible_bitrix_user_id="130757",
        responsible_name="Омар",
    )
    lifecycle_db.add(proposal)
    lifecycle_db.commit()

    eldar_result = approve_lifecycle_transitions(
        lifecycle_db,
        items=[_approval_item(proposal)],
        idempotency_key="sale-to-working-eldar",
        session=_session("4241", "Эльдар"),
        settings=_settings(),
    )
    assert eldar_result["summary"]["blocked"] == 1

    omar_result = approve_lifecycle_transitions(
        lifecycle_db,
        items=[_approval_item(proposal)],
        idempotency_key="sale-to-working-omar",
        session=_session("130757", "Омар"),
        settings=_settings(),
    )
    assert omar_result["summary"]["approved"] == 1


def test_batch_limit_is_100(lifecycle_db) -> None:
    with pytest.raises(ValueError, match="1..100"):
        approve_lifecycle_transitions(
            lifecycle_db,
            items=[{"proposal_id": index} for index in range(101)],
            idempotency_key="too-large-batch",
            session=_session("130757", "Омар"),
            settings=_settings(),
        )


def test_commerceml_readback_reflects_lifecycle_transition(
    lifecycle_db,
    monkeypatch,
) -> None:
    proposal = ProcurementLifecycleTransitionProposal(
        nomenclature_code="SALE-READBACK",
        nomenclature_ref=DISPLAY_GUID,
        product_guid=DISPLAY_GUID,
        product_name="Дисплей readback",
        folder="дисплеи",
        action_kind="transition",
        current_status="sale",
        target_status="working",
        status="applied",
        reason="Подтверждено",
        facts={},
        blockers=[],
        risk_codes=[],
        run_id=1,
        run_key="readback-run",
        facts_hash="b" * 64,
        idempotency_key="readback-transition-test",
    )
    lifecycle_db.add(proposal)
    lifecycle_db.commit()
    monkeypatch.setattr(bitrix_order_service, "load_order_formation_mapping", lambda _settings: {})
    monkeypatch.setattr(
        bitrix_order_service,
        "resolve_catalog_product_by_xml_id",
        lambda *_args, **_kwargs: BitrixCatalogProduct(
            product_id="1646",
            name="Дисплей readback",
            xml_id=DISPLAY_GUID,
            assortment_status="Рабочий",
        ),
    )

    summary = bitrix_order_service.reflect_classifications_from_bitrix(
        lifecycle_db,
        settings=Settings(),
    )

    lifecycle_db.refresh(proposal)
    assert summary == {"reflected": 1, "pending": 0, "missing": 0}
    assert proposal.status == "reflected"
    assert proposal.bitrix_readback_value == "Рабочий"
