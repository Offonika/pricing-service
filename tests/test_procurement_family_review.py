from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select

from app.core.config import Settings
from app.models.display_family_registry import (
    DisplayFamily,
    DisplayFamilyDecisionEvent,
    DisplayFamilyRegistryVersion,
)
from app.services import procurement_family_review as review_service
from app.services.procurement_family_review import (
    build_family_review_card,
    save_family_review_decision,
)
from app.services.procurement_order_formation import VersionConflictError


def _seed_registry(db_session):
    version = DisplayFamilyRegistryVersion(
        version_number=7,
        status="active",
        effective_from=date(2026, 9, 4),
        source_schema="test.v1",
        source_bundle_path="test",
        inventory_checksum="a" * 64,
        membership_checksum="b" * 64,
        inventory_sha256="c" * 64,
        inventory_csv_sha256="d" * 64,
        report_sha256="e" * 64,
        source_quality_checksum="f" * 64,
        expected_family_count=1,
        expected_member_count=2,
        actual_family_count=1,
        actual_member_count=2,
        source_manifest_json={},
        source_summary_json={},
        evidence_snapshot_json={},
        created_by="test",
    )
    db_session.add(version)
    db_session.flush()
    family = DisplayFamily(
        registry_version_id=version.id,
        family_key="family-a",
        member_count=2,
        is_singleton=False,
        total_current_stock_qty=1,
        review_member_count=1,
        matching_review_member_count=0,
        quality_unknown_member_count=0,
        construction_unknown_member_count=0,
        phone_model_ids_json=[],
        phone_models_json=[],
        physical_model_signatures_json=[],
        segment_ids_json=[],
        warning_codes_json=[],
        note_codes_json=[],
        evidence_snapshot_json={},
    )
    db_session.add(family)
    db_session.commit()
    return version, family


def _snapshot(version, family, *, sales="10"):
    member_card = {
        "identity": {
            "bitrix_product_id": "1",
            "xml_id": "",
            "nomenclature_code": "A",
            "name": "Основной товар",
            "article": "",
        },
        "properties": {},
        "lifecycle": {},
        "demand": {"sales_30": sales, "recommended_order": "2"},
        "quality": {},
        "supply": {},
        "family": {},
        "blockers": [{"code": "review"}],
        "orders": [],
        "recommendation": "Проверить",
        "source": {"state": "ready", "calculated_at": "2026-09-04"},
    }
    return {
        **member_card,
        "family": {
            "id": family.family_key,
            "record_id": family.id,
            "registry_version_id": version.id,
            "registry_version_number": version.version_number,
            "registry_inventory_checksum": version.inventory_checksum,
            "member_codes": ["A", "B"],
            "member_count": 2,
            "comparison_members": [
                {
                    "role": "primary",
                    "role_label": "Основная карточка",
                    "rank": 0,
                    "speed_score": "1",
                    "card": member_card,
                }
            ],
        },
    }


def test_quality_and_distribution_are_independent_idempotent_and_close_blocker(
    db_session,
    monkeypatch,
) -> None:
    version, family = _seed_registry(db_session)
    snapshot = _snapshot(version, family)
    monkeypatch.setattr(
        review_service,
        "build_product_card_review_snapshot",
        lambda *_args, **_kwargs: snapshot,
    )
    settings = Settings(procurement_family_review_decisions_enabled=True)
    card = build_family_review_card(db_session, nomenclature_code="A", settings=settings)

    quality = save_family_review_decision(
        db_session,
        nomenclature_code="A",
        kind="quality",
        expected_facts_hash=card["facts_hash"],
        expected_registry_version_number=7,
        decision={
            "result": "false_positive",
            "root_cause": "Возврат не связан с качеством",
            "checked_documents": ["ВР-1"],
            "comment": "Проверено",
        },
        actor="bitrix:42:Сергей",
        settings=settings,
    )
    db_session.commit()
    assert quality["blocker_ready"] is False
    assert quality["decisions"]["quality"]["decision"]["result"] == "false_positive"
    assert quality["decisions"]["distribution"] is None

    repeated = save_family_review_decision(
        db_session,
        nomenclature_code="A",
        kind="quality",
        expected_facts_hash=card["facts_hash"],
        expected_registry_version_number=7,
        decision={
            "result": "false_positive",
            "root_cause": "Возврат не связан с качеством",
            "checked_documents": ["ВР-1"],
            "comment": "Проверено",
        },
        actor="bitrix:42:Сергей",
        settings=settings,
    )
    assert repeated["idempotent"] is True
    assert db_session.scalar(select(func.count(DisplayFamilyDecisionEvent.id))) == 1

    distribution = save_family_review_decision(
        db_session,
        nomenclature_code="A",
        kind="distribution",
        expected_facts_hash=card["facts_hash"],
        expected_registry_version_number=7,
        decision={"quantities": {"A": 2, "B": 0}, "rationale": "Спрос", "comment": ""},
        actor="bitrix:43:Омар",
        settings=settings,
    )
    db_session.commit()
    assert distribution["blocker_ready"] is True
    assert distribution["decisions"]["quality"] is not None
    assert distribution["decisions"]["distribution"] is not None


def test_decision_rejects_changed_facts_and_requires_every_family_member(
    db_session,
    monkeypatch,
) -> None:
    version, family = _seed_registry(db_session)
    current = {"snapshot": _snapshot(version, family)}
    monkeypatch.setattr(
        review_service,
        "build_product_card_review_snapshot",
        lambda *_args, **_kwargs: current["snapshot"],
    )
    settings = Settings(procurement_family_review_decisions_enabled=True)
    card = build_family_review_card(db_session, nomenclature_code="A", settings=settings)

    try:
        save_family_review_decision(
            db_session,
            nomenclature_code="A",
            kind="distribution",
            expected_facts_hash=card["facts_hash"],
            expected_registry_version_number=7,
            decision={"quantities": {"A": 2}, "rationale": "Спрос"},
            actor="bitrix:43:Омар",
            settings=settings,
        )
    except ValueError as exc:
        assert "каждого члена семьи" in str(exc)
    else:
        raise AssertionError("missing zero quantity must be rejected")

    current["snapshot"] = _snapshot(version, family, sales="11")
    try:
        save_family_review_decision(
            db_session,
            nomenclature_code="A",
            kind="quality",
            expected_facts_hash=card["facts_hash"],
            expected_registry_version_number=7,
            decision={"result": "confirmed", "root_cause": "Брак"},
            actor="bitrix:42:Сергей",
            settings=settings,
        )
    except VersionConflictError as exc:
        assert "изменились" in str(exc)
    else:
        raise AssertionError("stale facts must be rejected")


def test_needs_data_quality_decision_does_not_close_blocker(db_session, monkeypatch) -> None:
    version, family = _seed_registry(db_session)
    snapshot = _snapshot(version, family)
    monkeypatch.setattr(
        review_service,
        "build_product_card_review_snapshot",
        lambda *_args, **_kwargs: snapshot,
    )
    settings = Settings(procurement_family_review_decisions_enabled=True)
    card = build_family_review_card(db_session, nomenclature_code="A", settings=settings)
    save_family_review_decision(
        db_session,
        nomenclature_code="A",
        kind="quality",
        expected_facts_hash=card["facts_hash"],
        expected_registry_version_number=7,
        decision={"result": "needs_data", "root_cause": "Нужны документы"},
        actor="bitrix:42:Сергей",
        settings=settings,
    )
    result = save_family_review_decision(
        db_session,
        nomenclature_code="A",
        kind="distribution",
        expected_facts_hash=card["facts_hash"],
        expected_registry_version_number=7,
        decision={"quantities": {"A": 2, "B": 0}, "rationale": "Спрос"},
        actor="bitrix:43:Омар",
        settings=settings,
    )

    assert result["decisions"]["quality"] is not None
    assert result["decisions"]["distribution"] is not None
    assert result["blocker_ready"] is False


def test_facts_snapshot_is_json_safe_before_event_insert(db_session, monkeypatch) -> None:
    version, family = _seed_registry(db_session)
    snapshot = _snapshot(version, family, sales=Decimal("10.25"))
    monkeypatch.setattr(
        review_service,
        "build_product_card_review_snapshot",
        lambda *_args, **_kwargs: snapshot,
    )
    settings = Settings(procurement_family_review_decisions_enabled=True)

    card = build_family_review_card(db_session, nomenclature_code="A", settings=settings)

    json.dumps(card["facts_snapshot"])
    result = save_family_review_decision(
        db_session,
        nomenclature_code="A",
        kind="quality",
        expected_facts_hash=card["facts_hash"],
        expected_registry_version_number=7,
        decision={"result": "needs_data", "root_cause": "Нужны документы"},
        actor="bitrix:42:Сергей",
        settings=settings,
    )
    db_session.flush()

    event = db_session.get(DisplayFamilyDecisionEvent, result["event"]["id"])
    assert event is not None
    json.dumps(event.evidence_snapshot_json)
