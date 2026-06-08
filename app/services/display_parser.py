from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ScreenMatrixType(StrEnum):
    LCD_TFT = "LCD_TFT"
    LCD_IPS = "LCD_IPS"
    LTPS_LCD = "LTPS_LCD"
    OLED = "OLED"
    AMOLED = "AMOLED"
    LTPO_AMOLED = "LTPO_AMOLED"
    UNKNOWN = "UNKNOWN"


class ScreenKit(StrEnum):
    DISPLAY_ONLY = "DISPLAY_ONLY"
    DISPLAY_WITH_TOUCH = "DISPLAY_WITH_TOUCH"
    DISPLAY_WITH_FRAME = "DISPLAY_WITH_FRAME"
    DISPLAY_TOUCH_FRAME = "DISPLAY_TOUCH_FRAME"
    UNKNOWN = "UNKNOWN"


class Backlight(StrEnum):
    WITH_BACKLIGHT = "WITH_BACKLIGHT"
    BRIGHT_BACKLIGHT = "BRIGHT_BACKLIGHT"
    NO_BACKLIGHT = "NO_BACKLIGHT"
    UNKNOWN = "UNKNOWN"


class ScreenConstruction(StrEnum):
    HARD_OLED = "HARD_OLED"
    SOFT_OLED = "SOFT_OLED"
    INCELL = "INCELL"
    ONCELL = "ONCELL"
    COF = "COF"
    COG = "COG"
    UNKNOWN = "UNKNOWN"


class ScreenQualityGrade(StrEnum):
    ORIGINAL = "ORIGINAL"
    ORIGINAL_REFURB = "ORIGINAL_REFURB"
    OEM = "OEM"
    GX = "GX"
    OR = "OR"
    OR100 = "OR100"
    PREMIUM = "PREMIUM"
    AAA = "AAA"
    HQ = "HQ"
    FIRST_CLASS = "FIRST_CLASS"
    COPY_HIGH = "COPY_HIGH"
    COPY_MEDIUM = "COPY_MEDIUM"
    COPY_LOW = "COPY_LOW"
    UNKNOWN = "UNKNOWN"


SCREEN_MATRIX_TYPE_RU = {
    ScreenMatrixType.LCD_TFT: "LCD (TFT)",
    ScreenMatrixType.LCD_IPS: "LCD (IPS)",
    ScreenMatrixType.LTPS_LCD: "LTPS LCD",
    ScreenMatrixType.OLED: "OLED",
    ScreenMatrixType.AMOLED: "AMOLED",
    ScreenMatrixType.LTPO_AMOLED: "LTPO AMOLED",
    ScreenMatrixType.UNKNOWN: "Не определено",
}

SCREEN_KIT_RU = {
    ScreenKit.DISPLAY_ONLY: "Без тачскрина",
    ScreenKit.DISPLAY_WITH_TOUCH: "В сборе с тачскрином",
    ScreenKit.DISPLAY_WITH_FRAME: "В сборе с рамкой",
    ScreenKit.DISPLAY_TOUCH_FRAME: "В сборе с тачскрином и рамкой",
    ScreenKit.UNKNOWN: "Не определено",
}

BACKLIGHT_RU = {
    Backlight.WITH_BACKLIGHT: "С подсветкой",
    Backlight.BRIGHT_BACKLIGHT: "Яркая подсветка",
    Backlight.NO_BACKLIGHT: "Без подсветки",
    Backlight.UNKNOWN: "Не определено",
}

SCREEN_CONSTRUCTION_RU = {
    ScreenConstruction.HARD_OLED: "Hard OLED",
    ScreenConstruction.SOFT_OLED: "Soft OLED",
    ScreenConstruction.INCELL: "In-Cell",
    ScreenConstruction.ONCELL: "On-Cell",
    ScreenConstruction.COF: "COF",
    ScreenConstruction.COG: "COG",
    ScreenConstruction.UNKNOWN: "Не определено",
}

SCREEN_QUALITY_RU = {
    ScreenQualityGrade.ORIGINAL: "Оригинал",
    ScreenQualityGrade.ORIGINAL_REFURB: "Оригинал (замена стекла)",
    ScreenQualityGrade.OEM: "OEM",
    ScreenQualityGrade.GX: "GX",
    ScreenQualityGrade.OR: "OR",
    ScreenQualityGrade.OR100: "OR 100%",
    ScreenQualityGrade.PREMIUM: "Премиум",
    ScreenQualityGrade.AAA: "AAA",
    ScreenQualityGrade.HQ: "HQ",
    ScreenQualityGrade.FIRST_CLASS: "1-я категория",
    ScreenQualityGrade.COPY_HIGH: "Копия (высокая)",
    ScreenQualityGrade.COPY_MEDIUM: "Копия (средняя)",
    ScreenQualityGrade.COPY_LOW: "Копия (низкая)",
    ScreenQualityGrade.UNKNOWN: "Не определено",
}

PARSE_STATUS_OK = "OK"
PARSE_STATUS_PARTIAL = "PARTIAL"
PARSE_STATUS_FAILED = "FAILED"

FORBIDDEN_MATRIX_PHRASES = (
    "для смартфона",
    "для iphone",
    "для айфон",
    "дисплей",
    "тачскрин",
    "подсветк",
    "рамк",
    "retina",
    "xdr",
    "hd",
    "fhd",
    "uhd",
)

RESOLUTION_RE = re.compile(r"\b\d{3,4}x\d{3,4}\b")

NOTE_STOPWORDS = {
    "для",
    "и",
    "в",
    "во",
    "с",
    "без",
    "на",
    "по",
    "под",
    "рамка",
    "рамки",
    "рамкой",
    "рамке",
    "тачскрин",
    "тачскрином",
    "сенсор",
    "сенсором",
    "дисплей",
    "экран",
    "модуль",
}

COLOR_PATTERNS = {
    "Черный": re.compile(r"\b(black|черн\w*)\b"),
    "Белый": re.compile(r"\b(white|бел\w*)\b"),
    "Красный": re.compile(r"\b(red|красн\w*)\b"),
    "Оранжевый": re.compile(r"\b(orange|оранж\w*)\b"),
    "Коралловый": re.compile(r"\b(coral|коралл\w*)\b"),
    "Желтый": re.compile(r"\b(yellow|желт\w*|жёлт\w*)\b"),
    "Синий": re.compile(r"\b(blue|син\w*)\b"),
    "Зеленый": re.compile(r"\b(green|зелен\w*)\b"),
    "Мятный": re.compile(r"\b(mint|мятн\w*)\b"),
    "Розовый": re.compile(r"\b(pink|розов\w*)\b"),
    "Фиолетовый": re.compile(r"\b(purple|violet|lavender|фиолет\w*|лаванд\w*)\b"),
    "Золотой": re.compile(r"\b(gold|golden|золот\w*)\b"),
    "Серебристый": re.compile(r"\b(silver|серебр\w*)\b"),
    "Бежевый": re.compile(r"\b(beige|бежев\w*)\b"),
    "Графитовый": re.compile(r"\b(graphite|графит\w*)\b"),
    "Бронзовый": re.compile(r"\b(bronze|бронз\w*)\b"),
    "Коричневый": re.compile(r"\b(brown|коричнев\w*)\b"),
    "Титановый": re.compile(r"\b(titanium|титан\w*)\b"),
    "Серый": re.compile(r"\b(gray|grey|серый|серая|серое|серые)\b"),
}

MANUFACTURER_PATTERNS = [
    ("Apple Co (SP)", re.compile(r"\bapple\s*co\s*sp\b")),
    ("Apple Co", re.compile(r"\bapple\s*co\b")),
    ("GX ORIG", re.compile(r"\bgx\s*orig\b")),
    ("F5ENERGY", re.compile(r"\bf5energy\b")),
    ("MOSHI", re.compile(r"\bmoshi\b")),
    ("Panda", re.compile(r"\bpanda\b")),
    ("LVIN", re.compile(r"\blvin\b")),
    ("MNK", re.compile(r"\bmnk\b")),
    ("MM", re.compile(r"\bmm\b")),
    ("RJ", re.compile(r"\brj\b")),
    ("SL", re.compile(r"\bsl\b")),
    ("DD", re.compile(r"\bdd\b")),
    ("HEX", re.compile(r"\bhex\b")),
    ("JK", re.compile(r"\bjk\b")),
    ("ALG", re.compile(r"\balg\b")),
    ("LOW", re.compile(r"\blow\b")),
    ("FOG", re.compile(r"\bfog\b")),
    ("GX", re.compile(r"\bgx\b")),
    ("ZY", re.compile(r"\bzy\b")),
    ("JCID", re.compile(r"\bjcid\b")),
]

MATRIX_TAG_PATTERNS = {
    "JCID": re.compile(r"\bjcid\b"),
    "ZY": re.compile(r"\bzy\b"),
    "Zetton": re.compile(r"\bzetton\b"),
    "GJX": re.compile(r"\bgjx\b"),
}


@dataclass
class DisplayParseResult:
    screen_matrix_type: ScreenMatrixType = ScreenMatrixType.UNKNOWN
    screen_kit: ScreenKit = ScreenKit.UNKNOWN
    backlight: Backlight = Backlight.UNKNOWN
    screen_construction: ScreenConstruction = ScreenConstruction.UNKNOWN
    screen_quality_grade: ScreenQualityGrade = ScreenQualityGrade.UNKNOWN
    refresh_rate_hz: int | None = None
    oleophobic: bool | None = None
    color: str | None = None
    has_frame: bool | None = None
    has_touch: bool | None = None
    has_ic_pad: bool | None = None
    has_binding_no_solder: bool | None = None
    manufacturer: str | None = None
    matrix_tags: list[str] = field(default_factory=list)
    notes_raw_tokens: list[str] = field(default_factory=list)
    parse_status: str = PARSE_STATUS_FAILED
    reasons: list[str] = field(default_factory=list)
    extracted_tokens: dict[str, list[str]] = field(default_factory=dict)
    llm_output: str | None = None

    def to_attrs(self) -> dict[str, Any]:
        final_result = {
            "screen_matrix_type": self.screen_matrix_type.value,
            "screen_matrix_type_ru": SCREEN_MATRIX_TYPE_RU[self.screen_matrix_type],
            "screen_kit": self.screen_kit.value,
            "screen_kit_ru": SCREEN_KIT_RU[self.screen_kit],
            "backlight": self.backlight.value,
            "backlight_ru": BACKLIGHT_RU[self.backlight],
            "screen_construction": self.screen_construction.value,
            "screen_construction_ru": SCREEN_CONSTRUCTION_RU[self.screen_construction],
            "screen_quality_grade": self.screen_quality_grade.value,
            "screen_quality_grade_ru": SCREEN_QUALITY_RU[self.screen_quality_grade],
            "refresh_rate_hz": self.refresh_rate_hz,
            "oleophobic": self.oleophobic,
            "color": self.color or "Не определено",
            "has_frame": self.has_frame,
            "has_touch": self.has_touch,
            "has_ic_pad": self.has_ic_pad,
            "has_binding_no_solder": self.has_binding_no_solder,
            "manufacturer": self.manufacturer,
            "matrix_tags": self.matrix_tags,
        }
        return {
            "screen_matrix_type": self.screen_matrix_type.value,
            "screen_kit": self.screen_kit.value,
            "backlight": self.backlight.value,
            "screen_construction": self.screen_construction.value,
            "screen_quality_grade": self.screen_quality_grade.value,
            "refresh_rate_hz": self.refresh_rate_hz,
            "oleophobic": self.oleophobic,
            "color": self.color,
            "has_frame": self.has_frame,
            "has_touch": self.has_touch,
            "has_ic_pad": self.has_ic_pad,
            "has_binding_no_solder": self.has_binding_no_solder,
            "manufacturer": self.manufacturer,
            "matrix_tags": self.matrix_tags,
            "notes_raw_tokens": self.notes_raw_tokens,
            "parse_status": self.parse_status,
            "reasons": self.reasons,
            "extracted_tokens": self.extracted_tokens,
            "llm_output": self.llm_output,
            "final_result": final_result,
        }


def normalize_display_name(value: str) -> str:
    text = value.lower().replace("ё", "е")
    text = re.sub(r"[+/_()\\-]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"(\d{2,3})\s*(hz|гц|герц)\b", r"\1hz", text)
    return text


def _add_token(extracted: dict[str, list[str]], key: str, token: str) -> None:
    extracted.setdefault(key, [])
    if token not in extracted[key]:
        extracted[key].append(token)


def _contains(text: str, pattern: str) -> bool:
    return pattern in text


def _contains_any(text: str, patterns: Iterable[str]) -> bool:
    return any(pattern in text for pattern in patterns)


def _extract_refresh_rate(text: str, extracted: dict[str, list[str]]) -> int | None:
    match = re.search(r"\b(60|90|120|144)hz\b", text)
    if not match:
        return None
    value = int(match.group(1))
    _add_token(extracted, "refresh_rate_hz", match.group(0))
    return value


def _extract_color(text: str, extracted: dict[str, list[str]]) -> str | None:
    for color, pattern in COLOR_PATTERNS.items():
        if pattern.search(text):
            _add_token(extracted, "color", color)
            return color
    return None


def _extract_ic_pad(text: str, extracted: dict[str, list[str]]) -> bool | None:
    if re.search(r"\bплощадк\w*\s+под\s+i[сc]\b", text):
        _add_token(extracted, "has_ic_pad", "площадка под ic")
        return True
    if re.search(r"\bic\s*pad\b", text):
        _add_token(extracted, "has_ic_pad", "ic pad")
        return True
    return None


def _extract_binding_no_solder(text: str, extracted: dict[str, list[str]]) -> bool | None:
    if re.search(r"\bпривяз\w*\s+без\s+пайк\w*\b", text):
        _add_token(extracted, "has_binding_no_solder", "привязка без пайки")
        return True
    if "без пайк" in text and "jcid" in text:
        _add_token(extracted, "has_binding_no_solder", "без пайки (jcid)")
        return True
    return None


def _extract_manufacturer(text: str, extracted: dict[str, list[str]]) -> str | None:
    for name, pattern in MANUFACTURER_PATTERNS:
        if pattern.search(text):
            _add_token(extracted, "manufacturer", name.lower())
            return name
    return None


def _extract_matrix_tags(text: str, extracted: dict[str, list[str]]) -> list[str]:
    tags = []
    for tag, pattern in MATRIX_TAG_PATTERNS.items():
        if pattern.search(text):
            tags.append(tag)
            _add_token(extracted, "matrix_tags", tag.lower())
    return tags


def _extract_quality(
    text: str,
    extracted: dict[str, list[str]],
    manufacturer: str | None = None,
) -> ScreenQualityGrade:
    if re.search(
        r"\b(change glass|replaced glass|замен\w*(?:\s+\w+){0,3}\s+стекл\w*|перекле\w*)\b",
        text,
    ):
        _add_token(extracted, "screen_quality_grade", "change glass")
        return ScreenQualityGrade.ORIGINAL_REFURB

    if re.search(r"\bor100\b", text):
        _add_token(extracted, "screen_quality_grade", "or100")
        return ScreenQualityGrade.OR100

    if re.search(r"\b100%?\s*or\b|\bor\s*100%?(?:\b|\s|$)|\b100%or\b", text):
        _add_token(extracted, "screen_quality_grade", "100% or")
        return ScreenQualityGrade.OR100

    if re.search(r"\borlcd\b", text):
        _add_token(extracted, "screen_quality_grade", "or lcd")
        return ScreenQualityGrade.OR

    if manufacturer not in {"GX", "GX ORIG"} and re.search(r"\bgx\b", text):
        _add_token(extracted, "screen_quality_grade", "gx")
        return ScreenQualityGrade.GX

    if manufacturer != "GX ORIG" and re.search(r"\borig(?:inal)?\b|оригинал", text):
        _add_token(extracted, "screen_quality_grade", "original")
        return ScreenQualityGrade.ORIGINAL

    if re.search(r"\boem\b", text):
        _add_token(extracted, "screen_quality_grade", "oem")
        return ScreenQualityGrade.OEM

    if re.search(r"\bпремиум\b", text):
        _add_token(extracted, "screen_quality_grade", "premium")
        return ScreenQualityGrade.PREMIUM

    if re.search(r"\baaa\b", text):
        _add_token(extracted, "screen_quality_grade", "aaa")
        return ScreenQualityGrade.AAA

    if re.search(r"\bhq\b", text):
        _add_token(extracted, "screen_quality_grade", "hq")
        return ScreenQualityGrade.HQ

    if re.search(r"\b(1[-\s]?я|1)\s*категор", text):
        _add_token(extracted, "screen_quality_grade", "1 категория")
        return ScreenQualityGrade.FIRST_CLASS

    if re.search(r"\bor\b", text):
        _add_token(extracted, "screen_quality_grade", "or")
        return ScreenQualityGrade.OR

    if _contains_any(text, ("copy", "копия")):
        if _contains_any(text, ("high", "hi", "premium", "aaa", "hq")):
            _add_token(extracted, "screen_quality_grade", "copy high")
            return ScreenQualityGrade.COPY_HIGH
        if _contains_any(text, ("low", "cheap", "эконом")):
            _add_token(extracted, "screen_quality_grade", "copy low")
            return ScreenQualityGrade.COPY_LOW
        _add_token(extracted, "screen_quality_grade", "copy")
        return ScreenQualityGrade.COPY_MEDIUM

    return ScreenQualityGrade.UNKNOWN


def _extract_construction(text: str, extracted: dict[str, list[str]]) -> ScreenConstruction:
    if _contains(text, "soft oled"):
        _add_token(extracted, "screen_construction", "soft oled")
        return ScreenConstruction.SOFT_OLED
    if _contains(text, "hard oled"):
        _add_token(extracted, "screen_construction", "hard oled")
        return ScreenConstruction.HARD_OLED
    if _contains_any(text, ("cof",)):
        _add_token(extracted, "screen_construction", "cof")
        return ScreenConstruction.COF
    if _contains_any(text, ("cog",)):
        _add_token(extracted, "screen_construction", "cog")
        return ScreenConstruction.COG
    if _contains_any(text, ("in-cell", "incell", "in cell")):
        _add_token(extracted, "screen_construction", "in-cell")
        return ScreenConstruction.INCELL
    if _contains_any(text, ("on-cell", "oncell", "on cell")):
        _add_token(extracted, "screen_construction", "on-cell")
        return ScreenConstruction.ONCELL
    return ScreenConstruction.UNKNOWN


def _extract_matrix_type(text: str, extracted: dict[str, list[str]]) -> ScreenMatrixType:
    if _contains(text, "ltpo"):
        _add_token(extracted, "screen_matrix_type", "ltpo")
        return ScreenMatrixType.LTPO_AMOLED
    if _contains(text, "amoled"):
        _add_token(extracted, "screen_matrix_type", "amoled")
        return ScreenMatrixType.AMOLED
    if _contains(text, "oled"):
        _add_token(extracted, "screen_matrix_type", "oled")
        return ScreenMatrixType.OLED
    if _contains(text, "ltps"):
        _add_token(extracted, "screen_matrix_type", "ltps")
        return ScreenMatrixType.LTPS_LCD
    if _contains(text, "ips"):
        _add_token(extracted, "screen_matrix_type", "ips")
        return ScreenMatrixType.LCD_IPS
    if _contains(text, "tft"):
        _add_token(extracted, "screen_matrix_type", "tft")
        return ScreenMatrixType.LCD_TFT
    if _contains(text, "lcd"):
        _add_token(extracted, "screen_matrix_type", "lcd")
        return ScreenMatrixType.LCD_IPS
    return ScreenMatrixType.UNKNOWN


def _extract_screen_kit(
    text: str, extracted: dict[str, list[str]]
) -> tuple[ScreenKit, bool | None]:
    has_frame: bool | None = None
    if re.search(r"\bбез\s*рамк\w*\b", text):
        has_frame = False
        _add_token(extracted, "screen_kit", "без рамки")

    if re.search(r"\b(с\s+рамк\w*|в\s+рамк\w*|рамк\w*\s+креплен\w*|рамк\w*)\b", text):
        if not re.search(r"\bбез\s*рамк\w*\b", text):
            has_frame = True
            _add_token(extracted, "screen_kit", "рамка")

    has_touch = False
    if re.search(r"\b(тачскрин\w*|сенсор\w*|touch|digitizer)\b", text):
        if not re.search(r"\bбез\s+(тачскрин\w*|сенсор\w*)\b", text):
            has_touch = True
            _add_token(extracted, "screen_kit", "тачскрин")

    has_no_touch = bool(re.search(r"\bбез\s+(тачскрин\w*|сенсор\w*)\b", text))
    if has_no_touch:
        _add_token(extracted, "screen_kit", "без тачскрина")

    if has_frame is True:
        if has_touch:
            return ScreenKit.DISPLAY_TOUCH_FRAME, True
        return ScreenKit.DISPLAY_WITH_FRAME, True
    if has_no_touch:
        return ScreenKit.DISPLAY_ONLY, None
    if has_touch:
        if has_frame is None:
            has_frame = False
            _add_token(extracted, "screen_kit", "тачскрин без рамки")
        return ScreenKit.DISPLAY_WITH_TOUCH, has_frame
    return ScreenKit.UNKNOWN, has_frame


def _derive_has_touch(screen_kit: ScreenKit) -> bool | None:
    if screen_kit in {ScreenKit.DISPLAY_WITH_TOUCH, ScreenKit.DISPLAY_TOUCH_FRAME}:
        return True
    if screen_kit in {ScreenKit.DISPLAY_ONLY, ScreenKit.DISPLAY_WITH_FRAME}:
        return False
    return None


def _extract_backlight(text: str, extracted: dict[str, list[str]]) -> Backlight:
    if re.search(r"\bбез\s+подсветк\w*\b", text):
        _add_token(extracted, "backlight", "без подсветки")
        return Backlight.NO_BACKLIGHT
    if re.search(r"\bярк\w*\s+подсветк\w*\b", text):
        _add_token(extracted, "backlight", "яркая подсветка")
        return Backlight.BRIGHT_BACKLIGHT
    if re.search(r"\bс\s+подсветк\w*\b", text):
        _add_token(extracted, "backlight", "с подсветкой")
        return Backlight.WITH_BACKLIGHT
    return Backlight.UNKNOWN


def _extract_oleophobic(text: str, extracted: dict[str, list[str]]) -> bool | None:
    if "олеофоб" in text:
        _add_token(extracted, "oleophobic", "олеофоб")
        return True
    return None


def _collect_forbidden_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for phrase in FORBIDDEN_MATRIX_PHRASES:
        if phrase in text:
            tokens.append(phrase)
    for match in RESOLUTION_RE.findall(text):
        tokens.append(match)
    return tokens


def _collect_unknown_tokens(text: str, extracted: dict[str, list[str]]) -> list[str]:
    tokens = re.findall(r"[a-zа-я0-9]+", text)
    known = set(NOTE_STOPWORDS)
    for values in extracted.values():
        for value in values:
            for token in re.findall(r"[a-zа-я0-9]+", value.lower()):
                known.add(token)
    leftovers = []
    for token in tokens:
        if token.isdigit() or token in known:
            continue
        if token not in leftovers:
            leftovers.append(token)
    return leftovers


def _parse_status(result: DisplayParseResult) -> str:
    main_fields = [
        result.screen_matrix_type,
        result.screen_kit,
        result.backlight,
        result.screen_construction,
        result.screen_quality_grade,
    ]
    known_main = sum(1 for value in main_fields if value != value.__class__.UNKNOWN)
    has_any = known_main > 0 or any(
        value is not None
        for value in (
            result.refresh_rate_hz,
            result.oleophobic,
            result.color,
            result.has_frame,
            result.has_ic_pad,
            result.has_binding_no_solder,
        )
    )
    if not has_any:
        return PARSE_STATUS_FAILED
    if known_main == len(main_fields):
        return PARSE_STATUS_OK
    return PARSE_STATUS_PARTIAL


def _normalize_enum_value(value: Any, enum_cls: type[StrEnum]) -> StrEnum | None:
    if value is None:
        return None
    if isinstance(value, enum_cls):
        return value
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        for item in enum_cls:
            if raw == item.value:
                return item
        # allow RU labels in LLM output
        ru_map = None
        if enum_cls is ScreenMatrixType:
            ru_map = SCREEN_MATRIX_TYPE_RU
        elif enum_cls is ScreenKit:
            ru_map = SCREEN_KIT_RU
        elif enum_cls is Backlight:
            ru_map = BACKLIGHT_RU
        elif enum_cls is ScreenConstruction:
            ru_map = SCREEN_CONSTRUCTION_RU
        elif enum_cls is ScreenQualityGrade:
            ru_map = SCREEN_QUALITY_RU
        if ru_map:
            for key, label in ru_map.items():
                if raw == label:
                    return key
    return None


def _apply_llm_suggestions(result: DisplayParseResult, llm_attrs: dict[str, Any]) -> None:
    def _set_enum(
        field_name: str,
        enum_cls: type[StrEnum],
    ) -> None:
        raw = llm_attrs.get(field_name)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return
        if isinstance(raw, str):
            for token in FORBIDDEN_MATRIX_PHRASES:
                if token in raw.lower() and token not in result.notes_raw_tokens:
                    result.notes_raw_tokens.append(token)
                    result.reasons.append("matrix_contains_forbidden_tokens")
        value = _normalize_enum_value(raw, enum_cls)
        if value is None:
            result.reasons.append(f"invalid_enum_value:{field_name}")
            return
        if getattr(result, field_name) == enum_cls.UNKNOWN:
            setattr(result, field_name, value)

    _set_enum("screen_matrix_type", ScreenMatrixType)
    _set_enum("screen_kit", ScreenKit)
    _set_enum("backlight", Backlight)
    _set_enum("screen_construction", ScreenConstruction)
    _set_enum("screen_quality_grade", ScreenQualityGrade)

    if result.refresh_rate_hz is None:
        raw = llm_attrs.get("refresh_rate_hz")
        try:
            value = int(str(raw))
        except (TypeError, ValueError):
            value = None
        if value in {60, 90, 120, 144}:
            result.refresh_rate_hz = value
        elif raw is not None:
            result.reasons.append("invalid_refresh_rate")

    if result.oleophobic is None:
        if isinstance(llm_attrs.get("oleophobic"), bool):
            result.oleophobic = llm_attrs.get("oleophobic")

    if result.has_frame is None:
        if isinstance(llm_attrs.get("has_frame"), bool):
            result.has_frame = llm_attrs.get("has_frame")

    if result.color is None:
        color = llm_attrs.get("color")
        if isinstance(color, str) and color.strip():
            result.color = color.strip()


def parse_display_attributes(
    raw_name: str,
    *,
    llm_attrs: dict[str, Any] | None = None,
    llm_output: str | None = None,
) -> DisplayParseResult:
    normalized = normalize_display_name(raw_name)
    extracted_tokens: dict[str, list[str]] = {}
    notes_raw_tokens = _collect_forbidden_tokens(normalized)

    result = DisplayParseResult(
        notes_raw_tokens=notes_raw_tokens,
        extracted_tokens=extracted_tokens,
        llm_output=llm_output,
    )

    screen_kit, has_frame = _extract_screen_kit(normalized, extracted_tokens)
    result.screen_kit = screen_kit
    if has_frame is True:
        result.has_frame = True
    elif has_frame is False:
        result.has_frame = False
    result.has_touch = _derive_has_touch(screen_kit)

    result.backlight = _extract_backlight(normalized, extracted_tokens)
    result.refresh_rate_hz = _extract_refresh_rate(normalized, extracted_tokens)
    result.oleophobic = _extract_oleophobic(normalized, extracted_tokens)
    result.manufacturer = _extract_manufacturer(normalized, extracted_tokens)
    result.screen_quality_grade = _extract_quality(
        normalized, extracted_tokens, result.manufacturer
    )
    result.screen_construction = _extract_construction(normalized, extracted_tokens)
    result.screen_matrix_type = _extract_matrix_type(normalized, extracted_tokens)
    result.color = _extract_color(normalized, extracted_tokens)
    result.has_ic_pad = _extract_ic_pad(normalized, extracted_tokens)
    result.has_binding_no_solder = _extract_binding_no_solder(normalized, extracted_tokens)
    result.matrix_tags = _extract_matrix_tags(normalized, extracted_tokens)

    if result.screen_construction in {ScreenConstruction.HARD_OLED, ScreenConstruction.SOFT_OLED}:
        result.screen_matrix_type = ScreenMatrixType.OLED
        _add_token(extracted_tokens, "screen_matrix_type", "oled (from construction)")

    if llm_attrs:
        _apply_llm_suggestions(result, llm_attrs)

    if result.screen_kit in {ScreenKit.DISPLAY_WITH_FRAME, ScreenKit.DISPLAY_TOUCH_FRAME}:
        result.has_frame = True
    result.has_touch = _derive_has_touch(result.screen_kit)

    unknown_tokens = _collect_unknown_tokens(normalized, extracted_tokens)
    for token in unknown_tokens:
        if token not in result.notes_raw_tokens:
            result.notes_raw_tokens.append(token)

    # Validate enum values (LLM may inject garbage)
    for field_name, enum_cls in (
        ("screen_matrix_type", ScreenMatrixType),
        ("screen_kit", ScreenKit),
        ("backlight", Backlight),
        ("screen_construction", ScreenConstruction),
        ("screen_quality_grade", ScreenQualityGrade),
    ):
        value = getattr(result, field_name)
        if not isinstance(value, enum_cls):
            result.reasons.append(f"invalid_enum_value:{field_name}")
            setattr(result, field_name, enum_cls.UNKNOWN)

    if result.refresh_rate_hz not in {None, 60, 90, 120, 144}:
        result.reasons.append("invalid_refresh_rate")
        result.refresh_rate_hz = None

    forbidden_from_value = []
    for phrase in FORBIDDEN_MATRIX_PHRASES:
        if phrase in str(result.screen_matrix_type.value).lower():
            forbidden_from_value.append(phrase)
    if forbidden_from_value:
        result.reasons.append("matrix_contains_forbidden_tokens")
        result.screen_matrix_type = ScreenMatrixType.UNKNOWN
        for token in forbidden_from_value:
            if token not in result.notes_raw_tokens:
                result.notes_raw_tokens.append(token)

    result.parse_status = _parse_status(result)
    return result
