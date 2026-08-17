import json
from datetime import UTC, date, datetime

from app.models.product import Product
from app.services.display_family_inventory import build_display_family_inventory
from app.services.onec_stock_availability import build_current_stock_snapshot
from tasks.report_display_family_registry_preflight import (
    MANIFEST_SCHEMA,
    build_source_quality,
    write_artifacts,
)


def _application_sources() -> dict[str, dict[str, object]]:
    return {
        "application_catalog": {"status": "ready", "row_count": 1},
        "lifecycle_history": {"status": "ready", "row_count": 1},
        "procurement_orders": {"status": "ready", "open_or_recent_sku_count": 0},
    }


def _ready_source_quality() -> dict[str, object]:
    snapshot = build_current_stock_snapshot(
        [
            {
                "product_code": "CODE-1",
                "source_row_count": 2,
                "positive_row_count": 1,
                "positive_quantity": "3",
                "net_quantity": "2",
            }
        ],
        captured_at=datetime(2026, 8, 16, 9, tzinfo=UTC),
    )
    return build_source_quality(
        as_of=date(2026, 8, 16),
        stock_snapshot=snapshot,
        application_sources=_application_sources(),
        matching_source={"status": "ready", "accepted_link_count": 0},
    )


def test_preflight_writes_hash_bound_read_only_bundle(tmp_path) -> None:
    product = Product(
        id=1,
        article="SKU-1",
        code_1c="CODE-1",
        name="Дисплей неизвестный",
        subject="display",
        category="Дисплеи",
        is_active=True,
        is_marked_for_deletion=False,
    )
    product.phone_model_links = []
    product.compatibilities = []
    product.stock = None
    payload = build_display_family_inventory(
        [product], evidence_by_code={}, as_of=date(2026, 8, 16)
    )

    manifest = write_artifacts(
        tmp_path,
        payload=payload,
        source_quality=_ready_source_quality(),
        source_warnings=["test_warning"],
    )

    assert manifest["schema"] == MANIFEST_SCHEMA
    assert manifest["status"] == "complete_read_only"
    assert manifest["source_quality_status"] == "ready"
    assert manifest["scope_policy_version"] == "display_scope_policy.v1"
    assert manifest["scope_excluded_count"] == 0
    assert manifest["scope_excluded_reason_counts"] == {}
    assert manifest["production_authorized"] is False
    assert manifest["external_writes"] is False
    assert set(manifest["artifact_sha256"]) == {
        "inventory.json",
        "inventory.csv",
        "report.html",
    }
    inventory = json.loads((tmp_path / "inventory.json").read_text(encoding="utf-8"))
    assert inventory["source_warnings"] == ["test_warning"]
    assert inventory["source_quality"]["status"] == "ready"
    assert "Режим: read-only" in (tmp_path / "report.html").read_text(encoding="utf-8")


def test_empty_stock_source_blocks_complete_bundle(tmp_path) -> None:
    snapshot = build_current_stock_snapshot(
        [],
        captured_at=datetime(2026, 8, 16, 9, tzinfo=UTC),
    )
    source_quality = build_source_quality(
        as_of=date(2026, 8, 16),
        stock_snapshot=snapshot,
        application_sources=_application_sources(),
        matching_source={"status": "ready", "accepted_link_count": 0},
    )
    payload = build_display_family_inventory([], evidence_by_code={}, as_of=date(2026, 8, 16))

    manifest = write_artifacts(
        tmp_path,
        payload=payload,
        source_quality=source_quality,
        source_warnings=[],
    )

    assert source_quality["gates"]["current_stock_snapshot_nonempty"]["status"] == "fail"
    assert manifest["status"] == "blocked_source_quality"


def test_stale_stock_source_blocks_complete_bundle(tmp_path) -> None:
    snapshot = build_current_stock_snapshot(
        [
            {
                "product_code": "CODE-1",
                "source_row_count": 1,
                "positive_row_count": 1,
                "positive_quantity": "1",
                "net_quantity": "1",
            }
        ],
        captured_at=datetime(2026, 8, 15, 9, tzinfo=UTC),
    )
    source_quality = build_source_quality(
        as_of=date(2026, 8, 16),
        stock_snapshot=snapshot,
        application_sources=_application_sources(),
        matching_source={"status": "ready", "accepted_link_count": 0},
    )
    payload = build_display_family_inventory([], evidence_by_code={}, as_of=date(2026, 8, 16))

    manifest = write_artifacts(
        tmp_path,
        payload=payload,
        source_quality=source_quality,
        source_warnings=[],
    )

    freshness_gate = source_quality["gates"]["current_stock_snapshot_fresh_for_as_of"]
    assert freshness_gate["status"] == "fail"
    assert manifest["status"] == "blocked_source_quality"
