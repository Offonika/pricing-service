from __future__ import annotations

import hashlib
import json
import urllib.parse
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from uuid import UUID

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session, selectinload

from app.core.config import Settings, get_settings
from app.models.display_family_registry import (
    DisplayFamily,
    DisplayFamilyMember,
    DisplayFamilyRegistryVersion,
)
from app.models.procurement_order_formation import (
    ProcurementOrderFormationLine,
    ProcurementProductCardSyncState,
)
from app.models.product import Product
from app.services.assortment_lifecycle_classification_store import (
    ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE,
)
from app.services.bitrix_order_formation import (
    BitrixCatalogProduct,
    bitrix_call,
    resolve_catalog_product_by_id,
    resolve_catalog_products_by_xml_ids,
)
from app.services.display_family_order_recommendation import demand_speed_scores
from app.services.procurement_order_formation import (
    line_blocker_details,
    normalize_guid,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
ACTIVE_ORDER_STATUSES = frozenset({"draft", "review", "approved", "transmitting", "transmitted"})

# Свойства с этими ключами принадлежат pricing-service. ID вида PROPERTY_123
# обнаруживаются на конкретном портале и хранятся только в runtime mapping.
PRODUCT_CARD_METRIC_FIELD_SPECS: tuple[dict[str, Any], ...] = (
    {"key": "calculated_at", "title": "[Авто] Дата расчёта", "type": "S", "sort": 810},
    {"key": "source_state", "title": "[Авто] Состояние данных", "type": "S", "sort": 820},
    {"key": "lifecycle_status", "title": "[Авто] Жизненный статус", "type": "S", "sort": 830},
    {"key": "lifecycle_reason", "title": "[Авто] Причина статуса", "type": "S", "sort": 840},
    {"key": "blocker_count", "title": "[Авто] Количество блокеров", "type": "N", "sort": 850},
    {"key": "blocker_summary", "title": "[Авто] Блокеры", "type": "S", "sort": 860},
    {"key": "recommendation", "title": "[Авто] Рекомендация", "type": "S", "sort": 870},
    {
        "key": "recommended_order",
        "title": "[Авто] Рекомендовано заказать",
        "type": "N",
        "sort": 880,
    },
)
PRODUCT_CARD_LINK_FIELD_SPEC: dict[str, Any] = {
    "key": "insights_url",
    "title": "Показатели товара",
    "type": "S",
    "sort": 800,
}
PRODUCT_CARD_FIELD_SPECS: tuple[dict[str, Any], ...] = (
    PRODUCT_CARD_LINK_FIELD_SPEC,
    *PRODUCT_CARD_METRIC_FIELD_SPECS,
)


def bitrix_product_path(product_id: str | int | None, *, catalog_id: int = 17) -> str | None:
    value = str(product_id or "").strip()
    if not value or not value.isdigit() or catalog_id <= 0:
        return None
    return f"/crm/catalog/{catalog_id}/product/{value}/"


def product_insights_url(
    product_id: str | int | None,
    *,
    settings: Settings | None = None,
) -> str | None:
    value = str(product_id or "").strip()
    if not value.isdigit():
        return None
    settings = settings or get_settings()
    base_url = settings.procurement_product_card_insights_base_url.strip().rstrip("/")
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("product insights base URL must be an absolute HTTPS URL without a query")
    query = urllib.parse.urlencode(
        {
            "params[VIEW]": "product_insights",
            "params[PRODUCT_ID]": value,
        }
    )
    return f"{base_url}/?{query}"


def load_product_card_mapping(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    path = _product_card_mapping_path(settings)
    if not path.exists():
        raise RuntimeError(f"product card mapping does not exist: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("fields"), dict):
        raise RuntimeError("product card mapping must contain a fields object")
    return payload


def build_product_card_snapshot(
    db: Session,
    *,
    product_id: str | None = None,
    xml_id: str | None = None,
    nomenclature_code: str | None = None,
    settings: Settings | None = None,
    product_loader: Callable[..., BitrixCatalogProduct | None] = resolve_catalog_product_by_id,
) -> dict[str, Any]:
    settings = settings or get_settings()
    lines = _matching_lines(
        db,
        product_id=product_id,
        xml_id=xml_id,
        nomenclature_code=nomenclature_code,
    )
    line = _latest_line(lines)
    product: BitrixCatalogProduct | None = None
    lookup_xml_id = normalize_guid(
        (line.bitrix_product_xml_id or line.nomenclature_ref) if line else xml_id
    )
    if line is None and product_id:
        state = db.scalar(
            select(ProcurementProductCardSyncState).where(
                ProcurementProductCardSyncState.bitrix_product_id == str(product_id).strip()
            )
        )
        if state is not None:
            lookup_xml_id = normalize_guid(state.product_xml_id)
            product = BitrixCatalogProduct(
                product_id=str(product_id).strip(),
                name="",
                xml_id=lookup_xml_id,
            )
        else:
            product = product_loader(product_id, settings=settings)
            if product is None:
                raise LookupError("Bitrix product was not found")
            lookup_xml_id = normalize_guid(product.xml_id)
        if lookup_xml_id:
            lines = _matching_lines(
                db,
                product_id=None,
                xml_id=lookup_xml_id,
                nomenclature_code=None,
            )
            line = _latest_line(lines)
    classification = _classification_row(
        db,
        nomenclature_code=(line.nomenclature_code if line else nomenclature_code),
        xml_id=lookup_xml_id,
    )
    if line is None and classification is None:
        raise LookupError("product card data was not found")
    return _snapshot_from_sources(
        line=line,
        related_lines=lines,
        classification=classification,
        product=product,
        settings=settings,
    )


def build_product_card_review_snapshot(
    db: Session,
    *,
    nomenclature_code: str,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Build a stable family review with the primary SKU and four candidates."""

    settings = settings or get_settings()
    primary = build_product_card_snapshot(
        db,
        nomenclature_code=nomenclature_code,
        settings=settings,
    )
    primary_code = str(primary["identity"].get("nomenclature_code") or "").strip()
    family_context = _active_family_member_rows(db, nomenclature_code=primary_code)
    member_rows = list((family_context or {}).get("members") or [])
    if not any(
        str(item.get("nomenclature_code") or "").strip() == primary_code for item in member_rows
    ):
        member_rows.insert(
            0,
            {
                "nomenclature_code": primary_code,
                "name": primary["identity"].get("name") or "",
                "article": primary["identity"].get("article") or "",
            },
        )

    cards_by_code: dict[str, dict[str, Any]] = {primary_code: primary}
    for member in member_rows:
        code = str(member.get("nomenclature_code") or "").strip()
        if not code or code in cards_by_code:
            continue
        try:
            cards_by_code[code] = build_product_card_snapshot(
                db,
                nomenclature_code=code,
                settings=settings,
            )
        except LookupError:
            cards_by_code[code] = _empty_product_card_snapshot(member)

    score_rows = {
        code: {
            "sales_qty_window_short": card["demand"].get("sales_30"),
            "sales_qty_window_medium": card["demand"].get("sales_90"),
            "sales_qty_window": card["demand"].get("sales_180"),
        }
        for code, card in cards_by_code.items()
    }
    scores, ranking_source = demand_speed_scores(score_rows)
    candidate_codes = sorted(
        (code for code in cards_by_code if code != primary_code),
        key=lambda code: (-scores.get(code, Decimal("0")), code),
    )[:4]
    visible_codes = [primary_code, *candidate_codes]
    comparison = [
        {
            "role": "primary" if code == primary_code else "candidate",
            "role_label": "Основная карточка" if code == primary_code else "Кандидат семьи",
            "rank": position,
            "speed_score": scores.get(code, Decimal("0")),
            "card": _comparison_card(cards_by_code[code]),
        }
        for position, code in enumerate(visible_codes)
    ]
    all_codes = [
        str(item.get("nomenclature_code") or "").strip()
        for item in member_rows
        if str(item.get("nomenclature_code") or "").strip()
    ]
    total_member_count = int(
        (family_context or {}).get("member_count")
        or primary.get("family", {}).get("member_count")
        or len(all_codes)
    )
    family = dict(primary.get("family") or {})
    if family_context:
        family["id"] = family_context.get("family_key") or family.get("id")
        family["record_id"] = family_context.get("family_record_id")
        family["registry_version_id"] = family_context.get("registry_version_id")
        family["registry_version_number"] = family_context.get("registry_version_number")
        family["registry_inventory_checksum"] = family_context.get("registry_inventory_checksum")
        family["label"] = family_context.get("label") or family.get("label")
    family.update(
        {
            "member_count": total_member_count,
            "total_member_count": total_member_count,
            "visible_member_count": len(comparison),
            "hidden_member_count": max(0, total_member_count - len(comparison)),
            "member_codes": all_codes or [primary_code],
            "ranking_source": ranking_source,
            "ranking_source_label": (
                "скорость завершённых продаж за 30 и 90 дней"
                if ranking_source == "completed_sales_rate_30_90"
                else "скорость завершённых продаж за 180 дней"
            ),
            "comparison_members": comparison,
        }
    )
    primary["family"] = family
    snapshot_at = dict(primary.get("source") or {}).get("calculated_at")
    member_sources = {
        code: {
            "state": dict(cards_by_code[code].get("source") or {}).get("state"),
            "calculated_at": dict(cards_by_code[code].get("source") or {}).get("calculated_at"),
        }
        for code in visible_codes
    }
    one_snapshot = all(
        item.get("calculated_at") in {None, snapshot_at} for item in member_sources.values()
    )
    all_ready = all(item.get("state") == "ready" for item in member_sources.values())
    source = dict(primary.get("source") or {})
    source.update(
        {
            "calculated_at": snapshot_at,
            "state": "ready" if one_snapshot and all_ready else "partial",
            "member_sources": member_sources,
            "single_snapshot": one_snapshot,
        }
    )
    primary["source"] = source
    return primary


def list_product_card_snapshots(
    db: Session,
    *,
    scope: str = "displays",
    limit: int | None = None,
    settings: Settings | None = None,
    resolver: Callable[..., dict[str, BitrixCatalogProduct]] = resolve_catalog_products_by_xml_ids,
) -> list[dict[str, Any]]:
    """Return one newest product snapshot per SKU for dry-run/apply synchronization."""

    if scope not in {"displays", "all"}:
        raise ValueError("scope must be displays or all")
    settings = settings or get_settings()
    classifications = _classification_rows(db, scope=scope)
    lines = _all_product_lines(db)
    line_by_code: dict[str, list[ProcurementOrderFormationLine]] = {}
    line_by_guid: dict[str, list[ProcurementOrderFormationLine]] = {}
    for line in lines:
        if line.nomenclature_code:
            line_by_code.setdefault(line.nomenclature_code.strip(), []).append(line)
        guid = normalize_guid(line.bitrix_product_xml_id or line.nomenclature_ref)
        if guid:
            line_by_guid.setdefault(guid, []).append(line)

    candidates: list[tuple[dict[str, Any] | None, ProcurementOrderFormationLine | None]] = []
    seen_codes: set[str] = set()
    seen_guids: set[str] = set()
    for row in classifications:
        code = str(row.get("nomenclature_code") or "").strip()
        source_record = dict(row.get("source_record") or {})
        guid = normalize_guid(row.get("product_ref") or source_record.get("product_ref"))
        if guid and guid in seen_guids:
            continue
        related = line_by_code.get(code) or line_by_guid.get(guid) or []
        candidates.append((row, _latest_line(related)))
        if code:
            seen_codes.add(code)
        if guid:
            seen_guids.add(guid)

    # Открытые строки заказа остаются видимы, даже если классификационная витрина
    # временно не обновилась. Для scope=displays нужен явный дисплейный признак.
    for line in lines:
        code = str(line.nomenclature_code or "").strip()
        guid = normalize_guid(line.bitrix_product_xml_id or line.nomenclature_ref)
        if code in seen_codes or (guid and guid in seen_guids):
            continue
        if scope == "displays" and not _line_is_display(line):
            continue
        candidates.append((None, line))
        if code:
            seen_codes.add(code)
        if guid:
            seen_guids.add(guid)

    if limit is not None:
        candidates = candidates[: max(0, limit)]

    unresolved_guids = []
    for row, line in candidates:
        if line is not None and line.bitrix_product_id:
            continue
        source_record = dict((row or {}).get("source_record") or {})
        guid = normalize_guid(
            (row or {}).get("product_ref")
            or source_record.get("product_ref")
            or (line.bitrix_product_xml_id if line else None)
        )
        if guid:
            unresolved_guids.append(guid)
    resolved = resolver(unresolved_guids, settings=settings) if unresolved_guids else {}

    snapshots: list[dict[str, Any]] = []
    for row, line in candidates:
        source_record = dict((row or {}).get("source_record") or {})
        guid = normalize_guid(
            (line.bitrix_product_xml_id if line else None)
            or (row or {}).get("product_ref")
            or source_record.get("product_ref")
        )
        product = resolved.get(guid)
        related = []
        if line is not None:
            related = line_by_code.get(str(line.nomenclature_code or "").strip(), [line])
        elif row is not None:
            related = line_by_code.get(str(row.get("nomenclature_code") or "").strip(), [])
        snapshot = _snapshot_from_sources(
            line=line,
            related_lines=related,
            classification=row,
            product=product,
            settings=settings,
        )
        if snapshot["identity"].get("bitrix_product_id"):
            snapshots.append(snapshot)
    return snapshots


def product_card_native_fields(
    snapshot: Mapping[str, Any],
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    demand = dict(snapshot.get("demand") or {})
    lifecycle = dict(snapshot.get("lifecycle") or {})
    source = dict(snapshot.get("source") or {})
    blocker_messages = [
        str(item.get("message") or item.get("code") or "").strip()
        for item in list(snapshot.get("blockers") or [])
        if isinstance(item, Mapping)
    ]
    fields = {
        "insights_url": product_insights_url(
            dict(snapshot.get("identity") or {}).get("bitrix_product_id"),
            settings=settings,
        ),
        "calculated_at": source.get("calculated_at"),
        "source_state": source.get("state"),
        "lifecycle_status": lifecycle.get("label") or lifecycle.get("status"),
        "lifecycle_reason": lifecycle.get("reason"),
        "blocker_count": len(blocker_messages),
        "blocker_summary": "; ".join(blocker_messages),
        "recommendation": snapshot.get("recommendation"),
        "recommended_order": demand.get("recommended_order"),
    }
    # Неизвестное значение не превращаем в ноль и не очищаем им карточку. Пустой
    # список блокеров, напротив, является известным состоянием и должен удалить
    # старый серверный текст из карточки.
    clearable = {"blocker_summary"}
    return {
        key: _field_value(value)
        for key, value in fields.items()
        if value is not None and (value != "" or key in clearable)
    }


def sync_product_cards(
    db: Session,
    *,
    scope: str = "displays",
    apply: bool = False,
    limit: int | None = None,
    product_id: str | None = None,
    allow_multiple: bool = False,
    settings: Settings | None = None,
    mapping: Mapping[str, Any] | None = None,
    caller: Callable[..., dict[str, Any]] = bitrix_call,
    resolver: Callable[..., dict[str, BitrixCatalogProduct]] = resolve_catalog_products_by_xml_ids,
) -> dict[str, Any]:
    settings = settings or get_settings()
    if apply and not settings.procurement_product_card_apply_enabled:
        raise PermissionError("product card apply is disabled")
    requested_product_id = str(product_id or "").strip()
    if requested_product_id and not requested_product_id.isdigit():
        raise ValueError("product_id must contain digits only")
    if apply and not requested_product_id and not allow_multiple:
        raise PermissionError(
            "product card apply requires an exact product_id; "
            "mass apply requires allow_multiple=True"
        )
    mapping_path = _product_card_mapping_path(settings)
    if mapping is not None:
        runtime_mapping = dict(mapping)
    elif apply or mapping_path.exists():
        runtime_mapping = load_product_card_mapping(settings)
    else:
        runtime_mapping = {
            "catalog_id": settings.procurement_product_card_catalog_id,
            "fields": {},
        }
    field_mapping = dict(runtime_mapping.get("fields") or {})
    missing = [
        spec["key"]
        for spec in PRODUCT_CARD_FIELD_SPECS
        if not str(field_mapping.get(spec["key"]) or "").strip()
    ]
    if apply and missing:
        raise RuntimeError("product card mapping is incomplete: " + ", ".join(missing))
    mapped_catalog_id = int(
        runtime_mapping.get("catalog_id") or settings.procurement_product_card_catalog_id
    )
    if mapped_catalog_id != settings.procurement_product_card_catalog_id:
        raise RuntimeError("product card mapping catalog does not match configured catalog")
    batch_size = settings.procurement_product_card_batch_size
    if batch_size < 1 or batch_size > 50:
        raise ValueError("product card batch size must be between 1 and 50")
    sync_state_table_exists = _product_card_sync_state_table_exists(db)
    if apply and not sync_state_table_exists:
        raise RuntimeError(
            "product card sync state table is missing; apply the required migration first"
        )

    snapshots = list_product_card_snapshots(
        db,
        scope=scope,
        limit=None if requested_product_id else limit,
        settings=settings,
        resolver=resolver,
    )
    if requested_product_id:
        snapshots = [
            snapshot
            for snapshot in snapshots
            if str(dict(snapshot.get("identity") or {}).get("bitrix_product_id") or "").strip()
            == requested_product_id
        ]
        if len(snapshots) != 1:
            raise LookupError(
                f"expected exactly one product snapshot for Bitrix product {requested_product_id}"
            )
    xml_ids = [
        str(snapshot.get("identity", {}).get("xml_id") or "")
        for snapshot in snapshots
        if snapshot.get("identity", {}).get("xml_id")
    ]
    states = (
        {
            state.product_xml_id: state
            for state in db.scalars(
                select(ProcurementProductCardSyncState).where(
                    ProcurementProductCardSyncState.product_xml_id.in_(xml_ids)
                )
            ).all()
        }
        if sync_state_table_exists and xml_ids
        else {}
    )
    results: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for snapshot in snapshots:
        identity = dict(snapshot["identity"])
        logical_fields = product_card_native_fields(snapshot, settings=settings)
        snapshot_hash = _payload_hash(logical_fields)
        xml_id = _canonical_guid(identity.get("xml_id"))
        if xml_id is None:
            results.append(
                {
                    **identity,
                    "status": "failed",
                    "changed_fields": sorted(logical_fields),
                    "mismatches": {},
                    "error": "valid normalized product XML_ID is missing",
                }
            )
            continue
        product_id = str(identity.get("bitrix_product_id") or "").strip()
        if not product_id.isdigit():
            results.append(
                {
                    **identity,
                    "status": "failed",
                    "changed_fields": sorted(logical_fields),
                    "mismatches": {},
                    "error": "numeric Bitrix product ID is missing",
                }
            )
            continue
        state = states.get(xml_id)
        unchanged = bool(
            state
            and state.status == "synced"
            and state.snapshot_hash == snapshot_hash
            and state.bitrix_product_id == identity["bitrix_product_id"]
        )
        if unchanged:
            results.append({**identity, "status": "unchanged", "changed_fields": []})
            continue
        mapped_fields = {
            field_mapping[key]: value
            for key, value in logical_fields.items()
            if key in field_mapping
        }
        if not apply:
            results.append(
                {
                    **identity,
                    "status": "would_update",
                    "changed_fields": sorted(logical_fields),
                    "fields": logical_fields,
                }
            )
            continue
        now = datetime.now(UTC).replace(tzinfo=None)
        if state is None:
            state = ProcurementProductCardSyncState(product_xml_id=xml_id)
            db.add(state)
            states[xml_id] = state
        state.bitrix_product_id = identity["bitrix_product_id"]
        state.scope = scope
        state.snapshot_hash = snapshot_hash
        state.desired_fields = logical_fields
        state.last_attempt_at = now
        pending.append(
            {
                "identity": identity,
                "logical_fields": logical_fields,
                "mapped_fields": mapped_fields,
                "state": state,
                "attempted_at": now,
            }
        )
    if apply:
        for start in range(0, len(pending), batch_size):
            _sync_product_card_batch(
                pending[start : start + batch_size],
                results=results,
                caller=caller,
                settings=settings,
            )
        db.commit()
    return {
        "mode": "apply" if apply else "dry_run",
        "scope": scope,
        "product_id": requested_product_id or None,
        "allow_multiple": allow_multiple,
        "total": len(results),
        "updated": sum(item["status"] == "synced" for item in results),
        "unchanged": sum(item["status"] == "unchanged" for item in results),
        "blocked": sum(item["status"] in {"readback_mismatch", "failed"} for item in results),
        "missing_mapping_fields": missing,
        "items": results,
    }


def _snapshot_from_sources(
    *,
    line: ProcurementOrderFormationLine | None,
    related_lines: Sequence[ProcurementOrderFormationLine],
    classification: Mapping[str, Any] | None,
    product: BitrixCatalogProduct | None,
    settings: Settings,
) -> dict[str, Any]:
    classification = dict(classification or {})
    source_record = dict(classification.get("source_record") or {})
    payload = {**source_record, **dict(line.payload or {})} if line is not None else source_record
    order = line.order if line is not None else None
    product_id = str(
        (line.bitrix_product_id if line else None) or (product.product_id if product else "") or ""
    )
    xml_id = normalize_guid(
        (line.bitrix_product_xml_id if line else None)
        or (product.xml_id if product else None)
        or classification.get("product_ref")
        or source_record.get("product_ref")
    )
    code = str(
        (line.nomenclature_code if line else None)
        or classification.get("nomenclature_code")
        or source_record.get("nomenclature_code")
        or ""
    ).strip()
    name = str(
        (line.nomenclature_name if line else None)
        or (product.name if product else None)
        or classification.get("name")
        or ""
    ).strip()
    lifecycle_status = str(
        classification.get("status") or (line.lifecycle_status if line else None) or ""
    ).strip()
    lifecycle_label = str(classification.get("status_label") or lifecycle_status).strip()
    reason = str(
        classification.get("reason_text") or (line.recommendation_reason if line else None) or ""
    ).strip()
    blockers = (
        line_blocker_details(line) if line is not None else _classification_blockers(classification)
    )
    calculated_at = (
        payload.get("metrics_as_of")
        or payload.get("calculation_at")
        or (order.order_date.isoformat() if order else None)
        or _date_text(classification.get("classified_at"))
    )
    source_state = _source_state(calculated_at, blockers=blockers, settings=settings)
    family = dict(payload.get("display_family_recommendation") or {})
    related_orders = _related_orders(
        related_lines, catalog_id=settings.procurement_product_card_catalog_id
    )
    sales_180 = _decimal(payload.get("sales_qty_window"))
    sales_90 = _decimal(payload.get("sales_qty_window_medium"))
    sales_30 = _decimal(payload.get("sales_qty_window_short"))
    return {
        "identity": {
            "bitrix_product_id": product_id,
            "xml_id": xml_id,
            "nomenclature_code": code,
            "name": name,
            "article": str(classification.get("article") or source_record.get("article") or ""),
            "photo_url": _first_photo(line, product),
            "website_url": payload.get("product_card_url"),
            "bitrix_url": bitrix_product_path(
                product_id,
                catalog_id=settings.procurement_product_card_catalog_id,
            ),
        },
        "properties": {
            "assortment_status": (
                line.assortment_status if line else product.assortment_status if product else None
            ),
            "quality": (
                line.quality
                if line
                else product.quality if product else classification.get("quality_raw")
            ),
            "procurement_profile": (
                line.procurement_profile
                if line
                else (
                    product.procurement_profile
                    if product
                    else classification.get("expensive_profile_label")
                )
            ),
            "manual_minimum": (
                line.manual_minimum if line else product.manual_minimum if product else None
            ),
            "subject": classification.get("subject_1c"),
            "category": classification.get("category_1c"),
            "brand": classification.get("brand_compatibility"),
            "model": classification.get("model_compatibility"),
            "characteristics": dict(classification.get("characteristic_values") or {}),
        },
        "lifecycle": {
            "status": lifecycle_status,
            "label": lifecycle_label,
            "reason": reason,
            "birthday": payload.get("birthday") or payload.get("first_sale_date"),
        },
        "demand": {
            "sales_30": sales_30,
            "sales_90": sales_90,
            "sales_180": sales_180,
            "rate_30": _rate(sales_30, 30),
            "rate_90": _rate(sales_90, 90),
            "rate_180": _rate(sales_180, 180),
            "sellable_stock": _decimal(payload.get("sellable_stock_qty")),
            "customer_orders": _decimal(payload.get("active_customer_order_qty")),
            "incoming": _decimal(payload.get("incoming_qty")),
            "target_stock": _decimal(payload.get("target_stock_qty")),
            "recommended_order": (
                line.recommended_quantity
                if line
                else _decimal(payload.get("recommended_order_qty"))
            ),
            "current_order": line.final_quantity if line else None,
        },
        "quality": {
            "return_qty_180": _decimal(payload.get("return_qty_window")),
            "return_document_count_180": payload.get("return_document_count_window"),
            "batch_return_qty_90": _decimal(payload.get("batch_error_return_qty")),
            "new_quality_return_qty_90": _decimal(
                _first_known(
                    payload.get("new_quality_return_qty_90"),
                    payload.get("batch_error_return_qty"),
                )
            ),
            "new_quality_return_document_count_90": payload.get(
                "new_quality_return_document_count_90"
            ),
            "site_excluded_return_qty_90": _decimal(payload.get("site_excluded_return_qty_90")),
            "site_excluded_return_document_count_90": payload.get(
                "site_excluded_return_document_count_90"
            ),
            "return_reasons_90": list(payload.get("return_reasons_90") or []),
            "defect_return_qty_90": _decimal(payload.get("defect_return_qty")),
            "defect_pct": _decimal(
                _first_known(
                    payload.get("product_defect_pct"),
                    payload.get("defect_share_pct"),
                )
            ),
            "defect_history_units": payload.get("product_defect_history_units"),
            "confidence": payload.get("product_defect_confidence"),
            "diagnostic_signal_pct": _decimal(
                _first_known(
                    payload.get("new_quality_return_share_pct"),
                    payload.get("batch_error_share_pct"),
                )
            ),
        },
        "supply": {
            "supplier_name": order.supplier_name if order else payload.get("supplier_name"),
            "purchase_price": (
                line.purchase_price if line else _decimal(payload.get("purchase_price"))
            ),
            "currency": line.currency if line else payload.get("currency"),
            "profitability_pct": _decimal(payload.get("profitability_pct")),
            "supplier_prepare_days": payload.get("supplier_prepare_days"),
            "logistics_days": payload.get("logistics_days"),
            "lead_time_days": payload.get("lead_time_days"),
            "lead_time_confidence": payload.get("lead_time_confidence"),
            "receipt_documents": list(
                payload.get("receipt_documents") or payload.get("supplier_receipt_documents") or []
            ),
        },
        "family": {
            "id": family.get("family_id"),
            "label": family.get("family_label"),
            "member_count": family.get("registry_member_count"),
            "recommendation": family.get("reason_ru"),
        },
        "blockers": blockers,
        "orders": related_orders,
        "recommendation": (line.recommendation_reason if line else None) or reason,
        "source": {
            "state": source_state,
            "calculated_at": calculated_at,
            "updated_at": _date_text(line.updated_at if line else classification.get("updated_at")),
        },
    }


def _active_family_member_rows(
    db: Session,
    *,
    nomenclature_code: str,
) -> dict[str, Any] | None:
    """Read family membership from the active registry, never from product names."""

    required_tables = (
        DisplayFamilyRegistryVersion.__tablename__,
        DisplayFamily.__tablename__,
        DisplayFamilyMember.__tablename__,
        Product.__tablename__,
    )
    inspector = inspect(db.get_bind())
    if not all(inspector.has_table(table_name) for table_name in required_tables):
        return None
    active = db.scalar(
        select(DisplayFamilyRegistryVersion).where(DisplayFamilyRegistryVersion.status == "active")
    )
    if active is None:
        return None
    family_id = db.scalar(
        select(DisplayFamilyMember.family_id)
        .join(Product, Product.id == DisplayFamilyMember.product_id)
        .where(
            DisplayFamilyMember.registry_version_id == active.id,
            Product.code_1c == nomenclature_code,
        )
    )
    if family_id is None:
        return None
    family = db.scalar(
        select(DisplayFamily).where(
            DisplayFamily.id == family_id,
            DisplayFamily.registry_version_id == active.id,
        )
    )
    if family is None:
        return None
    raw_members = db.execute(
        select(Product.code_1c, Product.name, Product.article)
        .join(DisplayFamilyMember, DisplayFamilyMember.product_id == Product.id)
        .where(
            DisplayFamilyMember.registry_version_id == active.id,
            DisplayFamilyMember.family_id == family_id,
            Product.code_1c.is_not(None),
        )
        .order_by(Product.code_1c, Product.id)
    ).all()
    members: list[dict[str, str]] = []
    seen_codes: set[str] = set()
    for row in raw_members:
        code = str(row.code_1c or "").strip()
        if not code or code in seen_codes:
            continue
        seen_codes.add(code)
        members.append(
            {
                "nomenclature_code": code,
                "name": str(row.name or "").strip(),
                "article": str(row.article or "").strip(),
            }
        )
    labels = sorted(
        {
            " ".join(
                str(value).strip()
                for value in (model.get("brand"), model.get("model_name"), model.get("variant"))
                if value is not None and str(value).strip()
            )
            for model in list(family.phone_models_json or [])
            if isinstance(model, Mapping)
        }
        - {""}
    )
    return {
        "registry_version_id": active.id,
        "registry_version_number": active.version_number,
        "registry_inventory_checksum": active.inventory_checksum,
        "family_record_id": family.id,
        "family_key": family.family_key,
        "label": ", ".join(labels) or family.family_key,
        "member_count": family.member_count,
        "members": members,
    }


def _empty_product_card_snapshot(member: Mapping[str, Any]) -> dict[str, Any]:
    """Keep a family member visible when its calculated facts are unavailable."""

    return {
        "identity": {
            "bitrix_product_id": "",
            "xml_id": "",
            "nomenclature_code": str(member.get("nomenclature_code") or "").strip(),
            "name": str(member.get("name") or "").strip(),
            "article": str(member.get("article") or "").strip(),
            "photo_url": None,
            "website_url": None,
            "bitrix_url": None,
        },
        "properties": {},
        "lifecycle": {},
        "demand": {},
        "quality": {},
        "supply": {},
        "family": {},
        "blockers": [],
        "orders": [],
        "recommendation": None,
        "source": {"state": "missing", "calculated_at": None, "updated_at": None},
    }


def _comparison_card(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Copy matrix data without recursively nesting comparison members."""

    family = {
        key: value
        for key, value in dict(snapshot.get("family") or {}).items()
        if key != "comparison_members"
    }
    return {
        "identity": dict(snapshot.get("identity") or {}),
        "properties": dict(snapshot.get("properties") or {}),
        "lifecycle": dict(snapshot.get("lifecycle") or {}),
        "demand": dict(snapshot.get("demand") or {}),
        "quality": dict(snapshot.get("quality") or {}),
        "supply": dict(snapshot.get("supply") or {}),
        "family": family,
        "blockers": list(snapshot.get("blockers") or []),
        "orders": list(snapshot.get("orders") or []),
        "recommendation": snapshot.get("recommendation"),
        "source": dict(snapshot.get("source") or {}),
    }


def _matching_lines(
    db: Session,
    *,
    product_id: str | None,
    xml_id: str | None,
    nomenclature_code: str | None,
) -> list[ProcurementOrderFormationLine]:
    statement = _line_statement()
    if product_id:
        statement = statement.where(
            ProcurementOrderFormationLine.bitrix_product_id == str(product_id).strip()
        )
    elif nomenclature_code:
        statement = statement.where(
            ProcurementOrderFormationLine.nomenclature_code == nomenclature_code.strip()
        )
    elif xml_id:
        normalized = normalize_guid(xml_id)
        rows = list(db.scalars(statement).unique().all())
        return [
            row
            for row in rows
            if normalize_guid(row.bitrix_product_xml_id or row.nomenclature_ref) == normalized
        ]
    else:
        raise ValueError("product_id, xml_id or nomenclature_code is required")
    return list(db.scalars(statement).unique().all())


def _all_product_lines(db: Session) -> list[ProcurementOrderFormationLine]:
    return list(
        db.scalars(
            _line_statement().where(
                ProcurementOrderFormationLine.removed.is_(False),
                ProcurementOrderFormationLine.bitrix_product_id.is_not(None),
            )
        )
        .unique()
        .all()
    )


def _line_statement():
    return select(ProcurementOrderFormationLine).options(
        selectinload(ProcurementOrderFormationLine.order),
        selectinload(ProcurementOrderFormationLine.classification_proposals),
    )


def _latest_line(
    lines: Sequence[ProcurementOrderFormationLine],
) -> ProcurementOrderFormationLine | None:
    candidates = [
        line for line in lines if not line.removed and line.order.status in ACTIVE_ORDER_STATUSES
    ] or [line for line in lines if not line.removed]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: (
            item.order.order_date,
            item.updated_at or item.created_at,
            item.id or 0,
        ),
    )


def _classification_row(
    db: Session,
    *,
    nomenclature_code: str | None,
    xml_id: str | None = None,
) -> dict[str, Any] | None:
    code = str(nomenclature_code or "").strip()
    if not _classification_table_exists(db):
        return None
    if code:
        row = (
            db.execute(
                select(ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE).where(
                    ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE.c.nomenclature_code == code
                )
            )
            .mappings()
            .first()
        )
        if row:
            return dict(row)
    normalized_xml_id = normalize_guid(xml_id)
    if not normalized_xml_id:
        return None
    rows = db.execute(select(ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE)).mappings().all()
    for row in rows:
        if normalize_guid(row.get("product_ref")) == normalized_xml_id:
            return dict(row)
    return None


def _classification_rows(db: Session, *, scope: str) -> list[dict[str, Any]]:
    if not _classification_table_exists(db):
        return []
    statement = select(ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE)
    rows = [dict(row) for row in db.execute(statement).mappings().all()]
    if scope == "all":
        return rows
    return [row for row in rows if "диспле" in str(row.get("folder") or "").casefold()]


def _classification_table_exists(db: Session) -> bool:
    bind = db.get_bind()
    return inspect(bind).has_table(ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE.name)


def _product_card_sync_state_table_exists(db: Session) -> bool:
    bind = db.get_bind()
    return inspect(bind).has_table(ProcurementProductCardSyncState.__tablename__)


def _line_is_display(line: ProcurementOrderFormationLine) -> bool:
    payload = dict(line.payload or {})
    if isinstance(payload.get("display_family_recommendation"), Mapping):
        return True
    return (
        "диспле"
        in " ".join(
            str(payload.get(key) or "") for key in ("folder", "source_folder", "subject")
        ).casefold()
    )


def _related_orders(
    lines: Sequence[ProcurementOrderFormationLine], *, catalog_id: int
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for line in sorted(lines, key=lambda item: item.order.order_date, reverse=True):
        order = line.order
        if order.id in seen or order.status == "superseded":
            continue
        seen.add(order.id)
        rows.append(
            {
                "order_id": order.id,
                "label": (
                    f"Заказ {order.onec_document_number}"
                    if order.onec_document_number
                    else f"Проект #{order.id}"
                ),
                "status": order.status,
                "onec_status": order.onec_status,
                "onec_document_number": order.onec_document_number,
                "bitrix_process_url": order.bitrix_item_url,
                "app_url": f"/bitrix/procurement-order-formation/orders/{order.id}",
            }
        )
    return rows


def _classification_blockers(classification: Mapping[str, Any]) -> list[dict[str, Any]]:
    codes = list(classification.get("blockers") or []) + list(
        classification.get("export_blockers") or []
    )
    return [
        {
            "code": str(code),
            "scope": "product",
            "severity": "hard",
            "line_id": None,
            "line_number": None,
            "message": str(code).replace("_", " "),
            "evidence": {},
            "resolution_actions": [],
        }
        for code in dict.fromkeys(codes)
    ]


def _first_photo(
    line: ProcurementOrderFormationLine | None,
    product: BitrixCatalogProduct | None,
) -> str | None:
    payload = dict(line.payload or {}) if line is not None else {}
    photos = payload.get("photos")
    if isinstance(photos, list):
        for photo in photos:
            if isinstance(photo, Mapping):
                value = str(photo.get("thumbnail") or photo.get("original") or "").strip()
                if value:
                    return value
    if product:
        return product.photo_thumbnail_url or product.photo_original_url or None
    return None


def _source_state(
    calculated_at: Any, *, blockers: Sequence[Mapping[str, Any]], settings: Settings
) -> str:
    if not calculated_at:
        return "missing"
    parsed = _parse_datetime(calculated_at)
    if parsed and datetime.now(UTC) - parsed > timedelta(
        hours=settings.procurement_product_card_stale_hours
    ):
        return "stale"
    if any(str(item.get("severity") or "") == "technical" for item in blockers):
        return "partial"
    return "ready"


def _parse_datetime(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.combine(date.fromisoformat(raw), datetime.min.time())
        except ValueError:
            return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).replace(" ", "").replace(",", "."))
    except (InvalidOperation, ValueError):
        return None


def _first_known(*values: Any) -> Any:
    return next((value for value in values if value not in (None, "")), None)


def _canonical_guid(value: Any) -> str | None:
    try:
        return str(UUID(normalize_guid(value)))
    except (TypeError, ValueError, AttributeError):
        return None


def _rate(value: Decimal | None, days: int) -> Decimal | None:
    return (value / Decimal(days)).quantize(Decimal("0.001")) if value is not None else None


def _date_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _product_card_mapping_path(settings: Settings) -> Path:
    path = Path(settings.procurement_product_card_mapping_path)
    return path if path.is_absolute() else REPO_ROOT / path


def _field_value(value: Any) -> str | int:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    return str(value).strip()[:4000]


def _payload_hash(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _sync_product_card_batch(
    entries: Sequence[dict[str, Any]],
    *,
    results: list[dict[str, Any]],
    caller: Callable[..., dict[str, Any]],
    settings: Settings,
) -> None:
    if not entries:
        return
    update_commands = {
        f"update_{index}": _product_update_command(
            str(entry["identity"]["bitrix_product_id"]),
            entry["mapped_fields"],
        )
        for index, entry in enumerate(entries)
    }
    try:
        update_results, update_errors = _call_bitrix_batch(
            update_commands,
            caller=caller,
            settings=settings,
        )
    except Exception as exc:
        for entry in entries:
            _fail_sync_entry(entry, exc, results=results)
        return

    readable: list[tuple[int, dict[str, Any]]] = []
    for index, entry in enumerate(entries):
        alias = f"update_{index}"
        error = update_errors.get(alias)
        if error is not None:
            _fail_sync_entry(
                entry,
                RuntimeError(_bitrix_batch_error(error)),
                results=results,
            )
            continue
        if update_results.get(alias) in (None, False):
            _fail_sync_entry(
                entry,
                RuntimeError("Bitrix product update returned an empty result"),
                results=results,
            )
            continue
        readable.append((index, entry))

    read_commands = {
        f"read_{index}": _product_read_command(str(entry["identity"]["bitrix_product_id"]))
        for index, entry in readable
    }
    if not read_commands:
        return
    try:
        read_results, read_errors = _call_bitrix_batch(
            read_commands,
            caller=caller,
            settings=settings,
        )
    except Exception as exc:
        for _index, entry in readable:
            _fail_sync_entry(entry, exc, results=results)
        return

    for index, entry in readable:
        alias = f"read_{index}"
        error = read_errors.get(alias)
        if error is not None:
            _fail_sync_entry(
                entry,
                RuntimeError(_bitrix_batch_error(error)),
                results=results,
            )
            continue
        row = read_results.get(alias)
        if not isinstance(row, Mapping):
            _fail_sync_entry(
                entry,
                RuntimeError("Bitrix product readback returned an empty result"),
                results=results,
            )
            continue
        mapped_fields = entry["mapped_fields"]
        readback = {
            str(code): _scalar(row.get(str(code), row.get(str(code).lower())))
            for code in mapped_fields
        }
        mismatches = {
            code: {"expected": expected, "actual": readback.get(code)}
            for code, expected in mapped_fields.items()
            if _comparable(readback.get(code)) != _comparable(expected)
        }
        state = entry["state"]
        state.readback_fields = readback
        state.status = "readback_mismatch" if mismatches else "synced"
        state.last_error = json.dumps(mismatches, ensure_ascii=False) if mismatches else None
        if not mismatches:
            state.last_success_at = entry["attempted_at"]
        results.append(
            {
                **entry["identity"],
                "status": state.status,
                "changed_fields": sorted(entry["logical_fields"]),
                "mismatches": mismatches,
            }
        )


def _call_bitrix_batch(
    commands: Mapping[str, str],
    *,
    caller: Callable[..., dict[str, Any]],
    settings: Settings,
) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = caller(
        "batch",
        {"halt": 0, "cmd": dict(commands)},
        settings=settings,
    ).get("result")
    if not isinstance(payload, Mapping):
        raise RuntimeError("Bitrix batch returned an empty result")
    raw_results = payload.get("result")
    raw_errors = payload.get("result_error")
    return (
        dict(raw_results) if isinstance(raw_results, Mapping) else {},
        dict(raw_errors) if isinstance(raw_errors, Mapping) else {},
    )


def _product_update_command(product_id: str, fields: Mapping[str, Any]) -> str:
    params: list[tuple[str, Any]] = [("id", int(product_id))]
    params.extend((f"fields[{code}]", value) for code, value in fields.items())
    return "crm.product.update?" + urllib.parse.urlencode(params)


def _product_read_command(product_id: str) -> str:
    return "crm.product.get?" + urllib.parse.urlencode({"id": int(product_id)})


def _bitrix_batch_error(error: Any) -> str:
    if isinstance(error, Mapping):
        code = str(error.get("error") or "").strip()
        description = str(error.get("error_description") or "").strip()
        return f"Bitrix batch command failed: {code} {description}".strip()
    return f"Bitrix batch command failed: {error}"


def _fail_sync_entry(
    entry: Mapping[str, Any],
    exc: Exception,
    *,
    results: list[dict[str, Any]],
) -> None:
    state = entry["state"]
    state.status = "failed"
    state.last_error = f"{type(exc).__name__}: {exc}"[:4000]
    results.append(
        {
            **entry["identity"],
            "status": "failed",
            "changed_fields": sorted(entry["logical_fields"]),
            "mismatches": {},
            "error": state.last_error,
        }
    )


def _scalar(value: Any) -> Any:
    if isinstance(value, list):
        return _scalar(value[0]) if value else None
    if isinstance(value, Mapping):
        for key in ("value", "VALUE", "text", "TEXT"):
            if key in value:
                return _scalar(value[key])
    return value


def _comparable(value: Any) -> str:
    decimal = _decimal(_scalar(value))
    if decimal is not None:
        return format(decimal.normalize(), "f")
    return str(_scalar(value) or "").strip()
