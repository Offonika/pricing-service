from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.models.product import Product
from app.services.display_parser import parse_display_attributes

DISPLAY_MODIFICATION_PARSE_VERSION = "product_display_modification_v2"

STATUS_CONFIRMED = "confirmed"
STATUS_DERIVED_FROM_NAME = "derived_from_name"
STATUS_DERIVED_FROM_ONEC = "derived_from_onec"
STATUS_CONFLICT = "conflict"
STATUS_UNKNOWN = "unknown"

SOURCE_ONEC_PROPERTY = "onec_property"
SOURCE_NAME_PARSER = "name_parser"
SOURCE_ONEC_AND_NAME = "onec_and_name"
SOURCE_CONFLICT = "conflict"
SOURCE_UNKNOWN = "unknown"

TRUE_VALUES = {
    "1",
    "true",
    "yes",
    "y",
    "да",
    "есть",
    "в рамке",
    "с рамкой",
    "рамка",
}
FALSE_VALUES = {
    "0",
    "false",
    "no",
    "n",
    "нет",
    "без рамки",
    "не в рамке",
    "нет рамки",
}


def _normalize_text(value: str | None) -> str:
    return " ".join(str(value or "").strip().lower().replace("ё", "е").split())


def _name_has_explicit_frame_false(value: str | None) -> bool:
    text = _normalize_text(value)
    return "без рам" in text or "не в рам" in text or "нет рам" in text


def _name_has_explicit_frame_true(value: str | None) -> bool:
    text = _normalize_text(value)
    if _name_has_explicit_frame_false(text):
        return False
    return "в рам" in text or "с рам" in text or "рамка" in text


@dataclass(frozen=True)
class ProductDisplayModificationResult:
    product_id: int | None
    article: str | None
    name: str
    in_frame_raw: str | None
    onec_has_frame: bool | None
    parsed_has_frame: bool | None
    display_has_frame: bool | None
    status: str
    source: str
    confidence: float | None
    parsed_screen_kit: str
    parsed_has_touch: bool | None
    parsed_has_ic_pad: bool | None
    parsed_has_binding_no_solder: bool | None
    parsed_backlight: str
    parsed_matrix_tags: list[str]
    notes: list[str]
    parse_version: str

    def as_report_row(self) -> dict[str, Any]:
        return {
            "product_id": self.product_id,
            "article": self.article,
            "name": self.name,
            "in_frame": self.in_frame_raw,
            "onec_has_frame": self.onec_has_frame,
            "parsed_has_frame": self.parsed_has_frame,
            "display_has_frame": self.display_has_frame,
            "status": self.status,
            "source": self.source,
            "confidence": self.confidence,
            "parsed_screen_kit": self.parsed_screen_kit,
            "parsed_has_touch": self.parsed_has_touch,
            "parsed_has_ic_pad": self.parsed_has_ic_pad,
            "parsed_has_binding_no_solder": self.parsed_has_binding_no_solder,
            "parsed_backlight": self.parsed_backlight,
            "parsed_matrix_tags": ",".join(self.parsed_matrix_tags),
            "notes": "; ".join(self.notes),
            "parse_version": self.parse_version,
        }


def normalize_onec_in_frame(value: str | None) -> bool | None:
    if value is None:
        return None
    text = _normalize_text(value)
    if not text:
        return None
    if text in TRUE_VALUES:
        return True
    if text in FALSE_VALUES:
        return False
    if "без рам" in text:
        return False
    if "рам" in text:
        return True
    return None


def is_display_product(product: Product) -> bool:
    haystack = " ".join(
        str(value).lower()
        for value in (
            product.subject_1c,
            product.subject_generated,
            product.subject,
            product.category,
            product.vid_nomenklatury_1c,
            product.vid_nomenklatury_generated,
            product.vid_nomenklatury,
            product.name,
            product.display_type,
        )
        if value
    )
    return any(
        token in haystack
        for token in ("дисп", "экран", "display", "lcd", "oled", "amoled", "тачскрин")
    )


def analyze_product_display_modification(
    product: Product,
    *,
    parse_version: str = DISPLAY_MODIFICATION_PARSE_VERSION,
) -> ProductDisplayModificationResult:
    parsed = parse_display_attributes(product.name or "")
    onec_has_frame = normalize_onec_in_frame(product.in_frame)
    parsed_has_frame = parsed.has_frame
    has_explicit_frame_false = _name_has_explicit_frame_false(product.name)
    has_explicit_frame_true = _name_has_explicit_frame_true(product.name)
    parsed_frame_is_explicit = has_explicit_frame_false or has_explicit_frame_true

    if onec_has_frame is not None and parsed_has_frame is not None:
        if onec_has_frame == parsed_has_frame:
            status = STATUS_CONFIRMED
            source = SOURCE_ONEC_AND_NAME
            display_has_frame = onec_has_frame
            confidence = 1.0
        elif parsed_frame_is_explicit:
            status = STATUS_CONFLICT
            source = SOURCE_CONFLICT
            display_has_frame = parsed_has_frame
            confidence = 0.9
        else:
            status = STATUS_DERIVED_FROM_ONEC
            source = SOURCE_ONEC_PROPERTY
            display_has_frame = onec_has_frame
            confidence = 0.8
    elif parsed_has_frame is not None:
        status = STATUS_DERIVED_FROM_NAME
        source = SOURCE_NAME_PARSER
        display_has_frame = parsed_has_frame
        confidence = 0.85
    elif onec_has_frame is not None:
        status = STATUS_DERIVED_FROM_ONEC
        source = SOURCE_ONEC_PROPERTY
        display_has_frame = onec_has_frame
        confidence = 0.8
    else:
        status = STATUS_UNKNOWN
        source = SOURCE_UNKNOWN
        display_has_frame = None
        confidence = None

    notes = list(parsed.notes_raw_tokens)
    if status == STATUS_CONFLICT:
        notes.append("in_frame_conflict:name_wins")

    return ProductDisplayModificationResult(
        product_id=getattr(product, "id", None),
        article=product.article,
        name=product.name or "",
        in_frame_raw=product.in_frame,
        onec_has_frame=onec_has_frame,
        parsed_has_frame=parsed_has_frame,
        display_has_frame=display_has_frame,
        status=status,
        source=source,
        confidence=confidence,
        parsed_screen_kit=parsed.screen_kit.value,
        parsed_has_touch=parsed.has_touch,
        parsed_has_ic_pad=parsed.has_ic_pad,
        parsed_has_binding_no_solder=parsed.has_binding_no_solder,
        parsed_backlight=parsed.backlight.value,
        parsed_matrix_tags=list(parsed.matrix_tags),
        notes=notes,
        parse_version=parse_version,
    )


def apply_product_display_modification(
    product: Product,
    result: ProductDisplayModificationResult,
) -> None:
    product.display_screen_kit = result.parsed_screen_kit
    product.display_has_frame = result.display_has_frame
    product.display_has_touch = result.parsed_has_touch
    product.display_has_ic_pad = result.parsed_has_ic_pad
    product.display_has_binding_no_solder = result.parsed_has_binding_no_solder
    product.display_backlight = result.parsed_backlight
    product.display_matrix_tags = result.parsed_matrix_tags or None
    product.display_modification_status = result.status
    product.display_modification_source = result.source
    product.display_modification_confidence = result.confidence
    product.display_parse_version = result.parse_version


def display_frame_conflict(
    product: Product,
    competitor_has_frame: bool | None,
) -> bool:
    product_has_frame = product.display_has_frame
    if product_has_frame is None or competitor_has_frame is None:
        return False
    return product_has_frame != competitor_has_frame


def display_frame_requires_review(
    product: Product,
    competitor_has_frame: bool | None,
) -> bool:
    if product.display_modification_status == STATUS_CONFLICT and product.display_has_frame is None:
        return True
    if product.display_has_frame is None and competitor_has_frame is not None:
        return True
    if product.display_has_frame is not None and competitor_has_frame is None:
        return True
    return False
