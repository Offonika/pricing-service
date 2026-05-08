from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Product, ProductSkuPlan
from app.services.competitor_matching import parse_model_name
from app.services.nomenclature_kind import nomenclature_kind
from app.services.phone_model_canonicalization import normalize_brand

SKU_MAX_LENGTH = 35
SKU_PATTERN = re.compile(r"^[A-Z0-9-]+$")

BRAND_CODES = {
    "f5": "F5",
    "f5energy": "F5",
    "f5 energy": "F5",
    "offonika": "OFN",
    "ofn": "OFN",
    "hoco": "OEM",
    "borofone": "OEM",
    "xo": "OEM",
    "usams": "OEM",
    "baseus": "OEM",
    "remax": "OEM",
    "cafele": "OEM",
    "kuulaa": "OEM",
    "acefast": "OEM",
    "essager": "OEM",
    "oem": "OEM",
}

CATEGORY_KEYWORDS = {
    "BAT": ("аккумулятор", "батар", "battery", "power"),
    "DSP": ("дисплей", "display", "screen", "lcd", "oled", "amoled", "тачскрин"),
    "GLS": ("glass", "стекл", "линз", "lens"),
    "CBL": ("кабель", "cable", "шнур"),
    "CHR": ("заряд", "charger", "адаптер", "gan", "pd", "qc"),
    "CAM": ("камера", "camera"),
    "FLX": ("шлейф", "flex", "connector", "коннектор"),
    "IC": ("микросх", "chip", "pmic", "контроллер", "tristar", "u2"),
    "PRT": ("рамк", "frame", "bracket", "корпус", "крышк", "держател"),
    "SET": ("комплект", "kit", "набор"),
}

QUALITY_CODES = {
    "Original": None,
    "Original Refurbished": "REF",
    "OEM": "OEM",
    "Copy High": "AAA",
    "Copy Medium": "A",
    "Copy Low": "B",
}

DISPLAY_TYPE_CODES = {
    "OLED": "OLD",
    "AMOLED": "AMD",
    "Dynamic AMOLED": "AMD",
    "Super AMOLED": "AMD",
    "LTPO AMOLED": "AMD",
    "LCD (IPS)": "IPS",
    "LCD (TFT)": "TFT",
    "LTPS LCD": "LTPS",
    "PLS": "PLS",
}

DISPLAY_GRADE_CODES = {
    "Original": "OR",
    "Original Refurbished": "RF",
    "OEM": "OEM",
    "Copy High": "CPH",
    "Copy Medium": "CPM",
    "Copy Low": "CPL",
}

DISPLAY_SERIES_CODES = ("JK", "MNK", "FOG", "GX", "SL", "ZY", "AG", "JCID", "DD", "PANDA")

CHARGER_TECH_CODES = {
    "gan": "GAN",
    "pd": "PD",
    "qc": "QC",
    "quick charge": "QC",
}

PLUG_CODES = {
    "eu": "EU",
    "euro": "EU",
    "us": "US",
    "uk": "UK",
    "cn": "CN",
}

CONNECTOR_CODES = {
    "type c": "USBC",
    "usb-c": "USBC",
    "usbc": "USBC",
    "usb c": "USBC",
    "lightning": "LTN",
    "micro usb": "MUSB",
    "microusb": "MUSB",
    "usb a": "USBA",
    "usb-a": "USBA",
    "usb": "USB",
}

COLOR_CODES = {
    "black": "BLK",
    "white": "WHT",
    "blue": "BLU",
    "green": "GRN",
    "red": "RED",
    "gold": "GLD",
    "silver": "SLV",
    "purple": "PRP",
    "pink": "PNK",
    "yellow": "YLW",
    "gray": "GRY",
    "grey": "GRY",
    "clear": "CLR",
    "transparent": "CLR",
    "черный": "BLK",
    "чёрный": "BLK",
    "белый": "WHT",
    "синий": "BLU",
    "голубой": "CYN",
    "зеленый": "GRN",
    "зелёный": "GRN",
    "красный": "RED",
    "золотой": "GLD",
    "серый": "GRY",
    "серебристый": "SLV",
    "золотистый": "GLD",
    "фиолетовый": "PRP",
    "розовый": "PNK",
    "оранжевый": "ORG",
    "бронзовый": "BRZ",
    "бежевый": "BEI",
    "прозрачный": "CLR",
}

BRAND_PREFIXES = {
    "apple": "IPH",
    "samsung": "SMG",
    "xiaomi": "XMI",
    "meizu": "MEI",
    "honor": "HNR",
    "huawei": "HWE",
    "realme": "RLM",
    "oppo": "OPP",
    "vivo": "VVO",
    "tecno": "TEC",
    "infinix": "INF",
    "oneplus": "ONE",
    "google": "GGL",
    "nokia": "NOK",
    "lenovo": "LEN",
    "zte": "ZTE",
    "asus": "ASU",
    "sony": "SON",
    "motorola": "MOT",
    "lg": "LGE",
    "fly": "FLY",
    "jbl": "JBL",
}


class SkuValidationError(ValueError):
    pass


@dataclass
class SkuGenerationResult:
    planned_sku: str | None
    status: str
    brand_code: str | None = None
    category_code: str | None = None
    device_code: str | None = None
    key_code: str | None = None
    rev: str | None = None
    reasons: list[str] = field(default_factory=list)
    source: str = "rules"


def _normalize_free_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = re.sub(r"\s+", " ", str(value).strip())
    return cleaned or None


def _slug_token(value: str | None, *, max_len: int | None = None) -> str | None:
    cleaned = _normalize_free_text(value)
    if not cleaned:
        return None
    cleaned = cleaned.upper()
    cleaned = cleaned.replace("Ё", "E")
    cleaned = re.sub(r"[^A-Z0-9]+", "-", cleaned)
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-")
    if not cleaned:
        return None
    if max_len is not None:
        cleaned = cleaned[:max_len].rstrip("-")
    return cleaned or None


def _find_code(value: str | None, mapping: dict[str, str]) -> str | None:
    cleaned = _normalize_free_text(value)
    if not cleaned:
        return None
    lower = cleaned.lower()
    if lower in mapping:
        return mapping[lower]
    compact = lower.replace("-", " ").replace("_", " ")
    compact = re.sub(r"\s+", " ", compact).strip()
    return mapping.get(compact)


def _sanitize_numeric_token(value: str | None, suffix: str = "") -> str | None:
    cleaned = _normalize_free_text(value)
    if not cleaned:
        return None
    digits = "".join(ch for ch in cleaned if ch.isdigit())
    if not digits:
        return None
    return f"{digits}{suffix}"


def _parse_int(value: str | int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    match = re.search(r"\d+", str(value))
    return int(match.group()) if match else None


def _normalize_length(value: str | None) -> str | None:
    cleaned = _normalize_free_text(value)
    if not cleaned:
        return None
    lower = cleaned.lower().replace(",", ".")
    match = re.search(r"(\d+(?:\.\d+)?)\s*(m|м|cm|см)", lower)
    if not match:
        return _slug_token(cleaned, max_len=8)
    num = match.group(1)
    unit = match.group(2)
    if unit in {"cm", "см"}:
        try:
            meters = float(num) / 100
            if meters.is_integer():
                num = str(int(meters))
            else:
                num = str(meters).rstrip("0").rstrip(".")
        except ValueError:
            return None
    return f"{num.upper()}M".replace(".0M", "M")


def _normalize_mp(value: str | int | None) -> str | None:
    megapixels = _parse_int(value)
    return f"{megapixels}MP" if megapixels else None


def _parenthesized_tokens(name: str | None) -> list[str]:
    return [
        _normalize_free_text(token).lower()
        for token in re.findall(r"\(([^()]+)\)", name or "")
        if _normalize_free_text(token)
    ]


def _has_parenthesized_token(name: str | None, *patterns: str) -> bool:
    tokens = _parenthesized_tokens(name)
    return any(any(pattern in token for pattern in patterns) for token in tokens)


def _compact_parts(parts: Iterable[str | None]) -> list[str]:
    return [part for part in parts if part]


def build_sku(
    brand_code: str,
    category_code: str,
    device_code: str,
    key_code: str,
    rev: str | None = None,
) -> str:
    parts = _compact_parts(
        [
            _slug_token(brand_code, max_len=8),
            _slug_token(category_code, max_len=8),
            _slug_token(device_code, max_len=32),
            _slug_token(key_code, max_len=64),
            _slug_token(rev, max_len=16),
        ]
    )
    if len(parts) < 4:
        raise SkuValidationError("missing required sku parts")
    sku = "-".join(parts)
    return validate_sku(sku)


def validate_sku(value: str) -> str:
    cleaned = value.strip().upper()
    if not cleaned:
        raise SkuValidationError("sku is empty")
    if len(cleaned) > SKU_MAX_LENGTH:
        raise SkuValidationError("sku exceeds max length")
    if not SKU_PATTERN.fullmatch(cleaned):
        raise SkuValidationError("sku contains invalid characters")
    return cleaned


def infer_brand_code(product: Product) -> str | None:
    for value in (product.brand, product.manufacturer, product.name):
        code = _find_code(value, BRAND_CODES)
        if code:
            return code

    if len(product.phone_model_links) == 1:
        return "OEM"
    if product.compatibilities:
        return "OEM"

    parsed = parse_model_name(product.name)
    if not parsed.ambiguous and parsed.brand:
        return "OEM"

    if any((product.category, product.subject, product.vid_nomenklatury)):
        return "OEM"
    return None


def infer_category_code(product: Product) -> str | None:
    lower_name = (product.name or "").lower()
    if lower_name.startswith("шлейф") and "диспле" in lower_name:
        return "FLX"

    explicit_display_candidates = [
        product.subject,
        product.vid_nomenklatury,
        getattr(product, "subject_1c", None),
        getattr(product, "vid_nomenklatury_1c", None),
    ]
    lowered_explicit = [c.lower() for c in explicit_display_candidates if c]
    if any(
        any(keyword in candidate for keyword in CATEGORY_KEYWORDS["DSP"])
        for candidate in lowered_explicit
    ):
        return "DSP"

    candidates = [
        product.category,
        product.subject,
        product.vid_nomenklatury,
        nomenclature_kind(product.subject, product.name),
        product.name,
    ]
    lowered_candidates = [c.lower() for c in candidates if c]
    for code, keywords in CATEGORY_KEYWORDS.items():
        if any(
            any(keyword in candidate for keyword in keywords) for candidate in lowered_candidates
        ):
            return code
    return None


def _has_duplicate_marker(name: str | None) -> bool:
    if not name:
        return False
    lower = re.sub(r"[^\w\s]+", " ", name.lower())
    return (
        "дубл" in lower
        or "дублик" in lower
        or "duplicate" in lower
        or "dupl" in lower
        or "списать" in lower
    )


def _device_code_from_model(
    brand: str | None, model_name: str | None, variant: str | None
) -> str | None:
    brand_norm = normalize_brand(brand)
    model_value = _normalize_free_text(model_name)
    if not brand_norm or not model_value:
        return None
    prefix = BRAND_PREFIXES.get(brand_norm, _slug_token(brand_norm, max_len=3))
    if not prefix:
        return None
    if brand_norm == "apple":
        suffix = _apple_device_suffix(model_value, variant)
        if suffix:
            return _slug_token(suffix, max_len=24)
    if brand_norm == "samsung":
        suffix = _samsung_device_suffix(model_value, variant)
        if suffix:
            return _slug_token(f"{prefix}-{suffix}", max_len=24)
    if brand_norm == "xiaomi":
        suffix = _xiaomi_device_suffix(model_value, variant)
        if suffix:
            return _slug_token(f"{prefix}-{suffix}", max_len=24)
    if brand_norm in {"huawei", "honor"}:
        suffix = _huawei_device_suffix(model_value, variant)
        if suffix:
            return _slug_token(f"{prefix}-{suffix}", max_len=24)
    if brand_norm in {"oppo", "realme", "oneplus"}:
        suffix = _oppo_device_suffix(model_value, variant, brand_norm)
        if suffix:
            return _slug_token(f"{prefix}-{suffix}", max_len=24)
    if brand_norm == "google":
        suffix = _google_device_suffix(model_value, variant)
        if suffix:
            return _slug_token(f"{prefix}-{suffix}", max_len=24)
    if brand_norm == "nokia":
        suffix = _nokia_device_suffix(model_value, variant)
        if suffix:
            return _slug_token(f"{prefix}-{suffix}", max_len=24)
    if brand_norm == "meizu":
        suffix = _meizu_device_suffix(model_value, variant)
        if suffix:
            return _slug_token(f"{prefix}-{suffix}", max_len=24)
    if brand_norm == "lenovo":
        suffix = _lenovo_device_suffix(model_value, variant)
        if suffix:
            return _slug_token(f"{prefix}-{suffix}", max_len=24)
    if brand_norm == "zte":
        suffix = _zte_device_suffix(model_value, variant)
        if suffix:
            return _slug_token(f"{prefix}-{suffix}", max_len=24)
    if brand_norm == "asus":
        suffix = _asus_device_suffix(model_value, variant)
        if suffix:
            return _slug_token(f"{prefix}-{suffix}", max_len=24)
    if brand_norm == "sony":
        suffix = _sony_device_suffix(model_value, variant)
        if suffix:
            return _slug_token(f"{prefix}-{suffix}", max_len=24)
    if brand_norm in {"motorola", "lg", "fly", "jbl"}:
        suffix = _misc_legacy_device_suffix(brand_norm, model_value, variant)
        if suffix:
            return _slug_token(f"{prefix}-{suffix}", max_len=24)
    if brand_norm in {"vivo", "tecno", "infinix"}:
        suffix = _transsion_vivo_device_suffix(model_value, variant, brand_norm)
        if suffix:
            return _slug_token(f"{prefix}-{suffix}", max_len=24)
    compact = re.sub(r"[^a-z0-9]+", " ", model_value.lower()).strip()
    compact = compact.replace("iphone ", "")
    tokens = [tok for tok in compact.split() if tok]
    if variant:
        tokens.extend(tok for tok in re.sub(r"[^a-z0-9]+", " ", variant.lower()).split() if tok)
    mapped: list[str] = []
    variant_map = {"mini": "MN", "pro": "P", "max": "M", "plus": "PL", "ultra": "U", "lite": "LT"}
    for token in tokens:
        if token in {"iphone", "galaxy", "redmi", "note", "mi", "mate", "nova", "watch"}:
            continue
        mapped.append(variant_map.get(token, token.upper()))
    suffix = "".join(mapped)
    if not suffix:
        return None
    separator = "" if brand_norm == "apple" else "-"
    return _slug_token(f"{prefix}{separator}{suffix}", max_len=32)


def _apple_device_suffix(model_value: str, variant: str | None) -> str | None:
    text = f"{model_value} {variant or ''}".lower()
    normalized = re.sub(r"[^a-z0-9+./ ]+", " ", text)
    normalized = re.sub(r"\b(?:sim|esim)\b", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()

    if "watch 5" in normalized and "watch se 2020" in normalized and "watch se 2022" in normalized:
        if "44 мм" in text or "44 mm" in normalized:
            return "IPHW5SE44"
        return "IPHW5SE40"

    if "совместим с ipad pro 11" in text:
        return "IPDP1118"

    patterns: list[tuple[str, str | callable]] = [
        (
            r"\bapple\s+watch\s+se\s*(2025)\b.*\b(40|44)\b",
            lambda m: f"IPHWSE25{m.group(2)}",
        ),
        (
            r"\bwatch\s+se\s*(2025)\b.*\b(40|44)\b",
            lambda m: f"IPHWSE25{m.group(2)}",
        ),
        (r"\bapple\s+watch\s*10.*\b(42|46)\b", lambda m: f"IPHW10{m.group(1)}"),
        (r"\bwatch\s*10.*\b(42|46)\b", lambda m: f"IPHW10{m.group(1)}"),
        (
            r"\bapple\s+watch\s+se\s*(2023)\b.*\b(40|44)\b",
            lambda m: f"IPHWSE23{m.group(2)}",
        ),
        (
            r"\bwatch\s+se\s*(2023)\b.*\b(40|44)\b",
            lambda m: f"IPHWSE23{m.group(2)}",
        ),
        (r"\biphone\s*xs\s*max\b", "IPHXSM"),
        (r"\biphone\s*xs\b", "IPHXS"),
        (r"\biphone\s*xr\b", "IPHXR"),
        (r"\biphone\s*x\b", "IPHX"),
        (r"\biphone\s*6s\s*plus\b", "IPH6SP"),
        (r"\biphone\s*6s\b", "IPH6S"),
        (r"\biphone\s*5s\s*/\s*iphone\s*se\b", "IPH5"),
        (r"\biphone\s*se\s*/\s*iphone\s*5s\b", "IPH5"),
        (r"\biphone\s*17\s*pro\s*max\b", "IPH17PM"),
        (r"\biphone\s*17\s*pro\b", "IPH17P"),
        (r"\biphone\s*17\s*air\b", "IPH17AIR"),
        (r"\biphone\s*17\b", "IPH17"),
        (r"\biphone\s*(\d+)\s*pro\s*max\b", lambda m: f"IPH{m.group(1)}PM"),
        (r"\biphone\s*(\d+)\s*pro\b", lambda m: f"IPH{m.group(1)}P"),
        (r"\biphone\s*(\d+)\s*plus\b", lambda m: f"IPH{m.group(1)}PL"),
        (r"\biphone\s*(\d+)\s*mini\b", lambda m: f"IPH{m.group(1)}MN"),
        (r"\biphone\s*(\d+)\s*e\b", lambda m: f"IPH{m.group(1)}E"),
        (r"\biphone\s*se\s*(2022)\b", "IPHSE22"),
        (r"\biphone\s*se\s*(2020)\b", "IPHSE20"),
        (r"\biphone\s*se\b", "IPHSE"),
        (r"\biphone\s*(\d+)\b", lambda m: f"IPH{m.group(1)}"),
        (
            r"\bwatch\s*5.*watch\s*se\s*2020.*watch\s*se\s*2022.*\b(40|44)\b",
            lambda m: f"IPHW5SE{m.group(1)}",
        ),
        (
            r"\bapple\s+watch\s*(\d+).*\b(38|40|41|42|44|45|46)\b",
            lambda m: f"IPHW{m.group(1)}{m.group(2)}",
        ),
        (r"\bwatch\s*(\d+).*\b(38|40|41|42|44|45|46)\b", lambda m: f"IPHW{m.group(1)}{m.group(2)}"),
        (r"\bipod\s+touch\s*5.*ipod\s+touch\s*6\b", "IPOD56"),
        (r"\bipod\s+touch\s*6.*ipod\s+touch\s*7\b", "IPOD67"),
        (r"\bairpods\s+pro\s*2\b", "AIRP2"),
        (r"\bairpods\s+pro\b", "AIRPP"),
        (r"\bairpods\s*3\b", "AIRP3"),
        (r"\bairpods\s*2\b", "AIRP2"),
        (r"\bairpods\b", "AIRP"),
        (
            r"\bmacbook\s+air\s*(11|13).*\b(a\d{4})\b",
            lambda m: f"MBA{m.group(1)}{m.group(2).upper()}",
        ),
        (
            r"\bmacbook\s+pro(?:\s+retina|\s+touch\s+bar)?\s*(13|14|15|16|17).*\b(a\d{4})\b",
            lambda m: f"MBP{m.group(1)}{m.group(2).upper()}",
        ),
        (r"\bmacbook\s*12\s*retina.*\b(a\d{4})\b", lambda m: f"MB12{m.group(1).upper()}"),
        (r"\bmacbook\s*12\b.*\b(a\d{4})\b", lambda m: f"MB12{m.group(1).upper()}"),
        (r"\bсовместим\s+с\s+ipad\s+pro\s*11", "IPDP1118"),
        (r"\bipad\s*mini\s*2.*ipad\s*mini\s*3\b", "IPDMN23"),
        (r"\bipad\s*air\s*3\b", "IPDA3"),
        (r"\bipad\s*2\b", "IPD2"),
        (r"\bipad\s*\(a1219/a1337\)\b", "IPD1"),
        (r"\bipad\b.*\b(a1219|a1337)\b", "IPD1"),
        (r"\bipad\s*3.*ipad\s*4\b", "IPD34"),
        (r"\bipad\s*air.*ipad\s*5\s*9\.?7\b", "IPDA15"),
        (r"\bipad\s*6\b.*\b9\.?7\b", "IPD697"),
        (r"\bipad\s*[789].*\b10\.?2\b", "IPD789102"),
        (r"\bipad\s*mini\s*7\b", "IPDMN7"),
        (r"\bipad\s*mini\s*5\b", "IPDMN5"),
        (r"\bipad\s*mini\s*4\b", "IPDMN4"),
        (r"\bipad\s*mini\b", "IPDMN"),
        (r"\bipad\s*air\s*7.*\b13\.?0\b", "IPDA713"),
        (r"\bipad\s*air\s*7.*\b11\.?0\b", "IPDA711"),
        (r"\bipad\s*air\s*6.*\b13\.?0\b", "IPDA613"),
        (r"\bipad\s*air\s*6.*\b11\.?0\b", "IPDA611"),
        (r"\bipad\s*air\s*5.*\b10\.?9\b", "IPDA5109"),
        (r"\bipad\s*air\s*4.*\b10\.?9\b", "IPDA4109"),
        (r"\bipad\s*air\s*3.*\b10\.?5\b", "IPDA3105"),
        (r"\bipad\s*air\s*2\b", "IPDA2"),
        (r"\bipad\s*pro.*\b12\.?9\b.*\b2015\b", "IPDP12915"),
        (r"\bipad\s*pro.*\b12\.?9\b.*\b(2018|2020)\b", "IPDP12918"),
        (r"\bipad\s*pro.*\b12\.?9\b.*\b(2021|2022)\b", "IPDP12921"),
        (r"\bipad\s*pro.*\b11[.,]?0\b.*\b(2018|2020)\b", "IPDP1118"),
        (r"\bipad\s*pro.*\b13\.?0\b", "IPDP13"),
        (r"\bipad\s*pro.*\b12\.?9\b", "IPDP129"),
        (r"\bipad\s*pro.*\b11\.?0\b", "IPDP11"),
        (r"\bipad\s*pro.*\b10\.?5\b", "IPDP105"),
        (r"\bipad\s*pro.*\b9\.?7\b", "IPDP97"),
        (r"\bipad\s*11.*\b11\.?0\b", "IPD11"),
        (r"\bipad\s*10.*\b10\.?9\b", "IPD10"),
    ]

    for pattern, replacement in patterns:
        if match := re.search(pattern, normalized):
            if callable(replacement):
                return replacement(match)
            return replacement
    return None


def _samsung_device_suffix(model_value: str, variant: str | None) -> str | None:
    text = f"{model_value} {variant or ''}".lower()
    normalized = re.sub(r"[^a-z0-9+ ]+", " ", text)
    normalized = re.sub(r"\s+", " ", normalized).strip()

    if match := re.search(r"\b(x\d{3})\b", normalized):
        return match.group(1).upper()
    if match := re.search(r"\b(i\d{4})\b", normalized):
        return match.group(1).upper()
    if match := re.search(r"\b(e\d{3})\b", normalized):
        return match.group(1).upper()
    if match := re.search(r"\b(c\d{4})\b", normalized):
        return match.group(1).upper()
    if match := re.search(r"\b(d\d{3,4})\b", normalized):
        return match.group(1).upper()
    if match := re.search(r"\b(b\d{3,4})\b", normalized):
        return match.group(1).upper()
    if match := re.search(r"\b(f761|f766)\b", normalized):
        return {"F761": "ZF7FE", "F766": "ZF7"}[match.group(1).upper()]
    if match := re.search(r"\b(f721|f726)\b", normalized):
        return "ZF4"
    if match := re.search(r"\b(f711|f716)\b", normalized):
        return "ZF3"
    if match := re.search(r"\b(g770)\b", normalized):
        return "S10LT"
    if match := re.search(r"\b(s901|s906|s908)\b", normalized):
        return {"S901": "S22", "S906": "S22P", "S908": "S22U"}[match.group(1).upper()]
    if match := re.search(r"\b(s911|s918)\b", normalized):
        return {"S911": "S23", "S918": "S23U"}[match.group(1).upper()]
    if match := re.search(r"\b(s921|s926)\b", normalized):
        return {"S921": "S24", "S926": "S24P"}[match.group(1).upper()]
    if match := re.search(r"\b(s931|s936|s937|s938)\b", normalized):
        return {
            "S931": "S25",
            "S936": "S25P",
            "S937": "S25E",
            "S938": "S25U",
        }[match.group(1).upper()]
    if match := re.search(r"(?:galaxy\s+)?z\s+flip\s+(\d+)\s*fe", normalized):
        return f"ZF{match.group(1)}FE"
    if match := re.search(r"(?:galaxy\s+)?z\s+flip\s+(\d+)", normalized):
        return f"ZF{match.group(1)}"
    if match := re.search(r"(?:galaxy\s+)?z\s+fold\s+(\d+)", normalized):
        return f"ZD{match.group(1)}"
    if match := re.search(r"\b(g970|g973|g975)\b", normalized):
        return {"G970": "S10E", "G973": "S10", "G975": "S10P"}[match.group(1).upper()]
    if match := re.search(r"\b(g980|g981|g985|g986|g988)\b", normalized):
        return {
            "G980": "S20",
            "G981": "S20",
            "G985": "S20P",
            "G986": "S20P",
            "G988": "S20U",
        }[match.group(1).upper()]
    if match := re.search(r"\b(g990|g991|g996|g998)\b", normalized):
        return {
            "G990": "S21FE",
            "G991": "S21",
            "G996": "S21P",
            "G998": "S21U",
        }[match.group(1).upper()]
    if match := re.search(r"(?:galaxy\s+)?s\s*(\d{2})\s*fe(?:\s*fe)?", normalized):
        return f"S{match.group(1)}FE"
    if match := re.search(r"(?:galaxy\s+)?s\s*(\d{2})\s*e\b", normalized):
        return f"S{match.group(1)}E"
    if match := re.search(r"(?:galaxy\s+)?s\s*(\d{2})\s*ultra", normalized):
        return f"S{match.group(1)}U"
    if match := re.search(r"(?:galaxy\s+)?s\s*(\d{2})\s*\+", normalized):
        return f"S{match.group(1)}P"
    if match := re.search(r"galaxy\s+s\s*(\d{2})", normalized):
        return f"S{match.group(1)}"
    if match := re.search(r"galaxy\s+note\s*(\d{2})\s*ultra", normalized):
        return f"N{match.group(1)}U"
    if match := re.search(r"galaxy\s+note\s*(\d{2})", normalized):
        return f"N{match.group(1)}"
    if match := re.search(r"\b(a\d{3}[a-z]?)\b", normalized):
        return match.group(1).upper()
    if match := re.search(r"\b(m\d{3}[a-z]?)\b", normalized):
        return match.group(1).upper()
    if match := re.search(r"\b(j\d{3}[a-z]?)\b", normalized):
        return match.group(1).upper()
    if match := re.search(r"galaxy\s+(a\d{2,3})", normalized):
        return match.group(1).upper()
    if match := re.search(r"galaxy\s+(m\d{2,3})", normalized):
        return match.group(1).upper()
    if match := re.search(r"galaxy\s+(j\d{2,3})", normalized):
        return match.group(1).upper()
    if match := re.search(r"galaxy\s+(t\d{3,4})", normalized):
        return match.group(1).upper()
    if match := re.search(r"galaxy\s+(p\d{3,4})", normalized):
        return match.group(1).upper()
    if match := re.search(r"galaxy\s+(r\d{3,4})", normalized):
        return match.group(1).upper()
    if match := re.search(r"galaxy\s+(l\d{3,4})", normalized):
        return match.group(1).upper()
    if match := re.search(r"\b(np\d{3}[a-z]?)\b", normalized):
        return match.group(1).upper()
    if match := re.search(r"\bsm[- ]?([a-z]\d{3,4}[a-z]?)\b", normalized):
        return match.group(1).upper()
    if match := re.search(r"\b([agjmnprstfl]\d{3,4}[a-z]?)\b", normalized):
        return match.group(1).upper()
    return None


def _xiaomi_device_suffix(model_value: str, variant: str | None) -> str | None:
    text = f"{model_value} {variant or ''}".lower()
    normalized = re.sub(r"[^a-z0-9+ ]+", " ", text)
    normalized = re.sub(r"\s+", " ", normalized).strip()

    patterns: list[tuple[str, str | callable]] = [
        (r"\bredmi\s+watch\s*(\d+)\s*lite\b", lambda m: f"RW{m.group(1)}L"),
        (r"\bredmi\s+watch\s*(\d+)\s*active\b", lambda m: f"RW{m.group(1)}A"),
        (r"\bredmi\s+watch\s*(\d+)\b", lambda m: f"RW{m.group(1)}"),
        (r"\bwatch\s*(\d+)\s*pro\s*lte\b", lambda m: f"W{m.group(1)}PL"),
        (r"\bwatch\s*s(\d+)\s*pro\b", lambda m: f"WS{m.group(1)}P"),
        (r"\bwatch\s*s(\d+)\b", lambda m: f"WS{m.group(1)}"),
        (r"\bwatch\s*(\d+)\s*pro\b", lambda m: f"W{m.group(1)}P"),
        (r"\bwatch\s*(\d+)\b", lambda m: f"W{m.group(1)}"),
        (r"\bmipad\s*(\d+)\s*plus\b", lambda m: f"MPAD{m.group(1)}P"),
        (r"\bmipad\s*(\d+)\b", lambda m: f"MPAD{m.group(1)}"),
        (r"\bpad\s*(\d+)\s*pro\b", lambda m: f"PAD{m.group(1)}P"),
        (r"\bpad\s*(\d+)\s*lte\b", lambda m: f"PAD{m.group(1)}L"),
        (r"\bpad\s*(\d+)\b", lambda m: f"PAD{m.group(1)}"),
        (r"\bredmi\s+pad\s+se\s*8\.?7\b", "RPSE87"),
        (r"\bredmi\s+pad\s+se\b", "RPSE"),
        (r"\bredmi\s+pad\s+2\s+pro\b", "RPAD2P"),
        (r"\bredmi\s+pad\s+2\b", "RPAD2"),
        (r"\bpoco\s+pad\b", "PPAD"),
        (r"\bmix\s+flip\b", "MXFL"),
        (r"\bmi\s+mix\s*(\d+)(s)?\b", lambda m: f"MIX{m.group(1)}{'S' if m.group(2) else ''}"),
        (r"\bmi\s+note\s*(\d+)\s*lite\b", lambda m: f"MN{m.group(1)}LT"),
        (r"\bmi\s+note\s*(\d+)\s*pro\b", lambda m: f"MN{m.group(1)}P"),
        (r"\bmi\s+note\s*(\d+)\b", lambda m: f"MN{m.group(1)}"),
        (r"\bmi\s+note\b", "MNOTE"),
        (r"\bpocophone\s+f(\d+)\b", lambda m: f"PF{m.group(1)}"),
        (r"\bpoco\s+f(\d+)\s*ultra\b", lambda m: f"PF{m.group(1)}U"),
        (r"\bpoco\s+f(\d+)\s*pro\b", lambda m: f"PF{m.group(1)}P"),
        (r"\bpoco\s+f(\d+)\b", lambda m: f"PF{m.group(1)}"),
        (r"\bpoco\s+x(\d+)\s*pro\b", lambda m: f"PX{m.group(1)}P"),
        (r"\bpoco\s+x(\d+)\s*gt\b", lambda m: f"PX{m.group(1)}GT"),
        (r"\bpoco\s+x(\d+)\s*nfc\b", lambda m: f"PX{m.group(1)}N"),
        (r"\bpoco\s+x(\d+)\b", lambda m: f"PX{m.group(1)}"),
        (r"\bpoco\s+m(\d+)\s*pro\s*5g\b", lambda m: f"PM{m.group(1)}P5"),
        (r"\bpoco\s+m(\d+)\s*pro\s*4g\b", lambda m: f"PM{m.group(1)}P4"),
        (r"\bpoco\s+m(\d+)\s*pro\b", lambda m: f"PM{m.group(1)}P"),
        (r"\bpoco\s+m(\d+)\s*5g\b", lambda m: f"PM{m.group(1)}5"),
        (r"\bpoco\s+m(\d+)\b", lambda m: f"PM{m.group(1)}"),
        (r"\bpoco\s+c(\d+)\b", lambda m: f"PC{m.group(1)}"),
        (r"\bredmi\s+note\s*(\d+)\s*a\s*prime\b", lambda m: f"RN{m.group(1)}AP"),
        (r"\bredmi\s+note\s*(\d+)\s*a\b", lambda m: f"RN{m.group(1)}A"),
        (r"\bredmi\s+note\s*(\d+)\s*x\b", lambda m: f"RN{m.group(1)}X"),
        (r"\bredmi\s+note\s*(\d+)\s*pro\+\s*5g\b", lambda m: f"RN{m.group(1)}PP5"),
        (r"\bredmi\s+note\s*(\d+)\s*pro\+\b", lambda m: f"RN{m.group(1)}PP"),
        (r"\bredmi\s+note\s*(\d+)\s*pro\s*5g\b", lambda m: f"RN{m.group(1)}P5"),
        (r"\bredmi\s+note\s*(\d+)\s*pro\s*4g\b", lambda m: f"RN{m.group(1)}P4"),
        (r"\bredmi\s+note\s*(\d+)\s*pro\b", lambda m: f"RN{m.group(1)}P"),
        (r"\bredmi\s+note\s*(\d+)\s*s\s*5g\b", lambda m: f"RN{m.group(1)}S5"),
        (r"\bredmi\s+note\s*(\d+)\s*s\b", lambda m: f"RN{m.group(1)}S"),
        (r"\bredmi\s+note\s*(\d+)\s*lite\b", lambda m: f"RN{m.group(1)}LT"),
        (r"\bredmi\s+note\s*(\d+)\s*5g\b", lambda m: f"RN{m.group(1)}5"),
        (r"\bredmi\s+note\s*(\d+)\s*4g\b", lambda m: f"RN{m.group(1)}4"),
        (r"\bredmi\s+note\s*(\d+)\b", lambda m: f"RN{m.group(1)}"),
        (r"\bredmi\s+a(\d+)(x)?\b", lambda m: f"RA{m.group(1)}{'X' if m.group(2) else ''}"),
        (r"\bredmi\s+4\s*a\b", "R4A"),
        (r"\bredmi\s+4\s*prime\b", "R4P"),
        (r"\bredmi\s+4\s*pro\b", "R4P"),
        (r"\bredmi\s+10\s*2022\b", "R1022"),
        (r"\bredmi\s+(\d+)\s*x\b", lambda m: f"R{m.group(1)}X"),
        (r"\bredmi\s+(\d+)\s*plus\b", lambda m: f"R{m.group(1)}P"),
        (r"\bredmi\s+(\d+)\s*lite\b", lambda m: f"R{m.group(1)}LT"),
        (r"\bredmi\s+(\d+)\s*pro\b", lambda m: f"R{m.group(1)}P"),
        (r"\bredmi\s+(\d+)\s*c\b", lambda m: f"R{m.group(1)}C"),
        (r"\bredmi\s+(\d+)\s*t\b", lambda m: f"R{m.group(1)}T"),
        (r"\bredmi\s+(\d+)\b", lambda m: f"R{m.group(1)}"),
        (r"\bmi\s+a(\d+)\s*lite\b", lambda m: f"MA{m.group(1)}LT"),
        (r"\bmi\s+a(\d+)\b", lambda m: f"MA{m.group(1)}"),
        (r"\bmi\s+(\d+)\s*se\b", lambda m: f"{m.group(1)}SE"),
        (r"\bmi\s+(\d+)\s*pro\b", lambda m: f"{m.group(1)}P"),
        (r"\bmi\s+(\d+)\s*s\s*ultra\b", lambda m: f"{m.group(1)}SU"),
        (r"\bmi\s+(\d+)\s*ultra\b", lambda m: f"{m.group(1)}U"),
        (r"\bmi\s+(\d+)\s*lite\s*5g\s*ne\b", lambda m: f"{m.group(1)}LTNE"),
        (r"\bmi\s+(\d+)\s*lite\s*5g\b", lambda m: f"{m.group(1)}LT5"),
        (r"\bmi\s+(\d+)\s*lite\b", lambda m: f"{m.group(1)}LT"),
        (r"\bmi\s+(\d+)\s*t\s*pro\b", lambda m: f"{m.group(1)}TP"),
        (r"\bmi\s+(\d+)\s*t\b", lambda m: f"{m.group(1)}T"),
        (r"\bmi\s+(\d+)\s*x\b", lambda m: f"{m.group(1)}X"),
        (r"\bmi\s+(\d+)\s*i\b", lambda m: f"{m.group(1)}I"),
        (r"\bmi\s+(\d+)\b", lambda m: f"{m.group(1)}"),
        (r"\b(\d+)\s*ultra\b", lambda m: f"{m.group(1)}U"),
        (r"\b(\d+)\s*s\s*ultra\b", lambda m: f"{m.group(1)}SU"),
        (r"\b(\d+)\s*pro\s*max\b", lambda m: f"{m.group(1)}PM"),
        (r"\b(\d+)\s*pro\+\b", lambda m: f"{m.group(1)}PP"),
        (r"\b(\d+)\s*pro\b", lambda m: f"{m.group(1)}P"),
        (r"\b(\d+)\s*se\b", lambda m: f"{m.group(1)}SE"),
        (r"\b(\d+)\s*lite\s*5g\s*ne\b", lambda m: f"{m.group(1)}LTNE"),
        (r"\b(\d+)\s*lite\b", lambda m: f"{m.group(1)}LT"),
        (r"\b(\d+)\s*t\s*pro\b", lambda m: f"{m.group(1)}TP"),
        (r"\b(\d+)\s*t\b", lambda m: f"{m.group(1)}T"),
        (r"\b(\d+)\s*x\b", lambda m: f"{m.group(1)}X"),
        (r"\b(\d+)\b", lambda m: f"{m.group(1)}"),
    ]

    for pattern, replacement in patterns:
        if match := re.search(pattern, normalized):
            if callable(replacement):
                return replacement(match)
            return replacement

    if match := re.search(r"\b([a-z]\d{3,}[a-z0-9]*)\b", normalized):
        return match.group(1).upper()
    return None


def _huawei_device_suffix(model_value: str, variant: str | None) -> str | None:
    text = f"{model_value} {variant or ''}".lower()
    normalized = re.sub(r"[^a-z0-9+./ ]+", " ", text)
    normalized = re.sub(r"\s+", " ", normalized).strip()

    patterns: list[tuple[str, str | callable]] = [
        (r"\bhonor\s+pad\s+x(\d+)\s*lite\b", lambda m: f"HPX{m.group(1)}L"),
        (r"\bmatepad\s*11\.5s\b", "MP115S"),
        (r"\bmatepad\s*10\.4(?:\s*\(?(2022)\)?)\b", "MP10422"),
        (r"\bmatepad\s*10\.4(?:\s*\(?(2020)\)?)\b", "MP10420"),
        (r"\bmediapad\s+m3\s*lite\s*10", "MM310"),
        (r"\bmediapad\s+m3\s*lite\s*8", "MM38"),
        (r"\bmediapad\s+m5\s*lite\s*10", "MM510"),
        (r"\bmediapad\s+m5\s*lite\s*8", "MM58"),
        (r"\by9\s*(2019)\b", "Y919"),
        (r"\by9\s*(2018)\b", "Y918"),
        (r"\bhonor\s+choice\s+watch\s*2\s*pro\b", "HCW2P"),
        (r"\bhonor\s+choice\s+watch\s*2i\b", "HCW2I"),
        (r"\bhonor\s+choice\s+watch\b", "HCW"),
        (r"\bhonor\s+watch\s+gs\s*(\d+)\b", lambda m: f"HWGS{m.group(1)}"),
        (r"\bhonor\s+watch\s*(\d+)\b", lambda m: f"HW{m.group(1)}"),
        (r"\bhonor\s+pad\s+x(\d+)([a-z]?)\b", lambda m: f"HPX{m.group(1)}{m.group(2).upper()}"),
        (r"\bhonor\s+pad\s+v(\d+)\b", lambda m: f"HPV{m.group(1)}"),
        (r"\bhonor\s+magicpad\s*(\d+)\b", lambda m: f"HMP{m.group(1)}"),
        (r"\bhonor\s+pad\s*(\d+)", lambda m: f"HPAD{m.group(1)}"),
        (r"\bhonor\s+magic\s*v(\d+)", lambda m: f"HMV{m.group(1)}"),
        (r"\bhonor\s+magic\s*(\d+)\s*lite\b", lambda m: f"HM{m.group(1)}LT"),
        (r"\bhonor\s+magic\s*(\d+)\b", lambda m: f"HM{m.group(1)}"),
        (
            r"\bmediapad\s+([tm])\s*(\d+)\s*(\d+(?:\.\d+)?)?",
            lambda m: f"M{m.group(1).upper()}{m.group(2)}{(m.group(3) or '').replace('.', '').rstrip('0')}",
        ),
        (
            r"\bmediapad\s+x\s*(\d+)\s*(\d+(?:\.\d+)?)?",
            lambda m: f"MX{m.group(1)}{(m.group(2) or '').replace('.', '').rstrip('0')}",
        ),
        (r"\bwatch\s+fit\s*special\s*edition\b", "WFSE"),
        (r"\bwatch\s+fit\s*(\d+)\s*pro\b", lambda m: f"WF{m.group(1)}P"),
        (r"\bwatch\s+fit\s*(\d+)\b", lambda m: f"WF{m.group(1)}"),
        (r"\bwatch\s+d\s*(\d+)\b", lambda m: f"WD{m.group(1)}"),
        (r"\bwatch\s+ultimate\s+disign\b", "WUD"),
        (r"\bwatch\s+ultimate\s+design\b", "WUD"),
        (r"\bwatch\s+ultimate\s+steel\b", "WUS"),
        (r"\bwatch\s+ultimate\b", "WU"),
        (r"\bwatch\s*(\d+)\s*pro\b", lambda m: f"W{m.group(1)}P"),
        (r"\bwatch\s*(\d+)\b", lambda m: f"W{m.group(1)}"),
        (r"\bwatch\s+gt\s*(\d+)\s*e\b", lambda m: f"WGT{m.group(1)}E"),
        (r"\bwatch\s+gt\s*(\d+)\s*pro\b", lambda m: f"WGT{m.group(1)}P"),
        (r"\bwatch\s+gt\s*(\d+)\b", lambda m: f"WGT{m.group(1)}"),
        (r"\benjoy\s*(\d+)x\b", lambda m: f"E{m.group(1)}X"),
        (r"\benjoy\s*(\d+)\b", lambda m: f"E{m.group(1)}"),
        (r"\bmate\s+xt\s+ultimate\b", "MXTU"),
        (r"\bmate\s+xts\b", "MXTS"),
        (r"\bmate\s+xt\b", "MXT"),
        (r"\bmatepad\s+air\s+wi\s*fi\b", "MPAIRW"),
        (r"\bmatepad\s+air\s*(\d+(?:\.\d+)?)", lambda m: f"MPA{m.group(1).replace('.', '')}"),
        (r"\bmatepad\s*(\d+(?:\.\d+)?)", lambda m: f"MP{m.group(1).replace('.', '')}"),
        (
            r"\bmatepad\s+t\s*(\d+)\s*(\d+(?:\.\d+)?)?",
            lambda m: f"MPT{m.group(1)}{(m.group(2) or '').replace('.', '').rstrip('0')}",
        ),
        (
            r"\bmatepad\s+se\s*(\d+(?:\.\d+)?)?",
            lambda m: f"MPSE{(m.group(1) or '').replace('.', '').rstrip('0')}",
        ),
        (r"\bmatepad\s+pro\s*(\d+(?:\.\d+)?)", lambda m: f"MPP{m.group(1).replace('.', '')}"),
        (r"\bmate\s+xs\s*(\d+)\b", lambda m: f"MXS{m.group(1)}"),
        (r"\bmate\s*x(\d+)\b", lambda m: f"MX{m.group(1)}"),
        (r"\bmate\s*(\d+)\s*lite\b", lambda m: f"M{m.group(1)}LT"),
        (r"\bmate\s*(\d+)\s*pro\b", lambda m: f"M{m.group(1)}P"),
        (r"\bmate\s*(\d+)\b", lambda m: f"M{m.group(1)}"),
        (r"\bpura\s*(\d+)\s*ultra\b", lambda m: f"{m.group(1)}U"),
        (r"\bpura\s*(\d+)\s*pro\+\b", lambda m: f"{m.group(1)}PP"),
        (r"\bpura\s*(\d+)\s*pro\b", lambda m: f"{m.group(1)}P"),
        (r"\bpura\s*(\d+)\b", lambda m: f"{m.group(1)}"),
        (r"\bp\s*smart\s*(\d{4})\b", lambda m: f"PSM{m.group(1)[-2:]}"),
        (r"\bp\s*smart\s*z\b", "PSMZ"),
        (r"\bp\s*smart\s*plus\b", "PSMP"),
        (r"\bp\s*smart\b", "PSM"),
        (r"\bnexus\s+6p\b", "N6P"),
        (r"\bmate\s+s\b", "MS"),
        (r"\bmediapad\s+10\s+link\b", "MT10L"),
        (r"\be5573\s*/\s*e5577\b", "E5573"),
        (r"\bp(\d+)\s*lite\s*(\d{4})\b", lambda m: f"P{m.group(1)}LT{m.group(2)[-2:]}"),
        (r"\bp(\d+)\s*lite\s*e\b", lambda m: f"P{m.group(1)}LE"),
        (r"\bp(\d+)\s*lite\b", lambda m: f"P{m.group(1)}LT"),
        (r"\bp(\d+)\s*plus\b", lambda m: f"P{m.group(1)}P"),
        (r"\bp(\d+)\s*pro\b", lambda m: f"P{m.group(1)}P"),
        (r"\bp(\d+)\b", lambda m: f"P{m.group(1)}"),
        (r"\bnova\s+(\d+)\s*pro\b", lambda m: f"N{m.group(1)}P"),
        (r"\bnova\s+(\d+)\s*lite\b", lambda m: f"N{m.group(1)}LT"),
        (r"\bnova\s+(\d+)\s*se\b", lambda m: f"N{m.group(1)}SE"),
        (r"\bnova\s+(\d+)i\b", lambda m: f"N{m.group(1)}I"),
        (r"\bnova\s+(\d+)s\b", lambda m: f"N{m.group(1)}S"),
        (r"\bnova\s+(\d+)\b", lambda m: f"N{m.group(1)}"),
        (r"\bnova\s+y(\d+)([a-z]?)\b", lambda m: f"NY{m.group(1)}{m.group(2).upper()}"),
        (r"\bnova\s+lite\s*2017\b", "NL17"),
        (r"\bnova\s+lite\b", "NLT"),
        (r"\by(\d+)\s*prime\b", lambda m: f"Y{m.group(1)}P"),
        (r"\by(\d+)\s*pro\b", lambda m: f"Y{m.group(1)}P"),
        (r"\by(\d+)\b", lambda m: f"Y{m.group(1)}"),
        (r"\bhonor\s+view\s*(\d+)\b", lambda m: f"HV{m.group(1)}"),
        (r"\bhonor\s+play\s*(\d+)\b", lambda m: f"HPLAY{m.group(1)}"),
        (r"\bhonor\s+play\b", "HPLAY"),
        (r"\bhonor\s+magicwatch\s*(\d+)\b", lambda m: f"HMW{m.group(1)}"),
        (r"\bhonor\s+v(\d+)\s*play\b", lambda m: f"HV{m.group(1)}P"),
        (r"\bhonor\s+(\d+)\s*lite\b", lambda m: f"H{m.group(1)}LT"),
        (r"\bhonor\s+(\d+)\s*premium\b", lambda m: f"H{m.group(1)}P"),
        (r"\bhonor\s+(\d+)\s*pro\b", lambda m: f"H{m.group(1)}P"),
        (r"\bhonor\s+(\d+)x\b", lambda m: f"H{m.group(1)}X"),
        (r"\bhonor\s+x(\d+[a-z]?)\b", lambda m: f"HX{m.group(1).upper()}"),
        (r"\bhonor\s+(\d+)a\b", lambda m: f"H{m.group(1)}A"),
        (r"\bhonor\s+(\d+)s\b", lambda m: f"H{m.group(1)}S"),
        (r"\bhonor\s+(\d+)i\b", lambda m: f"H{m.group(1)}I"),
        (r"\bhonor\s+(\d+)([a-z])\b", lambda m: f"H{m.group(1)}{m.group(2).upper()}"),
        (r"\bhonor\s+(\d+)\b", lambda m: f"H{m.group(1)}"),
    ]

    for pattern, replacement in patterns:
        if match := re.search(pattern, normalized):
            if callable(replacement):
                return replacement(match)
            return replacement

    if match := re.search(r"\b([a-z]{3,4})-[a-z0-9]{2,5}\b", normalized):
        return match.group(1).upper()
    return None


def _oppo_device_suffix(model_value: str, variant: str | None, brand: str) -> str | None:
    text = f"{model_value} {variant or ''}".lower()
    normalized = re.sub(r"[^a-z0-9+./ ]+", " ", text)
    normalized = re.sub(r"\s+", " ", normalized).strip()

    patterns: list[tuple[str, str | callable]] = []
    if brand == "oneplus":
        patterns.extend(
            [
                (r"\boneplus\s+nord\s+n(\d+)\s*se\b", lambda m: f"NN{m.group(1)}SE"),
                (r"\boneplus\s+(\d+)\s*pro\b", lambda m: f"{m.group(1)}P"),
                (r"\boneplus\s+(\d+)\s*r\s*(150|80)w\b", lambda m: f"{m.group(1)}R{m.group(2)}"),
                (r"\boneplus\s+pad\s+go\s*lte\b", "PADGL"),
                (r"\boneplus\s+pad\s+go\b", "PADG"),
                (r"\boneplus\s+pad\s*(\d+)\b", lambda m: f"PAD{m.group(1)}"),
                (r"\boneplus\s+pad\b", "PAD"),
                (r"\boneplus\s+nord\s+ce\s*(\d+)\s*lite\s*5g\b", lambda m: f"NCE{m.group(1)}L5"),
                (r"\boneplus\s+nord\s+ce\s*(\d+)\s*5g\b", lambda m: f"NCE{m.group(1)}5"),
                (r"\boneplus\s+nord\s+ce\s*5g\b", "NCE5"),
                (r"\boneplus\s+nord\s*n(\d+)\s*5g\b", lambda m: f"NN{m.group(1)}5"),
                (r"\boneplus\s+nord\s*(\d+)\s*t\b", lambda m: f"N{m.group(1)}T"),
                (r"\boneplus\s+nord\s*(\d+)\b", lambda m: f"N{m.group(1)}"),
                (r"\boneplus\s+nord\b", "NORD"),
                (r"\boneplus\s+x\b", "OPX"),
                (r"\boneplus\s+one\b", "OPO"),
                (r"\boneplus\s+ace\s*(\d+)\s*pro\b", lambda m: f"A{m.group(1)}P"),
                (r"\boneplus\s+ace\s*(\d+)\b", lambda m: f"A{m.group(1)}"),
                (r"\boneplus\s+(\d+)\s*rt\b", lambda m: f"{m.group(1)}RT"),
                (r"\boneplus\s+(\d+)\s*t\b", lambda m: f"{m.group(1)}T"),
                (r"\boneplus\s+(\d+)\s*r\b", lambda m: f"{m.group(1)}R"),
                (r"\boneplus\s+(\d+)\b", lambda m: f"{m.group(1)}"),
            ]
        )
    if brand == "oppo":
        patterns.extend(
            [
                (r"\ba5\s*2020\b", "A520"),
                (r"\ba9\s*2020\b", "A920"),
                (r"\bfind\s+n(\d+)\s*flip\b", lambda m: f"FN{m.group(1)}F"),
                (r"\bfind\s+n(\d+)\b", lambda m: f"FN{m.group(1)}"),
                (r"\bfind\s*x(\d+)\s*pro\b", lambda m: f"FX{m.group(1)}P"),
                (r"\bfind\s*x(\d+)\b", lambda m: f"FX{m.group(1)}"),
                (r"\bfind\s*(\d+)\b", lambda m: f"F{m.group(1)}"),
                (r"\breno\s*(\d+)\s*x\s*zoom\b", lambda m: f"R{m.group(1)}X"),
                (r"\breno\s*(\d+)\s*f\b", lambda m: f"R{m.group(1)}F"),
                (r"\breno\s*(\d+)\s*pro\s*\+\s*5g\b", lambda m: f"R{m.group(1)}PP5"),
                (r"\breno\s*(\d+)\s*pro\s*\+\b", lambda m: f"R{m.group(1)}PP"),
                (r"\breno\s*(\d+)\s*pro\s*5g\b", lambda m: f"R{m.group(1)}P5"),
                (r"\breno\s*(\d+)\s*pro\s*4g\b", lambda m: f"R{m.group(1)}P4"),
                (r"\breno\s*(\d+)\s*pro\b", lambda m: f"R{m.group(1)}P"),
                (r"\breno\s*(\d+)\s*lite\b", lambda m: f"R{m.group(1)}LT"),
                (r"\breno\s*(\d+)\s*5g\b", lambda m: f"R{m.group(1)}5"),
                (r"\breno\s*(\d+)\s*4g\b", lambda m: f"R{m.group(1)}4"),
                (r"\breno\s*(\d+)\b", lambda m: f"R{m.group(1)}"),
                (r"\brx\s*(\d+)\s*pro\b", lambda m: f"RX{m.group(1)}P"),
                (r"\brx\s*(\d+)\s*neo\b", lambda m: f"RX{m.group(1)}N"),
                (r"\brx\s*(\d+)\b", lambda m: f"RX{m.group(1)}"),
                (r"\ba(\d+)\s*4g\b", lambda m: f"A{m.group(1)}4"),
                (r"\ba(\d+)\s*5g\b", lambda m: f"A{m.group(1)}5"),
                (r"\ba(\d+)\s*pro\s*4g\b", lambda m: f"A{m.group(1)}P4"),
                (r"\ba(\d+)\s*pro\s*5g\b", lambda m: f"A{m.group(1)}P5"),
                (r"\ba(\d+)\s*pro\b", lambda m: f"A{m.group(1)}P"),
                (r"\ba(\d+)i\s*pro\s*4g\b", lambda m: f"A{m.group(1)}IP4"),
                (r"\ba(\d+)i\s*pro\b", lambda m: f"A{m.group(1)}IP"),
                (r"\ba(\d+)i\b", lambda m: f"A{m.group(1)}I"),
                (r"\ba(\d+)\s*\((?:20)?(\d{2})\)\b", lambda m: f"A{m.group(1)}{m.group(2)}"),
                (r"\ba(\d+)\b", lambda m: f"A{m.group(1)}"),
                (r"\br(\d+)\s*pro\b", lambda m: f"R{m.group(1)}P"),
                (r"\br(\d+)\b", lambda m: f"R{m.group(1)}"),
                (r"\br15x\b", "R15X"),
                (r"\bpad\s+air\b", "PAIR"),
                (r"\bpad\s+neo\b", "PNEO"),
                (r"\bpad\s*(\d+)\b", lambda m: f"PAD{m.group(1)}"),
            ]
        )
    if brand == "realme":
        patterns.extend(
            [
                (r"\bgt\s+neo\s*(\d+)\s*t\b", lambda m: f"GTN{m.group(1)}T"),
                (r"\bgt\s+neo\s*(\d+)\b", lambda m: f"GTN{m.group(1)}"),
                (r"\bgt\s+master\s*edition\b", "GTME"),
                (r"\bgt\s*(\d+)\s*pro\b", lambda m: f"GT{m.group(1)}P"),
                (r"\bgt\s*(\d+)\s*t\b", lambda m: f"GT{m.group(1)}T"),
                (r"\bgt\s*(\d+)\b", lambda m: f"GT{m.group(1)}"),
                (r"\btechlife\s+watch\s*r(\d+)\b", lambda m: f"TWR{m.group(1)}"),
                (
                    r"\bwatch\s*(\d+)\s*pro\b.*\b(rmw\d+)\b",
                    lambda m: f"W{m.group(1)}P{m.group(2)[-2:]}",
                ),
                (r"\bwatch\s*(\d+)\b.*\b(rmw\d+)\b", lambda m: f"W{m.group(1)}{m.group(2)[-2:]}"),
                (r"\bwatch\s*s\s*pro\b", "WSP"),
                (r"\bwatch\s*s\b", "WS"),
                (r"\bwatch\s*(\d+)\s*pro\b", lambda m: f"W{m.group(1)}P"),
                (r"\bwatch\s*(\d+)\b", lambda m: f"W{m.group(1)}"),
                (r"\bpad\s*mini\b", "PDMN"),
                (r"\bpad\b", "PAD"),
                (r"\bnarzo\s*(\d+)\s*a\b", lambda m: f"NZ{m.group(1)}A"),
                (r"\bnarzo\s*(\d+)\s*i\s*prime\b", lambda m: f"NZ{m.group(1)}IP"),
                (r"\bnarzo\s*(\d+)\s*pro\b", lambda m: f"NZ{m.group(1)}P"),
                (r"\bnarzo\s*(\d+)\s*5g\b", lambda m: f"NZ{m.group(1)}5"),
                (r"\bnarzo\s*(\d+)\s*4g\b", lambda m: f"NZ{m.group(1)}4"),
                (r"\bnarzo\s*(\d+)\b", lambda m: f"NZ{m.group(1)}"),
                (r"\bnote\s*(\d+)\b", lambda m: f"N{m.group(1)}"),
                (r"\bc(\d+)\s*s\b", lambda m: f"C{m.group(1)}S"),
                (r"\bc(\d+)\s*y\b", lambda m: f"C{m.group(1)}Y"),
                (r"\bc(\d+)\s*4g\b", lambda m: f"C{m.group(1)}4"),
                (r"\bc(\d+)\b", lambda m: f"C{m.group(1)}"),
                (r"\b(\d+)\s*pro\s*(?:\+|plus)\s*5g\b", lambda m: f"{m.group(1)}PP5"),
                (r"\b(\d+)\s*pro\s*(?:\+|plus)(?:\s|$)", lambda m: f"{m.group(1)}PP"),
                (r"\b(\d+)\s*pro\s*5g\b", lambda m: f"{m.group(1)}P5"),
                (r"\b(\d+)\s*pro\b", lambda m: f"{m.group(1)}P"),
                (r"\b(\d+)\s*\+\s*5g\b", lambda m: f"{m.group(1)}P5"),
                (r"\b(\d+)\s*5g\b", lambda m: f"{m.group(1)}5"),
                (r"\b(\d+)\s*4g\b", lambda m: f"{m.group(1)}4"),
                (r"\b(\d+)\s*i\b", lambda m: f"{m.group(1)}I"),
                (r"\b(\d+)\b", lambda m: f"{m.group(1)}"),
            ]
        )

    for pattern, replacement in patterns:
        if match := re.search(pattern, normalized):
            if callable(replacement):
                return replacement(match)
            return replacement

    if match := re.search(r"\b((?:cph|rmx|mt)\d{3,5})\b", normalized):
        return match.group(1).upper()
    return None


def _google_device_suffix(model_value: str, variant: str | None) -> str | None:
    text = f"{model_value} {variant or ''}".lower()
    normalized = re.sub(r"[^a-z0-9+./ ]+", " ", text)
    normalized = re.sub(r"\s+", " ", normalized).strip()

    patterns: list[tuple[str, str | callable]] = [
        (r"\bpixel\s*5a\s*5g\b", "PIXEL5A5"),
        (r"\bpixel\s*4a\s*5g\b", "PIXEL4A5"),
        (r"\bpixel\s*(\d+)\s*pro\s*xl\b", lambda m: f"PIXEL{m.group(1)}PX"),
        (r"\bpixel\s*(\d+)\s*pro\b", lambda m: f"PIXEL{m.group(1)}P"),
        (r"\bpixel\s*(\d+)a\b", lambda m: f"PIXEL{m.group(1)}A"),
        (r"\bpixel\s*(\d+)\b", lambda m: f"PIXEL{m.group(1)}"),
        (r"\bpixel\b", "PIXEL"),
    ]
    for pattern, replacement in patterns:
        if match := re.search(pattern, normalized):
            if callable(replacement):
                return replacement(match)
            return replacement
    return None


def _nokia_device_suffix(model_value: str, variant: str | None) -> str | None:
    text = f"{model_value} {variant or ''}".lower()
    normalized = re.sub(r"[^a-z0-9+./ -]+", " ", text)
    normalized = re.sub(r"\s+", " ", normalized).strip()

    patterns: list[tuple[str, str | callable]] = [
        (r"\b225\s*/\s*225\s*dual\s*/\s*230\s*dual\s*/\s*3310\s*\(?2017\)?\b", "225"),
        (r"\b225\s*/\s*225\s*dual\s*/\s*230\s*dual\s*/\s*3310\s*\(2017\)\b", "225"),
        (r"\b1202\s*/\s*1203\s*/\s*1661\b", "1202"),
        (r"\b7510\s+supernova\b", "7510"),
        (r"\b7900\s+prism\b", "7900"),
        (r"\b6700\s+classic\b", "6700"),
        (r"\b3120\s+classic\b", "3120"),
        (r"\b8800\s+sirocco\b", "8800S"),
        (r"\b7610\s+supernova\b", "7610"),
        (r"\b7390\b", "7390"),
        (r"\b101\s+dual\b", "101D"),
        (r"\b2630\b", "2630"),
        (r"\b2720\s+fold\b", "2720F"),
        (r"\b1280\b", "1280"),
        (r"\b3250\b", "3250"),
        (r"\b3720\s+classic\b", "3720"),
        (r"\bg\s*(\d+)\b", lambda m: f"G{m.group(1)}"),
        (r"\bx\s*(\d+)\b", lambda m: f"X{m.group(1)}"),
        (r"\bc\s*(\d+)\b", lambda m: f"C{m.group(1)}"),
        (r"\b(\d+)\.(\d+)\b", lambda m: f"{m.group(1)}{m.group(2)}"),
        (r"\b7\s*plus\b", "7P"),
        (r"\b(225)\s*asha\b", lambda m: m.group(1)),
        (r"\b(\d{3,4})\s*asha\b", lambda m: m.group(1)),
        (r"\blumia\s*(\d+)\b", lambda m: f"L{m.group(1)}"),
        (r"\bta[- ]?(\d{4})\b", lambda m: f"TA{m.group(1)}"),
        (r"\b([necx]\d{2,4}(?:-\d{2})?)\b", lambda m: m.group(1).replace("-", "").upper()),
    ]
    for pattern, replacement in patterns:
        if match := re.search(pattern, normalized):
            if callable(replacement):
                return replacement(match)
            return replacement
    return None


def _meizu_device_suffix(model_value: str, variant: str | None) -> str | None:
    text = f"{model_value} {variant or ''}".lower()
    normalized = re.sub(r"[^a-z0-9+./ -]+", " ", text)
    normalized = re.sub(r"\s+", " ", normalized).strip()

    patterns: list[tuple[str, str | callable]] = [
        (r"\bmblu\s*22\s*pro\b", "MB22P"),
        (r"\bmblu\s*22\b", "MB22"),
        (r"\bm3\s*note\b", "M3N"),
        (r"\bm5\s*note\b", "M5N"),
        (r"\bm5c\b", "M5C"),
        (r"\bm6s\b", "M6S"),
        (r"\bmx6\b", "MX6"),
        (r"\bmetal\b", "METAL"),
        (r"\bm(\d+)\s*note\b", lambda m: f"M{m.group(1)}N"),
        (r"\bm(\d+)\s*mini\b", lambda m: f"M{m.group(1)}M"),
        (r"\bm(\d+)t\b", lambda m: f"M{m.group(1)}T"),
        (r"\bm(\d+)c\b", lambda m: f"M{m.group(1)}C"),
        (r"\bm(\d+)s\b", lambda m: f"M{m.group(1)}S"),
        (r"\bm(\d+)\b", lambda m: f"M{m.group(1)}"),
        (r"\bmx(\d+)\b", lambda m: f"MX{m.group(1)}"),
        (r"\bpro\s*(\d+)\s*plus\b", lambda m: f"P{m.group(1)}P"),
        (r"\bpro\s*(\d+)s\b", lambda m: f"P{m.group(1)}S"),
        (r"\bpro\s*(\d+)\b", lambda m: f"P{m.group(1)}"),
        (r"\bnote\s*(\d+)\b", lambda m: f"N{m.group(1)}"),
        (r"\b([lm]\d{3,4}[a-z]?)\b", lambda m: m.group(1).upper()),
    ]
    for pattern, replacement in patterns:
        if match := re.search(pattern, normalized):
            if callable(replacement):
                return replacement(match)
            return replacement
    return None


def _transsion_vivo_device_suffix(model_value: str, variant: str | None, brand: str) -> str | None:
    text = f"{model_value} {variant or ''}".lower()
    normalized = re.sub(r"[^a-z0-9+./ ]+", " ", text)
    normalized = re.sub(r"\s+", " ", normalized).strip()

    if brand == "vivo":
        patterns: list[tuple[str, str | callable]] = [
            (r"\bx(\d+)\s*pro\s*mini\b", lambda m: f"X{m.group(1)}PM"),
            (r"\bx(\d+)\s*fe\b", lambda m: f"X{m.group(1)}F"),
            (r"\biqoo\s+neo\s*(\d+)\s*r\b", lambda m: f"IQN{m.group(1)}R"),
            (r"\biqoo\s+neo\s*(\d+)\b", lambda m: f"IQN{m.group(1)}"),
            (r"\biqoo\s+z(\d+)\s*lite\b", lambda m: f"IQZ{m.group(1)}L"),
            (r"\biqoo\s+z(\d+)x\b", lambda m: f"IQZ{m.group(1)}X"),
            (r"\biqoo\s+z(\d+)\b", lambda m: f"IQZ{m.group(1)}"),
            (r"\biqoo\s*(\d+)\b", lambda m: f"IQ{m.group(1)}"),
            (r"\bx\s*fold\s*(\d+)\b", lambda m: f"XFOLD{m.group(1)}"),
            (r"\bx(\d+)\s*ultra\b", lambda m: f"X{m.group(1)}U"),
            (r"\bx\s*(\d+)\s*pro\b", lambda m: f"X{m.group(1)}P"),
            (r"\bx\s*(\d+)\b", lambda m: f"X{m.group(1)}"),
            (r"\bv(\d+)\s*lite\s*5g\b", lambda m: f"V{m.group(1)}L5"),
            (r"\bv(\d+)\s*lite\s*(?:4g|5g)?\b", lambda m: f"V{m.group(1)}L"),
            (r"\bv(\d+)\s*se\b", lambda m: f"V{m.group(1)}SE"),
            (r"\bv(\d+)\s*neo\b", lambda m: f"V{m.group(1)}N"),
            (r"\bv(\d+)\s*pro\b", lambda m: f"V{m.group(1)}P"),
            (r"\bv(\d+)\s*india\b", lambda m: f"V{m.group(1)}I"),
            (r"\bv(\d+)\s*plus\b", lambda m: f"V{m.group(1)}P"),
            (r"\bv(\d+)\b", lambda m: f"V{m.group(1)}"),
            (r"\by(\d+)\s*prime\b", lambda m: f"Y{m.group(1)}P"),
            (r"\by(\d+)s\b", lambda m: f"Y{m.group(1)}S"),
            (r"\by(\d+)\s*lite\b", lambda m: f"Y{m.group(1)}L"),
            (r"\by(\d+)\s*plus\b", lambda m: f"Y{m.group(1)}P"),
            (r"\by(\d+)\s*neo\b", lambda m: f"Y{m.group(1)}N"),
            (r"\by(\d+)\s*pro\b", lambda m: f"Y{m.group(1)}P"),
            (r"\by(\d+)\b", lambda m: f"Y{m.group(1)}"),
            (r"\bv(\d{4})\b", lambda m: f"V{m.group(1)}"),
        ]
    elif brand == "tecno":
        patterns = [
            (r"\bphantom\s+v\s+fold\b", "PVF"),
            (r"\bmegapad\s+pro\b", "MPPRO"),
            (r"\bmegapad\s+11\b", "MP11"),
            (r"\bcamon\s*(\d+)\s*premier\s*5g\b", lambda m: f"C{m.group(1)}PR5"),
            (r"\bcamon\s*(\d+)\s*premier\b", lambda m: f"C{m.group(1)}PR"),
            (r"\bcamon\s*(\d+)s\s*pro\b", lambda m: f"C{m.group(1)}SP"),
            (r"\bcamon\s*(\d+)s\b", lambda m: f"C{m.group(1)}S"),
            (r"\bcamon\s*(\d+)\s*pro\s*5g\b", lambda m: f"C{m.group(1)}P5"),
            (r"\bcamon\s*(\d+)\s*pro\b", lambda m: f"C{m.group(1)}P"),
            (r"\bcamon\s*(\d+)\b", lambda m: f"C{m.group(1)}"),
            (r"\bpova\s*(\d+)\s*pro\s*5g\b", lambda m: f"POVA{m.group(1)}P5"),
            (r"\bspark\s+go\s*(\d+)\b", lambda m: f"SG{m.group(1)}"),
            (r"\b([a-z]{2}\d[a-z0-9]*)\b", lambda m: m.group(1).upper()),
        ]
    else:
        patterns = [
            (r"\bgt\s*(\d+)\s*pro\b", lambda m: f"GT{m.group(1)}P"),
            (r"\bgt\s*(\d+)\b", lambda m: f"GT{m.group(1)}"),
            (r"\bhot\s*(\d+)\s*play\s*nfc\b", lambda m: f"H{m.group(1)}PLN"),
            (r"\bhot\s*(\d+)\s*play\b", lambda m: f"H{m.group(1)}PL"),
            (r"\bhot\s*(\d+)s\b", lambda m: f"H{m.group(1)}S"),
            (r"\bhot\s*(\d+)\s*lite\b", lambda m: f"H{m.group(1)}L"),
            (r"\bhot\s*(\d+)\s*pro\+", lambda m: f"H{m.group(1)}PP"),
            (r"\bhot\s*(\d+)\s*pro\b", lambda m: f"H{m.group(1)}P"),
            (r"\bhot\s*(\d+)i\b", lambda m: f"H{m.group(1)}I"),
            (r"\bhot\s*(\d+)\b", lambda m: f"H{m.group(1)}"),
            (r"\bsmart\s*(\d+)\s*plus\b", lambda m: f"S{m.group(1)}PL"),
            (r"\bsmart\s*(\d+)\s*pro\b", lambda m: f"S{m.group(1)}P"),
            (r"\bsmart\s*(\d+)\s*hd\b", lambda m: f"S{m.group(1)}HD"),
            (r"\bsmart\s*(\d+)\b", lambda m: f"S{m.group(1)}"),
            (r"\bzero\s*(\d+)\s*ultra\b", lambda m: f"Z{m.group(1)}U"),
            (r"\bzero\s*(\d+)i\b", lambda m: f"Z{m.group(1)}I"),
            (r"\bzero\s*(\d+)\s*5g\b", lambda m: f"Z{m.group(1)}5"),
            (r"\bzero\s*(\d+)\b", lambda m: f"Z{m.group(1)}"),
            (r"\bnote\s*(\d+)\s*pro\s*4g\b", lambda m: f"N{m.group(1)}P4"),
            (r"\bnote\s*(\d+)\s*pro\+\s*5g\b", lambda m: f"N{m.group(1)}PP5"),
            (r"\bnote\s*(\d+)\s*pro\+\b", lambda m: f"N{m.group(1)}PP"),
            (r"\bnote\s*(\d+)\s*pro\s*nfc\b", lambda m: f"N{m.group(1)}PN"),
            (r"\bnote\s*(\d+)\s*pro\b", lambda m: f"N{m.group(1)}P"),
            (r"\bnote\s*(\d+)i\b", lambda m: f"N{m.group(1)}I"),
            (r"\bnote\s*(\d+)\b", lambda m: f"N{m.group(1)}"),
            (r"\b([a-z]\d{4})\b", lambda m: m.group(1).upper()),
        ]

    for pattern, replacement in patterns:
        if match := re.search(pattern, normalized):
            if callable(replacement):
                return replacement(match)
            return replacement
    return None


def _zte_device_suffix(model_value: str, variant: str | None) -> str | None:
    text = f"{model_value} {variant or ''}".lower()
    normalized = re.sub(r"[^a-z0-9+./ ]+", " ", text)
    normalized = re.sub(r"\s+", " ", normalized).strip()

    patterns: list[tuple[str, str | callable]] = [
        (
            r"\bblade\s+20\s+smart\s*/\s*blade\s+a6\s*/\s*blade\s+a6\s+lite\s*/\s*blade\s+v30\s*/\s*blade\s+v30\s+vita\b",
            "B20S",
        ),
        (r"\bblade\s+l5\s*/\s*blade\s+l5\s*plus\b", "BL5"),
        (r"\bblade\s+l4\b", "BL4"),
        (r"\bblade\s+a6\s+lite\b", "BA6L"),
        (r"\bblade\s+a6\b", "BA6"),
        (r"\bblade\s+v7\s+lite\b", "BV7L"),
        (r"\bblade\s+gf3\b", "BGF3"),
        (r"\bblade\s+a5\s*/\s*blade\s+a5\s*pro\s*/\s*blade\s+af3\b", "BA5"),
        (r"\bred\s+magic\s+r6\b", "RMR6"),
        (r"\bblade\s+af3\s*/\s*blade\s+af5\s*/\s*blade\s+a5\b", "BAF355"),
        (r"\bblade\s+v8\s*lite\b", "BV8L"),
        (r"\bblade\s+v8\s*mini\b", "BV8M"),
        (r"\bnubia\s+red\s+magic\s+astra\s+gaming\s+tablet\b", "RMAGT"),
        (r"\bnubia\s+red\s+magic\s+(\d+)\s*air\b", lambda m: f"RM{m.group(1)}A"),
        (r"\bnubia\s+red\s+magic\s+(\d+)\s*s\s*pro\b", lambda m: f"RM{m.group(1)}SP"),
        (r"\bnubia\s+red\s+magic\s+(\d+)\s*pro\b", lambda m: f"RM{m.group(1)}P"),
        (r"\bnubia\s+red\s+magic\s+(\d+)\s*s\b", lambda m: f"RM{m.group(1)}S"),
        (r"\bnubia\s+red\s+magic\s+(\d+)\b", lambda m: f"RM{m.group(1)}"),
        (r"\bnubia\s+z(\d+)s\s*pro\b", lambda m: f"Z{m.group(1)}SP"),
        (r"\bnubia\s+z(\d+)\s*ultra\s*leading\b", lambda m: f"Z{m.group(1)}UL"),
        (r"\bnubia\s+z(\d+)\s*ultra\b", lambda m: f"Z{m.group(1)}U"),
        (r"\bnubia\s+z(\d+)\b", lambda m: f"Z{m.group(1)}"),
        (r"\bnubia\s+v(\d+)\s*max\b", lambda m: f"V{m.group(1)}M"),
        (r"\bnubia\s+v(\d+)\s*design\b", lambda m: f"V{m.group(1)}D"),
        (r"\bnubia\s+v(\d+)\b", lambda m: f"V{m.group(1)}"),
        (r"\bnubia\s+neo\s+(\d+)\s*5g\b", lambda m: f"NN{m.group(1)}5"),
        (r"\bnubia\s+neo\s+(\d+)\b", lambda m: f"NN{m.group(1)}"),
        (r"\bnubia\s+flip\s+(\d+)\s*5g\b", lambda m: f"NF{m.group(1)}5"),
        (r"\bnubia\s+flip\s*5g\b", "NF5"),
        (r"\baxon\s+(\d+)\b", lambda m: f"AX{m.group(1)}"),
        (r"\bblade\s+([a-z]\d{2,4}[a-z]?)\b", lambda m: f"B{m.group(1).upper()}"),
        (r"\b(nx\d{3,4}[a-z]?)\b", lambda m: m.group(1).upper()),
        (r"\b(v\d{3,4}[a-z]?)\b", lambda m: m.group(1).upper()),
        (r"\b(z\d{4}[a-z]?)\b", lambda m: m.group(1).upper()),
    ]

    for pattern, replacement in patterns:
        if match := re.search(pattern, normalized):
            if callable(replacement):
                return replacement(match)
            return replacement
    return None


def _asus_device_suffix(model_value: str, variant: str | None) -> str | None:
    text = f"{model_value} {variant or ''}".lower()
    normalized = re.sub(r"[^a-z0-9+./ -]+", " ", text)
    normalized = re.sub(r"\s+", " ", normalized).strip()

    patterns: list[tuple[str, str | callable]] = [
        (r"\ba45\s*/\s*a55\s*/\s*a75\s*/\s*a95\b", "A45"),
        (r"\ba43\s*/\s*a53\s*/\s*k43\s*/\s*k53\s*/\s*x43\s*/\s*x44\s*/\s*x53\s*/\s*x54\b", "A43"),
        (r"\bx450a\s*/\s*x450c\s*/\s*x450e\s*/\s*x450v\b", "X450A"),
        (r"\bx450a\s*/\s*x550c\b", "X450A"),
        (r"\bx441ca\s*/\s*x551ca\s*/\s*x551ma\b", "X441CA"),
        (r"\bzenfone\s+go\s*\(zb450kl\)\s*/\s*zenfone\s+go\s*\(zb452kg\)\b", "ZFGO45"),
        (r"\bzenfone\s+go\s*\(zb500kg\)\s*/\s*zenfone\s+go\s*\(zb500kl\)\b", "ZFGO50"),
        (r"\bzenfone\s+go\s*\(zb551kl\)\b", "ZFGO55"),
        (r"\bzenfone\s+go\s+zb450kl\s*/\s*zenfone\s+go\s+zb452kg\b", "ZFGO45"),
        (r"\bzenfone\s+go\s+zb500kg\s*/\s*zenfone\s+go\s+zb500kl\b", "ZFGO50"),
        (r"\bzenfone\s+go\s+zb551kl\b", "ZFGO55"),
        (r"\bzenfone\s+max\s+m2\b", "ZFMM2"),
        (r"\bzenfone\s+max\s+pro\s+m1\b", "ZFMPM1"),
        (r"\bzenfone\s+max\s+pro\s+m2\b", "ZFMPM2"),
        (r"\brog\s+phone\s*5s\b", "ROG5S"),
        (r"\brog\s+phone\s*5\b", "ROG5"),
        (r"\beee\s+pc\s*1001\b", "E1001"),
        (r"\bzenpad\s+s\s*8\.?0\b", "ZPS80"),
        (r"\brog\s+phone\s*(\d+)\s*fe\b", lambda m: f"ROG{m.group(1)}FE"),
        (r"\bzenfone\s+zoom\b", "ZFZOOM"),
        (r"\bzenfone\s+c\b", "ZFC"),
        (r"\bzenfone\s+3\s+laser\b", "ZF3L"),
        (r"\bzenfone\s+(\d+)\s*ultra\b", lambda m: f"ZF{m.group(1)}U"),
        (r"\bzenfone\s+(\d+)\b", lambda m: f"ZF{m.group(1)}"),
        (r"\b(ai\d{4})\b", lambda m: m.group(1).upper()),
        (r"\b(zx\d{3,4}[a-z]*)\b", lambda m: m.group(1).upper()),
        (r"\b(zc\d{3,4}[a-z]*)\b", lambda m: m.group(1).upper()),
        (r"\b(a\d{4}[a-z]*)\b", lambda m: m.group(1).upper()),
    ]
    for pattern, replacement in patterns:
        if match := re.search(pattern, normalized):
            if callable(replacement):
                return replacement(match)
            return replacement
    return None


def _sony_device_suffix(model_value: str, variant: str | None) -> str | None:
    text = f"{model_value} {variant or ''}".lower()
    normalized = re.sub(r"[^a-z0-9+./ -]+", " ", text)
    normalized = re.sub(r"\s+", " ", normalized).strip()

    patterns: list[tuple[str, str | callable]] = [
        (r"\bc6603\s*/\s*lt36i\s+xperia\s+z\s*/\s*c2305\s+xperia\s+c\b", "XZC"),
        (r"\bvpc-sa(?:\s*,)?\s*vpc-sb(?:\s*,)?\s*vpc-se(?:\s*,)?\s*sv-s\b", "VPCS"),
        (r"\bxperia\s+xa2\b", "XA2"),
        (r"\bxperia\s+l2\b", "L2"),
        (r"\bxperia\s+1\s+iii\b", "X1III"),
        (r"\bxperia\s+5\s+iii\b", "X5III"),
        (r"\bxperia\s+1\s+iv\b", "X1IV"),
        (r"\bxperia\s+5\s+iv\b", "X5IV"),
        (r"\bxperia\s+10\s+iv\b", "X10IV"),
        (r"\bxperia\s+5\s+v\b", "X5V"),
        (r"\bxperia\s+10\s+v\b", "X10V"),
        (r"\bxperia\s+1\s+ii\b", "X1II"),
        (r"\bxperia\s+z1\b", "Z1"),
        (r"\bxperia\s+zl\b", "ZL"),
        (r"\bxperia\s+p\b", "XP"),
        (r"\bvaio\s+14e\b", "V14E"),
        (r"\bvaio\s+15e\b", "V15E"),
        (r"\bvaio\s+vpce\b", "VPCE"),
        (r"\bxperia\s+x\s+(?:performance|perfomance)\b", "XPERF"),
        (r"\bxperia\s+xz\s+premium\b", "XZP"),
        (r"\bxperia\s+xz1\s+compact\b", "XZ1C"),
        (r"\bxperia\s+xz1\b", "XZ1"),
        (r"\bxperia\s+xzs\b", "XZS"),
        (r"\bxperia\s+xz\b", "XZ"),
        (r"\bxperia\s+xa1\s+ultra\b", "XA1U"),
        (r"\bxperia\s+z5\s+premium\b", "Z5P"),
        (r"\bxperia\s+z5\b", "Z5"),
        (r"\bxperia\s+z1\s+compact\b", "Z1C"),
        (r"\bxperia\s+l1\b", "L1"),
        (r"\bxperia\s+m4\s+aqua\b", "M4A"),
        (r"\b([defgl]\d{4}[a-z]?)\b", lambda m: m.group(1).upper()),
    ]
    for pattern, replacement in patterns:
        if match := re.search(pattern, normalized):
            if callable(replacement):
                return replacement(match)
            return replacement
    return None


def _misc_legacy_device_suffix(brand: str, model_value: str, variant: str | None) -> str | None:
    text = f"{model_value} {variant or ''}".lower()
    normalized = re.sub(r"[^a-z0-9+./ -]+", " ", text)
    normalized = re.sub(r"\s+", " ", normalized).strip()

    if brand == "motorola":
        patterns = [
            (r"\bedge\s*(\d+)\s*fusion\b", lambda m: f"E{m.group(1)}F"),
            (r"\bedge\s*(\d+)\s*pro\b", lambda m: f"E{m.group(1)}P"),
            (r"\bedge\s*(\d+)\b", lambda m: f"E{m.group(1)}"),
            (r"\bxt(\d{4})-\d\b", lambda m: f"XT{m.group(1)}"),
        ]
    elif brand == "lg":
        patterns = [
            (r"\bg7\s+thinq\b", "G7"),
            (r"\bg2\b", "G2"),
            (r"\bnexus\s+5\b", "N5"),
            (r"\b(k220ds)\b", lambda m: m.group(1).upper()),
            (r"\b(k200ds)\b", lambda m: m.group(1).upper()),
            (r"\b(gm200)\b", lambda m: m.group(1).upper()),
            (r"\bclass\b", "CLASS"),
            (r"\b([dkmgx]\d{3,4}[a-z]?)\b", lambda m: m.group(1).upper()),
            (r"\b(h\d{3,4}[a-z]?)\b", lambda m: m.group(1).upper()),
        ]
    elif brand == "fly":
        patterns = [
            (r"\bfs(\d{3})\b", lambda m: f"FS{m.group(1)}"),
            (r"\biq(\d{3,4})\b", lambda m: f"IQ{m.group(1)}"),
        ]
    elif brand == "jbl":
        patterns = [
            (r"\bxtreme\s*2\b", "XT2"),
            (r"\bxtreme\b", "XT1"),
            (r"\bflip\s*5\b", "FL5"),
            (r"\bflip\s*4\b", "FL4"),
            (r"\bflip\s*3\b", "FL3"),
            (r"\bcharge\s*2\s*plus\b", "CH2P"),
            (r"\bcharge\s*2\+\b", "CH2P"),
            (r"\bcharge\s*2\b", "CH2"),
            (r"\bcharge\s*3\b", "CH3"),
            (r"\bcharge\s*4\b", "CH4"),
        ]
    else:
        return None

    for pattern, replacement in patterns:
        if match := re.search(pattern, normalized):
            if callable(replacement):
                return replacement(match)
            return replacement
    return None


def _lenovo_device_suffix(model_value: str, variant: str | None) -> str | None:
    text = f"{model_value} {variant or ''}".lower()
    normalized = re.sub(r"[^a-z0-9+./ -]+", " ", text)
    normalized = re.sub(r"\s+", " ", normalized).strip()

    patterns: list[tuple[str, str | callable]] = [
        (r"\blegion\s+y700\s*gen\s*3(?:\s*\(?(2025)\)?)?\b", "Y700G3"),
        (r"\byoga\s+tablet\s*3\s*10\.?1\b", "YT310"),
        (r"\byoga\s+tablet\s*3\s*8\.?0\b", "YT38"),
        (r"\byoga\s+tablet\s*2\s*10\.?1\b", "YT210"),
        (r"\byoga\s+tablet\s*2\s*8\.?0\b", "YT28"),
        (r"\byoga\s+tablet\s*10\.?1\b", "YT10"),
        (r"\bvibe\s+p1\s*pro\b", "VP1P"),
        (r"\bvibe\s+p1\s*turbo\b", "VP1T"),
        (r"\bvibe\s+p1\b", "VP1"),
        (r"\bvibe\s+x2\b", "VX2"),
        (r"\bsisley\s+s90\b", "S90"),
        (r"\bvibe\s+shot\s+z90\b", "VSZ90"),
        (r"\bvibe\s+s1\s*lite\b", "VS1L"),
        (r"\ba10-70f\s*/\s*a10-70l\s*tab\s*2\s*10\.?1\b", "A1070"),
        (r"\bvibe\s+k5\s*plus\b", "VK5P"),
        (r"\bvibe\s+k5\b", "VK5"),
        (r"\bk4\s*note\b", "K4N"),
        (r"\bvibe\s+x3\s*lite\b", "VX3L"),
        (r"\bvibe\s+z\b", "VZ"),
        (r"\bvibe\s+x\b", "VX"),
        (r"\bphab\s+plus\b", "PBP"),
        (r"\bideatab\s+7\.?0\b", "IT7"),
        (r"\bideatab\s+9\.?0\b", "IT9"),
        (r"\btab\s*4\s*plus\s*8\.?0\b", "TB4P8"),
        (r"\btab\s*4\s*8\.?0\b", "TB48"),
        (r"\btab\s*4\s*7\.?0\b", "TB47"),
        (r"\btab\s*3\s*7\.?0\s*essential\b", "TB37E"),
        (r"\btab\s*3\s*7\.?0\b", "TB37"),
        (r"\btab\s*3\s*8\.?0\b", "TB38"),
        (r"\btab\s*p11\b", "TBP11"),
        (r"\btab\s*e10\b", "TBE10"),
        (r"\bxiaoxin\s+ideapad\s+pro\s+12\.?7\b", "XIP127"),
        (r"\b(tb3[- ]?\d{3}[a-z]?)\b", lambda m: m.group(1).replace("-", "").upper()),
        (r"\b(tb[- ]?\d{4}[a-z]?)\b", lambda m: m.group(1).replace("-", "").upper()),
        (r"\b(pb1[- ]?\d{3}[a-z]?)\b", lambda m: m.group(1).replace("-", "").upper()),
        (r"\b(yt3[- ]?\d{3}[a-z]?)\b", lambda m: m.group(1).replace("-", "").upper()),
        (r"\b(yt[- ]?\d{3,4}[a-z]?)\b", lambda m: m.group(1).replace("-", "").upper()),
        (r"\b(a\d{3,4}[a-z]?)\b", lambda m: m.group(1).upper()),
        (r"\b(k\d{3,4}[a-z]?)\b", lambda m: m.group(1).upper()),
        (r"\b(p\d{2,4}[a-z]?)\b", lambda m: m.group(1).upper()),
        (r"\b(s\d{3,4}[a-z]?)\b", lambda m: m.group(1).upper()),
        (r"\b(v\d{3,4}[a-z]?)\b", lambda m: m.group(1).upper()),
        (r"\b(x\d{2,4}[a-z]?)\b", lambda m: m.group(1).upper()),
        (r"\b(1050[lfs])\b", lambda m: f"YT2{m.group(1).upper()}"),
    ]

    for pattern, replacement in patterns:
        if match := re.search(pattern, normalized):
            if callable(replacement):
                return replacement(match)
            return replacement
    return None


def infer_device_code(product: Product, category_code: str | None = None) -> str | None:
    category = category_code or infer_category_code(product)
    if category == "CBL":
        return _find_code(product.cable_connector_input, CONNECTOR_CODES) or _slug_token(
            product.cable_connector_input, max_len=8
        )
    if category == "CHR":
        return f"{product.charger_power_w}W" if product.charger_power_w else None

    apple_name = (product.name or "").lower()
    if re.search(r"\bapple\b|\biphone\b|\bipad\b", apple_name):
        apple_name_code = _device_code_from_model("apple", product.name, None)
        if apple_name_code:
            return apple_name_code

    samsung_name = (product.name or "").lower()
    if "samsung" in samsung_name or "galaxy" in samsung_name or "sm-" in samsung_name:
        samsung_name_code = _device_code_from_model("samsung", product.name, None)
        if samsung_name_code:
            return samsung_name_code

    xiaomi_name_code = _device_code_from_model("xiaomi", product.name, None)
    if xiaomi_name_code and any(
        token in (product.name or "").lower()
        for token in ("xiaomi", "redmi", "poco", "pocophone", "mi ", "mi-", "mi/")
    ):
        return xiaomi_name_code

    meizu_name = (product.name or "").lower()
    if "meizu" in meizu_name or "mblu" in meizu_name:
        meizu_name_code = _device_code_from_model("meizu", product.name, None)
        if meizu_name_code:
            return meizu_name_code

    nokia_name = (product.name or "").lower()
    if "nokia" in nokia_name or "lumia" in nokia_name or "asha" in nokia_name:
        nokia_name_code = _device_code_from_model("nokia", product.name, None)
        if nokia_name_code:
            return nokia_name_code

    lenovo_name = (product.name or "").lower()
    if any(
        token in lenovo_name
        for token in ("lenovo", "ideaphone", "ideatab", "legion y700", "xiaoxin")
    ):
        lenovo_name_code = _device_code_from_model("lenovo", product.name, None)
        if lenovo_name_code:
            return lenovo_name_code

    zte_name = (product.name or "").lower()
    if any(token in zte_name for token in ("zte", "nubia", "red magic", "axon")):
        zte_name_code = _device_code_from_model("zte", product.name, None)
        if zte_name_code:
            return zte_name_code

    huawei_name = (product.name or "").lower()
    if any(token in huawei_name for token in ("huawei", "honor", "mediapad", "pura", "nova ")):
        preferred_brand = "huawei" if "huawei" in huawei_name else "honor"
        huawei_name_code = _device_code_from_model(preferred_brand, product.name, None)
        if huawei_name_code:
            return huawei_name_code

    misc_name = (product.name or "").lower()
    for preferred_brand, markers in (
        ("asus", ("asus", "zenfone", "rog phone")),
        ("sony", ("sony", "xperia")),
        ("jbl", ("jbl",)),
        ("motorola", ("motorola", "moto ")),
        ("lg", ("lg ", "lg-", "class")),
        ("google", ("google", "pixel")),
        ("vivo", ("vivo", "iqoo")),
        ("fly", ("fly ", "fly iq")),
        ("tecno", ("tecno", "phantom", "pova", "spark")),
        ("infinix", ("infinix",)),
    ):
        if any(marker in misc_name for marker in markers):
            misc_code = _device_code_from_model(preferred_brand, product.name, None)
            if misc_code:
                return misc_code

    oppo_name = (product.name or "").lower()
    for preferred_brand, markers in (
        ("oneplus", ("oneplus",)),
        ("oppo", ("oppo", "reno", "find ", "rx17", "pad air", "pad neo")),
        ("realme", ("realme", "narzo", "gt neo", "gt ", " c", " note ")),
    ):
        if any(marker in oppo_name for marker in markers):
            oppo_name_code = _device_code_from_model(preferred_brand, product.name, None)
            if oppo_name_code:
                return oppo_name_code

    if len(product.phone_model_links) == 1:
        phone_model = product.phone_model_links[0].phone_model
        code = _device_code_from_model(
            phone_model.brand, phone_model.model_name, phone_model.variant
        )
        if code:
            return code

    if len(product.compatibilities) == 1:
        raw = product.compatibilities[0].value
        parsed = parse_model_name(raw)
        if not parsed.ambiguous:
            code = _device_code_from_model(parsed.brand, parsed.model, parsed.variant)
            if code:
                return code

    parsed = parse_model_name(product.name)
    if not parsed.ambiguous:
        return _device_code_from_model(parsed.brand, parsed.model, parsed.variant)
    return None


def _display_key(product: Product) -> str | None:
    display_type = _display_tech_code(product)
    color = _display_color_code(product)
    quality = _display_grade_code(product)
    return "-".join(_compact_parts([display_type, color, quality])) or None


def _normalized_display_texts(product: Product) -> list[str]:
    values = [
        product.name,
        product.display_type,
        product.display_quality,
        product.display_quality_raw,
        product.quality,
        product.quality_raw,
        product.manufacturer,
    ]
    return [value.lower() for value in values if value]


def _display_tech_code(product: Product) -> str | None:
    texts = _normalized_display_texts(product)
    joined = " ".join(texts)
    compact = joined.replace("-", "").replace(" ", "")
    if "superretina" in compact:
        if "xdr" in compact or "ltpo" in compact:
            return "AMD"
        return "OLD"
    if "soft oled" in joined or "softoled" in compact:
        return "SLD"
    if "hard oled" in joined or "hardoled" in compact:
        return "HLD"
    if "in cell" in joined or "incell" in compact:
        return "INL"
    if "ltpo super retina" in joined or "super retina xdr" in joined or "ltpo" in joined:
        return "AMD"
    if "super retina" in joined:
        return "OLD"
    if "liquid retina" in joined:
        return "IPS"
    if "dynamic amoled" in joined or "dynamicamoled" in compact:
        return "AMD"
    if "super amoled" in joined or "superamoled" in compact:
        return "AMD"
    if "amoled" in joined:
        return "AMD"
    if "oled" in joined:
        return "OLD"
    if "pls" in joined:
        return "PLS"
    if "cog" in joined:
        return "COG"
    if "cof" in joined:
        return "COF"
    if "ips" in joined:
        return "IPS"
    if "tft" in joined:
        return "TFT"

    return DISPLAY_TYPE_CODES.get(product.display_type or "", None) or _slug_token(
        product.display_type, max_len=12
    )


def _display_grade_code(product: Product) -> str | None:
    texts = _normalized_display_texts(product)
    joined = " ".join(texts)

    if "биток" in joined:
        return "BTK"
    if "orig1" in joined or "ориг1" in joined:
        return "OR1"
    if "orig100" in joined or "ориг100" in joined:
        return "OR1"
    if any(token in joined for token in ("с разбора", "снятый", "снятая", "clean")):
        return "PUL"

    if any(token in joined for token in ("переклей", "refurb", "восстанов", "renewed")):
        return "RF"
    if re.search(r"\borig(?:100)?\b", joined) or "ориг" in joined:
        return "OR"
    if any(token in joined for token in ("premium", "aaa", "ааа", "hq")):
        return "CPH"
    if any(token in joined for token in ("medium", "optima", "аналог", "anal", "std", "standard")):
        return "CPM"
    if any(token in joined for token in ("low", "эконом", "econom", "cheap")):
        return "CPL"

    normalized_quality = product.display_quality or product.quality
    if normalized_quality:
        code = DISPLAY_GRADE_CODES.get(normalized_quality)
        if code:
            return code

    raw_quality = product.display_quality_raw or product.quality_raw
    if raw_quality:
        raw_lower = raw_quality.lower()
        if "биток" in raw_lower:
            return "BTK"
        if "orig1" in raw_lower or "ориг1" in raw_lower:
            return "OR1"
        if "orig100" in raw_lower or "ориг100" in raw_lower:
            return "OR1"
        if any(token in raw_lower for token in ("с разбора", "снятый", "снятая", "clean")):
            return "PUL"
        if "orig" in raw_lower or "ориг" in raw_lower:
            return "OR"
        if any(token in raw_lower for token in ("переклей", "refurb", "восстанов")):
            return "RF"
        if any(token in raw_lower for token in ("high", "premium", "aaa", "hq")):
            return "CPH"
        if any(token in raw_lower for token in ("low", "эконом", "econom", "cheap")):
            return "CPL"
        if any(token in raw_lower for token in ("optima", "medium", "anal", "аналог", "std")):
            return "CPM"

    return None


def _display_color_code(product: Product) -> str | None:
    explicit = _find_code(product.color, COLOR_CODES) or _slug_token(product.color, max_len=8)
    if explicit:
        return explicit
    name = (product.name or "").lower()
    for raw, code in COLOR_CODES.items():
        pattern = rf"(?<![0-9a-zа-я]){re.escape(raw)}(?![0-9a-zа-я])"
        if re.search(pattern, name):
            return code
    return None


def _display_series_code(product: Product) -> str | None:
    values = [product.manufacturer, product.name]
    for value in values:
        cleaned = _normalize_free_text(value)
        if not cleaned:
            continue
        upper = cleaned.upper()
        for code in DISPLAY_SERIES_CODES:
            if re.search(rf"(?<![A-Z0-9]){code}(?![A-Z0-9])", upper):
                return code
    return None


def _display_variant_rev(product: Product) -> str | None:
    name = (product.name or "").lower()
    parts: list[str] = []
    is_xiaomi_display = any(
        token in name for token in ("xiaomi", "redmi", "poco", "pocophone", "mi ", "mi-", "mi/")
    )
    is_oppo_family_display = any(
        token in name
        for token in ("oppo", "realme", "oneplus", "reno", "narzo", "find ", "rx17", "nord")
    )
    is_samsung_display = any(token in name for token in ("samsung", "galaxy", "sm-"))
    is_apple_display = any(token in name for token in ("apple", "iphone", "ipad", "ipod", "watch"))
    is_lenovo_display = any(
        token in name for token in ("lenovo", "ideaphone", "ideatab", "legion y700", "xiaoxin")
    )
    is_misc_display = any(
        token in name for token in ("google", "pixel", "vivo", "tecno", "infinix", "zte", "nubia")
    )
    has_frame = "в рамке" in name
    has_sp = "service pack" in name or "(sp" in name or " sp)" in name
    has_inner = "внутренний" in name
    has_outer = "внешний" in name

    apple_clean = is_apple_display and "als" in name and "clean" in name

    if is_xiaomi_display:
        if has_frame and has_sp and has_inner:
            parts.append("FSI")
        elif has_frame and has_sp and has_outer:
            parts.append("FSO")
        elif has_frame and has_sp:
            parts.append("FS")
        elif has_frame:
            parts.append("FR")
    elif is_oppo_family_display and has_frame:
        parts.append("FR")
    elif is_lenovo_display and has_frame:
        parts.append("FR")
    elif is_samsung_display:
        if has_frame and has_sp and has_inner:
            parts.append("FSI")
        elif has_frame and has_sp and has_outer:
            parts.append("FSO")
        elif has_frame and has_sp:
            parts.append("F")
        elif has_frame and "full size" in name:
            parts.append("FF")
        elif has_frame and "small size" in name:
            parts.append("FS")
        elif has_frame:
            parts.append("FR")
    elif is_misc_display:
        if has_frame and has_sp and has_inner:
            parts.append("FSI")
        elif has_frame and has_sp and has_outer:
            parts.append("FSO")
        elif has_frame and has_sp:
            parts.append("F")
        elif has_frame and has_inner:
            parts.append("FI")
        elif has_frame and has_outer:
            parts.append("FO")
        elif has_frame and "full size" in name:
            parts.append("FF")
        elif has_frame and "small size" in name:
            parts.append("FS")
        elif has_frame:
            parts.append("FR")
    length_match = re.search(r"\b(162|164|166)\s*mm\b", name)

    if is_apple_display and has_sp and "als" in name and "/" in name:
        parts.append("SA2")
    elif apple_clean:
        parts.append("AC2" if "/" in name else "AC")
    else:
        if not (is_xiaomi_display and has_frame and has_sp):
            if not ((is_samsung_display or is_misc_display) and has_frame and has_sp):
                if has_sp:
                    parts.append("SP")
        if "als" in name:
            parts.append("ALS")

    if length_match:
        length_code = {"162": "62", "164": "64", "166": "66"}[length_match.group(1)]
        if has_sp:
            parts.append(length_code)
        else:
            parts.append(f"L{length_match.group(1)}")
    if match := re.search(r"\brev\.?\s*0*(\d+)\b", name):
        parts.append(f"R{match.group(1)}")
    if "papermatte" in name:
        parts.append("PM")
    if "отверстием под кнопку home" in name:
        parts.append("HM")
    if "clean" in name and not apple_clean:
        parts.append("CLN")
    if "широкий коннектор" in name:
        parts.append("WC")
    if "узкий коннектор" in name:
        parts.append("NC")
    if "full size" in name and not ((is_samsung_display or is_misc_display) and has_frame):
        parts.append("FS")
    if "small size" in name and not ((is_samsung_display or is_misc_display) and has_frame):
        parts.append("SS")
    if has_inner and not (
        (is_xiaomi_display and has_frame and has_sp)
        or (is_samsung_display and has_frame and has_sp and has_inner)
        or (is_misc_display and has_frame and has_inner)
    ):
        parts.append("IN")
    if has_outer and not (
        (is_xiaomi_display and has_frame and has_sp)
        or (is_samsung_display and has_frame and has_sp and has_outer)
        or (is_misc_display and has_frame and has_outer)
    ):
        parts.append("OT")

    multi_count = len(
        re.findall(r"iphone\s+[0-9a-z]+\s*(?:mini|plus|pro max|pro|max|air|e)?", name)
    )
    if "samsung" in name or "galaxy" in name or "sm-" in name:
        samsung_multi = len(
            re.findall(r"(?:galaxy\s+[a-z0-9+ ]+|sm-[a-z0-9]+|[agjmnprstfl]\d{3,4}[a-z]?)", name)
        )
        if "/" in name and samsung_multi >= 2:
            parts.append(str(min(samsung_multi, 9)))
    if "/" in name and multi_count >= 2 and "SA2" not in parts:
        parts.append(str(min(multi_count, 9)))

    if not parts:
        return None
    return "-".join(parts)


def _battery_key(product: Product) -> str | None:
    if product.battery_capacity_mah:
        prefix = "HC" if product.battery_is_high_capacity else ""
        return f"{prefix}{product.battery_capacity_mah}"

    name = (product.name or "").lower()
    has_premium_marker = _has_parenthesized_token(product.name, "premium", "premimum")
    has_orig_marker = _has_parenthesized_token(
        product.name, "orig100", "orig", "original", "оригинал"
    )
    high_capacity = bool(
        product.battery_is_high_capacity
        or any(
            token in name
            for token in (
                "усилен",
                "повышенной емкости",
                "high+",
                "high capacity",
                "special edition",
            )
        )
    )
    if match := re.search(r"\b(\d{3,5})\s*(?:mah|м?ач|м?ah)\b", name):
        prefix = "HC" if high_capacity else ""
        return f"{prefix}{match.group(1)}"

    if match := re.search(
        r"\b("
        r"blp\d{2,4}[a-z]*|"
        r"eb-[a-z0-9-]{5,}|"
        r"hb[0-9a-z]{5,}|"
        r"li[0-9a-z]{8,}|"
        r"bm[0-9a-z]{2,}|"
        r"bn[0-9a-z]{2,}|"
        r"c11p[0-9a-z]{2,}|"
        r"hq-?[0-9a-z]{2,}|"
        r"bt\d{2,4}[a-z]*|"
        r"ba\d{2,4}[a-z]*"
        r")\b",
        name,
    ):
        return _slug_token(match.group(1).upper(), max_len=16)

    for raw_token in reversed(re.findall(r"\(([^()]+)\)", product.name or "")):
        token = raw_token.strip().upper().replace("А", "A")
        if token in {
            "F5ENERGY",
            "DEJI",
            "PREMIUM",
            "ORIG",
            "ORIG100",
            "GENUNE",
            "GENUINE",
            "SPECIAL EDITION",
        }:
            continue
        if any(
            noise in token
            for noise in (
                "ORIG",
                "PREMIUM",
                "HIGH",
                "SPECIAL",
                "ORIGINAL",
                "LATE",
                "EARLY",
                "MID",
                "MM",
                "ММ",
                "В",
                "MAH",
                "МАЧ",
            )
        ):
            continue
        if " " in token:
            continue
        if not (4 <= len(token) <= 16):
            continue
        if not re.search(r"[A-Z]", token) or not re.search(r"\d", token):
            continue
        candidate = _slug_token(token, max_len=16)
        if candidate:
            return candidate

    if high_capacity:
        return "HC"
    if has_premium_marker:
        return "PRM"
    if has_orig_marker or any(token in name for token in ("100% оригинал", "100 оригинал")):
        return "OR"

    return None


def _battery_variant_rev(product: Product) -> str | None:
    name = (product.name or "").lower()
    has_premium_marker = _has_parenthesized_token(product.name, "premium", "premimum")
    has_orig100_marker = _has_parenthesized_token(product.name, "orig100")
    has_orig_marker = _has_parenthesized_token(product.name, "orig", "original", "оригинал")
    parts: list[str] = []

    if has_orig100_marker or "100% оригинал" in name or "100 оригинал" in name:
        parts.append("OR")
    elif has_orig_marker:
        parts.append("OR")
    elif has_premium_marker:
        parts.append("PR")
    elif any(
        token in name
        for token in ("high+", "high capacity", "special edition", "усиленн", "повышенной емкости")
    ):
        parts.append("HC")

    if "sp" in name:
        parts.append("SP")
    if "system diagnosable" in name or "system daignosable" in name:
        parts.append("SD")
    if "без шлейфа" in name:
        parts.append("NF")
    if "exynos" in name:
        parts.append("EXY")
    if "обратная полярность" in name:
        parts.append("RP")
    if "дополнительным коннектором" in name:
        parts.append("DC")
    if "снятый" in name:
        parts.append("SN")
    if "в кейс" in name:
        parts.append("CK")
    if "deji" in name:
        parts.append("DJ")
    if "заряженные" in name:
        parts.append("ZR")
    if "ic china" in name:
        parts.append("IC")
    if "f5energy" in name:
        parts.append("F5")
    if "genune" in name or "genuine" in name:
        parts.append("GN")
    if "/" in name and "premium" in name:
        if "honor 50" in name and "nova 9" in name:
            parts.append("H50")

    if not parts:
        return None
    return "-".join(parts[:3])


def _normalize_battery_rev_for_key(key_code: str | None, rev: str | None) -> str | None:
    if not key_code or not rev:
        return rev
    tokens = [token for token in rev.split("-") if token]
    leading_map = {
        "PRM": "PR",
        "HC": "HC",
        "OR": "OR",
    }
    duplicate = leading_map.get(key_code)
    if duplicate and tokens and tokens[0] == duplicate:
        tokens = tokens[1:]
    return "-".join(tokens) or None


def _cable_key(product: Product) -> str | None:
    right = _find_code(product.cable_connector_output, CONNECTOR_CODES) or _slug_token(
        product.cable_connector_output, max_len=8
    )
    length = _normalize_length(product.cable_length)
    color = _find_code(product.color, COLOR_CODES) or _slug_token(product.color, max_len=8)
    if not all([right, length]):
        return None
    return "-".join(_compact_parts([right, length, color]))


def _charger_key(product: Product) -> str | None:
    technology = _find_code(product.charger_technology, CHARGER_TECH_CODES) or _slug_token(
        product.charger_technology, max_len=8
    )
    plug = _find_code(product.charger_plug_type, PLUG_CODES) or _slug_token(
        product.charger_plug_type, max_len=8
    )
    return "-".join(_compact_parts([technology, plug])) or None


def _camera_key(product: Product) -> str | None:
    position = _slug_token(product.camera_position, max_len=8)
    mp = _normalize_mp(product.camera_megapixels)
    return "-".join(_compact_parts([position, mp])) or None


def _glass_key(product: Product) -> str | None:
    color = _find_code(product.color, COLOR_CODES) or _slug_token(product.color, max_len=8)
    glass_type = _slug_token(product.glass_type, max_len=12)
    glass_form = _slug_token(product.glass_form, max_len=12)
    value = "-".join(_compact_parts([color, glass_type, glass_form]))
    return value or None


def infer_key(product: Product, category_code: str | None = None) -> str | None:
    category = category_code or infer_category_code(product)
    if category == "BAT":
        return _battery_key(product)
    if category == "DSP":
        key = _display_key(product)
        if key:
            return key
        name = (product.name or "").lower()
        if "samsung" in name or "galaxy" in name:
            return "STD"
        if any(
            token in name for token in ("xiaomi", "redmi", "poco", "pocophone", "mi ", "mi-", "mi/")
        ):
            return "STD"
        if any(
            token in name for token in ("huawei", "honor", "mediapad", "matepad", "pura", "nova ")
        ):
            return "STD"
        if any(
            token in name
            for token in ("oppo", "realme", "oneplus", "reno", "narzo", "find ", "rx17")
        ):
            return "STD"
        return None
    if category == "CBL":
        return _cable_key(product)
    if category == "CHR":
        return _charger_key(product)
    if category == "CAM":
        return _camera_key(product)
    if category == "FLX":
        return _slug_token(product.flex_purpose, max_len=24)
    if category == "IC":
        return _slug_token(product.chip_code, max_len=24)
    if category == "GLS":
        return _glass_key(product)
    if category == "PRT":
        return _slug_token(product.part_type, max_len=24)
    if category == "SET":
        if product.set_quantity:
            return f"KIT-{product.set_quantity}PCS"
        return _slug_token(product.set_composition, max_len=24)
    return None


def infer_rev(product: Product, category_code: str | None = None) -> str | None:
    category = category_code or infer_category_code(product)
    if category == "DSP":
        parts = _compact_parts([_display_series_code(product), _display_variant_rev(product)])
        return "-".join(parts) or None
    if category == "BAT":
        return _battery_variant_rev(product)
    return None


def _xiaomi_family_conflict_rev(name: str | None) -> str | None:
    if not name:
        return None
    lower = name.lower()
    if not any(
        token in lower for token in ("xiaomi", "redmi", "poco", "pocophone", "mi ", "mi-", "mi/")
    ):
        return None
    if "13t pro" in lower and "13t (" in lower and "/" in lower:
        return "13T"
    family_markers = (
        ("redmi note 14 pro+ 5g", "RN14"),
        ("redmi note 14 pro 5g", "RN14"),
        ("mi 11x pro", "11XP"),
        ("redmi k40 pro", "K40P"),
        ("redmi k40", "K40"),
        ("mi 11i", "11I"),
        ("poco c51", "C51"),
        ("redmi a1+", "A1P"),
        ("redmi a1", "A1"),
        ("poco x3", "PX3"),
    )
    for marker, code in family_markers:
        if marker in lower:
            return code
    return None


def _huawei_conflict_rev(name: str | None) -> str | None:
    if not name:
        return None
    lower = name.lower()
    if "в рамке" in lower:
        return "FR"
    if match := re.search(r"\b(42|41|44|46)\s*мм\b", lower):
        return match.group(1)
    if "wi fi" in lower or "wifi" in lower or "wi-fi" in lower:
        return "WFI"
    if "lte" in lower:
        return "LTE"
    markers = (
        ("papermatte", "PM"),
        ("lite e", "LTE"),
        ("pro", "PRO"),
        ("prime", "PRM"),
        ("2017", "17"),
        ("2025", "25"),
    )
    for marker, code in markers:
        if marker in lower:
            return code
    return None


def _conflict_sequence_rev(
    session: Session,
    product: Product,
    device_code: str,
    key_code: str,
) -> str | None:
    normalized_name = _normalize_free_text(product.name)
    if not normalized_name:
        return None
    rows = session.execute(
        select(Product.id, Product.article)
        .join(ProductSkuPlan, ProductSkuPlan.product_id == Product.id)
        .where(
            ProductSkuPlan.is_active.is_(True),
            Product.id != product.id,
            Product.name == normalized_name,
            ProductSkuPlan.device_code == device_code,
            ProductSkuPlan.key_code == key_code,
        )
    ).all()
    if not rows:
        return None
    candidates = [(product.id, product.article or "")]
    candidates.extend((row[0], row[1] or "") for row in rows)
    ordered = sorted(candidates, key=lambda item: (item[1], item[0]))
    for index, (product_id, _) in enumerate(ordered, start=1):
        if product_id == product.id and index > 1:
            return str(index)
    return None


def _conflict_fallback_rev(
    session: Session,
    product: Product,
    *,
    category_code: str,
    device_code: str,
    key_code: str,
    current_rev: str | None,
) -> str | None:
    if category_code != "DSP":
        return None

    sequence_rev = _conflict_sequence_rev(session, product, device_code, key_code)
    if sequence_rev:
        parts = _compact_parts([current_rev, sequence_rev])
        return "-".join(parts)

    family_rev = _xiaomi_family_conflict_rev(product.name)
    if family_rev:
        parts = _compact_parts([current_rev, family_rev])
        if parts:
            return "-".join(parts)

    huawei_rev = _huawei_conflict_rev(product.name)
    if huawei_rev and any(
        token in (product.name or "").lower()
        for token in ("huawei", "honor", "mediapad", "matepad", "nova ", "p smart", "y")
    ):
        parts = _compact_parts([current_rev, huawei_rev])
        if parts:
            return "-".join(parts)

    return None


def _active_plan(product: Product) -> ProductSkuPlan | None:
    return next((plan for plan in product.sku_plans if plan.is_active), None)


def _sync_status(
    fact_sku: str | None, planned_sku: str | None, plan_status: str
) -> tuple[str, str | None]:
    if plan_status != "generated":
        return "manual_review", None
    if planned_sku and fact_sku:
        if planned_sku == fact_sku:
            return "match", None
        return "mismatch", f"fact_sku:{fact_sku};planned_sku:{planned_sku}"
    if planned_sku and not fact_sku:
        return "missing_in_1c", None
    if fact_sku and not planned_sku:
        return "missing_plan", None
    return "manual_review", None


def generate_sku_for_product(session: Session, product: Product) -> SkuGenerationResult:
    reasons: list[str] = []
    if _has_duplicate_marker(product.name):
        return SkuGenerationResult(
            planned_sku=None,
            status="manual_review",
            reasons=["duplicate_card"],
        )
    current_plan = _active_plan(product)
    rev = infer_rev(product)
    brand_code = infer_brand_code(product)
    if not brand_code:
        reasons.append("missing_brand_code")
    category_code = infer_category_code(product)
    if not category_code:
        reasons.append("missing_category_code")
    if rev is None and category_code != "BAT":
        rev = current_plan.rev if current_plan else None
    device_code = infer_device_code(product, category_code)
    if not device_code:
        reasons.append("missing_device_code")
    key_code = infer_key(product, category_code)
    if not key_code:
        reasons.append("missing_key_code")
    elif category_code == "BAT":
        rev = _normalize_battery_rev_for_key(key_code, rev)

    if reasons:
        return SkuGenerationResult(
            planned_sku=None,
            status="manual_review",
            brand_code=brand_code,
            category_code=category_code,
            device_code=device_code,
            key_code=key_code,
            rev=rev,
            reasons=reasons,
        )

    try:
        planned_sku = build_sku(brand_code, category_code, device_code, key_code, rev)
    except SkuValidationError as exc:
        return SkuGenerationResult(
            planned_sku=None,
            status="incomplete",
            brand_code=brand_code,
            category_code=category_code,
            device_code=device_code,
            key_code=key_code,
            rev=rev,
            reasons=[str(exc)],
        )

    existing = session.execute(
        select(ProductSkuPlan).where(
            ProductSkuPlan.planned_sku == planned_sku,
            ProductSkuPlan.product_id != product.id,
            ProductSkuPlan.is_active.is_(True),
        )
    ).scalar_one_or_none()
    if existing is not None:
        fallback_rev = _conflict_fallback_rev(
            session,
            product,
            category_code=category_code,
            device_code=device_code,
            key_code=key_code,
            current_rev=rev,
        )
        if fallback_rev and fallback_rev != rev:
            try:
                fallback_sku = build_sku(
                    brand_code,
                    category_code,
                    device_code,
                    key_code,
                    fallback_rev,
                )
            except SkuValidationError:
                fallback_sku = None
            if fallback_sku:
                fallback_existing = session.execute(
                    select(ProductSkuPlan).where(
                        ProductSkuPlan.planned_sku == fallback_sku,
                        ProductSkuPlan.product_id != product.id,
                        ProductSkuPlan.is_active.is_(True),
                    )
                ).scalar_one_or_none()
                if fallback_existing is None:
                    return SkuGenerationResult(
                        planned_sku=fallback_sku,
                        status="generated",
                        brand_code=brand_code,
                        category_code=category_code,
                        device_code=device_code,
                        key_code=key_code,
                        rev=fallback_rev,
                        reasons=[],
                    )
        return SkuGenerationResult(
            planned_sku=None,
            status="conflict",
            brand_code=brand_code,
            category_code=category_code,
            device_code=device_code,
            key_code=key_code,
            rev=rev,
            reasons=[f"sku_conflict:{planned_sku}"],
        )

    return SkuGenerationResult(
        planned_sku=planned_sku,
        status="generated",
        brand_code=brand_code,
        category_code=category_code,
        device_code=device_code,
        key_code=key_code,
        rev=rev,
        reasons=[],
    )


def sync_product_sku_status(product: Product, plan_status: str | None = None) -> None:
    current_plan = _active_plan(product)
    status, error = _sync_status(
        product.fact_sku,
        product.planned_sku,
        plan_status or (current_plan.status if current_plan else "manual_review"),
    )
    product.sku_sync_status = status
    product.sku_sync_error = error


def apply_sku_generation_result(
    session: Session,
    product: Product,
    result: SkuGenerationResult,
    *,
    source: str = "rules",
) -> ProductSkuPlan:
    for plan in product.sku_plans:
        if plan.is_active:
            plan.is_active = False

    plan = ProductSkuPlan(
        product=product,
        planned_sku=result.planned_sku,
        brand_code=result.brand_code,
        category_code=result.category_code,
        device_code=result.device_code,
        key_code=result.key_code,
        rev=result.rev,
        status=result.status,
        error_reason=";".join(result.reasons) if result.reasons else None,
        source=source,
        is_active=True,
    )
    session.add(plan)
    product.planned_sku = result.planned_sku
    sync_product_sku_status(product, result.status)
    return plan


def generate_sku_batch(
    session: Session,
    *,
    product_ids: Iterable[int] | None = None,
    dry_run: bool = True,
    only_missing: bool = True,
) -> dict[str, object]:
    query = select(Product).order_by(Product.id)
    if product_ids:
        query = query.where(Product.id.in_(list(product_ids)))
    if only_missing:
        query = query.where(Product.planned_sku.is_(None))
    products = session.execute(query).scalars().all()

    results: list[dict[str, object]] = []
    generated = 0
    for product in products:
        result = generate_sku_for_product(session, product)
        results.append(
            {
                "product_id": product.id,
                "article": product.article,
                "fact_sku": product.fact_sku,
                "planned_sku": result.planned_sku,
                "status": result.status,
                "sync_status": _sync_status(product.fact_sku, result.planned_sku, result.status)[0],
                "reasons": result.reasons,
            }
        )
        if not dry_run:
            apply_sku_generation_result(session, product, result)
        if result.status == "generated":
            generated += 1

    if not dry_run:
        session.commit()

    return {
        "total": len(products),
        "generated": generated,
        "manual_review": sum(1 for item in results if item["status"] == "manual_review"),
        "conflict": sum(1 for item in results if item["status"] == "conflict"),
        "incomplete": sum(1 for item in results if item["status"] == "incomplete"),
        "dry_run": dry_run,
        "items": results,
    }
