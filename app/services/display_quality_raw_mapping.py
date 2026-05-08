from __future__ import annotations

import re

RAW_QUALITY_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"\b(?:original\s*)?\(?\s*(?:change glass|replaced glass)\s*\)?",
            re.IGNORECASE,
        ),
        "Original Refurbished",
    ),
    (re.compile(r"\(OLED\)\s*\(Full Size\)", re.IGNORECASE), "(OLED) (Full Size)"),
    (re.compile(r"\(OLED\)\s*\(Small Size\)", re.IGNORECASE), "(OLED) (Small Size)"),
    (re.compile(r"\(In-Cell\)", re.IGNORECASE), "(In-Cell)"),
    (re.compile(r"\(OLED\)", re.IGNORECASE), "(OLED)"),
    (re.compile(r"\bOR\s*\(SP\)", re.IGNORECASE), "OR (SP)"),
    (re.compile(r"\bOR100\b", re.IGNORECASE), "OR100"),
    (re.compile(r"\b100%?\s*OR\b", re.IGNORECASE), "100% OR"),
    (re.compile(r"\bOR\s*100%?\b", re.IGNORECASE), "100% OR"),
    (re.compile(r"\bPremium\s+Quality\b", re.IGNORECASE), "Premium Quality"),
    (re.compile(r"\b1\s*-\s*я\s+категория\b", re.IGNORECASE), "1-я категория"),
    (re.compile(r"\bСтандарт\b", re.IGNORECASE), "Стандарт"),
    (re.compile(r"\bПремиум\b", re.IGNORECASE), "Премиум"),
    (re.compile(r"\bОптима\b", re.IGNORECASE), "Оптима"),
    (re.compile(r"\bAAA\b", re.IGNORECASE), "AAA"),
    (re.compile(r"\bHQ\b", re.IGNORECASE), "HQ"),
    (re.compile(r"\bOriginal\b", re.IGNORECASE), "Original"),
    (re.compile(r"\borig\b", re.IGNORECASE), "orig"),
    (re.compile(r"\bOEM\b", re.IGNORECASE), "OEM"),
    (re.compile(r"\bOR\b", re.IGNORECASE), "OR"),
)

MOBA_RAW_QUALITY_TO_1C_RAW = {
    "or": "(ORIG)",
    "or100": "(ORIG100)",
    "or (sp)": "(ORIG100) (SP)",
    "стандарт": "(Medium)",
    "оптима": "(Medium)",
    "премиум": "(Premium)",
}


def _normalize_raw_key(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = re.sub(r"\s+", " ", str(value).strip()).casefold()
    return normalized or None


def extract_quality_token_as_in_name(name: str | None) -> str | None:
    if not name:
        return None
    for pattern, label in RAW_QUALITY_PATTERNS:
        if pattern.search(name):
            return label
    return None


def map_competitor_raw_quality_to_1c_raw(
    source: str | None,
    competitor_quality_raw: str | None,
) -> str | None:
    if not source or not competitor_quality_raw:
        return None
    source_key = source.strip().casefold()
    raw_key = _normalize_raw_key(competitor_quality_raw)
    if raw_key is None:
        return None
    if source_key == "moba":
        return MOBA_RAW_QUALITY_TO_1C_RAW.get(raw_key)
    return None
