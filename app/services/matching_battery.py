from __future__ import annotations

import re
from typing import Any

from app.models import CompetitorItem, Product


def extract_battery_capacity(text: str | None) -> int | None:
    if not text:
        return None
    match = re.search(r"(\d{3,5})\s*(mah|мач)", text.lower())
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def battery_capacity_conflict(
    item_text: str,
    product_text: str,
    attrs: dict[str, Any] | None,
) -> bool:
    attr_capacity = None
    if attrs:
        raw = attrs.get("capacity")
        if raw:
            match = re.search(r"(\d{3,5})", str(raw))
            if match:
                try:
                    attr_capacity = int(match.group(1))
                except ValueError:
                    attr_capacity = None
    item_capacity = attr_capacity or extract_battery_capacity(item_text)
    product_capacity = extract_battery_capacity(product_text)
    if item_capacity and product_capacity:
        return abs(item_capacity - product_capacity) >= 200
    return False


def battery_part_codes_from_text(text: str | None) -> set[str]:
    normalized = (text or "").lower()
    codes: set[str] = set()
    patterns = (
        r"\bli[0-9][a-z0-9]{8,}\b",
        r"\bbl-?[0-9]{2}[a-z]{1,4}\b",
        r"\bblp[0-9]{3,5}\b",
        r"\bhb[0-9][a-z0-9]{7,}\b",
        r"\bbm[0-9a-z]{2,5}\b",
        r"\bbn[0-9a-z]{2,5}\b",
        r"\bbp[0-9a-z]{2,5}\b",
    )
    for pattern in patterns:
        codes.update(match.group(0).lower() for match in re.finditer(pattern, normalized))
    return codes


def competitor_battery_part_codes(item: CompetitorItem) -> set[str]:
    return set().union(
        *(
            battery_part_codes_from_text(value)
            for value in (item.name, item.normalized_title, item.external_id)
            if value
        )
    )


def product_battery_part_codes(product: Product) -> set[str]:
    return battery_part_codes_from_text(product.name)


def battery_part_code_conflict(product: Product, competitor_codes: set[str]) -> bool:
    product_codes = product_battery_part_codes(product)
    return bool(competitor_codes and product_codes and competitor_codes.isdisjoint(product_codes))


def text_has_battery_part_signal(text: str | None) -> bool:
    normalized = (text or "").lower().replace("ё", "е")
    return bool(re.search(r"\b(?:акб|battery)\b|аккумулятор", normalized))


def product_has_battery_part_signal(product: Product) -> bool:
    return text_has_battery_part_signal(
        " ".join(
            str(value)
            for value in (
                product.name,
                product.subject,
                product.subject_1c,
                product.subject_generated,
                product.category,
            )
            if value
        )
    )


def battery_subject_conflict_reason(
    item: CompetitorItem,
    product: Product,
    *,
    effective_item_type: str | None = None,
    item_text: str | None = None,
) -> str | None:
    resolved_item_type = effective_item_type or (item.item_type or "").strip().lower()
    if resolved_item_type != "battery":
        return None
    resolved_item_text = item_text or " ".join(
        value
        for value in (item.name, item.normalized_title, item.category, item.category_group)
        if value
    )
    if not text_has_battery_part_signal(resolved_item_text):
        return None
    if product_has_battery_part_signal(product):
        return None
    return "battery_vs_non_battery_product"


def premium_battery_product_signal(product: Product) -> bool:
    normalized = " ".join(
        str(value) for value in (product.name, product.quality, product.quality_raw) if value
    ).lower()
    return bool(re.search(r"\bpremium\b|\bпремиум\b", normalized))


def premium_battery_item_signal(item: CompetitorItem) -> bool:
    normalized = " ".join(value for value in (item.name, item.normalized_title) if value).lower()
    return bool(
        re.search(r"\bpremium\b|\bпремиум\b", normalized) or re.search(r"\bzevo\b", normalized)
    )


def battery_premium_tier_conflict(item: CompetitorItem, product: Product) -> bool:
    return premium_battery_item_signal(item) != premium_battery_product_signal(product)


def battery_pair_diagnostic_reasons(item: CompetitorItem, product: Product) -> list[str]:
    if (item.item_type or "").strip().lower() != "battery":
        return []

    reasons: list[str] = []
    item_text = " ".join(
        value for value in (item.name, item.normalized_title, item.external_id) if value
    )
    product_text = product.name or ""
    competitor_codes = competitor_battery_part_codes(item)

    if battery_subject_conflict_reason(item, product):
        reasons.append("battery_subject_conflict")
    if battery_part_code_conflict(product, competitor_codes):
        reasons.append("battery_part_code_conflict")
    if battery_capacity_conflict(item_text, product_text, item.attrs_json):
        reasons.append("battery_capacity_conflict")
    if battery_premium_tier_conflict(item, product):
        reasons.append("battery_premium_tier_conflict")
    return reasons
