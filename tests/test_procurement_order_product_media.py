from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.procurement_order_formation import (
    ProcurementOrderFormation,
    ProcurementOrderFormationEvent,
    ProcurementOrderFormationLine,
)
from app.models.product import Product
from app.services.assortment_lifecycle_classification_store import (
    ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE,
)
from app.services.master_mobile_catalog import ProductMediaResolution
from app.services.procurement_order_product_media import (
    apply_product_media_backfill,
    build_product_media_backfill_plan,
    rollback_product_media_backfill,
)
from tasks.backfill_procurement_order_product_media import (
    load_existing_committed_apply_result,
)


class StubResolver:
    def __init__(self, resolutions: dict[str, ProductMediaResolution]) -> None:
        self.resolutions = resolutions
        self.calls: list[list[str]] = []

    def resolve_many(self, articles: list[str]) -> dict[str, ProductMediaResolution]:
        self.calls.append(articles)
        return {
            article: self.resolutions[article]
            for article in articles
            if article in self.resolutions
        }


def _resolution(article: str = "044702") -> ProductMediaResolution:
    return ProductMediaResolution(
        article=article,
        status="found",
        product_id="40699",
        product_card_url="https://master-mobile.ru/catalog/zapchasti/40699/",
        photo_thumbnail_url="https://master-mobile.ru/upload/thumb/40699.webp",
        photo_original_url="https://master-mobile.ru/upload/original/40699.webp",
    )


def _open_order(
    db_session,
    *,
    nomenclature_code: str = "044702",
) -> ProcurementOrderFormation:
    ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE.create(
        bind=db_session.get_bind(),
        checkfirst=True,
    )
    order = ProcurementOrderFormation(
        stable_key="media-backfill-order",
        status="draft",
        version=1,
        supplier_ref="supplier-ref",
        supplier_code="SUP-1",
        supplier_name="Поставщик",
        contract_ref="contract-ref",
        contract_code="CON-1",
        contract_name="Договор",
        warehouse_ref="warehouse-ref",
        warehouse_code="WH-1",
        warehouse_name="Склад",
        currency="RUB",
        procurement_contour="ordinary",
        route="ordinary",
        batch_id="2026-08-01",
        order_date=date(2026, 8, 1),
        calculation_id="media-backfill",
        onec_status="not_sent",
        payload={},
    )
    order.lines.append(
        ProcurementOrderFormationLine(
            stable_key="media-backfill-order:044702",
            line_number=1,
            version=1,
            bitrix_product_id="40699",
            bitrix_product_xml_id="11111111-2222-3333-4444-555555555555",
            nomenclature_ref="11111111-2222-3333-4444-555555555555",
            nomenclature_code=nomenclature_code,
            nomenclature_name="Аккумулятор",
            recommended_quantity=Decimal("5"),
            final_quantity=Decimal("5"),
            purchase_price=Decimal("100"),
            amount=Decimal("500"),
            currency="RUB",
            source_kind="automatic",
            explicit_demand=False,
            risk_codes=[],
            blockers=[],
            payload={},
            removed=False,
        )
    )
    db_session.add(order)
    db_session.commit()
    return order


def test_backfill_dry_run_reports_exact_match_without_writing(db_session) -> None:
    order = _open_order(db_session)
    resolver = StubResolver({"044702": _resolution()})

    plan = build_product_media_backfill_plan(db_session, resolver, run_id="media-dry-run")
    db_session.rollback()
    db_session.refresh(order)

    assert plan["mode"] == "dry_run"
    assert plan["summary"] == {
        "orders_scanned": 1,
        "lines_scanned": 1,
        "found": 1,
        "not_found": 0,
        "ambiguous": 0,
        "article_mismatch": 0,
        "photo_missing": 0,
        "unsafe_url": 0,
        "fetch_error": 0,
        "changed": 1,
        "unchanged": 0,
    }
    assert order.lines[0].payload == {}
    assert order.version == 1
    assert order.lines[0].version == 1
    assert resolver.calls == [["044702"]]


def test_backfill_resolves_public_article_from_exact_1c_code(db_session) -> None:
    _open_order(db_session, nomenclature_code="РБ000053199")
    db_session.add(
        Product(
            article="044702",
            code_1c="РБ000053199",
            name="Аккумулятор",
            is_active=True,
        )
    )
    db_session.commit()
    resolver = StubResolver({"044702": _resolution()})

    plan = build_product_media_backfill_plan(
        db_session,
        resolver,
        run_id="media-article-map",
    )

    assert resolver.calls == [["044702"]]
    assert plan["summary"]["found"] == 1
    assert plan["items"][0]["nomenclature_code"] == "РБ000053199"
    assert plan["items"][0]["article"] == "044702"


def test_backfill_apply_is_idempotent_and_preserves_commercial_fields(db_session) -> None:
    order = _open_order(db_session)
    line = order.lines[0]
    commercial_before = (
        order.status,
        order.supplier_ref,
        order.contract_ref,
        line.final_quantity,
        line.purchase_price,
        line.amount,
        line.bitrix_product_id,
    )
    resolver = StubResolver({"044702": _resolution()})
    plan = build_product_media_backfill_plan(db_session, resolver, run_id="media-apply")

    manifest = apply_product_media_backfill(db_session, plan)
    db_session.commit()
    db_session.refresh(order)

    assert order.version == 2
    assert line.version == 2
    assert line.payload == {
        "product_card_url": "https://master-mobile.ru/catalog/zapchasti/40699/",
        "photos": [
            {
                "thumbnail": "https://master-mobile.ru/upload/thumb/40699.webp",
                "original": "https://master-mobile.ru/upload/original/40699.webp",
            }
        ],
        "photo_source": "master_mobile_site",
    }
    assert commercial_before == (
        order.status,
        order.supplier_ref,
        order.contract_ref,
        line.final_quantity,
        line.purchase_price,
        line.amount,
        line.bitrix_product_id,
    )
    assert manifest["summary"]["applied_lines"] == 1
    assert db_session.scalar(
        select(ProcurementOrderFormationEvent).where(
            ProcurementOrderFormationEvent.event_type == "procurement_product_media_backfilled"
        )
    )

    second_plan = build_product_media_backfill_plan(
        db_session,
        resolver,
        run_id="media-apply-again",
    )
    second_manifest = apply_product_media_backfill(db_session, second_plan)
    db_session.commit()

    assert second_plan["summary"]["changed"] == 0
    assert second_manifest["summary"]["applied_lines"] == 0
    assert order.version == 2
    assert line.version == 2


def test_backfill_rollback_restores_payload_with_monotonic_versions(db_session) -> None:
    order = _open_order(db_session)
    resolver = StubResolver({"044702": _resolution()})
    manifest = apply_product_media_backfill(
        db_session,
        build_product_media_backfill_plan(db_session, resolver, run_id="media-rollback"),
    )
    db_session.commit()
    applied_order_version = order.version
    applied_line_version = order.lines[0].version

    result = rollback_product_media_backfill(db_session, manifest)
    db_session.commit()
    db_session.refresh(order)

    assert result["summary"] == {"rolled_back_lines": 1, "rolled_back_orders": 1}
    assert order.lines[0].payload == {}
    assert order.version == applied_order_version + 1
    assert order.lines[0].version == applied_line_version + 1


def test_backfill_skips_orders_that_are_already_immutable(db_session) -> None:
    order = _open_order(db_session)
    order.status = "transmitted"
    order.onec_status = "transmitted"
    db_session.commit()

    plan = build_product_media_backfill_plan(
        db_session,
        StubResolver({"044702": _resolution()}),
        run_id="media-immutable",
    )

    assert plan["summary"]["orders_scanned"] == 0
    assert plan["summary"]["lines_scanned"] == 0


def test_committed_apply_manifest_is_reused_without_overwrite(tmp_path) -> None:
    path = tmp_path / "rollback.json"
    payload = {
        "mode": "apply",
        "run_id": "media-apply",
        "database_commit": True,
        "summary": {"applied_lines": 45},
    }
    original = json.dumps(payload, sort_keys=True)
    path.write_text(original, encoding="utf-8")

    result = load_existing_committed_apply_result(path, run_id="media-apply")

    assert result == payload
    assert path.read_text(encoding="utf-8") == original


def test_existing_apply_manifest_requires_matching_committed_run(tmp_path) -> None:
    path = tmp_path / "rollback.json"
    path.write_text(
        json.dumps(
            {
                "mode": "apply",
                "run_id": "media-apply",
                "database_commit": True,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit, match="another run-id"):
        load_existing_committed_apply_result(path, run_id="different-run")

    path.write_text(
        json.dumps(
            {
                "mode": "apply",
                "run_id": "media-apply",
                "database_commit": False,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="not a committed apply manifest"):
        load_existing_committed_apply_result(path, run_id="media-apply")
