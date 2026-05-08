from __future__ import annotations

import re

DISPLAY_TYPE_CANONICAL = (
    "LCD (IPS)",
    "LCD (TFT)",
    "OLED",
    "AMOLED",
    "Super AMOLED",
    "Dynamic AMOLED",
    "LTPS LCD",
    "LTPO AMOLED",
)

DISPLAY_QUALITY_CANONICAL = (
    "Original",
    "Original Refurbished",
    "OEM",
    "Copy High",
    "Copy Medium",
    "Copy Low",
)

DISPLAY_CONSTRUCTION_CANONICAL = (
    "In-Cell",
    "On-Cell",
    "COF",
    "COG",
)

DISPLAY_REFRESH_RATES_HZ = (60, 90, 120, 144)


def _normalize_text(value: str) -> str:
    return re.sub(r"[^\w]+", " ", value.lower()).strip()


def _normalize_compact(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _match_canonical(value: str, canonical: tuple[str, ...]) -> str | None:
    lower = value.strip().lower()
    for item in canonical:
        if lower == item.lower():
            return item
    return None


def _has_ascii_word(text: str, word: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", text) is not None


def normalize_display_type(value: str | None) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    canonical = _match_canonical(raw, DISPLAY_TYPE_CANONICAL)
    if canonical:
        return canonical

    text = _normalize_text(raw)
    compact = _normalize_compact(raw)

    if "ltpo" in compact:
        return "LTPO AMOLED"
    if "dynamic amoled" in text or "dynamicamoled" in compact:
        return "Dynamic AMOLED"
    if "super amoled" in text or "superamoled" in compact:
        return "Super AMOLED"
    if "amoled" in text or "amoled" in compact:
        return "AMOLED"
    if "hard oled" in text or "soft oled" in text:
        return "OLED"
    if "oled" in text or "oled" in compact:
        return "OLED"
    if "ltps" in compact:
        return "LTPS LCD"
    if "ips" in text or "ips" in compact:
        return "LCD (IPS)"
    if "tft" in text or "tft" in compact:
        return "LCD (TFT)"
    if "lcd" in text or "lcd" in compact:
        return "LCD (TFT)"
    return None


def normalize_display_quality(value: str | None) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    canonical = _match_canonical(raw, DISPLAY_QUALITY_CANONICAL)
    if canonical:
        return canonical

    text = raw.lower()

    if any(
        token in text
        for token in (
            "refurb",
            "refurbished",
            "восстанов",
            "переклей",
            "биток",
            "renewed",
            "change glass",
            "replaced glass",
        )
    ) or re.search(r"замен\w*(?:\s+\w+){0,3}\s+стекл\w*", text):
        return "Original Refurbished"
    if any(
        token in text
        for token in ("orig", "original", "ориг", "оригинал", "or100", "orig100", "ориг100")
    ):
        return "Original"
    if "oem" in text or "завод" in text or "factory" in text:
        return "OEM"

    if any(token in text for token in ("copy", "копия")):
        if _has_ascii_word(text, "high") or any(
            token in text for token in ("premium", "aaa", "hq", "hi-copy", "hicopy")
        ):
            return "Copy High"
        if _has_ascii_word(text, "low") or any(
            token in text for token in ("эконом", "econom", "cheap")
        ):
            return "Copy Low"
        if (
            _has_ascii_word(text, "medium")
            or _has_ascii_word(text, "std")
            or any(token in text for token in ("optima", "standard", "стандарт"))
        ):
            return "Copy Medium"
        return "Copy Medium"

    if any(token in text for token in ("premium", "aaa", "ааа", "hq", "hi-copy", "hicopy")):
        return "Copy High"
    if "аналог" in text:
        return "Copy Medium"
    if _has_ascii_word(text, "medium") or "средн" in text:
        return "Copy Medium"
    if _has_ascii_word(text, "high") or "высок" in text:
        return "Copy High"
    if re.search(r"\b1\s*-\s*я\s+категория\b", text):
        return "Copy Medium"
    if _has_ascii_word(text, "low") or any(
        token in text for token in ("низк", "эконом", "econom", "cheap")
    ):
        return "Copy Low"
    if "optima" in text or "standard" in text or "стандарт" in text or _has_ascii_word(text, "std"):
        return "Copy Medium"

    return None


def normalize_display_construction(value: str | None) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    canonical = _match_canonical(raw, DISPLAY_CONSTRUCTION_CANONICAL)
    if canonical:
        return canonical

    compact = _normalize_compact(raw)
    if "incell" in compact:
        return "In-Cell"
    if "oncell" in compact:
        return "On-Cell"
    if "cof" in compact:
        return "COF"
    if "cog" in compact:
        return "COG"
    return None


def normalize_refresh_rate_hz(value: object | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        hz = int(value)
    else:
        text = str(value).strip().lower()
        if not text:
            return None
        if text.isdigit():
            hz = int(text)
        else:
            match = re.search(r"\b(60|90|120|144)\s*(?:hz|гц|герц)\b", text)
            if not match:
                return None
            hz = int(match.group(1))
    return hz if hz in DISPLAY_REFRESH_RATES_HZ else None
