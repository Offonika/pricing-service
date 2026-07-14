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

CHARGER_SERIES_CODES = {
    "tiny star": "TSTAR",
    "circular": "CIRC",
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
    "черный глянец": "BLKGL",
    "чёрный глянец": "BLKGL",
    "черный": "BLK",
    "чёрный": "BLK",
    "черная": "BLK",
    "чёрная": "BLK",
    "белый": "WHT",
    "белая": "WHT",
    "синий": "BLU",
    "синяя": "BLU",
    "темно-синий": "DBL",
    "тёмно-синий": "DBL",
    "темно-синяя": "DBL",
    "тёмно-синяя": "DBL",
    "голубой": "CYN",
    "голубая": "CYN",
    "бирюзовый": "TRQ",
    "бирюзовая": "TRQ",
    "темно-зеленый": "DGR",
    "тёмно-зеленый": "DGR",
    "темно-зелёный": "DGR",
    "тёмно-зелёный": "DGR",
    "зеленый": "GRN",
    "зелёный": "GRN",
    "зеленая": "GRN",
    "зелёная": "GRN",
    "красный": "RED",
    "красная": "RED",
    "желтый": "YLW",
    "жёлтый": "YLW",
    "желтая": "YLW",
    "жёлтая": "YLW",
    "золотой": "GLD",
    "серый": "GRY",
    "серая": "GRY",
    "серебристый": "SLV",
    "серебристая": "SLV",
    "золотистый": "GLD",
    "золотистая": "GLD",
    "фиолетовый": "PRP",
    "темно-фиолетовый": "DPR",
    "тёмно-фиолетовый": "DPR",
    "фиолетовая": "PRP",
    "розовый": "PNK",
    "розовая": "PNK",
    "оранжевый": "ORG",
    "оранжевая": "ORG",
    "коричневый": "BRN",
    "коричневая": "BRN",
    "коралловый": "COR",
    "бронзовый": "BRZ",
    "титановый": "TIT",
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
    "doogee": "DOG",
    "itel": "ITE",
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


def _normalize_accessory_name(name: str | None) -> str:
    cleaned = (name or "").lower()
    cleaned = cleaned.replace("‑", "-").replace("–", "-").replace("—", "-")
    cleaned = cleaned.replace("usb-с", "usb-c").replace("type-с", "type-c")
    cleaned = cleaned.replace("typec", "type-c")
    cleaned = cleaned.replace("type c", "type-c")
    cleaned = cleaned.replace("usb‑c", "usb-c")
    cleaned = cleaned.replace("usb c", "usb-c")
    return re.sub(r"\s+", " ", cleaned).strip()


def _connector_code_from_text(value: str | None) -> str | None:
    cleaned = _normalize_accessory_name(value)
    if not cleaned:
        return None
    if re.search(r"\brj[-\s]?45\b", cleaned):
        return "RJ45"
    if re.search(r"\bhdmi\b", cleaned):
        return "HDMI"
    if re.search(r"\b30[-\s]?pin\b", cleaned):
        return "30P"
    if re.search(r"\b3\.5\s*jack\b", cleaned):
        return "AUX"
    if "apple watch magnetic" in cleaned:
        return "AWMG"
    if re.search(r"\b(?:type-c|usb-c|usbc)\b", cleaned):
        return "USBC"
    if "lightning" in cleaned or "8 pin" in cleaned:
        return "LTN"
    if re.search(r"\bmicro[-\s]?usb\b", cleaned) or "microusb" in cleaned:
        return "MUSB"
    if re.search(r"\b(?:usb-a|usb a)\b", cleaned):
        return "USBA"
    if re.search(r"(?<![-a-z0-9])usb(?![-a-z0-9])", cleaned):
        return "USBA"
    if "magsafe" in cleaned:
        return "MGS"
    return None


def _accessory_connector_pair_from_name(name: str | None) -> tuple[str | None, str | None]:
    normalized = _normalize_accessory_name(name)
    if not normalized:
        return None, None

    if "патч-корд" in normalized:
        return "RJ45", "RJ45"

    connector_pattern = (
        r"(?:apple watch magnetic|3\.5\s*jack|30[-\s]?pin|usb-c|type-c|"
        r"micro[-\s]?usb|microusb|lightning|rj[-\s]?45|hdmi|usb-a|usb)"
    )
    if match := re.search(rf"({connector_pattern})\s*-\s*({connector_pattern})", normalized):
        return _connector_code_from_text(match.group(1)), _connector_code_from_text(match.group(2))
    if match := re.search(rf"({connector_pattern})\s+({connector_pattern})", normalized):
        return _connector_code_from_text(match.group(1)), _connector_code_from_text(match.group(2))

    connector = _connector_code_from_text(normalized)
    if connector == "RJ45":
        return "RJ45", "RJ45"
    return connector, None


def _length_from_name(name: str | None) -> str | None:
    normalized = _normalize_accessory_name(name).replace(",", ".")
    if not normalized:
        return None
    if match := re.search(r"(?<![a-z0-9-])(\d+(?:\.\d+)?)\s*(?:m|м)\b", normalized):
        return _normalize_length(f"{match.group(1)} m")
    if match := re.search(r"(?<![a-z0-9-])(\d+(?:\.\d+)?)\s*(?:cm|см)\b", normalized):
        return _normalize_length(f"{match.group(1)} cm")
    return None


def _power_w_from_name(name: str | None) -> str | None:
    normalized = _normalize_accessory_name(name)
    if match := re.search(r"(\d{1,3})\s*(?:w|вт)\b", normalized):
        return f"{match.group(1)}W"
    return None


def _capacity_mah_from_name(name: str | None) -> str | None:
    normalized = _normalize_accessory_name(name)
    if match := re.search(r"(\d{3,5})\s*(?:mah|м\s*а\s*ч|мач|мah)\b", normalized):
        return f"{match.group(1)}MAH"
    return None


def _accessory_model_code_from_name(name: str | None) -> str | None:
    normalized = _normalize_accessory_name(name)
    patterns = (
        r"\b(rc[-\s]?c\d{2,4}[a-z]?)\b",
        r"\b(rpp[-\s]?\d{1,4}[a-z]?)\b",
        r"\b(rc[-\s]?\d{1,4}[a-z]?)\b",
        r"\b(wlx[-\s]?\d{1,4}\+?)\b",
        r"\b(rl[-\s]?\d{2,4}[a-z]?)\b",
        r"\b(cd[-\s]?[a-z]?\d{1,3}[a-z]?)\b",
        r"\b(hbsd[-\s]?\d{2,4}[a-z]?)\b",
        r"\b(gp[-\s]?\d{2,4}[a-z]?)\b",
        r"\b(j\d{2,4}[a-z]?)\b",
        r"\b(fm\d{1,3})\b",
        r"\b(f[сc]\d{1,3})\b",
        r"\b(icharge\s*\d+[a-z]?)\b",
        r"\b([a-z]{2,4}[-\s]?cb\d{2,4}[a-z]?)\b",
        r"\b([a-z]{1,4}[-\s]?\d{2,4}[a-z]?)\b",
        r"\b([a-z]{2,6}[-\s]?[a-z]{0,3}\d{2,4}[a-z]?)\b",
        r"\b(us\d{2,4})\b",
        r"\b(u\d{2,4})\b",
    )
    for pattern in patterns:
        if match := re.search(pattern, normalized):
            model = match.group(1).replace("+", "P").replace("с", "c").replace("-", "")
            model = re.sub(r"\s+", "", model)
            if (
                model.startswith(("orig", "typec", "usbc", "usb"))
                or "rj45" in model
                or re.fullmatch(r"[a-z]?\d{1,3}w", model)
                or re.fullmatch(r"c\d{1,3}", model)
            ):
                continue
            return _slug_token(model, max_len=12)
    return None


def _accessory_port_flags_from_name(name: str | None) -> list[str]:
    normalized = _normalize_accessory_name(name)
    flags: list[str] = []
    if match := re.search(r"\b(\d)\s*usb\s+порт", normalized):
        flags.append(f"{match.group(1)}USB")
    if match := re.search(r"\b(\d)\s*usb\s*a\b", normalized):
        flags.append(f"{match.group(1)}USBA")
    if match := re.search(r"\b(\d)\s*usb-c\b", normalized):
        flags.append(f"{match.group(1)}USBC")
    if "с дисплеем" in normalized:
        flags.append("DSP")
    if "беспровод" in normalized or "wireless" in normalized:
        flags.append("WL")
    if "magsafe" in normalized:
        flags.append("MGS")
    if "со встроенными кабелями" in normalized or "с кабелями" in normalized:
        flags.append("BCBL")
    return flags


def _cable_feature_flags_from_name(name: str | None) -> list[str]:
    normalized = _normalize_accessory_name(name)
    flags: list[str] = []
    series_flags = (
        ("lit button-control", "LBC"),
        ("new braided", "NBR"),
        ("cristal", "CRS"),
        ("cafule", "CAF"),
        ("palm", "PALM"),
        ("plus", "PL"),
    )
    for marker, code in series_flags:
        if marker in normalized:
            flags.append(code)
            break
    if re.search(r"\b4k\s*60", normalized):
        flags.append("4K60")
    elif re.search(r"\b4k\b", normalized):
        flags.append("4K")
    if "быстрой заряд" in normalized:
        flags.append("FCH")
    if "углов" in normalized:
        flags.append("ANG")
    if "левый" in normalized:
        flags.append("L")
    elif "правый" in normalized:
        flags.append("R")
    return flags


def _included_cable_flag_from_name(name: str | None) -> str | None:
    normalized = _normalize_accessory_name(name)
    if "+ кабель" not in normalized and "с кабелем" not in normalized:
        return None
    left, right = _accessory_connector_pair_from_name(normalized)
    if not left and not right:
        return "CBL"
    pair = "".join(_compact_parts([left, right]))
    short_pairs = {
        "USBCUSBC": "CTC",
        "USBCLTN": "CLTN",
        "USBAUSBC": "CUSBC",
        "USBAMUSB": "CMUSB",
        "USBALTN": "CLTN",
    }
    return short_pairs.get(pair, f"C{pair}"[:8])


def _accessory_quality_flags(product: Product) -> list[str]:
    name = _normalize_accessory_name(product.name)
    flags: list[str] = []
    if "тех. пак" in name or "тех пак" in name:
        flags.append("TPK")
    if "taiwan" in name:
        flags.append("TWN")
    if "orig+" in name:
        flags.append("ORP")
        grade = None
    elif re.search(r"\(\s*aaa\s*\)", name):
        flags.append("AAA")
        grade = None
    elif re.search(r"\(\s*aa\s*\)", name):
        flags.append("AA")
        grade = None
    elif "copy" in name:
        flags.append("CPY")
        grade = None
    else:
        grade = _name_grade_code(product)
    if grade:
        flags.append(grade)
    return flags


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


def _length_fallback_key_codes(category_code: str, key_code: str) -> list[str]:
    variants: list[str] = []
    parts = key_code.split("-")

    if category_code == "CBL":
        color_parts = {
            *COLOR_CODES.values(),
            "BLBK",
            "BKBL",
            "BKOR",
            "TBLU",
            "LBLU",
            "DRED",
        }
        without_fast_charge = [part for part in parts if part != "FCH"]
        if without_fast_charge != parts:
            variants.append("-".join(without_fast_charge))
        for candidate in (parts, without_fast_charge):
            without_color = [part for part in candidate if part not in color_parts]
            if without_color != candidate:
                variants.append("-".join(without_color))

    if category_code == "GLS":
        without_part_numbers = [
            part for part in parts if not re.fullmatch(r"(?:GL|ES)\d{1,3}", part)
        ]
        if without_part_numbers != parts:
            variants.append("-".join(without_part_numbers))
        candidates = [parts, without_part_numbers]
        for candidate in candidates:
            if candidate and candidate[0] == "MODG":
                compact = ["MG", *candidate[1:]]
                variants.append("-".join(compact))
            if "MST" in candidate and "OCA" in candidate:
                without_oca = [part for part in candidate if part != "OCA"]
                variants.append("-".join(without_oca))
                if without_oca and without_oca[0] == "MODG":
                    variants.append("-".join(["MG", *without_oca[1:]]))

    if category_code == "DSP" and parts and parts[0] == "MAT":
        without_matrix_marker = parts[1:]
        variants.append("-".join(without_matrix_marker))
        for grade_part in ("ASP", "OEM"):
            without_grade = [part for part in without_matrix_marker if part != grade_part]
            if without_grade != without_matrix_marker:
                variants.append("-".join(without_grade))

    if category_code == "PRT":
        if parts and parts[0] == "BCOV":
            variants.append("-".join(["BCV", *parts[1:]]))
        if parts and parts[0] == "HOUS":
            variants.append("-".join(["HOU", *parts[1:]]))
        if parts and parts[0] == "SIMTRAY":
            variants.append("-".join(["SIMTR", *parts[1:]]))

    if category_code == "IC":
        if any(part in {"OR1", "PR", "REF", "OEM", "AAA", "A", "B"} for part in parts):
            mic_short = ["M" if part == "MIC" else part for part in parts]
            if mic_short != parts:
                variants.append("-".join(mic_short))
            chg_mic_short = [
                "C" if part == "CHG" else "M" if part == "MIC" else part for part in parts
            ]
            if chg_mic_short != parts:
                variants.append("-".join(chg_mic_short))
        without_grade = [
            part for part in parts if part not in {"OR1", "PR", "REF", "OEM", "AAA", "A", "B"}
        ]
        if without_grade != parts:
            variants.append("-".join(without_grade))

    if category_code == "FLX":
        without_grade = [
            part for part in parts if part not in {"OR1", "PR", "REF", "OEM", "AAA", "A", "B"}
        ]
        if without_grade != parts:
            variants.append("-".join(without_grade))
        if parts and parts[0] == "SIMTRAY":
            variants.append("-".join(["SIMTR", *parts[1:]]))

    return list(dict.fromkeys(variant for variant in variants if variant and variant != key_code))


def _length_fallback_revs(rev: str | None) -> list[str | None]:
    if rev and "-" in rev:
        return [rev.replace("-", "")]
    return []


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

    lower_name = (product.name or "").lower()
    if lower_name.startswith("тестер") or lower_name.startswith("tester"):
        return "OEM"

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
    lower_subject = (product.subject or "").lower()
    lower_kind = (product.vid_nomenklatury or "").lower()
    lower_category = (product.category or "").lower()
    accessory_name = _normalize_accessory_name(product.name)
    is_cooler = "вентилятор" in lower_name or "кулер" in lower_name or "кулер" in lower_category
    if (
        lower_name.startswith(("камера", "camera"))
        or lower_subject == "камера"
        or lower_kind == "камеры"
        or "камеры для" in lower_category
    ):
        return "CAM"
    if any(
        marker in accessory_name
        for marker in (
            "адаптер питания",
            "power adapter",
            "блок питания",
            "док-станция",
            "зарядная станция",
            "сетевое зарядное устройство",
            "беспроводное автомобильное зарядное",
            "внешний аккумулятор",
            "внешний накопитель",
            "накопитель для телефона",
            "пауэрбанк",
        )
    ):
        return "CHR"
    if "патч-корд" in accessory_name:
        return "CBL"
    if lower_name.startswith("тестер") or lower_name.startswith("tester"):
        return "TST"
    if lower_name.startswith("матрица") or lower_subject == "матрица":
        return "DSP"
    if (
        lower_name.startswith(("вентилятор", "кулер"))
        or "вентилятор" in lower_name
        or "кулер" in lower_name
    ):
        return "PRT"
    if "подсветка дисплея" in lower_name or lower_name.startswith("подсветка"):
        return "PRT"
    if lower_name.startswith("тачпад"):
        return "PRT"
    if lower_name.startswith("шлейф"):
        return "FLX"
    if "плата питания" in lower_name:
        return "IC"
    if _game_device_code_from_name(product.name) and (
        "разъем" in lower_name
        or "разъём" in lower_name
        or "антенн" in lower_name
        or "датчик" in lower_name
    ):
        return "FLX"
    if "отверт" in lower_name:
        return "SET"
    if _game_device_code_from_name(product.name) and any(
        marker in lower_name or marker in lower_category
        for marker in (
            "джостик",
            "геймпад",
            "стики",
            "стиков",
            "кноп",
            "накладки на стики",
            "накладки",
            "ремеш",
            "футляр",
            "подставк",
            "чехол",
            "чехлы",
            "сумка",
            "сумки",
            "передняя панель",
            "панель",
            "привод",
            "радиатор",
            "слайдер",
        )
    ):
        return "PRT"
    if (
        "пленка oca" in lower_name
        or "плёнка oca" in lower_name
        or "поляризационная пленка" in lower_name
        or "поляризационная плёнка" in lower_name
    ):
        return "PRT"
    if "магнит magsafe" in lower_name:
        return "PRT"
    if (
        "форма дисплея" in lower_name
        or "форма вакуумного подогрева" in lower_name
        or lower_name.startswith("форма ")
    ):
        return "PRT"
    if "резиновые ножки" in lower_name:
        return "PRT"
    if (
        "нижняя плата" in lower_name
        or "плата питания" in lower_name
        or lower_subject == "плата"
        or "платы и электронные компоненты" in lower_kind
        or "нижние платы" in lower_category
    ):
        return "IC"
    if (
        lower_name.startswith("шлейф")
        or lower_subject == "шлейф"
        or ("держатель" in lower_name and ("сим" in lower_name or "sim" in lower_name))
        or lower_subject == "держатель сим-карты"
    ):
        return "FLX"
    if (
        "нижняя плата" in lower_name
        or "плата питания" in lower_name
        or lower_subject == "плата"
        or "платы и электронные компоненты" in lower_kind
        or "нижние платы" in lower_category
    ):
        return "IC"
    if not is_cooler and (
        lower_subject in {"динамик", "сетка динамика", "вибромотор"}
        or lower_name.startswith(("динамик", "сеточка динамика", "вибромотор"))
        or "динамики для" in lower_category
        or "сеточк" in lower_category
        or "вибромотор" in lower_category
    ):
        return "PRT"
    if (
        lower_subject in {"наклейка", "изолятор"}
        or lower_name.startswith(("проклейка", "прокладка"))
        or "проклейк" in lower_category
        or "прокладк" in lower_category
    ):
        return "PRT"
    if (
        lower_name.startswith("задняя крыш")
        or lower_name.startswith("рамка диспле")
        or lower_name.startswith("кнопка")
        or lower_subject in {"крышка", "корпус"}
        or lower_subject in {"кнопка", "кнопки", "клавиатура"}
        or "задние крышки" in lower_category
        or "рамки дисплеев" in lower_category
        or "кнопки" in lower_category
        or "клавиатуры" in lower_category
        or "средние части" in lower_category
    ):
        return "PRT"

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
    if brand_norm in {"motorola", "lg", "fly", "jbl", "doogee"}:
        suffix = _misc_legacy_device_suffix(brand_norm, model_value, variant)
        if suffix:
            return _slug_token(f"{prefix}-{suffix}", max_len=24)
    if brand_norm in {"vivo", "tecno", "infinix", "itel"}:
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
    text = re.sub(r"(iphone\s*5)с\b", r"\1c", text)
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
        (
            r"\bapple\s+watch\s+se\s*(2022)\b.*\b(40|44)\b",
            lambda m: f"IPHWSE22{m.group(2)}",
        ),
        (
            r"\bwatch\s+se\s*(2022)\b.*\b(40|44)\b",
            lambda m: f"IPHWSE22{m.group(2)}",
        ),
        (
            r"\bapple\s+watch\s+ultra\s*(\d*)\b.*\b(49)\b",
            lambda m: f"IPHWU{m.group(1) or ''}{m.group(2)}",
        ),
        (
            r"\bwatch\s+ultra\s*(\d*)\b.*\b(49)\b",
            lambda m: f"IPHWU{m.group(1) or ''}{m.group(2)}",
        ),
        (r"\biphone\s*xs\s*max\b", "IPHXSM"),
        (r"\biphone\s*xs\b", "IPHXS"),
        (r"\biphone\s*xr\b", "IPHXR"),
        (r"\biphone\s*x\b", "IPHX"),
        (r"\biphone\s*6s\s*plus\b", "IPH6SP"),
        (r"\biphone\s*6s\b", "IPH6S"),
        (r"\biphone\s*5[cс]\b", "IPH5C"),
        (r"\biphone\s*4s\b", "IPH4S"),
        (r"\biphone\s*5s\s*/\s*iphone\s*se\b", "IPH5"),
        (r"\biphone\s*se\s*/\s*iphone\s*5s\b", "IPH5"),
        (r"\biphone\s*5s\b", "IPH5S"),
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
        (r"\bapple\s+watch\b.*\b(38|40|41|42|44|45|46)\b", lambda m: f"IPHW{m.group(1)}"),
        (r"\bwatch\b.*\b(38|40|41|42|44|45|46)\b", lambda m: f"IPHW{m.group(1)}"),
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
        (r"\bmacbook\s+air\s*13\.?6\b", "MBA136"),
        (r"\bmacbook\s+air\s*15\.?3\b", "MBA153"),
        (r"\bmacbook\s+neo\s*13\b.*\b(a3404)\b", "MBN13"),
        (r"\bmacbook\s+air\b.*\bmacbook\s+pro\b", "MBAP"),
        (r"\bmacbook\s*12\s*retina.*\b(a\d{4})\b", lambda m: f"MB12{m.group(1).upper()}"),
        (r"\bmacbook\s*12\b.*\b(a\d{4})\b", lambda m: f"MB12{m.group(1).upper()}"),
        (r"\bсовместим\s+с\s+ipad\s+pro\s*11", "IPDP1118"),
        (r"\bapple\s*5s\b", "IPH5S"),
        (r"\bipad\s*mini\s*2.*ipad\s*mini\s*3\b", "IPDMN23"),
        (r"\bipad\s*air\s*3\b", "IPDA3"),
        (r"\bipad\s*2\b", "IPD2"),
        (r"\bipad\s*\(a1219/a1337\)\b", "IPD1"),
        (r"\bipad\b.*\b(a1219|a1337)\b", "IPD1"),
        (r"\bipad\s*3\b.*\b(a1416|a1430)\b", "IPD3"),
        (r"\bipad\s*4\b.*\b(a1458|a1459|a1460)\b", "IPD4"),
        (r"\bipad\s*5\b.*\b9\.?7\b.*\b(2017|a1822|a1823)\b", "IPD5"),
        (r"\bipad\s*3.*ipad\s*4\b", "IPD34"),
        (r"\bipad\s*air\b.*\b(a1474|a1475|a1476)\b", "IPDA1"),
        (r"\bipad\s*air.*ipad\s*5\s*9\.?7\b", "IPDA15"),
        (r"\bipad\s*6\b.*\b9\.?7\b", "IPD697"),
        (r"\bipad\s*9\b.*\b10\.?2\b", "IPD9102"),
        (r"\bipad\s*7\b.*\b10\.?2\b.*\bipad\s*8\b.*\b10\.?2\b", "IPD78102"),
        (r"\bipad\s*8\b.*\b10\.?2\b.*\bipad\s*7\b.*\b10\.?2\b", "IPD78102"),
        (r"\bipad\s*7\b.*\b10\.?2\b.*\b8\b.*\b10\.?2\b", "IPD78102"),
        (r"\bipad\s*8\b.*\b10\.?2\b", "IPD8102"),
        (r"\bipad\s*7\b.*\b10\.?2\b", "IPD7102"),
        (r"\bipad\s*[789].*\b10\.?2\b", "IPD789102"),
        (r"\bipad\s*mini\s*7\b", "IPDMN7"),
        (r"\bipad\s*mini\s*6\b", "IPDMN6"),
        (r"\bipad\s*mini\s*5\b", "IPDMN5"),
        (r"\bipad\s*mini\s*4\b", "IPDMN4"),
        (r"\bipad\s*mini\s*3\b", "IPDMN3"),
        (r"\bipad\s*mini\b.*\bipad\s*mini\s*2\b", "IPDMN12"),
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
        (r"\bipad\s*pro.*\b12\.?9\b.*\b2018\b", "IPDP12918"),
        (r"\bipad\s*pro.*\b12\.?9\b.*\b2020\b", "IPDP12920"),
        (r"\bipad\s*pro.*\b12\.?9\b.*\b2021\b", "IPDP12921"),
        (r"\bipad\s*pro.*\b12\.?9\b.*\b2022\b", "IPDP12922"),
        (r"\bipad\s*pro\s*11\b.*\b2024\b.*\bipad\s*pro\s*13\b.*\b2024\b", "IPDP24"),
        (r"\bipad\s*pro\s*13\b.*\b2025\b", "IPDP1325"),
        (r"\bipad\s*pro\s*11\b.*\b2025\b", "IPDP1125"),
        (r"\bipad\s*pro\s*13\b.*\b2024\b", "IPDP1324"),
        (r"\bipad\s*pro\s*11\b.*\b2024\b", "IPDP1124"),
        (r"\bipad\s*pro.*\b11[.,]?0\b.*\b2018\b", "IPDP1118"),
        (r"\bipad\s*pro.*\b11[.,]?0\b.*\b2020\b", "IPDP1120"),
        (r"\bipad\s*pro.*\b11[.,]?0\b.*\b2021\b", "IPDP1121"),
        (r"\bipad\s*pro.*\b11[.,]?0\b.*\b2022\b", "IPDP1122"),
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
    if match := re.search(r"\b(s942|s947)\b", normalized):
        return {"S942": "S26", "S947": "S26P"}[match.group(1).upper()]
    if match := re.search(r"(?:galaxy\s+)?z\s+flip\s+(\d+)\s*fe", normalized):
        return f"ZF{match.group(1)}FE"
    if match := re.search(r"(?:galaxy\s+)?z\s+flip\s+(\d+)", normalized):
        return f"ZF{match.group(1)}"
    if match := re.search(r"(?:galaxy\s+)?z\s+fold\s+(\d+)", normalized):
        return f"ZD{match.group(1)}"
    if match := re.search(r"\b(g970|g973|g975|g977)\b", normalized):
        return {
            "G970": "S10E",
            "G973": "S10",
            "G975": "S10P",
            "G977": "S105G",
        }[match.group(1).upper()]
    if match := re.search(r"\b(n770|n970|n975)\b", normalized):
        return {"N770": "N10LT", "N970": "N10", "N975": "N10P"}[match.group(1).upper()]
    if match := re.search(r"\b(g780|g781)\b", normalized):
        return {"G780": "S20FE", "G781": "S20FE5"}[match.group(1).upper()]
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
        (r"\bredmi\s+pad\s+wi\s*fi\b", "RPADW"),
        (r"\bxiaomi\s+pad\s+wi\s*fi\b", "PADW"),
        (r"\bredmi\s+pad\s+se\s*8\s*7\b", "RPSE87"),
        (r"\bredmi\s+pad\s+se\s*8\.?7\b", "RPSE87"),
        (r"\bredmi\s+pad\s+se\b", "RPSE"),
        (r"\bredmi\s+pad\s+2\s+pro\b", "RPAD2P"),
        (r"\bredmi\s+pad\s+2\b", "RPAD2"),
        (r"\bpoco\s+pad\s+m(\d+)\b", lambda m: f"PPADM{m.group(1)}"),
        (r"\bpoco\s+pad\b", "PPAD"),
        (r"\bmix\s+flip\b", "MXFL"),
        (r"\bmi\s+mix\s*(\d+)(s)?\b", lambda m: f"MIX{m.group(1)}{'S' if m.group(2) else ''}"),
        (r"\bmi\s+note\s*(\d+)\s*lite\b", lambda m: f"MN{m.group(1)}LT"),
        (r"\bmi\s+note\s*(\d+)\s*pro\b", lambda m: f"MN{m.group(1)}P"),
        (r"\bmi\s+note\s*(\d+)\b", lambda m: f"MN{m.group(1)}"),
        (r"\bmi\s+note\b", "MNOTE"),
        (r"\bredmi\s+k(\d+)\s*gaming.*mercedes\b", lambda m: f"RK{m.group(1)}GM"),
        (r"\bredmi\s+k(\d+)\s*ultra\b", lambda m: f"RK{m.group(1)}U"),
        (r"\bredmi\s+k(\d+)\s*pro\b", lambda m: f"RK{m.group(1)}P"),
        (r"\bredmi\s+k(\d+)\b", lambda m: f"RK{m.group(1)}"),
        (r"\bblack\s+shark\b", "BSH"),
        (r"\bpocophone\s+f(\d+)\b", lambda m: f"PF{m.group(1)}"),
        (r"\bpoco\s+f(\d+)\s*ultra\b", lambda m: f"PF{m.group(1)}U"),
        (r"\bpoco\s+f(\d+)\s*gt\b", lambda m: f"PF{m.group(1)}GT"),
        (r"\bpoco\s+f(\d+)\s*pro\b", lambda m: f"PF{m.group(1)}P"),
        (r"\bpoco\s+f(\d+)\b", lambda m: f"PF{m.group(1)}"),
        (r"\bpoco\s+x(\d+)\s*pro\s*max\b", lambda m: f"PX{m.group(1)}M"),
        (r"\bpoco\s+x(\d+)\s*pro\b", lambda m: f"PX{m.group(1)}P"),
        (r"\bpoco\s+x(\d+)\s*gt\b", lambda m: f"PX{m.group(1)}GT"),
        (r"\bpoco\s+x(\d+)\s*nfc\b", lambda m: f"PX{m.group(1)}N"),
        (r"\bpoco\s+x(\d+)\b", lambda m: f"PX{m.group(1)}"),
        (r"\bpoco\s+m(\d+)\s*pro\s*5g\b", lambda m: f"PM{m.group(1)}P5"),
        (r"\bpoco\s+m(\d+)\s*pro\s*4g\b", lambda m: f"PM{m.group(1)}P4"),
        (r"\bpoco\s+m(\d+)\s*pro\b", lambda m: f"PM{m.group(1)}P"),
        (r"\bpoco\s+m(\d+)s\b", lambda m: f"PM{m.group(1)}S"),
        (r"\bpoco\s+m(\d+)\s*5g\b", lambda m: f"PM{m.group(1)}5"),
        (r"\bpoco\s+m(\d+)\b", lambda m: f"PM{m.group(1)}"),
        (r"\bpoco\s+c(\d+)\b", lambda m: f"PC{m.group(1)}"),
        (r"\bredmi\s+note\s*(\d+)\s*a\s*prime\b", lambda m: f"RN{m.group(1)}AP"),
        (r"\bredmi\s+note\s*(\d+)\s*a\b", lambda m: f"RN{m.group(1)}A"),
        (r"\bredmi\s+note\s*(\d+)\s*e\b", lambda m: f"RN{m.group(1)}E"),
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
        (r"\bredmi\s+note\s*(\d+)\s*t\s*5g\b", lambda m: f"RN{m.group(1)}T5"),
        (r"\bredmi\s+note\s*(\d+)\s*t\b", lambda m: f"RN{m.group(1)}T"),
        (r"\bredmi\s+note\s*(\d+)\b", lambda m: f"RN{m.group(1)}"),
        (r"\bredmi\s+a(\d+)(x)?\b", lambda m: f"RA{m.group(1)}{'X' if m.group(2) else ''}"),
        (r"\bredmi\s+4\s*a\b", "R4A"),
        (r"\bredmi\s+4\s*prime\b", "R4P"),
        (r"\bredmi\s+4\s*pro\b", "R4P"),
        (r"\bredmi\s+10\s*2022\b", "R1022"),
        (r"\bredmi\s+(\d+)a\b", lambda m: f"R{m.group(1)}A"),
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
        (r"\bmi\s+(\d+)\s*t\s*lite\s*5g\b", lambda m: f"M{m.group(1)}TL5"),
        (r"\bmi\s+(\d+)\s*t\s*lite\b", lambda m: f"M{m.group(1)}TL"),
        (r"\bmi\s+(\d+)\s*t\s*pro\b", lambda m: f"{m.group(1)}TP"),
        (r"\bmi\s+(\d+)\s*t\b", lambda m: f"{m.group(1)}T"),
        (r"\bmi\s+(\d+)\s*x\b", lambda m: f"{m.group(1)}X"),
        (r"\bmi\s+(\d+)\s*i\b", lambda m: f"{m.group(1)}I"),
        (r"\bmi\s+(\d+)\b", lambda m: f"{m.group(1)}"),
        (r"\bxiaomi\s+13\b.*\bxiaomi\s+14\b", "1314"),
        (r"\bxiaomi\s+(\d+)\b", lambda m: f"{m.group(1)}"),
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
        (r"\bptp\s+n49\b", "HM7P"),
        (r"\bptp\s+n29\b", "HM7"),
        (r"\bhonor\s+7c\b", "H7C"),
        (r"\bcdy\s+nx9a\b", "P40LT5"),
        (r"\bdnp\s+an00\b", "H400PCN"),
        (r"\bdnp\s+nx9\b", "H400P"),
        (r"\bdby\s+w09\b", "MP1121"),
        (r"\bdbr\s+w09\b", "MP1123"),
        (r"\bdbr\s+w19\b", "MP115PM"),
        (r"\btxz\s+w09\b", "MP11525"),
        (r"\bbrc\s+nx1\b", "HX9S"),
        (r"\bbrp\s+nx1\b", "HX9C"),
        (r"\b(?:aum|atu)\s+l(?:11|29|41)\b", "AUM"),
        (r"\b(?:lyo|cam)\s+l(?:03|21)\b", "LYO"),
        (r"\b(?:jat|mrd)\s+l", "JAT"),
        (r"\b(?:ksa|amn|kse)\s+l", "KSA"),
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
        (r"\bhonor\s+tablet\s+v(\d+)\s*pro\b", lambda m: f"HTV{m.group(1)}P"),
        (r"\bhonor\s+pad\s+x(\d+)([a-z]?)\b", lambda m: f"HPX{m.group(1)}{m.group(2).upper()}"),
        (r"\bhonor\s+pad\s+v(\d+)\b", lambda m: f"HPV{m.group(1)}"),
        (r"\bhonor\s+magicpad\s*(\d+)\b", lambda m: f"HMP{m.group(1)}"),
        (r"\bhonor\s+pad\s*(\d+)", lambda m: f"HPAD{m.group(1)}"),
        (r"\bhonor\s+magic\s*v(\d+)", lambda m: f"HMV{m.group(1)}"),
        (r"\bhonor\s+magic\s*(\d+)\s*pro\b", lambda m: f"HM{m.group(1)}P"),
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
        (r"\bp(\d+)\s*pocket\b", lambda m: f"P{m.group(1)}PK"),
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
        (r"\bp(\d+)\s*pro\s*(?:\+|plus\b)", lambda m: f"P{m.group(1)}PP"),
        (r"\bp(\d+)\s*plus\b", lambda m: f"P{m.group(1)}P"),
        (r"\bp(\d+)\s*pro\b", lambda m: f"P{m.group(1)}P"),
        (r"\bp(\d+)\b", lambda m: f"P{m.group(1)}"),
        (r"\bnova\s+(\d+)\s*plus\b", lambda m: f"N{m.group(1)}P"),
        (r"\bnova\s+(\d+)\s*pro\b", lambda m: f"N{m.group(1)}P"),
        (r"\bnova\s+(\d+)\s*lite\b", lambda m: f"N{m.group(1)}LT"),
        (r"\bnova\s+(\d+)\s*se\b", lambda m: f"N{m.group(1)}SE"),
        (r"\bnova\s+(\d+)t\b", lambda m: f"N{m.group(1)}T"),
        (r"\bnova\s+(\d+)i\b", lambda m: f"N{m.group(1)}I"),
        (r"\bnova\s+(\d+)s\b", lambda m: f"N{m.group(1)}S"),
        (r"\bnova\s+(\d+)\b", lambda m: f"N{m.group(1)}"),
        (r"\bnova\s+y(\d+)([a-z]?)\b", lambda m: f"NY{m.group(1)}{m.group(2).upper()}"),
        (r"\bnova\s+lite\s*2017\b", "NL17"),
        (r"\bnova\s+lite\b", "NLT"),
        (r"\by(\d+)\s*prime\b", lambda m: f"Y{m.group(1)}P"),
        (r"\by(\d+)\s*pro\b", lambda m: f"Y{m.group(1)}P"),
        (r"\by(\d+)p\b", lambda m: f"Y{m.group(1)}P"),
        (r"\by(\d+)s\b", lambda m: f"Y{m.group(1)}S"),
        (r"\by(\d+)a\b", lambda m: f"Y{m.group(1)}A"),
        (r"\by(\d+)i\b", lambda m: f"Y{m.group(1)}I"),
        (r"\by(\d+)\b", lambda m: f"Y{m.group(1)}"),
        (r"\bhonor\s+view\s*(\d+)\b", lambda m: f"HV{m.group(1)}"),
        (r"\bhonor\s+play\s*(\d+)\b", lambda m: f"HPLAY{m.group(1)}"),
        (r"\bhonor\s+play\b", "HPLAY"),
        (r"\bhonor\s+magicwatch\s*(\d+)\b", lambda m: f"HMW{m.group(1)}"),
        (r"\bhonor\s+v(\d+)\s*play\b", lambda m: f"HV{m.group(1)}P"),
        (r"\bhonor\s+(\d+)\s*lite\b", lambda m: f"H{m.group(1)}LT"),
        (r"\bhonor\s+(\d+)\s*premium\b", lambda m: f"H{m.group(1)}P"),
        (r"\bhonor\s+(\d+)\s*smart\b", lambda m: f"H{m.group(1)}S"),
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
                (r"\boneplus\s+(\d+)\s*t\s*pro\b", lambda m: f"{m.group(1)}TP"),
                (r"\boneplus\s+(\d+)\s*pro\b", lambda m: f"{m.group(1)}P"),
                (r"\boneplus\s+(\d+)\s*r\s*(150|80)w\b", lambda m: f"{m.group(1)}R{m.group(2)}"),
                (r"\boneplus\s+pad\s+go\s*lte\b", "PADGL"),
                (r"\boneplus\s+pad\s+go\b", "PADG"),
                (r"\boneplus\s+pad\s*(\d+)\b", lambda m: f"PAD{m.group(1)}"),
                (r"\boneplus\s+pad\b", "PAD"),
                (r"\boneplus\s+nord\s+ce\s*(\d+)\s*lite\s*5g\b", lambda m: f"NCE{m.group(1)}L5"),
                (r"\boneplus\s+nord\s+ce\s*(\d+)\s*5g\b", lambda m: f"NCE{m.group(1)}5"),
                (r"\boneplus\s+nord\s+ce\s*(\d+)\b", lambda m: f"NCE{m.group(1)}"),
                (r"\boneplus\s+nord\s+ce\s*5g\b", "NCE5"),
                (r"\boneplus\s+nord\s*n(\d+)\s*5g\b", lambda m: f"NN{m.group(1)}5"),
                (r"\boneplus\s+nord\s*(\d+)\s*t\b", lambda m: f"N{m.group(1)}T"),
                (r"\boneplus\s+nord\s*(\d+)\b", lambda m: f"N{m.group(1)}"),
                (r"\boneplus\s+nord\b", "NORD"),
                (r"\boneplus\s+x\b", "OPX"),
                (r"\boneplus\s+one\b", "OPO"),
                (r"\boneplus\s+ace\s*(\d+)v\b", lambda m: f"A{m.group(1)}V"),
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
                (r"\bgt\s+explorer\s+master\b", "GTEM"),
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
                (r"\bx(\d+)\s*pro\s*5g\b", lambda m: f"X{m.group(1)}P5"),
                (r"\bx(\d+)\s*pro\b", lambda m: f"X{m.group(1)}P"),
                (r"\bx(\d+)\s*5g\b", lambda m: f"X{m.group(1)}5"),
                (r"\bx(\d+)\b", lambda m: f"X{m.group(1)}"),
                (r"\bp(\d+)\s*ultra\b", lambda m: f"P{m.group(1)}U"),
                (r"\bp(\d+)\s*pro\b", lambda m: f"P{m.group(1)}P"),
                (r"\bp(\d+)\b", lambda m: f"P{m.group(1)}"),
                (r"\brealme\s+c35\b", "C35"),
                (r"\bnarzo\s*(\d+)\s*a\b", lambda m: f"NZ{m.group(1)}A"),
                (r"\bnarzo\s*(\d+)\s*i\s*prime\b", lambda m: f"NZ{m.group(1)}IP"),
                (r"\bnarzo\s*(\d+)\s*pro\b", lambda m: f"NZ{m.group(1)}P"),
                (r"\bnarzo\s*(\d+)\s*5g\b", lambda m: f"NZ{m.group(1)}5"),
                (r"\bnarzo\s*(\d+)\s*4g\b", lambda m: f"NZ{m.group(1)}4"),
                (r"\bnarzo\s*(\d+)\b", lambda m: f"NZ{m.group(1)}"),
                (r"\bnote\s*(\d+)\b", lambda m: f"N{m.group(1)}"),
                (r"\brealme\s+c(\d+)i\b", lambda m: f"C{m.group(1)}I"),
                (r"\brealme\s+(\d+)\s*pro\s*(?:\+|plus)\s*5g\b", lambda m: f"{m.group(1)}PP5"),
                (r"\brealme\s+(\d+)\s*pro\s*(?:\+|plus)(?:\s|$)", lambda m: f"{m.group(1)}PP"),
                (r"\brealme\s+(\d+)\s*pro\s*5g\b", lambda m: f"{m.group(1)}P5"),
                (r"\brealme\s+(\d+)\s*pro\b", lambda m: f"{m.group(1)}P"),
                (r"\brealme\s+(\d+)\s*5g\s*india\b", lambda m: f"{m.group(1)}5I"),
                (r"\brealme\s+(\d+)\s*5g\b", lambda m: f"{m.group(1)}5"),
                (r"\brealme\s+(\d+)\s*4g\b", lambda m: f"{m.group(1)}4"),
                (r"\brealme\s+(\d+)t\b", lambda m: f"{m.group(1)}T"),
                (r"\brealme\s+(\d+)\b", lambda m: f"{m.group(1)}"),
                (r"\bc(\d+)\s*i\b", lambda m: f"C{m.group(1)}I"),
                (r"\bc(\d+)\s*s\b", lambda m: f"C{m.group(1)}S"),
                (r"\bc(\d+)\s*y\b", lambda m: f"C{m.group(1)}Y"),
                (r"\bc(\d+)\s*4g\b", lambda m: f"C{m.group(1)}4"),
                (r"\bc(\d+)\b", lambda m: f"C{m.group(1)}"),
                (r"\b(\d+)\s*pro\s*(?:\+|plus)\s*5g\b", lambda m: f"{m.group(1)}PP5"),
                (r"\b(\d+)\s*pro\s*(?:\+|plus)(?:\s|$)", lambda m: f"{m.group(1)}PP"),
                (r"\b(\d+)\s*pro\s*5g\b", lambda m: f"{m.group(1)}P5"),
                (r"\b(\d+)\s*pro\b", lambda m: f"{m.group(1)}P"),
                (r"\b(\d+)\s*\+\s*5g\b", lambda m: f"{m.group(1)}P5"),
                (r"\b(\d+)\s*5g\s*india\b", lambda m: f"{m.group(1)}5I"),
                (r"\b(\d+)\s*5g\b", lambda m: f"{m.group(1)}5"),
                (r"\b(\d+)\s*4g\b", lambda m: f"{m.group(1)}4"),
                (r"\b(\d+)t\b", lambda m: f"{m.group(1)}T"),
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
        (r"\b(\d+)\.(\d+)\s*plus\b", lambda m: f"{m.group(1)}{m.group(2)}P"),
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
        (r"\bm3x\b", "M3X"),
        (r"\bmx6\b", "MX6"),
        (r"\bmeizu\s*(\d+)\b", lambda m: f"M{m.group(1)}"),
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
            (r"\bx\s*(\d+)\s*pro\s*(?:plus|\+)", lambda m: f"X{m.group(1)}PP"),
            (r"\bx\s*(\d+)\s*pro\b", lambda m: f"X{m.group(1)}P"),
            (r"\bx\s*(\d+)\b", lambda m: f"X{m.group(1)}"),
            (r"\bv(\d+)\s*fe\b", lambda m: f"V{m.group(1)}FE"),
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
            (r"\bphantom\s+v\s+fold\s*2\b", "PVF2"),
            (r"\bphantom\s+v\s+fold\b", "PVF"),
            (r"\bmegapad\s+pro\b", "MPPRO"),
            (r"\bmegapad\s+11\b", "MP11"),
            (r"\bcamon\s*(\d+)\s*premier\s*5g\b", lambda m: f"C{m.group(1)}PR5"),
            (r"\bcamon\s*(\d+)\s*premier\b", lambda m: f"C{m.group(1)}PR"),
            (r"\bcamon\s*(\d+)s\s*pro\b", lambda m: f"C{m.group(1)}SP"),
            (r"\bcamon\s*(\d+)s\b", lambda m: f"C{m.group(1)}S"),
            (r"\bcamon\s*(\d+)\s*ultra\b", lambda m: f"C{m.group(1)}U"),
            (r"\bcamon\s*(\d+)\s*pro\s*5g\b", lambda m: f"C{m.group(1)}P5"),
            (r"\bcamon\s*(\d+)\s*pro\b", lambda m: f"C{m.group(1)}P"),
            (r"\bcamon\s*(\d+)\s*neo\b", lambda m: f"C{m.group(1)}N"),
            (r"\bcamon\s*(\d+)\s*5g\b", lambda m: f"C{m.group(1)}5"),
            (r"\bcamon\s*(\d+)\s*4g\b", lambda m: f"C{m.group(1)}4"),
            (r"\bcamon\s*(\d+)\b", lambda m: f"C{m.group(1)}"),
            (r"\bpova\s*(\d+)\s*pro\s*5g\b", lambda m: f"POVA{m.group(1)}P5"),
            (r"\bspark\s*(\d+)c\b", lambda m: f"S{m.group(1)}C"),
            (r"\bspark\s+go\s*(\d+)\b", lambda m: f"SG{m.group(1)}"),
            (r"\b([a-z]{2}\d[a-z0-9]*)\b", lambda m: m.group(1).upper()),
        ]
    elif brand == "itel":
        patterns = [
            (r"\bvision\s*3\s*plus\b", "V3P"),
            (r"\bvision\s*3\b", "V3"),
            (r"\ba60s\b", "A60S"),
            (r"\ba(\d+)([a-z]?)\b", lambda m: f"A{m.group(1)}{m.group(2).upper()}"),
            (r"\b([a-z]\d{3,4}[a-z]*)\b", lambda m: m.group(1).upper()),
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
            (r"\bnote\s*(\d+)\s*turbo\b", lambda m: f"N{m.group(1)}T"),
            (r"\bnote\s*(\d+)\s*vip\b", lambda m: f"N{m.group(1)}V"),
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
        (r"\bblade\s+v30\s+vita\b", "BV30V"),
        (r"\bblade\s+v30\b", "BV30"),
        (r"\bblade\s+v6\s*/\s*blade\s+x7\s*/\s*blade\s+z7\b", "BV6"),
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
        (r"\bf52\s*/\s*f90\s*/\s*k50\s*/\s*k51\s*/\s*k60i\s*/\s*k60ij\s*/\s*k61\b", "F52K72"),
        (
            r"\bk52\s*/\s*k53\s*/\s*k54\s*/\s*n50\s*/\s*n51\s*/\s*n52\s*/\s*n53\s*/\s*n60\b",
            "K52N60",
        ),
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
        (r"\bzenfone\s+go\s+zb452kg\b", "ZFGO45"),
        (r"\bzenfone\s+go\s+zb500kl\b", "ZFGO50"),
        (r"\bzenfone\s+go\s+zb552kl\b", "ZFGO55"),
        (r"\bzenfone\s+live\s+zb501kl\b", "ZFLIVE"),
        (r"\bzenfone\s+max\s+m2\b", "ZFMM2"),
        (r"\bzenfone\s+max\s+m1\b", "ZFMM1"),
        (r"\bzenfone\s+max\s+plus\s+m1\b", "ZFMAXP1"),
        (r"\bzenfone\s+max\s+pro\s+m1\b", "ZFMPM1"),
        (r"\bzenfone\s+max\s+pro\s+m2\b", "ZFMPM2"),
        (r"\bzenfone\s+selfie\s+zd551kl\b", "ZFSELF"),
        (r"\brog\s+xbox\s+ally\s+x\b", "XALLYX"),
        (r"\brog\s+xbox\s+ally\b", "XALLY"),
        (r"\brog\s+ally\s+x\s*2024\b", "ALLYX24"),
        (r"\brog\s+ally\s+x\b", "ALLYX"),
        (r"\brog\s+ally\s*2023\b", "ALLY23"),
        (r"\brog\s+ally\b.*\brc71l\b", "ALLY"),
        (r"\brog\s+ally\b", "ALLY"),
        (r"\bfonepad\s*7\b.*\b(fe170cg|memo\s+pad\s*7)\b", "FP7"),
        (r"\brog\s+phone\s*5s\b", "ROG5S"),
        (r"\brog\s+phone\s*5\b", "ROG5"),
        (r"\beee\s+pc\s*1001\b", "E1001"),
        (r"\bzenpad\s+s\s*8\.?0\b", "ZPS80"),
        (r"\bzenfone\s+3\s+max\b.*\bzc553kl\b", "ZF3M"),
        (r"\bzenfone\s+3\s+deluxe\b.*\bzs550kl\b", "ZF3D"),
        (r"\bzenfone\s+3\b.*\bze520kl\b", "ZF3"),
        (r"\brog\s+phone\s*(\d+)\s*fe\b", lambda m: f"ROG{m.group(1)}FE"),
        (r"\brog\s+phone\s*(\d+)\s*pro\b", lambda m: f"ROG{m.group(1)}P"),
        (r"\brog\s+phone\s*(\d+)\b", lambda m: f"ROG{m.group(1)}"),
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
        (r"\bc1905\s+xperia\s+m\s*/\s*c2005\s+xperia\s+m\s+dual\b", "XM"),
        (r"\bc6603\s*/\s*lt36i\s+xperia\s+z\b", "XZ"),
        (r"\bvpc-sa(?:\s*,)?\s*vpc-sb(?:\s*,)?\s*vpc-se(?:\s*,)?\s*sv-s\b", "VPCS"),
        (r"\bxperia\s+tablet\s+z3\s+compact\s*8\.?0\b", "XTZ3C8"),
        (r"\bxperia\s+tablet\s+z\s*10\.?1\b", "XTZ101"),
        (r"\bxperia\s+z\s+ultra\b", "ZU"),
        (r"\bxperia\s+xa2\b", "XA2"),
        (r"\bxperia\s+l2\b", "L2"),
        (r"\bxperia\s+1\s+vii\b", "X1VII"),
        (r"\bxperia\s+1\s+iii\b", "X1III"),
        (r"\bxperia\s+5\s+iii\b", "X5III"),
        (r"\bxperia\s+1\s+iv\b", "X1IV"),
        (r"\bxperia\s+5\s+iv\b", "X5IV"),
        (r"\bxperia\s+10\s+iv\b", "X10IV"),
        (r"\bxperia\s+5\s+v\b", "X5V"),
        (r"\bxperia\s+10\s+v\b", "X10V"),
        (r"\bxperia\s+1\s+ii\b", "X1II"),
        (r"\bxperia\s+10\s+plus\b", "X10P"),
        (r"\bxperia\s+z1\s+compact\b", "Z1C"),
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
        (r"\bxperia\s+z5\s+compact\b", "Z5C"),
        (r"\bxperia\s+z5\b", "Z5"),
        (r"\bxperia\s+l1\b", "L1"),
        (r"\bxperia\s+m4\s+aqua\b", "M4A"),
        (r"\bps4\s+slim\s*/\s*ps4\s+pro\b", "PS4SP"),
        (r"\bps5\s+slim\s*/\s*pro\b", "PS5SP"),
        (r"\bps5\s+new\s+v2\b", "PS5V2"),
        (r"\bps4\b", "PS4"),
        (r"\bps5\b", "PS5"),
        (r"\b([defgl]\d{4}[a-z]?)\b", lambda m: m.group(1).upper()),
    ]
    for pattern, replacement in patterns:
        if match := re.search(pattern, normalized):
            if callable(replacement):
                return replacement(match)
            return replacement
    return None


def _game_device_code_from_name(name: str | None) -> str | None:
    normalized = _normalize_accessory_name(name)
    if not normalized:
        return None

    patterns: list[tuple[str, str | callable]] = [
        (r"\basus\s+rog\s+xbox\s+ally\s+x\b", "ASU-XALLYX"),
        (r"\basus\s+rog\s+xbox\s+ally\b", "ASU-XALLY"),
        (r"\basus\s+rog\s+ally\s+x\s*2024\b", "ASU-ALLYX24"),
        (r"\basus\s+rog\s+ally\s+x\b", "ASU-ALLYX"),
        (r"\basus\s+rog\s+ally\b", "ASU-ALLY"),
        (r"\bmeta\s+(?:oculus\s+)?quest\s+pro\b", "META-QPRO"),
        (r"\b(?:meta\s+)?(?:oculus\s+)?quest\s+3\s*/\s*3s\b", "META-Q3S"),
        (r"\b(?:meta\s+)?(?:oculus\s+)?quest\s+3s\b", "META-Q3S"),
        (r"\b(?:meta\s+)?(?:oculus\s+)?quest\s+3\b", "META-Q3"),
        (r"\b(?:meta\s+)?(?:oculus\s+)?quest\s+2\b", "META-Q2"),
        (r"\boculus\s+quest\s+2\b", "META-Q2"),
        (r"\bsteam\s+deck\s+oled\b", "STM-DECKO"),
        (r"\bsteam\s+deck\b", "STM-DECK"),
        (r"\bnintendo\s+sound\s+clock\s+alarmo\b", "NIN-ALRMO"),
        (r"\bnintendo\s+new\s+2ds\s+xl\b", "NIN-2DSXL"),
        (r"\bnintendo\s+new\s+3ds\s+xl\b", "NIN-3DSXL"),
        (r"\bnintendo\s+new\s+3ds\b", "NIN-3DS"),
        (r"\bnintendo\s+dsi\s+xl\b", "NIN-DSIXL"),
        (r"\bnintendo\s+(?:dual\s+screen\s+lite|ds\s+lite)\b", "NIN-DSL"),
        (r"\bnintendo\s+3ds\s+(?:xl|ll)\b", "NIN-3DSXL"),
        (r"\bnintendo\s+3ds\b", "NIN-3DS"),
        (r"\bnintendo\s+switch\s+2\b", "NIN-SW2"),
        (r"\bnintendo\s+switch\s+lite\s*/\s*switch\s+oled\b", "NIN-SWLO"),
        (r"\bnintendo\s+switch\s*/\s*oled\s*/\s*lite\b", "NIN-SW"),
        (r"\bnintendo\s+switch\s+(?:oled|old)\b", "NIN-SWO"),
        (r"\bnintendo\s+switch\s+lite\b", "NIN-SWL"),
        (r"\bnintendo\s+switch\s+ns\b", "NIN-SW"),
        (r"\bnintendo\s+switch\b", "NIN-SW"),
        (r"\bnintendo\s+joy-con\b", "NIN-SWJC"),
        (r"\bplaystation\s*5\b", "SON-PS5"),
        (r"\bplaystation\s*4\b", "SON-PS4"),
        (r"\bplaystation\s*3\b", "SON-PS3"),
        (r"\bsony\s+psp\b", "SON-PSP"),
        (r"\bpsp\b", "SON-PSP"),
        (r"\bps5\b", "SON-PS5"),
        (r"\bps4\b", "SON-PS4"),
        (r"\bps3\b", "SON-PS3"),
        (r"\bxbox\s+one\s+elite\s*2\b", "XBX-ONEE2"),
        (r"\bxbox\s+one\b", "XBX-ONE"),
        (r"\bxbox\s+series\s+x\b", "XBX-SX"),
        (r"\bxbox\s+series\s+s\b", "XBX-SS"),
        (r"\bxbox\s+360\s+slim\b", "XBX-360S"),
        (r"\bxbox\s+360\b", "XBX-360"),
        (r"\bxbox\b", "XBX"),
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
    elif brand == "doogee":
        patterns = [
            (r"\bblade\s+gt\b", "BLGT"),
            (r"\bblade\b", "BLADE"),
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
        (r"\blegion\s+y700\s*5\b", "Y700G5"),
        (r"\blegion\s+y700\s*4\b", "Y700G4"),
        (r"\blegion\s+go\s*2\b.*\by700\s+gen\.?\s*5\b", "LGO2Y700G5"),
        (r"\blegion\s+y700\b.*\b(tb323fc|tb323fu)\b", "Y700G5"),
        (r"\blegion\s+y700\b.*\b(tb322fc|tb322fu)\b", "Y700G4"),
        (r"\blegion\s+y700\s*gen\s*3(?:\s*\(?(2025)\)?)?\b", "Y700G3"),
        (r"\btab\s+m10\s+plus\s+gen\s*3\b", "TBM10P3"),
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
    lower_name = (product.name or "").lower()
    game_device = _game_device_code_from_name(product.name)
    if game_device:
        return game_device
    if category == "CBL":
        explicit = _find_code(product.cable_connector_input, CONNECTOR_CODES) or _slug_token(
            product.cable_connector_input, max_len=8
        )
        if explicit:
            return explicit
        left, right = _accessory_connector_pair_from_name(product.name)
        return left or right
    if category == "CHR":
        if product.charger_power_w:
            return f"{product.charger_power_w}W"
        return (
            _power_w_from_name(product.name)
            or _capacity_mah_from_name(product.name)
            or _accessory_model_code_from_name(product.name)
        )
    if category == "TST":
        return "UNV"

    if category == "BAT" and ("универсальн" in lower_name or "батарейк" in lower_name):
        return "UNV"
    if category == "IC" and "микросхем" in lower_name:
        brand_families = (
            ("apple",),
            ("asus",),
            ("huawei", "honor"),
            ("lenovo",),
            ("samsung",),
            ("sony",),
            ("xiaomi",),
        )
        if (
            sum(1 for family in brand_families if any(marker in lower_name for marker in family))
            > 1
        ):
            return "UNV"

    apple_name = (product.name or "").lower()
    if re.search(r"\bapple\b|\biphone\b|\bipad\b", apple_name):
        apple_name_source = product.name
        if "в дизайне" in apple_name:
            apple_name_source = re.split(
                r"\bв\s+дизайне\b",
                product.name or "",
                maxsplit=1,
                flags=re.IGNORECASE,
            )[0]
        apple_name_code = _device_code_from_model("apple", apple_name_source, None)
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
        huawei_name_source = product.name
        if category == "DSP" and "matepad 11.5" in huawei_name and "txz" in huawei_name:
            huawei_name_source = re.sub(
                r"\([^()]*txz[^()]*\)", "", product.name or "", flags=re.IGNORECASE
            )
        if category == "GLS" and "matepad 11.5s" in huawei_name and "papermatte" in huawei_name:
            return "HWE-MP115SPM"
        huawei_name_code = _device_code_from_model(preferred_brand, huawei_name_source, None)
        if huawei_name_code:
            return huawei_name_code

    misc_name = (product.name or "").lower()
    if match := re.search(r"\b(?:nothing\s+)?cmf\s+phone\s*(\d+)([a-z]?)\b", misc_name):
        suffix = f"CMF{match.group(1)}{match.group(2).upper()}"
        return _slug_token(f"NOT-{suffix}", max_len=24)
    if match := re.search(r"\bnothing\s+phone\s*(\d+)([a-z]?)\s*lite\b", misc_name):
        suffix = f"PH{match.group(1)}{match.group(2).upper()}L"
        return _slug_token(f"NOT-{suffix}", max_len=24)
    if match := re.search(r"\bnothing\s+phone\s*(\d+)([a-z]?)\s*pro\b", misc_name):
        suffix = f"PH{match.group(1)}P"
        return _slug_token(f"NOT-{suffix}", max_len=24)
    if match := re.search(r"\bnothing\s+phone\s*(\d+)([a-z]?)\b", misc_name):
        suffix = f"PH{match.group(1)}{match.group(2).upper()}"
        return _slug_token(f"NOT-{suffix}", max_len=24)
    if match := re.search(r"\bulefone\s+power\s+armor\s*(\d+)\b", misc_name):
        return _slug_token(f"ULE-PA{match.group(1)}", max_len=24)

    for preferred_brand, markers in (
        ("asus", ("asus", "zenfone", "rog phone")),
        ("sony", ("sony", "xperia")),
        ("doogee", ("doogee",)),
        ("jbl", ("jbl",)),
        ("motorola", ("motorola", "moto ")),
        ("lg", ("lg ", "lg-", "class")),
        ("google", ("google", "pixel")),
        ("vivo", ("vivo", "iqoo")),
        ("fly", ("fly ", "fly iq")),
        ("tecno", ("tecno", "phantom", "pova", "spark")),
        ("infinix", ("infinix",)),
        ("itel", ("itel",)),
    ):
        if any(marker in misc_name for marker in markers):
            misc_code = _device_code_from_model(preferred_brand, product.name, None)
            if misc_code:
                return misc_code

    oppo_name = (product.name or "").lower()
    for preferred_brand, markers in (
        ("oneplus", ("oneplus",)),
        ("oppo", ("oppo", "reno", "find ", "rx17", "pad air", "pad neo")),
        ("realme", ("realme", "narzo", "gt neo", "gt explorer")),
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
    modifiers = _display_modifier_codes(product)
    return "-".join(_compact_parts([display_type, color, quality, *modifiers])) or None


def _display_modifier_codes(product: Product) -> list[str]:
    name = (product.name or "").lower()
    modifiers: list[str] = []
    is_touchscreen_only = re.match(r"\s*(?:тачскрин|сенсор)\b", name) and "дисплей" not in name
    if "в сборе" in name and "матрица" in name:
        modifiers.append("ASM")
    if "oca" in name:
        modifiers.append("OCA")
    if ("коннектор" in name or "connector" in name) and not (
        "широкий коннектор" in name or "узкий коннектор" in name
    ):
        modifiers.append("CON")
    if "кнопка home" in name:
        modifiers.append("HOME")
    if "без микросхемы" in name:
        modifiers.append("NOIC")
    if "под пайку" in name:
        modifiers.append("SOLD")
    if "musttby" in name:
        modifiers.append("MST")
    if "feaglet" in name:
        modifiers.append("FEA")
    if "матов" in name:
        modifiers.append("MAT")
    if "ultra soft oled" in name or "ultrasoft oled" in name:
        modifiers.append("USO")
    if "regular" in name:
        modifiers.append("REG")
    if "площадка под ic" in name or "площадка под микросхем" in name:
        modifiers.append("ICP")
    if "с верификацией" in name:
        modifiers.append("VER")
    if "матрица" in name and "macbook" in name:
        year_code = _year_span_code(name)
        if year_code:
            modifiers.append(year_code)
    if is_touchscreen_only and not modifiers:
        modifiers.append("TCH")
    return modifiers


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
    if "матрица" in joined:
        return "MAT"
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

    if "aasp" in joined:
        return "ASP"
    if re.search(r"\boem\b", joined):
        return "OEM"
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
    name = (product.name or "").lower()
    if "батарейк" in name:
        size: str | None = None
        if match := re.search(r"\b(cr20\d{2}|23a|ag13|lr44h|357a)\b", name):
            size = match.group(1).upper()
        elif re.search(r"\b(?:lr6\s*)?aa\b", name):
            size = "AA"
        elif re.search(r"\b(?:lr03\s*)?aaa\b", name):
            size = "AAA"
        qty_match = re.search(r"(\d{1,2})\s*шт", name)
        qty = qty_match.group(1) if qty_match else None
        brand_match = re.search(r"батарейки\s+([a-zа-я0-9]+)", name)
        brand = _slug_token(brand_match.group(1), max_len=8) if brand_match else None
        if brand == size:
            brand = None
        return "-".join(_compact_parts([size, qty, brand])) or None

    if "аккумулятор универсальный" in name:
        capacity_match = re.search(r"\b(\d{2,5})\s*(?:mah|м?ач|м?ah)\b", name)
        cell_code: str | None = None
        for token in reversed(_parenthesized_tokens(product.name)):
            candidate = _slug_token(token, max_len=12)
            if candidate and re.fullmatch(r"\d{5,7}P?", candidate):
                cell_code = candidate
                break
        return (
            "-".join(
                _compact_parts([capacity_match.group(1) if capacity_match else None, cell_code])
            )
            or None
        )

    if product.battery_capacity_mah:
        prefix = "HC" if product.battery_is_high_capacity else ""
        return f"{prefix}{product.battery_capacity_mah}"

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
    explicit_right = _find_code(product.cable_connector_output, CONNECTOR_CODES) or _slug_token(
        product.cable_connector_output, max_len=8
    )
    _, name_right = _accessory_connector_pair_from_name(product.name)
    right = explicit_right or name_right
    length = _normalize_length(product.cable_length) or _length_from_name(product.name)
    color = _color_code_from_product_or_name(product)
    model = _accessory_model_code_from_name(product.name)
    power = _power_w_from_name(product.name)
    flags = [*_cable_feature_flags_from_name(product.name), *_accessory_quality_flags(product)]
    if "удлинитель" in (product.name or "").lower():
        flags.insert(0, "EXT")
    if not right and not (model or length):
        return None
    parts = _compact_parts([right, length, color, model, power, *flags])
    return "-".join(dict.fromkeys(parts))


def _charger_key(product: Product) -> str | None:
    technology = _find_code(product.charger_technology, CHARGER_TECH_CODES) or _slug_token(
        product.charger_technology, max_len=8
    )
    name = _normalize_accessory_name(product.name)
    if not technology:
        for marker, code in CHARGER_TECH_CODES.items():
            if marker in name:
                technology = code
                break
    plug = _find_code(product.charger_plug_type, PLUG_CODES) or _slug_token(
        product.charger_plug_type, max_len=8
    )
    port = _connector_code_from_text(product.name)
    model = _accessory_model_code_from_name(product.name)
    series = next((code for marker, code in CHARGER_SERIES_CODES.items() if marker in name), None)
    device = (
        f"{product.charger_power_w}W"
        if product.charger_power_w
        else _power_w_from_name(product.name)
        or _capacity_mah_from_name(product.name)
        or _accessory_model_code_from_name(product.name)
    )
    if model and device == model:
        model = None
    color = _color_code_from_product_or_name(product)
    port_flags = _accessory_port_flags_from_name(product.name)
    if port in {"USBA", "USBC"} and any(
        flag.endswith(("USB", "USBA", "USBC")) for flag in port_flags
    ):
        port = None
    if "BCBL" in port_flags:
        port = None
    flags = [
        *port_flags,
        *_cable_feature_flags_from_name(product.name),
        _included_cable_flag_from_name(product.name),
        *_accessory_quality_flags(product),
    ]
    parts = _compact_parts([technology, plug, port, model, series, color, *flags])
    if not parts and device:
        parts = ["STD"]
    return "-".join(dict.fromkeys(parts)) or None


def _color_code_from_product_or_name(product: Product) -> str | None:
    name = (product.name or "").lower()
    if re.search(r"\(\s*титановый\s*\)", name):
        return "TIT"
    special_name_colors = (
        ("сине-черный", "BLBK"),
        ("сине-чёрный", "BLBK"),
        ("черно-оранжевый", "BKOR"),
        ("чёрно-оранжевый", "BKOR"),
        ("черно-синий", "BKBL"),
        ("чёрно-синий", "BKBL"),
        ("темно-синий", "DBL"),
        ("тёмно-синий", "DBL"),
        ("зеленый / фиолетовый", "GRPR"),
        ("зелёный / фиолетовый", "GRPR"),
        ("фиолетовый / зеленый", "GRPR"),
        ("фиолетовый / зелёный", "GRPR"),
        ("левая красный / правая синий", "RDBL"),
        ("левая красная / правая синяя", "RDBL"),
        ("синий-сумеречный", "TBLU"),
        ("cиний-сумеречный", "TBLU"),
        ("светло-синий", "LBLU"),
        ("cветло-синий", "LBLU"),
        ("темно-красный", "DRED"),
        ("тёмно-красный", "DRED"),
        ("красно-черный", "RDBK"),
        ("красно-чёрный", "RDBK"),
    )
    for marker, code in special_name_colors:
        if marker in name:
            return code
    for token in _parenthesized_tokens(product.name):
        token_lower = token.lower()
        for raw, code in sorted(COLOR_CODES.items(), key=lambda item: len(item[0]), reverse=True):
            pattern = rf"(?<![0-9a-zа-я]){re.escape(raw)}(?![0-9a-zа-я])"
            if re.search(pattern, token_lower):
                return code
    explicit = _find_code(product.color, COLOR_CODES) or _slug_token(product.color, max_len=8)
    if explicit:
        return explicit
    for raw, code in COLOR_CODES.items():
        pattern = rf"(?<![0-9a-zа-я]){re.escape(raw)}(?![0-9a-zа-я])"
        if re.search(pattern, name):
            return code
    return None


def _name_grade_code(product: Product) -> str | None:
    name = (product.name or "").lower()
    if "orig100" in name or "ориг100" in name:
        return "OR1"
    if re.search(r"\borig\b", name) or "ориг" in name:
        return "OR"
    if "premium" in name:
        return "PR"
    if re.search(r"\blow\b", name):
        return "LOW"
    if re.search(r"\(\s*sp\s*\)", name):
        return "SP"
    if "снятый" in name or "снятая" in name:
        return "PUL"
    return None


def _camera_key(product: Product) -> str | None:
    position = _slug_token(product.camera_position, max_len=8)
    name = (product.name or "").lower()
    if not position:
        if any(token in name for token in ("передняя", "фронтальная", "front")):
            position = "FCAM"
        elif any(token in name for token in ("задняя", "основная", "back", "rear")):
            position = "RCAM"
    mp = _normalize_mp(product.camera_megapixels)
    if not mp:
        if match := re.search(r"(\d{1,3})\s*(?:mp|мп)", name):
            mp = f"{match.group(1)}MP"
    number = None
    if match := re.search(r"\((\d)\)", name):
        number = f"N{match.group(1)}"
    return "-".join(_compact_parts([position, mp, number, _name_grade_code(product)])) or None


def _glass_key(product: Product) -> str | None:
    color = _color_code_from_product_or_name(product)
    glass_type = _slug_token(product.glass_type, max_len=12)
    name = (product.name or "").lower()
    if not glass_type:
        if any(
            token in name for token in ("стекло задней камеры", "стекло камеры", "на заднюю камеру")
        ):
            glass_type = "CAMG"
        elif "защит" in name:
            glass_type = "PROT"
        elif "стекло модуля" in name:
            glass_type = "MODG"
        elif "линз" in name:
            glass_type = "LENS"
        elif "стекл" in name:
            glass_type = "GLS"
    glass_form = _slug_token(product.glass_form, max_len=12)
    modifiers: list[str] = []
    if match := re.search(r"\bgl[-\s]?(\d{1,3})\b", name):
        modifiers.append(f"GL{match.group(1)}")
    if match := re.search(r"\bes[-\s]?(\d{1,3})\b", name):
        modifiers.append(f"ES{match.group(1)}")
    if "антиблик" in name:
        modifiers.append("AG")
    if "антишпион" in name:
        modifiers.append("PRV")
        if match := re.search(r"(\d{2,3})\s*(?:°|градус)", name):
            modifiers.append(f"D{match.group(1)}")
    if "матов" in name:
        modifiers.append("MAT")
    if "rock glass" in name or "ультрапроч" in name:
        modifiers.append("ROCK")
    if "sapphire glass" in name or "сапфиров" in name:
        modifiers.append("SAPH")
    if "edge armor" in name or "металлическая рамка" in name:
        modifiers.append("EDGE")
    if "в рамке" in name:
        modifiers.append("FR")
    elif "без рамк" in name:
        modifiers.append("NFR")
    if "polaris" in name:
        modifiers.append("POL")
    if "dragon armor" in name or "царапин" in name:
        modifiers.append("DRAG")
    if "тех. пак" in name or "тех пак" in name:
        modifiers.append("TPK")
    if "mossily" in name:
        modifiers.append("MOS")
    if re.search(r"\buv\b", name):
        modifiers.append("UV")
    if "full glue" in name:
        modifiers.append("FGL")
    if "high copy+" in name:
        modifiers.append("HCP")
    elif "high copy" in name:
        modifiers.append("HC")
    if "3 в 1" in name or "3в1" in name:
        modifiers.append("3IN1")
    if match := re.search(r"\((\d{1,3})\s*шт\.?\)", name):
        modifiers.append(f"Q{match.group(1)}")
    if "musttby" in name or "mustby" in name:
        modifiers.append("MST")
    if "oca" in name:
        modifiers.append("OCA")
    if "feaglet" in name:
        modifiers.append("FEA")
    grade = _name_grade_code(product)
    value = "-".join(_compact_parts([glass_type, *modifiers[:2], color, glass_form, grade]))
    return value or None


def _flex_key(product: Product) -> str | None:
    explicit = _slug_token(product.flex_purpose, max_len=24)
    if explicit:
        return explicit
    name = (product.name or "").lower()
    parts: list[str] = []
    if "держатель" in name and ("сим" in name or "sim" in name):
        parts.append("SIMTRAY")
    elif "коннектор" in name and ("сим" in name or "sim" in name):
        parts.append("SIMCON")
    elif "разъем sim" in name or "разъём sim" in name:
        parts.append("SIMCON")
    elif "шлейф" in name:
        parts.append("FLX")
    elif "разъем" in name or "разъём" in name:
        parts.append("CON")
    if parts and parts[0] in {"SIMTRAY", "SIMCON"}:
        if "2 sim version" in name or "dual sim" in name:
            parts.append("2SIM")
        elif "1 sim version" in name:
            parts.append("1SIM")
        elif "sim + esim" in name or "sim+esim" in name:
            parts.append("ESIM")
        elif (
            parts[0] == "SIMTRAY"
            and re.search(r"\biphone\s*5\b", name)
            and not re.search(r"\biphone\s*5s\b|\biphone\s*5c\b|\biphone\s*se\b", name)
        ):
            parts.append("IP5")
    if "карты памяти" in name or "mmc" in name:
        parts.append("MMC")
    if "заряд" in name or "type-c" in name or "type c" in name:
        parts.append("CHG")
    if "main" in name:
        parts.append("MAIN")
    if "гарнитур" in name or "audio" in name or "aux" in name:
        parts.append("AUX")
    if "микрофон" in name:
        parts.append("MIC")
        if re.search(r"нижн\w*\s+микрофон|на\s+нижн\w*\s+микрофон", name):
            parts.append("LOW")
        elif re.search(r"верхн\w*\s+микрофон|на\s+верхн\w*\s+микрофон", name):
            parts.append("UP")
    if "диспле" in name:
        parts.append("DSP")
    if "тачпад" in name:
        parts.append("TPD")
    if "межплат" in name:
        parts.append("SUB")
        for token in _parenthesized_tokens(product.name):
            token_code = _slug_token(token, max_len=12)
            if (
                token_code
                and re.fullmatch(r"[A-Z0-9]{4,12}", token_code)
                and ("FL" in token_code or "FU" in token_code)
            ):
                parts.append(token_code)
                break
        if "вертикаль" in name:
            parts.append("VERT")
        if "горизонталь" in name:
            parts.append("HOR")
        if re.search(r"\bтип\s*2\b|\btype\s*2\b", name):
            parts.append("T2")
        elif re.search(r"\bтип\s*1\b|\btype\s*1\b", name):
            parts.append("T1")
        if "широк" in name or "wide" in name:
            parts.append("WIDE")
        if "узк" in name or "narrow" in name:
            parts.append("NAR")
    if "удлинитель аккумулятор" in name:
        parts.append("BATEXT")
    elif "аккумулятор" in name:
        parts.append("BAT")
    if "face id" in name or "фейс id" in name:
        parts.append("FACEID")
    if "передней камер" in name or "фронтальн" in name or "front camera" in name:
        parts.append("FCAM")
    elif "задней камер" in name or "основной камер" in name or "rear camera" in name:
        parts.append("RCAM")
    if "сенсор" in name:
        parts.append("SENS")
    if "вспыш" in name or "flash" in name:
        parts.append("FLASH")
    if "esim" in name or "e-sim" in name:
        parts.append("ESIM")
    if "кнопк" in name:
        if "включен" in name or "power" in name:
            parts.append("PWRBTN")
        elif "громк" in name or "volume" in name:
            parts.append("VOLBTN")
        else:
            parts.append("BTN")
    if "сканер" in name or "fingerprint" in name:
        parts.append("FPR")
    if "digital crown" in name:
        parts.append("DCRWN")
    if "антенн" in name:
        antenna_parts: list[str] = []
        if "nfc" in name:
            antenna_parts.append("NFC")
        if "gsm" in name:
            antenna_parts.append("GSM")
        if "gps" in name:
            antenna_parts.append("GPS")
        if "wi-fi" in name or "wifi" in name:
            antenna_parts.append("WIFI")
        if "bluetooth" in name or " bt" in name:
            antenna_parts.append("BT")
        parts.append("ANT" + "".join(antenna_parts) if antenna_parts else "ANT")
    if "вибро" in name:
        parts.append("VIB")
    if "динамик" in name:
        parts.append("SPK")
    if "i/o плат" in name or "плату ввода" in name or "плата ввода" in name:
        parts.append("IO")
    if "жесткий диск" in name or "hard drive" in name or "hdd" in name:
        parts.append("HDD")
    if "усилитель" in name and ("wi-fi" in name or "wifi" in name):
        parts.append("WFIAMP")
    repair_modifier_targets = {"BAT", "FACEID", "FCAM", "RCAM", "SENS", "WFIAMP"}
    if any(part in repair_modifier_targets for part in parts):
        if "jcid" in name:
            parts.append("JCID")
        elif grade := _name_grade_code(product):
            parts.append(grade)
        if "сборе с коннектор" in name:
            parts.append("CON")
    if "wi-fi version" in name or "wifi version" in name:
        parts.append("WFI")
    if "cellular version" in name:
        parts.append("CEL")
    if not any(part in {"T1", "T2"} for part in parts):
        if re.search(r"\bтип\s*2\b|\btype\s*2\b", name):
            parts.append("T2")
        elif re.search(r"\bтип\s*1\b|\btype\s*1\b", name):
            parts.append("T1")
    if "macbook" in name:
        year_code = _year_span_code(name)
        if year_code:
            parts.append(year_code)
    if len(parts) > 1 and parts[0] == "FLX":
        parts = parts[1:]
    color = _color_code_from_product_or_name(product)
    finish = None
    if "матов" in name:
        finish = "MAT"
    elif "глян" in name and color != "BLKGL":
        finish = "GLS"
    flex_grade_targets = {"CHG", "MIC", "SUB", "DSP", "PWRBTN"}
    grade = _name_grade_code(product) if any(part in flex_grade_targets for part in parts) else None
    return "-".join(_compact_parts([*parts[:4], color, finish, grade])) or None


def _chip_key(product: Product) -> str | None:
    explicit = _slug_token(product.chip_code, max_len=24)
    if explicit:
        return explicit
    name = (product.name or "").lower()
    if "микросхема" in name:
        if match := re.search(
            r"\b(p13usb|pi3usb|bq\d{5}|cxd\d{5}[a-z0-9]*|max\d{4,8}[a-z0-9]*|m\d{2}t\d{2})\b",
            name,
        ):
            chip_code = _slug_token(match.group(1), max_len=24)
            if chip_code:
                return "-".join(_compact_parts([chip_code, _name_grade_code(product)]))
        for token in _parenthesized_tokens(product.name):
            if any(marker in token for marker in ("orig", "ориг", "premium")):
                continue
            chip_code = _slug_token(token, max_len=24)
            if chip_code:
                return "-".join(_compact_parts([chip_code, _name_grade_code(product)]))
    parts: list[str] = []
    if "нижняя плата" in name:
        parts.append("SUB")
    elif "плата" in name:
        parts.append("PCB")
    if "заряд" in name or "type-c" in name or "type c" in name:
        parts.append("CHG")
    if "микрофон" in name:
        parts.append("MIC")
    if "гарнитур" in name:
        parts.append("AUX")
    if "sim" in name or "сим" in name:
        parts.append("SIM")
    if "вибро" in name:
        parts.append("VIB")
    if "без микросхем" in name:
        parts.append("NOIC")
    if match := re.search(r"\b(\d{2,4})\s*(?:гб|gb)\b", name):
        parts.append(f"{match.group(1)}G")
    if "icloud locked" in name:
        parts.append("ICL")
    return "-".join(_compact_parts([*parts[:4], _name_grade_code(product)])) or None


def _tester_key(product: Product) -> str | None:
    name = (product.name or "").lower()
    if match := re.search(r"\bdl[-\s]?(\d{3,4})\b", name):
        return f"DL{match.group(1)}"
    if "xzz" in name and (match := re.search(r"\biphone\s*(\d{1,2})\b", name)):
        parts = ["XZZ", f"IP{match.group(1)}"]
        if "sim version" in name:
            parts.append("SIM")
        return "-".join(parts)
    if match := re.search(r"\brelife\s+([a-z]{1,3}\d{1,3})\b", name):
        parts = [match.group(1).upper()]
        if "заряд" in name:
            parts.append("CHG")
        return "-".join(parts)
    if match := re.search(r"\bs[-\s]?(\d{3,4})\b", name):
        parts = [f"S{match.group(1)}"]
        if "диспле" in name:
            parts.append("DSP")
        return "-".join(parts)
    if "home" in name:
        if match := re.search(r"версия\s*(\d+)", name):
            return f"HOME-V{match.group(1)}"
        return "HOME"
    if "тестер" in name or "tester" in name:
        return "TEST"
    return None


def _game_part_number_code(name: str | None) -> str | None:
    normalized = _normalize_accessory_name(name)
    if not normalized:
        return None
    patterns = (
        r"\b(adp)[-\s]?(\d{3}[a-z]{2})\b",
        r"\b(cfi)[-\s]?(\d{4})\b",
        r"\b(cuh)[-\s]?(\d{2}xx|\d{4}[a-z]?)\b",
        r"\b(kes|kem|dg|hop|bee|yce|jys)[-\s]?([a-z0-9]{2,8})\b",
        r"\b(p13usb|pi3usb|bq\d{5}|cxd\d{5}[a-z0-9]*)\b",
        r"\b(max\d{4,8}[a-z0-9]*)\b",
        r"\b(m\d{2}t\d{2})\b",
    )
    for pattern in patterns:
        if match := re.search(pattern, normalized):
            value = "".join(part for part in match.groups() if part)
            return _slug_token(value, max_len=14)
    return None


def _game_controller_model_code(name: str | None) -> str | None:
    normalized = _normalize_accessory_name(name)
    if not normalized:
        return None
    patterns = (
        (r"\bmobapad\s+m6[-\s]?hd\b", "M6HD"),
        (r"\bmobapad\s+m6s\b", "M6S"),
        (r"\bgamesir\s+nova\s+lite\s+t4n\b", "GNLT4N"),
        (r"\bgamesir\s+nova\s+ns\s+t4n\b", "GNST4N"),
        (r"\bpxn[-\s]?0082\b", "PXN82"),
        (r"\bs07\b", "S07"),
    )
    for pattern, code in patterns:
        if re.search(pattern, normalized):
            return code
    return None


def _game_position_flags(name: str | None) -> list[str]:
    normalized = _normalize_accessory_name(name)
    flags: list[str] = []
    if "передн" in normalized:
        flags.append("FR")
    if "верхн" in normalized:
        flags.append("UP")
    if "нижн" in normalized or "нжний" in normalized:
        flags.append("DN")
    if "лев" in normalized:
        flags.append("L")
    if "прав" in normalized:
        flags.append("R")
    return flags


def _quantity_flag_from_name(name: str | None) -> str | None:
    normalized = _normalize_accessory_name(name)
    if match := re.search(r"\b(\d{1,2})\s*шт", normalized):
        return f"Q{match.group(1)}"
    if "пара" in normalized:
        return "Q2"
    return None


def _game_accessory_key(product: Product, category_code: str | None) -> str | None:
    if not _game_device_code_from_name(product.name):
        return None
    name = _normalize_accessory_name(product.name)
    if not name:
        return None

    model = _game_part_number_code(product.name)
    color = _color_code_from_product_or_name(product)
    qty = _quantity_flag_from_name(product.name)
    positions = _game_position_flags(product.name)
    grade = _name_grade_code(product)

    if category_code == "CBL":
        if "переключатель камеры" in name:
            return "-".join(_compact_parts(["CAMSW", *positions]))
        if "сигнальная лампа" in name:
            return "-".join(_compact_parts(["LED", *positions]))
        if "разъем кабеля" in name or "разъём кабеля" in name:
            return "CON"
        return None

    if category_code == "DSP":
        is_touch_only = re.match(r"\s*(?:тачскрин|сенсор)\b", name) is not None
        parts = [_display_tech_code(product)]
        if is_touch_only:
            parts.append("TCH")
        parts.append(_display_color_code(product) or color or "STD")
        if "тачскрин" in name and not is_touch_only:
            parts.append("TCH")
        if "матов" in name:
            parts.append("MAT")
        if "держател" in name and "линз" in name:
            parts.append("LH")
        parts.extend(positions)
        return "-".join(_compact_parts(parts))

    if category_code == "CHR":
        if "memory stick" in name or "pro duo" in name:
            return "MSADP"
        if "блок питания" in name:
            return "-".join(_compact_parts(["PSU", model, grade]))
        if "сетевое зарядное устройство" in name:
            flags = ["FCH"] if "быстрая заряд" in name else []
            return "-".join(_compact_parts(["AC", *flags]))
        if "док-станц" in name or "зарядная станция" in name:
            flags: list[str] = []
            if "type-c - hdmi" in name or ("usb 3.0" in name and "pd" in name):
                flags.append("HUB")
            elif "hdmi" in name:
                flags.append("HDMI")
            if "сетев" in name:
                flags.append("LAN")
            if "m.2" in name or "ssd" in name:
                flags.append("SSD")
            return "-".join(_compact_parts(["DOCK", model, color, *([] if model else flags[:1])]))
        return None

    if category_code == "CAM":
        return "-".join(_compact_parts([*positions, grade])) or "STD"

    if category_code == "IC":
        if "плата питания" in name:
            return "-".join(_compact_parts(["PWRPCB", grade]))
        if "микросхем" in name:
            flags: list[str] = []
            if "заряд" in name:
                flags.append("CHG")
            elif "питания" in name or "питание" in name:
                flags.append("PWR")
            return "-".join(_compact_parts([model, *flags])) or None
        return None

    if category_code == "FLX":
        parts: list[str] = []
        if "на кнопку включения" in name and "громк" in name:
            parts.append("PWRVOL")
        elif "правую" in name and "zr" in name:
            parts.extend(["BTN", "ZR"])
        elif "инфрокрас" in name:
            parts.append("IR")
        elif "разъем гарнитуры" in name or "разъём гарнитуры" in name:
            parts.append("AUX")
        elif "датчик гарнитуры" in name:
            parts.append("AUXSENS")
        elif "датчик" in name and "глубин" in name:
            parts.append("DEPTHSENS" if name.startswith("датчик") else "DEPTH")
        elif "антенн" in name:
            parts.append("ANT")
            if "nfc" in name:
                parts.append("NFC")
            if "инфрокрас" in name:
                parts.append("IR")
        elif "переключатель камеры" in name:
            parts.append("CAMSW")
        elif "на камеру" in name:
            parts.append("CAM")
            if re.search(r"\bтип\s*a\b", name):
                parts.append("A")
            elif re.search(r"\bтип\s*b\b", name):
                parts.append("B")
            if "питание" in name:
                parts.append("PWR")
        elif "позиционирован" in name:
            parts.append("CTRLPOS")
        elif "обнаружение контроллера" in name:
            parts.append("CTRLDET")
        elif "для питания контроллера" in name:
            parts.append("CTRLPWR")
        elif "датчик глубины" in name:
            parts.append("DEPTH")
        elif "фоточувств" in name:
            parts.append("PHOTO")
        elif "сигнальная лампа" in name:
            parts.append("LED")
        elif "контроллер" in name:
            parts.append("CTRL")
        elif "модуль питания" in name:
            parts.append("PWRMOD")
        elif "кабель питания" in name:
            parts.append("PWRCBL")
        elif "joy-con" in name:
            parts.append("JC")
            if "слайдер" in name:
                parts.append("SLD")
        elif "sl / sr" in name:
            parts.append("SLSR")
        elif "sd карт" in name:
            parts.append("SD")
        if parts:
            light = None
            if "с подсветк" in name:
                light = "LED"
            elif "без подсветк" in name:
                light = "NLED"
            return "-".join(_compact_parts([*parts, *positions, qty, light, grade]))
        return None

    if category_code == "SET":
        if "отверт" in name:
            if match := re.search(r"\b(\d{1,2})\s*в\s*1\b", name):
                return f"TOOL{match.group(1)}"
            return "TOOL"
        return None

    if category_code != "PRT":
        return None

    part: str | None = None
    flags: list[str] = []
    if "джостик" in name or "геймпад" in name:
        if "геймпад" in name:
            part = "GPAD"
            model = _game_controller_model_code(product.name)
            if "беспровод" in name:
                flags.append("WL")
            if "вибрац" in name:
                flags.append("VIB")
        else:
            part = "JOY"
            model = None
        if "магнит" in name and "электромагнит" not in name:
            flags.append("MAG")
        if "электромагнит" in name:
            flags.append("EMG")
        elif "аналог" in name:
            flags.append("ANL")
        if "потенциометр" in name:
            flags.append("POT")
        if "высокоточ" in name:
            flags.append("HI")
        if match := re.search(r"\bgen\.?\s*(\d+)\b", name):
            flags.append(f"G{match.group(1)}")
        if "без стика" in name:
            flags.append("NOSTICK")
        if "защита от дрейфа" in name:
            flags.append("NODRIFT")
        if "joy-con" in name:
            flags.append("JC")
    elif ("комплект кнопок" in name or "набор" in name) and "кноп" in name and "стик" in name:
        part = "BTNSTK"
        if match := re.search(r"\bv\s*(\d)(?:\.0)?\b", name):
            flags.append(f"V{match.group(1)}")
    elif "стики" in name or "стик" in name:
        part = "STICK"
        if "наклад" in name:
            part = "STCOV"
    elif "кноп" in name:
        part = "BTN"
        if re.search(r"\ba\s*/\s*b\s*/\s*x\s*/\s*y\b", name):
            flags.append("ABXY")
        if re.search(r"\brb\s*/\s*lb\b", name):
            flags.append("RBLB")
        if re.search(r"\bl1\s*/\s*r1\b", name):
            flags.append("L1R1")
        if re.search(r"\bl4\s*/\s*l5\s*/\s*r4\s*/\s*r5\b", name):
            flags.append("L4R5")
        if re.search(r"\bl1\s*,?\s*r1\s*,?\s*l2\s*,?\s*r2\b", name):
            flags.append("L1R2")
    elif "привод" in name:
        part = "DRV"
        if "blu-ray" in name or "blue-ray" in name:
            flags.append("BR")
        if "с платой" in name:
            flags.append("PCB")
        if "замена" in name:
            flags.append("REP")
        if "cuh-12" in name or "cuh-12xxx" in name:
            flags.append("CUH12")
    elif "передняя панель" in name or re.search(r"\bпанел", name):
        part = "PNL"
    elif "чехол" in name:
        part = "CASE"
        if "силикон" in name:
            flags.append("SIL")
        if "влагозащит" in name:
            flags.append("WP")
    elif "сумка" in name:
        part = "BAG"
        if "через плечо" in name:
            flags.append("SLING")
        if "перегород" in name:
            flags.append("SEP")
        if "аксессуар" in name:
            flags.append("ACC")
        if "us version" in name:
            flags.append("US")
    elif "радиатор" in name:
        part = "HSINK"
        if "ssd" in name:
            flags.append("SSD")
        if "set. a" in name:
            flags.append("A")
        elif "set. b" in name:
            flags.append("B")
    elif "слайдер" in name or "направляющ" in name:
        part = "RAIL"
        if "со шлейфом" in name:
            flags.append("FLX")
        if "набор инструментов" in name:
            flags.append("TOOL")
    elif "ремеш" in name:
        part = "STRAP"
        if "нога" in name:
            flags.append("LEG")
    elif "футляр" in name:
        part = "CARTCASE" if "картридж" in name else "CASE"
        if match := re.search(r"\b(\d{1,2})\s*слот", name):
            flags.append(f"S{match.group(1)}")
    elif "подставк" in name:
        part = "STAND"
    elif "накладк" in name:
        part = "PAD"
        if "joy-con" in name:
            part = "JCCOV"
        if "резинов" in name:
            flags.append("RUB")
        if "токопровод" in name:
            flags.append("COND")

    if not part:
        return None
    return "-".join(_compact_parts([part, model, color, qty, grade, *positions, *flags]))


def _year_span_code(name: str) -> str | None:
    years = re.findall(r"\b(20\d{2}|19\d{2})\b", name)
    if not years:
        return None
    unique_years = list(dict.fromkeys(years))
    if len(unique_years) == 1:
        return f"Y{unique_years[0][-2:]}"
    return f"Y{unique_years[0][-2:]}{unique_years[-1][-2:]}"


def _screen_size_code(name: str) -> str | None:
    if match := re.search(r"\b(\d{1,2})[.,](\d)\b", name):
        return f"S{match.group(1)}{match.group(2)}"
    return None


def _part_key(product: Product) -> str | None:
    explicit = _slug_token(product.part_type, max_len=24)
    if explicit:
        return explicit
    name = (product.name or "").lower()
    category = (product.category or "").lower()
    subject = (product.subject or "").lower()
    is_cooler = "вентилятор" in name or "кулер" in name or "кулер" in category
    is_keyboard = "клавиатур" in name or subject == "клавиатура" or "клавиатуры" in category
    part: str | None = None
    special_part = False
    if is_cooler:
        part = "FAN"
        special_part = True
    elif is_keyboard:
        flags = ["FR"] if "в рамке" in name or "рамк" in name else []
        if "вертикаль" in name:
            flags.append("VERT")
        if "горизонталь" in name:
            flags.append("HOR")
        return "-".join(
            _compact_parts(
                [
                    "KBD",
                    *flags,
                    _color_code_from_product_or_name(product),
                    _name_grade_code(product),
                ]
            )
        )
    elif "сеточ" in name and "динамик" in name:
        part = "SPK-MESH"
        special_part = True
    elif subject == "сетка динамика" or ("сеточк" in category and "динамик" in category):
        part = "SPK-MESH"
        special_part = True
    elif "динамик" in name or subject == "динамик" or "динамики для" in category:
        if "слухов" in name:
            part = "EARSPK"
        elif "верхн" in name:
            part = "SPK-UP"
        elif "нижн" in name:
            part = "SPK-DN"
        elif "лев" in name:
            part = "SPK-L"
        elif "прав" in name:
            part = "SPK-R"
        else:
            part = "SPK"
        special_part = True
    elif "вибромотор" in name or subject == "вибромотор":
        part = "VIB"
        special_part = True
    elif "резиновые ножки" in name:
        part = "FEET"
    elif "тачпад" in name:
        part = "TPD"
    elif "магнит magsafe" in name:
        part = "MAGSAFE"
    elif "пленка oca" in name or "плёнка oca" in name:
        part = "OCA"
    elif "поляризационная пленка" in name or "поляризационная плёнка" in name:
        part = "POLFLM"
    elif "подсветка дисплея" in name:
        part = "BKL"
    elif "форма дисплея" in name:
        part = "DSPMOLD"
    elif "форма вакуумного подогрева" in name:
        part = "VACMOLD"
    elif name.startswith("форма "):
        part = "MOLD"
    elif "проклейк" in name or "проклейк" in category or "прокладк" in name:
        if "передней камеры" in name:
            part = "FCAM-GSK"
        elif "задней крыш" in name or "задних крыш" in category:
            part = "BCOV-ADH"
        elif "дисплейн" in name or "дисплейн" in category:
            part = "DSP-ADH"
        elif "аккумулятор" in name or "аккумулятор" in category:
            part = "BAT-ADH"
        else:
            part = "ADH"
        special_part = True
    elif "задняя крыш" in name:
        part = "BCOV"
    elif "рамка диспле" in name:
        part = "DFRM"
    elif "средняя часть" in name or "средняя рам" in name:
        part = "MID"
    elif "корпус" in name:
        part = "HOUS"
    elif "крыш" in name:
        part = "COV"
    elif "рамк" in name:
        part = "FRM"
    elif "держатель" in name and ("сим" in name or "sim" in name):
        part = "SIMTRAY"
    elif "кнопк" in name:
        part = "BTN"
    elif "креплен" in name or "крепеж" in name or "крепёж" in name:
        part = "MNT"
    elif "винт" in name:
        part = "SCR"
    if not part:
        return None
    flags: list[str] = []
    has_camera_glass = "со стеклом камеры" in name
    if "в сборе" in name and not has_camera_glass:
        flags.append("ASM")
    if has_camera_glass:
        flags.append("CG")
    if re.search(r"\(\s*sp\s*\)", name):
        flags.append("SP")
    if part == "BCOV" and ("широким отверстием" in name or "wide hole" in name):
        flags.append("WIDE")
    if part == "SPK-MESH":
        if "слухов" in name:
            flags.append("EAR")
        elif "полифоническ" in name:
            flags.append("POLY")
        if "с комп" in name:
            flags.append("KIT")
    if part in {"EARSPK", "SPK", "SPK-UP", "SPK-DN", "SPK-L", "SPK-R"}:
        if "малый" in name or "small" in name:
            flags.append("SM")
        if "лев" in name and "прав" in name:
            flags.append("LR")
        if "сабвуфер" in name or "subwoofer" in name:
            flags.append("SUB")
    if part.endswith("-ADH"):
        if "2uul" in name:
            flags.append("2UUL")
        if match := re.search(r"комплект\s+(\d{1,2})\s*шт", name):
            flags.append(f"K{match.group(1)}")
    if part in {"HOUS", "MID", "MNT"}:
        if "sim + esim" in name or "sim+esim" in name:
            flags.append("SIME")
        elif re.search(r"\besim\b", name):
            flags.append("ESIM")
    if part == "HOUS":
        if "материнск" in name and "плат" in name:
            flags.append("PCB")
        if re.search(r"в\s+дизайне\s+apple\s+iphone\s+17\s+pro\s+max", name):
            flags.append("D17PM")
    if part in {"HOUS", "FRM", "DFRM", "MID", "BCOV"}:
        if re.search(r"версия:\s*(?:wi-fi|wifi)", name) or "wi-fi version" in name:
            flags.append("WFI")
        if re.search(r"версия:\s*4g", name) or "cellular version" in name:
            flags.append("CEL")
        if "us version" in name:
            flags.append("US")
        if "china version" in name:
            flags.append("CN")
    if part == "BTN":
        if re.search(r"\ba\s*/\s*b\s*/\s*x\s*/\s*y\b", name):
            flags.append("ABXY")
        if re.search(r"\brb\s*/\s*lb\b", name):
            flags.append("RBLB")
    if part == "FAN":
        if "лев" in name:
            flags.append("L")
        if "прав" in name:
            flags.append("R")
        if match := re.search(r"(\d{1,2})\s+лопаст", name):
            flags.append(f"B{match.group(1)}")
    if part == "FEET":
        if match := re.search(r"комплект\s+(\d{1,2})\s*шт", name):
            flags.append(f"K{match.group(1)}")
    if part == "SCR":
        if match := re.search(r"(?:комплект|набор)?\s*(\d{1,2})\s*шт", name):
            flags.append(f"K{match.group(1)}")
        if "joy-con" in name:
            flags.append("JC")
    if part in {"BCOV", "DFRM", "FRM", "HOUS", "MID"}:
        if "freefire" in name or "free fire" in name:
            flags.append("FFE")
        if "матов" in name:
            flags.append("MAT")
        elif "глян" in name:
            flags.append("GLS")
    if part in {"TPD", "MAGSAFE"}:
        year_code = _year_span_code(name)
        if year_code:
            flags.append(year_code)
    if part == "BKL":
        if "без функции 3d touch" in name:
            flags.append("N3D")
        elif "3d touch" in name:
            flags.append("3D")
    if part in {"OCA", "POLFLM"}:
        if match := re.search(r"(\d{2,3})\s*(?:um|микрон)", name):
            flags.append(f"{match.group(1)}UM")
        if match := re.search(r"\((\d{2,3})\s*градус", name):
            flags.append(f"D{match.group(1)}")
        if part == "POLFLM":
            size_code = _screen_size_code(name)
            if size_code:
                flags.append(size_code)
    if part in {"DSPMOLD", "VACMOLD", "MOLD"}:
        if "металличес" in name:
            flags.append("MET")
        if "пластиков" in name:
            flags.append("PL")
        if "резинов" in name:
            flags.append("RUB")
    if special_part:
        return "-".join(_compact_parts([part, _name_grade_code(product), *flags]))
    return "-".join(
        _compact_parts(
            [part, _color_code_from_product_or_name(product), _name_grade_code(product), *flags]
        )
    )


def infer_key(product: Product, category_code: str | None = None) -> str | None:
    category = category_code or infer_category_code(product)
    game_key = _game_accessory_key(product, category)
    if game_key:
        return game_key
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
        return _flex_key(product)
    if category == "IC":
        return _chip_key(product)
    if category == "GLS":
        return _glass_key(product)
    if category == "PRT":
        return _part_key(product)
    if category == "SET":
        if product.set_quantity:
            return f"KIT-{product.set_quantity}PCS"
        return _slug_token(product.set_composition, max_len=24)
    if category == "TST":
        return _tester_key(product)
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
        fallback_sku: str | None = None
        fallback_key = key_code
        fallback_rev = rev
        if str(exc) == "sku exceeds max length":
            fallback_keys = [key_code, *_length_fallback_key_codes(category_code, key_code)]
            fallback_revs = [rev, *_length_fallback_revs(rev)]
            for candidate_key in fallback_keys:
                for candidate_rev in fallback_revs:
                    if candidate_key == key_code and candidate_rev == rev:
                        continue
                    try:
                        fallback_sku = build_sku(
                            brand_code,
                            category_code,
                            device_code,
                            candidate_key,
                            candidate_rev,
                        )
                    except SkuValidationError:
                        continue
                    fallback_key = candidate_key
                    fallback_rev = candidate_rev
                    break
                if fallback_sku:
                    break
        if fallback_sku:
            planned_sku = fallback_sku
            key_code = fallback_key
            rev = fallback_rev
        else:
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
    active_only: bool = True,
) -> dict[str, object]:
    query = select(Product).order_by(Product.id)
    if product_ids:
        query = query.where(Product.id.in_(list(product_ids)))
    if active_only:
        query = query.where(
            Product.is_active.is_(True),
            Product.is_marked_for_deletion.is_(False),
        )
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
                "code_1c": product.code_1c,
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
        "active_only": active_only,
        "items": results,
    }
