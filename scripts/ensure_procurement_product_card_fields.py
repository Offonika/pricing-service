#!/usr/bin/env python3
"""Discover or provision server-managed properties on the native Bitrix product card.

The command is read-only by default. ``--apply-fields`` and ``--apply-placement``
are independent, explicit external mutations.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.config import Settings, get_settings  # noqa: E402
from app.services.bitrix_order_formation import bitrix_call  # noqa: E402
from app.services.procurement_product_cards import PRODUCT_CARD_FIELD_SPECS  # noqa: E402


def parse_args() -> argparse.Namespace:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply-fields", action="store_true")
    parser.add_argument("--apply-placement", action="store_true")
    parser.add_argument("--handler-url", default="")
    parser.add_argument("--placement", default=settings.procurement_product_card_placement)
    parser.add_argument(
        "--mapping-path", type=Path, default=Path(settings.procurement_product_card_mapping_path)
    )
    return parser.parse_args()


def field_code(logical_key: str) -> str:
    return "MM_AUTO_" + logical_key.upper()


def existing_product_field_code(
    fields: Mapping[str, Mapping[str, Any]],
    spec: Mapping[str, Any],
) -> str | None:
    expected_code = field_code(str(spec["key"])).casefold()
    for code, details in fields.items():
        stable_values = (
            details.get("code"),
            details.get("CODE"),
            details.get("xmlId"),
            details.get("xml_id"),
            details.get("XML_ID"),
        )
        if any(str(value or "").strip().casefold() == expected_code for value in stable_values):
            return str(code)

    expected_title = str(spec["title"]).strip()
    for code, details in fields.items():
        title = str(details.get("title") or details.get("NAME") or "").strip()
        if title == expected_title:
            return str(code)
    return None


def discover_product_fields(
    *,
    caller: Callable[..., dict[str, Any]],
    settings: Settings,
) -> dict[str, dict[str, Any]]:
    result = caller("crm.product.fields", {}, settings=settings).get("result") or {}
    return {
        str(code): dict(details) for code, details in result.items() if isinstance(details, Mapping)
    }


def build_mapping(
    fields: Mapping[str, Mapping[str, Any]],
    *,
    catalog_id: int,
    placement: str,
) -> dict[str, Any]:
    mapped = {}
    for spec in PRODUCT_CARD_FIELD_SPECS:
        existing_code = existing_product_field_code(fields, spec)
        if existing_code is not None:
            mapped[spec["key"]] = existing_code
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "catalog_id": catalog_id,
        "fields": mapped,
        "missing_fields": [
            spec["key"] for spec in PRODUCT_CARD_FIELD_SPECS if spec["key"] not in mapped
        ],
        "field_specs": [dict(spec) for spec in PRODUCT_CARD_FIELD_SPECS],
        "placement": {"name": placement, "handler": None, "bound": False},
    }


def ensure_product_fields(
    *,
    caller: Callable[..., dict[str, Any]],
    settings: Settings,
) -> dict[str, dict[str, Any]]:
    fields = discover_product_fields(caller=caller, settings=settings)
    for spec in PRODUCT_CARD_FIELD_SPECS:
        if existing_product_field_code(fields, spec) is not None:
            continue
        result = caller(
            "crm.product.property.add",
            {
                "fields": {
                    "IBLOCK_ID": settings.procurement_product_card_catalog_id,
                    "NAME": spec["title"],
                    "ACTIVE": "Y",
                    "SORT": spec["sort"],
                    "CODE": field_code(spec["key"]),
                    "XML_ID": field_code(spec["key"]),
                    "PROPERTY_TYPE": spec["type"],
                    "MULTIPLE": "N",
                    "IS_REQUIRED": "N",
                }
            },
            settings=settings,
        ).get("result")
        if result in (None, False):
            raise RuntimeError(f"Bitrix did not create product property {spec['title']}")
    return discover_product_fields(caller=caller, settings=settings)


def ensure_placement(
    *,
    placement: str,
    handler_url: str,
    caller: Callable[..., dict[str, Any]],
    settings: Settings,
) -> bool:
    if not placement:
        raise ValueError("product card placement is empty")
    if not handler_url.startswith("https://"):
        raise ValueError("handler URL must use https")
    rows = caller("placement.get", {}, settings=settings).get("result") or []
    normalized = handler_url.rstrip("/")
    if any(
        str(item.get("placement") or "") == placement
        and str(item.get("handler") or "").rstrip("/") == normalized
        for item in rows
        if isinstance(item, Mapping)
    ):
        return False
    result = caller(
        "placement.bind",
        {
            "PLACEMENT": placement,
            "HANDLER": handler_url,
            "TITLE": "Показатели товара",
            "DESCRIPTION": "Спрос, качество, поставка и блокеры",
            "LANG_ALL": {
                "ru": {
                    "TITLE": "Показатели товара",
                    "DESCRIPTION": "Спрос, качество, поставка и блокеры",
                }
            },
        },
        settings=settings,
    ).get("result")
    if result is not True:
        raise RuntimeError("Bitrix product-card placement binding failed")
    return True


def write_mapping(path: Path, payload: Mapping[str, Any]) -> None:
    if not path.is_absolute():
        path = REPO_ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    args = parse_args()
    settings = get_settings()
    fields = (
        ensure_product_fields(caller=bitrix_call, settings=settings)
        if args.apply_fields
        else discover_product_fields(caller=bitrix_call, settings=settings)
    )
    mapping = build_mapping(
        fields,
        catalog_id=settings.procurement_product_card_catalog_id,
        placement=args.placement,
    )
    if args.apply_placement:
        changed = ensure_placement(
            placement=args.placement,
            handler_url=args.handler_url,
            caller=bitrix_call,
            settings=settings,
        )
        mapping["placement"] = {
            "name": args.placement,
            "handler": args.handler_url,
            "bound": True,
            "changed": changed,
        }
    write_mapping(args.mapping_path, mapping)
    print(json.dumps(mapping, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
