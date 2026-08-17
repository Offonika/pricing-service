from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    DisplayFamilyDecisionEvent,
    DisplayFamilyMember,
    DisplayFamilyRegistryVersion,
    Product,
)
from app.services.display_family_registry import (
    REQUIRED_SOURCE_GATES,
    ApprovedBundleContract,
    DisplayFamilyRegistryError,
    active_display_family_registry_version,
    apply_display_family_bootstrap,
    build_display_family_bootstrap_plan,
    display_family_registry_summary,
    get_active_display_family_detail,
    list_active_display_families,
    load_approved_display_family_bundle,
    readback_display_family_registry_version,
    rollback_display_family_registry,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _item(product_id: int, family_key: str, *, review: bool = False) -> dict:
    article = f"DISPLAY-{product_id}"
    return {
        "article": article,
        "available_at_status": "current_snapshot_only",
        "color": "black",
        "construction": "OLED",
        "construction_segment": "soft_oled",
        "current_stock_qty": product_id,
        "display_type": "Soft OLED",
        "has_binding_no_solder": None,
        "has_frame": False,
        "has_ic_pad": None,
        "has_recent_or_open_order": True,
        "identity_evidence": {"schema": "display_identity.v1"},
        "is_active": True,
        "is_marked_for_deletion": False,
        "last_sale_at": "2026-08-15",
        "matching_audit": {
            "accepted_count": 1 if review else 0,
            "manual_accepted_count": 0,
            "requires_review": review,
            "warnings": ["accepted_matching_review"] if review else [],
        },
        "model_keys": ["apple iphone test"],
        "name": f"Дисплей тестовый {product_id}",
        "nomenclature_code": f"CODE-{product_id}",
        "phone_model_ids": [100],
        "phone_models": [
            {
                "id": 100,
                "brand": "apple",
                "model_name": "iphone test",
                "variant": None,
            }
        ],
        "physical_model_signature": ["phone-model:100"],
        "product_id": product_id,
        "proposal_notes": [],
        "proposal_status": "proposed_exact_signature",
        "proposal_warnings": ["accepted_matching_review"] if review else [],
        "proposed_family_id": family_key,
        "quality": "Premium",
        "quality_segment": "premium",
        "related_model_signature": ["apple:iphone_test"],
        "requires_manual_review": review,
        "scope_classification_reason": "explicit_display_module_name",
        "scope_classification_warnings": [],
        "scope_reasons": ["active_catalog", "current_stock"],
        "segment_id": "premium|soft_oled|without_frame|ic_pad_unknown",
    }


def _bundle(tmp_path: Path, items: list[dict]) -> tuple[Path, ApprovedBundleContract]:
    bundle = tmp_path / "accepted-bundle"
    bundle.mkdir()
    family_count = len({item["proposed_family_id"] for item in items})
    checksum = "a" * 64
    inventory = {
        "schema": "display_family_inventory.test.v2",
        "as_of": "2026-08-16",
        "inventory_checksum": checksum,
        "items": items,
        "summary": {
            "included_display_sku_count": len(items),
            "proposed_family_count": family_count,
            "warning_counts": {"accepted_matching_review": 1},
            "status_counts": {"proposed_exact_signature": len(items)},
        },
        "scope": "test",
        "source_quality": {"status": "ready"},
    }
    (bundle / "inventory.json").write_text(
        json.dumps(inventory, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    (bundle / "inventory.csv").write_text("product_id,family\n", encoding="utf-8")
    (bundle / "report.html").write_text("<html>accepted</html>", encoding="utf-8")
    hashes = {
        name: _sha256(bundle / name) for name in ("inventory.json", "inventory.csv", "report.html")
    }
    manifest = {
        "schema": "display_family_registry_preflight_manifest.test.v2",
        "status": "complete_read_only",
        "as_of": "2026-08-16",
        "external_writes": False,
        "production_authorized": False,
        "source_quality_status": "ready",
        "source_quality_checksum": "b" * 64,
        "inventory_checksum": checksum,
        "artifact_sha256": hashes,
        "source_gates": {gate: {"status": "pass"} for gate in REQUIRED_SOURCE_GATES},
    }
    (bundle / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    contract = ApprovedBundleContract(
        bundle_path=bundle,
        as_of=date(2026, 8, 16),
        manifest_schema=manifest["schema"],
        inventory_schema=inventory["schema"],
        inventory_checksum=checksum,
        artifact_sha256=hashes,
        expected_member_count=len(items),
        expected_family_count=family_count,
    )
    return bundle, contract


def _products(session: Session, count: int = 3) -> list[Product]:
    products = [
        Product(article=f"DISPLAY-{index}", name=f"Дисплей тестовый {index}")
        for index in range(1, count + 1)
    ]
    session.add_all(products)
    session.commit()
    return products


def test_bundle_validation_is_fail_closed_on_artifact_change(
    tmp_path: Path, db_session: Session
) -> None:
    products = _products(db_session, 1)
    path, contract = _bundle(tmp_path, [_item(products[0].id, "display-family-test-1")])
    (path / "report.html").write_text("tampered", encoding="utf-8")

    with pytest.raises(DisplayFamilyRegistryError, match="file SHA-256 mismatch"):
        load_approved_display_family_bundle(path, contract=contract)


def test_bootstrap_plan_blocks_missing_product(tmp_path: Path, db_session: Session) -> None:
    path, contract = _bundle(tmp_path, [_item(999999, "display-family-test-1")])
    bundle = load_approved_display_family_bundle(path, contract=contract)

    plan = build_display_family_bootstrap_plan(db_session, bundle)

    assert plan.ready is False
    assert plan.missing_product_ids == (999999,)
    assert plan.blockers == ("bundle_contains_missing_products",)


def test_bootstrap_is_atomic_idempotent_and_queryable(tmp_path: Path, db_session: Session) -> None:
    products = _products(db_session)
    path, contract = _bundle(
        tmp_path,
        [
            _item(products[0].id, "display-family-test-shared"),
            _item(products[1].id, "display-family-test-shared", review=True),
            _item(products[2].id, "display-family-test-singleton"),
        ],
    )
    bundle = load_approved_display_family_bundle(path, contract=contract)
    db_session.rollback()

    result = apply_display_family_bootstrap(
        db_session, bundle, actor="test-user", reason="accepted test evidence"
    )

    assert result["applied"] is True
    assert result["readback"]["ok"] is True
    assert result["readback"]["family_count"] == 2
    assert result["readback"]["member_count"] == 3
    version = active_display_family_registry_version(db_session)
    assert version is not None
    assert version.created_by == "test-user"
    assert db_session.scalar(select(DisplayFamilyDecisionEvent)).action == "bootstrap_activate"
    assert len(db_session.scalars(select(DisplayFamilyMember)).all()) == 3

    summary = display_family_registry_summary(db_session)
    assert summary["family_count"] == 2
    assert summary["singleton_family_count"] == 1
    assert summary["matching_review_member_count"] == 1
    listing = list_active_display_families(db_session, page=1, page_size=20, matching_review=True)
    assert listing["total"] == 1
    assert listing["items"][0]["family_key"] == "display-family-test-shared"
    detail = get_active_display_family_detail(db_session, listing["items"][0]["id"])
    assert detail is not None
    assert len(detail["members"]) == 2
    assert detail["events"][0]["reason"] == "accepted test evidence"

    db_session.commit()
    idempotent = apply_display_family_bootstrap(db_session, bundle, actor="second-run")
    assert idempotent["applied"] is False
    assert idempotent["idempotent"] is True
    assert len(db_session.scalars(select(DisplayFamilyRegistryVersion)).all()) == 1


def test_readback_detects_membership_drift(tmp_path: Path, db_session: Session) -> None:
    products = _products(db_session, 1)
    path, contract = _bundle(tmp_path, [_item(products[0].id, "display-family-test-1")])
    bundle = load_approved_display_family_bundle(path, contract=contract)
    db_session.rollback()
    apply_display_family_bootstrap(db_session, bundle, actor="test-user")
    version = active_display_family_registry_version(db_session)
    member = db_session.scalar(select(DisplayFamilyMember))
    assert version is not None and member is not None
    member.segment_id = "changed"
    db_session.flush()

    readback = readback_display_family_registry_version(db_session, version)

    assert readback["ok"] is False
    assert "membership_checksum_mismatch" in readback["errors"]


def test_rollback_switches_active_version_without_deleting_history(db_session: Session) -> None:
    empty_membership_checksum = hashlib.sha256(b"").hexdigest()
    first = DisplayFamilyRegistryVersion(
        version_number=1,
        status="superseded",
        effective_from=date(2026, 8, 1),
        source_schema="test",
        source_bundle_path="/test/v1",
        inventory_checksum="1" * 64,
        membership_checksum=empty_membership_checksum,
        inventory_sha256="1" * 64,
        inventory_csv_sha256="1" * 64,
        report_sha256="1" * 64,
        source_quality_checksum="1" * 64,
        expected_family_count=0,
        expected_member_count=0,
        actual_family_count=0,
        actual_member_count=0,
        source_manifest_json={},
        source_summary_json={},
        evidence_snapshot_json={},
        created_by="test",
    )
    second = DisplayFamilyRegistryVersion(
        version_number=2,
        status="active",
        effective_from=date(2026, 8, 16),
        source_schema="test",
        source_bundle_path="/test/v2",
        inventory_checksum="2" * 64,
        membership_checksum=empty_membership_checksum,
        inventory_sha256="2" * 64,
        inventory_csv_sha256="2" * 64,
        report_sha256="2" * 64,
        source_quality_checksum="2" * 64,
        expected_family_count=0,
        expected_member_count=0,
        actual_family_count=0,
        actual_member_count=0,
        source_manifest_json={},
        source_summary_json={},
        evidence_snapshot_json={},
        created_by="test",
    )
    db_session.add_all([first, second])
    db_session.commit()

    result = rollback_display_family_registry(
        db_session,
        1,
        actor="rollback-user",
        reason="validated rollback",
        effective_at=date(2026, 8, 16),
    )

    assert result["readback"]["status"] == "active"
    versions = db_session.scalars(
        select(DisplayFamilyRegistryVersion).order_by(DisplayFamilyRegistryVersion.version_number)
    ).all()
    assert [(row.version_number, row.status) for row in versions] == [
        (1, "active"),
        (2, "rolled_back"),
    ]
    assert db_session.scalar(select(DisplayFamilyDecisionEvent)).action == "rollback_activate"
    assert len(versions) == 2
