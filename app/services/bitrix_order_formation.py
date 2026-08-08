from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.config import Settings, get_settings
from app.models.procurement_order_formation import (
    ProcurementClassificationProposal,
    ProcurementLifecycleTransitionProposal,
    ProcurementOrderFormation,
)
from app.services.procurement_order_formation import (
    LIFECYCLE_STATUS_LABELS,
    MANUAL_STATUS_LABELS,
    get_order,
    normalize_guid,
    normalize_status,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class BitrixCatalogProduct:
    product_id: str
    name: str
    xml_id: str
    assortment_status: str = ""
    quality: str = ""
    procurement_profile: str = ""
    manual_minimum: Decimal | None = None
    photo_thumbnail_url: str = ""
    photo_original_url: str = ""
    raw: dict[str, Any] | None = None


def load_order_formation_mapping(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    path = Path(settings.procurement_order_formation_mapping_path)
    if not path.is_absolute():
        path = REPO_ROOT / path
    if not path.exists():
        raise RuntimeError(f"order formation mapping does not exist: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("order formation mapping must be a JSON object")
    return payload


def resolve_catalog_product_by_xml_id(
    xml_id: str,
    *,
    settings: Settings | None = None,
    mapping: dict[str, Any] | None = None,
) -> BitrixCatalogProduct | None:
    settings = settings or get_settings()
    mapping = mapping or load_order_formation_mapping(settings)
    normalized_guid = normalize_guid(xml_id)
    if not normalized_guid:
        raise ValueError("1C nomenclature GUID is required")
    catalog = mapping.get("catalog") or {}
    selected = list(
        dict.fromkeys(
            [
                *(
                    str(value)
                    for key, value in catalog.items()
                    if key != "catalog_id" and str(value).strip()
                ),
                "PREVIEW_PICTURE",
                "DETAIL_PICTURE",
            ]
        )
    )
    result = bitrix_call(
        "crm.product.list",
        {
            "filter": {"XML_ID": normalized_guid},
            "select": selected,
        },
        settings=settings,
    ).get("result")
    rows = result if isinstance(result, list) else []
    exact = [
        row
        for row in rows
        if normalize_guid(_value(row, str(catalog.get("xml_id") or "XML_ID"))) == normalized_guid
    ]
    if not exact:
        return None
    if len(exact) > 1:
        raise RuntimeError(f"multiple Bitrix catalog products have XML_ID {xml_id}")
    return _catalog_product_from_row(exact[0], catalog=catalog)


def resolve_catalog_products_by_xml_ids(
    xml_ids: list[str],
    *,
    settings: Settings | None = None,
    mapping: dict[str, Any] | None = None,
    chunk_size: int = 40,
) -> dict[str, BitrixCatalogProduct]:
    """Resolve catalog products in bounded batches keyed by normalized 1C GUID."""

    settings = settings or get_settings()
    mapping = mapping or load_order_formation_mapping(settings)
    normalized_ids = list(
        dict.fromkeys(normalized for xml_id in xml_ids if (normalized := normalize_guid(xml_id)))
    )
    if not normalized_ids:
        return {}
    if chunk_size < 1:
        raise ValueError("chunk_size must be positive")

    catalog = mapping.get("catalog") or {}
    selected = list(
        dict.fromkeys(
            [
                *(
                    str(value)
                    for key, value in catalog.items()
                    if key != "catalog_id" and str(value).strip()
                ),
                "PREVIEW_PICTURE",
                "DETAIL_PICTURE",
            ]
        )
    )
    xml_field = str(catalog.get("xml_id") or "XML_ID")
    resolved: dict[str, BitrixCatalogProduct] = {}
    for start in range(0, len(normalized_ids), chunk_size):
        chunk = normalized_ids[start : start + chunk_size]
        commands = {
            f"catalog_{index}": "crm.product.list?"
            + urllib.parse.urlencode(
                _flatten_params(
                    {
                        "filter": {xml_field: normalized_guid},
                        "select": selected,
                    }
                )
            )
            for index, normalized_guid in enumerate(chunk)
        }
        payload = bitrix_call(
            "batch",
            {"halt": 1, "cmd": commands},
            settings=settings,
        )
        batch_payload = payload.get("result") or {}
        batch_errors = batch_payload.get("result_error") or {}
        if batch_errors:
            first_key = sorted(batch_errors)[0]
            raise RuntimeError(
                f"Bitrix catalog batch failed at {first_key}: {batch_errors[first_key]}"
            )
        batch_results = batch_payload.get("result") or {}
        for index, requested_guid in enumerate(chunk):
            result = batch_results.get(f"catalog_{index}")
            rows = result if isinstance(result, list) else []
            exact = [
                row for row in rows if normalize_guid(_value(row, xml_field)) == requested_guid
            ]
            if len(exact) > 1:
                raise RuntimeError(f"multiple Bitrix catalog products have XML_ID {requested_guid}")
            if exact:
                resolved[requested_guid] = _catalog_product_from_row(exact[0], catalog=catalog)
    return resolved


def _catalog_product_from_row(
    row: dict[str, Any], *, catalog: dict[str, Any]
) -> BitrixCatalogProduct:
    enum_values = catalog.get("enum_values") or {}
    return BitrixCatalogProduct(
        product_id=str(_value(row, str(catalog.get("product_id") or "ID")) or "").strip(),
        name=str(_value(row, str(catalog.get("name") or "NAME")) or "").strip(),
        xml_id=str(_value(row, str(catalog.get("xml_id") or "XML_ID")) or "").strip(),
        assortment_status=_decoded_property_value(
            row,
            field_name=str(catalog.get("assortment_status") or ""),
            values=enum_values.get("assortment_status") or {},
        ),
        quality=_decoded_property_value(
            row,
            field_name=str(catalog.get("quality") or ""),
            values=enum_values.get("quality") or {},
        ),
        procurement_profile=_decoded_property_value(
            row,
            field_name=str(catalog.get("procurement_profile") or ""),
            values=enum_values.get("procurement_profile") or {},
        ),
        manual_minimum=_decimal_or_none(
            _scalar(_value(row, str(catalog.get("manual_minimum") or "")))
        ),
        photo_thumbnail_url=_file_url(_value(row, "PREVIEW_PICTURE")),
        photo_original_url=_file_url(_value(row, "DETAIL_PICTURE")),
        raw=row,
    )


def create_or_update_bitrix_card(
    db: Session,
    order_id: int,
    *,
    apply: bool = False,
    settings: Settings | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    mapping = load_order_formation_mapping(settings)
    order = get_order(db, order_id)
    fields = build_bitrix_item_fields(order, mapping=mapping)
    product_rows = build_bitrix_product_rows(order)
    preview = {
        "entity_type_id": int((mapping.get("process") or {}).get("entity_type_id") or 0),
        "item_id": order.bitrix_item_id,
        "fields": fields,
        "product_rows": product_rows,
    }
    if not apply:
        return {**preview, "dry_run": True}
    if not preview["entity_type_id"]:
        raise RuntimeError("Bitrix entity_type_id is missing in mapping")
    if order.bitrix_item_id:
        bitrix_call(
            "crm.item.update",
            {
                "entityTypeId": preview["entity_type_id"],
                "id": order.bitrix_item_id,
                "fields": fields,
            },
            settings=settings,
        )
        item_id = order.bitrix_item_id
    else:
        response = bitrix_call(
            "crm.item.add",
            {"entityTypeId": preview["entity_type_id"], "fields": fields},
            settings=settings,
        )
        item = (response.get("result") or {}).get("item") or {}
        item_id = str(item.get("id") or "").strip()
        if not item_id:
            raise RuntimeError("crm.item.add returned no item id")
        order.bitrix_entity_type_id = preview["entity_type_id"]
        order.bitrix_item_id = item_id
        order.bitrix_category_id = int((mapping.get("process") or {}).get("category_id") or 0)
        order.bitrix_item_url = _bitrix_item_url(
            entity_type_id=preview["entity_type_id"], item_id=item_id, settings=settings
        )
        db.commit()
    sync_bitrix_product_rows(order, mapping=mapping, settings=settings)
    return {**preview, "item_id": item_id, "dry_run": False}


def build_bitrix_item_fields(
    order: ProcurementOrderFormation,
    *,
    mapping: dict[str, Any],
) -> dict[str, Any]:
    fields_mapping = mapping.get("fields") or {}
    process = mapping.get("process") or {}
    stage_map = mapping.get("stage_map") or {}
    fields: dict[str, Any] = {
        "title": f"Заказ поставщику: {order.supplier_name} — {order.order_date.isoformat()}",
        "categoryId": int(process.get("category_id") or 0),
        "stageId": stage_map.get(order.status) or stage_map.get("draft"),
    }
    if order.responsible_bitrix_user_id:
        fields["assignedById"] = order.responsible_bitrix_user_id
    values = {
        "backend_order_id": order.id,
        "stable_key": order.stable_key,
        "version": order.version,
        "supplier_ref": order.supplier_ref,
        "supplier_code": order.supplier_code,
        "supplier_name": order.supplier_name,
        "contract_ref": order.contract_ref,
        "contract_code": order.contract_code,
        "contract_name": order.contract_name,
        "currency": order.currency,
        "warehouse_ref": order.warehouse_ref,
        "warehouse_code": order.warehouse_code,
        "warehouse_name": order.warehouse_name,
        "procurement_contour": order.procurement_contour,
        "route": order.route,
        "batch_id": order.batch_id,
        "order_date": order.order_date.isoformat(),
        "calculation_id": order.calculation_id,
        "source_run_id": order.source_run_id,
        "approved_version": order.approved_version,
        "approved_by": order.approved_by_name,
        "approved_at": order.approved_at.isoformat() if order.approved_at else None,
        "connector_status": order.onec_status,
        "onec_message_id": order.onec_message_id,
        "onec_document_ref": order.onec_document_ref,
        "onec_document_number": order.onec_document_number,
        "onec_error": order.onec_error,
    }
    for logical_key, value in values.items():
        field_name = str(fields_mapping.get(logical_key) or "").strip()
        if field_name:
            fields[crm_item_rest_field_name(field_name)] = "" if value is None else value
    return {key: value for key, value in fields.items() if value not in (None, "")}


def build_bitrix_product_rows(order: ProcurementOrderFormation) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in order.lines:
        if line.removed:
            continue
        if not str(line.bitrix_product_id or "").strip():
            raise ValueError(f"line {line.line_number} has no Bitrix catalog product")
        if normalize_guid(line.bitrix_product_xml_id) != normalize_guid(line.nomenclature_ref):
            raise ValueError(f"line {line.line_number} XML_ID does not match 1C GUID")
        rows.append(
            {
                "productId": int(str(line.bitrix_product_id)),
                "productName": line.nomenclature_name,
                "price": str(line.purchase_price),
                "quantity": str(line.final_quantity),
                "currencyId": line.currency,
                "sort": line.line_number * 10,
            }
        )
    return rows


def sync_bitrix_product_rows(
    order: ProcurementOrderFormation,
    *,
    mapping: dict[str, Any] | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    mapping = mapping or load_order_formation_mapping(settings)
    if not order.bitrix_item_id:
        raise ValueError("Bitrix item id is required before product row sync")
    owner_type = str((mapping.get("process") or {}).get("owner_type") or "").strip()
    if not owner_type:
        raise RuntimeError("Bitrix owner_type is missing in order formation mapping")
    return bitrix_call(
        "crm.item.productrow.set",
        {
            "ownerId": int(order.bitrix_item_id),
            "ownerType": owner_type,
            "productRows": build_bitrix_product_rows(order),
        },
        settings=settings,
    )


def refresh_line_catalog_snapshot(
    db: Session,
    order_id: int,
    *,
    settings: Settings | None = None,
) -> ProcurementOrderFormation:
    settings = settings or get_settings()
    mapping = load_order_formation_mapping(settings)
    order = get_order(db, order_id)
    for line in order.lines:
        product = resolve_catalog_product_by_xml_id(
            line.nomenclature_ref,
            settings=settings,
            mapping=mapping,
        )
        if product is None:
            line.bitrix_product_id = None
            line.blockers = list(dict.fromkeys([*(line.blockers or []), "catalog_product_missing"]))
            continue
        line.bitrix_product_id = product.product_id
        line.bitrix_product_xml_id = product.xml_id
        line.assortment_status = product.assortment_status or line.assortment_status
        line.quality = product.quality or line.quality
        line.procurement_profile = product.procurement_profile or line.procurement_profile
        line.manual_minimum = (
            product.manual_minimum if product.manual_minimum is not None else line.manual_minimum
        )
        line.blockers = [
            blocker
            for blocker in (line.blockers or [])
            if blocker not in {"catalog_product_missing", "catalog_xml_id_mismatch"}
        ]
    db.commit()
    return get_order(db, order_id)


_KNOWN_STATUS_CODES = frozenset({**MANUAL_STATUS_LABELS, **LIFECYCLE_STATUS_LABELS})

_UNRECOGNIZED_READBACK_BLOCKER = "bitrix_readback_unrecognized_status"


def _is_unrecognized_bitrix_status(raw_value: str | None) -> bool:
    """True if a non-empty Bitrix readback value can never match any known status.

    ``normalize_status`` falls back to returning the raw text unchanged when it
    can't resolve a code or label (e.g. a legacy enum value like "Эксклюзив"
    that used to be a lifecycle status and no longer is). Such values will
    never equal ``normalize_status(<any real status>)``, so without this check
    the row stays "pending" forever, indistinguishable from a genuinely
    in-flight 1С update.
    """

    text = (raw_value or "").strip()
    if not text:
        return False
    return normalize_status(text) not in _KNOWN_STATUS_CODES


def reflect_classifications_from_bitrix(
    db: Session,
    *,
    settings: Settings | None = None,
) -> dict[str, int]:
    settings = settings or get_settings()
    mapping = load_order_formation_mapping(settings)
    proposals = db.scalars(
        select(ProcurementClassificationProposal)
        .options(selectinload(ProcurementClassificationProposal.line))
        .where(ProcurementClassificationProposal.status == "applied")
    ).all()
    reflected = 0
    pending = 0
    missing = 0
    unrecognized = 0
    for proposal in proposals:
        product = resolve_catalog_product_by_xml_id(
            proposal.line.bitrix_product_xml_id or proposal.line.nomenclature_ref,
            settings=settings,
            mapping=mapping,
        )
        if product is None:
            missing += 1
            continue
        proposal.bitrix_readback_value = product.assortment_status
        if normalize_status(product.assortment_status) == normalize_status(
            proposal.proposed_status
        ):
            proposal.status = "reflected"
            proposal.reflected_at = datetime.now(UTC).replace(tzinfo=None)
            proposal.line.assortment_status = proposal.proposed_status
            if proposal.manual_minimum is not None:
                proposal.line.manual_minimum = proposal.manual_minimum
            reflected += 1
        elif _is_unrecognized_bitrix_status(product.assortment_status):
            unrecognized += 1
        else:
            pending += 1

    transitions = db.scalars(
        select(ProcurementLifecycleTransitionProposal).where(
            ProcurementLifecycleTransitionProposal.status == "applied"
        )
    ).all()
    for proposal in transitions:
        product = resolve_catalog_product_by_xml_id(
            proposal.product_guid or proposal.nomenclature_ref or "",
            settings=settings,
            mapping=mapping,
        )
        if product is None:
            missing += 1
            continue
        proposal.bitrix_readback_value = product.assortment_status
        if normalize_status(product.assortment_status) == normalize_status(proposal.target_status):
            proposal.status = "reflected"
            proposal.reflected_at = datetime.now(UTC).replace(tzinfo=None)
            reflected += 1
        elif _is_unrecognized_bitrix_status(product.assortment_status):
            unrecognized += 1
            if _UNRECOGNIZED_READBACK_BLOCKER not in proposal.blockers:
                proposal.blockers = [*proposal.blockers, _UNRECOGNIZED_READBACK_BLOCKER]
        else:
            pending += 1
    db.commit()
    return {
        "reflected": reflected,
        "pending": pending,
        "missing": missing,
        "unrecognized": unrecognized,
    }


def bitrix_call(
    method: str,
    params: dict[str, Any] | None = None,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    webhook = (
        settings.procurement_labels_bitrix_webhook_url
        or settings.procurement_bitrix_webhook_url
        or settings.bitrix_box_webhook_base
        or ""
    ).strip()
    if not webhook:
        raise RuntimeError("Bitrix procurement webhook is not configured")
    body = urllib.parse.urlencode(_flatten_params(params or {})).encode("utf-8")
    request = urllib.request.Request(
        webhook.rstrip("/") + f"/{method}.json",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=settings.procurement_labels_bitrix_rest_timeout_seconds,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Bitrix API {method}: HTTP {exc.code} {response_body[:500]}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Bitrix API {method} is unavailable") from exc
    if payload.get("error"):
        raise RuntimeError(
            f"Bitrix API {method}: {payload['error']} {payload.get('error_description', '')}".strip()
        )
    return payload


def crm_item_rest_field_name(field: str) -> str:
    raw = str(field or "").strip()
    builtins = {
        "ID": "id",
        "TITLE": "title",
        "STAGE_ID": "stageId",
        "CATEGORY_ID": "categoryId",
        "ASSIGNED_BY_ID": "assignedById",
    }
    upper = raw.upper()
    if upper in builtins:
        return builtins[upper]
    if upper.startswith("UF_CRM_"):
        parts = [part for part in raw.split("_")[2:] if part]
        return "ufCrm" + "".join(part[:1].upper() + part[1:].lower() for part in parts)
    return raw


def _flatten_params(params: dict[str, Any]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for key, value in params.items():
        rows.extend(_flatten_param(key, value))
    return rows


def _flatten_param(prefix: str, value: Any) -> list[tuple[str, str]]:
    if isinstance(value, dict):
        rows: list[tuple[str, str]] = []
        for key, child in value.items():
            rows.extend(_flatten_param(f"{prefix}[{key}]", child))
        return rows
    if isinstance(value, list):
        rows = []
        for index, child in enumerate(value):
            rows.extend(_flatten_param(f"{prefix}[{index}]", child))
        return rows
    return [(prefix, "" if value is None else str(value))]


def _value(row: dict[str, Any], field_name: str) -> Any:
    if not field_name:
        return None
    return row.get(field_name, row.get(field_name.lower()))


def _scalar(value: Any) -> Any:
    if isinstance(value, list):
        return _scalar(value[0]) if value else None
    if isinstance(value, dict):
        for key in ("value", "VALUE", "name", "NAME", "text", "TEXT"):
            if key in value:
                return _scalar(value[key])
        return next(iter(value.values()), None)
    return value


def _decimal_or_none(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).replace(" ", "").replace(",", "."))
    except (InvalidOperation, ValueError):
        return None


def _file_url(value: Any) -> str:
    if isinstance(value, list):
        for item in value:
            if url := _file_url(item):
                return url
        return ""
    if isinstance(value, dict):
        for key in (
            "downloadUrl",
            "download_url",
            "showUrl",
            "show_url",
            "src",
            "url",
            "URL",
        ):
            if url := _file_url(value.get(key)):
                return url
        return ""
    text = str(value or "").strip()
    return text if text.startswith(("https://", "http://", "/")) else ""


def _decoded_property_value(row: dict[str, Any], *, field_name: str, values: dict[str, str]) -> str:
    raw = _value(row, field_name)
    scalar = str(_scalar(raw) or "").strip()
    if isinstance(raw, dict):
        value_id = str(raw.get("value") or raw.get("VALUE") or "").strip()
        if values.get(value_id):
            return str(values[value_id]).strip()
    return str(values.get(scalar) or scalar).strip()


def _bitrix_item_url(*, entity_type_id: int, item_id: str, settings: Settings) -> str:
    webhook = (
        settings.procurement_labels_bitrix_webhook_url
        or settings.procurement_bitrix_webhook_url
        or settings.bitrix_box_webhook_base
        or ""
    )
    parsed = urllib.parse.urlparse(webhook)
    if not parsed.netloc:
        return ""
    return (
        f"{parsed.scheme or 'https'}://{parsed.netloc}/crm/type/{entity_type_id}/details/{item_id}/"
    )
