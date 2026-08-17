"""Canonical normalized display identity shared by matching and family reports."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

from app.models import CompetitorItem, Product
from app.services.display_parser import parse_display_attributes
from app.services.matching_attributes import (
    competitor_attribute,
    normalize_display_construction_value,
    normalize_display_quality_value,
    normalize_display_type_value,
    product_attribute,
)
from app.services.product_display_modification import (
    DISPLAY_MODIFICATION_PARSE_VERSION,
    analyze_product_display_modification,
)

DISPLAY_IDENTITY_SCHEMA_VERSION = "display_identity.v1"
DISPLAY_IDENTITY_RULES_VERSION = "display_identity_rules.v1"


def _clean(value: object | None) -> str:
    return " ".join(str(value or "").strip().split())


def _slug(value: object | None, *, fallback: str = "unknown") -> str:
    text = _clean(value).casefold().replace("ё", "е")
    text = re.sub(r"[^a-zа-я0-9]+", "_", text).strip("_")
    return text or fallback


def _optional_bool(value: object | None) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    normalized = _clean(value).casefold()
    if normalized in {"1", "true", "yes", "да", "есть"}:
        return True
    if normalized in {"0", "false", "no", "нет"}:
        return False
    return None


def _tuple_strings(values: object | None) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, Sequence):
        return ()
    return tuple(sorted({_clean(value) for value in values if _clean(value)}))


def _quality_segment(value: str | None) -> str:
    return {
        "Original": "original",
        "Original Refurbished": "original_refurbished",
        "OEM": "oem",
        "Copy High": "copy_high",
        "Copy Medium": "copy_medium",
        "Copy Low": "copy_low",
    }.get(value or "", "unknown")


def _construction_segment(*, construction: str | None, display_type: str | None) -> str:
    canonical = {
        "HARD_OLED": "hard_oled",
        "SOFT_OLED": "soft_oled",
        "In-Cell": "in_cell",
        "On-Cell": "on_cell",
        "COF": "cof",
        "COG": "cog",
    }.get(construction or "")
    if canonical:
        return canonical
    return {
        "OLED": "oled",
        "AMOLED": "amoled",
        "Super AMOLED": "super_amoled",
        "Dynamic AMOLED": "dynamic_amoled",
        "LTPO AMOLED": "ltpo_amoled",
        "LTPS LCD": "ltps_lcd",
        "LCD (IPS)": "lcd_ips",
        "LCD (TFT)": "lcd_tft",
    }.get(display_type or "", "unknown")


def _frame_segment(value: bool | None) -> str:
    if value is True:
        return "with_frame"
    if value is False:
        return "without_frame"
    return "frame_unknown"


def _significant_modifiers(name: str, matrix_tags: Sequence[str] = ()) -> tuple[str, ...]:
    text = _clean(name).casefold().replace("ё", "е")
    modifiers: set[str] = set()
    if "als" in text and ("шлейф" in text or "flex" in text):
        modifiers.add("als_flex")
    if "верификац" in text or "verified" in text:
        modifiers.add("verified")
    # Manufacturer/matrix tags remain evidence.  They are not a physical segment
    # until a separately versioned rule proves that they change interchangeability.
    _ = matrix_tags
    return tuple(sorted(modifiers))


@dataclass(frozen=True)
class DisplayPhoneModelIdentity:
    phone_model_id: int
    brand: str
    model_name: str
    variant: str | None
    source: str
    is_manual: bool
    confidence: float | None

    @property
    def base_key(self) -> str:
        return ":".join((_slug(self.brand), _slug(self.model_name)))


@dataclass(frozen=True)
class DisplayIdentity:
    entity_type: str
    entity_id: int | None
    code: str
    name: str
    subject: str | None
    phone_models: tuple[DisplayPhoneModelIdentity, ...]
    model_keys: tuple[str, ...]
    quality: str | None
    display_type: str | None
    construction: str | None
    color: str | None
    has_frame: bool | None
    has_touch: bool | None
    has_ic_pad: bool | None
    has_binding_no_solder: bool | None
    backlight: str | None
    matrix_tags: tuple[str, ...]
    modifiers: tuple[str, ...]
    refresh_rate_hz: int | None
    quality_segment: str
    construction_segment: str
    segment_id: str
    warnings: tuple[str, ...]
    evidence: Mapping[str, Any]
    schema_version: str = DISPLAY_IDENTITY_SCHEMA_VERSION
    rules_version: str = DISPLAY_IDENTITY_RULES_VERSION

    @property
    def phone_model_ids(self) -> tuple[int, ...]:
        return tuple(model.phone_model_id for model in self.phone_models)

    @property
    def physical_model_signature(self) -> tuple[str, ...]:
        return tuple(f"phone-model:{model_id}" for model_id in self.phone_model_ids)

    @property
    def related_model_signature(self) -> tuple[str, ...]:
        return tuple(sorted({model.base_key for model in self.phone_models}))

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _segment_id(
    *,
    quality: str | None,
    construction: str | None,
    display_type: str | None,
    has_frame: bool | None,
    has_ic_pad: bool | None,
    has_binding_no_solder: bool | None,
    significant_modifiers: Sequence[str] = (),
) -> tuple[str, str, str]:
    quality_segment = _quality_segment(quality)
    construction_segment = _construction_segment(
        construction=construction,
        display_type=display_type,
    )
    modifiers = [_frame_segment(has_frame)]
    if has_ic_pad is True:
        modifiers.append("ic_pad")
    elif has_ic_pad is None:
        modifiers.append("ic_pad_unknown")
    if has_binding_no_solder is True:
        modifiers.append("binding_no_solder")
    modifiers.extend(sorted(set(significant_modifiers)))
    return (
        quality_segment,
        construction_segment,
        "|".join((quality_segment, construction_segment, *modifiers)),
    )


def _warnings(
    *,
    model_ids: Sequence[int],
    quality: str | None,
    construction_segment: str,
    has_frame: bool | None,
    modification_status: str | None = None,
) -> tuple[str, ...]:
    values: list[str] = []
    if not model_ids:
        values.append("phone_model_unresolved")
    if quality is None:
        values.append("quality_unknown")
    if construction_segment == "unknown":
        values.append("construction_unknown")
    if has_frame is None:
        values.append("frame_unknown")
    if modification_status == "conflict":
        values.append("display_modification_conflict")
    return tuple(values)


def _product_phone_models(product: Product) -> tuple[DisplayPhoneModelIdentity, ...]:
    models: dict[int, DisplayPhoneModelIdentity] = {}
    for link in getattr(product, "phone_model_links", ()) or ():
        model = getattr(link, "phone_model", None)
        model_id = getattr(link, "phone_model_id", None)
        if model is None or model_id is None:
            continue
        confidence = float(link.confidence) if link.confidence is not None else None
        candidate = DisplayPhoneModelIdentity(
            phone_model_id=int(model_id),
            brand=_clean(model.brand),
            model_name=_clean(model.model_name),
            variant=_clean(model.variant) or None,
            source=_clean(link.source) or "unknown",
            is_manual=bool(link.is_manual),
            confidence=confidence,
        )
        existing = models.get(candidate.phone_model_id)
        if existing is None or (candidate.is_manual and not existing.is_manual):
            models[candidate.phone_model_id] = candidate
    return tuple(models[key] for key in sorted(models))


def display_identity_for_product(product: Product) -> DisplayIdentity:
    modification = analyze_product_display_modification(product)
    phone_models = _product_phone_models(product)
    quality = product_attribute(product, "display.quality")
    display_type = product_attribute(product, "display.type")
    construction = product_attribute(product, "display.construction")
    has_frame = product_attribute(product, "display.has_frame")
    has_touch = product_attribute(product, "display.has_touch")
    has_ic_pad = (
        product.display_has_ic_pad
        if product.display_has_ic_pad is not None
        else modification.parsed_has_ic_pad
    )
    has_binding_no_solder = (
        product.display_has_binding_no_solder
        if product.display_has_binding_no_solder is not None
        else modification.parsed_has_binding_no_solder
    )
    matrix_tags = _tuple_strings(product_attribute(product, "display.matrix_tags"))
    modifiers = _significant_modifiers(product.name or "", matrix_tags)
    quality_segment, construction_segment, segment_id = _segment_id(
        quality=quality,
        construction=construction,
        display_type=display_type,
        has_frame=has_frame,
        has_ic_pad=has_ic_pad,
        has_binding_no_solder=has_binding_no_solder,
        significant_modifiers=modifiers,
    )
    code = _clean(product.code_1c or product.article or product.fact_sku or product.id)
    model_keys = _tuple_strings(product_attribute(product, "compatibility.model"))
    warnings = _warnings(
        model_ids=[model.phone_model_id for model in phone_models],
        quality=quality,
        construction_segment=construction_segment,
        has_frame=has_frame,
        modification_status=modification.status,
    )
    return DisplayIdentity(
        entity_type="product",
        entity_id=getattr(product, "id", None),
        code=code,
        name=_clean(product.name),
        subject=_clean(product_attribute(product, "subject")) or None,
        phone_models=phone_models,
        model_keys=model_keys,
        quality=quality,
        display_type=display_type,
        construction=construction,
        color=product_attribute(product, "display.color"),
        has_frame=_optional_bool(has_frame),
        has_touch=_optional_bool(has_touch),
        has_ic_pad=_optional_bool(has_ic_pad),
        has_binding_no_solder=_optional_bool(has_binding_no_solder),
        backlight=product_attribute(product, "display.backlight"),
        matrix_tags=matrix_tags,
        modifiers=modifiers,
        refresh_rate_hz=product_attribute(product, "display.refresh_rate_hz"),
        quality_segment=quality_segment,
        construction_segment=construction_segment,
        segment_id=segment_id,
        warnings=warnings,
        evidence={
            "phone_model_links": [asdict(model) for model in phone_models],
            "compatibility_model_keys": list(model_keys),
            "display_modification": modification.as_report_row(),
            "raw": {
                "display_quality": product.display_quality,
                "quality": product.quality,
                "quality_raw": product.quality_raw,
                "display_type": product.display_type,
                "display_construction": product.display_construction,
                "display_has_frame": product.display_has_frame,
                "in_frame": product.in_frame,
            },
        },
    )


def display_identity_for_competitor(item: CompetitorItem) -> DisplayIdentity:
    phone_models: list[DisplayPhoneModelIdentity] = []
    for compatibility in getattr(item, "compatibilities", ()) or ():
        model = getattr(compatibility, "phone_model", None)
        model_id = getattr(compatibility, "phone_model_id", None)
        if model is None or model_id is None:
            continue
        phone_models.append(
            DisplayPhoneModelIdentity(
                phone_model_id=int(model_id),
                brand=_clean(model.brand),
                model_name=_clean(model.model_name),
                variant=_clean(model.variant) or None,
                source=_clean(compatibility.source) or "unknown",
                is_manual=_clean(compatibility.source).casefold() == "manual",
                confidence=None,
            )
        )
    phone_models = sorted(
        {model.phone_model_id: model for model in phone_models}.values(),
        key=lambda model: model.phone_model_id,
    )
    quality = competitor_attribute(item, "display.quality")
    display_type = competitor_attribute(item, "display.type")
    construction = competitor_attribute(item, "display.construction")
    has_frame = competitor_attribute(item, "display.has_frame")
    has_touch = competitor_attribute(item, "display.has_touch")
    parsed = parse_display_attributes(item.name or item.normalized_title or "")
    has_ic_pad = item.has_ic_pad if item.has_ic_pad is not None else parsed.has_ic_pad
    has_binding_no_solder = (
        item.has_binding_no_solder
        if item.has_binding_no_solder is not None
        else parsed.has_binding_no_solder
    )
    matrix_tags = _tuple_strings(competitor_attribute(item, "display.matrix_tags"))
    modifiers = _significant_modifiers(item.name or item.normalized_title or "", matrix_tags)
    quality_segment, construction_segment, segment_id = _segment_id(
        quality=quality,
        construction=construction,
        display_type=display_type,
        has_frame=has_frame,
        has_ic_pad=has_ic_pad,
        has_binding_no_solder=has_binding_no_solder,
        significant_modifiers=modifiers,
    )
    model_keys = _tuple_strings(competitor_attribute(item, "compatibility.model"))
    return DisplayIdentity(
        entity_type="competitor_item",
        entity_id=getattr(item, "id", None),
        code=_clean(item.external_id or item.id),
        name=_clean(item.name or item.normalized_title),
        subject=_clean(competitor_attribute(item, "subject")) or None,
        phone_models=tuple(phone_models),
        model_keys=model_keys,
        quality=quality,
        display_type=display_type,
        construction=construction,
        color=competitor_attribute(item, "display.color"),
        has_frame=_optional_bool(has_frame),
        has_touch=_optional_bool(has_touch),
        has_ic_pad=_optional_bool(has_ic_pad),
        has_binding_no_solder=_optional_bool(has_binding_no_solder),
        backlight=competitor_attribute(item, "display.backlight"),
        matrix_tags=matrix_tags,
        modifiers=modifiers,
        refresh_rate_hz=competitor_attribute(item, "display.refresh_rate_hz"),
        quality_segment=quality_segment,
        construction_segment=construction_segment,
        segment_id=segment_id,
        warnings=_warnings(
            model_ids=[model.phone_model_id for model in phone_models],
            quality=quality,
            construction_segment=construction_segment,
            has_frame=_optional_bool(has_frame),
        ),
        evidence={
            "phone_model_links": [asdict(model) for model in phone_models],
            "compatibility_model_keys": list(model_keys),
            "parser_version": DISPLAY_MODIFICATION_PARSE_VERSION,
        },
    )


def display_identity_from_mapping(item: Mapping[str, Any]) -> DisplayIdentity:
    """Build the same segment contract for frozen/backtest mapping rows."""

    name = _clean(item.get("name"))
    parsed = parse_display_attributes(name)
    quality = normalize_display_quality_value(
        item.get("display_quality")
        or item.get("quality")
        or item.get("quality_raw")
        or parsed.screen_quality_grade
    )
    display_type = normalize_display_type_value(
        item.get("display_type") or parsed.screen_matrix_type
    )
    construction = normalize_display_construction_value(
        item.get("display_construction") or parsed.screen_construction
    )
    has_frame = _optional_bool(item.get("display_has_frame"))
    if has_frame is None:
        has_frame = parsed.has_frame
    has_ic_pad = _optional_bool(item.get("display_has_ic_pad"))
    if has_ic_pad is None:
        has_ic_pad = parsed.has_ic_pad
    has_binding_no_solder = _optional_bool(item.get("display_has_binding_no_solder"))
    if has_binding_no_solder is None:
        has_binding_no_solder = parsed.has_binding_no_solder
    matrix_tags = _tuple_strings(item.get("display_matrix_tags") or parsed.matrix_tags)
    modifiers = _significant_modifiers(name, matrix_tags)
    quality_segment, construction_segment, segment_id = _segment_id(
        quality=quality,
        construction=construction,
        display_type=display_type,
        has_frame=has_frame,
        has_ic_pad=has_ic_pad,
        has_binding_no_solder=has_binding_no_solder,
        significant_modifiers=modifiers,
    )
    model_keys = _tuple_strings(item.get("model_tokens"))
    explicit_has_touch = _optional_bool(item.get("display_has_touch"))
    return DisplayIdentity(
        entity_type="mapping",
        entity_id=None,
        code=_clean(item.get("nomenclature_code") or item.get("code")),
        name=name,
        subject=_clean(item.get("subject")) or None,
        phone_models=(),
        model_keys=model_keys,
        quality=quality,
        display_type=display_type,
        construction=construction,
        color=_clean(item.get("color")) or parsed.color,
        has_frame=has_frame,
        has_touch=explicit_has_touch if explicit_has_touch is not None else parsed.has_touch,
        has_ic_pad=has_ic_pad,
        has_binding_no_solder=has_binding_no_solder,
        backlight=_clean(item.get("display_backlight")) or parsed.backlight.value,
        matrix_tags=matrix_tags,
        modifiers=modifiers,
        refresh_rate_hz=item.get("display_refresh_rate_hz") or parsed.refresh_rate_hz,
        quality_segment=quality_segment,
        construction_segment=construction_segment,
        segment_id=segment_id,
        warnings=_warnings(
            model_ids=[1] if model_keys else [],
            quality=quality,
            construction_segment=construction_segment,
            has_frame=has_frame,
        ),
        evidence={
            "model_tokens": list(model_keys),
            "parser_status": parsed.parse_status,
            "parser_reasons": list(parsed.reasons),
        },
    )
