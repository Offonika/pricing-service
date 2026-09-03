from __future__ import annotations

from datetime import UTC, date, datetime
from io import BytesIO
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from fastapi import HTTPException

import app.api.procurement_order_formation as api_module
from app.api.procurement_order_formation import _content_disposition
from app.core.config import Settings
from app.main import app
from app.schemas.procurement_order_formation import (
    ProcurementLifecycleTransitionApprovalRequest,
    ProcurementOrderLabelSourceLinkRequest,
)
from app.services.bitrix_procurement_labels_auth import (
    create_procurement_labels_session_token,
)
from app.services.bitrix_procurement_order_formation_auth import (
    ProcurementOrderFormationSession,
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


def test_order_label_exports_are_exposed_in_existing_order_application() -> None:
    paths = app.openapi()["paths"]

    assert "/api/procurement-order-formation/orders/{order_id}/labels/source" in paths
    assert "/api/procurement-order-formation/orders/{order_id}/labels/preview" in paths
    assert "/api/procurement-order-formation/orders/{order_id}/labels.pdf" in paths
    assert "/api/procurement-order-formation/orders/{order_id}/labels.xlsx" in paths
    pdf_parameters = paths["/api/procurement-order-formation/orders/{order_id}/labels.pdf"]["get"][
        "parameters"
    ]
    checksum = next(item for item in pdf_parameters if item["name"] == "source_checksum")
    assert checksum["required"] is True


def test_order_label_content_disposition_supports_russian_onec_number() -> None:
    disposition = _content_disposition(
        order_id=14,
        onec_number="РБГУ0000543",
        size="50x40",
        format_="pdf",
    )

    disposition.encode("latin-1")
    assert 'filename="supplier-order-14-labels-50x40.pdf"' in disposition
    assert "filename*=UTF-8''supplier-order-%D0%A0%D0%91%D0%93%D0%A3" in disposition


def test_manual_label_source_endpoint_commits_audit_event(monkeypatch) -> None:
    class FakeDb:
        committed = False
        rolled_back = False

        def commit(self) -> None:
            self.committed = True

        def rollback(self) -> None:
            self.rolled_back = True

    db = FakeDb()
    recorded: dict = {}
    source = {
        "origin": "manual",
        "onec_number": "РБГУ0000543",
        "onec_date": date(2026, 8, 3),
        "linked_at": datetime(2026, 8, 31, 12, 0),
    }
    preview = {
        "order_id": 14,
        "onec_number": "РБГУ0000543",
        "onec_date": date(2026, 8, 3),
        "label_size": "50x40",
        "source_checksum": "a" * 64,
        "max_page_count": 1000,
        "position_count": 1,
        "product_label_count": 1,
        "separator_count": 0,
        "total_page_count": 1,
        "export_file_count": 1,
        "ready": True,
        "blockers": [],
        "rows": [
            {
                "line_no": 1,
                "onec_item_code": "062852",
                "item_name": "Дисплей",
                "article_1c": "062852",
                "barcode": "2900000636873",
                "quantity": 1,
            }
        ],
    }
    monkeypatch.setattr(api_module, "get_order", lambda _db, order_id: SimpleNamespace(id=order_id))
    monkeypatch.setattr(api_module, "serialize_order_label_source", lambda _order: None)
    monkeypatch.setattr(
        api_module,
        "link_order_label_source",
        lambda *_args, **_kwargs: (source, preview),
    )
    monkeypatch.setattr(
        api_module,
        "record_event",
        lambda *_args, **kwargs: recorded.update(kwargs),
    )
    session = ProcurementOrderFormationSession(
        actor="bitrix:member:115204",
        domain="crm.example.test",
        member_id="member-1",
        user_id="115204",
        expires_at=datetime.now(UTC),
        user_name="Арсений",
    )

    result = api_module.attach_order_label_source(
        14,
        ProcurementOrderLabelSourceLinkRequest(onec_number="РБГУ0000543", label_size="50x40"),
        db,
        session,
    )

    assert db.committed is True
    assert db.rolled_back is False
    assert recorded["event_type"] == "label_source_linked"
    assert recorded["after"] == source
    assert result.preview.source_checksum == "a" * 64


def test_large_label_download_returns_zip_with_part_files(monkeypatch) -> None:
    from app.services.procurement_order_labels import build_preview_from_rows

    preview = build_preview_from_rows(
        order_id=14,
        onec_number="РБГУ0000590",
        onec_date=date(2026, 8, 31),
        label_size="50x40",
        rows=[
            {
                "line_no": 1,
                "onec_item_code": "0001",
                "item_name": "Товар",
                "article_1c": "A-1",
                "barcode": "460000000001",
                "quantity": 4,
            }
        ],
        max_page_count=3,
    )
    monkeypatch.setattr(
        api_module,
        "build_order_label_preview",
        lambda *_args, **_kwargs: preview,
    )

    response = api_module._order_label_download(
        14,
        "50x40",
        "pdf",
        preview["source_checksum"],
        object(),
    )

    assert response.headers["content-type"] == "application/zip"
    assert "supplier-order-14-labels-50x40-pdf.zip" in response.headers["content-disposition"]
    with ZipFile(BytesIO(response.body)) as archive:
        assert len(archive.namelist()) == 2
        assert archive.namelist()[0].endswith("part-01-of-02.pdf")


def test_native_product_card_insights_endpoints_are_exposed() -> None:
    openapi = app.openapi()
    paths = openapi["paths"]

    assert "get" in paths["/api/procurement-order-formation/products/{product_id}/card"]
    assert "get" in paths["/api/procurement-order-formation/products/by-xml/{xml_id}/card"]
    schema = openapi["components"]["schemas"]["ProcurementProductCardRead"]
    assert {
        "identity",
        "properties",
        "lifecycle",
        "demand",
        "quality",
        "supply",
        "family",
        "blockers",
        "orders",
        "source",
    }.issubset(schema["properties"])


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


def test_order_assistant_openapi_exposes_metric_evidence_and_supplier_profile_version() -> None:
    schema = app.openapi()["components"]["schemas"]["ProcurementOrderFormationLineRead"]
    properties = schema["properties"]
    for field_name in (
        "metrics_as_of",
        "metrics_window_days",
        "profitability_calculation_basis",
        "profitability_status",
        "product_defect_pct",
        "supplier_defect_pct",
        "supplier_defect_attribution",
        "price_history_expected_currency",
        "price_history_available_currencies",
        "supplier_prepare_days",
        "logistics_days",
        "lead_time_days",
        "lead_time_source_level",
        "lead_time_confidence",
        "supplier_selection_rule",
        "supplier_selection_reason",
        "supplier_cost_tie_pct",
        "supplier_price_candidate_count",
        "supplier_price_min",
        "supplier_selected_purchase_price",
        "supplier_selected_price_currency",
    ):
        assert field_name in properties

    profile = app.openapi()["components"]["schemas"]["ProcurementSupplierProfileRead"]
    assert "version" in profile["properties"]
    assert "terms_status" in profile["properties"]
    assert "can_edit" in profile["properties"]


def test_supplier_profile_and_classification_rejection_endpoints_are_versioned() -> None:
    paths = app.openapi()["paths"]
    profile_path = "/api/procurement-order-formation/suppliers/{supplier_ref}/profile"
    reject_path = (
        "/api/procurement-order-formation/orders/{order_id}/lines/{line_id}/"
        "classification/{proposal_id}/reject"
    )
    assert {"get", "patch"}.issubset(paths[profile_path])
    assert "post" in paths[reject_path]

    update_schema = app.openapi()["components"]["schemas"][
        "ProcurementSupplierProfileUpdateRequest"
    ]
    reject_schema = app.openapi()["components"]["schemas"]["ProcurementClassificationRejectRequest"]
    assert "expected_version" in update_schema["required"]
    assert {"expected_order_version", "expected_line_version", "reason"}.issubset(
        reject_schema["required"]
    )


def test_supplier_review_room_endpoints_are_exposed() -> None:
    openapi = app.openapi()
    paths = openapi["paths"]
    schemas = openapi["components"]["schemas"]

    assert "get" in paths["/api/procurement-order-formation/suppliers/options"]
    assert (
        "patch"
        in paths["/api/procurement-order-formation/orders/{order_id}/lines/{line_id}/main-supplier"]
    )
    assert (
        "post"
        in paths[
            "/api/procurement-order-formation/orders/{order_id}/distribute-by-suppliers/preview"
        ]
    )
    assert (
        "post"
        in paths["/api/procurement-order-formation/orders/{order_id}/distribute-by-suppliers"]
    )
    assert {"expected_order_version", "expected_line_version", "supplier_ref"}.issubset(
        schemas["ProcurementLineSupplierSelectionRequest"]["required"]
    )


def test_order_resolution_contract_is_exposed_in_openapi() -> None:
    openapi = app.openapi()
    paths = openapi["paths"]
    schemas = openapi["components"]["schemas"]

    manual_path = (
        "/api/procurement-order-formation/lifecycle/transitions/{proposal_id}/manual-decision"
    )
    matching_path = (
        "/api/procurement-order-formation/orders/{order_id}/lines/{line_id}/"
        "matching-review/confirm"
    )
    assert "post" in paths[manual_path]
    assert "post" in paths[matching_path]

    order_properties = schemas["ProcurementOrderFormationRead"]["properties"]
    line_properties = schemas["ProcurementOrderFormationLineRead"]["properties"]
    lifecycle_properties = schemas["ProcurementLifecycleTransitionRead"]["properties"]
    assert "blocker_details" in order_properties
    assert "blocker_details" in line_properties
    assert {"actionability", "suggested_manual_status"}.issubset(lifecycle_properties)

    update_schema = schemas["ProcurementOrderLineUpdateRequest"]
    assert {"removal_reason", "replacement_sku_code"}.issubset(update_schema["properties"])
    manual_schema = schemas["ProcurementLifecycleManualDecisionRequest"]
    assert {"decision", "reason", "expected_run_id", "facts_hash"}.issubset(
        manual_schema["required"]
    )
