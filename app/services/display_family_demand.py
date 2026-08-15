"""Pure helpers for shadow/backtest allocation of display-family demand."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping, Sequence

ZERO = Decimal("0")
ONE = Decimal("1")


@dataclass(frozen=True)
class DisplayFamilyMember:
    nomenclature_code: str
    name: str
    family_id: str
    segment_id: str
    quality_segment: str
    construction_segment: str
    model_tokens: tuple[str, ...]


@dataclass(frozen=True)
class DisplayFamilyAllocation:
    nomenclature_code: str
    family_id: str
    segment_id: str
    baseline_rate: Decimal
    family_baseline_rate: Decimal
    recent_sales_qty: Decimal
    family_recent_sales_qty: Decimal
    pure_family_rate: Decimal
    allocated_rate: Decimal
    sku_share: Decimal
    allocation_source: str


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _family_id(tokens: Sequence[str], *, fallback_code: str) -> str:
    normalized = sorted({_clean(token).casefold() for token in tokens if _clean(token)})
    if not normalized:
        return f"display-singleton-{fallback_code.casefold()}"
    digest = hashlib.sha256("\0".join(normalized).encode()).hexdigest()[:16]
    return f"display-family-{digest}"


def _compatibility_signature(tokens: Sequence[str]) -> tuple[str, ...]:
    normalized = tuple(sorted({_clean(token).casefold() for token in tokens if _clean(token)}))
    model_tokens = tuple(token for token in normalized if ":model:" in token)
    return model_tokens or normalized


def display_quality_segment(name: str) -> str:
    compact = re.sub(r"[^a-zа-я0-9]+", "", _clean(name).casefold().replace("ё", "е"))
    if "softoled" in compact or "ультрасофт" in compact or "ultrasoft" in compact:
        return "soft_oled"
    if "hardoled" in compact:
        return "hard_oled"
    if "incell" in compact:
        return "in_cell"
    if any(token in compact for token in ("orig100", "or100", "ориг100")):
        return "orig100"
    if any(token in compact for token in ("original", "orig", "ориг")):
        return "original"
    if "oled" in compact or "амолед" in compact or "amoled" in compact:
        return "oled"
    if "lcd" in compact:
        return "lcd"
    return "unknown"


def display_construction_segment(name: str) -> str:
    text = _clean(name).casefold().replace("ё", "е")
    tags: list[str] = []
    if "в рамке" in text or "с рамкой" in text:
        tags.append("with_frame")
    else:
        tags.append("without_frame")
    if "под ic" in text or "площадка ic" in text:
        tags.append("ic_pad")
    if "als" in text and "шлейф" in text:
        tags.append("als_flex")
    if "верификац" in text:
        tags.append("verified")
    return "+".join(tags)


def build_display_family_members(
    items: Sequence[Mapping[str, Any]],
) -> dict[str, DisplayFamilyMember]:
    """Build conservative deterministic families from compatibility signatures."""

    normalized: list[dict[str, Any]] = []
    for item in items:
        code = _clean(item.get("nomenclature_code"))
        if not code:
            continue
        tokens = _compatibility_signature(item.get("model_tokens", ()))
        normalized.append(
            {
                "nomenclature_code": code,
                "name": _clean(item.get("name")),
                "model_tokens": tokens,
            }
        )

    grouped_indexes: dict[tuple[str, ...] | tuple[str, str], list[int]] = defaultdict(list)
    for index, item in enumerate(normalized):
        tokens = item["model_tokens"]
        group_key: tuple[str, ...] | tuple[str, str]
        if tokens:
            group_key = tokens
        else:
            group_key = ("singleton", item["nomenclature_code"].casefold())
        grouped_indexes[group_key].append(index)

    result: dict[str, DisplayFamilyMember] = {}
    for indexes in grouped_indexes.values():
        group_tokens = normalized[indexes[0]]["model_tokens"]
        for index in indexes:
            item = normalized[index]
            code = item["nomenclature_code"]
            name = item["name"]
            family_id = _family_id(group_tokens, fallback_code=code)
            quality = display_quality_segment(name)
            construction = display_construction_segment(name)
            result[code] = DisplayFamilyMember(
                nomenclature_code=code,
                name=name,
                family_id=family_id,
                segment_id=f"{quality}|{construction}",
                quality_segment=quality,
                construction_segment=construction,
                model_tokens=item["model_tokens"],
            )
    return result


def _weights(
    codes: Sequence[str],
    *,
    primary: Mapping[str, Decimal],
    fallback: Mapping[str, Decimal],
) -> tuple[dict[str, Decimal], str]:
    primary_total = sum((max(ZERO, primary.get(code, ZERO)) for code in codes), ZERO)
    if primary_total > ZERO:
        return (
            {code: max(ZERO, primary.get(code, ZERO)) / primary_total for code in codes},
            "recent_sales",
        )
    fallback_total = sum((max(ZERO, fallback.get(code, ZERO)) for code in codes), ZERO)
    if fallback_total > ZERO:
        return (
            {code: max(ZERO, fallback.get(code, ZERO)) / fallback_total for code in codes},
            "baseline_rate",
        )
    equal = ONE / Decimal(len(codes))
    return ({code: equal for code in codes}, "equal")


def allocate_display_family_rates(
    members: Mapping[str, DisplayFamilyMember],
    *,
    baseline_rates: Mapping[str, Decimal],
    recent_sales: Mapping[str, Decimal],
    blend: Decimal,
) -> dict[str, DisplayFamilyAllocation]:
    """Reallocate, but never add, forecast rate within each compatible family."""

    normalized_blend = Decimal(blend)
    if not ZERO <= normalized_blend <= ONE:
        raise ValueError("family allocation blend must be between zero and one")

    by_family: dict[str, list[str]] = defaultdict(list)
    for code, member in members.items():
        if code in baseline_rates:
            by_family[member.family_id].append(code)

    allocations: dict[str, DisplayFamilyAllocation] = {}
    for family_id, family_codes in by_family.items():
        family_codes = sorted(family_codes)
        family_rate = sum(
            (max(ZERO, Decimal(baseline_rates.get(code, ZERO))) for code in family_codes),
            ZERO,
        )
        family_sales = sum(
            (max(ZERO, Decimal(recent_sales.get(code, ZERO))) for code in family_codes),
            ZERO,
        )
        segment_codes: dict[str, list[str]] = defaultdict(list)
        for code in family_codes:
            segment_codes[members[code].segment_id].append(code)

        segment_sales = {
            segment_id: sum(
                (max(ZERO, Decimal(recent_sales.get(code, ZERO))) for code in codes),
                ZERO,
            )
            for segment_id, codes in segment_codes.items()
        }
        segment_baseline = {
            segment_id: sum(
                (max(ZERO, Decimal(baseline_rates.get(code, ZERO))) for code in codes),
                ZERO,
            )
            for segment_id, codes in segment_codes.items()
        }
        segment_weights, segment_source = _weights(
            sorted(segment_codes),
            primary=segment_sales,
            fallback=segment_baseline,
        )

        for segment_id, codes in segment_codes.items():
            sku_weights, sku_source = _weights(
                sorted(codes),
                primary=recent_sales,
                fallback=baseline_rates,
            )
            segment_rate = family_rate * segment_weights[segment_id]
            for code in codes:
                baseline = max(ZERO, Decimal(baseline_rates.get(code, ZERO)))
                pure_rate = baseline if family_sales == ZERO else segment_rate * sku_weights[code]
                allocated = baseline * (ONE - normalized_blend) + pure_rate * normalized_blend
                allocations[code] = DisplayFamilyAllocation(
                    nomenclature_code=code,
                    family_id=family_id,
                    segment_id=segment_id,
                    baseline_rate=baseline,
                    family_baseline_rate=family_rate,
                    recent_sales_qty=max(ZERO, Decimal(recent_sales.get(code, ZERO))),
                    family_recent_sales_qty=family_sales,
                    pure_family_rate=pure_rate,
                    allocated_rate=allocated,
                    sku_share=(allocated / family_rate if family_rate > ZERO else ZERO),
                    allocation_source=f"segment:{segment_source};sku:{sku_source}",
                )
    return allocations
