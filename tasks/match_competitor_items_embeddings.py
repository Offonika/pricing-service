"""Match competitor items to products using embeddings + guardrails."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

import numpy as np
from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session, joinedload

from app.core.config import get_settings
from app.infrastructure.db.engines import build_engine
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
from app.services.llm_fallback import FallbackChatClient
from app.services.matching_guardrails import (
    basic_candidate_guardrails,
    catalog_family,
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


def _json_report_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    raise TypeError(f"Object of type {value.__class__.__name__} is not JSON serializable")


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
    "герметик",
    "b-7000",
    "b7000",
    "zhanlida",
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
    "itel": "itel",
    "infinix": "infinix",
    "tecno": "tecno",
    "meizu": "meizu",
    "sony": "sony",
    "nokia": "nokia",
    "zte": "zte",
    "nubia": "zte",
    "lenovo": "lenovo",
    "xiaoxin": "lenovo",
    "motorola": "motorola",
    "moto": "motorola",
    "asus": "asus",
    "rog": "asus",
    "google": "google",
    "pixel": "google",
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
    "coral": {"coral", "коралл", "коралловый", "коралловая"},
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
    "beige": {"beige", "бежевый", "бежевая"},
    "graphite": {"graphite", "графит", "графитовый", "графитовая"},
    "brown": {"brown", "коричневый", "коричневая"},
    "burgundy": {"burgundy", "maroon", "бордовый", "бордовая"},
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
COLOR_SENSITIVE_ITEM_TYPES = {"housing", "flex"}

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
    if _is_battery_adhesive(normalized):
        return "other"
    if _is_non_phone_battery_accessory(normalized):
        return "other"
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


def _is_battery_adhesive(text: str | None) -> bool:
    normalized = (text or "").lower().replace("ё", "е")
    if not normalized:
        return False
    return bool(
        re.search(r"\bскотч\w*\s+(?:акб|аккумулятор\w*)\b", normalized)
        or re.search(r"\b(?:акб|аккумулятор\w*)\s+скотч\w*\b", normalized)
        or re.search(r"\bbattery\s+(?:adhesive|sticker|tape)\b", normalized)
        or re.search(r"\b(?:adhesive|sticker|tape)\s+(?:for\s+)?battery\b", normalized)
    )


def _is_non_phone_battery_accessory(text: str | None) -> bool:
    normalized = (text or "").lower().replace("ё", "е")
    if not normalized:
        return False
    return bool(
        re.search(r"аккумулятор\w*\s+для\s+электроинструмент\w*", normalized)
        or re.search(
            r"(?:сетев\w*\s+)?зарядн\w*\s+устройств\w*\s+для\s+аккумулятор\w*",
            normalized,
        )
        or (
            re.search(r"\b(?:makita|hitachi|greenworks|bosch)\b", normalized)
            and re.search(r"\b(?:12v|14,?4|18v|21v|24v|ni-cd|li-ion)\b", normalized)
        )
    )


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
        "зарядная станция",
        "зарядное",
        "зарядка",
        "азу",
    )
    return any(marker in lower for marker in feature_markers)


def _effective_item_type(item: CompetitorItem) -> str | None:
    item_type = item.item_type if item.item_type in ITEM_TYPES else None
    text = " ".join(
        value
        for value in (item.name, item.normalized_title, item.category, item.category_group)
        if value
    )
    if _is_battery_adhesive(text):
        return "other"
    if item_type == "battery" and _is_non_phone_battery_accessory(text):
        return "other"
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
DISPLAY_MATRIX_VENDOR_TAG_PATTERNS: dict[str, re.Pattern[str]] = {
    "ALG": re.compile(r"\balg\b", re.IGNORECASE),
    "DD": re.compile(r"\bdd\b", re.IGNORECASE),
    "F5ENERGY": re.compile(r"\bf5\s*energy\b|\bf5energy\b", re.IGNORECASE),
    "FOG": re.compile(r"\bfog\b", re.IGNORECASE),
    "GX": re.compile(r"\bgx\b", re.IGNORECASE),
    "JCID": re.compile(r"\bjcid\b", re.IGNORECASE),
    "JK": re.compile(r"\bjk\b", re.IGNORECASE),
    "MNK": re.compile(r"\bmnk\b", re.IGNORECASE),
    "RJ": re.compile(r"\brj\b", re.IGNORECASE),
    "SL": re.compile(r"\bsl\b", re.IGNORECASE),
    "ZY": re.compile(r"\bzy\b", re.IGNORECASE),
}


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
    colors = _extract_color_values(value)
    for canonical in COLOR_ALIASES:
        if canonical in colors:
            return canonical
    return None


def _extract_color_values(value: str | None) -> set[str]:
    if not value:
        return set()
    tokens = set(re.findall(r"[a-zа-яё]+", value.lower().replace("ё", "е")))
    colors: set[str] = set()
    for canonical, aliases in COLOR_ALIASES.items():
        normalized_aliases = {alias.replace("ё", "е") for alias in aliases}
        if tokens & normalized_aliases:
            colors.add(canonical)
    return colors


def _first_color_values(*values: str | None) -> set[str]:
    for value in values:
        colors = _extract_color_values(value)
        if colors:
            return colors
    return set()


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


def _competitor_part_colors(item: CompetitorItem) -> set[str]:
    return _first_color_values(
        item.name,
        item.normalized_title,
        item.color,
        item.attrs_color,
    )


def _product_part_colors(product: Product) -> set[str]:
    return _first_color_values(product.name, product.color)


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


def _battery_verification_signal(text: str | None) -> bool:
    normalized = (text or "").lower().replace("ё", "е")
    return bool(
        "system diagnosable" in normalized
        or "system daignosable" in normalized
        or "верификац" in normalized
        or "новая запчаст" in normalized
    )


def _safe_battery_verification_suggest(
    item: CompetitorItem,
    product: Product,
    *,
    score: float,
) -> bool:
    if score < 0.70:
        return False
    item_text = " ".join(filter(None, [item.name, item.normalized_title]))
    product_text = product.name or ""
    if not _battery_verification_signal(item_text):
        return False
    normalized_product_text = product_text.lower()
    if (
        "system diagnosable" not in normalized_product_text
        and "system daignosable" not in normalized_product_text
    ):
        return False
    if _capacity_conflict(item_text, product_text, item.attrs_json):
        return False
    competitor_keys = _extract_device_model_keys(_competitor_device_model_text(item))
    product_keys = _extract_device_model_keys(product.name)
    return _device_model_keys_overlap(competitor_keys, product_keys)


def _safe_iphone_battery_model_capacity_suggest(
    item: CompetitorItem,
    product: Product,
    *,
    score: float,
) -> bool:
    if score < 0.68:
        return False
    item_text = " ".join(filter(None, [item.name, item.normalized_title, item.external_id]))
    product_text = product.name or ""
    normalized_item = item_text.lower().replace("ё", "е")
    normalized_product = product_text.lower().replace("ё", "е")
    if "аккумулятор" not in normalized_item or "iphone" not in normalized_item:
        return False
    if "аккумулятор" not in normalized_product or "iphone" not in normalized_product:
        return False
    if not (
        "battery collection" in normalized_item
        or "верификац" in normalized_item
        or "новая запчаст" in normalized_item
    ):
        return False
    competitor_keys = _extract_device_model_keys(_competitor_device_model_text(item))
    product_keys = _extract_device_model_keys(product.name)
    if not _device_model_keys_overlap(competitor_keys, product_keys):
        return False
    return not _capacity_conflict(item_text, product_text, item.attrs_json)


def _product_iphone_enhanced_battery_signal(text: str | None) -> bool:
    normalized = (text or "").lower().replace("ё", "е")
    return bool(
        _battery_verification_signal(normalized)
        or "f5energy" in normalized
        or "f5 energy" in normalized
        or "musttby" in normalized
        or "special edition" in normalized
    )


def _safe_iphone_battery_capacity_auto_accept(
    item: CompetitorItem,
    product: Product,
    *,
    score: float,
) -> bool:
    if not _safe_iphone_battery_model_capacity_suggest(item, product, score=score):
        return False
    return _product_iphone_enhanced_battery_signal(product.name)


def _battery_part_codes_from_text(text: str | None) -> set[str]:
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


def _competitor_battery_part_codes(item: CompetitorItem) -> set[str]:
    return set().union(
        *(
            _battery_part_codes_from_text(value)
            for value in (item.name, item.normalized_title, item.external_id)
            if value
        )
    )


def _product_battery_part_codes(product: Product) -> set[str]:
    return _battery_part_codes_from_text(product.name)


def _battery_part_code_conflict(product: Product, competitor_codes: set[str]) -> bool:
    product_codes = _product_battery_part_codes(product)
    return bool(competitor_codes and product_codes and competitor_codes.isdisjoint(product_codes))


def _text_has_battery_part_signal(text: str | None) -> bool:
    normalized = (text or "").lower().replace("ё", "е")
    return bool(re.search(r"\b(?:акб|battery)\b|аккумулятор", normalized))


def _product_has_battery_part_signal(product: Product) -> bool:
    return _text_has_battery_part_signal(
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


def _battery_subject_conflict_reason(item: CompetitorItem, product: Product) -> str | None:
    if _effective_item_type(item) != "battery":
        return None
    if not _text_has_battery_part_signal(_combined_item_text(item)):
        return None
    if _product_has_battery_part_signal(product):
        return None
    return "battery_vs_non_battery_product"


def _safe_battery_part_code_model_suggest(
    item: CompetitorItem,
    product: Product,
    *,
    filtered_count: int,
    score: float,
) -> bool:
    if score < 0.68:
        return False
    competitor_codes = _competitor_battery_part_codes(item)
    product_codes = _product_battery_part_codes(product)
    if not competitor_codes:
        return False
    if product_codes and competitor_codes.isdisjoint(product_codes):
        return False
    if not product_codes and (filtered_count != 1 or score < 0.80):
        return False
    competitor_keys = _extract_device_model_keys(_competitor_device_model_text(item))
    product_keys = _extract_device_model_keys(product.name)
    has_code_overlap = bool(product_codes and competitor_codes & product_codes)
    has_model_overlap = _device_model_keys_overlap(competitor_keys, product_keys)
    if not has_model_overlap and not (has_code_overlap and score >= 0.80):
        return False
    if _capacity_conflict(
        " ".join(filter(None, [item.name, item.normalized_title])),
        product.name or "",
        item.attrs_json,
    ):
        return False
    return True


def _battery_original_100_signal(text: str | None) -> bool:
    return bool(re.search(r"\b(?:or100|orig100)\b", (text or "").lower()))


def _safe_battery_original_part_code_auto_accept(
    item: CompetitorItem,
    product: Product,
    *,
    score: float,
    min_score: float,
) -> bool:
    if score < min_score:
        return False
    if not item.competitor or item.competitor.casefold() != "moba":
        return False
    competitor_codes = _competitor_battery_part_codes(item)
    product_codes = _product_battery_part_codes(product)
    if not competitor_codes or not product_codes or competitor_codes.isdisjoint(product_codes):
        return False
    item_text = " ".join(filter(None, [item.name, item.normalized_title, item.external_id]))
    product_text = " ".join(
        str(value) for value in (product.name, product.quality, product.quality_raw) if value
    )
    if not (_battery_original_100_signal(item_text) and _battery_original_100_signal(product_text)):
        return False
    return _safe_battery_part_code_model_suggest(
        item,
        product,
        filtered_count=1,
        score=score,
    )


def _safe_battery_part_code_auto_accept(
    item: CompetitorItem,
    product: Product,
    *,
    score: float,
    min_score: float,
) -> bool:
    code_min_score = min(min_score, 0.75)
    if score < code_min_score:
        return False
    competitor_codes = _competitor_battery_part_codes(item)
    product_codes = _product_battery_part_codes(product)
    if not competitor_codes:
        return False
    if product_codes and competitor_codes.isdisjoint(product_codes):
        return False
    item_text = " ".join(filter(None, [item.name, item.normalized_title, item.external_id]))
    product_text = product.name or ""
    if _battery_original_100_signal(item_text) or _battery_original_100_signal(product_text):
        return False
    if re.search(r"\bfilling\s+capacity\b", item_text.lower()):
        return False
    return _safe_battery_part_code_model_suggest(
        item,
        product,
        filtered_count=1,
        score=score,
    )


DISPOSABLE_BATTERY_BRANDS = {
    "duracell",
    "energizer",
    "gp",
    "gopower",
    "hoco",
    "kodak",
    "panasonic",
    "renata",
    "varta",
}


def _disposable_battery_brand(text: str | None) -> str | None:
    normalized = (text or "").lower()
    for token in re.findall(r"[a-z0-9]+", normalized):
        if token in DISPOSABLE_BATTERY_BRANDS:
            return token
    return None


def _disposable_battery_size(text: str | None) -> str | None:
    normalized = (text or "").lower()
    for pattern, size in (
        (r"\b(?:lr03|aaa)\b", "aaa"),
        (r"\b(?:lr6|lr06|aa)\b", "aa"),
        (r"\b(?:6f22|6lr61|крона|9v1?)\b", "9v"),
        (r"\b(?:lr20|d)\b", "d"),
        (r"\b27a\b", "27a"),
        (r"\b(cr20\d{2}|cr16\d{2}|cr12\d{2}|ag13|lr44h?|357a)\b", None),
    ):
        match = re.search(pattern, normalized)
        if match:
            return size or match.group(1)
    return None


def _disposable_battery_pack_count(text: str | None) -> int | None:
    normalized = (text or "").lower()
    match = re.search(r"\b(\d{1,2})\s*(?:шт|pcs|pieces|pack|упак)", normalized)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _safe_disposable_battery_suggest(
    item: CompetitorItem,
    product: Product,
    *,
    score: float,
) -> bool:
    if score < 0.75:
        return False
    item_text = " ".join(filter(None, [item.name, item.normalized_title, item.external_id]))
    product_text = product.name or ""
    item_family = catalog_family(item_text)
    product_family = catalog_family(product_text)
    if item_family not in {
        "battery_aa",
        "battery_aaa",
        "battery_9v",
        "battery_d",
        "battery_27a",
        "battery_coin",
    }:
        return False
    if product_family != item_family:
        return False
    item_brand = _disposable_battery_brand(item_text)
    product_brand = _disposable_battery_brand(product_text)
    if not item_brand or item_brand != product_brand:
        return False
    item_size = _disposable_battery_size(item_text)
    product_size = _disposable_battery_size(product_text)
    if not item_size or item_size != product_size:
        return False
    item_count = _disposable_battery_pack_count(item_text)
    product_count = _disposable_battery_pack_count(product_text)
    return bool(item_count and item_count == product_count)


STENCIL_GENERIC_TOKENS = {
    "bga",
    "xzz",
    "mijing",
    "relife",
    "rl",
    "трафарет",
    "трафареты",
    "для",
    "серии",
    "series",
    "pro",
    "plus",
    "max",
}
STENCIL_CHIPSET_TOKENS = {
    "snapdragon",
    "sdm",
    "msm",
    "exynos",
    "kirin",
    "hisilicon",
    "hi36c0",
    "dimensity",
    "mediatek",
    "emcp",
    "emmc",
    "cpu",
}


def _stencil_signature_tokens(text: str | None) -> set[str]:
    normalized = (text or "").lower().replace("ё", "е")
    normalized = normalized.replace("hi-silicon", "hisilicon")
    return {
        token
        for token in re.findall(r"[a-zа-я0-9]+", normalized)
        if token not in STENCIL_GENERIC_TOKENS and len(token) > 1
    }


def _safe_stencil_suggest(
    item: CompetitorItem,
    product: Product,
    *,
    score: float,
) -> bool:
    if score < 0.68:
        return False
    item_text = " ".join(filter(None, [item.name, item.normalized_title, item.external_id]))
    product_text = product.name or ""
    if catalog_family(item_text) != "stencil" or catalog_family(product_text) != "stencil":
        return False

    item_tokens = _stencil_signature_tokens(item_text)
    product_tokens = _stencil_signature_tokens(product_text)
    overlap = item_tokens & product_tokens
    if len(overlap) < 2:
        return False

    chipset_overlap = overlap & STENCIL_CHIPSET_TOKENS
    numeric_overlap = {token for token in overlap if token.isdigit() or re.search(r"\d", token)}
    if chipset_overlap and numeric_overlap:
        return score >= 0.80

    if {"iphone", "macbook"} & overlap and numeric_overlap:
        return True

    if "универсальный" in overlap or "universal" in overlap:
        return score >= 0.88

    return False


def _camera_glass_frame_state(text: str | None) -> str | None:
    normalized = (text or "").lower().replace("ё", "е")
    if re.search(r"\b(без\s+рамк\w*|without\s+frame)\b", normalized):
        return "without_frame"
    if re.search(r"\b(в\s+рамк\w*|с\s+рамк\w*|with\s+frame)\b", normalized):
        return "with_frame"
    return None


def _explicit_piece_pack_count(text: str | None) -> int | None:
    normalized = (text or "").lower().replace("ё", "е")
    match = re.search(r"\b(?:комплект\s+)?(\d{1,2})\s*(?:шт|pcs|pieces)\b", normalized)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _safe_phone_camera_glass_suggest(
    item: CompetitorItem,
    product: Product,
    *,
    score: float,
) -> bool:
    if score < 0.83:
        return False
    item_text = " ".join(filter(None, [item.name, item.normalized_title, item.external_id]))
    product_text = product.name or ""
    if catalog_family(item_text) != "phone_camera_glass":
        return False
    if catalog_family(product_text) != "phone_camera_glass":
        return False
    item_keys = _extract_device_model_keys(item_text)
    product_keys = _extract_device_model_keys(product_text)
    item_codes = _extract_device_codes(item_text)
    product_codes = _extract_device_codes(product_text)
    has_model_overlap = _device_model_keys_overlap(item_keys, product_keys)
    has_code_overlap = bool(item_codes and product_codes and item_codes & product_codes)
    if not (has_model_overlap or has_code_overlap):
        return False
    item_colors = _first_color_values(item.name, item.normalized_title)
    product_colors = _first_color_values(product.name, product.color)
    if item_colors and product_colors and item_colors.isdisjoint(product_colors):
        return False
    item_frame = _camera_glass_frame_state(item_text)
    product_frame = _camera_glass_frame_state(product_text)
    if item_frame and product_frame and item_frame != product_frame:
        return False
    item_count = _explicit_piece_pack_count(item_text)
    product_count = _explicit_piece_pack_count(product_text)
    if item_count and item_count > 1 and item_count != product_count:
        return False
    if product_count and product_count > 1 and product_count != item_count:
        return False
    return True


def _safe_screen_protector_suggest(
    item: CompetitorItem,
    product: Product,
    *,
    score: float,
) -> bool:
    if score < 0.80:
        return False
    item_text = " ".join(filter(None, [item.name, item.normalized_title, item.external_id]))
    product_text = product.name or ""
    if catalog_family(item_text) != "screen_protector":
        return False
    if catalog_family(product_text) != "screen_protector":
        return False
    item_keys = _extract_device_model_keys(item_text)
    product_keys = _extract_device_model_keys(product_text)
    item_codes = _extract_device_codes(item_text)
    product_codes = _extract_device_codes(product_text)
    has_model_overlap = _device_model_keys_overlap(item_keys, product_keys)
    has_code_overlap = bool(item_codes and product_codes and item_codes & product_codes)
    if not (has_model_overlap or has_code_overlap):
        return False
    item_colors = _first_color_values(item.name, item.normalized_title)
    product_colors = _first_color_values(product.name, product.color)
    return not (item_colors and product_colors and item_colors.isdisjoint(product_colors))


def _safe_phone_sim_tray_suggest(
    item: CompetitorItem,
    product: Product,
    *,
    score: float,
) -> bool:
    if score < 0.70:
        return False
    item_text = " ".join(filter(None, [item.name, item.normalized_title, item.external_id]))
    product_text = product.name or ""
    if catalog_family(item_text) != "phone_sim_tray":
        return False
    if catalog_family(product_text) != "phone_sim_tray":
        return False
    item_codes = _extract_device_codes(item_text)
    product_codes = _extract_device_codes(product_text)
    if item_codes and product_codes and item_codes.isdisjoint(product_codes):
        return False
    item_keys = _extract_device_model_keys(item_text)
    product_keys = _extract_device_model_keys(product_text)
    has_code_overlap = bool(item_codes and product_codes and item_codes & product_codes)
    has_model_overlap = _device_model_keys_overlap(item_keys, product_keys)
    if not (has_code_overlap or has_model_overlap):
        return False
    item_colors = _first_color_values(item.name, item.normalized_title)
    product_colors = _first_color_values(product.name, product.color)
    return bool(
        item_colors
        and product_colors
        and not _strict_color_sets_conflict(item_colors, product_colors)
    )


def _safe_phone_speaker_suggest(
    item: CompetitorItem,
    product: Product,
    *,
    score: float,
) -> bool:
    if score < 0.60:
        return False
    item_text = " ".join(filter(None, [item.name, item.normalized_title, item.external_id]))
    product_text = product.name or ""
    if catalog_family(item_text) != "phone_speaker":
        return False
    if catalog_family(product_text) != "phone_speaker":
        return False

    item_codes = _extract_device_codes(item_text)
    product_codes = _extract_device_codes(product_text)
    has_code_overlap = bool(item_codes and product_codes and item_codes & product_codes)
    item_keys = _extract_device_model_keys(item_text)
    product_keys = _extract_device_model_keys(product_text)
    has_model_overlap = _device_model_keys_overlap(item_keys, product_keys)
    if (
        item_codes
        and product_codes
        and item_codes.isdisjoint(product_codes)
        and not has_model_overlap
    ):
        return False
    if has_code_overlap:
        return True

    return bool(score >= 0.75 and has_model_overlap)


def _text_has_5g_marker(text: str | None) -> bool:
    return bool(re.search(r"\b5g\b", (text or "").lower()))


def _safe_touchscreen_suggest(
    item: CompetitorItem,
    product: Product,
    *,
    score: float,
) -> bool:
    if score < 0.72:
        return False
    item_text = " ".join(filter(None, [item.name, item.normalized_title, item.external_id]))
    product_text = product.name or ""
    normalized_item = item_text.lower().replace("ё", "е")
    normalized_product = product_text.lower().replace("ё", "е")
    if not re.search(r"\b(?:тачскрин|touchscreen)\b", normalized_item):
        return False
    if not re.search(r"\b(?:тачскрин|touchscreen)\b", normalized_product):
        return False
    if re.search(r"\b(?:диспле[йя]|display)\b", normalized_product):
        return False

    item_keys = _extract_device_model_keys(item_text)
    product_keys = _extract_device_model_keys(product_text)
    if not _device_model_keys_overlap(item_keys, product_keys):
        return False

    item_colors = _first_color_values(item.name, item.normalized_title)
    product_colors = _first_color_values(product.name, product.color)
    return bool(
        item_colors
        and product_colors
        and not _strict_color_sets_conflict(item_colors, product_colors)
    )


def _module_glass_oca_signal(text: str | None) -> bool:
    normalized = (text or "").lower().replace("ё", "е")
    return "oca" in normalized and bool(
        re.search(r"стекл\w*\s+для\s+переклейк\w*", normalized)
        or re.search(r"\bg\s*\+\s*oca\b", normalized)
        or re.search(r"стекл\w*\s+модул\w*", normalized)
        or re.search(r"\bтачскрин\b.*\+\s*oca", normalized)
    )


def _safe_module_glass_oca_suggest(
    item: CompetitorItem,
    product: Product,
    *,
    score: float,
) -> bool:
    if score < 0.72:
        return False
    item_text = " ".join(filter(None, [item.name, item.normalized_title, item.external_id]))
    product_text = product.name or ""
    if not _module_glass_oca_signal(item_text) or not _module_glass_oca_signal(product_text):
        return False

    item_colors = _first_color_values(item.name, item.normalized_title)
    product_colors = _first_color_values(product.name, product.color)
    if (
        not item_colors
        or not product_colors
        or _strict_color_sets_conflict(item_colors, product_colors)
    ):
        return False

    item_codes = _extract_device_codes(item_text)
    product_codes = _extract_device_codes(product_text)
    has_code_overlap = bool(item_codes and product_codes and item_codes & product_codes)
    if item_codes and product_codes and item_codes.isdisjoint(product_codes) and score < 0.82:
        return False

    if _text_has_5g_marker(item_text) != _text_has_5g_marker(product_text) and not has_code_overlap:
        return False

    item_keys = _extract_device_model_keys(item_text)
    product_keys = _extract_device_model_keys(product_text)
    return has_code_overlap or _device_model_keys_overlap(item_keys, product_keys)


def _network_cable_model(text: str | None) -> str | None:
    normalized = (text or "").lower().replace("ё", "е")
    match = re.search(r"\b([a-z]{2,5}\d{1,4})\b", normalized)
    return match.group(1) if match else None


def _cable_length_meters(text: str | None) -> float | None:
    normalized = (text or "").lower().replace("ё", "е").replace(",", ".")
    match = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:м|m)\b", normalized)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _safe_network_cable_suggest(
    item: CompetitorItem,
    product: Product,
    *,
    score: float,
) -> bool:
    if score < 0.72:
        return False
    item_text = " ".join(filter(None, [item.name, item.normalized_title, item.external_id]))
    product_text = product.name or ""
    if catalog_family(item_text) != "network_cable":
        return False
    if catalog_family(product_text) != "network_cable":
        return False
    item_model = _network_cable_model(item_text)
    product_model = _network_cable_model(product_text)
    if not item_model or item_model != product_model:
        return False
    item_length = _cable_length_meters(item_text)
    product_length = _cable_length_meters(product_text)
    return bool(item_length and product_length and item_length == product_length)


def _charging_station_model(text: str | None) -> str | None:
    normalized = (text or "").lower().replace("ё", "е")
    normalized = re.sub(r"[^a-zа-я0-9]+", " ", normalized)
    match = re.search(r"\b(icharge\s*\d+[a-z]?)\b", normalized)
    if match:
        return re.sub(r"\s+", "", match.group(1))
    match = re.search(r"\b(wlx\s*\d+\+?)\b", normalized)
    if match:
        return re.sub(r"\s+", "", match.group(1))
    return None


def _safe_charging_station_suggest(
    item: CompetitorItem,
    product: Product,
    *,
    score: float,
) -> bool:
    if score < 0.80:
        return False
    item_text = " ".join(filter(None, [item.name, item.normalized_title, item.external_id]))
    product_text = product.name or ""
    if "зарядная станция" not in item_text.lower().replace("ё", "е"):
        return False
    if "зарядная станция" not in product_text.lower().replace("ё", "е"):
        return False
    item_model = _charging_station_model(item_text)
    product_model = _charging_station_model(product_text)
    return bool(item_model and item_model == product_model)


def _safe_middle_frame_suggest(
    item: CompetitorItem,
    product: Product,
    *,
    score: float,
) -> bool:
    if score < 0.78:
        return False
    item_text = " ".join(filter(None, [item.name, item.normalized_title, item.external_id]))
    product_text = product.name or ""
    if catalog_family(item_text) != "middle_frame":
        return False
    if catalog_family(product_text) != "middle_frame":
        return False

    item_codes = _extract_device_codes(item_text)
    product_codes = _extract_device_codes(product_text)
    if item_codes and product_codes and item_codes.isdisjoint(product_codes):
        return False

    item_keys = _extract_device_model_keys(item_text)
    product_keys = _extract_device_model_keys(product_text)
    has_code_overlap = bool(item_codes and product_codes and item_codes & product_codes)
    has_model_overlap = _device_model_keys_overlap(item_keys, product_keys)
    if not (has_code_overlap or has_model_overlap):
        return False

    item_colors = _first_color_values(item.name, item.normalized_title)
    product_colors = _first_color_values(product.name, product.color)
    return bool(item_colors and product_colors and not item_colors.isdisjoint(product_colors))


def _magsafe_power_watts(text: str | None) -> int | None:
    normalized = (text or "").lower().replace("ё", "е")
    match = re.search(r"\b(\d{2,3})\s*(?:w|вт)\b", normalized)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _magsafe_generation(text: str | None) -> int | None:
    normalized = (text or "").lower().replace("ё", "е")
    match = re.search(r"\bmagsafe\s*(\d)\b", normalized)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _is_magsafe_power_adapter(text: str | None) -> bool:
    normalized = (text or "").lower().replace("ё", "е")
    return bool(
        re.search(r"\b(?:блок|адаптер)\s+питани", normalized)
        or re.search(r"\bpower\s+adapter\b", normalized)
    )


def _safe_magsafe_power_adapter_suggest(
    item: CompetitorItem,
    product: Product,
    *,
    score: float,
) -> bool:
    if score < 0.72:
        return False
    item_text = " ".join(filter(None, [item.name, item.normalized_title, item.external_id]))
    product_text = product.name or ""
    if catalog_family(item_text) != "magsafe":
        return False
    if catalog_family(product_text) != "magsafe":
        return False
    if not _is_magsafe_power_adapter(item_text) or not _is_magsafe_power_adapter(product_text):
        return False

    item_watts = _magsafe_power_watts(item_text)
    product_watts = _magsafe_power_watts(product_text)
    if not item_watts or item_watts != product_watts:
        return False

    item_generation = _magsafe_generation(item_text)
    product_generation = _magsafe_generation(product_text)
    return not (item_generation and product_generation and item_generation != product_generation)


def _adhesive_model(text: str | None) -> str | None:
    normalized = (text or "").lower().replace("ё", "е")
    match = re.search(r"\b([a-z]?)[-\s]?(\d{4})\b", normalized)
    if not match:
        return None
    prefix = match.group(1)
    digits = match.group(2)
    return f"{prefix}{digits}" if prefix else digits


def _volume_ml(text: str | None) -> float | None:
    normalized = (text or "").lower().replace("ё", "е").replace(",", ".")
    match = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:мл|ml)\b", normalized)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _safe_adhesive_suggest(
    item: CompetitorItem,
    product: Product,
    *,
    score: float,
) -> bool:
    if score < 0.70:
        return False
    item_text = " ".join(filter(None, [item.name, item.normalized_title, item.external_id]))
    product_text = product.name or ""
    if catalog_family(item_text) != "adhesive":
        return False
    if catalog_family(product_text) != "adhesive":
        return False

    item_model = _adhesive_model(item_text)
    product_model = _adhesive_model(product_text)
    if not item_model or item_model != product_model:
        return False

    item_volume = _volume_ml(item_text)
    product_volume = _volume_ml(product_text)
    return bool(item_volume and product_volume and item_volume == product_volume)


def _safe_steam_deck_screen_protector_suggest(
    item: CompetitorItem,
    product: Product,
    *,
    score: float,
) -> bool:
    if score < 0.80:
        return False
    item_text = " ".join(filter(None, [item.name, item.normalized_title, item.external_id]))
    product_text = product.name or ""
    if catalog_family(item_text) != "screen_protector":
        return False
    if catalog_family(product_text) != "screen_protector":
        return False
    return "steam deck" in item_text.lower() and "steam deck" in product_text.lower()


def _phone_sim_tray_model_or_code_conflict(item: CompetitorItem, product: Product) -> bool:
    item_text = " ".join(filter(None, [item.name, item.normalized_title, item.external_id]))
    product_text = product.name or ""
    if catalog_family(item_text) != "phone_sim_tray":
        return False
    if catalog_family(product_text) != "phone_sim_tray":
        return False
    item_codes = _extract_device_codes(item_text)
    product_codes = _extract_device_codes(product_text)
    if item_codes and product_codes and item_codes.isdisjoint(product_codes):
        return True
    item_keys = _extract_device_model_keys(item_text)
    product_keys = _extract_device_model_keys(product_text)
    return bool(
        item_keys and product_keys and not _device_model_keys_overlap(item_keys, product_keys)
    )


def _strict_color_sets_conflict(left: set[str], right: set[str]) -> bool:
    if not left or not right or not left.isdisjoint(right):
        return False
    if left <= {"gray", "silver"} and right <= {"gray", "silver"}:
        return False
    return True


def _tool_set_signal(text: str | None) -> bool:
    normalized = (text or "").lower().replace("ё", "е")
    return bool(
        re.search(r"\bнабор\w*\s+отверт\w*", normalized)
        or re.search(r"\b(?:отвертк|отверточн)\w*\b", normalized)
        or re.search(r"\bscrewdriver\w*\b", normalized)
    )


def _laptop_keyboard_model_tokens(text: str | None) -> set[str]:
    normalized = (text or "").lower().replace("ё", "е")
    normalized = re.sub(r"[^a-zа-я0-9+/-]+", " ", normalized)
    tokens: set[str] = set()
    series_patterns = (
        r"\belitebook\s+\d{3,4}[a-z]?\b",
        r"\bpavilion\s+\d{2,3}[a-z]?(?:[-\s][a-z0-9]+)?\b",
        r"\blatitude\s+\d{3,4}\b",
        r"\binspiron\s+\d{3,4}\b",
        r"\bthinkpad\s+[a-z]\d{2,4}[a-z]?\b",
        r"\bideapad\s+[a-z0-9-]+\b",
        r"\bmacbook\s+(?:air|pro)\s+\d{2,4}\b",
    )
    for pattern in series_patterns:
        for match in re.finditer(pattern, normalized):
            tokens.add(re.sub(r"[^a-z0-9а-я]+", "_", match.group(0)).strip("_"))
    for match in re.finditer(r"\b\d{3,4}[a-z]\b", normalized):
        tokens.add(match.group(0))
    return tokens


def _tecno_phantom_model_tokens(text: str | None) -> set[str]:
    normalized = (text or "").lower().replace("ё", "е")
    normalized = re.sub(r"[^a-zа-я0-9]+", " ", normalized)
    tokens: set[str] = set()
    for match in re.finditer(
        r"\btecno\s+phantom\s+((?:v\s+)?(?:fold\s+)?\d|x\d{1,2})\b",
        normalized,
    ):
        tokens.add("tecno_phantom_" + re.sub(r"\s+", "_", match.group(1)))
    return tokens


def _other_family_conflict_details(item: CompetitorItem, product: Product) -> dict[str, Any] | None:
    item_text = " ".join(filter(None, [item.name, item.normalized_title, item.external_id]))
    product_text = product.name or ""
    item_family = catalog_family(item_text)
    product_family = catalog_family(product_text)
    common = {
        "family": item_family,
        "product_family": product_family,
    }

    if item_family == "phone_screws" and _tool_set_signal(product_text):
        return {
            "reason": "phone_screws_vs_tool_conflict",
            **common,
        }

    if item_family != product_family:
        return None

    if item_family == "middle_frame":
        item_colors = _first_color_values(item.name, item.normalized_title)
        product_colors = _first_color_values(product.name, product.color)
        if _strict_color_sets_conflict(item_colors, product_colors):
            return {
                "reason": "middle_frame_color_conflict",
                **common,
                "competitor_colors": sorted(item_colors),
                "product_colors": sorted(product_colors),
            }

    if item_family == "laptop_keyboard":
        item_tokens = _laptop_keyboard_model_tokens(item_text)
        product_tokens = _laptop_keyboard_model_tokens(product_text)
        if item_tokens and product_tokens and item_tokens.isdisjoint(product_tokens):
            return {
                "reason": "laptop_keyboard_model_conflict",
                **common,
                "competitor_model_tokens": sorted(item_tokens),
                "product_model_tokens": sorted(product_tokens),
            }

    if item_family == "phone_camera_glass":
        item_phantom_tokens = _tecno_phantom_model_tokens(item_text)
        product_phantom_tokens = _tecno_phantom_model_tokens(product_text)
        if (
            item_phantom_tokens
            and product_phantom_tokens
            and item_phantom_tokens.isdisjoint(product_phantom_tokens)
        ):
            return {
                "reason": "phone_camera_glass_model_conflict",
                **common,
                "competitor_model_keys": sorted(item_phantom_tokens),
                "product_model_keys": sorted(product_phantom_tokens),
            }
        item_keys = _extract_device_model_keys(item_text)
        product_keys = _extract_device_model_keys(product_text)
        has_model_overlap = _device_model_keys_overlap(item_keys, product_keys)
        item_codes = _extract_device_codes(item_text)
        product_codes = _extract_device_codes(product_text)
        if (
            not has_model_overlap
            and item_codes
            and product_codes
            and item_codes.isdisjoint(product_codes)
        ):
            return {
                "reason": "phone_camera_glass_device_code_conflict",
                **common,
                "competitor_codes": sorted(item_codes),
                "product_codes": sorted(product_codes),
            }
        if item_keys and product_keys and not has_model_overlap:
            return {
                "reason": "phone_camera_glass_model_conflict",
                **common,
                "competitor_model_keys": sorted(item_keys),
                "product_model_keys": sorted(product_keys),
            }
        item_colors = _first_color_values(item.name, item.normalized_title)
        product_colors = _first_color_values(product.name, product.color)
        if _strict_color_sets_conflict(item_colors, product_colors):
            return {
                "reason": "phone_camera_glass_color_conflict",
                **common,
                "competitor_colors": sorted(item_colors),
                "product_colors": sorted(product_colors),
            }
        item_frame = _camera_glass_frame_state(item_text)
        product_frame = _camera_glass_frame_state(product_text)
        if item_frame and product_frame and item_frame != product_frame:
            return {
                "reason": "phone_camera_glass_frame_conflict",
                **common,
                "competitor_frame": item_frame,
                "product_frame": product_frame,
            }
        item_count = _explicit_piece_pack_count(item_text)
        product_count = _explicit_piece_pack_count(product_text)
        if item_count and item_count > 1 and item_count != product_count:
            return {
                "reason": "phone_camera_glass_pack_count_conflict",
                **common,
                "competitor_pack_count": item_count,
                "product_pack_count": product_count,
            }
        if product_count and product_count > 1 and product_count != item_count:
            return {
                "reason": "phone_camera_glass_pack_count_conflict",
                **common,
                "competitor_pack_count": item_count,
                "product_pack_count": product_count,
            }

    if item_family == "phone_sim_tray":
        item_keys = _extract_device_model_keys(item_text)
        product_keys = _extract_device_model_keys(product_text)
        has_model_overlap = _device_model_keys_overlap(item_keys, product_keys)
        item_codes = _extract_device_codes(item_text)
        product_codes = _extract_device_codes(product_text)
        if (
            not has_model_overlap
            and item_codes
            and product_codes
            and item_codes.isdisjoint(product_codes)
        ):
            return {
                "reason": "phone_sim_tray_device_code_conflict",
                **common,
                "competitor_codes": sorted(item_codes),
                "product_codes": sorted(product_codes),
            }
        if item_keys and product_keys and not has_model_overlap:
            return {
                "reason": "phone_sim_tray_model_conflict",
                **common,
                "competitor_model_keys": sorted(item_keys),
                "product_model_keys": sorted(product_keys),
            }
        item_colors = _first_color_values(item.name, item.normalized_title)
        product_colors = _first_color_values(product.name, product.color)
        if _strict_color_sets_conflict(item_colors, product_colors):
            return {
                "reason": "phone_sim_tray_color_conflict",
                **common,
                "competitor_colors": sorted(item_colors),
                "product_colors": sorted(product_colors),
            }

    if _module_glass_oca_signal(item_text) and _module_glass_oca_signal(product_text):
        item_colors = _first_color_values(item.name, item.normalized_title)
        product_colors = _first_color_values(product.name, product.color)
        if _strict_color_sets_conflict(item_colors, product_colors):
            return {
                "reason": "module_glass_oca_color_conflict",
                **common,
                "competitor_colors": sorted(item_colors),
                "product_colors": sorted(product_colors),
            }
        item_codes = _extract_device_codes(item_text)
        product_codes = _extract_device_codes(product_text)
        has_code_overlap = bool(item_codes and product_codes and item_codes & product_codes)
        if (
            _text_has_5g_marker(item_text) != _text_has_5g_marker(product_text)
            and not has_code_overlap
        ):
            return {
                "reason": "module_glass_oca_network_generation_conflict",
                **common,
                "competitor_has_5g": _text_has_5g_marker(item_text),
                "product_has_5g": _text_has_5g_marker(product_text),
                "competitor_codes": sorted(item_codes),
                "product_codes": sorted(product_codes),
            }
        item_keys = _extract_device_model_keys(item_text)
        product_keys = _extract_device_model_keys(product_text)
        if item_keys and product_keys and not _device_model_keys_overlap(item_keys, product_keys):
            return {
                "reason": "module_glass_oca_model_conflict",
                **common,
                "competitor_model_keys": sorted(item_keys),
                "product_model_keys": sorted(product_keys),
            }

    if item_family == "stencil" and product_family == "stencil":
        item_tokens = _stencil_signature_tokens(item_text)
        product_tokens = _stencil_signature_tokens(product_text)
        item_chipsets = item_tokens & STENCIL_CHIPSET_TOKENS
        product_chipsets = product_tokens & STENCIL_CHIPSET_TOKENS
        item_numbers = {token for token in item_tokens if re.search(r"\d", token)}
        product_numbers = {token for token in product_tokens if re.search(r"\d", token)}
        if item_chipsets and product_chipsets and item_chipsets.isdisjoint(product_chipsets):
            return {
                "reason": "stencil_chipset_family_conflict",
                **common,
                "competitor_chipsets": sorted(item_chipsets),
                "product_chipsets": sorted(product_chipsets),
            }
        if (
            item_chipsets
            and product_chipsets
            and item_numbers
            and product_numbers
            and item_numbers.isdisjoint(product_numbers)
        ):
            return {
                "reason": "stencil_chipset_number_conflict",
                **common,
                "competitor_chipsets": sorted(item_chipsets),
                "product_chipsets": sorted(product_chipsets),
                "competitor_numbers": sorted(item_numbers),
                "product_numbers": sorted(product_numbers),
            }

    return None


def _safe_housing_part_suggest(
    item: CompetitorItem,
    product: Product,
    *,
    score: float,
    min_score: float = 0.80,
) -> bool:
    if score < min_score:
        return False
    competitor_kind = _competitor_housing_part_kind(item)
    product_kind = _product_housing_part_kind(product)
    if not competitor_kind or competitor_kind != product_kind:
        return False
    if _part_assembly_conflict_reason(item, product):
        return False
    if _housing_variant_conflict_reason(item, product):
        return False
    competitor_quality = _competitor_part_quality_tier(item)
    if _part_quality_conflict(product, competitor_quality):
        return False
    product_quality = _product_part_quality_tier(product)
    if product_quality == "original" and competitor_quality != "original":
        return False
    item_text = " ".join(filter(None, [item.name, item.normalized_title, item.external_id]))
    product_text = product.name or ""
    item_codes = _extract_device_codes(item_text)
    product_codes = _extract_device_codes(product_text)
    if item_codes and product_codes and item_codes.isdisjoint(product_codes):
        return False
    item_keys = _extract_device_model_keys(item_text)
    product_keys = _extract_device_model_keys(product_text)
    has_code_overlap = bool(item_codes and product_codes and item_codes & product_codes)
    has_model_overlap = _device_model_keys_overlap(item_keys, product_keys)
    if not (has_code_overlap or has_model_overlap):
        return False
    item_colors = _first_color_values(item.name, item.normalized_title)
    product_colors = _first_color_values(product.name, product.color)
    return bool(item_colors and product_colors and not item_colors.isdisjoint(product_colors))


def _safe_housing_part_auto_accept(
    item: CompetitorItem,
    product: Product,
    *,
    score: float,
    min_score: float,
) -> bool:
    kind = _competitor_housing_part_kind(item)
    role_min_score = 0.75 if kind == "back_cover" else min_score
    if kind == "housing":
        role_min_score = min(role_min_score, 0.79)
    if score < role_min_score:
        return False
    if not _safe_housing_part_suggest(item, product, score=score, min_score=role_min_score):
        return False
    if score < min_score:
        item_text = " ".join(filter(None, [item.name, item.normalized_title, item.external_id]))
        product_text = product.name or ""
        item_codes = _extract_device_codes(item_text)
        product_codes = _extract_device_codes(product_text)
        if item_codes and product_codes and not item_codes.isdisjoint(product_codes):
            return True
        item_keys = _extract_device_model_keys(item_text)
        product_keys = _extract_device_model_keys(product_text)
        if not item_keys or item_keys != product_keys:
            return False
    return True


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


def _part_color_conflict(product: Product, competitor_colors: set[str]) -> bool:
    product_colors = _product_part_colors(product)
    if competitor_colors and product_colors:
        return competitor_colors.isdisjoint(product_colors)
    return False


def _part_quality_tier_from_text(text: str | None) -> str | None:
    normalized = (text or "").lower().replace("ё", "е")
    if not normalized:
        return None
    if re.search(r"\b(premium)\b|\bпремиум\b", normalized):
        return "premium"
    if re.search(
        r"\b(orig|orig100|or100|original|genuine)\b|ориг|снятый|с\s+разбора",
        normalized,
    ):
        return "original"
    return None


def _competitor_part_quality_tier(item: CompetitorItem) -> str | None:
    return _part_quality_tier_from_text(" ".join(filter(None, [item.name, item.normalized_title])))


def _product_part_quality_tier(product: Product) -> str | None:
    return _part_quality_tier_from_text(
        " ".join(
            str(value)
            for value in (
                product.name,
                product.quality,
                product.quality_raw,
            )
            if value
        )
    )


def _part_quality_conflict(product: Product, competitor_quality: str | None) -> bool:
    product_quality = _product_part_quality_tier(product)
    return bool(
        competitor_quality
        and product_quality
        and {competitor_quality, product_quality} == {"premium", "original"}
    )


def _part_has_camera_glass(text: str | None) -> bool:
    normalized = (text or "").lower().replace("ё", "е")
    return bool(
        re.search(r"в\s+сборе\s+со\s+стекл\w*\s+камер\w*", normalized)
        or re.search(r"\bсо\s+стекл\w*\s+камер\w*", normalized)
        or re.search(r"\bcamera\s+glass\b", normalized)
    )


def _part_has_flex_assembly(text: str | None) -> bool:
    normalized = (text or "").lower().replace("ё", "е")
    return bool(
        re.search(r"в\s+сборе\s+со\s+шлейф\w*", normalized)
        or re.search(r"\bсо\s+шлейф\w*", normalized)
        or re.search(r"\bи\s+шлейф\w*", normalized)
        or re.search(r"\bшлейф\w*\s+magsafe\b", normalized)
    )


def _part_assembly_conflict_reason(item: CompetitorItem, product: Product) -> str | None:
    competitor_text = " ".join(filter(None, [item.name, item.normalized_title]))
    product_text = product.name or ""
    if _part_has_camera_glass(competitor_text) and not _part_has_camera_glass(product_text):
        return "part_camera_glass_missing_on_product"
    if _part_has_camera_glass(product_text) and not _part_has_camera_glass(competitor_text):
        return "part_camera_glass_extra_on_product"
    if _part_has_flex_assembly(competitor_text) and not _part_has_flex_assembly(product_text):
        return "part_flex_assembly_missing_on_product"
    if _part_has_flex_assembly(product_text) and not _part_has_flex_assembly(competitor_text):
        return "part_flex_assembly_extra_on_product"
    return None


def _housing_variant_flags(text: str | None) -> set[str]:
    normalized = (text or "").lower().replace("ё", "е")
    flags: set[str] = set()
    if re.search(r"широк\w*\s+отверст", normalized):
        flags.add("wide_hole")
    if re.search(r"узк\w*\s+отверст", normalized):
        flags.add("narrow_hole")
    return flags


def _housing_variant_conflict_reason(item: CompetitorItem, product: Product) -> str | None:
    competitor_text = " ".join(filter(None, [item.name, item.normalized_title]))
    product_text = product.name or ""
    competitor_flags = _housing_variant_flags(competitor_text)
    product_flags = _housing_variant_flags(product_text)
    if competitor_flags != product_flags and (competitor_flags or product_flags):
        return "housing_variant_conflict"
    return None


def _housing_device_code_conflict(item: CompetitorItem, product: Product) -> bool:
    competitor_codes = _extract_device_codes(_competitor_device_code_text(item))
    product_codes = _extract_device_codes(product.name)
    return bool(competitor_codes and product_codes and competitor_codes.isdisjoint(product_codes))


def _housing_part_kind_from_text(text: str | None) -> str | None:
    normalized = (text or "").lower().replace("ё", "е")
    if not normalized:
        return None
    if re.search(r"держател\w*\s+(?:sim|сим)|sim\s*tray", normalized):
        return "sim_tray"
    if re.search(r"рамк\w*\s+диспле\w*|display\s+frame", normalized):
        return "display_frame"
    if re.search(r"средн\w*\s+част\w*|middle\s+frame", normalized):
        return "middle_frame"
    if re.search(r"(?:задн\w*\s+)?крышк\w*|back\s+cover", normalized):
        return "back_cover"
    if re.search(r"\bкорпус\w*\b|\bhousing\b", normalized):
        return "housing"
    return None


def _competitor_housing_part_kind(item: CompetitorItem) -> str | None:
    return _housing_part_kind_from_text(
        " ".join(
            value
            for value in (item.name, item.normalized_title, item.category, item.category_group)
            if value
        )
    )


def _product_housing_part_kind(product: Product) -> str | None:
    return _housing_part_kind_from_text(
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


def _housing_part_kind_conflict(product: Product, competitor_kind: str | None) -> bool:
    product_kind = _product_housing_part_kind(product)
    return bool(competitor_kind and product_kind and competitor_kind != product_kind)


def _camera_position_from_text(text: str | None) -> str | None:
    normalized = (text or "").lower().replace("ё", "е")
    if not normalized:
        return None
    if re.search(r"\b(передн\w*|front|selfie)\b", normalized):
        return "front"
    if re.search(r"\b(задн\w*|основн\w*|rear|back|main)\b", normalized):
        return "rear"
    return None


def _competitor_camera_position(item: CompetitorItem) -> str | None:
    return _camera_position_from_text(" ".join(filter(None, [item.name, item.normalized_title])))


def _product_camera_position(product: Product) -> str | None:
    return _camera_position_from_text(product.name)


def _camera_position_conflict(product: Product, competitor_position: str | None) -> bool:
    product_position = _product_camera_position(product)
    return bool(
        competitor_position and product_position and competitor_position != product_position
    )


def _iphone_se_exact_model_overlap(item_text: str | None, product_text: str | None) -> bool:
    item_normalized = (item_text or "").lower().replace("ё", "е")
    product_normalized = (product_text or "").lower().replace("ё", "е")
    if not (
        re.search(r"\biphone\s+se\b", item_normalized)
        and re.search(r"\biphone\s+se\b", product_normalized)
    ):
        return False
    return not re.search(r"\b(?:2020|2022|2nd|3rd|2\s*gen|3\s*gen)\b", item_normalized)


def _safe_camera_suggest(
    item: CompetitorItem,
    product: Product,
    *,
    score: float,
) -> bool:
    if score < 0.80:
        return False
    item_text = " ".join(filter(None, [item.name, item.normalized_title, item.external_id]))
    product_text = product.name or ""
    normalized_item = item_text.lower().replace("ё", "е")
    normalized_product = product_text.lower().replace("ё", "е")
    if _effective_item_type(item) != "camera":
        return False
    if "камер" not in normalized_item and "camera" not in normalized_item:
        return False
    if "камер" not in normalized_product and "camera" not in normalized_product:
        return False
    if catalog_family(product_text) == "phone_camera_glass":
        return False

    competitor_position = _competitor_camera_position(item)
    product_position = _product_camera_position(product)
    if not competitor_position or competitor_position != product_position:
        return False

    item_keys = _extract_device_model_keys(item_text)
    product_keys = _extract_device_model_keys(product_text)
    if _device_model_keys_overlap(item_keys, product_keys):
        return True
    return _iphone_se_exact_model_overlap(item_text, product_text)


def _safe_connector_suggest(
    item: CompetitorItem,
    product: Product,
    *,
    score: float,
) -> bool:
    if score < 0.74:
        return False
    item_text = " ".join(filter(None, [item.name, item.normalized_title, item.external_id]))
    product_text = product.name or ""
    normalized_item = item_text.lower().replace("ё", "е")
    normalized_product = product_text.lower().replace("ё", "е")
    if _effective_item_type(item) != "connector":
        return False
    if not re.search(r"\b(?:разъем|разъём|connector|port)\b", normalized_item):
        return False
    if not re.search(r"\b(?:разъем|разъём|connector|port)\b", normalized_product):
        return False

    item_port = _extract_port_type(item_text)
    product_port = _extract_port_type(product_text)
    if not item_port or item_port != product_port:
        return False

    item_codes = _extract_device_codes(_competitor_device_code_text(item))
    product_codes = _extract_device_codes(product_text)
    if item_codes and product_codes and item_codes.isdisjoint(product_codes):
        return False

    item_keys = _extract_device_model_keys(item_text)
    product_keys = _extract_device_model_keys(product_text)
    has_code_overlap = bool(item_codes and product_codes and item_codes & product_codes)
    has_model_overlap = _device_model_keys_overlap(item_keys, product_keys)
    return has_code_overlap or has_model_overlap


def _flex_role_from_text(text: str | None) -> str | None:
    normalized = (text or "").lower().replace("ё", "е")
    if not normalized:
        return None
    if re.search(r"\b(button|buttons|volume|power\s+button)\b", normalized) or re.search(
        r"\b(кнопк\w*|кнопк\w*\s+включен\w*|громкост\w*|блокировк\w*)\b",
        normalized,
    ):
        return "buttons"
    if re.search(r"\b(fingerprint|touch\s+id)\b", normalized) or re.search(
        r"сканер\w*\s+отпечатк\w*|отпечатк\w*\s+пальц\w*",
        normalized,
    ):
        return "fingerprint"
    if re.search(
        r"для\s+тестирован\w*\s+работ\w*\s+диспле\w*|на\s+диспле\w*|"
        r"display\s+test|display\s+flex",
        normalized,
    ):
        return "display"
    if re.search(r"\b(межплатн\w*|interconnect|main\s+board\s+flex)\b", normalized):
        return "interboard"
    if re.search(
        r"системн\w*\s+разъ[еe]м\w*|разъ[еe]м\w*\s+зарядк\w*|зарядк\w*|"
        r"charging\s+(?:port|connector)|dock\s+connector|микрофон\w*",
        normalized,
    ):
        return "charge_mic"
    if re.search(r"\b(sensor|сенсор\w*)\b", normalized):
        return "sensor"
    return None


def _competitor_flex_role(item: CompetitorItem) -> str | None:
    return _flex_role_from_text(" ".join(filter(None, [item.name, item.normalized_title])))


def _product_flex_role(product: Product) -> str | None:
    return _flex_role_from_text(product.name)


def _flex_role_conflict(product: Product, competitor_role: str | None) -> bool:
    product_role = _product_flex_role(product)
    return bool(competitor_role and product_role and competitor_role != product_role)


def _flex_has_fingerprint(text: str | None) -> bool:
    normalized = (text or "").lower().replace("ё", "е")
    return bool(
        re.search(r"сканер\w*\s+отпечатк\w*|отпечатк\w*\s+пальц\w*|fingerprint", normalized)
    )


def _flex_fingerprint_conflict(item: CompetitorItem, product: Product) -> bool:
    item_text = " ".join(filter(None, [item.name, item.normalized_title, item.external_id]))
    product_text = product.name or ""
    return _flex_has_fingerprint(item_text) != _flex_has_fingerprint(product_text)


def _flex_button_controls(text: str | None) -> set[str]:
    normalized = (text or "").lower().replace("ё", "е")
    controls: set[str] = set()
    if re.search(r"\b(volume)\b|громкост", normalized):
        controls.add("volume")
    if re.search(r"power\s+button|кнопк\w*\s+включен\w*|включен\w*|блокировк", normalized):
        controls.add("power")
    return controls


def _flex_button_control_conflict(item: CompetitorItem, product: Product) -> bool:
    if _competitor_flex_role(item) != "buttons" or _product_flex_role(product) != "buttons":
        return False
    item_controls = _flex_button_controls(
        " ".join(filter(None, [item.name, item.normalized_title, item.external_id]))
    )
    product_controls = _flex_button_controls(product.name)
    return bool(item_controls and product_controls and item_controls != product_controls)


def _flex_button_text_has_extra_assembly(text: str | None) -> bool:
    normalized = (text or "").lower().replace("ё", "е")
    return bool(re.search(r"микрофон\w*|вспышк\w*|разъ[еe]м\w*|зарядк\w*", normalized))


def _flex_extra_components(text: str | None) -> set[str]:
    normalized = (text or "").lower().replace("ё", "е")
    components: set[str] = set()
    if re.search(r"микрофон\w*|microphone|mic\b", normalized):
        components.add("microphone")
    if re.search(r"вспышк\w*|flash", normalized):
        components.add("flash")
    if re.search(r"разъ[еe]м\w*|зарядк\w*|charging\s+(?:port|connector)", normalized):
        components.add("charging_connector")
    return components


def _flex_conflict_details(item: CompetitorItem, product: Product) -> dict[str, Any] | None:
    competitor_role = _competitor_flex_role(item)
    product_role = _product_flex_role(product)
    if competitor_role and product_role and competitor_role != product_role:
        return {
            "reason": "flex_role_conflict",
            "competitor_role": competitor_role,
            "product_role": product_role,
        }

    item_text = " ".join(filter(None, [item.name, item.normalized_title, item.external_id]))
    product_text = product.name or ""

    if competitor_role == "buttons" and product_role == "buttons":
        competitor_components = _flex_extra_components(item_text)
        product_components = _flex_extra_components(product_text)
        if competitor_components != product_components and (
            competitor_components or product_components
        ):
            return {
                "reason": "flex_extra_component_conflict",
                "competitor_role": competitor_role,
                "product_role": product_role,
                "competitor_components": sorted(competitor_components),
                "product_components": sorted(product_components),
            }

    if competitor_role and product_role and competitor_role == product_role:
        competitor_codes = _extract_device_codes(_competitor_device_code_text(item))
        product_codes = _extract_device_codes(product_text)
        if competitor_codes and product_codes and competitor_codes.isdisjoint(product_codes):
            return {
                "reason": "flex_device_code_conflict",
                "competitor_role": competitor_role,
                "product_role": product_role,
                "competitor_codes": sorted(competitor_codes),
                "product_codes": sorted(product_codes),
            }

    return None


def _safe_flex_suggest(
    item: CompetitorItem,
    product: Product,
    *,
    score: float,
    min_score: float = 0.80,
) -> bool:
    if score < min_score:
        return False
    competitor_role = _competitor_flex_role(item)
    product_role = _product_flex_role(product)
    if not competitor_role or competitor_role != product_role:
        return False
    item_text = " ".join(filter(None, [item.name, item.normalized_title, item.external_id]))
    product_text = product.name or ""
    if _flex_fingerprint_conflict(item, product):
        return False
    if _flex_button_control_conflict(item, product):
        return False
    item_codes = _extract_device_codes(_competitor_device_code_text(item))
    product_codes = _extract_device_codes(product_text)
    if item_codes and product_codes and item_codes.isdisjoint(product_codes):
        return False
    item_keys = _extract_device_model_keys(item_text)
    product_keys = _extract_device_model_keys(product_text)
    has_code_overlap = bool(item_codes and product_codes and item_codes & product_codes)
    has_model_overlap = _device_model_keys_overlap(item_keys, product_keys)
    if not (has_code_overlap or has_model_overlap):
        return False
    item_colors = _first_color_values(item.name, item.normalized_title)
    product_colors = _first_color_values(product.name, product.color)
    return not (item_colors and product_colors and item_colors.isdisjoint(product_colors))


def _safe_flex_auto_accept(
    item: CompetitorItem,
    product: Product,
    *,
    score: float,
    min_score: float,
) -> bool:
    role = _competitor_flex_role(item)
    role_min_score = min_score
    if role == "buttons":
        role_min_score = 0.74
    elif role == "charge_mic":
        role_min_score = 0.75
    if score < role_min_score:
        return False
    if not _safe_flex_suggest(item, product, score=score, min_score=role_min_score):
        return False
    if role == "buttons":
        item_text = " ".join(filter(None, [item.name, item.normalized_title, item.external_id]))
        product_text = product.name or ""
        if _flex_button_text_has_extra_assembly(item_text) or _flex_button_text_has_extra_assembly(
            product_text
        ):
            return False
        item_keys = _extract_device_model_keys(item_text)
        product_keys = _extract_device_model_keys(product_text)
        if not _device_model_keys_overlap(item_keys, product_keys):
            return False
    if role == "charge_mic" and score < min_score:
        item_colors = _first_color_values(item.name, item.normalized_title)
        product_colors = _first_color_values(product.name, product.color)
        if not item_colors or not product_colors or item_colors.isdisjoint(product_colors):
            return False
    return role in {"buttons", "fingerprint", "interboard", "charge_mic"}


def _is_lower_board_charge_part(text: str | None) -> bool:
    normalized = (text or "").lower().replace("ё", "е")
    if not normalized:
        return False
    has_board = bool(re.search(r"нижн\w*\s+плат\w*|плат\w*\s+нижн\w*", normalized))
    has_charge_context = bool(
        re.search(
            r"системн\w*\s+разъ[еe]м\w*|разъ[еe]м\w*\s+зарядк\w*|" r"зарядк\w*|микрофон\w*",
            normalized,
        )
    )
    return has_board and has_charge_context


def _is_charge_board_flex_item(text: str | None) -> bool:
    normalized = (text or "").lower().replace("ё", "е")
    if not normalized:
        return False
    has_flex_word = bool(re.search(r"\b(?:шлейф|fpc|flex)\b", normalized))
    has_board_word = bool(re.search(r"\bплат\w*\b|\bboard\b", normalized))
    has_charge_context = bool(
        re.search(
            r"системн\w*\s+разъ[еe]м\w*|разъ[еe]м\w*\s+зарядк\w*|"
            r"зарядк\w*|charging\s+(?:port|connector)|dock\s+connector|микрофон\w*",
            normalized,
        )
    )
    return has_flex_word and has_board_word and has_charge_context


def _safe_lower_board_flex_auto_accept(
    item: CompetitorItem,
    product: Product,
    *,
    score: float,
    min_score: float,
) -> bool:
    if score < min_score:
        return False
    if _effective_item_type(item) != "flex" or _competitor_flex_role(item) != "charge_mic":
        return False
    item_text = " ".join(filter(None, [item.name, item.normalized_title, item.external_id]))
    product_text = _combined_product_text(product)
    if not _is_charge_board_flex_item(item_text):
        return False
    if not _is_lower_board_charge_part(product_text):
        return False
    if _flex_fingerprint_conflict(item, product) or _flex_button_control_conflict(item, product):
        return False

    item_codes = _extract_device_codes(_competitor_device_code_text(item))
    product_codes = _extract_device_codes(product_text)
    if item_codes and product_codes and item_codes.isdisjoint(product_codes):
        return False

    item_keys = _extract_device_model_keys(item_text)
    product_keys = _extract_device_model_keys(product_text)
    has_code_overlap = bool(item_codes and product_codes and item_codes & product_codes)
    has_model_overlap = _device_model_keys_overlap(item_keys, product_keys)
    if not (has_code_overlap or has_model_overlap):
        return False

    item_colors = _first_color_values(item.name, item.normalized_title)
    product_colors = _first_color_values(product.name, product.color)
    return not (item_colors and product_colors and item_colors.isdisjoint(product_colors))


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


DISPLAY_SIZE_QUALITY_MARKERS = (
    "small size",
    "full size",
    "big size",
    "large size",
)


def _display_quality_raw_is_size_marker(value: str | None) -> bool:
    raw_key = _raw_quality_key(value)
    return bool(raw_key and any(marker in raw_key for marker in DISPLAY_SIZE_QUALITY_MARKERS))


def _product_display_quality(product: Product) -> str | None:
    for value in (
        product.name,
        parse_display_attributes(product.name or "").screen_quality_grade if product.name else None,
    ):
        normalized = _normalize_display_quality_guard(value)
        if normalized:
            return normalized
    if _display_quality_raw_is_size_marker(_product_display_quality_raw(product)):
        return None
    for value in (
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


DISPLAY_OPTIONAL_MEDIUM_RAW_QUALITY = {"оптима", "стандарт"}


def _raw_quality_key(value: str | None) -> str | None:
    if not value:
        return None
    return re.sub(r"\s+", " ", value.strip().replace("ё", "е")).casefold()


def _competitor_medium_quality_can_match_unknown_product(
    item: CompetitorItem | None, competitor_quality: str | None
) -> bool:
    if not item or competitor_quality != "Copy Medium":
        return False
    raw_quality = _raw_quality_key(_competitor_display_quality_raw(item))
    return bool(
        item.competitor
        and item.competitor.casefold() == "moba"
        and raw_quality in DISPLAY_OPTIONAL_MEDIUM_RAW_QUALITY
    )


def _display_quality_requires_review(
    product: Product,
    competitor_quality: str | None,
    item: CompetitorItem | None = None,
) -> bool:
    product_quality = _product_display_quality(product)
    if (
        not product_quality
        and competitor_quality
        and _competitor_medium_quality_can_match_unknown_product(item, competitor_quality)
    ):
        return False
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


def _display_matrix_vendor_tags_from_text(text: str | None) -> set[str]:
    if not text:
        return set()
    return {
        tag for tag, pattern in DISPLAY_MATRIX_VENDOR_TAG_PATTERNS.items() if pattern.search(text)
    }


def _competitor_display_matrix_tags(item: CompetitorItem) -> set[str]:
    tags: set[str] = set()
    for text in (item.name, item.normalized_title):
        if not text:
            continue
        tags |= _normalize_matrix_tags(parse_display_attributes(text).matrix_tags)
    tags |= _normalize_matrix_tags(item.matrix_tags)
    return tags


def _product_display_matrix_tags(product: Product) -> set[str]:
    tags = _normalize_matrix_tags(parse_display_attributes(product.name or "").matrix_tags)
    tags |= _normalize_matrix_tags(product.display_matrix_tags)
    return tags


def _competitor_display_matrix_vendor_tags(item: CompetitorItem) -> set[str]:
    tags = _competitor_display_matrix_tags(item)
    for text in (item.name, item.normalized_title, item.external_id):
        tags |= _display_matrix_vendor_tags_from_text(text)
    return tags


def _product_display_matrix_vendor_tags(product: Product) -> set[str]:
    return _product_display_matrix_tags(product) | _display_matrix_vendor_tags_from_text(
        product.name
    )


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
    if re.search(r"\b(?:XIAOMI|REDMI|POCO)\b", normalized):
        for match in re.finditer(r"\b\d{4}[A-Z]{4,8}\b", normalized):
            codes.add(match.group(0))
    for match in re.finditer(r"\b\d{6,}[A-Z]{1,4}\b", normalized):
        codes.add(match.group(0))
    for match in re.finditer(r"\b\d{5,}[A-Z]{2,8}\b", normalized):
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

    if re.search(r"\b(?:TECNO|INFINIX|ITEL)\b", normalized):
        for match in re.finditer(r"\b[A-Z]{2,4}\d[A-Z0-9]{0,3}\b", normalized):
            code = match.group(0)
            if code not in {
                "AMOLED",
                "FHD",
                "FPC",
                "HD",
                "INCL",
                "LCD",
                "OLED",
                "OR100",
                "USB",
            }:
                codes.add(code)
                if re.match(r"^[A-Z]{2,4}\dN$", code):
                    codes.add(code[:-1])

    if re.search(r"\b(?:ASUS|ZENFONE|ROG\s+PHONE)\b", normalized):
        for match in re.finditer(r"\b(?:AI|ZS|ZC)\d{3,5}[A-Z0-9]{0,4}\b", normalized):
            codes.add(match.group(0))

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


def _display_model_code_overlap_details(
    item: CompetitorItem, product: Product
) -> dict[str, list[str]]:
    competitor_codes = _extract_device_codes(_competitor_device_code_text(item))
    product_codes = _extract_device_codes(product.name)
    return {
        "competitor_codes": sorted(competitor_codes),
        "product_codes": sorted(product_codes),
        "overlap_codes": sorted(competitor_codes.intersection(product_codes)),
    }


def _combined_item_text(item: CompetitorItem) -> str:
    return " ".join(
        value
        for value in (item.name, item.normalized_title, item.category, item.category_group)
        if value
    )


def _combined_product_text(product: Product) -> str:
    return " ".join(
        value
        for value in (product.name, product.category, product.subject, product.subject_1c)
        if value
    )


def _normalized_rule_text(text: str | None) -> str:
    normalized = (text or "").lower().replace("ё", "е")
    normalized = re.sub(r"[^a-z0-9а-я.,]+", " ", normalized)
    return " ".join(normalized.split())


def _has_redmi_pad_se_87(text: str) -> bool:
    return "redmi pad se" in text and bool(re.search(r"\b8[.,]7\b", text))


def _extract_screen_inches(text: str) -> set[str]:
    return {
        match.group(1).replace(",", ".")
        for match in re.finditer(
            r"\b(\d{1,2}(?:[.,]\d)?)\s*(?:\"|дюйм|дюйма|inch|inches)\b",
            text,
        )
    }


def _explicit_model_conflict_reason(item: CompetitorItem, product: Product) -> str | None:
    item_text = _normalized_rule_text(_combined_item_text(item))
    product_text = _normalized_rule_text(_combined_product_text(product))
    if not _is_screen_or_touch_part(item_text) or not _is_screen_or_touch_part(product_text):
        return None

    item_has_mix_flip = "mix flip" in item_text
    product_has_mix_flip = "mix flip" in product_text
    item_has_mi_mix = "mi mix" in item_text and not item_has_mix_flip
    product_has_mi_mix = "mi mix" in product_text and not product_has_mix_flip
    if (item_has_mi_mix and product_has_mix_flip) or (product_has_mi_mix and item_has_mix_flip):
        return "xiaomi_mi_mix_vs_mix_flip"

    item_has_redmi_go = "redmi go" in item_text
    product_has_redmi_go = "redmi go" in product_text
    item_redmi_number = re.search(r"\bredmi\s+(?:note\s+)?\d{1,2}[a-z]?\b", item_text)
    product_redmi_number = re.search(r"\bredmi\s+(?:note\s+)?\d{1,2}[a-z]?\b", product_text)
    if (item_has_redmi_go and product_redmi_number) or (product_has_redmi_go and item_redmi_number):
        return "xiaomi_redmi_go_vs_numbered_redmi"

    if "redmi pad se" in item_text and "redmi pad se" in product_text:
        item_inches = _extract_screen_inches(item_text)
        product_inches = _extract_screen_inches(product_text)
        if item_inches and product_inches and item_inches.isdisjoint(product_inches):
            return "xiaomi_redmi_pad_se_size_conflict"
        if _has_redmi_pad_se_87(item_text) != _has_redmi_pad_se_87(product_text):
            return "xiaomi_redmi_pad_se_87_conflict"

    return None


def _extract_xiaomi_regional_model_label(text: str | None) -> str | None:
    normalized = _normalized_rule_text(text)
    match = re.search(r"\bxiaomi\s+(\d{1,2}t(?:\s+(?:pro|ultra))?)\b", normalized)
    if not match:
        return None
    return f"xiaomi {match.group(1)}"


def _safe_xiaomi_regional_model_auto_accept(
    item: CompetitorItem,
    product: Product,
    *,
    item_type: str | None,
    score: float,
    min_score: float,
) -> str | None:
    if score < min_score:
        return None
    if item_type not in {"display", "other"}:
        return None
    item_text = _combined_item_text(item)
    product_text = _combined_product_text(product)
    if not _is_screen_or_touch_part(item_text) or not _is_screen_or_touch_part(product_text):
        return None
    if _explicit_model_conflict_reason(item, product):
        return None
    item_label = _extract_xiaomi_regional_model_label(item_text)
    product_label = _extract_xiaomi_regional_model_label(product_text)
    if item_label and product_label and item_label == product_label:
        return item_label
    return None


COPY_DISPLAY_CONSTRUCTIONS = {
    "In-Cell",
    "On-Cell",
    "COF",
    "COG",
    "HARD_OLED",
    "SOFT_OLED",
    "Hard OLED",
    "Soft OLED",
}
COPY_DISPLAY_QUALITIES = {"Copy High", "Copy Medium", "Copy Low"}
DISPLAY_EXACT_MODEL_GUARDRAIL_OVERRIDE_REASONS = {
    "compatibility_model_conflict",
    "compatibility_phone_model_conflict",
}


def _display_text_has_original_refurb_signal(text: str | None) -> bool:
    normalized = (text or "").lower().replace("ё", "е")
    return bool(
        "биток" in normalized
        or "переклей" in normalized
        or "change glass" in normalized
        or "replaced glass" in normalized
        or "refurb" in normalized
        or re.search(r"замен\w*(?:\s+\w+){0,3}\s+стекл\w*", normalized)
    )


def _display_text_has_original_signal(text: str | None) -> bool:
    normalized = (text or "").lower().replace("ё", "е")
    return bool(
        _display_text_has_original_refurb_signal(normalized)
        or re.search(r"\b(orig|orig100|original|or100|100%\s*or|or\s*100%)\b", normalized)
        or "ориг" in normalized
    )


def _display_text_has_copy_signal(text: str | None) -> bool:
    normalized = (text or "").lower().replace("ё", "е")
    return bool(
        "mecanico" in normalized
        or re.search(r"\bamp\b", normalized)
        or re.search(r"\b(copy|копия|analog|аналог)\b", normalized)
    )


def _display_item_has_aftermarket_signal(item: CompetitorItem) -> bool:
    item_text = _combined_item_text(item)
    if _display_text_has_original_signal(item_text):
        return False
    normalized = (item_text or "").lower().replace("ё", "е")
    return bool(
        _display_text_has_copy_signal(item_text)
        or _competitor_display_type(item)
        or _competitor_display_construction(item)
        or _competitor_display_matrix_tags(item)
        or re.search(r"\blcd\s+диспле", normalized)
    )


def _display_text_has_size_modifier(text: str | None) -> bool:
    normalized = (text or "").lower().replace("ё", "е")
    return bool(
        "small size" in normalized
        or "big size" in normalized
        or "large size" in normalized
        or "mini size" in normalized
    )


def _is_laptop_matrix_flex(text: str | None) -> bool:
    normalized = (text or "").lower().replace("ё", "е")
    if not normalized:
        return False
    if re.search(r"\bfpc[-\s_/]*mtx[-\s_/]*lp\b", normalized):
        return True
    has_matrix_flex = bool(
        re.search(r"\bшлейф\w*\s+матриц\w*\b", normalized)
        or re.search(r"\bmatrix\s+(?:cable|flex)\b", normalized)
    )
    has_laptop_context = bool(
        re.search(
            r"\b(ноутбук\w*|laptop|macbook|vaio|thinkpad|ideapad|pavilion|inspiron)\b",
            normalized,
        )
    )
    return has_matrix_flex and has_laptop_context


def _long_device_codes(codes: set[str]) -> set[str]:
    return {code for code in codes if len(re.sub(r"[^A-Z0-9]", "", code.upper())) >= 8}


def _extract_accessory_model_codes(text: str | None) -> set[str]:
    if not text:
        return set()
    normalized = _normalize_code_text(text)
    codes = set(_extract_device_codes(normalized))
    for match in re.finditer(
        r"\b(?:[A-Z]{1,6}-\d{2,5}[A-Z]*|[A-Z]{1,4}\d{1,5}[A-Z]*|\d{3,4}[A-Z]{1,4})\b", normalized
    ):
        codes.add(match.group(0))
    filtered: set[str] = set()
    for code in codes:
        compact = re.sub(r"[^A-Z0-9]", "", code.upper())
        if not compact:
            continue
        if len(compact) < 3:
            continue
        if re.fullmatch(r"ORIG\d{0,3}|OR\d{0,3}", compact):
            continue
        if compact.endswith("MAH"):
            continue
        if compact.endswith("CC") and compact[:-2].isdigit():
            continue
        if re.fullmatch(r"\d{3,4}P|\d{3,4}X\d{3,4}", compact):
            continue
        if re.fullmatch(r"(?:PD|QC)\d{1,3}W?", compact):
            continue
        if re.fullmatch(r"\d{2,5}(?:W|V|C)", compact):
            continue
        if re.fullmatch(r"\d{4}", compact):
            continue
        if compact in {"USB", "QC30", "QC3", "PD20W", "PD20", "LIION", "LIPOL"}:
            continue
        if not (re.search(r"[A-Z]", compact) and re.search(r"\d", compact)):
            continue
        filtered.add(compact)
    return filtered


def _explicit_display_attribute_conflict_reason(
    item: CompetitorItem,
    product: Product,
    competitor_quality: str | None = None,
) -> str | None:
    item_text = _combined_item_text(item)
    product_text = _combined_product_text(product)
    if not _is_screen_or_touch_part(item_text) or not _is_screen_or_touch_part(product_text):
        return None

    product_quality = _product_display_quality(product)
    competitor_quality = competitor_quality or _competitor_display_quality(item)
    competitor_construction = _competitor_display_construction(item)

    if product_quality == "Original Refurbished" and competitor_quality != ("Original Refurbished"):
        if competitor_quality in {"Original", *COPY_DISPLAY_QUALITIES}:
            return "display_original_refurb_vs_regular_competitor"
        if _display_text_has_copy_signal(item_text):
            return "display_original_refurb_vs_regular_competitor"
        if (
            competitor_construction in COPY_DISPLAY_CONSTRUCTIONS
            and not _display_text_has_original_refurb_signal(item_text)
        ):
            return "display_original_refurb_vs_regular_competitor"

    if (
        product_quality == "Original"
        and competitor_quality is None
        and competitor_construction in COPY_DISPLAY_CONSTRUCTIONS
        and not _display_text_has_original_signal(item_text)
    ):
        return "display_original_vs_copy_construction"
    if (
        product_quality == "Original"
        and competitor_quality is None
        and _display_text_has_copy_signal(item_text)
        and not _display_text_has_original_signal(item_text)
    ):
        return "display_original_vs_copy_signal"
    if (
        product_quality == "Original"
        and competitor_quality is None
        and _display_text_has_original_signal(product_text)
        and _display_item_has_aftermarket_signal(item)
    ):
        return "display_original_vs_aftermarket_competitor"

    return None


def _explicit_display_subject_conflict_reason(
    item: CompetitorItem,
    product: Product,
    *,
    item_type: str | None = None,
) -> str | None:
    if item_type and item_type != "display":
        return None
    item_text = _combined_item_text(item)
    product_text = _combined_product_text(product)
    if _is_screen_or_touch_part(item_text) and not _is_screen_or_touch_part(product_text):
        return "display_candidate_vs_non_display_product"
    return None


def _display_exact_model_evidence_details(
    item: CompetitorItem, product: Product
) -> dict[str, list[str]]:
    competitor_codes = _extract_device_codes(_competitor_device_code_text(item))
    product_codes = _extract_device_codes(product.name)
    competitor_keys = _extract_device_model_keys(_competitor_device_model_text(item))
    product_keys = _extract_device_model_keys(product.name)
    return {
        "competitor_codes": sorted(competitor_codes),
        "product_codes": sorted(product_codes),
        "overlap_codes": sorted(competitor_codes.intersection(product_codes)),
        "competitor_model_keys": sorted(competitor_keys),
        "product_model_keys": sorted(product_keys),
        "overlap_model_keys": sorted(competitor_keys.intersection(product_keys)),
    }


def _has_safe_display_exact_model_evidence(item: CompetitorItem, product: Product) -> bool:
    details = _display_exact_model_evidence_details(item, product)
    if details["competitor_codes"] and details["product_codes"]:
        return bool(details["overlap_codes"])
    return bool(details["overlap_codes"] or details["overlap_model_keys"])


def _basic_or_display_exact_model_guardrails_allowed(
    item: CompetitorItem,
    product: Product,
    *,
    item_type: str | None,
) -> bool:
    guardrails = basic_candidate_guardrails(item, product)
    if guardrails.allowed:
        return True
    if item_type != "display":
        return False
    if guardrails.reason not in DISPLAY_EXACT_MODEL_GUARDRAIL_OVERRIDE_REASONS:
        return False
    if _explicit_model_conflict_reason(item, product):
        return False
    if _display_text_model_conflict(item, product):
        return False
    if _display_model_code_conflict(item, product):
        return False
    return _has_safe_display_exact_model_evidence(item, product)


def _safe_display_original_quality_auto_accept(
    item: CompetitorItem,
    product: Product,
    *,
    item_type: str | None,
    score: float,
    min_score: float,
) -> str | None:
    score_min = min_score
    details = _display_exact_model_evidence_details(item, product)
    if score < min_score:
        score_min = 0.75
        if score < score_min:
            return None
        if not details["overlap_codes"]:
            return None
        if _display_low_score_condition_mismatch(item, product):
            return None
    if score < score_min:
        return None
    if item_type != "display":
        return None
    if _explicit_display_subject_conflict_reason(item, product, item_type=item_type):
        return None
    if not _basic_or_display_exact_model_guardrails_allowed(
        item,
        product,
        item_type=item_type,
    ):
        return None
    item_text = _combined_item_text(item)
    product_text = _combined_product_text(product)
    if not _is_screen_or_touch_part(item_text) or not _is_screen_or_touch_part(product_text):
        return None
    if _explicit_model_conflict_reason(item, product):
        return None
    if _display_text_model_conflict(item, product):
        return None
    if _display_model_code_conflict(item, product):
        return None
    if not _display_exact_model_evidence_is_safe(details):
        return None
    competitor_quality = _competitor_display_quality(item)
    product_quality = _product_display_quality(product)
    if _explicit_display_attribute_conflict_reason(item, product, competitor_quality):
        return None
    if _display_quality_conflict(product, competitor_quality):
        return None
    if competitor_quality == "Original" and product_quality == "Original":
        reason = "display_original_quality_exact_model"
    elif competitor_quality == "Original Refurbished" and product_quality == "Original Refurbished":
        reason = "display_original_refurb_quality_exact_model"
    else:
        return None

    competitor_has_frame = _competitor_display_has_frame(item)
    if display_frame_conflict(product, competitor_has_frame):
        return None
    if _display_touch_conflict(product, _competitor_display_has_touch(item)):
        return None
    if _display_backlight_conflict(product, _competitor_display_backlight(item)):
        return None
    if _display_matrix_tags_conflict(product, _competitor_display_matrix_tags(item)):
        return None
    competitor_construction = _competitor_display_construction(item)
    if _display_construction_conflict(product, competitor_construction):
        return None
    if _display_matrix_family_conflict(
        product,
        _competitor_display_type(item),
        competitor_construction,
    ):
        return None
    if _display_refresh_rate_conflict(product, _competitor_display_refresh_rate_hz(item)):
        return None
    if _display_color_conflict(product, _competitor_display_color(item)):
        return None
    return reason


def _display_exact_model_evidence_is_safe(details: dict[str, list[str]]) -> bool:
    if details["competitor_codes"] and details["product_codes"]:
        return bool(details["overlap_codes"])
    return bool(details["overlap_codes"] or details["overlap_model_keys"])


def _safe_display_unspecified_quality_auto_accept(
    item: CompetitorItem,
    product: Product,
    *,
    item_type: str | None,
    score: float,
) -> str | None:
    if score < 0.83:
        return None
    if item_type != "display":
        return None
    if _explicit_display_subject_conflict_reason(item, product, item_type=item_type):
        return None
    if not _basic_or_display_exact_model_guardrails_allowed(
        item,
        product,
        item_type=item_type,
    ):
        return None
    item_text = _combined_item_text(item)
    product_text = _combined_product_text(product)
    if not _is_screen_or_touch_part(item_text) or not _is_screen_or_touch_part(product_text):
        return None
    if _explicit_model_conflict_reason(item, product):
        return None
    if _display_text_model_conflict(item, product):
        return None
    if _display_model_code_conflict(item, product):
        return None
    details = _display_exact_model_evidence_details(item, product)
    if not _display_exact_model_evidence_is_safe(details):
        return None
    competitor_quality = _competitor_display_quality(item)
    if competitor_quality is not None or _product_display_quality(product) is not None:
        return None
    if _explicit_display_attribute_conflict_reason(item, product, competitor_quality):
        return None
    if _display_condition_conflict_reason(item, product):
        return None
    competitor_has_frame = _competitor_display_has_frame(item)
    if display_frame_requires_review(product, competitor_has_frame):
        return None
    if display_frame_conflict(product, competitor_has_frame):
        return None
    if _display_touch_conflict(product, _competitor_display_has_touch(item)):
        return None
    if _display_backlight_conflict(product, _competitor_display_backlight(item)):
        return None
    if _display_matrix_tags_conflict(product, _competitor_display_matrix_tags(item)):
        return None
    competitor_construction = _competitor_display_construction(item)
    if _display_construction_conflict(product, competitor_construction):
        return None
    if _display_matrix_family_conflict(
        product,
        _competitor_display_type(item),
        competitor_construction,
    ):
        return None
    if _display_refresh_rate_conflict(product, _competitor_display_refresh_rate_hz(item)):
        return None
    competitor_color = _competitor_display_color(item)
    product_color = _product_display_color(product)
    if not competitor_color or not product_color:
        return None
    if _display_color_conflict(product, competitor_color):
        return None
    return "display_unspecified_quality_exact_model"


DISPLAY_LOW_SCORE_CONDITION_TOKENS = (
    "снятый",
    "снятая",
    "снято",
    "с разбора",
    "биток",
    "дефект",
    "defect",
)


DISPLAY_USED_CONDITION_TOKENS = (
    "снятый",
    "снятая",
    "снято",
    "с разбора",
    "биток",
)
DISPLAY_NEW_PART_CONDITION_TOKENS = (
    "новая запчасть",
    "новую запчасть",
    "новой запчасти",
    "new part",
)
DISPLAY_DEFECT_CONDITION_TOKENS = ("дефект", "defect")


def _display_low_score_condition_mismatch(item: CompetitorItem, product: Product) -> bool:
    item_text = _normalized_rule_text(_combined_item_text(item))
    product_text = _normalized_rule_text(_combined_product_text(product))
    for token in DISPLAY_LOW_SCORE_CONDITION_TOKENS:
        if (token in item_text) != (token in product_text):
            return True
    return False


def _has_any_token(text: str, tokens: Iterable[str]) -> bool:
    return any(token in text for token in tokens)


def _has_defect_condition(text: str) -> bool:
    normalized = _normalized_rule_text(text)
    if re.search(r"\bбез\s+дефект", normalized):
        return False
    return _has_any_token(normalized, DISPLAY_DEFECT_CONDITION_TOKENS)


def _display_condition_conflict_reason(item: CompetitorItem, product: Product) -> str | None:
    item_text = _normalized_rule_text(_combined_item_text(item))
    product_text = _normalized_rule_text(_combined_product_text(product))
    item_is_new = _has_any_token(item_text, DISPLAY_NEW_PART_CONDITION_TOKENS)
    product_is_new = _has_any_token(product_text, DISPLAY_NEW_PART_CONDITION_TOKENS)
    item_is_used = _has_any_token(item_text, DISPLAY_USED_CONDITION_TOKENS)
    product_is_used = _has_any_token(product_text, DISPLAY_USED_CONDITION_TOKENS)
    if (item_is_new and product_is_used) or (product_is_new and item_is_used):
        return "display_new_part_vs_used_condition"

    item_has_defect = _has_defect_condition(item_text)
    product_has_defect = _has_defect_condition(product_text)
    if item_has_defect != product_has_defect:
        return "display_defect_condition_conflict"

    return None


def _housing_condition_conflict_reason(item: CompetitorItem, product: Product) -> str | None:
    item_has_defect = _has_defect_condition(_combined_item_text(item))
    product_has_defect = _has_defect_condition(_combined_product_text(product))
    if item_has_defect != product_has_defect:
        return "housing_defect_condition_conflict"
    return None


def _safe_display_copy_construction_auto_accept(
    item: CompetitorItem,
    product: Product,
    *,
    item_type: str | None,
    score: float,
    min_score: float,
) -> str | None:
    if score < min_score:
        return None
    if item_type != "display":
        return None
    if _explicit_display_subject_conflict_reason(item, product, item_type=item_type):
        return None
    if not _basic_or_display_exact_model_guardrails_allowed(
        item,
        product,
        item_type=item_type,
    ):
        return None
    item_text = _combined_item_text(item)
    product_text = _combined_product_text(product)
    if not _is_screen_or_touch_part(item_text) or not _is_screen_or_touch_part(product_text):
        return None
    if _explicit_model_conflict_reason(item, product):
        return None
    if _display_text_model_conflict(item, product):
        return None
    if _display_model_code_conflict(item, product):
        return None
    if not _has_safe_display_exact_model_evidence(item, product):
        return None
    if _competitor_display_matrix_tags(item) or _product_display_matrix_tags(product):
        return None
    competitor_quality = _competitor_display_quality(item)
    product_quality = _product_display_quality(product)
    if competitor_quality is not None or product_quality not in COPY_DISPLAY_QUALITIES:
        return None
    competitor_construction = _competitor_display_construction(item)
    product_construction = _product_display_construction(product)
    if (
        not competitor_construction
        or not product_construction
        or competitor_construction != product_construction
        or competitor_construction not in LCD_PIXEL_CONSTRUCTIONS
    ):
        return None
    if display_frame_conflict(product, _competitor_display_has_frame(item)):
        return None
    if _display_touch_conflict(product, _competitor_display_has_touch(item)):
        return None
    if _display_backlight_conflict(product, _competitor_display_backlight(item)):
        return None
    if _display_matrix_family_conflict(
        product,
        _competitor_display_type(item),
        competitor_construction,
    ):
        return None
    if _display_refresh_rate_conflict(product, _competitor_display_refresh_rate_hz(item)):
        return None
    if _display_color_conflict(product, _competitor_display_color(item)):
        return None
    return "display_copy_construction_exact_model"


def _safe_display_matrix_tag_auto_accept(
    item: CompetitorItem,
    product: Product,
    *,
    item_type: str | None,
    score: float,
    min_score: float,
) -> str | None:
    if score < min_score:
        return None
    if item_type != "display":
        return None
    if _explicit_display_subject_conflict_reason(item, product, item_type=item_type):
        return None
    if not _basic_or_display_exact_model_guardrails_allowed(
        item,
        product,
        item_type=item_type,
    ):
        return None
    if _explicit_model_conflict_reason(item, product):
        return None
    if _display_text_model_conflict(item, product):
        return None
    if _display_model_code_conflict(item, product):
        return None
    if not _has_safe_display_exact_model_evidence(item, product):
        return None
    competitor_tags = _competitor_display_matrix_tags(item)
    product_tags = _product_display_matrix_tags(product)
    overlap_tags = competitor_tags.intersection(product_tags)
    if not overlap_tags:
        return None
    competitor_quality = _competitor_display_quality(item)
    if _explicit_display_attribute_conflict_reason(item, product, competitor_quality):
        return None
    if _display_quality_conflict(product, competitor_quality):
        return None
    if display_frame_conflict(product, _competitor_display_has_frame(item)):
        return None
    if _display_touch_conflict(product, _competitor_display_has_touch(item)):
        return None
    if _display_backlight_conflict(product, _competitor_display_backlight(item)):
        return None
    competitor_construction = _competitor_display_construction(item)
    if _display_construction_conflict(product, competitor_construction):
        return None
    if _display_matrix_family_conflict(
        product,
        _competitor_display_type(item),
        competitor_construction,
    ):
        return None
    if _display_refresh_rate_conflict(product, _competitor_display_refresh_rate_hz(item)):
        return None
    if _display_color_conflict(product, _competitor_display_color(item)):
        return None
    return "display_same_matrix_tag_exact_model"


def _safe_display_copy_matrix_type_auto_accept(
    item: CompetitorItem,
    product: Product,
    *,
    item_type: str | None,
    score: float,
    min_score: float,
) -> str | None:
    if score < min_score:
        return None
    if item_type != "display":
        return None
    if _explicit_display_subject_conflict_reason(item, product, item_type=item_type):
        return None
    if not _basic_or_display_exact_model_guardrails_allowed(
        item,
        product,
        item_type=item_type,
    ):
        return None
    item_text = _combined_item_text(item)
    product_text = _combined_product_text(product)
    if not _is_screen_or_touch_part(item_text) or not _is_screen_or_touch_part(product_text):
        return None
    if _explicit_model_conflict_reason(item, product):
        return None
    if _display_text_model_conflict(item, product):
        return None
    if _display_model_code_conflict(item, product):
        return None
    if not _has_safe_display_exact_model_evidence(item, product):
        return None
    if _display_text_has_original_signal(item_text):
        return None
    if _display_text_has_size_modifier(item_text) or _display_text_has_size_modifier(product_text):
        return None
    if _competitor_display_matrix_tags(item) or _product_display_matrix_tags(product):
        return None
    competitor_quality = _competitor_display_quality(item)
    product_quality = _product_display_quality(product)
    if competitor_quality is not None or product_quality not in COPY_DISPLAY_QUALITIES:
        return None
    if _explicit_display_attribute_conflict_reason(item, product, competitor_quality):
        return None
    if _display_quality_conflict(product, competitor_quality):
        return None
    competitor_type = _competitor_display_type(item)
    product_type = _product_display_type(product)
    if (
        not competitor_type
        or not product_type
        or competitor_type != product_type
        or competitor_type not in OLED_DISPLAY_TYPES
    ):
        return None
    competitor_construction = _competitor_display_construction(item)
    if competitor_construction:
        return None
    if display_frame_conflict(product, _competitor_display_has_frame(item)):
        return None
    if _display_touch_conflict(product, _competitor_display_has_touch(item)):
        return None
    if _display_backlight_conflict(product, _competitor_display_backlight(item)):
        return None
    if _display_matrix_family_conflict(product, competitor_type, competitor_construction):
        return None
    if _display_refresh_rate_conflict(product, _competitor_display_refresh_rate_hz(item)):
        return None
    if _display_color_conflict(product, _competitor_display_color(item)):
        return None
    return "display_copy_matrix_type_exact_model"


def _is_screen_or_touch_part(text: str | None) -> bool:
    normalized = (text or "").lower().replace("ё", "е")
    if not normalized:
        return False
    component_only_tokens = (
        "защитное стекло",
        "стекло камеры",
        "стекло задней камеры",
        "стекло для переклейки",
        "стекло модуля",
        "oca",
        "осa",
        "пленка",
        "плёнка",
        "film",
        "camera glass",
        "screen protector",
    )
    if any(token in normalized for token in component_only_tokens):
        return False
    screen_tokens = (
        "дисплей",
        "экран",
        "тачскрин",
        "сенсорное стекло",
        "lcd",
        "oled",
        "amoled",
        "touchscreen",
        "digitizer",
    )
    return any(token in normalized for token in screen_tokens)


def _safe_explicit_code_overlap_auto_accept(
    item: CompetitorItem,
    product: Product,
    *,
    item_type: str | None,
    score: float,
    min_score: float,
) -> bool:
    if score < min_score:
        return False
    if item_type not in {"display", "other"}:
        return False
    item_text = _combined_item_text(item)
    product_text = _combined_product_text(product)
    if not _is_screen_or_touch_part(item_text) or not _is_screen_or_touch_part(product_text):
        return False
    if _explicit_model_conflict_reason(item, product):
        return False
    if item_type == "display":
        competitor_quality = _competitor_display_quality(item)
        if _explicit_display_attribute_conflict_reason(item, product, competitor_quality):
            return False
        if _display_quality_conflict(product, competitor_quality):
            return False
        if _display_quality_requires_review(product, competitor_quality, item):
            return False
        if display_frame_conflict(product, _competitor_display_has_frame(item)):
            return False
        if _display_touch_conflict(product, _competitor_display_has_touch(item)):
            return False
        if _display_backlight_conflict(product, _competitor_display_backlight(item)):
            return False
        if _display_matrix_tags_conflict(product, _competitor_display_matrix_tags(item)):
            return False
        competitor_construction = _competitor_display_construction(item)
        if _display_construction_conflict(product, competitor_construction):
            return False
        if _display_matrix_family_conflict(
            product,
            _competitor_display_type(item),
            competitor_construction,
        ):
            return False
        if _display_refresh_rate_conflict(product, _competitor_display_refresh_rate_hz(item)):
            return False
        if _display_color_conflict(product, _competitor_display_color(item)):
            return False
    details = _display_model_code_overlap_details(item, product)
    return bool(details["overlap_codes"])


def _code_overlap_compatibility_brand(item: CompetitorItem, product: Product) -> str:
    brand = (
        _normalize_brand(item.parsed_device_brand)
        or _extract_brand_from_text(_combined_item_text(item))
        or _normalize_brand(product.brand)
        or _extract_brand_from_text(_combined_product_text(product))
    )
    return brand or "unknown"


def _ensure_code_overlap_compatibilities(
    session: Session,
    item: CompetitorItem,
    product: Product,
    details: dict[str, list[str]],
) -> int:
    overlap_codes = details.get("overlap_codes") or []
    if not overlap_codes:
        return 0
    brand = _code_overlap_compatibility_brand(item, product)
    created = 0
    for code in overlap_codes:
        normalized_code = code.strip().lower()
        if not normalized_code:
            continue
        exists_query = select(CompetitorItemCompatibility.id).where(
            CompetitorItemCompatibility.competitor_item_id == item.id,
            func.lower(CompetitorItemCompatibility.device_brand) == brand.lower(),
            func.lower(CompetitorItemCompatibility.device_model) == normalized_code,
            CompetitorItemCompatibility.device_variant.is_(None),
        )
        if session.scalar(exists_query):
            continue
        session.add(
            CompetitorItemCompatibility(
                competitor_item_id=item.id,
                device_brand=brand,
                device_model=normalized_code,
                device_variant=None,
                source="auto_code_overlap",
                notes=f"auto-accepted by shared device code for product {product.article or product.id}",
            )
        )
        created += 1
    return created


def _ensure_model_text_compatibility(
    session: Session,
    item: CompetitorItem,
    product: Product,
    model_label: str,
) -> int:
    parts = model_label.split(maxsplit=1)
    brand = parts[0] if parts else _code_overlap_compatibility_brand(item, product)
    model = parts[1] if len(parts) > 1 else model_label
    exists_query = select(CompetitorItemCompatibility.id).where(
        CompetitorItemCompatibility.competitor_item_id == item.id,
        func.lower(CompetitorItemCompatibility.device_brand) == brand.lower(),
        func.lower(CompetitorItemCompatibility.device_model) == model.lower(),
        CompetitorItemCompatibility.device_variant.is_(None),
    )
    if session.scalar(exists_query):
        return 0
    session.add(
        CompetitorItemCompatibility(
            competitor_item_id=item.id,
            device_brand=brand,
            device_model=model,
            device_variant=None,
            source="auto_model_text",
            notes=f"auto-accepted by explicit model text for product {product.article or product.id}",
        )
    )
    return 1


def _ensure_display_model_key_compatibilities(
    session: Session,
    item: CompetitorItem,
    product: Product,
    details: dict[str, list[str]],
) -> int:
    overlap_model_keys = details.get("overlap_model_keys") or []
    if not overlap_model_keys:
        return 0
    brand = _code_overlap_compatibility_brand(item, product)
    created = 0
    for model_key in overlap_model_keys:
        normalized_model = model_key.strip().lower()
        if not normalized_model:
            continue
        exists_query = select(CompetitorItemCompatibility.id).where(
            CompetitorItemCompatibility.competitor_item_id == item.id,
            func.lower(CompetitorItemCompatibility.device_brand) == brand.lower(),
            func.lower(CompetitorItemCompatibility.device_model) == normalized_model,
            CompetitorItemCompatibility.device_variant.is_(None),
        )
        if session.scalar(exists_query):
            continue
        session.add(
            CompetitorItemCompatibility(
                competitor_item_id=item.id,
                device_brand=brand,
                device_model=normalized_model,
                device_variant=None,
                source="auto_model_key",
                notes=f"auto-accepted by shared model key for product {product.article or product.id}",
            )
        )
        created += 1
    return created


def _auto_reject_explicit_model_conflicts(session: Session) -> int:
    matches = (
        session.execute(
            select(CompetitorItemMatch)
            .options(
                joinedload(CompetitorItemMatch.competitor_item),
                joinedload(CompetitorItemMatch.product),
            )
            .where(
                CompetitorItemMatch.status.in_(
                    (
                        CompetitorItemMatchStatus.SUGGESTED,
                        CompetitorItemMatchStatus.NEEDS_REVIEW,
                        CompetitorItemMatchStatus.AMBIGUOUS,
                    )
                ),
                CompetitorItemMatch.method != CompetitorItemMatchMethod.MANUAL,
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    rejected = 0
    for match in matches:
        item = match.competitor_item
        product = match.product
        if not item or not product:
            continue
        reason = _explicit_model_conflict_reason(item, product)
        if not reason:
            continue
        rationale = dict(match.rationale_json or {})
        rationale["auto_reject_explicit_model_conflict"] = {
            "reason": reason,
            "rejected_at": now.isoformat(),
        }
        match.status = CompetitorItemMatchStatus.REJECTED
        match.rationale_json = rationale
        match.updated_at = now
        session.add(match)
        rejected += 1
    return rejected


def _auto_reject_display_subject_conflicts(session: Session) -> int:
    matches = (
        session.execute(
            select(CompetitorItemMatch)
            .options(
                joinedload(CompetitorItemMatch.competitor_item),
                joinedload(CompetitorItemMatch.product),
            )
            .where(
                CompetitorItemMatch.status.in_(
                    (
                        CompetitorItemMatchStatus.SUGGESTED,
                        CompetitorItemMatchStatus.NEEDS_REVIEW,
                        CompetitorItemMatchStatus.AMBIGUOUS,
                    )
                ),
                CompetitorItemMatch.method != CompetitorItemMatchMethod.MANUAL,
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    rejected = 0
    for match in matches:
        item = match.competitor_item
        product = match.product
        if not item or not product:
            continue
        item_type = _effective_item_type(item)
        reason = _explicit_display_subject_conflict_reason(
            item,
            product,
            item_type=item_type,
        )
        if not reason:
            continue
        rationale = dict(match.rationale_json or {})
        rationale["auto_reject_display_subject_conflict"] = {
            "reason": reason,
            "rejected_at": now.isoformat(),
        }
        match.status = CompetitorItemMatchStatus.REJECTED
        match.rationale_json = rationale
        match.updated_at = now
        session.add(match)
        rejected += 1
    return rejected


def _auto_reject_display_frame_conflicts(session: Session) -> int:
    matches = (
        session.execute(
            select(CompetitorItemMatch)
            .options(
                joinedload(CompetitorItemMatch.competitor_item),
                joinedload(CompetitorItemMatch.product),
            )
            .where(
                CompetitorItemMatch.status.in_(
                    (
                        CompetitorItemMatchStatus.SUGGESTED,
                        CompetitorItemMatchStatus.NEEDS_REVIEW,
                        CompetitorItemMatchStatus.AMBIGUOUS,
                    )
                ),
                CompetitorItemMatch.method != CompetitorItemMatchMethod.MANUAL,
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    rejected = 0
    for match in matches:
        item = match.competitor_item
        product = match.product
        if not item or not product or _effective_item_type(item) != "display":
            continue
        competitor_has_frame = _competitor_display_has_frame(item)
        if not display_frame_conflict(product, competitor_has_frame):
            continue
        rationale = dict(match.rationale_json or {})
        rationale["auto_reject_display_frame_conflict"] = {
            "reason": "display_frame_conflict",
            "competitor_has_frame": competitor_has_frame,
            "product_display_has_frame": product.display_has_frame,
            "rejected_at": now.isoformat(),
        }
        match.status = CompetitorItemMatchStatus.REJECTED
        match.rationale_json = rationale
        match.updated_at = now
        session.add(match)
        rejected += 1
    return rejected


def _auto_reject_display_color_conflicts(session: Session) -> int:
    matches = (
        session.execute(
            select(CompetitorItemMatch)
            .options(
                joinedload(CompetitorItemMatch.competitor_item),
                joinedload(CompetitorItemMatch.product),
            )
            .where(
                CompetitorItemMatch.status.in_(
                    (
                        CompetitorItemMatchStatus.SUGGESTED,
                        CompetitorItemMatchStatus.NEEDS_REVIEW,
                        CompetitorItemMatchStatus.AMBIGUOUS,
                    )
                ),
                CompetitorItemMatch.method != CompetitorItemMatchMethod.MANUAL,
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    rejected = 0
    for match in matches:
        item = match.competitor_item
        product = match.product
        if not item or not product or _effective_item_type(item) != "display":
            continue
        competitor_color = _competitor_display_color(item)
        if not _display_color_conflict(product, competitor_color):
            continue
        rationale = dict(match.rationale_json or {})
        rationale["auto_reject_display_color_conflict"] = {
            "reason": "display_color_conflict",
            "competitor_color": competitor_color,
            "product_color": _product_display_color(product),
            "rejected_at": now.isoformat(),
        }
        match.status = CompetitorItemMatchStatus.REJECTED
        match.rationale_json = rationale
        match.updated_at = now
        session.add(match)
        rejected += 1
    return rejected


def _auto_reject_part_color_conflicts(session: Session) -> int:
    matches = (
        session.execute(
            select(CompetitorItemMatch)
            .options(
                joinedload(CompetitorItemMatch.competitor_item),
                joinedload(CompetitorItemMatch.product),
            )
            .where(
                CompetitorItemMatch.status.in_(
                    (
                        CompetitorItemMatchStatus.SUGGESTED,
                        CompetitorItemMatchStatus.NEEDS_REVIEW,
                        CompetitorItemMatchStatus.AMBIGUOUS,
                    )
                ),
                CompetitorItemMatch.method != CompetitorItemMatchMethod.MANUAL,
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    rejected = 0
    for match in matches:
        item = match.competitor_item
        product = match.product
        if not item or not product:
            continue
        item_type = _effective_item_type(item)
        if item_type not in COLOR_SENSITIVE_ITEM_TYPES:
            continue
        competitor_colors = _competitor_part_colors(item)
        if not _part_color_conflict(product, competitor_colors):
            continue
        product_colors = _product_part_colors(product)
        rationale = dict(match.rationale_json or {})
        rationale["auto_reject_part_color_conflict"] = {
            "reason": "part_color_conflict",
            "item_type": item_type,
            "competitor_colors": sorted(competitor_colors),
            "product_colors": sorted(product_colors),
            "rejected_at": now.isoformat(),
        }
        match.status = CompetitorItemMatchStatus.REJECTED
        match.rationale_json = rationale
        match.updated_at = now
        session.add(match)
        rejected += 1
    return rejected


def _auto_reject_part_quality_conflicts(session: Session) -> int:
    matches = (
        session.execute(
            select(CompetitorItemMatch)
            .options(
                joinedload(CompetitorItemMatch.competitor_item),
                joinedload(CompetitorItemMatch.product),
            )
            .where(
                CompetitorItemMatch.status.in_(
                    (
                        CompetitorItemMatchStatus.SUGGESTED,
                        CompetitorItemMatchStatus.NEEDS_REVIEW,
                        CompetitorItemMatchStatus.AMBIGUOUS,
                    )
                ),
                CompetitorItemMatch.method != CompetitorItemMatchMethod.MANUAL,
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    rejected = 0
    for match in matches:
        item = match.competitor_item
        product = match.product
        if not item or not product:
            continue
        item_type = _effective_item_type(item)
        if item_type not in COLOR_SENSITIVE_ITEM_TYPES:
            continue
        competitor_quality = _competitor_part_quality_tier(item)
        if not _part_quality_conflict(product, competitor_quality):
            continue
        product_quality = _product_part_quality_tier(product)
        rationale = dict(match.rationale_json or {})
        rationale["auto_reject_part_quality_conflict"] = {
            "reason": "part_quality_conflict",
            "item_type": item_type,
            "competitor_quality": competitor_quality,
            "product_quality": product_quality,
            "rejected_at": now.isoformat(),
        }
        match.status = CompetitorItemMatchStatus.REJECTED
        match.rationale_json = rationale
        match.updated_at = now
        session.add(match)
        rejected += 1
    return rejected


def _auto_reject_part_assembly_conflicts(session: Session) -> int:
    matches = (
        session.execute(
            select(CompetitorItemMatch)
            .options(
                joinedload(CompetitorItemMatch.competitor_item),
                joinedload(CompetitorItemMatch.product),
            )
            .where(
                CompetitorItemMatch.status.in_(
                    (
                        CompetitorItemMatchStatus.SUGGESTED,
                        CompetitorItemMatchStatus.NEEDS_REVIEW,
                        CompetitorItemMatchStatus.AMBIGUOUS,
                    )
                ),
                CompetitorItemMatch.method != CompetitorItemMatchMethod.MANUAL,
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    rejected = 0
    for match in matches:
        item = match.competitor_item
        product = match.product
        if not item or not product or _effective_item_type(item) != "housing":
            continue
        reason = _part_assembly_conflict_reason(item, product)
        if not reason:
            continue
        rationale = dict(match.rationale_json or {})
        rationale["auto_reject_part_assembly_conflict"] = {
            "reason": reason,
            "item_type": "housing",
            "rejected_at": now.isoformat(),
        }
        match.status = CompetitorItemMatchStatus.REJECTED
        match.rationale_json = rationale
        match.updated_at = now
        session.add(match)
        rejected += 1
    return rejected


def _auto_reject_other_family_conflicts(session: Session) -> int:
    matches = (
        session.execute(
            select(CompetitorItemMatch)
            .options(
                joinedload(CompetitorItemMatch.competitor_item),
                joinedload(CompetitorItemMatch.product),
            )
            .where(
                CompetitorItemMatch.status.in_(
                    (
                        CompetitorItemMatchStatus.SUGGESTED,
                        CompetitorItemMatchStatus.NEEDS_REVIEW,
                        CompetitorItemMatchStatus.AMBIGUOUS,
                    )
                ),
                CompetitorItemMatch.method != CompetitorItemMatchMethod.MANUAL,
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    rejected = 0
    for match in matches:
        item = match.competitor_item
        product = match.product
        if not item or not product or _effective_item_type(item) != "other":
            continue
        details = _other_family_conflict_details(item, product)
        if not details:
            continue
        rationale = dict(match.rationale_json or {})
        rationale["auto_reject_other_family_conflict"] = {
            "rejected_at": now.isoformat(),
            **details,
        }
        match.status = CompetitorItemMatchStatus.REJECTED
        match.rationale_json = rationale
        match.updated_at = now
        session.add(match)
        rejected += 1
    return rejected


def _auto_reject_housing_part_kind_conflicts(session: Session) -> int:
    matches = (
        session.execute(
            select(CompetitorItemMatch)
            .options(
                joinedload(CompetitorItemMatch.competitor_item),
                joinedload(CompetitorItemMatch.product),
            )
            .where(
                CompetitorItemMatch.status.in_(
                    (
                        CompetitorItemMatchStatus.SUGGESTED,
                        CompetitorItemMatchStatus.NEEDS_REVIEW,
                        CompetitorItemMatchStatus.AMBIGUOUS,
                    )
                ),
                CompetitorItemMatch.method != CompetitorItemMatchMethod.MANUAL,
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    rejected = 0
    for match in matches:
        item = match.competitor_item
        product = match.product
        if not item or not product or _effective_item_type(item) != "housing":
            continue
        competitor_kind = _competitor_housing_part_kind(item)
        if not _housing_part_kind_conflict(product, competitor_kind):
            continue
        rationale = dict(match.rationale_json or {})
        rationale["auto_reject_housing_part_kind_conflict"] = {
            "reason": "housing_part_kind_conflict",
            "competitor_kind": competitor_kind,
            "product_kind": _product_housing_part_kind(product),
            "rejected_at": now.isoformat(),
        }
        match.status = CompetitorItemMatchStatus.REJECTED
        match.rationale_json = rationale
        match.updated_at = now
        session.add(match)
        rejected += 1
    return rejected


def _auto_reject_housing_device_code_conflicts(session: Session) -> int:
    matches = (
        session.execute(
            select(CompetitorItemMatch)
            .options(
                joinedload(CompetitorItemMatch.competitor_item),
                joinedload(CompetitorItemMatch.product),
            )
            .where(
                CompetitorItemMatch.status.in_(
                    (
                        CompetitorItemMatchStatus.SUGGESTED,
                        CompetitorItemMatchStatus.NEEDS_REVIEW,
                        CompetitorItemMatchStatus.AMBIGUOUS,
                    )
                ),
                CompetitorItemMatch.method != CompetitorItemMatchMethod.MANUAL,
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    rejected = 0
    for match in matches:
        item = match.competitor_item
        product = match.product
        if not item or not product or _effective_item_type(item) != "housing":
            continue
        if not _housing_device_code_conflict(item, product):
            continue
        rationale = dict(match.rationale_json or {})
        rationale["auto_reject_housing_device_code_conflict"] = {
            "reason": "housing_device_code_conflict",
            "competitor_codes": sorted(_extract_device_codes(_competitor_device_code_text(item))),
            "product_codes": sorted(_extract_device_codes(product.name)),
            "rejected_at": now.isoformat(),
        }
        match.status = CompetitorItemMatchStatus.REJECTED
        match.rationale_json = rationale
        match.updated_at = now
        session.add(match)
        rejected += 1
    return rejected


def _auto_reject_camera_position_conflicts(session: Session) -> int:
    matches = (
        session.execute(
            select(CompetitorItemMatch)
            .options(
                joinedload(CompetitorItemMatch.competitor_item),
                joinedload(CompetitorItemMatch.product),
            )
            .where(
                CompetitorItemMatch.status.in_(
                    (
                        CompetitorItemMatchStatus.SUGGESTED,
                        CompetitorItemMatchStatus.NEEDS_REVIEW,
                        CompetitorItemMatchStatus.AMBIGUOUS,
                    )
                ),
                CompetitorItemMatch.method != CompetitorItemMatchMethod.MANUAL,
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    rejected = 0
    for match in matches:
        item = match.competitor_item
        product = match.product
        if not item or not product or _effective_item_type(item) != "camera":
            continue
        competitor_position = _competitor_camera_position(item)
        if not _camera_position_conflict(product, competitor_position):
            continue
        rationale = dict(match.rationale_json or {})
        rationale["auto_reject_camera_position_conflict"] = {
            "reason": "camera_position_conflict",
            "competitor_position": competitor_position,
            "product_position": _product_camera_position(product),
            "rejected_at": now.isoformat(),
        }
        match.status = CompetitorItemMatchStatus.REJECTED
        match.rationale_json = rationale
        match.updated_at = now
        session.add(match)
        rejected += 1
    return rejected


def _auto_reject_flex_role_conflicts(session: Session) -> int:
    matches = (
        session.execute(
            select(CompetitorItemMatch)
            .options(
                joinedload(CompetitorItemMatch.competitor_item),
                joinedload(CompetitorItemMatch.product),
            )
            .where(
                CompetitorItemMatch.status.in_(
                    (
                        CompetitorItemMatchStatus.SUGGESTED,
                        CompetitorItemMatchStatus.NEEDS_REVIEW,
                        CompetitorItemMatchStatus.AMBIGUOUS,
                    )
                ),
                CompetitorItemMatch.method != CompetitorItemMatchMethod.MANUAL,
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    rejected = 0
    for match in matches:
        item = match.competitor_item
        product = match.product
        if not item or not product or _effective_item_type(item) != "flex":
            continue
        details = _flex_conflict_details(item, product)
        if not details:
            continue
        rationale = dict(match.rationale_json or {})
        rationale["auto_reject_flex_role_conflict"] = {
            "rejected_at": now.isoformat(),
            **details,
        }
        match.status = CompetitorItemMatchStatus.REJECTED
        match.rationale_json = rationale
        match.updated_at = now
        session.add(match)
        rejected += 1
    return rejected


def _auto_reject_battery_part_code_conflicts(session: Session) -> int:
    matches = (
        session.execute(
            select(CompetitorItemMatch)
            .options(
                joinedload(CompetitorItemMatch.competitor_item),
                joinedload(CompetitorItemMatch.product),
            )
            .where(
                CompetitorItemMatch.status.in_(
                    (
                        CompetitorItemMatchStatus.SUGGESTED,
                        CompetitorItemMatchStatus.NEEDS_REVIEW,
                        CompetitorItemMatchStatus.AMBIGUOUS,
                    )
                ),
                CompetitorItemMatch.method != CompetitorItemMatchMethod.MANUAL,
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    rejected = 0
    for match in matches:
        item = match.competitor_item
        product = match.product
        if not item or not product or _effective_item_type(item) != "battery":
            continue
        competitor_codes = _competitor_battery_part_codes(item)
        if not _battery_part_code_conflict(product, competitor_codes):
            continue
        rationale = dict(match.rationale_json or {})
        rationale["auto_reject_battery_part_code_conflict"] = {
            "reason": "battery_part_code_conflict",
            "competitor_codes": sorted(competitor_codes),
            "product_codes": sorted(_product_battery_part_codes(product)),
            "rejected_at": now.isoformat(),
        }
        match.status = CompetitorItemMatchStatus.REJECTED
        match.rationale_json = rationale
        match.updated_at = now
        session.add(match)
        rejected += 1
    return rejected


def _auto_reject_battery_subject_conflicts(session: Session) -> int:
    matches = (
        session.execute(
            select(CompetitorItemMatch)
            .options(
                joinedload(CompetitorItemMatch.competitor_item),
                joinedload(CompetitorItemMatch.product),
            )
            .where(
                CompetitorItemMatch.status.in_(
                    (
                        CompetitorItemMatchStatus.SUGGESTED,
                        CompetitorItemMatchStatus.NEEDS_REVIEW,
                        CompetitorItemMatchStatus.AMBIGUOUS,
                    )
                ),
                CompetitorItemMatch.method != CompetitorItemMatchMethod.MANUAL,
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    rejected = 0
    for match in matches:
        item = match.competitor_item
        product = match.product
        if not item or not product:
            continue
        reason = _battery_subject_conflict_reason(item, product)
        if not reason:
            continue
        rationale = dict(match.rationale_json or {})
        rationale["auto_reject_battery_subject_conflict"] = {
            "reason": reason,
            "rejected_at": now.isoformat(),
        }
        match.status = CompetitorItemMatchStatus.REJECTED
        match.rationale_json = rationale
        match.updated_at = now
        session.add(match)
        rejected += 1
    return rejected


def _auto_reject_display_long_model_code_conflicts(session: Session) -> int:
    matches = (
        session.execute(
            select(CompetitorItemMatch)
            .options(
                joinedload(CompetitorItemMatch.competitor_item),
                joinedload(CompetitorItemMatch.product),
            )
            .where(
                CompetitorItemMatch.status.in_(
                    (
                        CompetitorItemMatchStatus.SUGGESTED,
                        CompetitorItemMatchStatus.NEEDS_REVIEW,
                        CompetitorItemMatchStatus.AMBIGUOUS,
                    )
                ),
                CompetitorItemMatch.method != CompetitorItemMatchMethod.MANUAL,
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    rejected = 0
    for match in matches:
        item = match.competitor_item
        product = match.product
        if not item or not product or _effective_item_type(item) != "display":
            continue
        competitor_codes = _long_device_codes(
            _extract_device_codes(_competitor_device_code_text(item))
        )
        product_codes = _long_device_codes(_extract_device_codes(product.name))
        if (
            not competitor_codes
            or not product_codes
            or not competitor_codes.isdisjoint(product_codes)
        ):
            continue
        if not _display_model_code_blocks(item, product):
            continue
        rationale = dict(match.rationale_json or {})
        rationale["auto_reject_display_long_model_code_conflict"] = {
            "reason": "display_long_model_code_conflict",
            "competitor_codes": sorted(competitor_codes),
            "product_codes": sorted(product_codes),
            "rejected_at": now.isoformat(),
        }
        match.status = CompetitorItemMatchStatus.REJECTED
        match.rationale_json = rationale
        match.updated_at = now
        session.add(match)
        rejected += 1
    return rejected


def _auto_reject_display_text_model_conflicts(session: Session) -> int:
    matches = (
        session.execute(
            select(CompetitorItemMatch)
            .options(
                joinedload(CompetitorItemMatch.competitor_item),
                joinedload(CompetitorItemMatch.product),
            )
            .where(
                CompetitorItemMatch.status.in_(
                    (
                        CompetitorItemMatchStatus.SUGGESTED,
                        CompetitorItemMatchStatus.NEEDS_REVIEW,
                        CompetitorItemMatchStatus.AMBIGUOUS,
                    )
                ),
                CompetitorItemMatch.method != CompetitorItemMatchMethod.MANUAL,
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    rejected = 0
    for match in matches:
        item = match.competitor_item
        product = match.product
        if not item or not product or _effective_item_type(item) != "display":
            continue
        if not _display_text_model_conflict(item, product):
            continue
        rationale = dict(match.rationale_json or {})
        rationale["auto_reject_display_text_model_conflict"] = {
            "reason": "display_text_model_conflict",
            "rejected_at": now.isoformat(),
        }
        match.status = CompetitorItemMatchStatus.REJECTED
        match.rationale_json = rationale
        match.updated_at = now
        session.add(match)
        rejected += 1
    return rejected


def _auto_reject_display_module_component_conflicts(session: Session) -> int:
    matches = (
        session.execute(
            select(CompetitorItemMatch)
            .options(
                joinedload(CompetitorItemMatch.competitor_item),
                joinedload(CompetitorItemMatch.product),
            )
            .where(
                CompetitorItemMatch.status.in_(
                    (
                        CompetitorItemMatchStatus.SUGGESTED,
                        CompetitorItemMatchStatus.NEEDS_REVIEW,
                        CompetitorItemMatchStatus.AMBIGUOUS,
                    )
                ),
                CompetitorItemMatch.method != CompetitorItemMatchMethod.MANUAL,
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    rejected = 0
    for match in matches:
        item = match.competitor_item
        product = match.product
        if not item or not product:
            continue
        guardrail = basic_candidate_guardrails(item, product)
        if guardrail.allowed or guardrail.reason != "display_module_component_conflict":
            continue
        rationale = dict(match.rationale_json or {})
        rationale["auto_reject_display_module_component_conflict"] = {
            "reason": guardrail.reason,
            "rejected_at": now.isoformat(),
        }
        match.status = CompetitorItemMatchStatus.REJECTED
        match.rationale_json = rationale
        match.updated_at = now
        session.add(match)
        rejected += 1
    return rejected


def _auto_reject_laptop_matrix_flex_conflicts(session: Session) -> int:
    matches = (
        session.execute(
            select(CompetitorItemMatch)
            .options(
                joinedload(CompetitorItemMatch.competitor_item),
                joinedload(CompetitorItemMatch.product),
            )
            .where(
                CompetitorItemMatch.status.in_(
                    (
                        CompetitorItemMatchStatus.SUGGESTED,
                        CompetitorItemMatchStatus.NEEDS_REVIEW,
                        CompetitorItemMatchStatus.AMBIGUOUS,
                    )
                ),
                CompetitorItemMatch.method != CompetitorItemMatchMethod.MANUAL,
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    rejected = 0
    for match in matches:
        item = match.competitor_item
        product = match.product
        if not item or not product:
            continue
        item_text = _combined_item_text(item)
        product_text = _combined_product_text(product)
        if not _is_laptop_matrix_flex(item_text) or _is_laptop_matrix_flex(product_text):
            continue
        rationale = dict(match.rationale_json or {})
        rationale["auto_reject_laptop_matrix_flex_conflict"] = {
            "reason": "laptop_matrix_flex_vs_other_product",
            "rejected_at": now.isoformat(),
        }
        match.status = CompetitorItemMatchStatus.REJECTED
        match.rationale_json = rationale
        match.updated_at = now
        session.add(match)
        rejected += 1
    return rejected


def _auto_reject_display_matrix_tag_conflicts(session: Session) -> int:
    matches = (
        session.execute(
            select(CompetitorItemMatch)
            .options(
                joinedload(CompetitorItemMatch.competitor_item),
                joinedload(CompetitorItemMatch.product),
            )
            .where(
                CompetitorItemMatch.status.in_(
                    (
                        CompetitorItemMatchStatus.SUGGESTED,
                        CompetitorItemMatchStatus.NEEDS_REVIEW,
                        CompetitorItemMatchStatus.AMBIGUOUS,
                    )
                ),
                CompetitorItemMatch.method != CompetitorItemMatchMethod.MANUAL,
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    rejected = 0
    for match in matches:
        item = match.competitor_item
        product = match.product
        if not item or not product or _effective_item_type(item) != "display":
            continue
        competitor_tags = _competitor_display_matrix_vendor_tags(item)
        product_tags = _product_display_matrix_vendor_tags(product)
        if not competitor_tags or not product_tags or not competitor_tags.isdisjoint(product_tags):
            continue
        rationale = dict(match.rationale_json or {})
        rationale["auto_reject_display_matrix_tag_conflict"] = {
            "reason": "display_matrix_tag_conflict",
            "competitor_matrix_tags": sorted(competitor_tags),
            "product_matrix_tags": sorted(product_tags),
            "rejected_at": now.isoformat(),
        }
        match.status = CompetitorItemMatchStatus.REJECTED
        match.rationale_json = rationale
        match.updated_at = now
        session.add(match)
        rejected += 1
    return rejected


def _auto_reject_display_condition_conflicts(session: Session) -> int:
    matches = (
        session.execute(
            select(CompetitorItemMatch)
            .options(
                joinedload(CompetitorItemMatch.competitor_item),
                joinedload(CompetitorItemMatch.product),
            )
            .where(
                CompetitorItemMatch.status.in_(
                    (
                        CompetitorItemMatchStatus.SUGGESTED,
                        CompetitorItemMatchStatus.NEEDS_REVIEW,
                        CompetitorItemMatchStatus.AMBIGUOUS,
                    )
                ),
                CompetitorItemMatch.method != CompetitorItemMatchMethod.MANUAL,
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    rejected = 0
    for match in matches:
        item = match.competitor_item
        product = match.product
        if not item or not product or _effective_item_type(item) != "display":
            continue
        reason = _display_condition_conflict_reason(item, product)
        if not reason:
            continue
        rationale = dict(match.rationale_json or {})
        rationale["auto_reject_display_condition_conflict"] = {
            "reason": reason,
            "rejected_at": now.isoformat(),
        }
        match.status = CompetitorItemMatchStatus.REJECTED
        match.rationale_json = rationale
        match.updated_at = now
        session.add(match)
        rejected += 1
    return rejected


def _auto_reject_housing_condition_conflicts(session: Session) -> int:
    matches = (
        session.execute(
            select(CompetitorItemMatch)
            .options(
                joinedload(CompetitorItemMatch.competitor_item),
                joinedload(CompetitorItemMatch.product),
            )
            .where(
                CompetitorItemMatch.status.in_(
                    (
                        CompetitorItemMatchStatus.SUGGESTED,
                        CompetitorItemMatchStatus.NEEDS_REVIEW,
                        CompetitorItemMatchStatus.AMBIGUOUS,
                    )
                ),
                CompetitorItemMatch.method != CompetitorItemMatchMethod.MANUAL,
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    rejected = 0
    for match in matches:
        item = match.competitor_item
        product = match.product
        if not item or not product or _effective_item_type(item) != "housing":
            continue
        reason = _housing_condition_conflict_reason(item, product)
        if not reason:
            continue
        rationale = dict(match.rationale_json or {})
        rationale["auto_reject_housing_condition_conflict"] = {
            "reason": reason,
            "rejected_at": now.isoformat(),
        }
        match.status = CompetitorItemMatchStatus.REJECTED
        match.rationale_json = rationale
        match.updated_at = now
        session.add(match)
        rejected += 1
    return rejected


def _auto_reject_non_display_model_code_conflicts(session: Session) -> int:
    matches = (
        session.execute(
            select(CompetitorItemMatch)
            .options(
                joinedload(CompetitorItemMatch.competitor_item),
                joinedload(CompetitorItemMatch.product),
            )
            .where(
                CompetitorItemMatch.status.in_(
                    (
                        CompetitorItemMatchStatus.SUGGESTED,
                        CompetitorItemMatchStatus.NEEDS_REVIEW,
                        CompetitorItemMatchStatus.AMBIGUOUS,
                    )
                ),
                CompetitorItemMatch.method != CompetitorItemMatchMethod.MANUAL,
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    rejected = 0
    for match in matches:
        item = match.competitor_item
        product = match.product
        if not item or not product:
            continue
        item_type = _effective_item_type(item)
        if item_type == "display":
            continue
        competitor_codes = _extract_accessory_model_codes(_combined_item_text(item))
        product_codes = _extract_accessory_model_codes(_combined_product_text(product))
        if not competitor_codes or not product_codes:
            continue
        if not competitor_codes.isdisjoint(product_codes):
            continue
        competitor_keys = _extract_device_model_keys(_combined_item_text(item))
        product_keys = _extract_device_model_keys(_combined_product_text(product))
        if _device_model_keys_overlap(competitor_keys, product_keys):
            continue
        rationale = dict(match.rationale_json or {})
        rationale["auto_reject_non_display_model_code_conflict"] = {
            "reason": "non_display_model_code_conflict",
            "competitor_codes": sorted(competitor_codes),
            "product_codes": sorted(product_codes),
            "rejected_at": now.isoformat(),
        }
        match.status = CompetitorItemMatchStatus.REJECTED
        match.rationale_json = rationale
        match.updated_at = now
        session.add(match)
        rejected += 1
    return rejected


def _auto_reject_guardrail_device_group_conflicts(session: Session) -> int:
    matches = (
        session.execute(
            select(CompetitorItemMatch)
            .options(
                joinedload(CompetitorItemMatch.competitor_item),
                joinedload(CompetitorItemMatch.product),
            )
            .where(
                CompetitorItemMatch.status.in_(
                    (
                        CompetitorItemMatchStatus.SUGGESTED,
                        CompetitorItemMatchStatus.NEEDS_REVIEW,
                        CompetitorItemMatchStatus.AMBIGUOUS,
                    )
                ),
                CompetitorItemMatch.method != CompetitorItemMatchMethod.MANUAL,
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    rejected = 0
    for match in matches:
        item = match.competitor_item
        product = match.product
        if not item or not product:
            continue
        guardrail = basic_candidate_guardrails(item, product)
        if guardrail.allowed or guardrail.reason != "device_group_conflict":
            continue
        rationale = dict(match.rationale_json or {})
        rationale["auto_reject_guardrail_conflict"] = {
            "reason": guardrail.reason,
            "rejected_at": now.isoformat(),
        }
        match.status = CompetitorItemMatchStatus.REJECTED
        match.rationale_json = rationale
        match.updated_at = now
        session.add(match)
        rejected += 1
    return rejected


def _auto_reject_guardrail_catalog_family_conflicts(session: Session) -> int:
    matches = (
        session.execute(
            select(CompetitorItemMatch)
            .options(
                joinedload(CompetitorItemMatch.competitor_item),
                joinedload(CompetitorItemMatch.product),
            )
            .where(
                CompetitorItemMatch.status.in_(
                    (
                        CompetitorItemMatchStatus.SUGGESTED,
                        CompetitorItemMatchStatus.NEEDS_REVIEW,
                        CompetitorItemMatchStatus.AMBIGUOUS,
                    )
                ),
                CompetitorItemMatch.method != CompetitorItemMatchMethod.MANUAL,
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    rejected = 0
    for match in matches:
        item = match.competitor_item
        product = match.product
        if not item or not product:
            continue
        guardrail = basic_candidate_guardrails(item, product)
        if guardrail.allowed or guardrail.reason != "catalog_family_conflict":
            continue
        rationale = dict(match.rationale_json or {})
        rationale["auto_reject_guardrail_catalog_family_conflict"] = {
            "reason": guardrail.reason,
            "rejected_at": now.isoformat(),
        }
        match.status = CompetitorItemMatchStatus.REJECTED
        match.rationale_json = rationale
        match.updated_at = now
        session.add(match)
        rejected += 1
    return rejected


def _auto_reject_display_attribute_conflicts(session: Session) -> int:
    matches = (
        session.execute(
            select(CompetitorItemMatch)
            .options(
                joinedload(CompetitorItemMatch.competitor_item),
                joinedload(CompetitorItemMatch.product),
            )
            .where(
                CompetitorItemMatch.status.in_(
                    (
                        CompetitorItemMatchStatus.SUGGESTED,
                        CompetitorItemMatchStatus.NEEDS_REVIEW,
                        CompetitorItemMatchStatus.AMBIGUOUS,
                    )
                ),
                CompetitorItemMatch.method != CompetitorItemMatchMethod.MANUAL,
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    rejected = 0
    for match in matches:
        item = match.competitor_item
        product = match.product
        if not item or not product:
            continue
        reason = _explicit_display_attribute_conflict_reason(item, product)
        if not reason:
            continue
        rationale = dict(match.rationale_json or {})
        rationale["auto_reject_display_attribute_conflict"] = {
            "reason": reason,
            "competitor_quality": _competitor_display_quality(item),
            "competitor_construction": _competitor_display_construction(item),
            "product_quality": _product_display_quality(product),
            "rejected_at": now.isoformat(),
        }
        match.status = CompetitorItemMatchStatus.REJECTED
        match.rationale_json = rationale
        match.updated_at = now
        session.add(match)
        rejected += 1
    return rejected


def _auto_accept_explicit_model_text_matches(session: Session, *, min_score: float) -> int:
    matches = (
        session.execute(
            select(CompetitorItemMatch)
            .options(
                joinedload(CompetitorItemMatch.competitor_item),
                joinedload(CompetitorItemMatch.product),
            )
            .where(
                CompetitorItemMatch.status.in_(
                    (
                        CompetitorItemMatchStatus.SUGGESTED,
                        CompetitorItemMatchStatus.NEEDS_REVIEW,
                        CompetitorItemMatchStatus.AMBIGUOUS,
                    )
                ),
                CompetitorItemMatch.method != CompetitorItemMatchMethod.MANUAL,
                CompetitorItemMatch.final_score >= min_score,
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    accepted = 0
    for match in matches:
        item = match.competitor_item
        product = match.product
        if not item or not product:
            continue
        item_type = _effective_item_type(item)
        model_label = _safe_xiaomi_regional_model_auto_accept(
            item,
            product,
            item_type=item_type,
            score=float(match.final_score or 0),
            min_score=min_score,
        )
        if not model_label:
            continue
        _ensure_model_text_compatibility(session, item, product, model_label)
        rationale = dict(match.rationale_json or {})
        rationale["auto_accept_explicit_model_text"] = {
            "reason": "same_xiaomi_model_with_regional_codes",
            "model": model_label,
            "min_score": min_score,
            "accepted_at": now.isoformat(),
        }
        match.status = CompetitorItemMatchStatus.ACCEPTED
        match.rationale_json = rationale
        match.updated_at = now
        session.add(match)
        accepted += 1
    return accepted


def _auto_accept_battery_original_part_code_matches(session: Session, *, min_score: float) -> int:
    matches = (
        session.execute(
            select(CompetitorItemMatch)
            .options(
                joinedload(CompetitorItemMatch.competitor_item),
                joinedload(CompetitorItemMatch.product),
            )
            .where(
                CompetitorItemMatch.status.in_(
                    (
                        CompetitorItemMatchStatus.SUGGESTED,
                        CompetitorItemMatchStatus.NEEDS_REVIEW,
                        CompetitorItemMatchStatus.AMBIGUOUS,
                    )
                ),
                CompetitorItemMatch.method != CompetitorItemMatchMethod.MANUAL,
                CompetitorItemMatch.final_score >= min_score,
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    accepted = 0
    for match in matches:
        item = match.competitor_item
        product = match.product
        if not item or not product or _effective_item_type(item) != "battery":
            continue
        if not _safe_battery_original_part_code_auto_accept(
            item,
            product,
            score=float(match.final_score or 0),
            min_score=min_score,
        ):
            continue
        competitor_codes = _competitor_battery_part_codes(item)
        product_codes = _product_battery_part_codes(product)
        rationale = dict(match.rationale_json or {})
        rationale["auto_accept_battery_original_part_code"] = {
            "reason": "moba_original_battery_part_code_and_model_overlap",
            "min_score": min_score,
            "accepted_at": now.isoformat(),
            "competitor_codes": sorted(competitor_codes),
            "product_codes": sorted(product_codes),
            "overlap_codes": sorted(competitor_codes & product_codes),
            "overlap_model_keys": sorted(
                _extract_device_model_keys(_competitor_device_model_text(item)).intersection(
                    _extract_device_model_keys(product.name)
                )
            ),
        }
        match.status = CompetitorItemMatchStatus.ACCEPTED
        match.rationale_json = rationale
        match.updated_at = now
        session.add(match)
        accepted += 1
    return accepted


def _auto_accept_battery_part_code_matches(session: Session, *, min_score: float) -> int:
    query_min_score = min(min_score, 0.75)
    matches = (
        session.execute(
            select(CompetitorItemMatch)
            .options(
                joinedload(CompetitorItemMatch.competitor_item),
                joinedload(CompetitorItemMatch.product),
            )
            .where(
                CompetitorItemMatch.status.in_(
                    (
                        CompetitorItemMatchStatus.SUGGESTED,
                        CompetitorItemMatchStatus.NEEDS_REVIEW,
                        CompetitorItemMatchStatus.AMBIGUOUS,
                    )
                ),
                CompetitorItemMatch.method != CompetitorItemMatchMethod.MANUAL,
                CompetitorItemMatch.final_score >= query_min_score,
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    accepted = 0
    for match in matches:
        item = match.competitor_item
        product = match.product
        if not item or not product or _effective_item_type(item) != "battery":
            continue
        if not _safe_battery_part_code_auto_accept(
            item,
            product,
            score=float(match.final_score or 0),
            min_score=min_score,
        ):
            continue
        competitor_codes = _competitor_battery_part_codes(item)
        product_codes = _product_battery_part_codes(product)
        reason = (
            "battery_part_code_and_model_overlap"
            if product_codes
            else "battery_part_code_with_product_model_overlap"
        )
        rationale = dict(match.rationale_json or {})
        rationale["auto_accept_battery_part_code"] = {
            "reason": reason,
            "min_score": min_score,
            "query_min_score": query_min_score,
            "accepted_at": now.isoformat(),
            "competitor_codes": sorted(competitor_codes),
            "product_codes": sorted(product_codes),
            "overlap_codes": sorted(competitor_codes & product_codes),
            "overlap_model_keys": sorted(
                _extract_device_model_keys(_competitor_device_model_text(item)).intersection(
                    _extract_device_model_keys(product.name)
                )
            ),
        }
        match.status = CompetitorItemMatchStatus.ACCEPTED
        match.rationale_json = rationale
        match.updated_at = now
        session.add(match)
        accepted += 1
    return accepted


def _auto_accept_iphone_battery_capacity_matches(session: Session, *, min_score: float) -> int:
    query_min_score = min(min_score, 0.68)
    matches = (
        session.execute(
            select(CompetitorItemMatch)
            .options(
                joinedload(CompetitorItemMatch.competitor_item),
                joinedload(CompetitorItemMatch.product),
            )
            .where(
                CompetitorItemMatch.status.in_(
                    (
                        CompetitorItemMatchStatus.SUGGESTED,
                        CompetitorItemMatchStatus.NEEDS_REVIEW,
                        CompetitorItemMatchStatus.AMBIGUOUS,
                    )
                ),
                CompetitorItemMatch.method != CompetitorItemMatchMethod.MANUAL,
                CompetitorItemMatch.final_score >= query_min_score,
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    accepted = 0
    for match in matches:
        item = match.competitor_item
        product = match.product
        if not item or not product or _effective_item_type(item) != "battery":
            continue
        if not _safe_iphone_battery_capacity_auto_accept(
            item,
            product,
            score=float(match.final_score or 0),
        ):
            continue
        item_text = " ".join(filter(None, [item.name, item.normalized_title, item.external_id]))
        product_text = product.name or ""
        rationale = dict(match.rationale_json or {})
        rationale["auto_accept_iphone_battery_capacity"] = {
            "reason": "iphone_battery_model_capacity_and_enhanced_product_signal",
            "min_score": min_score,
            "query_min_score": query_min_score,
            "accepted_at": now.isoformat(),
            "competitor_capacity": _extract_capacity(item_text),
            "product_capacity": _extract_capacity(product_text),
            "overlap_model_keys": sorted(
                _extract_device_model_keys(_competitor_device_model_text(item)).intersection(
                    _extract_device_model_keys(product_text)
                )
            ),
        }
        match.status = CompetitorItemMatchStatus.ACCEPTED
        match.rationale_json = rationale
        match.updated_at = now
        session.add(match)
        accepted += 1
    return accepted


def _auto_accept_housing_part_matches(session: Session, *, min_score: float) -> int:
    query_min_score = min(min_score, 0.75)
    matches = (
        session.execute(
            select(CompetitorItemMatch)
            .options(
                joinedload(CompetitorItemMatch.competitor_item),
                joinedload(CompetitorItemMatch.product),
            )
            .where(
                CompetitorItemMatch.status.in_(
                    (
                        CompetitorItemMatchStatus.SUGGESTED,
                        CompetitorItemMatchStatus.NEEDS_REVIEW,
                        CompetitorItemMatchStatus.AMBIGUOUS,
                    )
                ),
                CompetitorItemMatch.method != CompetitorItemMatchMethod.MANUAL,
                CompetitorItemMatch.final_score >= query_min_score,
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    accepted = 0
    for match in matches:
        item = match.competitor_item
        product = match.product
        if not item or not product or _effective_item_type(item) != "housing":
            continue
        if not _safe_housing_part_auto_accept(
            item,
            product,
            score=float(match.final_score or 0),
            min_score=min_score,
        ):
            continue
        item_text = " ".join(filter(None, [item.name, item.normalized_title, item.external_id]))
        product_text = product.name or ""
        rationale = dict(match.rationale_json or {})
        rationale["auto_accept_housing_part"] = {
            "reason": "housing_part_model_or_code_color_kind_match",
            "min_score": min_score,
            "query_min_score": query_min_score,
            "accepted_at": now.isoformat(),
            "kind": _competitor_housing_part_kind(item),
            "product_kind": _product_housing_part_kind(product),
            "overlap_model_keys": sorted(
                _extract_device_model_keys(item_text).intersection(
                    _extract_device_model_keys(product_text)
                )
            ),
            "overlap_codes": sorted(
                _extract_device_codes(item_text).intersection(_extract_device_codes(product_text))
            ),
            "competitor_colors": sorted(_first_color_values(item.name, item.normalized_title)),
            "product_colors": sorted(_first_color_values(product.name, product.color)),
        }
        match.status = CompetitorItemMatchStatus.ACCEPTED
        match.rationale_json = rationale
        match.updated_at = now
        session.add(match)
        accepted += 1
    return accepted


def _auto_accept_flex_matches(session: Session, *, min_score: float) -> int:
    query_min_score = min(min_score, 0.74)
    matches = (
        session.execute(
            select(CompetitorItemMatch)
            .options(
                joinedload(CompetitorItemMatch.competitor_item),
                joinedload(CompetitorItemMatch.product),
            )
            .where(
                CompetitorItemMatch.status.in_(
                    (
                        CompetitorItemMatchStatus.SUGGESTED,
                        CompetitorItemMatchStatus.NEEDS_REVIEW,
                        CompetitorItemMatchStatus.AMBIGUOUS,
                    )
                ),
                CompetitorItemMatch.method != CompetitorItemMatchMethod.MANUAL,
                CompetitorItemMatch.final_score >= query_min_score,
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    accepted = 0
    for match in matches:
        item = match.competitor_item
        product = match.product
        if not item or not product or _effective_item_type(item) != "flex":
            continue
        score = float(match.final_score or 0)
        lower_board_accept = _safe_lower_board_flex_auto_accept(
            item,
            product,
            score=score,
            min_score=min_score,
        )
        if not lower_board_accept and not _safe_flex_auto_accept(
            item,
            product,
            score=score,
            min_score=min_score,
        ):
            continue
        item_text = " ".join(filter(None, [item.name, item.normalized_title, item.external_id]))
        product_text = product.name or ""
        rationale = dict(match.rationale_json or {})
        reason = (
            "lower_board_charge_flex_model_or_code_match"
            if lower_board_accept
            else "flex_role_model_or_code_color_match"
        )
        rationale["auto_accept_flex"] = {
            "reason": reason,
            "min_score": min_score,
            "accepted_at": now.isoformat(),
            "role": _competitor_flex_role(item),
            "product_role": _product_flex_role(product),
            "lower_board_charge_part": lower_board_accept,
            "overlap_model_keys": sorted(
                _extract_device_model_keys(item_text).intersection(
                    _extract_device_model_keys(product_text)
                )
            ),
            "overlap_codes": sorted(
                _extract_device_codes(item_text).intersection(_extract_device_codes(product_text))
            ),
            "competitor_colors": sorted(_first_color_values(item.name, item.normalized_title)),
            "product_colors": sorted(_first_color_values(product.name, product.color)),
        }
        match.status = CompetitorItemMatchStatus.ACCEPTED
        match.rationale_json = rationale
        match.updated_at = now
        session.add(match)
        accepted += 1
    return accepted


def _auto_accept_camera_matches(session: Session, *, min_score: float) -> int:
    query_min_score = min(min_score, 0.80)
    matches = (
        session.execute(
            select(CompetitorItemMatch)
            .options(
                joinedload(CompetitorItemMatch.competitor_item),
                joinedload(CompetitorItemMatch.product),
            )
            .where(
                CompetitorItemMatch.status.in_(
                    (
                        CompetitorItemMatchStatus.SUGGESTED,
                        CompetitorItemMatchStatus.NEEDS_REVIEW,
                        CompetitorItemMatchStatus.AMBIGUOUS,
                    )
                ),
                CompetitorItemMatch.method != CompetitorItemMatchMethod.MANUAL,
                CompetitorItemMatch.final_score >= query_min_score,
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    accepted = 0
    for match in matches:
        item = match.competitor_item
        product = match.product
        if not item or not product or _effective_item_type(item) != "camera":
            continue
        if not _safe_camera_suggest(item, product, score=float(match.final_score or 0)):
            continue
        item_text = " ".join(filter(None, [item.name, item.normalized_title, item.external_id]))
        product_text = product.name or ""
        rationale = dict(match.rationale_json or {})
        rationale["auto_accept_camera"] = {
            "reason": "camera_position_model_match",
            "min_score": min_score,
            "accepted_at": now.isoformat(),
            "competitor_position": _competitor_camera_position(item),
            "product_position": _product_camera_position(product),
            "overlap_model_keys": sorted(
                _extract_device_model_keys(item_text).intersection(
                    _extract_device_model_keys(product_text)
                )
            ),
            "iphone_se_exact_overlap": _iphone_se_exact_model_overlap(item_text, product_text),
        }
        match.status = CompetitorItemMatchStatus.ACCEPTED
        match.rationale_json = rationale
        match.updated_at = now
        session.add(match)
        accepted += 1
    return accepted


def _auto_accept_connector_matches(session: Session, *, min_score: float) -> int:
    query_min_score = min(min_score, 0.74)
    matches = (
        session.execute(
            select(CompetitorItemMatch)
            .options(
                joinedload(CompetitorItemMatch.competitor_item),
                joinedload(CompetitorItemMatch.product),
            )
            .where(
                CompetitorItemMatch.status.in_(
                    (
                        CompetitorItemMatchStatus.SUGGESTED,
                        CompetitorItemMatchStatus.NEEDS_REVIEW,
                        CompetitorItemMatchStatus.AMBIGUOUS,
                    )
                ),
                CompetitorItemMatch.method != CompetitorItemMatchMethod.MANUAL,
                CompetitorItemMatch.final_score >= query_min_score,
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    accepted = 0
    for match in matches:
        item = match.competitor_item
        product = match.product
        if not item or not product or _effective_item_type(item) != "connector":
            continue
        if not _safe_connector_suggest(item, product, score=float(match.final_score or 0)):
            continue
        item_text = " ".join(filter(None, [item.name, item.normalized_title, item.external_id]))
        product_text = product.name or ""
        rationale = dict(match.rationale_json or {})
        rationale["auto_accept_connector"] = {
            "reason": "connector_port_model_or_code_match",
            "min_score": min_score,
            "accepted_at": now.isoformat(),
            "competitor_port": _extract_port_type(item_text),
            "product_port": _extract_port_type(product_text),
            "overlap_model_keys": sorted(
                _extract_device_model_keys(item_text).intersection(
                    _extract_device_model_keys(product_text)
                )
            ),
            "overlap_codes": sorted(
                _extract_device_codes(item_text).intersection(_extract_device_codes(product_text))
            ),
        }
        match.status = CompetitorItemMatchStatus.ACCEPTED
        match.rationale_json = rationale
        match.updated_at = now
        session.add(match)
        accepted += 1
    return accepted


def _safe_other_family_auto_accept_details(
    item: CompetitorItem,
    product: Product,
    *,
    score: float,
) -> dict[str, Any] | None:
    item_text = " ".join(filter(None, [item.name, item.normalized_title, item.external_id]))
    product_text = product.name or ""
    family = catalog_family(item_text)
    product_family = catalog_family(product_text)
    common = {
        "family": family,
        "product_family": product_family,
    }

    if _safe_disposable_battery_suggest(item, product, score=score):
        return {
            "reason": "disposable_battery_brand_size_pack_match",
            **common,
            "competitor_brand": _disposable_battery_brand(item_text),
            "product_brand": _disposable_battery_brand(product_text),
            "competitor_size": _disposable_battery_size(item_text),
            "product_size": _disposable_battery_size(product_text),
            "competitor_pack_count": _disposable_battery_pack_count(item_text),
            "product_pack_count": _disposable_battery_pack_count(product_text),
        }

    if _safe_phone_camera_glass_suggest(item, product, score=score):
        item_frame = _camera_glass_frame_state(item_text)
        product_frame = _camera_glass_frame_state(product_text)
        if "with_frame" in {item_frame, product_frame} and item_frame != product_frame:
            return None
        return {
            "reason": "phone_camera_glass_family_model_color_frame_match",
            **common,
            "overlap_model_keys": sorted(
                _extract_device_model_keys(item_text).intersection(
                    _extract_device_model_keys(product_text)
                )
            ),
            "overlap_codes": sorted(
                _extract_device_codes(item_text).intersection(_extract_device_codes(product_text))
            ),
            "competitor_colors": sorted(_first_color_values(item.name, item.normalized_title)),
            "product_colors": sorted(_first_color_values(product.name, product.color)),
            "competitor_frame": item_frame,
            "product_frame": product_frame,
        }

    if _safe_phone_sim_tray_suggest(item, product, score=score):
        return {
            "reason": "phone_sim_tray_family_model_or_code_color_match",
            **common,
            "overlap_model_keys": sorted(
                _extract_device_model_keys(item_text).intersection(
                    _extract_device_model_keys(product_text)
                )
            ),
            "overlap_codes": sorted(
                _extract_device_codes(item_text).intersection(_extract_device_codes(product_text))
            ),
            "competitor_colors": sorted(_first_color_values(item.name, item.normalized_title)),
            "product_colors": sorted(_first_color_values(product.name, product.color)),
        }

    if _safe_phone_speaker_suggest(item, product, score=score):
        return {
            "reason": "phone_speaker_model_or_code_match",
            **common,
            "overlap_model_keys": sorted(
                _extract_device_model_keys(item_text).intersection(
                    _extract_device_model_keys(product_text)
                )
            ),
            "overlap_codes": sorted(
                _extract_device_codes(item_text).intersection(_extract_device_codes(product_text))
            ),
        }

    if _safe_touchscreen_suggest(item, product, score=score):
        return {
            "reason": "touchscreen_model_color_match",
            **common,
            "overlap_model_keys": sorted(
                _extract_device_model_keys(item_text).intersection(
                    _extract_device_model_keys(product_text)
                )
            ),
            "competitor_colors": sorted(_first_color_values(item.name, item.normalized_title)),
            "product_colors": sorted(_first_color_values(product.name, product.color)),
        }

    if _safe_module_glass_oca_suggest(item, product, score=score):
        return {
            "reason": "module_glass_oca_model_or_code_color_match",
            **common,
            "overlap_model_keys": sorted(
                _extract_device_model_keys(item_text).intersection(
                    _extract_device_model_keys(product_text)
                )
            ),
            "overlap_codes": sorted(
                _extract_device_codes(item_text).intersection(_extract_device_codes(product_text))
            ),
            "competitor_colors": sorted(_first_color_values(item.name, item.normalized_title)),
            "product_colors": sorted(_first_color_values(product.name, product.color)),
        }

    if _safe_screen_protector_suggest(item, product, score=score):
        return {
            "reason": "screen_protector_family_model_or_code_color_match",
            **common,
            "overlap_model_keys": sorted(
                _extract_device_model_keys(item_text).intersection(
                    _extract_device_model_keys(product_text)
                )
            ),
            "overlap_codes": sorted(
                _extract_device_codes(item_text).intersection(_extract_device_codes(product_text))
            ),
            "competitor_colors": sorted(_first_color_values(item.name, item.normalized_title)),
            "product_colors": sorted(_first_color_values(product.name, product.color)),
        }

    if _safe_network_cable_suggest(item, product, score=score):
        return {
            "reason": "network_cable_model_and_length_match",
            **common,
            "competitor_model": _network_cable_model(item_text),
            "product_model": _network_cable_model(product_text),
            "competitor_length_m": _cable_length_meters(item_text),
            "product_length_m": _cable_length_meters(product_text),
        }

    if _safe_charging_station_suggest(item, product, score=score):
        return {
            "reason": "charging_station_exact_model_match",
            **common,
            "competitor_model": _charging_station_model(item_text),
            "product_model": _charging_station_model(product_text),
        }

    if _safe_middle_frame_suggest(item, product, score=score):
        return {
            "reason": "middle_frame_model_or_code_color_match",
            **common,
            "overlap_model_keys": sorted(
                _extract_device_model_keys(item_text).intersection(
                    _extract_device_model_keys(product_text)
                )
            ),
            "overlap_codes": sorted(
                _extract_device_codes(item_text).intersection(_extract_device_codes(product_text))
            ),
            "competitor_colors": sorted(_first_color_values(item.name, item.normalized_title)),
            "product_colors": sorted(_first_color_values(product.name, product.color)),
        }

    if _safe_magsafe_power_adapter_suggest(item, product, score=score):
        return {
            "reason": "magsafe_power_adapter_wattage_match",
            **common,
            "competitor_watts": _magsafe_power_watts(item_text),
            "product_watts": _magsafe_power_watts(product_text),
            "competitor_generation": _magsafe_generation(item_text),
            "product_generation": _magsafe_generation(product_text),
        }

    if _safe_adhesive_suggest(item, product, score=score):
        return {
            "reason": "adhesive_model_and_volume_match",
            **common,
            "competitor_model": _adhesive_model(item_text),
            "product_model": _adhesive_model(product_text),
            "competitor_volume_ml": _volume_ml(item_text),
            "product_volume_ml": _volume_ml(product_text),
        }

    if _safe_steam_deck_screen_protector_suggest(item, product, score=score):
        return {
            "reason": "steam_deck_screen_protector_match",
            **common,
        }

    if _safe_stencil_suggest(item, product, score=score):
        return {
            "reason": "stencil_family_chipset_or_series_overlap",
            **common,
            "overlap_tokens": sorted(
                _stencil_signature_tokens(item_text).intersection(
                    _stencil_signature_tokens(product_text)
                )
            ),
        }

    return None


def _auto_accept_other_safe_family_matches(session: Session, *, min_score: float) -> int:
    query_min_score = min(min_score, 0.60)
    matches = (
        session.execute(
            select(CompetitorItemMatch)
            .options(
                joinedload(CompetitorItemMatch.competitor_item),
                joinedload(CompetitorItemMatch.product),
            )
            .where(
                CompetitorItemMatch.status.in_(
                    (
                        CompetitorItemMatchStatus.SUGGESTED,
                        CompetitorItemMatchStatus.NEEDS_REVIEW,
                        CompetitorItemMatchStatus.AMBIGUOUS,
                    )
                ),
                CompetitorItemMatch.method != CompetitorItemMatchMethod.MANUAL,
                CompetitorItemMatch.final_score >= query_min_score,
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    accepted = 0
    for match in matches:
        item = match.competitor_item
        product = match.product
        if not item or not product:
            continue
        if _effective_item_type(item) not in {"other", "cable"}:
            continue
        details = _safe_other_family_auto_accept_details(
            item,
            product,
            score=float(match.final_score or 0),
        )
        if not details:
            continue
        rationale = dict(match.rationale_json or {})
        rationale["auto_accept_other_safe_family"] = {
            "min_score": min_score,
            "query_min_score": query_min_score,
            "accepted_at": now.isoformat(),
            **details,
        }
        match.status = CompetitorItemMatchStatus.ACCEPTED
        match.rationale_json = rationale
        match.updated_at = now
        session.add(match)
        accepted += 1
    return accepted


def _auto_accept_display_original_quality_matches(session: Session, *, min_score: float) -> int:
    query_min_score = min(min_score, 0.75)
    matches = (
        session.execute(
            select(CompetitorItemMatch)
            .options(
                joinedload(CompetitorItemMatch.competitor_item),
                joinedload(CompetitorItemMatch.product),
            )
            .where(
                CompetitorItemMatch.status.in_(
                    (
                        CompetitorItemMatchStatus.SUGGESTED,
                        CompetitorItemMatchStatus.NEEDS_REVIEW,
                        CompetitorItemMatchStatus.AMBIGUOUS,
                    )
                ),
                CompetitorItemMatch.method != CompetitorItemMatchMethod.MANUAL,
                CompetitorItemMatch.final_score >= query_min_score,
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    accepted = 0
    for match in matches:
        item = match.competitor_item
        product = match.product
        if not item or not product:
            continue
        item_type = _effective_item_type(item)
        reason = _safe_display_original_quality_auto_accept(
            item,
            product,
            item_type=item_type,
            score=float(match.final_score or 0),
            min_score=min_score,
        )
        if not reason:
            continue
        details = _display_exact_model_evidence_details(item, product)
        _ensure_code_overlap_compatibilities(session, item, product, details)
        _ensure_display_model_key_compatibilities(session, item, product, details)
        rationale = dict(match.rationale_json or {})
        rationale["auto_accept_display_original_quality"] = {
            "reason": reason,
            "competitor_quality": _competitor_display_quality(item),
            "product_quality": _product_display_quality(product),
            "min_score": min_score,
            "query_min_score": query_min_score,
            "accepted_at": now.isoformat(),
            **details,
        }
        match.status = CompetitorItemMatchStatus.ACCEPTED
        match.rationale_json = rationale
        match.updated_at = now
        session.add(match)
        accepted += 1
    return accepted


def _auto_accept_display_unspecified_quality_matches(session: Session, *, min_score: float) -> int:
    query_min_score = max(0.83, min_score)
    matches = (
        session.execute(
            select(CompetitorItemMatch)
            .options(
                joinedload(CompetitorItemMatch.competitor_item),
                joinedload(CompetitorItemMatch.product),
            )
            .where(
                CompetitorItemMatch.status.in_(
                    (
                        CompetitorItemMatchStatus.SUGGESTED,
                        CompetitorItemMatchStatus.NEEDS_REVIEW,
                        CompetitorItemMatchStatus.AMBIGUOUS,
                    )
                ),
                CompetitorItemMatch.method != CompetitorItemMatchMethod.MANUAL,
                CompetitorItemMatch.final_score >= query_min_score,
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    accepted = 0
    for match in matches:
        item = match.competitor_item
        product = match.product
        if not item or not product:
            continue
        item_type = _effective_item_type(item)
        reason = _safe_display_unspecified_quality_auto_accept(
            item,
            product,
            item_type=item_type,
            score=float(match.final_score or 0),
        )
        if not reason:
            continue
        details = _display_exact_model_evidence_details(item, product)
        _ensure_code_overlap_compatibilities(session, item, product, details)
        _ensure_display_model_key_compatibilities(session, item, product, details)
        rationale = dict(match.rationale_json or {})
        rationale["auto_accept_display_unspecified_quality"] = {
            "reason": reason,
            "competitor_quality": _competitor_display_quality(item),
            "product_quality": _product_display_quality(product),
            "min_score": query_min_score,
            "accepted_at": now.isoformat(),
            **details,
        }
        match.status = CompetitorItemMatchStatus.ACCEPTED
        match.rationale_json = rationale
        match.updated_at = now
        session.add(match)
        accepted += 1
    return accepted


def _auto_accept_display_construction_matches(session: Session, *, min_score: float) -> int:
    matches = (
        session.execute(
            select(CompetitorItemMatch)
            .options(
                joinedload(CompetitorItemMatch.competitor_item),
                joinedload(CompetitorItemMatch.product),
            )
            .where(
                CompetitorItemMatch.status.in_(
                    (
                        CompetitorItemMatchStatus.SUGGESTED,
                        CompetitorItemMatchStatus.NEEDS_REVIEW,
                        CompetitorItemMatchStatus.AMBIGUOUS,
                    )
                ),
                CompetitorItemMatch.method != CompetitorItemMatchMethod.MANUAL,
                CompetitorItemMatch.final_score >= min_score,
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    accepted = 0
    for match in matches:
        item = match.competitor_item
        product = match.product
        if not item or not product:
            continue
        item_type = _effective_item_type(item)
        reason = _safe_display_copy_construction_auto_accept(
            item,
            product,
            item_type=item_type,
            score=float(match.final_score or 0),
            min_score=min_score,
        )
        if not reason:
            continue
        details = _display_exact_model_evidence_details(item, product)
        _ensure_code_overlap_compatibilities(session, item, product, details)
        _ensure_display_model_key_compatibilities(session, item, product, details)
        rationale = dict(match.rationale_json or {})
        rationale["auto_accept_display_construction"] = {
            "reason": reason,
            "competitor_construction": _competitor_display_construction(item),
            "product_construction": _product_display_construction(product),
            "product_quality": _product_display_quality(product),
            "min_score": min_score,
            "accepted_at": now.isoformat(),
            **details,
        }
        match.status = CompetitorItemMatchStatus.ACCEPTED
        match.rationale_json = rationale
        match.updated_at = now
        session.add(match)
        accepted += 1
    return accepted


def _auto_accept_display_matrix_tag_matches(session: Session, *, min_score: float) -> int:
    matches = (
        session.execute(
            select(CompetitorItemMatch)
            .options(
                joinedload(CompetitorItemMatch.competitor_item),
                joinedload(CompetitorItemMatch.product),
            )
            .where(
                CompetitorItemMatch.status.in_(
                    (
                        CompetitorItemMatchStatus.SUGGESTED,
                        CompetitorItemMatchStatus.NEEDS_REVIEW,
                        CompetitorItemMatchStatus.AMBIGUOUS,
                    )
                ),
                CompetitorItemMatch.method != CompetitorItemMatchMethod.MANUAL,
                CompetitorItemMatch.final_score >= min_score,
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    accepted = 0
    for match in matches:
        item = match.competitor_item
        product = match.product
        if not item or not product:
            continue
        item_type = _effective_item_type(item)
        reason = _safe_display_matrix_tag_auto_accept(
            item,
            product,
            item_type=item_type,
            score=float(match.final_score or 0),
            min_score=min_score,
        )
        if not reason:
            continue
        details = _display_exact_model_evidence_details(item, product)
        _ensure_code_overlap_compatibilities(session, item, product, details)
        _ensure_display_model_key_compatibilities(session, item, product, details)
        competitor_tags = sorted(_competitor_display_matrix_tags(item))
        product_tags = sorted(_product_display_matrix_tags(product))
        rationale = dict(match.rationale_json or {})
        rationale["auto_accept_display_matrix_tag"] = {
            "reason": reason,
            "competitor_matrix_tags": competitor_tags,
            "product_matrix_tags": product_tags,
            "overlap_matrix_tags": sorted(set(competitor_tags).intersection(product_tags)),
            "min_score": min_score,
            "accepted_at": now.isoformat(),
            **details,
        }
        match.status = CompetitorItemMatchStatus.ACCEPTED
        match.rationale_json = rationale
        match.updated_at = now
        session.add(match)
        accepted += 1
    return accepted


def _auto_accept_display_matrix_type_matches(session: Session, *, min_score: float) -> int:
    matches = (
        session.execute(
            select(CompetitorItemMatch)
            .options(
                joinedload(CompetitorItemMatch.competitor_item),
                joinedload(CompetitorItemMatch.product),
            )
            .where(
                CompetitorItemMatch.status.in_(
                    (
                        CompetitorItemMatchStatus.SUGGESTED,
                        CompetitorItemMatchStatus.NEEDS_REVIEW,
                        CompetitorItemMatchStatus.AMBIGUOUS,
                    )
                ),
                CompetitorItemMatch.method != CompetitorItemMatchMethod.MANUAL,
                CompetitorItemMatch.final_score >= min_score,
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    accepted = 0
    for match in matches:
        item = match.competitor_item
        product = match.product
        if not item or not product:
            continue
        item_type = _effective_item_type(item)
        reason = _safe_display_copy_matrix_type_auto_accept(
            item,
            product,
            item_type=item_type,
            score=float(match.final_score or 0),
            min_score=min_score,
        )
        if not reason:
            continue
        details = _display_exact_model_evidence_details(item, product)
        _ensure_code_overlap_compatibilities(session, item, product, details)
        _ensure_display_model_key_compatibilities(session, item, product, details)
        rationale = dict(match.rationale_json or {})
        rationale["auto_accept_display_matrix_type"] = {
            "reason": reason,
            "competitor_display_type": _competitor_display_type(item),
            "product_display_type": _product_display_type(product),
            "product_quality": _product_display_quality(product),
            "min_score": min_score,
            "accepted_at": now.isoformat(),
            **details,
        }
        match.status = CompetitorItemMatchStatus.ACCEPTED
        match.rationale_json = rationale
        match.updated_at = now
        session.add(match)
        accepted += 1
    return accepted


def _auto_accept_explicit_code_overlap_matches(session: Session, *, min_score: float) -> int:
    matches = (
        session.execute(
            select(CompetitorItemMatch)
            .options(
                joinedload(CompetitorItemMatch.competitor_item),
                joinedload(CompetitorItemMatch.product),
            )
            .where(
                CompetitorItemMatch.status.in_(
                    (
                        CompetitorItemMatchStatus.SUGGESTED,
                        CompetitorItemMatchStatus.NEEDS_REVIEW,
                    )
                ),
                CompetitorItemMatch.method != CompetitorItemMatchMethod.MANUAL,
                CompetitorItemMatch.final_score >= min_score,
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    accepted = 0
    for match in matches:
        item = match.competitor_item
        product = match.product
        if not item or not product:
            continue
        item_type = _effective_item_type(item)
        if not _safe_explicit_code_overlap_auto_accept(
            item,
            product,
            item_type=item_type,
            score=float(match.final_score or 0),
            min_score=min_score,
        ):
            continue
        details = _display_model_code_overlap_details(item, product)
        _ensure_code_overlap_compatibilities(session, item, product, details)
        rationale = dict(match.rationale_json or {})
        rationale["auto_accept_explicit_model_code_overlap"] = {
            "reason": "screen_or_touch_part_with_shared_device_code",
            "min_score": min_score,
            "accepted_at": now.isoformat(),
            **details,
        }
        match.status = CompetitorItemMatchStatus.ACCEPTED
        match.rationale_json = rationale
        match.updated_at = now
        session.add(match)
        accepted += 1
    return accepted


def _backfill_explicit_code_overlap_compatibilities(session: Session) -> int:
    matches = (
        session.execute(
            select(CompetitorItemMatch)
            .options(
                joinedload(CompetitorItemMatch.competitor_item),
                joinedload(CompetitorItemMatch.product),
            )
            .where(
                CompetitorItemMatch.status == CompetitorItemMatchStatus.ACCEPTED,
                CompetitorItemMatch.rationale_json[
                    "auto_accept_explicit_model_code_overlap"
                ].is_not(None),
            )
        )
        .scalars()
        .all()
    )
    created = 0
    for match in matches:
        item = match.competitor_item
        product = match.product
        if not item or not product:
            continue
        details = _display_model_code_overlap_details(item, product)
        created += _ensure_code_overlap_compatibilities(session, item, product, details)
    return created


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
    normalized = re.sub(r"(?<=\w)\+(?=\s|$)", " plus", normalized)
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
            rf"\btecno\s+{line}\s+(\d{{1,2}}[a-z]?)"
            rf"(?:\s+(lite|pro(?:\s+plus)?|max|plus|premier|neo))?"
            rf"(?:\s+(4g|5g))?\b",
            normalized,
        ):
            model, variant, network = match.groups()
            base = f"tecno_{line}_{model}"
            if variant:
                base = f"{base}_{variant.replace(' ', '_')}"
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

    for line in ("hot", "smart"):
        for match in re.finditer(
            rf"\binfinix\s+{line}\s+(\d{{1,2}}[a-z]?)"
            rf"(?:\s+(lite|pro|max|plus|ultra))?"
            rf"(?:\s+(4g|5g))?\b",
            normalized,
        ):
            model, variant, network = match.groups()
            base = f"infinix_{line}_{model}"
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

    for match in re.finditer(
        r"\b(?:zte\s+)?(?:nubia\s+)?red\s+magic\s+(\d{1,2}s?)"
        r"(?:\s+(lite|pro|max|plus|ultra))?(?:\s+(4g|5g))?\b",
        normalized,
    ):
        model, variant, network = match.groups()
        base = f"zte_nubia_red_magic_{model}"
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
        r"\bgoogle\s+pixel\s+(\d{1,2}[a-z]?)(?:\s+(pro\s+xl|lite|pro|max|plus|xl|fold))?\b",
        normalized,
    ):
        model, variant = match.groups()
        base = f"google_pixel_{model}"
        if variant:
            base = f"{base}_{variant.replace(' ', '_')}"
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
        r"\biqoo\s+neo\s+(\d{1,3}[a-z]?)(?:\s+(lite|pro|max|plus|ultra|se))?(?:\s+(4g|5g))?\b",
        normalized,
    ):
        model, variant, network = match.groups()
        base = f"vivo_iqoo_neo_{model}"
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
        r"\boneplus\s+(?!(?:nord|ace)\b)([a-z]?\d{1,2}[a-z]?|x)([rt])?(?:\s+(lite|pro|max|plus|ultra))?\b",
        normalized,
    ):
        model, suffix, variant = match.groups()
        base = f"oneplus_{model}{suffix or ''}"
        if variant:
            base = f"{base}_{variant}"
        keys.add(base)

    for match in re.finditer(
        r"\boneplus\s+nord\s+(?:ce\s*)?(\d{1,2})([rt])?(?:\s+(lite|pro|max|plus|ultra))?\b",
        normalized,
    ):
        model, suffix, variant = match.groups()
        prefix = "oneplus_nord_ce" if "nord ce" in match.group(0) else "oneplus_nord"
        base = f"{prefix}_{model}{suffix or ''}"
        if variant:
            base = f"{base}_{variant}"
        keys.add(base)

    for match in re.finditer(
        r"\bmotorola\s+(?:moto\s+)?([cegw]\d{1,3}|x\s+play)(?:\s+(lite|pro|max|plus))?\b",
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

    if re.search(r"\biphone\s+air\b", normalized):
        keys.add("iphone_air")

    apple_watch_prefix = r"\b(?:apple\s+)?watch\s+"
    for match in re.finditer(
        apple_watch_prefix + r"(ultra\s+\d|se\s+\d{4}|se|\d{1,2})"
        r"(?:\s*/?\s*se)?"
        r"(?:\s+(\d{2})\s*(?:mm|мм))?\b",
        normalized,
    ):
        model, size = match.groups()
        model_key = model.replace(" ", "_")
        if model_key == "se" and re.search(r"\b\d{4}\b", normalized):
            continue
        key = f"apple_watch_{model_key}"
        if size:
            key = f"{key}_{size}mm"
        keys.add(key)

    for match in re.finditer(
        apple_watch_prefix + r"(\d{1,2})\s*/?\s*se" r"(?:\s+(\d{2})\s*(?:mm|мм))?\b",
        normalized,
    ):
        model, size = match.groups()
        key = f"apple_watch_{model}"
        if size:
            key = f"{key}_{size}mm"
        keys.add(key)

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


def _json_has_product_id(content: str) -> bool:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return False
    return isinstance(parsed, dict) and "product_id" in parsed


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
    competitor_item_ids: list[int] | None = None,
    auto_accept_unique: bool = False,
    auto_accept_code_overlap: bool = True,
    auto_accept_min_score: float = 0.80,
    live_embed_missing: bool = True,
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
    if competitor_item_ids:
        query = query.where(CompetitorItem.id.in_(competitor_item_ids))
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

    llm_client: FallbackChatClient | None = None
    if use_llm_arbiter:
        llm_client = FallbackChatClient.from_env(timeout=30.0)
        if not llm_client.has_providers:
            logging.warning("LLM arbiter requested but no local/OpenAI providers are configured")
            llm_client = None
        else:
            logging.info("LLM arbiter fallback enabled: providers=%s", llm_client.provider_names)

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
            if not live_embed_missing:
                stats["skipped_no_embedding"] += 1
                continue
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
        competitor_part_colors = (
            _competitor_part_colors(item) if item_type in COLOR_SENSITIVE_ITEM_TYPES else set()
        )
        competitor_part_quality = (
            _competitor_part_quality_tier(item) if item_type in COLOR_SENSITIVE_ITEM_TYPES else None
        )
        competitor_housing_part_kind = (
            _competitor_housing_part_kind(item) if item_type == "housing" else None
        )
        competitor_camera_position = (
            _competitor_camera_position(item) if item_type == "camera" else None
        )
        competitor_flex_role = _competitor_flex_role(item) if item_type == "flex" else None
        competitor_battery_part_codes = (
            _competitor_battery_part_codes(item) if item_type == "battery" else set()
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
                if not _basic_or_display_exact_model_guardrails_allowed(
                    item,
                    prod,
                    item_type=item_type,
                ):
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
            if not _basic_or_display_exact_model_guardrails_allowed(
                item,
                prod,
                item_type=item_type,
            ):
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
            if _phone_sim_tray_model_or_code_conflict(item, prod):
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
            if item_type == "display" and _explicit_display_subject_conflict_reason(
                item,
                prod,
                item_type=item_type,
            ):
                continue
            if item_type == "display" and _explicit_display_attribute_conflict_reason(
                item,
                prod,
                competitor_display_quality,
            ):
                continue
            if item_type == "display" and _display_quality_conflict(
                prod, competitor_display_quality
            ):
                continue
            if item_type == "display" and _display_quality_requires_review(
                prod, competitor_display_quality, item
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
            if item_type == "battery" and _battery_part_code_conflict(
                prod,
                competitor_battery_part_codes,
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
            if item_type in COLOR_SENSITIVE_ITEM_TYPES and _part_color_conflict(
                prod,
                competitor_part_colors,
            ):
                continue
            if item_type in COLOR_SENSITIVE_ITEM_TYPES and _part_quality_conflict(
                prod,
                competitor_part_quality,
            ):
                continue
            if item_type == "housing" and _part_assembly_conflict_reason(item, prod):
                continue
            if item_type == "housing" and _housing_part_kind_conflict(
                prod,
                competitor_housing_part_kind,
            ):
                continue
            if item_type == "housing" and _housing_device_code_conflict(item, prod):
                continue
            if item_type == "camera" and _camera_position_conflict(
                prod,
                competitor_camera_position,
            ):
                continue
            if item_type == "flex" and _flex_role_conflict(prod, competitor_flex_role):
                continue
            if item_type == "flex" and _flex_fingerprint_conflict(item, prod):
                continue
            if item_type == "flex" and _flex_button_control_conflict(item, prod):
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
        code_overlap_auto_accept = False
        battery_part_code_model_suggest = False
        battery_verification_suggest = False
        iphone_battery_model_capacity_suggest = False
        disposable_battery_suggest = False
        phone_camera_glass_suggest = False
        screen_protector_suggest = False
        phone_sim_tray_suggest = False
        network_cable_suggest = False
        housing_part_suggest = False
        flex_suggest = False
        stencil_suggest = False
        if best_score < min_embed_score:
            status = CompetitorItemMatchStatus.NEEDS_REVIEW
        elif not item_type:
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
            best_product = products.get(best_pid)
            if (
                auto_accept_code_overlap
                and best_product
                and _safe_explicit_code_overlap_auto_accept(
                    item,
                    best_product,
                    item_type=item_type,
                    score=best_score,
                    min_score=auto_accept_min_score,
                )
            ):
                status = CompetitorItemMatchStatus.ACCEPTED
                code_overlap_auto_accept = True
            elif (
                item_type == "battery"
                and best_product
                and _safe_battery_part_code_model_suggest(
                    item,
                    best_product,
                    filtered_count=len(filtered),
                    score=best_score,
                )
            ):
                status = CompetitorItemMatchStatus.SUGGESTED
                battery_part_code_model_suggest = True
            else:
                status = CompetitorItemMatchStatus.NEEDS_REVIEW

        if status == CompetitorItemMatchStatus.AMBIGUOUS and item_type == "battery":
            best_product = products.get(best_pid)
            if best_product and _safe_battery_verification_suggest(
                item,
                best_product,
                score=best_score,
            ):
                status = CompetitorItemMatchStatus.SUGGESTED
                battery_verification_suggest = True

        if (
            status
            in {
                CompetitorItemMatchStatus.AMBIGUOUS,
                CompetitorItemMatchStatus.NEEDS_REVIEW,
            }
            and item_type == "battery"
        ):
            best_product = products.get(best_pid)
            if best_product and _safe_battery_part_code_model_suggest(
                item,
                best_product,
                filtered_count=len(filtered),
                score=best_score,
            ):
                status = CompetitorItemMatchStatus.SUGGESTED
                battery_part_code_model_suggest = True

        if (
            status
            in {
                CompetitorItemMatchStatus.AMBIGUOUS,
                CompetitorItemMatchStatus.NEEDS_REVIEW,
            }
            and item_type == "battery"
        ):
            best_product = products.get(best_pid)
            if best_product and _safe_iphone_battery_model_capacity_suggest(
                item,
                best_product,
                score=best_score,
            ):
                status = CompetitorItemMatchStatus.SUGGESTED
                iphone_battery_model_capacity_suggest = True

        if status in {
            CompetitorItemMatchStatus.AMBIGUOUS,
            CompetitorItemMatchStatus.NEEDS_REVIEW,
        }:
            best_product = products.get(best_pid)
            if best_product and _safe_disposable_battery_suggest(
                item,
                best_product,
                score=best_score,
            ):
                status = CompetitorItemMatchStatus.SUGGESTED
                disposable_battery_suggest = True

        if status in {
            CompetitorItemMatchStatus.AMBIGUOUS,
            CompetitorItemMatchStatus.NEEDS_REVIEW,
        }:
            best_product = products.get(best_pid)
            if best_product and _safe_phone_camera_glass_suggest(
                item,
                best_product,
                score=best_score,
            ):
                status = CompetitorItemMatchStatus.SUGGESTED
                phone_camera_glass_suggest = True

        if status in {
            CompetitorItemMatchStatus.AMBIGUOUS,
            CompetitorItemMatchStatus.NEEDS_REVIEW,
        }:
            best_product = products.get(best_pid)
            if best_product and _safe_screen_protector_suggest(
                item,
                best_product,
                score=best_score,
            ):
                status = CompetitorItemMatchStatus.SUGGESTED
                screen_protector_suggest = True

        if status in {
            CompetitorItemMatchStatus.AMBIGUOUS,
            CompetitorItemMatchStatus.NEEDS_REVIEW,
        }:
            best_product = products.get(best_pid)
            if best_product and _safe_phone_sim_tray_suggest(
                item,
                best_product,
                score=best_score,
            ):
                status = CompetitorItemMatchStatus.SUGGESTED
                phone_sim_tray_suggest = True

        if status in {
            CompetitorItemMatchStatus.AMBIGUOUS,
            CompetitorItemMatchStatus.NEEDS_REVIEW,
        }:
            best_product = products.get(best_pid)
            if best_product and _safe_network_cable_suggest(
                item,
                best_product,
                score=best_score,
            ):
                status = CompetitorItemMatchStatus.SUGGESTED
                network_cable_suggest = True

        if (
            status
            in {
                CompetitorItemMatchStatus.AMBIGUOUS,
                CompetitorItemMatchStatus.NEEDS_REVIEW,
            }
            and item_type == "housing"
        ):
            best_product = products.get(best_pid)
            if best_product and _safe_housing_part_suggest(
                item,
                best_product,
                score=best_score,
            ):
                status = CompetitorItemMatchStatus.SUGGESTED
                housing_part_suggest = True

        if (
            status
            in {
                CompetitorItemMatchStatus.AMBIGUOUS,
                CompetitorItemMatchStatus.NEEDS_REVIEW,
            }
            and item_type == "flex"
        ):
            best_product = products.get(best_pid)
            if best_product and _safe_flex_suggest(
                item,
                best_product,
                score=best_score,
            ):
                status = CompetitorItemMatchStatus.SUGGESTED
                flex_suggest = True

        if status in {
            CompetitorItemMatchStatus.AMBIGUOUS,
            CompetitorItemMatchStatus.NEEDS_REVIEW,
        }:
            best_product = products.get(best_pid)
            if best_product and _safe_stencil_suggest(
                item,
                best_product,
                score=best_score,
            ):
                status = CompetitorItemMatchStatus.SUGGESTED
                stencil_suggest = True

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
        if code_overlap_auto_accept:
            best_product = products.get(best_pid)
            rationale["auto_accept_explicit_model_code_overlap"] = {
                "reason": "screen_or_touch_part_with_shared_device_code",
                "min_score": auto_accept_min_score,
                **(
                    _display_model_code_overlap_details(item, best_product)
                    if best_product
                    else {
                        "competitor_codes": [],
                        "product_codes": [],
                        "overlap_codes": [],
                    }
                ),
            }
        if battery_part_code_model_suggest:
            best_product = products.get(best_pid)
            rationale["battery_part_code_model_suggest"] = {
                "reason": "battery_candidate_with_part_code_and_model_overlap",
                "competitor_codes": sorted(_competitor_battery_part_codes(item)),
                "product_codes": sorted(_product_battery_part_codes(best_product or Product())),
                "overlap_model_keys": sorted(
                    _extract_device_model_keys(_competitor_device_model_text(item)).intersection(
                        _extract_device_model_keys(best_product.name if best_product else None)
                    )
                ),
            }
        if battery_verification_suggest:
            best_product = products.get(best_pid)
            rationale["battery_verification_suggest"] = {
                "reason": "battery_verification_signal_with_system_diagnosable_model_overlap",
                "overlap_model_keys": sorted(
                    _extract_device_model_keys(_competitor_device_model_text(item)).intersection(
                        _extract_device_model_keys(best_product.name if best_product else None)
                    )
                ),
            }
        if iphone_battery_model_capacity_suggest:
            best_product = products.get(best_pid)
            item_text = " ".join(filter(None, [item.name, item.normalized_title, item.external_id]))
            product_text = best_product.name if best_product else None
            rationale["iphone_battery_model_capacity_suggest"] = {
                "reason": "iphone_battery_model_overlap_without_capacity_conflict",
                "competitor_capacity": _extract_capacity(item_text),
                "product_capacity": _extract_capacity(product_text),
                "overlap_model_keys": sorted(
                    _extract_device_model_keys(_competitor_device_model_text(item)).intersection(
                        _extract_device_model_keys(product_text)
                    )
                ),
            }
        if disposable_battery_suggest:
            best_product = products.get(best_pid)
            item_text = " ".join(filter(None, [item.name, item.normalized_title, item.external_id]))
            product_text = best_product.name if best_product else None
            rationale["disposable_battery_suggest"] = {
                "reason": "disposable_battery_brand_size_pack_count_match",
                "brand": _disposable_battery_brand(item_text),
                "size": _disposable_battery_size(item_text),
                "pack_count": _disposable_battery_pack_count(item_text),
                "product_brand": _disposable_battery_brand(product_text),
                "product_size": _disposable_battery_size(product_text),
                "product_pack_count": _disposable_battery_pack_count(product_text),
            }
        if phone_camera_glass_suggest:
            best_product = products.get(best_pid)
            item_text = " ".join(filter(None, [item.name, item.normalized_title, item.external_id]))
            product_text = best_product.name if best_product else None
            rationale["phone_camera_glass_suggest"] = {
                "reason": "phone_camera_glass_family_model_color_frame_match",
                "overlap_model_keys": sorted(
                    _extract_device_model_keys(item_text).intersection(
                        _extract_device_model_keys(product_text)
                    )
                ),
                "competitor_colors": sorted(_first_color_values(item.name, item.normalized_title)),
                "product_colors": sorted(
                    _first_color_values(best_product.name, best_product.color)
                    if best_product
                    else set()
                ),
                "competitor_frame": _camera_glass_frame_state(item_text),
                "product_frame": _camera_glass_frame_state(product_text),
            }
        if screen_protector_suggest:
            best_product = products.get(best_pid)
            item_text = " ".join(filter(None, [item.name, item.normalized_title, item.external_id]))
            product_text = best_product.name if best_product else None
            rationale["screen_protector_suggest"] = {
                "reason": "screen_protector_family_model_or_code_color_match",
                "overlap_model_keys": sorted(
                    _extract_device_model_keys(item_text).intersection(
                        _extract_device_model_keys(product_text)
                    )
                ),
                "overlap_codes": sorted(
                    _extract_device_codes(item_text).intersection(
                        _extract_device_codes(product_text)
                    )
                ),
                "competitor_colors": sorted(_first_color_values(item.name, item.normalized_title)),
                "product_colors": sorted(
                    _first_color_values(best_product.name, best_product.color)
                    if best_product
                    else set()
                ),
            }
        if phone_sim_tray_suggest:
            best_product = products.get(best_pid)
            item_text = " ".join(filter(None, [item.name, item.normalized_title, item.external_id]))
            product_text = best_product.name if best_product else None
            rationale["phone_sim_tray_suggest"] = {
                "reason": "phone_sim_tray_family_model_or_code_color_match",
                "overlap_model_keys": sorted(
                    _extract_device_model_keys(item_text).intersection(
                        _extract_device_model_keys(product_text)
                    )
                ),
                "overlap_codes": sorted(
                    _extract_device_codes(item_text).intersection(
                        _extract_device_codes(product_text)
                    )
                ),
                "competitor_colors": sorted(_first_color_values(item.name, item.normalized_title)),
                "product_colors": sorted(
                    _first_color_values(best_product.name, best_product.color)
                    if best_product
                    else set()
                ),
            }
        if network_cable_suggest:
            best_product = products.get(best_pid)
            item_text = " ".join(filter(None, [item.name, item.normalized_title, item.external_id]))
            product_text = best_product.name if best_product else None
            rationale["network_cable_suggest"] = {
                "reason": "network_cable_model_and_length_match",
                "competitor_model": _network_cable_model(item_text),
                "product_model": _network_cable_model(product_text),
                "competitor_length_m": _cable_length_meters(item_text),
                "product_length_m": _cable_length_meters(product_text),
            }
        if housing_part_suggest:
            best_product = products.get(best_pid)
            item_text = " ".join(filter(None, [item.name, item.normalized_title, item.external_id]))
            product_text = best_product.name if best_product else None
            rationale["housing_part_suggest"] = {
                "reason": "housing_family_model_or_code_color_kind_match",
                "kind": _competitor_housing_part_kind(item),
                "product_kind": _product_housing_part_kind(best_product) if best_product else None,
                "overlap_model_keys": sorted(
                    _extract_device_model_keys(item_text).intersection(
                        _extract_device_model_keys(product_text)
                    )
                ),
                "overlap_codes": sorted(
                    _extract_device_codes(item_text).intersection(
                        _extract_device_codes(product_text)
                    )
                ),
                "competitor_colors": sorted(_first_color_values(item.name, item.normalized_title)),
                "product_colors": sorted(
                    _first_color_values(best_product.name, best_product.color)
                    if best_product
                    else set()
                ),
            }
        if flex_suggest:
            best_product = products.get(best_pid)
            item_text = " ".join(filter(None, [item.name, item.normalized_title, item.external_id]))
            product_text = best_product.name if best_product else None
            rationale["flex_suggest"] = {
                "reason": "flex_role_model_or_code_color_match",
                "role": _competitor_flex_role(item),
                "product_role": _product_flex_role(best_product) if best_product else None,
                "overlap_model_keys": sorted(
                    _extract_device_model_keys(item_text).intersection(
                        _extract_device_model_keys(product_text)
                    )
                ),
                "overlap_codes": sorted(
                    _extract_device_codes(item_text).intersection(
                        _extract_device_codes(product_text)
                    )
                ),
                "competitor_colors": sorted(_first_color_values(item.name, item.normalized_title)),
                "product_colors": sorted(
                    _first_color_values(best_product.name, best_product.color)
                    if best_product
                    else set()
                ),
                "fingerprint": _flex_has_fingerprint(item_text),
                "product_fingerprint": _flex_has_fingerprint(product_text),
            }
        if stencil_suggest:
            best_product = products.get(best_pid)
            item_text = " ".join(filter(None, [item.name, item.normalized_title, item.external_id]))
            product_text = best_product.name if best_product else None
            rationale["stencil_suggest"] = {
                "reason": "stencil_family_chipset_or_series_overlap",
                "overlap_tokens": sorted(
                    _stencil_signature_tokens(item_text).intersection(
                        _stencil_signature_tokens(product_text)
                    )
                ),
            }
        if not item_type:
            rationale["item_type_review"] = {
                "reason": "competitor_item_type_missing",
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
            try:
                result = llm_client.chat_completion(
                    messages=[
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": _arbiter_prompt(item, candidates_for_llm)},
                    ],
                    temperature=0.0,
                    max_tokens=200,
                    response_validator=lambda content: _json_has_product_id(content),
                )
                content = result.content
                parsed = json.loads(content)
                selected_id = parsed.get("product_id")
                llm_confidence = parsed.get("confidence")
                rationale["llm"] = parsed
                rationale["llm_provider"] = result.provider
                rationale["llm_model"] = result.model
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
        best_product = products.get(best_pid)
        match.product_id = best_pid
        if best_product is not None:
            match.product = best_product
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
        if code_overlap_auto_accept:
            best_product = products.get(best_pid)
            if best_product:
                _ensure_code_overlap_compatibilities(
                    session,
                    item,
                    best_product,
                    _display_model_code_overlap_details(item, best_product),
                )

    if auto_accept_code_overlap and not dry_run:
        stats["auto_rejected_model_conflict"] = _auto_reject_explicit_model_conflicts(session)
        stats["auto_rejected_display_subject_conflict"] = _auto_reject_display_subject_conflicts(
            session
        )
        stats["auto_rejected_display_frame_conflict"] = _auto_reject_display_frame_conflicts(
            session
        )
        stats["auto_rejected_display_color_conflict"] = _auto_reject_display_color_conflicts(
            session
        )
        stats["auto_rejected_part_color_conflict"] = _auto_reject_part_color_conflicts(session)
        stats["auto_rejected_part_quality_conflict"] = _auto_reject_part_quality_conflicts(session)
        stats["auto_rejected_part_assembly_conflict"] = _auto_reject_part_assembly_conflicts(
            session
        )
        stats["auto_rejected_other_family_conflict"] = _auto_reject_other_family_conflicts(session)
        stats["auto_rejected_housing_condition_conflict"] = (
            _auto_reject_housing_condition_conflicts(session)
        )
        stats["auto_rejected_housing_part_kind_conflict"] = (
            _auto_reject_housing_part_kind_conflicts(session)
        )
        stats["auto_rejected_housing_device_code_conflict"] = (
            _auto_reject_housing_device_code_conflicts(session)
        )
        stats["auto_rejected_camera_position_conflict"] = _auto_reject_camera_position_conflicts(
            session
        )
        stats["auto_rejected_flex_role_conflict"] = _auto_reject_flex_role_conflicts(session)
        stats["auto_rejected_battery_part_code_conflict"] = (
            _auto_reject_battery_part_code_conflicts(session)
        )
        stats["auto_rejected_battery_subject_conflict"] = _auto_reject_battery_subject_conflicts(
            session
        )
        stats["auto_rejected_display_long_model_code_conflict"] = (
            _auto_reject_display_long_model_code_conflicts(session)
        )
        stats["auto_rejected_display_text_model_conflict"] = (
            _auto_reject_display_text_model_conflicts(session)
        )
        stats["auto_rejected_display_module_component_conflict"] = (
            _auto_reject_display_module_component_conflicts(session)
        )
        stats["auto_rejected_laptop_matrix_flex_conflict"] = (
            _auto_reject_laptop_matrix_flex_conflicts(session)
        )
        stats["auto_rejected_display_matrix_tag_conflict"] = (
            _auto_reject_display_matrix_tag_conflicts(session)
        )
        stats["auto_rejected_display_condition_conflict"] = (
            _auto_reject_display_condition_conflicts(session)
        )
        stats["auto_rejected_non_display_model_code_conflict"] = (
            _auto_reject_non_display_model_code_conflicts(session)
        )
        stats["auto_rejected_guardrail_device_group_conflict"] = (
            _auto_reject_guardrail_device_group_conflicts(session)
        )
        stats["auto_rejected_guardrail_catalog_family_conflict"] = (
            _auto_reject_guardrail_catalog_family_conflicts(session)
        )
        stats["auto_rejected_display_attribute_conflict"] = (
            _auto_reject_display_attribute_conflicts(session)
        )
    if auto_accept_unique and not dry_run:
        stats["auto_accepted_unique"] = _auto_accept_unique_suggested_matches(
            session,
            min_score=auto_accept_min_score,
        )
    if auto_accept_code_overlap and not dry_run:
        stats["auto_accepted_code_overlap"] = _auto_accept_explicit_code_overlap_matches(
            session,
            min_score=auto_accept_min_score,
        )
        stats["auto_accepted_model_text"] = _auto_accept_explicit_model_text_matches(
            session,
            min_score=auto_accept_min_score,
        )
        stats["auto_accepted_battery_part_code"] = _auto_accept_battery_part_code_matches(
            session,
            min_score=auto_accept_min_score,
        )
        stats["auto_accepted_battery_original_part_code"] = (
            _auto_accept_battery_original_part_code_matches(
                session,
                min_score=auto_accept_min_score,
            )
        )
        stats["auto_accepted_iphone_battery_capacity"] = (
            _auto_accept_iphone_battery_capacity_matches(
                session,
                min_score=auto_accept_min_score,
            )
        )
        stats["auto_accepted_housing_part"] = _auto_accept_housing_part_matches(
            session,
            min_score=auto_accept_min_score,
        )
        stats["auto_accepted_flex"] = _auto_accept_flex_matches(
            session,
            min_score=auto_accept_min_score,
        )
        stats["auto_accepted_camera"] = _auto_accept_camera_matches(
            session,
            min_score=auto_accept_min_score,
        )
        stats["auto_accepted_connector"] = _auto_accept_connector_matches(
            session,
            min_score=auto_accept_min_score,
        )
        stats["auto_accepted_other_safe_family"] = _auto_accept_other_safe_family_matches(
            session,
            min_score=auto_accept_min_score,
        )
        stats["auto_accepted_display_original_quality"] = (
            _auto_accept_display_original_quality_matches(
                session,
                min_score=auto_accept_min_score,
            )
        )
        stats["auto_accepted_display_unspecified_quality"] = (
            _auto_accept_display_unspecified_quality_matches(
                session,
                min_score=auto_accept_min_score,
            )
        )
        stats["auto_accepted_display_construction"] = _auto_accept_display_construction_matches(
            session,
            min_score=auto_accept_min_score,
        )
        stats["auto_accepted_display_matrix_tag"] = _auto_accept_display_matrix_tag_matches(
            session,
            min_score=auto_accept_min_score,
        )
        stats["auto_accepted_display_matrix_type"] = _auto_accept_display_matrix_type_matches(
            session,
            min_score=auto_accept_min_score,
        )
        stats["auto_code_overlap_compatibilities_created"] = (
            _backfill_explicit_code_overlap_compatibilities(session)
        )

    if not dry_run:
        session.commit()
    if llm_client:
        llm_client.close()
    if samples_file:
        payload = {"stats": stats, "samples": samples}
        with open(samples_file, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, default=_json_report_default)
    if report_file:
        payload = {"stats": stats, "items": report_rows}
        with open(report_file, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, default=_json_report_default)
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
                        "candidates": json.dumps(
                            row.get("candidates", []),
                            ensure_ascii=False,
                            default=_json_report_default,
                        ),
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
        "--competitor-item-id",
        type=int,
        action="append",
        dest="competitor_item_ids",
        help="Process only specific competitor item id; can be passed multiple times",
    )
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
        "--auto-accept-code-overlap",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Auto-accept screen/touch matches with explicit overlapping device codes",
    )
    parser.add_argument(
        "--auto-accept-min-score",
        type=float,
        default=0.80,
        help="Min score for safe auto-accept rules",
    )
    parser.add_argument(
        "--live-embed-missing",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Compute missing competitor embeddings during matching",
    )
    args = parser.parse_args()

    settings = get_settings()
    engine = build_engine(settings.database_url)
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
            competitor_item_ids=args.competitor_item_ids,
            auto_accept_unique=args.auto_accept_unique,
            auto_accept_code_overlap=args.auto_accept_code_overlap,
            auto_accept_min_score=args.auto_accept_min_score,
            live_embed_missing=args.live_embed_missing,
        )
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    logging.info("match_competitor_items_embeddings done: %s", stats)


if __name__ == "__main__":
    main()
