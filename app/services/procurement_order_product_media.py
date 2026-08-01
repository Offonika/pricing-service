"""Safe product-card and original-photo enrichment for open procurement orders."""

from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.procurement_order_formation import (
    ProcurementOrderFormation,
    ProcurementOrderFormationEvent,
    ProcurementOrderFormationLine,
)
from app.models.product import Product
from app.services.assortment_lifecycle_classification_store import (
    ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE,
)
from app.services.master_mobile_catalog import (
    PHOTO_SOURCE,
    MasterMobileCatalogResolver,
    ProductMediaResolution,
)

MANIFEST_SCHEMA_VERSION = 1
OPEN_ASSISTANT_STATUSES = frozenset({"draft", "review", "error"})
IMMUTABLE_ONEC_STATUSES = frozenset({"pending", "transmitted"})


def build_product_media_backfill_plan(
    db: Session,
    resolver: MasterMobileCatalogResolver,
    *,
    run_id: str | None = None,
) -> dict[str, Any]:
    run_id = run_id or f"procurement-media-{uuid.uuid4().hex}"
    orders = list(
        db.scalars(
            select(ProcurementOrderFormation)
            .where(ProcurementOrderFormation.status.in_(OPEN_ASSISTANT_STATUSES))
            .where(~ProcurementOrderFormation.onec_status.in_(IMMUTABLE_ONEC_STATUSES))
            .options(selectinload(ProcurementOrderFormation.lines))
            .order_by(ProcurementOrderFormation.id)
        )
        .unique()
        .all()
    )
    lines = [
        line
        for order in orders
        for line in sorted(order.lines, key=lambda item: item.line_number)
        if not line.removed
    ]
    codes = [str(line.nomenclature_code or "").strip() for line in lines]
    article_candidates = _product_articles_by_code(db, codes)
    articles = [
        _single_public_article(article_candidates.get(code))
        or (code if not article_candidates.get(code) else "")
        for code in codes
    ]
    resolutions = resolver.resolve_many(articles)
    items: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    changed_count = 0
    for line in lines:
        nomenclature_code = str(line.nomenclature_code or "").strip()
        candidates = article_candidates.get(nomenclature_code) or set()
        article = _single_public_article(candidates) or nomenclature_code
        if len(candidates) > 1:
            resolution = ProductMediaResolution(
                article=article,
                status="ambiguous",
                detail="multiple public articles mapped to exact 1C code",
            )
        else:
            resolution = resolutions.get(article) or ProductMediaResolution(
                article=article,
                status="not_found",
            )
        status_counts[resolution.status] += 1
        before_payload = _json_copy(line.payload or {})
        after_payload = _enriched_payload(before_payload, resolution)
        changed = before_payload != after_payload
        changed_count += int(changed)
        items.append(
            {
                "order_id": int(line.order_id),
                "line_id": int(line.id),
                "line_number": line.line_number,
                "nomenclature_code": nomenclature_code,
                "article": article,
                "resolution_status": resolution.status,
                "product_card_url": resolution.product_card_url,
                "photo_original_url": resolution.photo_original_url,
                "photo_thumbnail_url": resolution.photo_thumbnail_url,
                "changed": changed,
                "before_payload": before_payload,
                "after_payload": after_payload,
                "before_line_version": line.version,
            }
        )
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "run_id": run_id,
        "mode": "dry_run",
        "created_at": datetime.now(UTC).isoformat(),
        "database_commit": False,
        "summary": {
            "orders_scanned": len(orders),
            "lines_scanned": len(lines),
            "found": status_counts["found"],
            "not_found": status_counts["not_found"],
            "ambiguous": status_counts["ambiguous"],
            "article_mismatch": status_counts["article_mismatch"],
            "photo_missing": status_counts["photo_missing"],
            "unsafe_url": status_counts["unsafe_url"],
            "fetch_error": status_counts["fetch_error"],
            "changed": changed_count,
            "unchanged": len(lines) - changed_count,
        },
        "safety": {
            "bitrix_write": False,
            "onec_write": False,
            "commercial_fields_write": False,
            "open_assistant_orders_only": True,
        },
        "items": items,
        "orders": [
            {
                "order_id": int(order.id),
                "before_order_version": order.version,
            }
            for order in orders
            if any(item["order_id"] == order.id and item["changed"] for item in items)
        ],
    }


def apply_product_media_backfill(
    db: Session,
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    _validate_manifest(plan, expected_mode="dry_run")
    items = [dict(item) for item in plan.get("items", []) if dict(item).get("changed")]
    order_items: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        order_items[int(item["order_id"])].append(item)
    applied_items: list[dict[str, Any]] = []
    applied_orders: list[dict[str, Any]] = []
    for order_id, changes in order_items.items():
        order = _get_order_for_update(db, order_id)
        if not _order_is_open(order):
            raise RuntimeError(f"order {order_id} left the open assistant queue")
        before_order_version = order.version
        before_event_lines: list[dict[str, Any]] = []
        after_event_lines: list[dict[str, Any]] = []
        for change in changes:
            line = _line_by_id(order, int(change["line_id"]))
            if line.removed:
                raise RuntimeError(f"line {line.id} is no longer open")
            if line.version != int(change["before_line_version"]):
                raise RuntimeError(f"line {line.id} version changed")
            if _json_copy(line.payload or {}) != dict(change["before_payload"]):
                raise RuntimeError(f"line {line.id} payload changed")
            before_event_lines.append(
                {"line_id": line.id, "version": line.version, "payload": line.payload or {}}
            )
            line.payload = _json_copy(change["after_payload"])
            line.version += 1
            change["applied_line_version"] = line.version
            after_event_lines.append(
                {"line_id": line.id, "version": line.version, "payload": line.payload}
            )
            applied_items.append(change)
        order.version += 1
        applied_orders.append(
            {
                "order_id": order.id,
                "before_order_version": before_order_version,
                "applied_order_version": order.version,
            }
        )
        db.add(
            ProcurementOrderFormationEvent(
                order_id=order.id,
                entity_type="order",
                entity_id=str(order.id),
                event_type="procurement_product_media_backfilled",
                actor="system:procurement-product-media-backfill",
                idempotency_key=f"{plan['run_id']}:order:{order.id}",
                before={"version": before_order_version, "lines": before_event_lines},
                after={"version": order.version, "lines": after_event_lines},
                payload={
                    "source": PHOTO_SOURCE,
                    "changed_line_count": len(changes),
                    "bitrix_write": False,
                    "onec_write": False,
                },
            )
        )
    db.flush()
    result = _json_copy(plan)
    result.update(
        {
            "mode": "apply",
            "applied_at": datetime.now(UTC).isoformat(),
            "items": applied_items,
            "orders": applied_orders,
        }
    )
    result["summary"]["applied_lines"] = len(applied_items)
    result["summary"]["applied_orders"] = len(applied_orders)
    return result


def rollback_product_media_backfill(
    db: Session,
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    _validate_manifest(manifest, expected_mode="apply")
    run_id = str(manifest["run_id"])
    order_entries = {int(item["order_id"]): dict(item) for item in manifest.get("orders", [])}
    line_entries: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for item in manifest.get("items", []):
        line_entries[int(item["order_id"])].append(dict(item))
    rolled_back_lines = 0
    rolled_back_orders = 0
    for order_id, entries in line_entries.items():
        rollback_key = f"{run_id}:rollback:order:{order_id}"
        if db.scalar(
            select(ProcurementOrderFormationEvent).where(
                ProcurementOrderFormationEvent.idempotency_key == rollback_key
            )
        ):
            continue
        order = _get_order_for_update(db, order_id)
        if not _order_is_open(order):
            raise RuntimeError(f"order {order_id} is immutable and cannot be rolled back")
        order_entry = order_entries.get(order_id)
        if order_entry is None or order.version != int(order_entry["applied_order_version"]):
            raise RuntimeError(f"order {order_id} version changed after backfill")
        before_event_lines: list[dict[str, Any]] = []
        after_event_lines: list[dict[str, Any]] = []
        for entry in entries:
            line = _line_by_id(order, int(entry["line_id"]))
            if line.version != int(entry["applied_line_version"]):
                raise RuntimeError(f"line {line.id} version changed after backfill")
            if _json_copy(line.payload or {}) != dict(entry["after_payload"]):
                raise RuntimeError(f"line {line.id} payload changed after backfill")
            before_event_lines.append(
                {"line_id": line.id, "version": line.version, "payload": line.payload or {}}
            )
            line.payload = _json_copy(entry["before_payload"])
            line.version += 1
            rolled_back_lines += 1
            after_event_lines.append(
                {"line_id": line.id, "version": line.version, "payload": line.payload}
            )
        before_order_version = order.version
        order.version += 1
        rolled_back_orders += 1
        db.add(
            ProcurementOrderFormationEvent(
                order_id=order.id,
                entity_type="order",
                entity_id=str(order.id),
                event_type="procurement_product_media_backfill_rolled_back",
                actor="system:procurement-product-media-backfill",
                idempotency_key=rollback_key,
                before={"version": before_order_version, "lines": before_event_lines},
                after={"version": order.version, "lines": after_event_lines},
                payload={"source_run_id": run_id},
            )
        )
    db.flush()
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "run_id": run_id,
        "mode": "rollback",
        "rolled_back_at": datetime.now(UTC).isoformat(),
        "summary": {
            "rolled_back_lines": rolled_back_lines,
            "rolled_back_orders": rolled_back_orders,
        },
    }


def _enriched_payload(
    payload: Mapping[str, Any],
    resolution: ProductMediaResolution,
) -> dict[str, Any]:
    enriched = _json_copy(payload)
    if not resolution.found:
        return enriched
    enriched["product_card_url"] = resolution.product_card_url
    existing_photos = enriched.get("photos")
    photos = list(existing_photos) if isinstance(existing_photos, list) else []
    first = dict(photos[0]) if photos and isinstance(photos[0], dict) else {}
    existing_original = str(first.get("original") or first.get("original_url") or "").strip()
    if not existing_original:
        resolved_photo = {
            "thumbnail": resolution.photo_thumbnail_url or resolution.photo_original_url,
            "original": resolution.photo_original_url,
        }
        photos = [resolved_photo, *photos[1:]] if photos else [resolved_photo]
        enriched["photos"] = photos
        enriched["photo_source"] = PHOTO_SOURCE
    return enriched


def _get_order_for_update(db: Session, order_id: int) -> ProcurementOrderFormation:
    order = db.scalar(
        select(ProcurementOrderFormation)
        .where(ProcurementOrderFormation.id == order_id)
        .options(selectinload(ProcurementOrderFormation.lines))
        .with_for_update()
    )
    if order is None:
        raise LookupError(f"order {order_id} was not found")
    return order


def _line_by_id(
    order: ProcurementOrderFormation,
    line_id: int,
) -> ProcurementOrderFormationLine:
    line = next((item for item in order.lines if item.id == line_id), None)
    if line is None:
        raise LookupError(f"line {line_id} was not found in order {order.id}")
    return line


def _order_is_open(order: ProcurementOrderFormation) -> bool:
    return (
        order.status in OPEN_ASSISTANT_STATUSES and order.onec_status not in IMMUTABLE_ONEC_STATUSES
    )


def _product_articles_by_code(db: Session, codes: list[str]) -> dict[str, set[str]]:
    clean_codes = sorted({code for code in codes if code})
    if not clean_codes:
        return {}
    rows = db.execute(
        select(Product.code_1c, Product.article).where(
            Product.code_1c.in_(clean_codes),
            Product.is_active.is_(True),
        )
    ).all()
    classification_rows = db.execute(
        select(
            ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE.c.nomenclature_code,
            ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE.c.article,
        ).where(ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE.c.nomenclature_code.in_(clean_codes))
    ).all()
    result: dict[str, set[str]] = defaultdict(set)
    for code, article in [*rows, *classification_rows]:
        clean_code = str(code or "").strip()
        clean_article = str(article or "").strip()
        if clean_code and clean_article:
            result[clean_code].add(clean_article)
    return dict(result)


def _single_public_article(candidates: set[str] | None) -> str | None:
    if not candidates or len(candidates) != 1:
        return None
    return next(iter(candidates))


def _validate_manifest(manifest: Mapping[str, Any], *, expected_mode: str) -> None:
    if int(manifest.get("schema_version") or 0) != MANIFEST_SCHEMA_VERSION:
        raise ValueError("unsupported product media manifest schema")
    if manifest.get("mode") != expected_mode:
        raise ValueError(f"product media manifest mode must be {expected_mode}")
    if not str(manifest.get("run_id") or "").strip():
        raise ValueError("product media manifest run_id is required")


def _json_copy(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_copy(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_copy(item) for item in value]
    if isinstance(value, tuple):
        return [_json_copy(item) for item in value]
    return value
