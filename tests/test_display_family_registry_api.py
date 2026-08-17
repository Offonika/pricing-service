from __future__ import annotations

import hashlib
from collections.abc import Generator
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api import matching as matching_api
from app.api.dependencies import get_db
from app.main import app
from app.models import (
    DisplayFamily,
    DisplayFamilyDecisionEvent,
    DisplayFamilyMember,
    DisplayFamilyRegistryVersion,
    Product,
)


@pytest.fixture()
def display_family_client(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> Generator[TestClient, None, None]:
    monkeypatch.setattr(matching_api.settings, "api_basic_user", "api")
    monkeypatch.setattr(matching_api.settings, "api_basic_password", "secret")

    def override_db() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def _auth() -> tuple[str, str]:
    return ("api", "secret")


def _seed(session: Session) -> DisplayFamily:
    product = Product(
        article="DISPLAY-API-1",
        code_1c="CODE-API-1",
        name="Дисплей для Apple iPhone API",
    )
    session.add(product)
    session.flush()
    version = DisplayFamilyRegistryVersion(
        version_number=1,
        status="active",
        effective_from=date(2026, 8, 16),
        source_schema="display_family_registry_preflight_manifest.v2",
        source_bundle_path="/accepted",
        inventory_checksum="a" * 64,
        membership_checksum="b" * 64,
        inventory_sha256="c" * 64,
        inventory_csv_sha256="d" * 64,
        report_sha256="e" * 64,
        source_quality_checksum="f" * 64,
        expected_family_count=1,
        expected_member_count=1,
        actual_family_count=1,
        actual_member_count=1,
        source_manifest_json={},
        source_summary_json={
            "warning_counts": {"accepted_matching_review": 1},
            "status_counts": {"singleton_exact_signature": 1},
        },
        evidence_snapshot_json={},
        created_by="bootstrap-user",
    )
    session.add(version)
    session.flush()
    family = DisplayFamily(
        registry_version_id=version.id,
        family_key="display-family-api-test",
        member_count=1,
        is_singleton=True,
        total_current_stock_qty=7,
        review_member_count=1,
        matching_review_member_count=1,
        quality_unknown_member_count=1,
        construction_unknown_member_count=0,
        phone_model_ids_json=[100],
        phone_models_json=[
            {"id": 100, "brand": "apple", "model_name": "iphone api", "variant": None}
        ],
        physical_model_signatures_json=[["phone-model:100"]],
        segment_ids_json=["unknown|soft_oled|without_frame|ic_pad_unknown"],
        warning_codes_json=["accepted_matching_review", "quality_unknown"],
        note_codes_json=[],
        evidence_snapshot_json={"proposal_status_counts": {"singleton": 1}},
    )
    session.add(family)
    session.flush()
    session.add(
        DisplayFamilyMember(
            registry_version_id=version.id,
            family_id=family.id,
            product_id=product.id,
            segment_id="unknown|soft_oled|without_frame|ic_pad_unknown",
            proposal_status="singleton_exact_signature",
            quality_segment="unknown",
            construction_segment="soft_oled",
            requires_manual_review=True,
            current_stock_qty=7,
            warning_codes_json=["quality_unknown"],
            note_codes_json=[],
            scope_reasons_json=["active_catalog", "current_stock"],
            product_snapshot_json={
                "article": product.article,
                "nomenclature_code": product.code_1c,
                "name": product.name,
                "last_sale_at": "2026-08-15",
            },
            matching_evidence_json={
                "accepted_count": 1,
                "requires_review": True,
                "warnings": ["accepted_matching_review"],
            },
            identity_evidence_json={"schema": "display_identity.v1"},
            evidence_snapshot_json={"product_id": product.id},
        )
    )
    session.add(
        DisplayFamilyDecisionEvent(
            registry_version_id=version.id,
            action="bootstrap_activate",
            actor="bootstrap-user",
            reason="accepted bundle",
            effective_at=date(2026, 8, 16),
            evidence_snapshot_json={"checksum": "a" * 64},
        )
    )
    session.commit()
    return family


def test_display_family_registry_api_is_authenticated_and_read_only(
    display_family_client: TestClient, db_session: Session
) -> None:
    family = _seed(db_session)
    before_hash = hashlib.sha256(
        repr((family.family_key, family.member_count, family.total_current_stock_qty)).encode()
    ).hexdigest()

    unauthorized = display_family_client.get("/api/matching/compatibility/display-families/summary")
    assert unauthorized.status_code == 401

    summary = display_family_client.get(
        "/api/matching/compatibility/display-families/summary", auth=_auth()
    )
    assert summary.status_code == 200
    assert summary.json()["active_version"]["version_number"] == 1
    assert summary.json()["family_count"] == 1
    assert summary.json()["matching_review_member_count"] == 1

    listing = display_family_client.get(
        "/api/matching/compatibility/display-families",
        params={"search": "iPhone API", "matching_review": "true"},
        auth=_auth(),
    )
    assert listing.status_code == 200
    assert listing.json()["total"] == 1
    assert listing.json()["items"][0]["family_key"] == family.family_key

    detail = display_family_client.get(
        f"/api/matching/compatibility/display-families/{family.id}", auth=_auth()
    )
    assert detail.status_code == 200
    assert detail.json()["members"][0]["product"]["article"] == "DISPLAY-API-1"
    assert detail.json()["members"][0]["matching_evidence"]["accepted_count"] == 1
    assert detail.json()["events"][0]["action"] == "bootstrap_activate"

    versions = display_family_client.get(
        "/api/matching/compatibility/display-families/versions", auth=_auth()
    )
    assert versions.status_code == 200
    assert versions.json()[0]["inventory_checksum"] == "a" * 64

    db_session.refresh(family)
    after_hash = hashlib.sha256(
        repr((family.family_key, family.member_count, family.total_current_stock_qty)).encode()
    ).hexdigest()
    assert after_hash == before_hash


def test_display_family_detail_rejects_inactive_or_unknown_family(
    display_family_client: TestClient, db_session: Session
) -> None:
    _seed(db_session)
    response = display_family_client.get(
        "/api/matching/compatibility/display-families/999999", auth=_auth()
    )
    assert response.status_code == 404
