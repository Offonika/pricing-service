from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.core.config import Settings
from app.main import app
from app.schemas.procurement_order_formation import (
    ProcurementLifecycleTransitionApprovalRequest,
)
from app.services.bitrix_procurement_labels_auth import (
    create_procurement_labels_session_token,
)
from app.services.bitrix_procurement_order_formation_auth import (
    create_procurement_order_formation_session_token,
    verify_procurement_order_formation_session_token,
)


def _settings() -> Settings:
    return Settings(
        procurement_order_formation_bitrix_enabled=True,
        procurement_order_formation_bitrix_allowed_domains=["crm.example.test"],
        procurement_order_formation_bitrix_allowed_member_ids=["member-1"],
        procurement_order_formation_bitrix_allowed_user_ids=["115204", "130757", "4241"],
        procurement_order_formation_bitrix_session_secret="formation-secret-for-tests",
        procurement_labels_bitrix_enabled=True,
        procurement_labels_bitrix_allowed_domains=["crm.example.test"],
        procurement_labels_bitrix_allowed_member_ids=["member-1"],
        procurement_labels_bitrix_allowed_user_ids=["115204"],
        procurement_labels_bitrix_session_secret="formation-secret-for-tests",
    )


def test_order_formation_oauth_session_has_dedicated_scope() -> None:
    settings = _settings()
    token, _expires_at = create_procurement_order_formation_session_token(
        domain="crm.example.test",
        member_id="member-1",
        user_id="130757",
        user_name="Омар",
        settings=settings,
        now=1000,
    )

    session = verify_procurement_order_formation_session_token(
        token,
        settings=settings,
        now=1001,
    )

    assert session.user_id == "130757"
    assert session.user_name == "Омар"


def test_labels_oauth_token_cannot_open_order_formation() -> None:
    settings = _settings()
    token, _expires_at = create_procurement_labels_session_token(
        domain="crm.example.test",
        member_id="member-1",
        user_id="115204",
        user_name="Арсений",
        settings=settings,
        now=1000,
    )

    with pytest.raises(HTTPException) as exc_info:
        verify_procurement_order_formation_session_token(
            token,
            settings=settings,
            now=1001,
        )

    assert exc_info.value.status_code == 401


def test_send_to_onec_endpoint_has_no_browser_apply_field() -> None:
    operation = app.openapi()["paths"][
        "/api/procurement-order-formation/orders/{order_id}/send-to-1c"
    ]["post"]

    assert "requestBody" not in operation


def test_order_excel_export_is_exposed_as_xlsx() -> None:
    operation = app.openapi()["paths"]["/api/procurement-order-formation/orders/export.xlsx"]["get"]

    assert (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        in operation["responses"]["200"]["content"]
    )


def test_lifecycle_approval_schema_limits_batch_to_100() -> None:
    item = {
        "proposal_id": 1,
        "expected_run_id": 361,
        "expected_current_status": "fruit",
        "facts_hash": "a" * 64,
    }

    with pytest.raises(ValueError):
        ProcurementLifecycleTransitionApprovalRequest(
            idempotency_key="batch-too-large",
            items=[item] * 101,
        )


def test_order_assistant_line_schema_exposes_catalog_card_and_photo_source() -> None:
    schema = app.openapi()["components"]["schemas"]["ProcurementOrderFormationLineRead"]

    assert "product_card_url" in schema["properties"]
    assert "photo_source" in schema["properties"]
