from __future__ import annotations

import hashlib
from typing import Any


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(value.strip().split())


SKIP_ATTR_KEYS = {
    "extracted_tokens",
    "final_result",
    "llm_output",
    "notes_raw_tokens",
    "parse_status",
    "reasons",
}


def attrs_to_string(attrs: dict[str, Any] | None) -> str:
    if not attrs:
        return ""
    parts = []
    for key in sorted(attrs.keys()):
        if key in SKIP_ATTR_KEYS:
            continue
        val = attrs[key]
        if val is None:
            continue
        if isinstance(val, (list, tuple)):
            val_text = ", ".join(str(v) for v in val if v)
        else:
            val_text = str(val)
        val_text = normalize_text(val_text)
        if not val_text:
            continue
        parts.append(f"{key}: {val_text}")
    return "\n".join(parts)


def compose_competitor_text(normalized_title: str | None, attrs: dict[str, Any] | None) -> str:
    title = normalize_text(normalized_title)
    attrs_text = attrs_to_string(attrs)
    if title and attrs_text:
        return f"{title}\n{attrs_text}"
    return title or attrs_text


def compose_product_text(
    name: str | None,
    brand: str | None,
    category: str | None,
    quality: str | None,
    display_type: str | None,
    display_quality: str | None,
    display_construction: str | None,
    display_refresh_rate_hz: int | None,
    display_screen_kit: str | None = None,
    display_has_frame: bool | None = None,
    display_has_touch: bool | None = None,
    display_has_ic_pad: bool | None = None,
    display_has_binding_no_solder: bool | None = None,
    display_backlight: str | None = None,
    display_matrix_tags: list[str] | None = None,
    display_modification_status: str | None = None,
    color: str | None = None,
    article: str | None = None,
) -> str:
    parts = [normalize_text(name)]
    frame_text = (
        "display_has_frame: true"
        if display_has_frame is True
        else "display_has_frame: false" if display_has_frame is False else None
    )
    touch_text = (
        "display_has_touch: true"
        if display_has_touch is True
        else "display_has_touch: false" if display_has_touch is False else None
    )
    ic_pad_text = "display_has_ic_pad: true" if display_has_ic_pad is True else None
    binding_text = (
        "display_has_binding_no_solder: true" if display_has_binding_no_solder is True else None
    )
    matrix_tags_text = (
        "display_matrix_tags: " + ", ".join(display_matrix_tags) if display_matrix_tags else None
    )
    for value in (
        brand,
        category,
        quality,
        display_type,
        display_quality,
        display_construction,
        str(display_refresh_rate_hz) if display_refresh_rate_hz is not None else None,
        display_screen_kit,
        frame_text,
        touch_text,
        ic_pad_text,
        binding_text,
        display_backlight,
        matrix_tags_text,
        display_modification_status,
        color,
        article,
    ):
        text = normalize_text(value)
        if text:
            parts.append(text)
    return "\n".join([p for p in parts if p])


def text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
