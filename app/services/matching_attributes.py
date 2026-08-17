from __future__ import annotations

import re
from typing import Any

from app.models import CompetitorItem, Product
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
from app.services.matching_guardrails import phone_model_keys
from app.services.product_display_modification import normalize_onec_in_frame

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
    "gold": {"gold", "golden", "золотой", "золотая", "золото", "золотистый"},
    "gray": {"gray", "grey", "серый", "серая"},
    "silver": {"silver", "серебристый", "серебристая", "серебро"},
    "beige": {"beige", "бежевый", "бежевая"},
    "graphite": {"graphite", "графит", "графитовый", "графитовая"},
    "brown": {"brown", "коричневый", "коричневая"},
    "purple": {"purple", "violet", "lavender", "фиолетовый", "фиолетовая"},
    "bronze": {"bronze", "бронзовый", "бронзовая"},
    "titanium": {"titanium", "титановый", "титановая", "титан"},
}

QUALITY_ALIASES = {
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


def normalize_mapping_text(value: object | None) -> str:
    return " ".join(str(value or "").strip().casefold().replace("ё", "е").split())


def display_value(value: object | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, (list, set, tuple)):
        values = [str(item) for item in value if str(item).strip()]
        return ", ".join(sorted(values)) if values else None
    text = str(value).strip()
    return text or None


def _parsed_texts(*values: str | None):
    for value in values:
        if value:
            yield parse_display_attributes(value)


def _normalize_display_quality_value(value: object | None) -> str | None:
    if value is None:
        return None
    raw = value.value if isinstance(value, ScreenQualityGrade) else str(value).strip()
    if not raw or raw == ScreenQualityGrade.UNKNOWN.value:
        return None
    alias = QUALITY_ALIASES.get(raw)
    if alias:
        return alias
    return normalize_display_quality(raw)


def normalize_display_quality_value(value: object | None) -> str | None:
    """Public canonical adapter used by matching and display-family identity."""

    return _normalize_display_quality_value(value)


def _normalize_display_type_value(value: object | None) -> str | None:
    if value is None:
        return None
    raw = value.value if isinstance(value, ScreenMatrixType) else str(value).strip()
    if not raw or raw == ScreenMatrixType.UNKNOWN.value:
        return None
    return normalize_display_type(raw)


def normalize_display_type_value(value: object | None) -> str | None:
    """Public canonical adapter used by matching and display-family identity."""

    return _normalize_display_type_value(value)


def _normalize_display_construction_value(value: object | None) -> str | None:
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


def normalize_display_construction_value(value: object | None) -> str | None:
    """Public canonical adapter used by matching and display-family identity."""

    return _normalize_display_construction_value(value)


def _normalize_backlight(value: object | None) -> str | None:
    if value is None:
        return None
    raw = value.value if isinstance(value, Backlight) else str(value).strip()
    if not raw or raw == Backlight.UNKNOWN.value:
        return None
    for item in Backlight:
        if raw == item.value:
            return item.value
    parsed = parse_display_attributes(raw).backlight
    return parsed.value if parsed != Backlight.UNKNOWN else None


def _normalize_color(value: str | None) -> str | None:
    if not value:
        return None
    tokens = set(re.findall(r"[a-zа-яё]+", value.lower().replace("ё", "е")))
    for canonical, aliases in COLOR_ALIASES.items():
        normalized_aliases = {alias.replace("ё", "е") for alias in aliases}
        if tokens & normalized_aliases:
            return canonical
    return None


def _explicit_frame_value(text: str | None) -> bool | None:
    if not text:
        return None
    normalized = text.lower().replace("ё", "е")
    if re.search(r"\bбез\s*рамк\w*\b", normalized):
        return False
    if re.search(r"\b(с\s+рамк\w*|в\s+рамк\w*|рамк\w*)\b", normalized):
        return True
    return None


def _explicit_touch_value(text: str | None) -> bool | None:
    if not text:
        return None
    normalized = text.lower().replace("ё", "е")
    if re.search(r"\bбез\s+(тачскрин\w*|сенсор\w*|touch|digitizer)\b", normalized):
        return False
    if re.search(r"\b(тачскрин\w*|сенсор\w*|touch|digitizer)\b", normalized):
        return True
    return None


def _normalize_model(value: str | None) -> str | None:
    if not value:
        return None
    keys = phone_model_keys(value)
    if keys:
        return ", ".join(sorted(keys))
    normalized = value.lower().replace("ё", "е")
    normalized = re.sub(r"[^a-z0-9а-я]+", " ", normalized)
    normalized = re.sub(r"\b(для|with|black|white|черный|белый)\b", " ", normalized)
    normalized = " ".join(normalized.split())
    return normalized or None


def _phone_model_text(
    brand: object | None, model: object | None, variant: object | None
) -> str | None:
    text = " ".join(
        str(value).strip() for value in (brand, model, variant) if str(value or "").strip()
    )
    return text or None


def _compatibility_model_values_from_product(product: Product) -> list[str]:
    values: set[str] = set()
    for link in getattr(product, "phone_model_links", []) or []:
        phone_model = getattr(link, "phone_model", None)
        if phone_model:
            text = _phone_model_text(phone_model.brand, phone_model.model_name, phone_model.variant)
            keys = phone_model_keys(text)
            values.update(keys or ({normalize_mapping_text(text)} if text else set()))
        elif link.raw_value:
            keys = phone_model_keys(link.raw_value)
            values.update(keys or {normalize_mapping_text(link.raw_value)})
    for compat in getattr(product, "compatibilities", []) or []:
        keys = phone_model_keys(compat.value)
        values.update(keys or {normalize_mapping_text(compat.value)})
    if not values:
        keys = phone_model_keys(_phone_model_text(product.brand, product.name, None))
        values.update(keys)
    return sorted(value for value in values if value)


def _compatibility_model_values_from_competitor(item: CompetitorItem) -> list[str]:
    values: set[str] = set()
    for compat in getattr(item, "compatibilities", []) or []:
        phone_model = getattr(compat, "phone_model", None)
        if phone_model:
            text = _phone_model_text(phone_model.brand, phone_model.model_name, phone_model.variant)
            keys = phone_model_keys(text)
            values.update(keys or ({normalize_mapping_text(text)} if text else set()))
        text = _phone_model_text(compat.device_brand, compat.device_model, compat.device_variant)
        keys = phone_model_keys(text)
        values.update(keys or ({normalize_mapping_text(text)} if text else set()))
    if not values:
        text = _phone_model_text(
            item.item_brand,
            item.attrs_model or item.parsed_device_model or item.product_model,
            None,
        )
        keys = (
            phone_model_keys(text)
            or phone_model_keys(item.name)
            or phone_model_keys(item.normalized_title)
        )
        values.update(keys)
    return sorted(value for value in values if value)


def _json_path(value: dict[str, Any] | None, path: str) -> object | None:
    current: object | None = value
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _infer_item_type(*values: object | None) -> str | None:
    text = " ".join(str(value or "").lower() for value in values)
    if any(token in text for token in ("дисплей", "тачскрин", "lcd", "oled", "экран", "display")):
        return "display"
    if any(token in text for token in ("аккумулятор", "акб", "battery")):
        return "battery"
    if "камера" in text or "camera" in text:
        return "camera"
    if "шлейф" in text or "flex" in text:
        return "flex"
    if any(token in text for token in ("разъем", "разъём", "коннектор", "connector")):
        return "connector"
    if any(token in text for token in ("крышка", "корпус", "housing", "cover")):
        return "housing"
    if any(token in text for token in ("кабель", "провод", "cable")):
        return "cable"
    return None


def product_attribute(product: Product, field: str) -> object | None:
    if field == "subject":
        return (
            _infer_item_type(
                product.subject,
                product.subject_1c,
                product.subject_generated,
                product.category,
                product.name,
            )
            or product.subject
            or product.subject_1c
            or product.subject_generated
        )
    if field == "display.model":
        return _normalize_model(product.name)
    if field == "compatibility.model":
        return _compatibility_model_values_from_product(product)
    if field == "display.quality":
        for parsed in _parsed_texts(product.name):
            normalized = _normalize_display_quality_value(parsed.screen_quality_grade)
            if normalized:
                return normalized
        for value in (
            product.display_quality,
            product.quality,
            product.display_quality_raw,
            product.quality_raw,
        ):
            normalized = _normalize_display_quality_value(value)
            if normalized:
                return normalized
        return _normalize_display_quality_value(extract_quality_token_as_in_name(product.name))
    if field == "display.color":
        return _normalize_color(product.name) or _normalize_color(product.color)
    if field == "display.has_frame":
        return (
            _explicit_frame_value(product.name)
            if _explicit_frame_value(product.name) is not None
            else (
                product.display_has_frame
                if product.display_has_frame is not None
                else normalize_onec_in_frame(product.in_frame)
            )
        )
    if field == "display.has_touch":
        explicit = _explicit_touch_value(product.name)
        return explicit if explicit is not None else product.display_has_touch
    if field == "display.type":
        for parsed in _parsed_texts(product.name):
            normalized = _normalize_display_type_value(parsed.screen_matrix_type)
            if normalized:
                return normalized
        return _normalize_display_type_value(product.display_type)
    if field == "display.construction":
        for parsed in _parsed_texts(product.name):
            normalized = _normalize_display_construction_value(parsed.screen_construction)
            if normalized:
                return normalized
        return _normalize_display_construction_value(product.display_construction)
    if field == "display.backlight":
        for parsed in _parsed_texts(product.name):
            normalized = _normalize_backlight(parsed.backlight)
            if normalized:
                return normalized
        return _normalize_backlight(product.display_backlight)
    if field == "display.matrix_tags":
        tags = set(parse_display_attributes(product.name or "").matrix_tags)
        tags.update(str(tag).strip().upper() for tag in product.display_matrix_tags or [])
        return sorted(tag for tag in tags if tag)
    if field == "display.refresh_rate_hz":
        return normalize_refresh_rate_hz(
            parse_display_attributes(product.name or "").refresh_rate_hz
        ) or normalize_refresh_rate_hz(product.display_refresh_rate_hz)
    if field == "battery.capacity_mah":
        return product.battery_capacity_mah
    if field == "connector.type":
        return (
            product.cable_connector_output
            or product.cable_connector_input
            or product.charger_plug_type
        )
    return getattr(product, field, None)


def competitor_attribute(item: CompetitorItem, field: str) -> object | None:
    if field.startswith("attrs."):
        return _json_path(item.attrs_json, field.removeprefix("attrs."))
    if field == "subject":
        return (
            item.item_type
            or _infer_item_type(item.category_group, item.category, item.name)
            or item.category_group
            or item.category
        )
    if field == "display.model":
        return _normalize_model(item.attrs_model or item.parsed_device_model or item.name)
    if field == "compatibility.model":
        return _compatibility_model_values_from_competitor(item)
    if field == "display.quality":
        raw_quality = extract_quality_token_as_in_name(
            item.name
        ) or extract_quality_token_as_in_name(item.normalized_title)
        mapped_raw = map_competitor_raw_quality_to_1c_raw(item.competitor, raw_quality)
        for value in (
            mapped_raw,
            raw_quality,
            item.attrs_quality,
            item.screen_quality_grade,
            parse_display_attributes(item.name or "").screen_quality_grade if item.name else None,
            (
                parse_display_attributes(item.normalized_title or "").screen_quality_grade
                if item.normalized_title
                else None
            ),
        ):
            normalized = _normalize_display_quality_value(value)
            if normalized:
                return normalized
        return None
    if field == "display.color":
        return (
            _normalize_color(item.name)
            or _normalize_color(item.normalized_title)
            or _normalize_color(item.color)
            or _normalize_color(item.attrs_color)
        )
    if field == "display.has_frame":
        for text in (item.name, item.normalized_title):
            explicit = _explicit_frame_value(text)
            if explicit is not None:
                return explicit
        if item.competitor and item.competitor.casefold() == "moba":
            tokens = {
                token
                for token in re.split(r"[^a-z0-9]+", (item.external_id or "").casefold())
                if token
            }
            if "fr" in tokens:
                return True
            if "cp" in tokens:
                return False
        return item.has_frame
    if field == "display.has_touch":
        for text in (item.name, item.normalized_title):
            explicit = _explicit_touch_value(text)
            if explicit is not None:
                return explicit
        return True if item.has_touch is True else None
    if field == "display.type":
        for text in (item.name, item.normalized_title):
            if text:
                normalized = _normalize_display_type_value(
                    parse_display_attributes(text).screen_matrix_type
                )
                if normalized:
                    return normalized
        for value in (item.screen_matrix_type, item.attrs_type):
            normalized = _normalize_display_type_value(value)
            if normalized:
                return normalized
        return None
    if field == "display.construction":
        for text in (item.name, item.normalized_title):
            if text:
                normalized = _normalize_display_construction_value(
                    parse_display_attributes(text).screen_construction
                )
                if normalized:
                    return normalized
        return _normalize_display_construction_value(item.screen_construction)
    if field == "display.backlight":
        for text in (item.name, item.normalized_title):
            if text:
                normalized = _normalize_backlight(parse_display_attributes(text).backlight)
                if normalized:
                    return normalized
        return _normalize_backlight(item.backlight)
    if field == "display.matrix_tags":
        tags: set[str] = set()
        for text in (item.name, item.normalized_title):
            if text:
                tags.update(parse_display_attributes(text).matrix_tags)
        tags.update(str(tag).strip().upper() for tag in item.matrix_tags or [])
        return sorted(tag for tag in tags if tag)
    if field == "display.refresh_rate_hz":
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
    if field == "battery.capacity_mah":
        raw = item.attrs_capacity
        if raw:
            match = re.search(r"\d{3,5}", str(raw))
            return int(match.group(0)) if match else None
        return None
    if field == "connector.type":
        return item.attrs_type
    return getattr(item, field, None)
