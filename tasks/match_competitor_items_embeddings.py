"""Match competitor items to products using embeddings + guardrails."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import numpy as np
from sqlalchemy import create_engine, exists, func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import CompetitorItem, Product
from app.models.competitor_item_compatibility import CompetitorItemCompatibility
from app.models.competitor_item_match import (
    CompetitorItemMatch,
    CompetitorItemMatchMethod,
    CompetitorItemMatchStatus,
)
from app.models.device_model import PhoneModel
from app.models.product_phone_model import ProductPhoneModel
from app.services.display_normalization import (
    normalize_display_construction,
    normalize_display_quality,
    normalize_display_type,
    normalize_refresh_rate_hz,
)
from app.services.display_parser import (
    Backlight,
    ScreenConstruction,
    ScreenMatrixType,
    ScreenQualityGrade,
    parse_display_attributes,
)
from app.services.display_quality_raw_mapping import (
    extract_quality_token_as_in_name,
    map_competitor_raw_quality_to_1c_raw,
)
from app.services.embedding_utils import compose_competitor_text
from app.services.embeddings import EmbeddingClient
from app.services.matching_guardrails import (
    basic_candidate_guardrails,
    device_group,
    device_group_conflict,
)
from app.services.product_display_modification import (
    STATUS_CONFLICT as PRODUCT_DISPLAY_MODIFICATION_CONFLICT,
)
from app.services.product_display_modification import (
    display_frame_conflict,
    display_frame_requires_review,
)
from app.services.prompts import get_llm_match_arbiter_prompt

UTC = timezone.utc
UNSAFE_AUTO_ACCEPT_CUTOFF = date(2026, 5, 1)
ITEM_TYPES = {
    "display",
    "battery",
    "camera",
    "flex",
    "housing",
    "connector",
    "cable",
    "board",
    "other",
}
MODEL_GUARDRAIL_ITEM_TYPES = ITEM_TYPES - {"other", "cable"}

TYPE_TOKENS = {
    "display": [
        "дисплей",
        "экран",
        "матрица",
        "lcd",
        "oled",
        "amoled",
        "super amoled",
        "dynamic amoled",
        "ltps",
        "ltpo",
        "ips",
        "module",
        "модуль",
        "тачскрин",
    ],
    "battery": [
        "аккумулятор",
        "battery",
        "акб",
        "аккум",
        "batt",
        "mah",
        "мач",
        "power bank",
        "powerbank",
        "пауэрбанк",
        "внешний накопитель",
    ],
    "camera": ["камера", "camera"],
    "flex": ["шлейф", "flex", "шлейфа"],
    "housing": ["корпус", "крышка", "рамка", "frame", "cover", "панель"],
    "connector": ["разъем", "разъём", "коннектор", "port", "гнездо"],
    "cable": ["кабель", "usb", "type-c", "micro-usb", "lightning", "провод"],
    "board": ["плата", "board", "pcb", "материнская плата", "микросхема", "контроллер"],
}

NON_DISPLAY_TOOL_TOKENS = (
    "трекпад",
    "тачпад",
    "trackpad",
    "touchpad",
    "инвертор",
    "трафарет",
    "tmr механизм",
    "механизм геймпада",
    "геймпад",
    "струна",
    "сепаратор",
    "станок",
    "станция",
    "зарядная станция",
    "зарядное устройство",
    "зарядная",
    "паяльник",
    "паяльная станция",
    "термовоздушная",
    "припой",
    "флюс",
    "скотч",
    "проклейка",
    "пленка",
    "плёнка",
    "наклейка",
    "изоляционная",
    "защитная пленка",
    "защитная плёнка",
    "защитное стекло",
    "гидрогелевая",
    "hydrogel",
    "camera film",
    "накладка на модуль",
    "модуль подогрева",
    "модуль восстановления nand",
    "magsafe",
    "магнит magsafe",
    "bga",
    "nand",
    "лопатка",
    "медиатор",
    "присоска",
    "держатель плат",
    "разделения дисплейных модулей",
    "разборки дисплейных модулей",
    "инструмент для демонтажа экрана",
    "демонтажа экрана",
    "демонтаж экрана",
    "интеллектуальный модуль",
    "источник питания",
    "лабораторный источник",
    "tws",
    "гарнитура",
    "наушник",
    "колонка",
    "колонки",
    "nfc module",
    "модуль nfc",
    "nfc модуль",
    "бесконтактный модуль",
    "rfid module",
    "модуль rfid",
    "rfid модуль",
    "ламинатор",
    "триммер",
    "светодиодная подсветка",
    "подсветка для телевизоров",
    "модуль памяти",
    "sodimm",
    "ddr3",
    "ddr4",
    "ящик для запчастей",
    "ящик",
    "ультразвуковая ванночка",
    "ультразвуковая ванна",
)

VARIANT_TOKENS = {"pro", "max", "plus", "mini", "ultra", "se", "gt"}

BRAND_SYNONYMS = {
    "apple": "apple",
    "iphone": "apple",
    "samsung": "samsung",
    "xiaomi": "xiaomi",
    "redmi": "xiaomi",
    "poco": "xiaomi",
    "huawei": "huawei",
    "honor": "honor",
    "realme": "realme",
    "oppo": "oppo",
    "vivo": "vivo",
    "oneplus": "oneplus",
    "tcl": "tcl",
    "lenovo": "lenovo",
    "xiaoxin": "lenovo",
}
MODEL_KEY_BRANDS = {
    *BRAND_SYNONYMS.keys(),
    "apple",
    "ipad",
    "tcl",
    "itel",
    "infinix",
    "tecno",
    "meizu",
    "sony",
    "nokia",
    "zte",
    "motorola",
    "lenovo",
    "asus",
    "google",
}

PORT_TYPES = {"type-c", "typec", "type c", "micro-usb", "microusb", "lightning"}
COLOR_ALIASES = {
    "black": {"black", "черный", "черная", "чёрный", "чёрная"},
    "white": {"white", "белый", "белая"},
    "red": {"red", "красный", "красная"},
    "orange": {"orange", "оранжевый", "оранжевая"},
    "yellow": {"yellow", "желтый", "желтая", "жёлтый", "жёлтая"},
    "blue": {"blue", "синий", "синяя"},
    "lightblue": {"голубой", "голубая"},
    "green": {"green", "зеленый", "зеленая", "зелёный", "зелёная"},
    "mint": {"mint", "мятный", "мятная"},
    "pink": {"pink", "розовый", "розовая"},
    "gold": {
        "gold",
        "golden",
        "золотой",
        "золотая",
        "золото",
        "золотистый",
        "золотистая",
    },
    "gray": {"gray", "grey", "серый", "серая"},
    "silver": {"silver", "серебристый", "серебристая", "серебро"},
    "brown": {"brown", "коричневый", "коричневая"},
    "purple": {
        "purple",
        "violet",
        "lavender",
        "фиолетовый",
        "фиолетовая",
        "лавандовый",
        "лавандовая",
    },
    "bronze": {"bronze", "бронзовый", "бронзовая"},
    "titanium": {"titanium", "титановый", "титановая", "титан"},
}

CYRILLIC_CODE_CHARS = str.maketrans(
    {
        "А": "A",
        "В": "B",
        "Е": "E",
        "К": "K",
        "М": "M",
        "Н": "H",
        "О": "O",
        "Р": "P",
        "С": "C",
        "Т": "T",
        "Х": "X",
        "а": "A",
        "в": "B",
        "е": "E",
        "к": "K",
        "м": "M",
        "н": "H",
        "о": "O",
        "р": "P",
        "с": "C",
        "т": "T",
        "х": "X",
    }
)


def _load_embeddings(prefix: str, embeddings_dir: Path) -> tuple[np.ndarray, dict]:
    index_path = embeddings_dir / f"{prefix}_index.json"
    if not index_path.exists():
        raise FileNotFoundError(f"Missing embeddings index: {index_path}")
    index = json.loads(index_path.read_text(encoding="utf-8"))
    matrix_file = index.get("meta", {}).get("matrix_file")
    if matrix_file:
        matrix_path = embeddings_dir / matrix_file
    else:
        matrix_path = embeddings_dir / f"{prefix}_embeddings.npy"
    if not matrix_path.exists():
        raise FileNotFoundError(f"Missing embeddings matrix: {matrix_path}")
    matrix = np.load(matrix_path)
    return matrix, index


def _auto_accept_unique_suggested_matches(session: Session, *, min_score: float) -> int:
    grouped = (
        select(
            CompetitorItemMatch.product_id.label("product_id"),
            CompetitorItem.competitor.label("competitor"),
            func.count().label("cnt"),
        )
        .join(CompetitorItem, CompetitorItem.id == CompetitorItemMatch.competitor_item_id)
        .where(CompetitorItemMatch.status == CompetitorItemMatchStatus.SUGGESTED)
        .group_by(CompetitorItemMatch.product_id, CompetitorItem.competitor)
        .subquery()
    )
    matches = (
        session.execute(
            select(CompetitorItemMatch)
            .join(CompetitorItem, CompetitorItem.id == CompetitorItemMatch.competitor_item_id)
            .join(
                grouped,
                (grouped.c.product_id == CompetitorItemMatch.product_id)
                & (grouped.c.competitor == CompetitorItem.competitor),
            )
            .where(
                CompetitorItemMatch.status == CompetitorItemMatchStatus.SUGGESTED,
                CompetitorItemMatch.method != CompetitorItemMatchMethod.MANUAL,
                CompetitorItemMatch.final_score >= min_score,
                CompetitorItem.attrs_json.is_not(None),
                exists().where(CompetitorItemCompatibility.competitor_item_id == CompetitorItem.id),
                grouped.c.cnt == 1,
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    for match in matches:
        rationale = dict(match.rationale_json or {})
        rationale["auto_accept_unique"] = {
            "reason": "single_suggested_candidate_per_product_competitor",
            "min_score": min_score,
            "accepted_at": now.isoformat(),
        }
        match.status = CompetitorItemMatchStatus.ACCEPTED
        match.rationale_json = rationale
        match.updated_at = now
        session.add(match)
    return len(matches)


def _normalize(vec: np.ndarray) -> np.ndarray:
    norm = np.linalg.norm(vec)
    return vec if norm == 0 else vec / norm


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _infer_item_type(text: str | None) -> str | None:
    if not text:
        return None
    strong_type = _strong_non_display_type(text)
    if strong_type:
        return strong_type
    lower = text.lower()
    for item_type, tokens in TYPE_TOKENS.items():
        if any(tok in lower for tok in tokens):
            return item_type
    return None


def _strong_non_display_type(text: str | None) -> str | None:
    if not text:
        return None
    lower = text.lower()
    normalized = re.sub(r"[\s/_-]+", " ", lower).strip()
    if re.match(
        r"^\s*((задн\w*\s+)?крышк\w*|корпус\w*|панел\w*|back\s+cover|housing)\b",
        normalized,
    ):
        return "housing"
    if re.match(r"^\s*(шлейф|fpc|flex)\b", normalized):
        return "flex"
    if re.match(r"^\s*(коннектор|разъ[её]м|fpc\s+коннектор)\b", normalized):
        return "connector"
    if re.match(r"^\s*(ic|микросхема|контроллер)\b", normalized):
        return "board"
    if re.match(
        r"^\s*рамк\w*\s+(диспле\w*|тачскрин\w*|сенсорн\w*\s+экран\w*)\b",
        normalized,
    ):
        return "housing"
    if re.match(r"^\s*(подсветк\w*\s+диспле\w*|display\s+backlight|lcd\s+backlight)\b", normalized):
        return "other"
    if re.match(r"^\s*(тачскрин|touchscreen|digitizer)\b", normalized) and not re.search(
        r"\b(дисплей|lcd|oled|amoled|экран|display)\b",
        normalized,
    ):
        return "other"
    if re.match(
        r"^\s*стекло\s+(для\s+переклейки|модуля|задней\s+камеры)\b",
        normalized,
    ):
        return "other"
    if re.match(r"^\s*(back\s+camera\s+glass|camera\s+glass)\b", normalized):
        return "other"
    if re.search(r"\b(аккумулятор\w*|акб|battery|batt)\b", lower):
        has_display_module = any(
            token in lower for token in ("дисплей", "lcd", "экран", "тачскрин")
        )
        is_power_bank = any(
            token in lower
            for token in (
                "внешний акб",
                "внешний аккумулятор",
                "power bank",
                "powerbank",
                "пауэрбанк",
            )
        )
        starts_as_battery = re.match(r"^\s*(аккумулятор|акб|battery|batt)\b", lower) is not None
        if has_display_module and not is_power_bank and not starts_as_battery:
            return None
        return "battery"
    if re.search(r"\bклей\b", lower):
        return "other"
    if any(token in lower for token in NON_DISPLAY_TOOL_TOKENS):
        return "other"
    return None


def _display_word_is_feature(text: str | None) -> bool:
    if not text:
        return False
    lower = text.lower()
    has_display_word = any(token in lower for token in ("дисплей", "display", "экран", "led"))
    if not has_display_word:
        return False
    feature_markers = (
        "внешний акб",
        "внешний аккумулятор",
        "power bank",
        "powerbank",
        "пауэрбанк",
        "mah",
        "мач",
        "внешний накопитель",
        "зарядное",
        "зарядка",
    )
    return any(marker in lower for marker in feature_markers)


def _effective_item_type(item: CompetitorItem) -> str | None:
    item_type = item.item_type if item.item_type in ITEM_TYPES else None
    text = " ".join(
        value
        for value in (item.name, item.normalized_title, item.category, item.category_group)
        if value
    )
    strong_type = _strong_non_display_type(text)
    if strong_type == "housing":
        return strong_type
    if item_type == "display" and strong_type:
        return strong_type
    if item_type == "display" and _display_word_is_feature(text):
        lower = text.lower()
        if any(
            marker in lower
            for marker in (
                "внешний акб",
                "внешний аккумулятор",
                "power bank",
                "powerbank",
                "пауэрбанк",
                "mah",
                "мач",
                "внешний накопитель",
            )
        ):
            return "battery"
        return "other"
    return item_type


def _normalize_brand(brand: str | None) -> str | None:
    if not brand:
        return None
    key = brand.strip().lower()
    return BRAND_SYNONYMS.get(key, key)


def _extract_brand_from_text(text: str | None) -> str | None:
    if not text:
        return None
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    for token in tokens:
        if token in BRAND_SYNONYMS:
            return BRAND_SYNONYMS[token]
    return None


def _variant_set(text: str) -> set[str]:
    lower = text.lower()
    tokens = set(re.findall(r"[a-z0-9]+", lower))
    variants = {tok for tok in tokens if tok in VARIANT_TOKENS}
    if "pro" in variants and "max" in variants:
        variants.discard("pro")
        variants.discard("max")
        variants.add("pro max")
    return variants


def _variant_conflict(source: str, candidate: str) -> bool:
    src = _variant_set(source)
    cand = _variant_set(candidate)
    if not src and not cand:
        return False
    if src and not cand:
        return True
    if cand and not src:
        return True
    return not src.issubset(cand) and not cand.issubset(src)


def _device_group(text: str) -> str | None:
    return device_group(text)


def _device_conflict(source: str, candidate: str) -> bool:
    return device_group_conflict(source, candidate)


def _extract_capacity(text: str | None) -> int | None:
    if not text:
        return None
    match = re.search(r"(\d{3,5})\s*(mah|мач)", text.lower())
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


OLED_DISPLAY_TYPES = {"OLED", "AMOLED", "Super AMOLED", "Dynamic AMOLED", "LTPO AMOLED"}
LCD_PIXEL_CONSTRUCTIONS = {"In-Cell", "On-Cell"}


def _normalize_display_type_guard(value: str | ScreenMatrixType | None) -> str | None:
    if value is None:
        return None
    raw = value.value if isinstance(value, ScreenMatrixType) else str(value).strip()
    if not raw or raw == ScreenMatrixType.UNKNOWN.value:
        return None
    return normalize_display_type(raw)


def _extract_display_type(text: str | None) -> str | None:
    return _normalize_display_type_guard(text)


def _competitor_display_type(item: CompetitorItem) -> str | None:
    for value in (
        parse_display_attributes(item.name or "").screen_matrix_type if item.name else None,
        (
            parse_display_attributes(item.normalized_title or "").screen_matrix_type
            if item.normalized_title
            else None
        ),
        item.screen_matrix_type,
        item.attrs_type,
    ):
        normalized = _normalize_display_type_guard(value)
        if normalized:
            return normalized
    return None


def _product_display_type(product: Product) -> str | None:
    for value in (
        parse_display_attributes(product.name or "").screen_matrix_type if product.name else None,
        product.display_type,
    ):
        normalized = _normalize_display_type_guard(value)
        if normalized:
            return normalized
    return None


def _normalize_color_value(value: str | None) -> str | None:
    if not value:
        return None
    tokens = set(re.findall(r"[a-zа-яё]+", value.lower().replace("ё", "е")))
    for canonical, aliases in COLOR_ALIASES.items():
        normalized_aliases = {alias.replace("ё", "е") for alias in aliases}
        if tokens & normalized_aliases:
            return canonical
    return None


def _competitor_display_color(item: CompetitorItem) -> str | None:
    for value in (
        parse_display_attributes(item.name or "").color if item.name else None,
        item.name,
        (
            parse_display_attributes(item.normalized_title or "").color
            if item.normalized_title
            else None
        ),
        item.normalized_title,
        item.color,
        item.attrs_color,
    ):
        normalized = _normalize_color_value(value)
        if normalized:
            return normalized
    return None


def _product_display_color(product: Product) -> str | None:
    return _normalize_color_value(product.name) or _normalize_color_value(product.color)


def _extract_port_type(text: str | None) -> str | None:
    if not text:
        return None
    lower = text.lower()
    for token in PORT_TYPES:
        if token in lower:
            if token == "typec":
                return "type-c"
            if token == "type c":
                return "type-c"
            if token == "microusb":
                return "micro-usb"
            return token
    return None


def _capacity_conflict(item_text: str, product_text: str, attrs: dict[str, Any] | None) -> bool:
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
    item_capacity = attr_capacity or _extract_capacity(item_text)
    product_capacity = _extract_capacity(product_text)
    if item_capacity and product_capacity:
        return abs(item_capacity - product_capacity) >= 200
    return False


def _display_type_conflict(item_text: str, product_text: str, attrs: dict[str, Any] | None) -> bool:
    attr_type = None
    if attrs and attrs.get("type"):
        attr_type = _extract_display_type(str(attrs.get("type")))
    item_type = attr_type or _extract_display_type(item_text)
    product_type = _extract_display_type(product_text)
    if item_type and product_type:
        return item_type != product_type
    return False


def _display_matrix_family_conflict(
    product: Product,
    competitor_type: str | None,
    competitor_construction: str | None,
) -> bool:
    product_type = _product_display_type(product)
    product_construction = _product_display_construction(product)
    if competitor_type in OLED_DISPLAY_TYPES and product_construction in LCD_PIXEL_CONSTRUCTIONS:
        return True
    if product_type in OLED_DISPLAY_TYPES and competitor_construction in LCD_PIXEL_CONSTRUCTIONS:
        return True
    return False


def _display_color_conflict(
    product: Product,
    competitor_color: str | None,
) -> bool:
    product_color = _product_display_color(product)
    if competitor_color and product_color:
        return competitor_color != product_color
    return False


QUALITY_GRADE_ALIASES = {
    ScreenQualityGrade.ORIGINAL.value: "Original",
    ScreenQualityGrade.ORIGINAL_REFURB.value: "Original Refurbished",
    ScreenQualityGrade.OEM.value: "OEM",
    ScreenQualityGrade.COPY_HIGH.value: "Copy High",
    ScreenQualityGrade.COPY_MEDIUM.value: "Copy Medium",
    ScreenQualityGrade.COPY_LOW.value: "Copy Low",
    ScreenQualityGrade.OR.value: "Original",
    ScreenQualityGrade.OR100.value: "Original",
    ScreenQualityGrade.GX.value: "Copy High",
    ScreenQualityGrade.PREMIUM.value: "Copy High",
    ScreenQualityGrade.AAA.value: "Copy High",
    ScreenQualityGrade.HQ.value: "Copy High",
    ScreenQualityGrade.FIRST_CLASS.value: "Copy Medium",
}


def _normalize_display_quality_guard(value: str | ScreenQualityGrade | None) -> str | None:
    if value is None:
        return None
    raw = value.value if isinstance(value, ScreenQualityGrade) else str(value).strip()
    if not raw or raw == ScreenQualityGrade.UNKNOWN.value:
        return None
    alias = QUALITY_GRADE_ALIASES.get(raw)
    if alias:
        return alias
    return normalize_display_quality(raw)


def _competitor_display_quality_raw(item: CompetitorItem) -> str | None:
    return extract_quality_token_as_in_name(item.name) or extract_quality_token_as_in_name(
        item.normalized_title
    )


def _competitor_display_mapped_1c_quality_raw(item: CompetitorItem) -> str | None:
    return map_competitor_raw_quality_to_1c_raw(
        item.competitor,
        _competitor_display_quality_raw(item),
    )


def _competitor_display_quality(item: CompetitorItem) -> str | None:
    for text in (item.name, item.normalized_title):
        if not text:
            continue
        parsed_quality = parse_display_attributes(text).screen_quality_grade
        if parsed_quality == ScreenQualityGrade.ORIGINAL_REFURB:
            return _normalize_display_quality_guard(parsed_quality)

    normalized_mapped_quality = _normalize_display_quality_guard(
        _competitor_display_mapped_1c_quality_raw(item)
    )
    if normalized_mapped_quality:
        return normalized_mapped_quality

    for value in (
        item.name,
        parse_display_attributes(item.name or "").screen_quality_grade if item.name else None,
        item.normalized_title,
        (
            parse_display_attributes(item.normalized_title or "").screen_quality_grade
            if item.normalized_title
            else None
        ),
    ):
        normalized = _normalize_display_quality_guard(value)
        if normalized:
            return normalized
    return None


def _product_display_quality_raw(product: Product) -> str | None:
    return (
        product.display_quality_raw
        or product.quality_raw
        or extract_quality_token_as_in_name(product.name)
    )


def _product_display_quality(product: Product) -> str | None:
    for value in (
        product.name,
        parse_display_attributes(product.name or "").screen_quality_grade if product.name else None,
        product.display_quality,
        product.quality,
        product.display_quality_raw,
        product.quality_raw,
    ):
        normalized = _normalize_display_quality_guard(value)
        if normalized:
            return normalized
    return None


def _display_quality_conflict(product: Product, competitor_quality: str | None) -> bool:
    product_quality = _product_display_quality(product)
    if product_quality and competitor_quality:
        return product_quality != competitor_quality
    return False


def _display_quality_requires_review(product: Product, competitor_quality: str | None) -> bool:
    product_quality = _product_display_quality(product)
    return bool(product_quality) != bool(competitor_quality)


def _competitor_display_has_frame(item: CompetitorItem) -> bool | None:
    for text in (item.name, item.normalized_title):
        if not text:
            continue
        explicit_has_frame = _explicit_display_frame_value(text)
        if explicit_has_frame is not None:
            return explicit_has_frame
    if item.competitor and item.competitor.casefold() == "moba":
        sku_tokens = {
            token for token in re.split(r"[^a-z0-9]+", (item.external_id or "").casefold()) if token
        }
        if "fr" in sku_tokens:
            return True
        if "cp" in sku_tokens:
            return False
    for text in (item.name, item.normalized_title):
        if not text:
            continue
        parsed_has_frame = parse_display_attributes(text).has_frame
        if parsed_has_frame is not None:
            return parsed_has_frame
    return item.has_frame


def _explicit_display_frame_value(text: str | None) -> bool | None:
    if not text:
        return None
    normalized = text.lower().replace("ё", "е")
    if re.search(r"\bбез\s*рамк\w*\b", normalized):
        return False
    if re.search(r"\b(с\s+рамк\w*|в\s+рамк\w*|рамк\w*\s+креплен\w*|рамк\w*)\b", normalized):
        return True
    return None


def _explicit_display_touch_value(text: str | None) -> bool | None:
    if not text:
        return None
    normalized = text.lower()
    if re.search(r"\bбез\s+(тачскрин\w*|сенсор\w*|touch|digitizer)\b", normalized):
        return False
    if re.search(r"\b(тачскрин\w*|сенсор\w*|touch|digitizer)\b", normalized):
        return True
    return None


def _competitor_display_has_touch(item: CompetitorItem) -> bool | None:
    for text in (item.name, item.normalized_title):
        explicit = _explicit_display_touch_value(text)
        if explicit is not None:
            return explicit
    if item.has_touch is True:
        return True
    return None


def _product_display_has_touch(product: Product) -> bool | None:
    explicit = _explicit_display_touch_value(product.name)
    if explicit is not None:
        return explicit
    if product.display_has_touch is True:
        return True
    return None


def _display_touch_conflict(product: Product, competitor_has_touch: bool | None) -> bool:
    product_has_touch = _product_display_has_touch(product)
    if product_has_touch is None or competitor_has_touch is None:
        return False
    return product_has_touch != competitor_has_touch


def _normalize_backlight(value: str | Backlight | None) -> str | None:
    if value is None:
        return None
    raw = value.value if isinstance(value, Backlight) else str(value).strip()
    if not raw or raw == Backlight.UNKNOWN.value:
        return None
    for item in Backlight:
        if raw == item.value:
            return item.value
    parsed = parse_display_attributes(raw).backlight
    if parsed != Backlight.UNKNOWN:
        return parsed.value
    return None


def _competitor_display_backlight(item: CompetitorItem) -> str | None:
    for text in (item.name, item.normalized_title):
        if not text:
            continue
        normalized = _normalize_backlight(parse_display_attributes(text).backlight)
        if normalized:
            return normalized
    return _normalize_backlight(item.backlight)


def _product_display_backlight(product: Product) -> str | None:
    parsed = _normalize_backlight(parse_display_attributes(product.name or "").backlight)
    if parsed:
        return parsed
    return _normalize_backlight(product.display_backlight)


def _display_backlight_conflict(product: Product, competitor_backlight: str | None) -> bool:
    product_backlight = _product_display_backlight(product)
    if not product_backlight or not competitor_backlight:
        return False
    no_backlight = Backlight.NO_BACKLIGHT.value
    return (product_backlight == no_backlight) != (competitor_backlight == no_backlight)


def _normalize_matrix_tags(tags: list[str] | tuple[str, ...] | None) -> set[str]:
    if not tags:
        return set()
    return {str(tag).strip().upper() for tag in tags if str(tag).strip()}


def _competitor_display_matrix_tags(item: CompetitorItem) -> set[str]:
    tags: set[str] = set()
    for text in (item.name, item.normalized_title):
        if text:
            tags |= _normalize_matrix_tags(parse_display_attributes(text).matrix_tags)
    tags |= _normalize_matrix_tags(item.matrix_tags)
    return tags


def _product_display_matrix_tags(product: Product) -> set[str]:
    tags = _normalize_matrix_tags(parse_display_attributes(product.name or "").matrix_tags)
    tags |= _normalize_matrix_tags(product.display_matrix_tags)
    return tags


def _display_matrix_tags_conflict(product: Product, competitor_tags: set[str]) -> bool:
    product_tags = _product_display_matrix_tags(product)
    if product_tags and competitor_tags:
        return product_tags.isdisjoint(competitor_tags)
    return False


def _normalize_display_construction_guard(
    value: str | ScreenConstruction | None,
) -> str | None:
    if value is None:
        return None
    raw = value.value if isinstance(value, ScreenConstruction) else str(value).strip()
    if not raw or raw == ScreenConstruction.UNKNOWN.value:
        return None
    if raw == ScreenConstruction.INCELL.value:
        return "In-Cell"
    if raw == ScreenConstruction.ONCELL.value:
        return "On-Cell"
    if raw == ScreenConstruction.COF.value:
        return "COF"
    if raw == ScreenConstruction.COG.value:
        return "COG"
    if raw in {ScreenConstruction.HARD_OLED.value, ScreenConstruction.SOFT_OLED.value}:
        return raw
    return normalize_display_construction(raw)


def _competitor_display_construction(item: CompetitorItem) -> str | None:
    for value in (
        parse_display_attributes(item.name or "").screen_construction if item.name else None,
        (
            parse_display_attributes(item.normalized_title or "").screen_construction
            if item.normalized_title
            else None
        ),
    ):
        normalized = _normalize_display_construction_guard(value)
        if normalized:
            return normalized
    return None


def _product_display_construction(product: Product) -> str | None:
    for value in (
        parse_display_attributes(product.name or "").screen_construction if product.name else None,
        product.display_construction,
    ):
        normalized = _normalize_display_construction_guard(value)
        if normalized:
            return normalized
    return None


def _display_construction_conflict(product: Product, competitor_construction: str | None) -> bool:
    product_construction = _product_display_construction(product)
    if product_construction and competitor_construction:
        return product_construction != competitor_construction
    return False


def _competitor_display_refresh_rate_hz(item: CompetitorItem) -> int | None:
    for value in (
        parse_display_attributes(item.name or "").refresh_rate_hz if item.name else None,
        (
            parse_display_attributes(item.normalized_title or "").refresh_rate_hz
            if item.normalized_title
            else None
        ),
        item.refresh_rate_hz,
        item.attrs_refresh_rate_hz,
    ):
        normalized = normalize_refresh_rate_hz(value)
        if normalized:
            return normalized
    return None


def _product_display_refresh_rate_hz(product: Product) -> int | None:
    for value in (
        parse_display_attributes(product.name or "").refresh_rate_hz if product.name else None,
        product.display_refresh_rate_hz,
    ):
        normalized = normalize_refresh_rate_hz(value)
        if normalized:
            return normalized
    return None


def _display_refresh_rate_conflict(
    product: Product, competitor_refresh_rate_hz: int | None
) -> bool:
    product_refresh_rate_hz = _product_display_refresh_rate_hz(product)
    if product_refresh_rate_hz and competitor_refresh_rate_hz:
        return product_refresh_rate_hz != competitor_refresh_rate_hz
    return False


def _normalize_code_text(text: str | None) -> str:
    if not text:
        return ""
    return text.translate(CYRILLIC_CODE_CHARS).upper()


def _add_letter_digit_code(codes: set[str], raw_code: str) -> None:
    code = re.sub(r"[^A-Z0-9]", "", raw_code.upper())
    if code.startswith("SM"):
        code = code[2:]
    if re.match(r"M\d{4}", code):
        return
    match = re.fullmatch(r"([A-Z]\d{3,5})([A-Z0-9]{0,4})", code)
    if not match:
        return
    full_code = f"{match.group(1)}{match.group(2)}"
    codes.add(full_code)
    if len(match.group(1)) == 4:
        codes.add(match.group(1))


def _add_xiaomi_m_code(codes: set[str], raw_code: str) -> None:
    code = re.sub(r"[^A-Z0-9]", "", raw_code.upper())
    match = re.fullmatch(r"M\d{4}[A-Z]\d{1,2}[A-Z0-9]{0,5}", code)
    if not match:
        return
    codes.add(code)
    family_match = re.match(r"M\d{4}[A-Z]\d{1,2}", code)
    if family_match:
        codes.add(family_match.group(0))


def _extract_device_codes(text: str | None) -> set[str]:
    normalized = _normalize_code_text(text)
    if not normalized:
        return set()

    codes: set[str] = set()

    for match in re.finditer(r"\bSM[\s-]*[AFGJMSX]\d{3,5}[A-Z0-9]{0,4}\b", normalized):
        _add_letter_digit_code(codes, match.group(0))
    for match in re.finditer(r"\b[A-Z]\d{3,5}[A-Z0-9]{0,4}\b", normalized):
        _add_letter_digit_code(codes, match.group(0))

    for match in re.finditer(r"\bA\d{4,5}\b", normalized):
        codes.add(match.group(0))

    for match in re.finditer(r"\bM\d{4}[A-Z]\d{1,2}[A-Z0-9]{0,5}\b", normalized):
        _add_xiaomi_m_code(codes, match.group(0))
    for match in re.finditer(r"\b\d{6,}[A-Z]{1,4}\b", normalized):
        codes.add(match.group(0))
    for match in re.finditer(r"\b\d{4,}[A-Z]{1,4}\d[A-Z0-9]*\b", normalized):
        codes.add(match.group(0))

    huawei_honor_prefixes = (
        "AGS",
        "ANA",
        "ANE",
        "BAH",
        "BKL",
        "CLT",
        "COL",
        "CRT",
        "DUB",
        "ELE",
        "ELS",
        "EVR",
        "FIG",
        "JNY",
        "JSN",
        "KOB",
        "LDN",
        "LIO",
        "LYA",
        "MAR",
        "MRD",
        "NAM",
        "PAR",
        "POT",
        "SNE",
        "STK",
        "VOG",
        "VTR",
        "WAS",
        "WKG",
        "YAL",
    )
    huawei_honor_re = rf"\b(?:{'|'.join(huawei_honor_prefixes)})[-\s][A-Z0-9]{{2,8}}\b"
    for match in re.finditer(huawei_honor_re, normalized):
        code = re.sub(r"\s+", "-", match.group(0))
        codes.add(code)

    for match in re.finditer(r"\b[A-Z]{2,5}\d?-[A-Z0-9]{2,6}\b", normalized):
        code = match.group(0)
        if code not in {"IN-CELL", "ON-CELL", "WI-FI"}:
            codes.add(code)

    for match in re.finditer(r"\b(?:RMX|CPH)[-\s]?\d{3,6}\b", normalized):
        codes.add(re.sub(r"[-\s]", "", match.group(0)))

    for match in re.finditer(r"\bA\d{4}\b", normalized):
        codes.add(match.group(0))

    return codes


def _competitor_device_code_text(item: CompetitorItem) -> str:
    return " ".join(
        value
        for value in (
            item.name,
            item.normalized_title,
            item.parsed_device_model,
            item.parsed_device_variant,
        )
        if value
    )


def _display_model_code_conflict(item: CompetitorItem, product: Product) -> bool:
    competitor_codes = _extract_device_codes(_competitor_device_code_text(item))
    product_codes = _extract_device_codes(product.name)
    if competitor_codes and product_codes:
        return competitor_codes.isdisjoint(product_codes)
    return False


def _display_model_code_overlap(item: CompetitorItem, product: Product) -> bool:
    competitor_codes = _extract_device_codes(_competitor_device_code_text(item))
    product_codes = _extract_device_codes(product.name)
    return bool(
        competitor_codes and product_codes and not competitor_codes.isdisjoint(product_codes)
    )


def _display_model_text_overlap(item: CompetitorItem, product: Product) -> bool:
    competitor_keys = _extract_device_model_keys(_competitor_device_model_text(item))
    product_keys = _extract_device_model_keys(product.name)
    return _device_model_keys_overlap(competitor_keys, product_keys)


def _display_model_code_requires_review(item: CompetitorItem, product: Product) -> bool:
    return _display_model_code_conflict(item, product) and _display_model_text_overlap(
        item, product
    )


def _display_model_code_blocks(item: CompetitorItem, product: Product) -> bool:
    return _display_model_code_conflict(item, product) and not _display_model_text_overlap(
        item, product
    )


def _normalize_model_text(text: str | None) -> str:
    if not text:
        return ""
    normalized = text.lower().replace("ё", "е")
    normalized = re.sub(r"\bteco\b", "tecno", normalized)
    normalized = re.sub(r"(?<=\d)а\b", "a", normalized)
    normalized = re.sub(r"(?<=\d)к\b", "k", normalized)
    normalized = re.sub(r"(?<=\d)х\b", "x", normalized)
    normalized = re.sub(r"[^a-z0-9а-я]+", " ", normalized)
    return " ".join(normalized.split())


def _add_key_with_optional_network(keys: set[str], base: str, network: str | None) -> None:
    keys.add(base)
    if network:
        keys.add(f"{base}_{network}")


def _model_variants(variant: str | None) -> list[str]:
    if not variant:
        return []
    if variant in {"pro", "prime"}:
        return ["pro", "prime"]
    return [variant]


def _extract_device_model_keys(text: str | None) -> set[str]:
    normalized = _normalize_model_text(text)
    if not normalized:
        return set()

    keys: set[str] = set()

    for match in re.finditer(
        r"\bredmi\s+note\s+(\d{1,2})([a-z])?"
        r"(?:\s+(pro|prime|max|plus|ultra|lite|se))?"
        r"(?:\s+(4g|5g|global(?:\s+version)?))?\b",
        normalized,
    ):
        number, suffix, variant, network = match.groups()
        base = f"redmi_note_{number}{suffix or ''}"
        variants = _model_variants(variant)
        if not variants:
            _add_key_with_optional_network(
                keys, base, network.replace(" ", "_") if network else None
            )
        for model_variant in variants:
            _add_key_with_optional_network(
                keys,
                f"{base}_{model_variant}",
                network.replace(" ", "_") if network else None,
            )

    for match in re.finditer(
        r"\bredmi\s+(?!note\b)(\d{1,2}[a-z]?)"
        r"(?:\s+(pro|prime|max|plus|ultra|lite|se))?"
        r"(?:\s+(4g|5g))?\b",
        normalized,
    ):
        model, variant, network = match.groups()
        base = f"redmi_{model}"
        variants = _model_variants(variant)
        if not variants:
            _add_key_with_optional_network(keys, base, network)
        for model_variant in variants:
            _add_key_with_optional_network(keys, f"{base}_{model_variant}", network)

    for match in re.finditer(
        r"\bredmi\s+(a\d{1,2}[a-z]?)(?:\s+(pro|max|plus|ultra|lite|se))?(?:\s+(4g|5g))?\b",
        normalized,
    ):
        model, variant, network = match.groups()
        base = f"redmi_{model}"
        if variant:
            base = f"{base}_{variant}"
        _add_key_with_optional_network(keys, base, network)

    for match in re.finditer(
        r"\bredmi\s+(s\d{1,2}[a-z]?)(?:\s+(pro|max|plus|ultra|lite|se))?(?:\s+(4g|5g))?\b",
        normalized,
    ):
        model, variant, network = match.groups()
        base = f"redmi_{model}"
        if variant:
            base = f"{base}_{variant}"
        _add_key_with_optional_network(keys, base, network)

    for match in re.finditer(
        r"\bpoco\s+([a-z]\d{1,2}[a-z]?)"
        r"(?:\s+(pro|max|plus|ultra|lite|se|gt))?"
        r"(?:\s+(4g|5g))?\b",
        normalized,
    ):
        model, variant, network = match.groups()
        base = f"poco_{model}"
        if variant:
            base = f"{base}_{variant}"
        _add_key_with_optional_network(keys, base, network)

    for match in re.finditer(
        r"\bhonor\s+(x?\d{1,3}[a-z]?|\d{1,3}x)" r"(?:\s+(lite|pro|max|plus|premium))?\b",
        normalized,
    ):
        model, variant = match.groups()
        base = f"honor_{model}"
        if variant:
            base = f"{base}_{variant}"
        keys.add(base)

    for match in re.finditer(
        r"\bhonor\s+view\s+(\d{1,2})(?:\s+(lite|pro|max|plus))?\b", normalized
    ):
        model, variant = match.groups()
        base = f"honor_view_{model}"
        if variant:
            base = f"{base}_{variant}"
        keys.add(base)

    for match in re.finditer(
        r"\bhonor\s+magic\s?([a-z]?\d{1,2})" r"(?:\s+(lite|pro|max|plus|ultimate))?\b",
        normalized,
    ):
        model, variant = match.groups()
        base = f"honor_magic_{model}"
        if variant:
            base = f"{base}_{variant}"
        keys.add(base)

    for match in re.finditer(
        r"\bhonor\s+pad\s+(x?\d{1,2}[a-z]?)(?:\s+(lite|pro|max|plus))?\b",
        normalized,
    ):
        model, variant = match.groups()
        base = f"honor_pad_{model}"
        if variant:
            base = f"{base}_{variant}"
        keys.add(base)

    for match in re.finditer(
        r"\bhonor\s+y(\d{1,2}[a-z]?)(?:\s+(lite|prime|pro|max|plus))?\b",
        normalized,
    ):
        model, variant = match.groups()
        base = f"honor_y{model}"
        if variant:
            base = f"{base}_{variant}"
        keys.add(base)

    for match in re.finditer(r"\bhonor\s+play(?:\s+(\d{1,2}[a-z]?))?\b", normalized):
        model = match.group(1)
        keys.add(f"honor_play_{model}" if model else "honor_play")

    for match in re.finditer(
        r"\bhonor\s+v(\d{1,2}[a-z]?)(?:\s+(lite|play|pro|max|plus))?\b",
        normalized,
    ):
        model, variant = match.groups()
        base = f"honor_v{model}"
        if variant:
            base = f"{base}_{variant}"
        keys.add(base)

    for match in re.finditer(
        r"\b(?:huawei\s+)?nova\s+(\d{1,2}[a-z]?)" r"(?:\s+(lite|pro|max|plus|premium))?\b",
        normalized,
    ):
        model, variant = match.groups()
        base = f"nova_{model}"
        if variant:
            base = f"{base}_{variant}"
        keys.add(base)

    for match in re.finditer(
        r"\b(?:huawei\s+)?nova\s+y(\d{1,2}[a-z]?)" r"(?:\s+(lite|pro|max|plus))?\b",
        normalized,
    ):
        model, variant = match.groups()
        base = f"nova_y{model}"
        if variant:
            base = f"{base}_{variant}"
        keys.add(base)

    if re.search(r"\b(?:huawei\s+)?nova\s+plus\b", normalized):
        keys.add("nova_plus")
    if re.search(r"\b(?:huawei\s+)?nova\b", normalized) and not re.search(
        r"\b(?:huawei\s+)?nova\s+(?:\d{1,2}[a-z]?|y\d{1,2}[a-z]?|plus)\b",
        normalized,
    ):
        keys.add("nova_base")

    for match in re.finditer(
        r"\bhuawei\s+mate\s+(\d{1,2})(?:\s+(lite|pro|max|plus))?\b",
        normalized,
    ):
        model, variant = match.groups()
        base = f"huawei_mate_{model}"
        if variant:
            base = f"{base}_{variant}"
        keys.add(base)

    for match in re.finditer(
        r"\bhuawei\s+p\s+smart(?:\s+(\d{4}))?\b",
        normalized,
    ):
        year = match.group(1)
        keys.add(f"huawei_p_smart_{year}" if year else "huawei_p_smart")

    for match in re.finditer(
        r"\bhuawei\s+y(\d{1,2}[a-z]?)(?:\s+(lite|prime|pro|max|plus))?(?:\s+(\d{4}))?\b",
        normalized,
    ):
        model, variant, year = match.groups()
        base = f"huawei_y{model}"
        if variant:
            base = f"{base}_{variant}"
        if year:
            base = f"{base}_{year}"
        keys.add(base)

    for match in re.finditer(
        r"\bhuawei\s+ascend\s+([gy]\d{3,4}[a-z]?|y\d{1,3}[a-z]?)\b",
        normalized,
    ):
        model = match.group(1)
        keys.add(f"huawei_ascend_{model}")
        keys.add(f"huawei_{model}")

    for match in re.finditer(r"\bhuawei\s+(g\d{3,4}[a-z]?|y\d{3,4}[a-z]?|u\d{4})\b", normalized):
        keys.add(f"huawei_{match.group(1)}")

    for match in re.finditer(r"\bhuawei\s+ideos\s+([a-z]\d{1,2})\b", normalized):
        keys.add(f"huawei_ideos_{match.group(1)}")

    for match in re.finditer(
        r"\bhuawei\s+p\s?(\d{1,2})" r"(?:\s+(lite|pro|max|plus))?" r"(?:\s+([a-z]))?\b",
        normalized,
    ):
        model, variant, suffix = match.groups()
        base = f"huawei_p{model}"
        if variant:
            base = f"{base}_{variant}"
        if suffix:
            base = f"{base}_{suffix}"
        keys.add(base)

    for match in re.finditer(
        r"\bhuawei\s+pura\s+(\d{1,2})(?:\s+(lite|pro|max|plus|ultra))?\b",
        normalized,
    ):
        model, variant = match.groups()
        base = f"huawei_pura_{model}"
        if variant:
            base = f"{base}_{variant}"
        keys.add(base)

    for match in re.finditer(
        r"\b(?:huawei\s+)?(?:matepad|mediapad)\s+"
        r"([a-z]?\d{1,2}[a-z]?|t\s?\d{1,2}s?|m\d|m\d\s+lite|pro|12x)"
        r"(?:\s+(lite|pro|max|plus))?"
        r"(?:\s+(\d{1,2}(?:\s+\d)?))?\b",
        normalized,
    ):
        model, variant, size = match.groups()
        base = "huawei_tablet_" + model.replace(" ", "")
        if variant:
            base = f"{base}_{variant}"
        if size:
            base = f"{base}_{size.replace(' ', '')}"
        keys.add(base)

    for match in re.finditer(
        r"\blenovo\s+tab\s+([mp]?\d{1,2})(?:\s+(lite|plus|pro|max))?(?:\s+gen\s+(\d))?",
        normalized,
    ):
        model, variant, generation = match.groups()
        base = f"lenovo_tab_{model}"
        if variant:
            base = f"{base}_{variant}"
        if generation:
            base = f"{base}_gen{generation}"
        keys.add(base)

    for match in re.finditer(r"\blenovo\s+tab\s+(\d)\s+(plus|pro|max)\b", normalized):
        model, variant = match.groups()
        keys.add(f"lenovo_tab_{model}_{variant}")

    for match in re.finditer(
        r"\bxiaomi\s+mi\s+([a-z]?\d{1,2}[a-z]?)(?:\s+(lite|pro|max|plus|ultra))?\b",
        normalized,
    ):
        model, variant = match.groups()
        base = f"xiaomi_mi_{model}"
        if variant:
            base = f"{base}_{variant}"
        keys.add(base)

    for match in re.finditer(
        r"\bmi\s+([a-z]?\d{1,2}[a-z]?)(?:\s+(lite|pro|max|plus|ultra))?\b",
        normalized,
    ):
        model, variant = match.groups()
        if "xiaomi" in normalized or "redmi" in normalized or "poco" in normalized:
            base = f"xiaomi_mi_{model}"
            if variant:
                base = f"{base}_{variant}"
            keys.add(base)

    for match in re.finditer(
        r"\b(?:xiaomi\s+)?mi\s?pad\s+(\d{1,2}[a-z]?)(?:\s+(lite|pro|max|plus))?\b",
        normalized,
    ):
        model, variant = match.groups()
        base = f"xiaomi_mipad_{model}"
        if variant:
            base = f"{base}_{variant}"
        keys.add(base)

    for match in re.finditer(
        r"\bmi\s+note\s+(\d{1,2}[a-z]?)(?:\s+(lite|pro|max|plus|ultra))?\b",
        normalized,
    ):
        model, variant = match.groups()
        if "xiaomi" in normalized:
            base = f"xiaomi_mi_note_{model}"
            if variant:
                base = f"{base}_{variant}"
            keys.add(base)

    for match in re.finditer(
        r"\bredmi\s+pad\s+(\d{1,2}[a-z]?)(?:\s+(lite|pro|max|plus))?\b",
        normalized,
    ):
        model, variant = match.groups()
        base = f"redmi_pad_{model}"
        if variant:
            base = f"{base}_{variant}"
        keys.add(base)

    for match in re.finditer(
        r"\bxiaomi\s+(\d{1,2}[a-z]?)(?:\s+(lite|pro|max|plus|ultra))?\b",
        normalized,
    ):
        model, variant = match.groups()
        base = f"xiaomi_{model}"
        if variant:
            base = f"{base}_{variant}"
        keys.add(base)

    for match in re.finditer(
        r"\bredmi\s+k(\d{1,2})(?:\s+(lite|pro|max|plus|ultra))?\b",
        normalized,
    ):
        model, variant = match.groups()
        base = f"redmi_k{model}"
        if variant:
            base = f"{base}_{variant}"
        keys.add(base)

    for line in ("spark", "pova", "camon", "pop"):
        for match in re.finditer(
            rf"\btecno\s+{line}\s+(\d{{1,2}}[a-z]?)(?:\s+(lite|pro|max|plus|premier))?(?:\s+(4g|5g))?\b",
            normalized,
        ):
            model, variant, network = match.groups()
            base = f"tecno_{line}_{model}"
            if variant:
                base = f"{base}_{variant}"
            _add_key_with_optional_network(keys, base, network)

    for match in re.finditer(
        r"\btecno\s+pova\s+neo\s+(\d{1,2}[a-z]?)(?:\s+(lite|pro|max|plus))?(?:\s+(4g|5g))?\b",
        normalized,
    ):
        model, variant, network = match.groups()
        base = f"tecno_pova_neo_{model}"
        if variant:
            base = f"{base}_{variant}"
        _add_key_with_optional_network(keys, base, network)

    for match in re.finditer(
        r"\binfinix\s+note\s+(\d{1,2}[a-z]?)(?:\s+(lite|pro|max|plus))?(?:\s+(4g|5g|2023))?\b",
        normalized,
    ):
        model, variant, network = match.groups()
        base = f"infinix_note_{model}"
        if variant:
            base = f"{base}_{variant}"
        _add_key_with_optional_network(keys, base, network)

    for match in re.finditer(
        r"\binfinix\s+zero\s+(\d{1,2}[a-z]?)(?:\s+(lite|pro|max|plus|ultra))?(?:\s+(4g|5g))?\b",
        normalized,
    ):
        model, variant, network = match.groups()
        base = f"infinix_zero_{model}"
        if variant:
            base = f"{base}_{variant}"
        _add_key_with_optional_network(keys, base, network)

    for match in re.finditer(
        r"\bmeizu\s+([a-z]\d{1,2}[a-z]?)(?:\s+(lite|pro|max|plus))?\b",
        normalized,
    ):
        model, variant = match.groups()
        base = f"meizu_{model}"
        if variant:
            base = f"{base}_{variant}"
        keys.add(base)

    for match in re.finditer(
        r"\bmeizu\s+m(\d{1,2}[a-z]?)\s+note\b",
        normalized,
    ):
        model = match.group(1)
        keys.add(f"meizu_m{model}_note")

    for match in re.finditer(
        r"\bmeizu\s+note\s+(\d{1,2})(?:\s+(lite|pro|max|plus))?(?:\s+(4g|5g))?\b",
        normalized,
    ):
        model, variant, network = match.groups()
        base = f"meizu_note_{model}"
        if variant:
            base = f"{base}_{variant}"
        _add_key_with_optional_network(keys, base, network)

    for match in re.finditer(
        r"\bmeizu\s+(?:mblu\s+)?(\d{1,2})(?:\s+(lite|pro|max|plus|note))?(?:\s+(4g|5g))?\b",
        normalized,
    ):
        model, variant, network = match.groups()
        base = f"meizu_{model}"
        if variant:
            base = f"{base}_{variant}"
        _add_key_with_optional_network(keys, base, network)

    for match in re.finditer(
        r"\bzte\s+nubia\s+([a-z]?\d{1,3}[a-z]?)(?:\s+(lite|pro|max|plus|ultra))?\b",
        normalized,
    ):
        model, variant = match.groups()
        base = f"zte_nubia_{model}"
        if variant:
            base = f"{base}_{variant}"
        keys.add(base)

    for match in re.finditer(
        r"\bzte\s+nubia\s+(flip)\s+(\d{1,2})(?:\s+(lite|pro|max|plus|ultra))?(?:\s+(4g|5g))?\b",
        normalized,
    ):
        line, model, variant, network = match.groups()
        base = f"zte_nubia_{line}_{model}"
        if variant:
            base = f"{base}_{variant}"
        _add_key_with_optional_network(keys, base, network)

    for match in re.finditer(r"\balcatel\s+ot\s+(\d{3,5}[a-z]?)\b", normalized):
        keys.add(f"alcatel_ot_{match.group(1)}")

    for match in re.finditer(
        r"\bzte\s+blade\s+([a-z]?\d{1,3}[a-z]?)(?:\s+(lite|pro|max|plus|ultra))?\b",
        normalized,
    ):
        model, variant = match.groups()
        base = f"zte_blade_{model}"
        if variant:
            base = f"{base}_{variant}"
        keys.add(base)

    for match in re.finditer(
        r"\bumidigi\s+([a-z]\d{1,3}[a-z]?)(?:\s+(lite|pro|max|plus|ultra))?\b",
        normalized,
    ):
        model, variant = match.groups()
        base = f"umidigi_{model}"
        if variant:
            base = f"{base}_{variant}"
        keys.add(base)

    for match in re.finditer(
        r"\bulefone\s+armor\s+([a-z]?\d{1,3}[a-z]?)(?:\s+(lite|pro|max|plus|ultra))?\b",
        normalized,
    ):
        model, variant = match.groups()
        base = f"ulefone_armor_{model}"
        if variant:
            base = f"{base}_{variant}"
        keys.add(base)

    for match in re.finditer(
        r"\bgoogle\s+pixel\s+(\d{1,2}[a-z]?)(?:\s+(lite|pro|max|plus|xl|fold))?\b",
        normalized,
    ):
        model, variant = match.groups()
        base = f"google_pixel_{model}"
        if variant:
            base = f"{base}_{variant}"
        keys.add(base)

    for match in re.finditer(
        r"\bhtc\s+u\s+(ultra|play)\b",
        normalized,
    ):
        keys.add(f"htc_u_{match.group(1)}")

    for match in re.finditer(
        r"\basus\s+zenfone\s+(\d{1,2})(?:\s+(lite|pro|max|plus|ultra))?\b",
        normalized,
    ):
        model, variant = match.groups()
        base = f"asus_zenfone_{model}"
        if variant:
            base = f"{base}_{variant}"
        keys.add(base)

    if "asus" in normalized or "zenfone" in normalized:
        for match in re.finditer(r"\b(zc\d{3}[a-z]{2})\b", normalized):
            keys.add(f"asus_{match.group(1)}")

    for match in re.finditer(
        r"\bsony\s+xperia\s+(\d{1,2})(?:\s+(i{1,3}|iv|v|lite|pro|max|plus|ultra))?\b",
        normalized,
    ):
        model, variant = match.groups()
        base = f"sony_xperia_{model}"
        if variant:
            base = f"{base}_{variant}"
        keys.add(base)

    for match in re.finditer(
        r"\bsony\s+xperia\s+([a-z]\d{1,2})(?:\s+(lite|pro|max|plus|ultra))?\b",
        normalized,
    ):
        model, variant = match.groups()
        base = f"sony_xperia_{model}"
        if variant:
            base = f"{base}_{variant}"
        keys.add(base)

    for match in re.finditer(r"\b(?:sony(?:\s+ericsson)?\s+)?xperia\s+([a-z])\b", normalized):
        keys.add(f"sony_xperia_{match.group(1)}")

    for match in re.finditer(r"\bsony(?:\s+ericsson)?\s+([a-z]{1,3}\d{2,4}i?)\b", normalized):
        keys.add(f"sony_ericsson_{match.group(1)}")

    if "sony" in normalized or "xperia" in normalized:
        for match in re.finditer(r"\b(c\d{4}|lt\d{2}i?|st\d{2}i?|mt\d{2}i?)\b", normalized):
            code = match.group(1)
            if code.startswith(("lt", "st", "mt")) and not code.endswith("i"):
                code = f"{code}i"
            keys.add(f"sony_ericsson_{code}")

    for match in re.finditer(
        r"\bsony(?:\s+ericsson)?\s+(?:lt|st|mt)(\d{2})i?\s+xperia\s+([a-z])\b",
        normalized,
    ):
        model, line = match.groups()
        keys.add(f"sony_ericsson_lt{model}i")
        keys.add(f"sony_xperia_{line}")

    for match in re.finditer(r"\bsony\s+.*\bxz(\d)(?:\s+(compact|premium))?\b", normalized):
        model, variant = match.groups()
        base = f"sony_xperia_xz{model}"
        if variant:
            base = f"{base}_{variant}"
        keys.add(base)

    for match in re.finditer(
        r"\boppo\s+a(\d{1,3}[a-z]?)(?:\s+(lite|pro|max|plus))?(?:\s+(4g|5g))?\b",
        normalized,
    ):
        model, variant, network = match.groups()
        base = f"oppo_a{model}"
        if variant:
            base = f"{base}_{variant}"
        _add_key_with_optional_network(keys, base, network)

    for match in re.finditer(
        r"\boppo\s+([fkx]\d{1,3}[a-z]?)(?:\s+(lite|pro|max|plus))?(?:\s+(4g|5g))?\b",
        normalized,
    ):
        model, variant, network = match.groups()
        base = f"oppo_{model}"
        if variant:
            base = f"{base}_{variant}"
        _add_key_with_optional_network(keys, base, network)

    for match in re.finditer(
        r"\boppo\s+reno\s+(\d{1,2}[a-z]?)(?:\s+(lite|pro|max|plus|f|t))?(?:\s+(4g|5g))?\b",
        normalized,
    ):
        model, variant, network = match.groups()
        base = f"oppo_reno_{model}"
        if variant:
            base = f"{base}_{variant}"
        _add_key_with_optional_network(keys, base, network)

    for match in re.finditer(
        r"\boppo\s+find\s+x(\d{1,2})(?:\s+(lite|pro|max|plus|ultra))?(?:\s+(4g|5g))?\b",
        normalized,
    ):
        model, variant, network = match.groups()
        base = f"oppo_find_x{model}"
        if variant:
            base = f"{base}_{variant}"
        _add_key_with_optional_network(keys, base, network)

    for match in re.finditer(
        r"\biqoo\s+([a-z]?\d{1,3}[a-z]?)(?:\s+(lite|pro|max|plus|ultra))?(?:\s+(4g|5g))?\b",
        normalized,
    ):
        model, variant, network = match.groups()
        base = f"vivo_iqoo_{model}"
        if variant:
            base = f"{base}_{variant}"
        _add_key_with_optional_network(keys, base, network)

    for match in re.finditer(
        r"\brealme\s+gt\s+neo\s+(\d{1,2}[a-z]?)(?:\s+(lite|pro|max|plus|ultra|t))?(?:\s+(4g|5g))?\b",
        normalized,
    ):
        model, variant, network = match.groups()
        base = f"realme_gt_neo_{model}"
        if variant:
            base = f"{base}_{variant}"
        _add_key_with_optional_network(keys, base, network)

    for match in re.finditer(
        r"\brealme\s+(gt|note|narzo|q)?\s?([a-z]?\d{1,3}[a-z]?)"
        r"(?:\s+(lite|pro|max|plus|ultra|t))?"
        r"(?:\s+(4g|5g))?\b",
        normalized,
    ):
        line, model, variant, network = match.groups()
        base = f"realme_{line + '_' if line else ''}{model}"
        if variant:
            base = f"{base}_{variant}"
        _add_key_with_optional_network(keys, base, network)

    for match in re.finditer(
        r"\bitel\s+(vision\s+)?([aps]\d{1,3}[a-z]?|\d{1,2})(?:\s+(lite|pro|max|plus))?\b",
        normalized,
    ):
        line, model, variant = match.groups()
        prefix = "itel_vision" if line else "itel"
        base = f"{prefix}_{model}"
        if variant:
            base = f"{base}_{variant}"
        keys.add(base)

    for match in re.finditer(
        r"\bvivo\s+([tvxy]\d{1,3}[a-z]?)(?:\s+(lite|pro|max|plus))?(?:\s+(4g|5g))?\b",
        normalized,
    ):
        model, variant, network = match.groups()
        base = f"vivo_{model}"
        if variant:
            base = f"{base}_{variant}"
        _add_key_with_optional_network(keys, base, network)

    for match in re.finditer(
        r"\blg\s+([xld]\s*[a-z]?\d{1,3}[a-z]?|l\s+bello|x\s+style|x\s+max)(?:\s+(lite|pro|max|plus))?\b",
        normalized,
    ):
        model, variant = match.groups()
        base = "lg_" + model.replace(" ", "_")
        if variant:
            base = f"{base}_{variant}"
        keys.add(base)

    for match in re.finditer(r"\blg\s+((?:kg|mg|ku|ke|kp)\d{3,4}[a-z]?)\b", normalized):
        keys.add(f"lg_{match.group(1)}")

    for match in re.finditer(
        r"\bdoogee\s+([a-z]\d{0,3}[a-z]?)(?:\s+(lite|pro|max|plus))?\b",
        normalized,
    ):
        model, variant = match.groups()
        base = "doogee_" + model.replace(" ", "_")
        if variant:
            base = f"{base}_{variant}"
        keys.add(base)

    for match in re.finditer(
        r"\bdoogee\s+([a-z])\s+(max|pro|plus|ultra)\b",
        normalized,
    ):
        model, variant = match.groups()
        keys.add(f"doogee_{model}_{variant}")

    for match in re.finditer(
        r"\btcl\s+(\d{1,3}[a-z]?)(?:\s+(lite|pro|max|plus|y|e))?\b",
        normalized,
    ):
        model, variant = match.groups()
        base = f"tcl_{model}"
        if variant:
            base = f"{base}_{variant}"
        keys.add(base)

    for match in re.finditer(
        r"\bnokia\s+([cgx]?\d{1,2}(?:\s+\d)?[a-z]?)(?:\s+(lite|pro|max|plus))?\b",
        normalized,
    ):
        model, variant = match.groups()
        base = "nokia_" + model.replace(" ", "_")
        if variant:
            base = f"{base}_{variant}"
        keys.add(base)

    for match in re.finditer(r"\bnokia\s+(\d{3,4}[a-z]?)\b", normalized):
        keys.add(f"nokia_{match.group(1)}")

    for match in re.finditer(r"\bnokia\s+lumia\s+(\d{3,4}[a-z]?)\b", normalized):
        keys.add(f"nokia_lumia_{match.group(1)}")

    if re.search(r"\bnokia\s+xl\b", normalized):
        keys.add("nokia_xl")

    for match in re.finditer(
        r"\bnothing\s+phone\s+(\d{1,2}[a-z]?)(?:\s+(lite|pro|max|plus|ultra))?\b",
        normalized,
    ):
        model, variant = match.groups()
        base = f"nothing_phone_{model}"
        if variant:
            base = f"{base}_{variant}"
        keys.add(base)

    for match in re.finditer(
        r"\boneplus\s+([a-z]?\d{1,2}[a-z]?|x)(?:\s+(lite|pro|max|plus|t))?\b",
        normalized,
    ):
        model, variant = match.groups()
        base = f"oneplus_{model}"
        if variant:
            base = f"{base}_{variant}"
        keys.add(base)

    for match in re.finditer(
        r"\bmotorola\s+(?:moto\s+)?([cw]\d{1,3}|x\s+play)(?:\s+(lite|pro|max|plus))?\b",
        normalized,
    ):
        model, variant = match.groups()
        base = "motorola_" + model.replace(" ", "_")
        if variant:
            base = f"{base}_{variant}"
        keys.add(base)

    for match in re.finditer(
        r"\bmotorola\s+razr\s+(\d{1,2})(?:\s+(lite|pro|max|plus|ultra))?\b",
        normalized,
    ):
        model, variant = match.groups()
        base = f"motorola_razr_{model}"
        if variant:
            base = f"{base}_{variant}"
        keys.add(base)

    for match in re.finditer(
        r"\boukitel\s+([a-z]{1,3}\d{1,3}[a-z]?)(?:\s+(lite|pro|max|plus))?\b",
        normalized,
    ):
        model, variant = match.groups()
        base = f"oukitel_{model}"
        if variant:
            base = f"{base}_{variant}"
        keys.add(base)

    for match in re.finditer(
        r"\biphone\s+(\d{1,2})(s|c|e)?(?:\s+(pro\s+max|plus|pro|max|mini))?\b",
        normalized,
    ):
        model, suffix, variant = match.groups()
        base = f"iphone_{model}{suffix or ''}"
        if variant:
            base = f"{base}_{variant.replace(' ', '_')}"
        keys.add(base)

    for match in re.finditer(
        r"\biphone\s+(x|xr|xs)(?:\s+(pro\s+max|max|plus|pro|mini))?\b",
        normalized,
    ):
        model, variant = match.groups()
        base = f"iphone_{model}"
        if variant:
            base = f"{base}_{variant.replace(' ', '_')}"
        keys.add(base)

    for match in re.finditer(
        r"\bipad(?:\s+(mini|air|pro))?(?:\s+(\d{1,2}))?",
        normalized,
    ):
        line, model = match.groups()
        if line or model:
            base = f"ipad_{line or 'base'}"
            if model:
                base = f"{base}_{model}"
            keys.add(base)
        elif "apple" in normalized:
            keys.add("ipad_base")

    for pattern in (
        r"\bsamsung\s+(?:galaxy\s+)?([am]\d{1,3}[a-z]?)\b",
        r"\bgalaxy\s+([am]\d{1,3}[a-z]?)\b",
    ):
        for match in re.finditer(pattern, normalized):
            keys.add(f"samsung_{match.group(1)}")

    for match in re.finditer(
        r"\b(?:samsung\s+)?galaxy\s+s(\d{1,2})(?:\s+(lite|fe|plus|ultra|edge))?\b",
        normalized,
    ):
        model, variant = match.groups()
        base = f"samsung_s{model}"
        if variant:
            base = f"{base}_{variant}"
        keys.add(base)

    if "samsung" in normalized or "galaxy" in normalized:
        for match in re.finditer(r"\b([a-z]\d{3,4}[a-z]?)\b", normalized):
            keys.add(f"samsung_{match.group(1)}")

    for match in re.finditer(
        r"\b(?:samsung\s+)?galaxy\s+note\s+(\d{1,2})(?:\s+(lite|fe|plus|ultra))?\b",
        normalized,
    ):
        model, variant = match.groups()
        base = f"samsung_note_{model}"
        if variant:
            base = f"{base}_{variant}"
        keys.add(base)

    for match in re.finditer(r"\bsamsung\s+pixon\s+([a-z]?\d{1,4}[a-z]?)\b", normalized):
        keys.add(f"samsung_pixon_{match.group(1)}")

    for match in re.finditer(r"\bsamsung\s+c(\d{3,5})\b", normalized):
        keys.add(f"samsung_c{match.group(1)}")

    for match in re.finditer(r"\bnintendo\s+(3ds|2ds|ds)(?:\s+(ll|xl|lite))?\b", normalized):
        model, variant = match.groups()
        base = f"nintendo_{model}"
        if variant:
            base = f"{base}_{variant}"
        keys.add(base)

    return keys


def _competitor_device_model_text(item: CompetitorItem) -> str:
    return " ".join(
        value
        for value in (
            item.name,
            item.normalized_title,
            item.parsed_device_model,
            item.parsed_device_variant,
        )
        if value
    )


def _leaf_device_model_keys(keys: set[str]) -> set[str]:
    return {key for key in keys if not any(other.startswith(f"{key}_") for other in keys)}


def _device_model_keys_overlap(left: set[str], right: set[str]) -> bool:
    if not left or not right:
        return False
    if left.isdisjoint(right):
        return False

    left_leaf = _leaf_device_model_keys(left)
    right_leaf = _leaf_device_model_keys(right)
    if left_leaf and right_leaf:
        return not left_leaf.isdisjoint(right_leaf)
    return True


def _display_text_model_conflict(item: CompetitorItem, product: Product) -> bool:
    competitor_keys = _extract_device_model_keys(_competitor_device_model_text(item))
    product_keys = _extract_device_model_keys(product.name)
    if competitor_keys and product_keys:
        return not _device_model_keys_overlap(competitor_keys, product_keys)
    return False


def _text_model_conflict(item: CompetitorItem, product: Product) -> bool:
    competitor_keys = _extract_device_model_keys(_competitor_device_model_text(item))
    product_keys = _extract_device_model_keys(product.name)
    if competitor_keys and product_keys:
        return not _device_model_keys_overlap(competitor_keys, product_keys)
    return False


def _compatibility_model_keys(*values: str | None) -> set[str]:
    keys: set[str] = set()
    text = " ".join(value for value in values if value)
    if not text:
        return keys

    parsed_keys = _extract_device_model_keys(text)
    if parsed_keys:
        keys.update(parsed_keys)

    for value in (*values, text):
        normalized = _normalize_model_text(value)
        if not normalized:
            continue
        candidates = {normalized, re.sub(r"\b([a-zа-я])\s+(\d{1,3})\b", r"\1\2", normalized)}
        parts = normalized.split()
        if parts and parts[0] in MODEL_KEY_BRANDS:
            without_brand = " ".join(parts[1:])
            if without_brand:
                candidates.add(without_brand)
                candidates.add(re.sub(r"\b([a-zа-я])\s+(\d{1,3})\b", r"\1\2", without_brand))

        without_codes = re.sub(r"\([^)]*\)", " ", value or "")
        without_codes = re.sub(
            r"\b(?=[a-zа-я0-9]*\d)(?=[a-zа-я0-9]*[a-zа-я])[a-zа-я0-9]{4,}\b",
            " ",
            without_codes.lower(),
        )
        without_codes = _normalize_model_text(without_codes)
        if without_codes:
            candidates.add(without_codes)
            candidates.add(re.sub(r"\b([a-zа-я])\s+(\d{1,3})\b", r"\1\2", without_codes))
            parts = without_codes.split()
            if parts and parts[0] in MODEL_KEY_BRANDS:
                without_brand = " ".join(parts[1:])
                if without_brand:
                    candidates.add(without_brand)
                    candidates.add(re.sub(r"\b([a-zа-я])\s+(\d{1,3})\b", r"\1\2", without_brand))

        keys.update(candidate for candidate in candidates if candidate)

    return keys


def _display_phone_model_conflict(
    item: CompetitorItem,
    product: Product,
    *,
    competitor_phone_model_ids: set[int] | None = None,
    product_phone_model_ids: set[int] | None = None,
    competitor_phone_model_keys: set[str] | None = None,
    product_phone_model_keys: set[str] | None = None,
) -> bool:
    competitor_ids = set(competitor_phone_model_ids or ())
    product_ids = set(product_phone_model_ids or ())
    competitor_keys = set(competitor_phone_model_keys or ())
    product_keys = set(product_phone_model_keys or ())

    if not competitor_ids:
        compatibilities = getattr(item, "compatibilities", [])
        competitor_ids = {
            compatibility.phone_model_id
            for compatibility in compatibilities
            if compatibility.phone_model_id
        }
        for compatibility in compatibilities:
            competitor_keys.update(
                _compatibility_model_keys(
                    compatibility.device_brand,
                    compatibility.device_model,
                    compatibility.device_variant,
                    (
                        " ".join(
                            filter(
                                None,
                                [
                                    compatibility.phone_model.brand,
                                    compatibility.phone_model.model_name,
                                    compatibility.phone_model.variant,
                                ],
                            )
                        )
                        if compatibility.phone_model
                        else None
                    ),
                )
            )
    if not product_ids:
        links = getattr(product, "phone_model_links", [])
        product_ids = {link.phone_model_id for link in links if link.phone_model_id}
        for link in links:
            product_keys.update(
                _compatibility_model_keys(
                    link.raw_value,
                    (
                        " ".join(
                            filter(
                                None,
                                [
                                    link.phone_model.brand,
                                    link.phone_model.model_name,
                                    link.phone_model.variant,
                                ],
                            )
                        )
                        if link.phone_model
                        else None
                    ),
                )
            )

    if competitor_ids and product_ids:
        if not competitor_ids.isdisjoint(product_ids):
            return False
        competitor_codes = _extract_device_codes(_competitor_device_code_text(item))
        product_codes = _extract_device_codes(product.name)
        if competitor_codes and product_codes and not competitor_codes.isdisjoint(product_codes):
            return False
        competitor_text_keys = _extract_device_model_keys(_competitor_device_model_text(item))
        product_text_keys = _extract_device_model_keys(product.name)
        if (
            competitor_text_keys
            and product_text_keys
            and not competitor_text_keys.isdisjoint(product_text_keys)
        ):
            return False
        if competitor_keys and product_keys:
            return competitor_keys.isdisjoint(product_keys)
    return False


def _port_type_conflict(item_text: str, product_text: str, attrs: dict[str, Any] | None) -> bool:
    attr_port = None
    if attrs and attrs.get("type"):
        attr_port = _extract_port_type(str(attrs.get("type")))
    item_port = attr_port or _extract_port_type(item_text)
    product_port = _extract_port_type(product_text)
    if item_port and product_port:
        return item_port != product_port
    return False


def _arbiter_prompt(item: CompetitorItem, candidates: list[Product]) -> str:
    lines = [
        f"Competitor item: {item.name}",
        f"Normalized title: {item.normalized_title}",
        f"Item type: {item.item_type}",
        "Candidates:",
    ]
    for prod in candidates:
        lines.append(
            f"- product_id={prod.id} name={prod.name} brand={prod.brand} category={prod.category}"
        )
    return "\n".join(lines)


def match_items(
    session: Session,
    *,
    embeddings_dir: Path,
    min_embed_score: float,
    min_gap: float,
    top_k: int,
    top_k_llm: int,
    use_llm_arbiter: bool,
    limit: int | None,
    only_null: bool,
    include_status: list[str] | None,
    force: bool,
    dry_run: bool,
    sample_limit: int,
    samples_file: str | None,
    report_file: str | None,
    report_limit: int,
    report_csv_file: str | None,
    sources: list[str] | None = None,
    first_seen_after: date | None = None,
    last_seen_after: date | None = None,
    auto_accept_unique: bool = False,
    auto_accept_min_score: float = 0.80,
) -> dict[str, int]:
    product_matrix, product_index = _load_embeddings("our_catalog", embeddings_dir)
    if not product_index.get("meta", {}).get("normalized", False):
        norms = np.linalg.norm(product_matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        product_matrix = product_matrix / norms
    product_rows = {int(pid): meta["row"] for pid, meta in product_index.get("items", {}).items()}
    if not product_rows:
        raise RuntimeError("Product embeddings index is empty.")

    competitor_matrix = None
    competitor_index = None
    comp_index_path = embeddings_dir / "competitor_items_index.json"
    if comp_index_path.exists():
        competitor_index = json.loads(comp_index_path.read_text(encoding="utf-8"))
        comp_matrix_file = competitor_index.get("meta", {}).get("matrix_file")
        comp_path = (
            embeddings_dir / comp_matrix_file
            if comp_matrix_file
            else embeddings_dir / "competitor_items_embeddings.npy"
        )
        if comp_path.exists():
            competitor_matrix = np.load(comp_path)

    product_id_by_row = {row: pid for pid, row in product_rows.items()}
    products = {
        p.id: p
        for p in session.execute(select(Product).where(Product.is_active.is_(True))).scalars()
    }
    product_item_type = {p.id: _infer_item_type(p.name or "") for p in products.values()}
    product_brand = {
        p.id: _normalize_brand(p.brand) or _extract_brand_from_text(p.name or "")
        for p in products.values()
    }
    phone_model_text_by_id = {
        phone_model.id: " ".join(
            filter(
                None,
                [phone_model.brand, phone_model.model_name, phone_model.variant],
            )
        )
        for phone_model in session.execute(select(PhoneModel)).scalars()
    }
    product_phone_model_ids: dict[int, set[int]] = {}
    product_phone_model_keys: dict[int, set[str]] = {}
    for product_id, phone_model_id, raw_value in session.execute(
        select(
            ProductPhoneModel.product_id,
            ProductPhoneModel.phone_model_id,
            ProductPhoneModel.raw_value,
        )
    ):
        product_phone_model_ids.setdefault(product_id, set()).add(phone_model_id)
        product_phone_model_keys.setdefault(product_id, set()).update(
            _compatibility_model_keys(raw_value, phone_model_text_by_id.get(phone_model_id))
        )

    query = select(CompetitorItem)
    if sources:
        query = query.where(CompetitorItem.competitor.in_(sources))
    if first_seen_after:
        query = query.where(func.date(CompetitorItem.first_seen_at) >= first_seen_after)
    if last_seen_after:
        query = query.where(func.date(CompetitorItem.last_seen_at) >= last_seen_after)
    if only_null:
        query = query.where(~CompetitorItem.match.has())
    if limit:
        query = query.limit(limit)

    items = list(session.execute(query).scalars())
    item_ids = [item.id for item in items]
    competitor_phone_model_ids: dict[int, set[int]] = {}
    competitor_phone_model_keys: dict[int, set[str]] = {}
    competitor_item_ids_with_compat: set[int] = set()
    if item_ids:
        competitor_item_ids_with_compat = set(
            session.execute(
                select(CompetitorItemCompatibility.competitor_item_id)
                .where(CompetitorItemCompatibility.competitor_item_id.in_(item_ids))
                .distinct()
            ).scalars()
        )
        for item_id, phone_model_id, device_brand, device_model, device_variant in session.execute(
            select(
                CompetitorItemCompatibility.competitor_item_id,
                CompetitorItemCompatibility.phone_model_id,
                CompetitorItemCompatibility.device_brand,
                CompetitorItemCompatibility.device_model,
                CompetitorItemCompatibility.device_variant,
            ).where(
                CompetitorItemCompatibility.competitor_item_id.in_(item_ids),
                CompetitorItemCompatibility.phone_model_id.is_not(None),
            )
        ):
            competitor_phone_model_ids.setdefault(item_id, set()).add(phone_model_id)
            competitor_phone_model_keys.setdefault(item_id, set()).update(
                _compatibility_model_keys(
                    device_brand,
                    device_model,
                    device_variant,
                    phone_model_text_by_id.get(phone_model_id),
                )
            )

    llm_client = None
    base_url = os.environ.get("LOCAL_LLM_BASE_URL")
    model = os.environ.get("LOCAL_LLM_CHAT_MODEL")
    if use_llm_arbiter:
        if not base_url or not model:
            raise RuntimeError(
                "LOCAL_LLM_BASE_URL и LOCAL_LLM_CHAT_MODEL должны быть заданы для LLM арбитра"
            )
        llm_client = httpx.Client(timeout=30.0)

    stats = {
        "processed": 0,
        "matched": 0,
        "needs_review": 0,
        "ambiguous": 0,
        "skipped_no_embedding": 0,
        "skipped_no_candidates": 0,
        "skipped_accepted": 0,
        "pruned_no_candidates": 0,
    }
    samples: dict[str, list] = {"needs_review": [], "ambiguous": [], "skipped_no_candidates": []}
    report_rows: list[dict[str, Any]] = []

    for item in items:
        stats["processed"] += 1
        existing_match = item.match
        if existing_match:
            if existing_match.status == CompetitorItemMatchStatus.ACCEPTED and not force:
                stats["skipped_accepted"] += 1
                continue
            if (
                existing_match.status == CompetitorItemMatchStatus.REJECTED
                and existing_match.method == CompetitorItemMatchMethod.MANUAL
                and not force
            ):
                stats["skipped_accepted"] += 1
                continue
            if existing_match.method == CompetitorItemMatchMethod.MANUAL and not force:
                stats["skipped_accepted"] += 1
                continue
            if include_status and existing_match.status.value not in include_status and not force:
                continue

        text = compose_competitor_text(item.normalized_title or item.name, item.attrs_json)
        if not text:
            stats["skipped_no_embedding"] += 1
            continue

        if competitor_index and competitor_matrix is not None:
            meta = competitor_index.get("items", {}).get(str(item.id))
            if meta and meta.get("row") is not None and meta["row"] < competitor_matrix.shape[0]:
                item_vec = competitor_matrix[meta["row"]]
            else:
                item_vec = None
        else:
            item_vec = None

        if item_vec is None:
            client = EmbeddingClient(model=product_index.get("meta", {}).get("model"))
            embeddings = client.embed_texts([text])
            if not embeddings:
                stats["skipped_no_embedding"] += 1
                continue
            item_vec = _normalize(np.array(embeddings[0], dtype=np.float32))

        scores = product_matrix @ item_vec
        if scores.size == 0:
            stats["skipped_no_candidates"] += 1
            continue
        k = min(top_k, scores.size)
        top_idx = np.argpartition(scores, -k)[-k:]
        top_sorted = top_idx[np.argsort(scores[top_idx])[::-1]]

        candidates: list[tuple[int, float]] = []
        for row in top_sorted:
            pid = product_id_by_row.get(int(row))
            if pid is None:
                continue
            candidates.append((pid, float(scores[row])))

        if not candidates:
            stats["skipped_no_candidates"] += 1
            continue

        filtered: list[tuple[int, float]] = []
        frame_review_product_ids: set[int] = set()
        code_review_product_ids: set[int] = set()
        quality_review_product_ids: set[int] = set()
        item_type = _effective_item_type(item)
        competitor_display_has_frame = (
            _competitor_display_has_frame(item) if item_type == "display" else None
        )
        competitor_display_color = (
            _competitor_display_color(item) if item_type == "display" else None
        )
        competitor_display_type = _competitor_display_type(item) if item_type == "display" else None
        competitor_display_quality = (
            _competitor_display_quality(item) if item_type == "display" else None
        )
        competitor_display_has_touch = (
            _competitor_display_has_touch(item) if item_type == "display" else None
        )
        competitor_display_backlight = (
            _competitor_display_backlight(item) if item_type == "display" else None
        )
        competitor_display_matrix_tags = (
            _competitor_display_matrix_tags(item) if item_type == "display" else set()
        )
        competitor_display_construction = (
            _competitor_display_construction(item) if item_type == "display" else None
        )
        competitor_display_refresh_rate_hz = (
            _competitor_display_refresh_rate_hz(item) if item_type == "display" else None
        )
        item_brand = _normalize_brand(item.parsed_device_brand)
        if not item_brand:
            item_brand = _extract_brand_from_text(item.normalized_title or item.name or "")
        code_overlap_product_ids: set[int] = set()
        if item_type == "display":
            for pid, _ in candidates:
                prod = products.get(pid)
                if not prod:
                    continue
                if not basic_candidate_guardrails(item, prod).allowed:
                    continue
                prod_type = product_item_type.get(pid)
                if item_type and prod_type and item_type != prod_type:
                    continue
                if item_brand and product_brand.get(pid) and item_brand != product_brand.get(pid):
                    continue
                if _variant_conflict(item.normalized_title or item.name or "", prod.name or ""):
                    continue
                if _device_conflict(item.normalized_title or item.name or "", prod.name or ""):
                    continue
                if _display_model_code_overlap(item, prod):
                    code_overlap_product_ids.add(pid)
        for pid, score in candidates:
            prod = products.get(pid)
            if not prod:
                continue
            if not basic_candidate_guardrails(item, prod).allowed:
                continue
            prod_type = product_item_type.get(pid)
            if item_type and prod_type and item_type != prod_type:
                continue
            if item_brand and product_brand.get(pid) and item_brand != product_brand.get(pid):
                continue
            if _variant_conflict(item.normalized_title or item.name or "", prod.name or ""):
                continue
            if _device_conflict(item.normalized_title or item.name or "", prod.name or ""):
                continue
            if item_type in MODEL_GUARDRAIL_ITEM_TYPES and _text_model_conflict(item, prod):
                continue
            if item_type == "display" and _display_model_code_blocks(item, prod):
                continue
            if (
                item_type == "display"
                and code_overlap_product_ids
                and pid not in code_overlap_product_ids
                and _display_model_code_conflict(item, prod)
            ):
                continue
            if item_type == "display" and _display_model_code_requires_review(item, prod):
                code_review_product_ids.add(pid)
            if item_type == "display" and _display_phone_model_conflict(
                item,
                prod,
                competitor_phone_model_ids=competitor_phone_model_ids.get(item.id),
                product_phone_model_ids=product_phone_model_ids.get(pid),
                competitor_phone_model_keys=competitor_phone_model_keys.get(item.id),
                product_phone_model_keys=product_phone_model_keys.get(pid),
            ):
                continue
            if item_type == "display" and _display_text_model_conflict(item, prod):
                continue
            if item_type == "display" and _display_quality_conflict(
                prod, competitor_display_quality
            ):
                continue
            if item_type == "display" and _display_quality_requires_review(
                prod, competitor_display_quality
            ):
                quality_review_product_ids.add(pid)
            if item_type == "display" and _display_touch_conflict(
                prod, competitor_display_has_touch
            ):
                continue
            if item_type == "display" and _display_backlight_conflict(
                prod, competitor_display_backlight
            ):
                continue
            if item_type == "display" and _display_matrix_tags_conflict(
                prod, competitor_display_matrix_tags
            ):
                continue
            if item_type == "display" and _display_construction_conflict(
                prod, competitor_display_construction
            ):
                continue
            if item_type == "display" and _display_matrix_family_conflict(
                prod,
                competitor_display_type,
                competitor_display_construction,
            ):
                continue
            if item_type == "display" and _display_refresh_rate_conflict(
                prod, competitor_display_refresh_rate_hz
            ):
                continue
            if item_type == "battery" and _capacity_conflict(
                item.normalized_title or item.name or "",
                prod.name or "",
                item.attrs_json,
            ):
                continue
            if item_type == "display" and _display_type_conflict(
                item.normalized_title or item.name or "",
                prod.name or "",
                item.attrs_json,
            ):
                continue
            if item_type == "display" and display_frame_conflict(
                prod, competitor_display_has_frame
            ):
                continue
            if item_type == "display" and display_frame_requires_review(
                prod, competitor_display_has_frame
            ):
                frame_review_product_ids.add(pid)
            if item_type == "display" and _display_color_conflict(prod, competitor_display_color):
                continue
            if item_type in {"connector", "cable"} and _port_type_conflict(
                item.normalized_title or item.name or "",
                prod.name or "",
                item.attrs_json,
            ):
                continue
            filtered.append((pid, score))

        if not filtered:
            stats["skipped_no_candidates"] += 1
            if (
                existing_match
                and existing_match.method != CompetitorItemMatchMethod.MANUAL
                and existing_match.status != CompetitorItemMatchStatus.ACCEPTED
                and not dry_run
            ):
                session.delete(existing_match)
                stats["pruned_no_candidates"] += 1
            if len(samples["skipped_no_candidates"]) < sample_limit:
                samples["skipped_no_candidates"].append(
                    {
                        "competitor": item.competitor,
                        "external_id": item.external_id,
                        "name": item.name,
                    }
                )
            continue

        best_pid, best_score = filtered[0]
        second_score = filtered[1][1] if len(filtered) > 1 else None
        gap = best_score - second_score if second_score is not None else best_score

        status = CompetitorItemMatchStatus.SUGGESTED
        if best_score < min_embed_score:
            status = CompetitorItemMatchStatus.NEEDS_REVIEW
        elif gap < min_gap:
            status = CompetitorItemMatchStatus.AMBIGUOUS
        elif best_pid in frame_review_product_ids:
            status = CompetitorItemMatchStatus.NEEDS_REVIEW
        elif best_pid in code_review_product_ids:
            status = CompetitorItemMatchStatus.NEEDS_REVIEW
        elif best_pid in quality_review_product_ids:
            status = CompetitorItemMatchStatus.NEEDS_REVIEW
        elif (_as_date(item.first_seen_at) or date.min) >= UNSAFE_AUTO_ACCEPT_CUTOFF and (
            item.attrs_json is None or item.id not in competitor_item_ids_with_compat
        ):
            status = CompetitorItemMatchStatus.NEEDS_REVIEW

        method = CompetitorItemMatchMethod.EMBEDDING_AUTO
        llm_confidence = None
        candidate_rows = [
            {
                "product_id": pid,
                "name": products.get(pid).name if products.get(pid) else None,
                "article": products.get(pid).article if products.get(pid) else None,
                "score": score,
                "display_has_frame": (
                    products.get(pid).display_has_frame if products.get(pid) else None
                ),
                "display_modification_status": (
                    products.get(pid).display_modification_status if products.get(pid) else None
                ),
            }
            for pid, score in filtered[:top_k_llm]
        ]
        rationale: dict[str, Any] = {
            "best_score": best_score,
            "gap": gap,
            "filtered_candidates": candidate_rows,
        }
        if best_pid in frame_review_product_ids:
            best_product = products.get(best_pid)
            rationale["display_frame_review"] = {
                "product_display_has_frame": (
                    best_product.display_has_frame if best_product else None
                ),
                "competitor_has_frame": competitor_display_has_frame,
                "product_modification_status": (
                    best_product.display_modification_status if best_product else None
                ),
                "reason": (
                    "product_display_modification_conflict"
                    if best_product
                    and best_product.display_modification_status
                    == PRODUCT_DISPLAY_MODIFICATION_CONFLICT
                    else "display_frame_unknown_side"
                ),
            }
        if best_pid in code_review_product_ids:
            best_product = products.get(best_pid)
            rationale["display_model_code_review"] = {
                "competitor_codes": sorted(
                    _extract_device_codes(_competitor_device_code_text(item))
                ),
                "product_codes": (
                    sorted(_extract_device_codes(best_product.name)) if best_product else []
                ),
                "competitor_model_keys": sorted(
                    _extract_device_model_keys(_competitor_device_model_text(item))
                ),
                "product_model_keys": (
                    sorted(_extract_device_model_keys(best_product.name)) if best_product else []
                ),
                "reason": "model_text_overlap_but_device_codes_differ",
            }
        if best_pid in quality_review_product_ids:
            best_product = products.get(best_pid)
            rationale["display_quality_review"] = {
                "competitor_quality": competitor_display_quality,
                "product_quality": (
                    _product_display_quality(best_product) if best_product else None
                ),
                "reason": "display_quality_unknown_on_one_side",
            }

        if use_llm_arbiter and status == CompetitorItemMatchStatus.SUGGESTED and llm_client:
            candidates_for_llm = [
                products[pid] for pid, _ in filtered[:top_k_llm] if pid in products
            ]
            prompt = get_llm_match_arbiter_prompt()
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": _arbiter_prompt(item, candidates_for_llm)},
                ],
                "temperature": 0.0,
                "max_tokens": 200,
            }
            try:
                resp = llm_client.post(f"{base_url}/v1/chat/completions", json=payload)
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"]
                parsed = json.loads(content)
                selected_id = parsed.get("product_id")
                llm_confidence = parsed.get("confidence")
                rationale["llm"] = parsed
                if selected_id in {pid for pid, _ in filtered}:
                    best_pid = selected_id
                    for pid, score in filtered:
                        if pid == selected_id:
                            best_score = score
                            break
                    method = CompetitorItemMatchMethod.LLM_ARBITRATE
                    status = CompetitorItemMatchStatus.SUGGESTED
            except Exception:  # noqa: BLE001
                rationale["llm_error"] = "failed"

        if status == CompetitorItemMatchStatus.NEEDS_REVIEW:
            stats["needs_review"] += 1
            if len(samples["needs_review"]) < sample_limit:
                samples["needs_review"].append(
                    {
                        "competitor": item.competitor,
                        "external_id": item.external_id,
                        "name": item.name,
                        "best_score": best_score,
                        "gap": gap,
                        "candidate_ids": [pid for pid, _ in filtered[:top_k_llm]],
                    }
                )
        elif status == CompetitorItemMatchStatus.AMBIGUOUS:
            stats["ambiguous"] += 1
            if len(samples["ambiguous"]) < sample_limit:
                samples["ambiguous"].append(
                    {
                        "competitor": item.competitor,
                        "external_id": item.external_id,
                        "name": item.name,
                        "best_score": best_score,
                        "gap": gap,
                        "candidate_ids": [pid for pid, _ in filtered[:top_k_llm]],
                    }
                )
        else:
            stats["matched"] += 1

        if (report_file or report_csv_file) and len(report_rows) < report_limit:
            best_product = products.get(best_pid)
            report_rows.append(
                {
                    "competitor_item_id": item.id,
                    "competitor": item.competitor,
                    "external_id": item.external_id,
                    "name": item.name,
                    "item_type": item_type,
                    "normalized_title": item.normalized_title,
                    "parsed_device_brand": item.parsed_device_brand,
                    "parsed_device_model": item.parsed_device_model,
                    "parsed_device_variant": item.parsed_device_variant,
                    "llm_confidence": item.llm_confidence,
                    "parse_status": item.parse_status.value if item.parse_status else None,
                    "status": status.value,
                    "method": method.value,
                    "best_product_id": best_pid,
                    "best_product_name": best_product.name if best_product else None,
                    "best_product_article": best_product.article if best_product else None,
                    "best_product_brand": best_product.brand if best_product else None,
                    "best_product_category": best_product.category if best_product else None,
                    "best_product_quality": best_product.quality if best_product else None,
                    "competitor_quality_raw": (
                        _competitor_display_quality_raw(item) if item_type == "display" else None
                    ),
                    "competitor_mapped_1c_quality_raw": (
                        _competitor_display_mapped_1c_quality_raw(item)
                        if item_type == "display"
                        else None
                    ),
                    "competitor_normalized_quality": (
                        _competitor_display_quality(item) if item_type == "display" else None
                    ),
                    "product_quality_raw": (
                        _product_display_quality_raw(best_product) if best_product else None
                    ),
                    "product_normalized_quality": (
                        _product_display_quality(best_product) if best_product else None
                    ),
                    "best_product_display_type": (
                        best_product.display_type if best_product else None
                    ),
                    "best_product_color": best_product.color if best_product else None,
                    "best_score": best_score,
                    "gap": gap,
                    "candidates": candidate_rows,
                }
            )

        if dry_run:
            continue

        match = existing_match or CompetitorItemMatch(
            competitor_item_id=item.id,
            product_id=best_pid,
        )
        match.product_id = best_pid
        match.status = status
        match.method = method
        match.score_embed_best = best_score
        match.score_embed_gap = gap
        match.score_llm = llm_confidence
        match.final_score = best_score
        match.llm_confidence = llm_confidence
        match.rationale_json = rationale
        match.embed_model = product_index.get("meta", {}).get("model")
        match.embed_dim = product_index.get("meta", {}).get("dim")
        match.topk_used = top_k
        match.updated_at = datetime.now(UTC)
        session.add(match)

    if auto_accept_unique and not dry_run:
        stats["auto_accepted_unique"] = _auto_accept_unique_suggested_matches(
            session,
            min_score=auto_accept_min_score,
        )

    if not dry_run:
        session.commit()
    if llm_client:
        llm_client.close()
    if samples_file:
        payload = {"stats": stats, "samples": samples}
        with open(samples_file, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
    if report_file:
        payload = {"stats": stats, "items": report_rows}
        with open(report_file, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
    if report_csv_file:
        report_csv_file = Path(report_csv_file)
        report_csv_file.parent.mkdir(parents=True, exist_ok=True)
        with open(report_csv_file, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "competitor_item_id",
                    "competitor",
                    "external_id",
                    "name",
                    "item_type",
                    "normalized_title",
                    "parsed_device_brand",
                    "parsed_device_model",
                    "parsed_device_variant",
                    "llm_confidence",
                    "parse_status",
                    "status",
                    "method",
                    "best_product_id",
                    "best_product_name",
                    "best_product_article",
                    "best_product_brand",
                    "best_product_category",
                    "best_product_quality",
                    "competitor_quality_raw",
                    "competitor_mapped_1c_quality_raw",
                    "competitor_normalized_quality",
                    "product_quality_raw",
                    "product_normalized_quality",
                    "best_product_display_type",
                    "best_product_color",
                    "best_score",
                    "gap",
                    "candidates",
                ],
            )
            writer.writeheader()
            for row in report_rows:
                writer.writerow(
                    {
                        **row,
                        "candidates": json.dumps(row.get("candidates", []), ensure_ascii=False),
                    }
                )
    return stats


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Match competitor items using embeddings.")
    parser.add_argument("--limit", type=int, help="Limit records")
    parser.add_argument(
        "--only-null", action="store_true", help="Process only items without match (default)"
    )
    parser.add_argument(
        "--only-open", action="store_true", help="Process suggested/needs_review/ambiguous"
    )
    parser.add_argument(
        "--include-status", action="append", help="Process only specific match statuses"
    )
    parser.add_argument("--force", action="store_true", help="Overwrite accepted/manual matches")
    parser.add_argument("--top-k", type=int, default=None, help="Top-K candidates")
    parser.add_argument("--top-k-llm", type=int, default=None, help="Top-K for LLM arbiter")
    parser.add_argument("--min-embed-score", type=float, default=None, help="Min embed score")
    parser.add_argument("--min-gap", type=float, default=None, help="Min gap between #1 and #2")
    parser.add_argument("--use-llm-arbiter", action="store_true", help="Use LLM for final pick")
    parser.add_argument("--embeddings-dir", default=None, help="Embeddings directory")
    parser.add_argument("--dry-run", action="store_true", help="Do not write to DB")
    parser.add_argument("--source", action="append", help="Filter by competitor source")
    parser.add_argument(
        "--first-seen-after",
        help="Process competitor items with first_seen_at date >= YYYY-MM-DD",
    )
    parser.add_argument(
        "--last-seen-after",
        help="Process competitor items with last_seen_at date >= YYYY-MM-DD",
    )
    parser.add_argument("--sample-limit", type=int, default=10, help="Sample size for outputs")
    parser.add_argument("--samples-file", help="Write samples JSON to file")
    parser.add_argument("--report-file", help="Write match report JSON to file")
    parser.add_argument("--report-csv", help="Write match report CSV to file")
    parser.add_argument("--report-limit", type=int, default=1000, help="Max rows for report file")
    parser.add_argument(
        "--auto-accept-unique",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Auto-accept unique suggested item per product and competitor",
    )
    parser.add_argument(
        "--auto-accept-min-score",
        type=float,
        default=0.80,
        help="Min score for unique suggested auto-accept",
    )
    args = parser.parse_args()

    settings = get_settings()
    engine = create_engine(settings.database_url)
    embeddings_dir = Path(args.embeddings_dir or settings.embeddings_dir)
    first_seen_after = (
        datetime.strptime(args.first_seen_after, "%Y-%m-%d").date()
        if args.first_seen_after
        else None
    )
    last_seen_after = (
        datetime.strptime(args.last_seen_after, "%Y-%m-%d").date() if args.last_seen_after else None
    )

    include_status = args.include_status or []
    if args.only_open:
        include_status = list({*include_status, "suggested", "needs_review", "ambiguous"})
    include_status_value = include_status or None

    stats = {}
    with Session(engine) as session:
        stats = match_items(
            session,
            embeddings_dir=embeddings_dir,
            min_embed_score=args.min_embed_score or settings.matching_min_embed_score,
            min_gap=args.min_gap or settings.matching_min_gap,
            top_k=args.top_k or settings.matching_top_k,
            top_k_llm=args.top_k_llm or settings.matching_top_k_llm,
            use_llm_arbiter=args.use_llm_arbiter,
            limit=args.limit,
            only_null=(args.only_null or not args.force) and not args.only_open,
            include_status=include_status_value,
            force=args.force,
            dry_run=args.dry_run,
            sample_limit=args.sample_limit,
            samples_file=args.samples_file,
            report_file=args.report_file,
            report_limit=args.report_limit,
            report_csv_file=args.report_csv,
            sources=args.source,
            first_seen_after=first_seen_after,
            last_seen_after=last_seen_after,
            auto_accept_unique=args.auto_accept_unique,
            auto_accept_min_score=args.auto_accept_min_score,
        )
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    logging.info("match_competitor_items_embeddings done: %s", stats)


if __name__ == "__main__":
    main()
