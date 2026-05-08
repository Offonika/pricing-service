from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from urllib.parse import urlparse

import httpx
from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.models import (
    Competitor,
    CompetitorFtpRecord,
    CompetitorItem,
    CompetitorItemParseStatus,
    CompetitorItemSnapshot,
    CompetitorPrice,
    PhoneModel,
    Product,
    ProductMatch,
    ProductMatchOverride,
    ProductPhoneModel,
)
from app.services.competitor_category import (
    CategoryClassifier,
    canonicalize_category,
    category_group,
)
from app.services.display_normalization import normalize_display_quality, normalize_display_type
from app.services.phone_model_canonicalization import PhoneModelCanonicalizer
from app.services.prompts import get_llm_parse_prompt

logger = logging.getLogger("app.matching.competitor_ftp")


@dataclass
class MatchStats:
    processed: int = 0
    matched: int = 0
    prices_created: int = 0
    matches_created: int = 0
    unmatched: int = 0
    ambiguous: int = 0
    skipped_no_price: int = 0
    skipped_low_conf: int = 0

    def as_dict(self) -> dict:
        return {
            "processed": self.processed,
            "matched": self.matched,
            "prices_created": self.prices_created,
            "matches_created": self.matches_created,
            "unmatched": self.unmatched,
            "ambiguous": self.ambiguous,
            "skipped_no_price": self.skipped_no_price,
            "skipped_low_conf": self.skipped_low_conf,
        }


def _normalize_sku(value: str | None) -> str:
    if not value:
        return ""
    s = str(value).strip().lower()
    s = s.replace("–", "-").replace("—", "-")
    s = re.sub(r"[\s\t\n\r]+", "", s)
    return s


def _load_products_by_article(session: Session) -> dict[str, list[Product]]:
    products: dict[str, list[Product]] = {}
    for product in session.execute(select(Product)).scalars():
        key = _normalize_sku(product.article)
        if not key:
            continue
        products.setdefault(key, []).append(product)
    return products


BRAND_SYNONYMS = {
    "iphone": "apple",
    "apple": "apple",
    "ipad": "apple",
    "ipod": "apple",
    "watch": "apple",
    "samsung": "samsung",
    "galaxy": "samsung",
    "xiaomi": "xiaomi",
    "poco": "xiaomi",
    "mi": "xiaomi",
    "redmi": "xiaomi",
    "mipad": "xiaomi",
    "realme": "realme",
    "vivo": "vivo",
    "oppo": "oppo",
    "oneplus": "oneplus",
    "huawei": "huawei",
    "honor": "honor",
    "nokia": "nokia",
    "sony": "sony",
    "zte": "zte",
    "lenovo": "lenovo",
    "motorola": "motorola",
    "meizu": "meizu",
    "alcatel": "alcatel",
    "blackview": "blackview",
    "infinix": "infinix",
    "tecno": "tecno",
    "itel": "itel",
    "doogee": "doogee",
    "oukitel": "oukitel",
    "ulefone": "ulefone",
    "cubot": "cubot",
    "tcl": "tcl",
    "google": "google",
    "pixel": "google",
    "wiko": "wiko",
    "nothing": "nothing",
    "umidigi": "umidigi",
    "fly": "fly",
    "philips": "philips",
    "leeco": "leeco",
}

STOP_TOKENS = {
    "в",
    "с",
    "сборе",
    "тачскрином",
    "тачскрин",
    "черный",
    "черный-",
    "черная",
    "чёрный",
    "чёрная",
    "белый",
    "белая",
    "золотистый",
    "серый",
    "синий",
    "голубой",
    "красный",
    "розовый",
    "фиолетовый",
    "green",
    "black",
    "white",
    "gold",
    "blue",
    "red",
    "pink",
    "оптима",
    "копия",
    "ориг",
    "оригинал",
    "premium",
    "orig",
    "or",
    "sp",
    "ref",
    "oem",
    "aaa",
    "incell",
    "oled",
    "lcd",
    "frame",
    "без",
    "рамки",
    "разъем",
    "разъём",
    "коннектор",
    "аккумулятор",
    "акб",
    "для",
    "wi",
    "fi",
    "wifi",
}

VARIANT_TOKENS = {
    "pro",
    "max",
    "promax",
    "pro max",
    "plus",
    "mini",
    "ultra",
    "fe",
    "lite",
    "se",
    "edge",
    "xl",
    "note",
    "neo",
    "youth",
    "air",
}
NETWORK_TOKENS = {"4g", "5g"}

APPLE_A_CODE_RE = re.compile(r"a\d{4,5}", re.IGNORECASE)
YEAR_RE = re.compile(r"(20\d{2})")

MODEL_PARSE_KEYWORDS = (
    "дисплей",
    "экран",
    "тачскрин",
    "lcd",
    "oled",
    "аккумулятор",
    "акб",
    "battery",
    "шлейф",
    "flex",
    "камера",
    "camera",
    "рамка дисплея",
    "крышка",
    "корпус",
    "разъем",
    "разъём",
    "коннектор",
    "connector",
    "port",
)


@dataclass
class ParsedItem:
    model: str
    codes: list[str]


@dataclass
class ParsedModel:
    brand: str | None
    model: str | None
    variant: str | None
    confidence: float
    reason: str
    ambiguous: bool = False
    models: list[str] | None = None
    items: list[ParsedItem] | None = None
    llm_raw_json: str | None = None
    llm_model_name: str | None = None
    parse_origin: str | None = None


class LlmParseClient:
    """Простой клиент к совместимому с OpenAI chat completions API для парсинга модели."""

    def __init__(self, base_url: str, model: str, timeout: float = 90.0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = httpx.Timeout(timeout, connect=10.0)
        host = (urlparse(self.base_url).hostname or "").lower()
        self.trust_env = host not in {"localhost", "127.0.0.1", "::1"}

    def parse(self, source: str, sku: str, name: str) -> ParsedModel | None:
        system_prompt = get_llm_parse_prompt()
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Source: {source} SKU: {sku}\nName: {name}"},
            ],
            "temperature": 0.0,
            "max_tokens": 200,
        }
        resp = httpx.post(
            f"{self.base_url}/v1/chat/completions",
            json=payload,
            timeout=self.timeout,
            trust_env=self.trust_env,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        parsed = _relaxed_json_loads(content)
        if parsed is None:
            return None
        parsed_model = _parse_llm_response(parsed)
        if not parsed_model:
            return None
        parsed_model.confidence = 0.9
        parsed_model.reason = "llm"
        parsed_model.ambiguous = False
        parsed_model.llm_raw_json = json.dumps(parsed, ensure_ascii=False, sort_keys=True)
        parsed_model.llm_model_name = self.model
        parsed_model.parse_origin = "llm"
        return parsed_model


def _relaxed_json_loads(content: str) -> dict | None:
    """LLM иногда возвращает JSON в code fence или с префиксом/суффиксом текста."""
    if not content:
        return None
    text = content.strip()

    def _loads_with_repairs(candidate: str) -> dict | None:
        s = candidate.strip()
        for _ in range(2):
            try:
                parsed = json.loads(s)
                return parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                # частые артефакты локальных LLM
                s = re.sub(r"([\]\}])\s*\(\s*\)", r"\1", s)
                s = re.sub(r",\s*([}\]])", r"\1", s)
        return None

    parsed = _loads_with_repairs(text)
    if parsed is not None:
        return parsed

    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.IGNORECASE | re.DOTALL)
    if fence:
        candidate = fence.group(1).strip()
        parsed = _loads_with_repairs(candidate)
        return parsed

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    candidate = text[start : end + 1].strip()
    return _loads_with_repairs(candidate)


_LLM_BRAND_WHITELIST = {
    "acer",
    "alcatel",
    "apple",
    "asus",
    "blackview",
    "cubot",
    "dtc",
    "doogee",
    "explay",
    "fly",
    "generic",
    "google",
    "honor",
    "htc",
    "huawei",
    "infinix",
    "itel",
    "lenovo",
    "lg",
    "meizu",
    "nokia",
    "nothing",
    "oneplus",
    "oppo",
    "oukitel",
    "philips",
    "realme",
    "samsung",
    "smartbuy",
    "sony",
    "tcl",
    "tecno",
    "tp-cl",
    "unknown",
    "ulefone",
    "vivo",
    "wiko",
    "xiaomi",
    "umidigi",
    "leeco",
    "zte",
}

_LLM_FORBIDDEN_MODEL_TOKENS = {
    # product/type words
    "display",
    "screen",
    "lcd",
    "touchscreen",
    "touch",
    "digitizer",
    "glass",
    "module",
    "assembly",
    "with",
    "for",
    "replacement",
    "back",
    "дисплей",
    "экран",
    "тачскрин",
    "стекло",
    "модуль",
    "шлейф",
    "рамка",
    "в",
    "рамке",
    "без",
    "рамки",
    "сборе",
    "с",
    "тачскрином",
    # quality/class
    "or",
    "or100",
    "orig",
    "original",
    "oem",
    "copy",
    "premium",
    "optima",
    "aaa",
    "fog",
    "ref",
    "ор",
    "ориг",
    "оригинал",
    "копия",
    "премиум",
    "оптима",
    # display tech
    "oled",
    "amoled",
    "super",
    "dynamic",
    "tft",
    "ips",
    "ltps",
    "ltpo",
    "incell",
    "in-cell",
    "on-cell",
    "cof",
    "cog",
    "hard",
    "soft",
    # colors
    "black",
    "white",
    "red",
    "blue",
    "pink",
    "gold",
    "green",
    "gray",
    "grey",
    "silver",
    "черный",
    "чёрный",
    "белый",
    "красный",
    "синий",
    "розовый",
    "золотистый",
    "серый",
}

PHONE_MODEL_NAME_MAX_LEN = 150
PHONE_MODEL_VARIANT_MAX_LEN = 50
PHONE_MODEL_MAX_TOKENS = 8
PHONE_MODEL_MAX_NUMERIC_TOKENS = 3


def _normalize_llm_brand(value: str | None) -> str | None:
    if not value:
        return None
    brand = str(value).strip().lower()
    if brand == "unknown":
        return None
    if brand in _LLM_BRAND_WHITELIST:
        return brand
    return None


def _sanitize_llm_model(value: str) -> str:
    lowered = value.lower()
    lowered = re.sub(r"[\"'()\[\]{}]", " ", lowered)
    lowered = re.sub(r"[^a-z0-9а-яё.+\\-\\s/]", " ", lowered)
    lowered = lowered.replace("/", " / ")
    tokens = re.findall(r"[a-z0-9а-яё.+-]+|/", lowered)
    cleaned: list[str] = []
    for tok in tokens:
        if tok == "/":
            cleaned.append(tok)
            continue
        if tok in _LLM_FORBIDDEN_MODEL_TOKENS:
            continue
        cleaned.append(tok)
    text = " ".join(cleaned)
    text = re.sub(r"\s*/\s*", "/", text)
    text = " ".join(text.split())
    return text


def _normalize_llm_model(value: str | None) -> str | None:
    if not value:
        return None
    model = _sanitize_llm_model(str(value).strip())
    model = _strip_device_code_tokens(model)
    if model and not _is_reasonable_phone_model_name(model):
        return None
    return model or None


def _strip_device_code_tokens(model: str) -> str:
    tokens = model.split()
    if not tokens:
        return model
    # Не выкидываем короткие letter+digits токены вроде "bv5800"/"a105" (это часто реальная модель).
    # Убираем только "явные" device-codes.
    code_re = re.compile(r"^(?:sm-[a-z0-9]{3,8}|a\\d{4,5}|(?=.*[a-z])(?=.*\\d)[a-z0-9]{10,})$")
    filtered = [tok for tok in tokens if not code_re.fullmatch(tok)]
    return " ".join(filtered)


def _expand_slash_models(model: str) -> list[str]:
    if "/" not in model:
        return [model]
    parts = [part.strip() for part in model.split("/") if part.strip()]
    if len(parts) <= 1:
        return [model]
    left_tokens = parts[0].split()
    shared_prefix: list[str] = []
    for tok in left_tokens:
        if re.search(r"\d", tok):
            break
        shared_prefix.append(tok)
    expanded: list[str] = [parts[0]]
    for part in parts[1:]:
        if not left_tokens:
            expanded.append(part)
            continue
        part_tokens = part.split()
        if shared_prefix and part_tokens[: len(shared_prefix)] != shared_prefix:
            prefix_tokens = shared_prefix[:]
        elif part_tokens and part_tokens[0] in VARIANT_TOKENS:
            prefix_tokens = left_tokens[:]
        elif len(left_tokens) > 1:
            prefix_tokens = left_tokens[:-1]
        else:
            prefix_tokens = left_tokens[:]
        if prefix_tokens and part_tokens and part_tokens[0] == prefix_tokens[-1]:
            prefix_tokens = prefix_tokens[:-1]
        if not prefix_tokens:
            expanded.append(part)
            continue
        prefix = " ".join(prefix_tokens)
        if part.startswith(prefix):
            expanded.append(part)
            continue
        if any(tok in part_tokens for tok in prefix_tokens):
            expanded.append(part)
            continue
        expanded.append(f"{prefix} {part}".strip())
    result: list[str] = []
    seen: set[str] = set()
    for item in expanded:
        normalized = " ".join(item.split())
        if normalized and normalized not in seen:
            result.append(normalized)
            seen.add(normalized)
    return result or [model]


def _normalize_llm_codes(values: Sequence[object] | None) -> list[str]:
    if not values:
        return []
    seen = set()
    result: list[str] = []
    code_re = re.compile(
        r"^(?:SM-[A-Z0-9]{3,6}|[A-Z]\d{3,5}[A-Z]?|[A-Z]{2,4}\d{3,5}[A-Z]?|[A-Z]{3,4}\d{1,2}[A-Z0-9]?|[A-Z]{2,4}-[A-Z]{1,2}\d{1,2}[A-Z]?|M\d{3,4}[A-Z]\d{0,2}[A-Z]?)$"
    )
    for raw in values:
        if not isinstance(raw, str):
            continue
        code = raw.strip().upper()
        if not code:
            continue
        if not code_re.fullmatch(code):
            continue
        if code not in seen:
            result.append(code)
            seen.add(code)
    return result


def _parse_llm_response(payload: dict) -> ParsedModel | None:
    brand = _normalize_llm_brand(payload.get("brand"))

    items_payload = payload.get("items")
    items: list[ParsedItem] | None = None
    models: list[str] | None = None

    if isinstance(items_payload, list):
        items = []
        models = []
        for entry in items_payload:
            if not isinstance(entry, dict):
                continue
            model = _normalize_llm_model(entry.get("model"))
            if not model:
                continue
            codes = _normalize_llm_codes(entry.get("codes"))
            for expanded in _expand_slash_models(model):
                items.append(ParsedItem(model=expanded, codes=codes))
                if expanded not in models:
                    models.append(expanded)

    if items is not None:
        if not brand and not items:
            return None
        return ParsedModel(
            brand=brand,
            model=models[0] if models else None,
            models=models or None,
            variant=None,
            confidence=0.0,
            reason="",
            items=items,
        )

    legacy_models = payload.get("models")
    if legacy_models and isinstance(legacy_models, list):
        expanded_models: list[str] = []
        for raw in legacy_models:
            normalized = _normalize_llm_model(raw if isinstance(raw, str) else None)
            if not normalized:
                continue
            expanded_models.extend(_expand_slash_models(normalized))
        legacy_models = expanded_models or None
    legacy_model = payload.get("model")
    legacy_variant = payload.get("variant")
    normalized_legacy = _normalize_llm_model(
        legacy_model if isinstance(legacy_model, str) else None
    )
    if normalized_legacy:
        expanded_legacy = _expand_slash_models(normalized_legacy)
        legacy_model = expanded_legacy[0] if expanded_legacy else legacy_model
        if expanded_legacy and (not legacy_models or legacy_models == [legacy_model]):
            legacy_models = expanded_legacy
    if not brand and not legacy_model and not legacy_models:
        return None
    return ParsedModel(
        brand=brand,
        model=legacy_model,
        models=legacy_models,
        variant=legacy_variant,
        confidence=0.0,
        reason="",
    )


def _normalize_model_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


LLM_MAX_MODELS = 4
LLM_BLOCKED_MODEL_SUBSTRINGS = {
    "universal",
    "universally compatible",
    "adapter",
    "usb флеш",
    "smartbuy",
    "flash",
}
LLM_BLOCKED_WEARABLE_SUBSTRINGS = {
    "watch",
    "smart watch",
    "apple watch",
    "galaxy watch",
    "gear",
    "band",
    "smartband",
}


def _brands_in_text(value: str | None) -> set[str]:
    if not value:
        return set()
    lowered = f" {value.lower()} "
    found: set[str] = set()
    for token, canonical in BRAND_SYNONYMS.items():
        if re.search(rf"\b{re.escape(token)}\b", lowered):
            found.add(canonical)
    return found


def _infer_brand_from_model_text(value: str | None) -> str | None:
    brands = _brands_in_text(value)
    if len(brands) == 1:
        return next(iter(brands))
    return None


def _sanitize_llm_models(
    parsed: ParsedModel,
    *,
    item_name: str | None,
) -> tuple[ParsedModel | None, str]:
    brand = (parsed.brand or "").strip().lower()
    if brand in {"", "generic", "unknown"}:
        return None, "llm_blocked_generic_brand"

    raw_text = " ".join(part for part in (item_name, parsed.variant) if part).lower()
    if any(token in raw_text for token in LLM_BLOCKED_MODEL_SUBSTRINGS):
        return None, "llm_blocked_non_device_title"
    if any(token in raw_text for token in LLM_BLOCKED_WEARABLE_SUBSTRINGS):
        return None, "llm_blocked_wearable"

    clean_models: list[str] = []
    clean_items: list[ParsedItem] = []
    source_models = parsed.items or [
        ParsedItem(model=m, codes=[]) for m in (parsed.models or []) if m
    ]
    if not source_models and parsed.model:
        source_models = [ParsedItem(model=parsed.model, codes=[])]

    seen: set[str] = set()
    for entry in source_models:
        model = entry.model
        if not model or not _is_reasonable_phone_model_name(model):
            continue
        if any(token in model for token in LLM_BLOCKED_MODEL_SUBSTRINGS):
            continue
        if any(token in model for token in LLM_BLOCKED_WEARABLE_SUBSTRINGS):
            continue
        model_brands = _brands_in_text(model)
        if len(model_brands) > 1:
            continue
        inferred_brand = _infer_brand_from_model_text(model)
        if inferred_brand and inferred_brand != brand:
            continue
        if _normalize_model_key(model) in seen:
            continue
        seen.add(_normalize_model_key(model))
        clean_models.append(model)
        clean_items.append(ParsedItem(model=model, codes=entry.codes))

    if not clean_models:
        return None, "llm_no_valid_models"
    if len(clean_models) > LLM_MAX_MODELS:
        return None, "llm_multi_model_overflow"

    parsed.brand = brand
    parsed.models = clean_models
    parsed.items = clean_items if parsed.items is not None else None
    parsed.model = clean_models[0]
    return parsed, "llm_sanitized"


def _is_reasonable_phone_model_name(value: str | None) -> bool:
    if not value:
        return False
    normalized = " ".join(str(value).split()).strip()
    if not normalized or len(normalized) > PHONE_MODEL_NAME_MAX_LEN:
        return False
    tokens = normalized.split()
    if len(tokens) > PHONE_MODEL_MAX_TOKENS:
        return False
    numeric_tokens = sum(1 for tok in tokens if re.search(r"\d", tok))
    if numeric_tokens > PHONE_MODEL_MAX_NUMERIC_TOKENS:
        return False
    return True


def _should_canonicalize_competitor_model(parsed: ParsedModel | None) -> bool:
    if not parsed or not parsed.model or parsed.ambiguous:
        return False
    if parsed.brand in {None, "generic"}:
        return False
    reason = (parsed.reason or "").lower()
    if "ambiguous" in reason or reason == "no keyword":
        return False
    return _is_reasonable_phone_model_name(parsed.model)


def _split_mixed_token(token: str) -> list[str]:
    lowered = token.lower()
    if re.fullmatch(r"[45]g", lowered):
        return [lowered]
    if re.fullmatch(r"\d{1,2}[a-z]", lowered):
        return [lowered]
    if re.fullmatch(r"[a-z]\d{1,4}[a-z]?", lowered):
        return [lowered]
    return re.findall(r"[a-z]+|\d+", lowered)


def _clean_tokens(raw_tokens: list[str]) -> list[str]:
    cleaned: list[str] = []
    for tok in raw_tokens:
        if not tok:
            continue
        if tok in STOP_TOKENS:
            continue
        parts = _split_mixed_token(tok)
        for part in parts:
            if not part or part in STOP_TOKENS:
                continue
            cleaned.append(part)
    return cleaned


def _detect_brand(tokens: list[str]) -> tuple[str | None, int | None]:
    for idx, tok in enumerate(tokens):
        brand = BRAND_SYNONYMS.get(tok)
        if brand:
            return brand, idx
    return None, None


def _parse_apple(
    tokens: list[str],
    a_code: str | None = None,
    device_prefix: str | None = None,
) -> ParsedModel:
    tokens = [t for t in tokens if not YEAR_RE.fullmatch(t)]
    if device_prefix == "ipad":
        parsed_ipad = _parse_apple_ipad(tokens, a_code=a_code)
        if parsed_ipad is not None:
            return parsed_ipad
    tokens = [t for t in tokens if t not in {"iphone", "ipad", "ipod", "watch"}]
    filtered_tokens: list[str] = []
    first_numeric_seen = False
    for tok in tokens:
        if re.fullmatch(r"\d", tok):
            if first_numeric_seen:
                continue
            first_numeric_seen = True
        filtered_tokens.append(tok)
    tokens = filtered_tokens
    if not tokens:
        return ParsedModel(
            brand="apple",
            model=None,
            variant=None,
            confidence=0.0,
            reason="no tokens",
            ambiguous=True,
        )

    gen: str | None = None
    variant: str | None = None
    for idx, tok in enumerate(tokens):
        if tok in {"x", "xr", "xs", "xsmax", "xsm"}:
            gen = tok
            if "max" in tok:
                variant = "max"
            elif tok in {"x", "xs"} and idx + 1 < len(tokens) and tokens[idx + 1] == "max":
                variant = "max"
            break
        if re.fullmatch(r"\d{1,2}", tok) and idx + 1 < len(tokens) and tokens[idx + 1] == "e":
            gen = f"{tok}e"
            if idx + 3 < len(tokens) and tokens[idx + 2] == "pro" and tokens[idx + 3] == "max":
                variant = "pro max"
            elif idx + 2 < len(tokens) and tokens[idx + 2] in VARIANT_TOKENS:
                variant = tokens[idx + 2]
            break
        if re.fullmatch(r"\d{1,2}e", tok):
            gen = tok
            if idx + 2 < len(tokens) and tokens[idx + 1] == "pro" and tokens[idx + 2] == "max":
                variant = "pro max"
            elif idx + 1 < len(tokens) and tokens[idx + 1] in VARIANT_TOKENS:
                variant = tokens[idx + 1]
            break
        if re.fullmatch(r"\d{1,2}", tok) or re.fullmatch(r"\d{1,2}s", tok):
            gen = tok
            if idx + 2 < len(tokens) and tokens[idx + 1] == "pro" and tokens[idx + 2] == "max":
                variant = "pro max"
            elif idx + 1 < len(tokens) and tokens[idx + 1] in VARIANT_TOKENS:
                variant = tokens[idx + 1]
            break
        if tok.startswith("se"):
            gen = "se"
            break
    if gen is None:
        return ParsedModel(
            brand="apple",
            model=None,
            variant=None,
            confidence=0.0,
            reason="no generation",
            ambiguous=True,
        )

    generations = {
        part for tok in tokens for part in _split_mixed_token(tok) if re.fullmatch(r"\d{1,2}", part)
    }
    if len(generations) > 1:
        return ParsedModel(
            brand="apple",
            model=None,
            variant=None,
            confidence=0.0,
            reason="multi generations",
            ambiguous=True,
        )

    model_name = f"{gen}".strip()
    if variant:
        model_name = f"{model_name} {variant}"
    if device_prefix:
        model_name = f"{device_prefix} {model_name}".strip()

    confidence = 0.9
    if any(len(tok) > 8 for tok in tokens):
        confidence = 0.6
    return ParsedModel(
        brand="apple",
        model=model_name,
        variant=a_code or variant,
        confidence=confidence,
        reason="apple",
    )


def _parse_apple_ipad(tokens: list[str], a_code: str | None = None) -> ParsedModel | None:
    cleaned = [t for t in tokens if t not in {"ipad", "apple"}]
    if not cleaned:
        return None

    def _extract_ipad_size(parts: list[str], start_idx: int = 0) -> str | None:
        compact_sizes = {
            "97": "9.7",
            "102": "10.2",
            "105": "10.5",
            "109": "10.9",
            "110": "11.0",
            "129": "12.9",
            "130": "13.0",
        }
        valid_sizes = set(compact_sizes.values())
        for idx in range(start_idx, len(parts)):
            token = parts[idx]
            if token in compact_sizes:
                return compact_sizes[token]
            if re.fullmatch(r"\d{1,2}\.\d", token) and token in valid_sizes:
                return token
            if idx + 1 >= len(parts):
                continue
            candidate = f"{token}.{parts[idx + 1]}"
            if candidate in valid_sizes:
                return candidate
        return None

    family: str | None = None
    model: str | None = None

    if "mini" in cleaned:
        family = "mini"
        idx = cleaned.index("mini")
        gen = next((tok for tok in cleaned[idx + 1 :] if re.fullmatch(r"\d{1,2}", tok)), None)
        if gen:
            model = f"ipad {family} {gen}"
    elif "air" in cleaned:
        family = "air"
        idx = cleaned.index("air")
        gen = next((tok for tok in cleaned[idx + 1 :] if re.fullmatch(r"\d{1,2}", tok)), None)
        if gen:
            model = f"ipad {family} {gen}"
        else:
            model = "ipad air"
    elif "pro" in cleaned:
        family = "pro"
        idx = cleaned.index("pro")
        size = _extract_ipad_size(cleaned, idx + 1)
        if size:
            model = f"ipad {family} {size}"
        else:
            model = "ipad pro"
    else:
        gen_idx = next(
            (idx for idx, tok in enumerate(cleaned) if re.fullmatch(r"\d{1,2}[a-z]?", tok)),
            None,
        )
        gen = cleaned[gen_idx] if gen_idx is not None else None
        if gen:
            model = f"ipad {gen}"
            size = _extract_ipad_size(cleaned, gen_idx + 1)
            if size:
                model = f"{model} {size}"

    if not model:
        return None

    return ParsedModel(
        brand="apple",
        model=model,
        variant=a_code,
        confidence=0.9,
        reason="apple_ipad",
    )


def _compose_model(base: str, number: str, variant: str | None) -> str:
    model = f"{base} {number}".strip()
    if variant:
        model = f"{model} {variant}"
    return model.strip()


def _parse_samsung(tokens: list[str]) -> ParsedModel:
    """
    Heuristics for Galaxy S/A/M/Note/Z lines.
    """
    candidates: list[str] = []
    variants: list[str | None] = []
    for idx, tok in enumerate(tokens):
        nxt = tokens[idx + 1] if idx + 1 < len(tokens) else None
        nxt2 = tokens[idx + 2] if idx + 2 < len(tokens) else None
        var = None
        base = None
        number = None
        # Combined tokens like s23, a54, m33
        if re.fullmatch(r"[samj]\d{1,3}", tok):
            base = tok[0]
            number = tok[1:]
        # Note line
        elif tok == "note" and nxt and re.fullmatch(r"\d{1,3}", nxt):
            base = "note"
            number = nxt
        # Z Fold / Flip
        elif tok in {"fold", "flip"} and nxt and re.fullmatch(r"\d{1,2}", nxt):
            base = f"z {tok}"
            number = nxt
        # Separate S/A/M token followed by number
        elif tok in {"s", "a", "m", "j"} and nxt and re.fullmatch(r"\d{1,3}", nxt):
            base = tok
            number = nxt
        if base and number:
            if nxt and nxt in VARIANT_TOKENS:
                var = nxt
            elif nxt2 and nxt2 in VARIANT_TOKENS:
                var = nxt2
            candidates.append(_compose_model(base.upper(), number, var))
            variants.append(var)
    if not candidates:
        return ParsedModel(
            brand="samsung",
            model=None,
            variant=None,
            confidence=0.0,
            reason="no series",
            ambiguous=True,
        )
    # pick first candidate; if more than one distinct -> ambiguous
    uniq = {candidates[0]}
    for cand in candidates[1:]:
        uniq.add(cand)
    if len(uniq) > 1:
        return ParsedModel(
            brand="samsung",
            model=None,
            variant=None,
            confidence=0.0,
            reason="multi candidates",
            ambiguous=True,
        )
    model = candidates[0]
    model_key = _normalize_model_key(model)
    if len(model_key) > 20:
        return ParsedModel(
            brand="samsung",
            model=None,
            variant=None,
            confidence=0.0,
            reason="too long",
            ambiguous=True,
        )
    return ParsedModel(
        brand="samsung", model=model.lower(), variant=variants[0], confidence=0.85, reason="samsung"
    )


def _parse_xiaomi(tokens: list[str]) -> ParsedModel:
    """
    Heuristics for Xiaomi/Redmi/Poco lines, e.g. "redmi note 11 pro", "poco x5 pro".
    """
    family = None
    number = None
    variant = None

    def _apply_network(
        base_number: str, base_variant: str | None, index: int
    ) -> tuple[str, str | None]:
        if index < len(tokens) and tokens[index] in NETWORK_TOKENS:
            network = tokens[index]
            if base_variant:
                return f"{base_number} {base_variant} {network}", None
            return f"{base_number} {network}", None
        return base_number, base_variant

    for idx, tok in enumerate(tokens):
        if tok in {"redmi", "poco", "mi", "xiaomi"}:
            family = tok
            # look ahead for note/x/k/m/number tokens
            if idx + 1 < len(tokens):
                nxt = tokens[idx + 1]
                combined = re.fullmatch(r"([kxmfca])(\d{1,3}[a-z]?)", nxt)
                if combined:
                    number = f"{combined.group(1)}{combined.group(2)}"
                    if idx + 2 < len(tokens) and tokens[idx + 2] in VARIANT_TOKENS:
                        variant = tokens[idx + 2]
                        number, variant = _apply_network(number, variant, idx + 3)
                    else:
                        number, variant = _apply_network(number, variant, idx + 2)
                    continue
                if nxt == "pad" and idx + 2 < len(tokens):
                    suffix = tokens[idx + 2]
                    if re.fullmatch(r"[a-z]?\d{1,3}[a-z]?", suffix):
                        number = f"{nxt} {suffix}"
                        number, variant = _apply_network(number, variant, idx + 3)
                    continue
                if nxt in {"note", "k", "x", "m", "f", "c", "a"}:
                    if idx + 2 < len(tokens) and re.fullmatch(r"\d{1,3}[a-z]?", tokens[idx + 2]):
                        suffix = tokens[idx + 2]
                        if idx + 3 < len(tokens) and re.fullmatch(r"[a-z]", tokens[idx + 3]):
                            suffix = f"{suffix}{tokens[idx + 3]}"
                            if idx + 4 < len(tokens) and tokens[idx + 4] in VARIANT_TOKENS:
                                variant = tokens[idx + 4]
                                number, variant = _apply_network(
                                    f"{nxt} {suffix}", variant, idx + 5
                                )
                                if variant is None:
                                    continue
                            number = f"{nxt} {suffix}"
                            number, variant = _apply_network(number, variant, idx + 4)
                            continue
                        elif idx + 3 < len(tokens) and tokens[idx + 3] in VARIANT_TOKENS:
                            variant = tokens[idx + 3]
                            number, variant = _apply_network(f"{nxt} {suffix}", variant, idx + 4)
                            if variant is None:
                                continue
                        if nxt == "note":
                            number = f"{nxt} {suffix}"
                        else:
                            number = f"{nxt}{suffix}"
                        number, variant = _apply_network(number, variant, idx + 3)
                    continue
                if re.fullmatch(r"\d{1,3}", nxt):
                    suffix = nxt
                    if idx + 2 < len(tokens) and re.fullmatch(r"[a-z]", tokens[idx + 2]):
                        suffix = f"{suffix}{tokens[idx + 2]}"
                        if idx + 3 < len(tokens) and tokens[idx + 3] in VARIANT_TOKENS:
                            variant = tokens[idx + 3]
                            number, variant = _apply_network(suffix, variant, idx + 4)
                            if variant is None:
                                continue
                        number = suffix
                        number, variant = _apply_network(number, variant, idx + 3)
                        continue
                    elif idx + 2 < len(tokens) and tokens[idx + 2] in VARIANT_TOKENS:
                        variant = tokens[idx + 2]
                        number, variant = _apply_network(suffix, variant, idx + 3)
                        if variant is None:
                            continue
                    number = suffix
                    number, variant = _apply_network(number, variant, idx + 2)
            break
    if not family and tokens:
        first = tokens[0]
        second = tokens[1] if len(tokens) > 1 else None
        third = tokens[2] if len(tokens) > 2 else None
        if first in {"note", "x", "m", "f", "c", "pad"} and second:
            family = "poco" if first in {"x", "m", "f", "c"} else "redmi" if first == "note" else ""
            if re.fullmatch(r"\d{1,3}[a-z]?", second):
                number = f"{first} {second}" if first in {"note", "pad"} else f"{first}{second}"
                if third in VARIANT_TOKENS:
                    variant = third
                number, variant = _apply_network(number, variant, 3)
        elif re.fullmatch(r"\d{1,3}[a-z]?", first):
            family = ""
            number = first
            if (
                second
                and re.fullmatch(r"[a-z]", second)
                and second not in VARIANT_TOKENS
                and not re.search(r"[a-z]$", first)
            ):
                number = f"{first}{second}"
                if third in VARIANT_TOKENS:
                    variant = third
                    number, variant = _apply_network(number, variant, 3)
                elif third in NETWORK_TOKENS:
                    number = f"{number} {third}"
            elif second in VARIANT_TOKENS:
                variant = second
                number, variant = _apply_network(number, variant, 2)
            elif second in NETWORK_TOKENS:
                number = f"{number} {second}"
            elif second and re.fullmatch(r"\d{3,}", second):
                number = first
            elif second:
                number = f"{number} {second}"
    if family is not None and number:
        model = f"{family} {number}".strip()
        if variant:
            model = f"{model} {variant}"
        model_key = _normalize_model_key(model)
        if len(model_key) > 24:
            return ParsedModel(
                brand="xiaomi",
                model=None,
                variant=None,
                confidence=0.0,
                reason="too long",
                ambiguous=True,
            )
        return ParsedModel(
            brand="xiaomi", model=model, variant=variant, confidence=0.8, reason="xiaomi"
        )
    return ParsedModel(
        brand="xiaomi", model=None, variant=None, confidence=0.0, reason="no series", ambiguous=True
    )


def _parse_honor(tokens: list[str], brand: str) -> ParsedModel:
    """
    Honor/Huawei heuristic: series + number + variant (e.g. nova 2, p 20 lite, p smart 2019).
    """
    series_tokens = {"nova", "mate", "p", "y", "honor", "enjoy"}
    for idx, tok in enumerate(tokens):
        series = None
        consume = 1
        if tok == "p" and idx + 1 < len(tokens) and tokens[idx + 1] == "smart":
            series = "p smart"
            consume = 2
        elif tok in series_tokens:
            series = tok
        if not series:
            continue
        rest = tokens[idx + consume :]
        number = None
        variant = None
        extra: list[str] = []
        j = 0
        while j < len(rest):
            r = rest[j]
            if re.fullmatch(r"\d{1,3}", r):
                if number is not None:
                    j += 1
                    continue
                if (
                    extra
                    and len(extra[0]) == 1
                    and j + 1 < len(rest)
                    and re.fullmatch(r"[a-z]", rest[j + 1])
                ):
                    number = f"{extra[0]}{r}{rest[j + 1]}"
                    extra = []
                    j += 2
                    continue
                if extra and len(extra[0]) == 1:
                    number = f"{extra[0]}{r}"
                    extra = []
                    j += 1
                    continue
                if j > 0 and rest[j - 1].isalpha() and len(rest[j - 1]) <= 3:
                    j += 1
                    continue
                if (
                    j + 1 < len(rest)
                    and re.fullmatch(r"[a-z]", rest[j + 1])
                    and rest[j + 1] not in VARIANT_TOKENS
                ):
                    number = f"{r}{rest[j + 1]}"
                    j += 2
                else:
                    number = r
                    j += 1
                continue
            if r in VARIANT_TOKENS:
                variant = r
                j += 1
                continue
            if number is None and re.search(r"\d", r):
                number = r
                j += 1
                continue
            if number is None and not extra:
                extra.append(r)
            j += 1
        if (
            number
            and extra
            and len(extra[0]) == 1
            and re.fullmatch(r"[a-z]\d{1,3}[a-z]?", f"{extra[0]}{number}")
        ):
            number = f"{extra[0]}{number}"
            extra = []
        parts = [series]
        if number:
            parts.append(number)
        elif extra:
            parts.append(extra[0])
        if variant:
            parts.append(variant)
        model = " ".join(parts).strip()
        key = _normalize_model_key(model)
        if len(key) > 24:
            return ParsedModel(
                brand=brand,
                model=None,
                variant=None,
                confidence=0.0,
                reason="too long",
                ambiguous=True,
            )
        return ParsedModel(
            brand=brand, model=model, variant=variant, confidence=0.78, reason="huawei"
        )

    number = None
    variant = None
    for idx, tok in enumerate(tokens):
        if re.fullmatch(r"\d{1,3}", tok):
            number = tok
            if idx + 1 < len(tokens) and tokens[idx + 1] in VARIANT_TOKENS:
                variant = tokens[idx + 1]
            break
    if number:
        model = number
        if variant:
            model = f"{model} {variant}"
        key = _normalize_model_key(model)
        if len(key) > 20:
            return ParsedModel(
                brand=brand,
                model=None,
                variant=None,
                confidence=0.0,
                reason="too long",
                ambiguous=True,
            )
        return ParsedModel(
            brand=brand, model=model, variant=variant, confidence=0.75, reason="huawei"
        )
    return ParsedModel(
        brand=brand, model=None, variant=None, confidence=0.0, reason="no number", ambiguous=True
    )


def _parse_realme_oppo_vivo(tokens: list[str], brand: str) -> ParsedModel:
    """
    Heuristic for Realme/Oppo/Vivo/OnePlus: supports series tokens (reno, neo, gt, x, k, nord, r, t, f, v, y)
    + number + variant.
    """
    number = None
    variant = None
    series = None
    network = None
    SERIES_TOKENS = {"reno", "x", "k", "neo", "gt", "nord", "r", "t", "f", "v", "y"}
    for idx, tok in enumerate(tokens):
        nxt = tokens[idx + 1] if idx + 1 < len(tokens) else None
        nxt2 = tokens[idx + 2] if idx + 2 < len(tokens) else None
        if tok == "gt" and nxt == "master":
            model = "gt master edition" if nxt2 == "edition" else "gt master"
            return ParsedModel(
                brand=brand, model=model, variant=None, confidence=0.75, reason=brand
            )
        combined = re.fullmatch(r"([afrxyvgtk])(\d{1,3})", tok)
        if combined and combined.group(1) in SERIES_TOKENS:
            series = combined.group(1)
            number = combined.group(2)
            if nxt == "pro" and nxt2 == "plus":
                variant = "pro plus"
                if idx + 3 < len(tokens) and tokens[idx + 3] in NETWORK_TOKENS:
                    network = tokens[idx + 3]
            elif nxt in VARIANT_TOKENS:
                variant = nxt
                if nxt2 in NETWORK_TOKENS:
                    network = nxt2
            elif nxt in NETWORK_TOKENS:
                network = nxt
            break
        # pattern: series + number
        if tok in SERIES_TOKENS and nxt and re.fullmatch(r"\d{1,3}", nxt):
            series = tok
            number = nxt
            if nxt2 in VARIANT_TOKENS:
                variant = nxt2
                if idx + 3 < len(tokens) and tokens[idx + 3] in NETWORK_TOKENS:
                    network = tokens[idx + 3]
            elif idx + 3 < len(tokens) and nxt2 == "pro" and tokens[idx + 3] == "plus":
                variant = "pro plus"
                if idx + 4 < len(tokens) and tokens[idx + 4] in NETWORK_TOKENS:
                    network = tokens[idx + 4]
            break
        # combined token like "11r"
        match = re.fullmatch(r"(\d{1,3})(r|t)", tok)
        if match:
            number = match.group(1)
            variant = match.group(2)
            break
        if re.fullmatch(r"\d{1,3}", tok) and series is None:
            number = tok
            if nxt == "pro" and nxt2 == "plus":
                variant = "pro plus"
                if idx + 3 < len(tokens) and tokens[idx + 3] in NETWORK_TOKENS:
                    network = tokens[idx + 3]
            elif nxt in VARIANT_TOKENS:
                variant = nxt
                if nxt2 in NETWORK_TOKENS:
                    network = nxt2
            elif nxt in NETWORK_TOKENS:
                network = nxt
            break
    if number:
        model_parts = []
        if series and series not in {"r", "t"}:
            model_parts.append(series)
        model_parts.append(number)
        if variant:
            model_parts.append(variant)
        if network:
            model_parts.append(network)
        model = " ".join(model_parts)
        key = _normalize_model_key(model)
        if len(key) > 20:
            return ParsedModel(
                brand=brand,
                model=None,
                variant=None,
                confidence=0.0,
                reason="too long",
                ambiguous=True,
            )
        return ParsedModel(brand=brand, model=model, variant=variant, confidence=0.75, reason=brand)
    return ParsedModel(
        brand=brand, model=None, variant=None, confidence=0.0, reason="no number", ambiguous=True
    )


def _parse_tablet_watch(tokens: list[str], brand: str) -> ParsedModel:
    """
    Rough parsing for tablets/watches: look for size (two digits), year, or GT pattern.
    """
    size = None
    year = None
    variant = None
    gt_version = None
    for idx, tok in enumerate(tokens):
        if re.fullmatch(r"\d{2}", tok):
            size = tok
            if idx + 1 < len(tokens) and tokens[idx + 1] in VARIANT_TOKENS:
                variant = tokens[idx + 1]
            break
        if YEAR_RE.fullmatch(tok):
            year = tok
        if tok == "gt" and idx + 1 < len(tokens) and re.fullmatch(r"\d{1,2}", tokens[idx + 1]):
            gt_version = tokens[idx + 1]
    model_parts = []
    if gt_version:
        model_parts.append("gt")
        model_parts.append(gt_version)
    if size:
        model_parts.append(size)
    if year:
        model_parts.append(year)
    if not model_parts:
        return ParsedModel(
            brand=brand,
            model=None,
            variant=None,
            confidence=0.0,
            reason="no size/year",
            ambiguous=True,
        )
    model = " ".join(model_parts)
    if variant:
        model = f"{model} {variant}"
    key = _normalize_model_key(model)
    if len(key) > 20:
        return ParsedModel(
            brand=brand, model=None, variant=None, confidence=0.0, reason="too long", ambiguous=True
        )
    return ParsedModel(
        brand=brand, model=model, variant=variant, confidence=0.78, reason="tablet/watch"
    )


def _parse_generic(tokens: list[str], brand: str) -> ParsedModel:
    tokens = [tok for tok in tokens if tok not in {"filling", "capacity"}]
    family_model = _generic_family_model(tokens)
    if family_model:
        return ParsedModel(
            brand=brand,
            model=family_model,
            variant=None,
            confidence=0.76,
            reason="generic_family",
        )
    candidates = [tok for tok in tokens if tok not in NETWORK_TOKENS and re.search(r"\d", tok)]
    if not candidates:
        return ParsedModel(
            brand=brand,
            model=None,
            variant=None,
            confidence=0.0,
            reason="no numeric",
            ambiguous=True,
        )
    if len(candidates) > 1:
        return ParsedModel(
            brand=brand,
            model=None,
            variant=None,
            confidence=0.0,
            reason="multiple numeric tokens",
            ambiguous=True,
        )
    candidate = candidates[0]
    variant: str | None = None
    try:
        candidate_idx = tokens.index(candidate)
    except ValueError:
        candidate_idx = -1
    if (
        candidate_idx >= 0
        and candidate_idx + 1 < len(tokens)
        and re.fullmatch(r"[a-z]", tokens[candidate_idx + 1])
        and tokens[candidate_idx + 1] not in VARIANT_TOKENS
    ):
        candidate = f"{candidate}{tokens[candidate_idx + 1]}"
    for tok in tokens[candidate_idx + 1 :] if candidate_idx >= 0 else tokens:
        if tok in VARIANT_TOKENS:
            variant = tok
            if variant == "xl" and "pro" in tokens[max(0, candidate_idx) : candidate_idx + 3]:
                variant = "pro xl"
            break
    model_name = candidate
    if variant and variant not in model_name:
        model_name = f"{model_name} {variant}"

    model_key = _normalize_model_key(model_name)
    if len(model_key) > 20:
        return ParsedModel(
            brand=brand,
            model=None,
            variant=None,
            confidence=0.0,
            reason="model too long",
            ambiguous=True,
        )
    return ParsedModel(
        brand=brand, model=model_name, variant=variant, confidence=0.75, reason="generic"
    )


def _generic_family_model(tokens: list[str]) -> str | None:
    family_tokens = {
        "camon",
        "phantom",
        "pova",
        "pouvoir",
        "spark",
        "pop",
        "hot",
        "note",
        "smart",
        "zero",
        "xpad",
        "megapad",
        "pad",
        "tab",
        "xiaoxin",
        "m10",
        "gt",
        "vision",
        "go",
    }
    for idx, token in enumerate(tokens):
        if token not in family_tokens:
            continue
        model_parts = [token]
        for tail in tokens[idx + 1 : idx + 5]:
            if (
                tail in NETWORK_TOKENS
                or tail in VARIANT_TOKENS
                or re.fullmatch(r"\d{1,4}[a-z]?", tail)
                or re.fullmatch(r"[a-z]{1,4}", tail)
            ):
                model_parts.append(tail)
                continue
            break
        if len(model_parts) > 1:
            return " ".join(model_parts)
    return None


_PAREN_NOISE_TOKENS = {
    "black",
    "white",
    "blue",
    "red",
    "green",
    "gold",
    "gray",
    "grey",
    "silver",
    "черный",
    "чёрный",
    "белый",
    "синий",
    "красный",
    "зеленый",
    "зелёный",
    "серый",
    "серебро",
    "золото",
    "sim",
    "esim",
}


def _strip_model_parse_noise(value: str) -> str:
    value = re.sub(r"\b\d{2,3}\s*%", " ", value)
    value = re.sub(r"\b1\s*:\s*1\b", " ", value)
    value = re.sub(r"\b\d+\s*[- ]?я\s+категория\b", " ", value)
    value = re.sub(r"\bor\s*100\b|\bor100\b", " ", value, flags=re.IGNORECASE)

    def replace_parenthesized(match: re.Match[str]) -> str:
        inner = match.group(1).strip().lower()
        if not inner:
            return " "
        if any(token in inner for token in _PAREN_NOISE_TOKENS):
            return " "
        if re.search(
            r"\b(?:sm-[a-z0-9]{3,6}|gh\d{4,}[a-z]?|[a-z]{1,5}-\d{1,5}[a-z]{0,4}|[a-z]{1,5}\d{1,5}[a-z]{0,4}(?:-[a-z0-9]+)?)\b",
            inner,
        ):
            return " "
        return match.group(0)

    return re.sub(r"\(([^)]*)\)", replace_parenthesized, value)


def _strip_non_apple_screen_size_noise(value: str) -> str:
    value = re.sub(r"\b\d{1,2}[.,]\d{1,2}\s*(?:\"|”|″|дюйм\w*|inch(?:es)?)", " ", value)
    return re.sub(r"\b\d{1,2}\s*(?:\"|”|″|дюйм\w*|inch(?:es)?)", " ", value)


_SLASH_MODEL_FAMILY_TOKENS = {
    "a",
    "m",
    "s",
    "j",
    "tab",
    "galaxy",
    "spark",
    "hot",
    "note",
    "zero",
    "smart",
    "xpad",
    "megapad",
    "pad",
    "xiaoxin",
    "poco",
    "redmi",
    "mi",
    "gt",
    "vision",
    "go",
    "camon",
    "pova",
    "pouvoir",
    "pop",
    "m10",
}

_SLASH_MODEL_TRAILING_NOISE = {
    "battery",
    "collection",
    "narrow",
    "wide",
    "connector",
    "premium",
    "премиум",
    "узкий",
    "широкий",
    "коллекция",
}


def _looks_like_model_token(token: str) -> bool:
    return bool(
        token in VARIANT_TOKENS
        or token in NETWORK_TOKENS
        or re.fullmatch(r"[a-z]{0,4}\d{1,4}[a-z]?", token)
        or re.fullmatch(r"\d{1,4}[a-z]?", token)
    )


def _trim_slash_model_tokens(tokens: list[str], brand: str | None) -> list[str]:
    if not tokens:
        return []
    start = 0
    for idx, token in enumerate(tokens):
        if token in _SLASH_MODEL_FAMILY_TOKENS or _looks_like_model_token(token):
            start = idx
            break
    else:
        return []
    trimmed: list[str] = []
    for token in tokens[start:]:
        if token in _SLASH_MODEL_TRAILING_NOISE:
            break
        if token in STOP_TOKENS and token not in {"m", "s", "a", "j"}:
            continue
        if token in _SLASH_MODEL_FAMILY_TOKENS or _looks_like_model_token(token):
            trimmed.append(token)
            continue
        if brand in {"samsung", "lenovo", "realme"} and token in {
            "lite",
            "master",
            "edition",
            "plus",
            "gen",
        }:
            trimmed.append(token)
            continue
        if len(trimmed) >= 2:
            break
    while trimmed and trimmed[-1] in {"plus"} and len(trimmed) == 1:
        trimmed.pop()
    return trimmed[:6]


def _slash_model_prefix(tokens: list[str], brand: str | None) -> list[str]:
    if not tokens:
        return []
    if brand == "samsung":
        if tokens[:2] == ["galaxy", "tab"]:
            return ["galaxy", "tab"]
        if tokens[0] == "tab":
            return ["galaxy", "tab"]
        return ["galaxy"]
    if tokens[0] in {
        "spark",
        "hot",
        "note",
        "xpad",
        "megapad",
        "pad",
        "xiaoxin",
        "vision",
        "camon",
        "pouvoir",
        "pova",
        "pop",
    }:
        return [tokens[0]]
    if tokens[0] in {"redmi", "poco", "mi"}:
        return [tokens[0]]
    return []


def _parse_slash_models(
    source: str,
    *,
    default_brand: str | None,
) -> ParsedModel | None:
    if "/" not in source or not default_brand:
        return None
    source = source.replace("+", " plus ")
    source = re.sub(
        r"\bsm-[a-z]\d{3,5}[a-z]?(?:\s*/\s*[a-z]\d{3,5}[a-z]?)+\b",
        " ",
        source,
        flags=re.IGNORECASE,
    )
    segments = [segment.strip() for segment in re.split(r"\s*/\s*", source) if segment.strip()]
    if len(segments) < 2:
        return None

    models: list[str] = []
    seen: set[str] = set()
    current_brand = default_brand
    inherited_prefix: list[str] = []

    for segment in segments:
        raw_tokens = re.findall(r"[a-z0-9а-яё]+", segment)
        tokens = _clean_tokens(raw_tokens)
        if not tokens:
            continue
        segment_brand, brand_idx = _detect_brand(tokens)
        if segment_brand:
            current_brand = segment_brand
            tokens = tokens[brand_idx + 1 :]
        if not tokens:
            continue
        segment_started_with_go = tokens[0] == "go"
        used_go_prefix = inherited_prefix == ["spark", "go"] and tokens[0] != "spark"
        if current_brand == "samsung" and tokens[0] == "tab":
            tokens = ["galaxy", *tokens]
        if inherited_prefix and (
            (tokens[0] not in _SLASH_MODEL_FAMILY_TOKENS and _looks_like_model_token(tokens[0]))
            or tokens[0] == "go"
        ):
            tokens = [*inherited_prefix, *tokens]
        model_tokens = _trim_slash_model_tokens(tokens, current_brand)
        if not model_tokens:
            continue
        if current_brand == "samsung" and model_tokens[0] != "galaxy":
            model_tokens = ["galaxy", *model_tokens]

        model = " ".join(model_tokens).strip()
        if current_brand != default_brand:
            model = f"{current_brand} {model}".strip()
        key = _normalize_model_key(model)
        if not key or key in seen:
            inherited_prefix = _slash_model_prefix(model_tokens, current_brand)
            continue
        models.append(model)
        seen.add(key)
        inherited_prefix = (
            ["spark", "go"]
            if (segment_started_with_go or used_go_prefix) and model_tokens[:2] == ["spark", "go"]
            else _slash_model_prefix(model_tokens, current_brand)
        )

    if len(models) < 2:
        return None
    return ParsedModel(
        brand=default_brand,
        model=models[0],
        variant=None,
        confidence=0.78,
        reason="slash_models",
        models=models,
        ambiguous=False,
    )


def parse_model_name(raw_name: str | None) -> ParsedModel:
    if not raw_name:
        return ParsedModel(
            brand=None, model=None, variant=None, confidence=0.0, reason="empty", ambiguous=True
        )
    lower = raw_name.lower()
    normalized_source = _strip_model_parse_noise(lower)
    normalized_source = re.sub(
        r"\bsm-[a-z]\d{3,5}[a-z]?(?:\s*/\s*[a-z]\d{3,5}[a-z]?)+\b",
        " ",
        normalized_source,
        flags=re.IGNORECASE,
    )
    device_code_re = re.compile(
        r"(?i)\b(?:sm-[a-z0-9]{3,6}|gh\d{4,}[a-z]?|[a-z]{2,4}-[a-z]{1,2}\d{1,2}[a-z]?|m\d{3,4}[a-z]\d{1,2}[a-z]?|\d{6,}[a-z]{1,3}|\d{4,}[a-z]{1,4}\d[a-z0-9]*)\b"
    )
    token_source = device_code_re.sub(" ", normalized_source)
    if not any(keyword in lower for keyword in MODEL_PARSE_KEYWORDS):
        return ParsedModel(
            brand=None,
            model=None,
            variant=None,
            confidence=0.0,
            reason="no keyword",
            ambiguous=True,
        )
    raw_tokens = re.findall(r"[a-z0-9а-яё]+", token_source)
    brand, brand_idx = _detect_brand(raw_tokens)
    if not brand:
        return ParsedModel(
            brand=None, model=None, variant=None, confidence=0.0, reason="no brand", ambiguous=True
        )
    if brand != "apple":
        token_source = _strip_non_apple_screen_size_noise(token_source)
        raw_tokens = re.findall(r"[a-z0-9а-яё]+", token_source)
        brand, brand_idx = _detect_brand(raw_tokens)
    slash_parsed = _parse_slash_models(token_source, default_brand=brand)
    if slash_parsed is not None:
        return slash_parsed
    tail_tokens = raw_tokens[brand_idx + 1 :]
    cleaned = _clean_tokens(tail_tokens)
    if brand == "apple":
        brand_token = raw_tokens[brand_idx] if brand_idx is not None else None
        device_prefix = None
        if brand_token in {"iphone", "ipad", "ipod", "watch"}:
            device_prefix = brand_token
        elif brand_token == "apple":
            for candidate in ("ipad", "iphone", "ipod", "watch"):
                if candidate in raw_tokens[brand_idx + 1 :]:
                    device_prefix = candidate
                    break
            if device_prefix is None:
                device_prefix = "iphone"
        a_codes = []
        for tok in tail_tokens:
            if APPLE_A_CODE_RE.fullmatch(tok):
                code = tok.upper()
                if code not in a_codes:
                    a_codes.append(code)
        a_code = "/".join(a_codes) if a_codes else None
        parsed = _parse_apple(cleaned, a_code=a_code, device_prefix=device_prefix)
        return parsed
    if brand == "samsung":
        return _parse_samsung(cleaned)
    if brand == "xiaomi":
        return _parse_xiaomi(cleaned)
    if brand in {"huawei", "honor"}:
        series_tokens = {"honor", "nova", "mate", "p", "y", "enjoy", "smart"}
        if "p" in cleaned:
            for idx, tok in enumerate(cleaned[:-1]):
                if tok == "p" and cleaned[idx + 1] == "smart":
                    series_tokens.add("p smart")
                    break
        has_series = any(tok in series_tokens for tok in cleaned)
        # attempt tablet/watch parsing first only when no phone series tokens
        if not has_series:
            parsed_tab = _parse_tablet_watch(cleaned, brand)
            if parsed_tab.model and parsed_tab.confidence >= 0.7:
                return parsed_tab
        return _parse_honor(cleaned, brand=brand)
    if brand in {"realme", "oppo", "vivo", "oneplus"}:
        return _parse_realme_oppo_vivo(cleaned, brand=brand)
    parsed = _parse_generic(cleaned, brand=brand)
    return parsed


def _extract_quality(name: str | None) -> str | None:
    return normalize_display_quality(name)


def _normalize_quality_value(value: str | None) -> str | None:
    return normalize_display_quality(value)


def _first_normalized_quality(*values: str | None) -> str | None:
    for value in values:
        normalized = _normalize_quality_value(value)
        if normalized:
            return normalized
    return None


def _product_quality_value(product: Product) -> str | None:
    return _first_normalized_quality(
        product.display_quality,
        product.quality,
        product.display_quality_raw,
        product.quality_raw,
    )


def _competitor_quality_value(
    item: CompetitorItem | None,
    fallback_name: str | None,
) -> str | None:
    return _first_normalized_quality(
        item.attrs_quality if item else None,
        item.screen_quality_grade if item else None,
        fallback_name,
    )


def _extract_display_type(name: str | None) -> str | None:
    return normalize_display_type(name)


def _extract_in_frame(name: str | None) -> bool | None:
    if not name:
        return None
    lower = name.lower()
    if "в рамке" in lower:
        return True
    if "без рамки" in lower or "no frame" in lower:
        return False
    return None


def _extract_variant(model_tokens: list[str]) -> tuple[list[str], str | None]:
    if not model_tokens:
        return model_tokens, None
    variant = None
    filtered: list[str] = []
    for tok in model_tokens:
        if tok in VARIANT_TOKENS and variant is None:
            variant = tok
            continue
        filtered.append(tok)
    return filtered, variant


def _load_products_by_brand_model(session: Session) -> dict[str, list[tuple[str, Product]]]:
    brand_map: dict[str, list[tuple[str, Product]]] = {}
    for product in session.execute(select(Product)).scalars():
        parsed = parse_model_name(product.name)
        if not parsed.brand or parsed.ambiguous or parsed.confidence < 0.7 or not parsed.model:
            continue
        model_key = _normalize_model_key(parsed.model)
        if not model_key:
            continue
        brand_map.setdefault(parsed.brand, []).append((model_key, product))
    return brand_map


def _load_products_by_phone_model(session: Session) -> dict[int, list[Product]]:
    product_map: dict[int, list[Product]] = {}
    rows = session.execute(
        select(ProductPhoneModel, Product).join(Product, ProductPhoneModel.product_id == Product.id)
    ).all()
    for link, product in rows:
        product_map.setdefault(link.phone_model_id, []).append(product)
    return product_map


def _filter_product_candidates(
    products: list[Product],
    quality_token: str | None,
    display_type_token: str | None,
    in_frame_token: bool | None,
) -> list[Product]:
    matched_products = list(products)
    if len(matched_products) > 1:
        norm_quality = _normalize_quality_value(quality_token)
        if norm_quality:
            filtered = [
                p
                for p in matched_products
                if _normalize_quality_value(_product_quality_value(p)) == norm_quality
            ]
            if filtered:
                matched_products = filtered
    if len(matched_products) > 1 and display_type_token:
        filtered = [
            p
            for p in matched_products
            if normalize_display_type(getattr(p, "display_type", None)) == display_type_token
        ]
        if filtered:
            matched_products = filtered
    if len(matched_products) > 1 and in_frame_token is not None:
        filtered = []
        for p in matched_products:
            val = getattr(p, "in_frame", None)
            if val is None:
                continue
            val_norm = str(val).lower()
            if in_frame_token and val_norm in {"да", "yes", "true", "1"}:
                filtered.append(p)
            if in_frame_token is False and val_norm in {"нет", "no", "false", "0"}:
                filtered.append(p)
        if filtered:
            matched_products = filtered
    return matched_products


def _load_overrides(
    session: Session, sources: Sequence[str] | None
) -> dict[tuple, ProductMatchOverride]:
    query = select(ProductMatchOverride)
    if sources:
        query = query.where(ProductMatchOverride.competitor_source.in_(list(sources)))
    overrides: dict[tuple, ProductMatchOverride] = {}
    for ov in session.execute(query).scalars():
        key = (
            ov.competitor_source,
            _normalize_sku(ov.competitor_sku) if ov.competitor_sku else None,
        )
        overrides[key] = ov
    return overrides


def _ensure_competitor(session: Session, name: str) -> Competitor:
    competitor = session.execute(
        select(Competitor).where(Competitor.name == name)
    ).scalar_one_or_none()
    if competitor:
        return competitor
    competitor = Competitor(name=name)
    session.add(competitor)
    session.flush()
    return competitor


def _existing_match(session: Session, product_id: int, competitor_id: int) -> ProductMatch | None:
    stmt: Select[tuple[ProductMatch]] = select(ProductMatch).where(
        ProductMatch.product_id == product_id,
        ProductMatch.competitor_id == competitor_id,
    )
    return session.execute(stmt).scalar_one_or_none()


def _existing_price(
    session: Session,
    product_id: int,
    competitor_id: int,
    collected_at,
) -> CompetitorPrice | None:
    stmt: Select[tuple[CompetitorPrice]] = select(CompetitorPrice).where(
        CompetitorPrice.product_id == product_id,
        CompetitorPrice.competitor_id == competitor_id,
        CompetitorPrice.collected_at == collected_at,
    )
    return session.execute(stmt).scalar_one_or_none()


def _preload_catalog_items(
    session: Session,
    records: Sequence[CompetitorFtpRecord],
) -> dict[tuple[str, str], CompetitorItem]:
    keys = {(record.source, record.sku) for record in records if record.source and record.sku}
    if not keys:
        return {}
    sources = sorted({source for source, _ in keys})
    external_ids = sorted({sku for _, sku in keys})
    items = session.execute(
        select(CompetitorItem).where(
            CompetitorItem.competitor.in_(sources),
            CompetitorItem.external_id.in_(external_ids),
        )
    ).scalars()
    return {(item.competitor, item.external_id): item for item in items}


def _preload_competitors(
    session: Session, records: Sequence[CompetitorFtpRecord]
) -> dict[str, Competitor]:
    source_names = sorted({record.source for record in records if record.source})
    if not source_names:
        return {}
    competitors = {
        competitor.name: competitor
        for competitor in session.execute(
            select(Competitor).where(Competitor.name.in_(source_names))
        ).scalars()
    }
    created = False
    for name in source_names:
        if name not in competitors:
            competitor = Competitor(name=name)
            session.add(competitor)
            competitors[name] = competitor
            created = True
    if created:
        session.flush()
    return competitors


def _preload_price_keys(
    session: Session,
    competitor_ids: Sequence[int],
    since_date: date,
) -> set[tuple[int, int, object]]:
    if not competitor_ids:
        return set()
    rows = session.execute(
        select(
            CompetitorPrice.product_id, CompetitorPrice.competitor_id, CompetitorPrice.collected_at
        ).where(
            CompetitorPrice.competitor_id.in_(list(competitor_ids)),
            CompetitorPrice.collected_at >= since_date,
        )
    )
    return {
        (product_id, competitor_id, collected_at)
        for product_id, competitor_id, collected_at in rows
    }


def _preload_matches(
    session: Session,
    competitor_ids: Sequence[int],
) -> dict[tuple[int, int], ProductMatch]:
    if not competitor_ids:
        return {}
    matches = session.execute(
        select(ProductMatch).where(ProductMatch.competitor_id.in_(list(competitor_ids)))
    ).scalars()
    return {(match.product_id, match.competitor_id): match for match in matches}


def _record_freshness_key(record: CompetitorFtpRecord) -> tuple:
    return (
        record.file_date or date.min,
        record.observed_at,
        record.id or 0,
    )


def _latest_records_by_catalog_key(
    records: Sequence[CompetitorFtpRecord],
) -> dict[tuple[str, str], CompetitorFtpRecord]:
    latest: dict[tuple[str, str], CompetitorFtpRecord] = {}
    for record in records:
        key = (record.source, record.sku)
        current = latest.get(key)
        if current is None or _record_freshness_key(record) > _record_freshness_key(current):
            latest[key] = record
    return latest


def _preload_snapshot_keys(
    session: Session,
    items: Sequence[CompetitorItem],
    since_date: date,
) -> set[tuple[int, object]]:
    item_ids = [item.id for item in items if item.id is not None]
    if not item_ids:
        return set()
    rows = session.execute(
        select(CompetitorItemSnapshot.competitor_item_id, CompetitorItemSnapshot.scraped_at).where(
            CompetitorItemSnapshot.competitor_item_id.in_(item_ids),
            CompetitorItemSnapshot.scraped_at >= since_date,
        )
    )
    return {(item_id, scraped_at) for item_id, scraped_at in rows}


def match_competitor_ftp_records(
    session: Session,
    days_back: int = 3,
    sources: Sequence[str] | None = None,
    max_samples: int = 20,
    name_contains: str | None = None,
    limit: int | None = None,
    llm_client: LlmParseClient | None = None,
    llm_limit: int = 0,
    llm_threshold: float = 0.7,
    catalog_only_new: bool = False,
    catalog_llm_new: bool = True,
    category_llm_enabled: bool = True,
) -> dict:
    """
    Сопоставляет FTP-записи конкурентов с товарами по SKU и пишет цены в competitor_price.
    """
    stats = MatchStats()
    catalog_created = 0
    catalog_updated = 0
    catalog_snapshots = 0
    since_date = date.today() - timedelta(days=days_back)

    query = select(CompetitorFtpRecord).where(CompetitorFtpRecord.file_date >= since_date)
    if sources:
        query = query.where(CompetitorFtpRecord.source.in_(list(sources)))
    if name_contains:
        query = query.where(CompetitorFtpRecord.name.ilike(f"%{name_contains}%"))
    if limit:
        query = query.limit(limit)

    records = sorted(session.execute(query).scalars(), key=_record_freshness_key)
    if not records:
        return {"skipped": True, "reason": "no_records"}
    latest_catalog_records = _latest_records_by_catalog_key(records)

    products_by_sku = _load_products_by_article(session)
    products_by_brand_model = _load_products_by_brand_model(session)
    products_by_phone_model = _load_products_by_phone_model(session)
    overrides = _load_overrides(session, sources)
    competitors = _preload_competitors(session, records)
    catalog_items = _preload_catalog_items(session, records)
    existing_snapshot_keys = _preload_snapshot_keys(
        session, list(catalog_items.values()), since_date
    )
    existing_price_keys = _preload_price_keys(
        session, [competitor.id for competitor in competitors.values()], since_date
    )
    existing_matches = _preload_matches(
        session, [competitor.id for competitor in competitors.values()]
    )
    phone_models_cache: dict[tuple[str, str, str | None], PhoneModel] = {}
    unmatched_samples: list[dict] = []
    ambiguous_samples: list[dict] = []
    low_conf_samples: list[dict] = []
    llm_calls_used = 0
    category_classifier: CategoryClassifier | None = None
    canonicalizer = PhoneModelCanonicalizer(session)

    try:
        for record in records:
            stats.processed += 1
            product: Product | None = None
            phone_model: PhoneModel | None = None
            quality = None
            is_manual = False
            llm_audit_raw_json: str | None = None
            llm_audit_model: str | None = None
            llm_audit_error: str | None = None

            parsed_model: ParsedModel = parse_model_name(record.name)
            if parsed_model:
                record.parsed_device_brand = parsed_model.brand
                record.parsed_device_model = parsed_model.model
                record.parsed_device_variant = parsed_model.variant
                record.parse_confidence = parsed_model.confidence
                notes = parsed_model.reason or None
                if parsed_model.ambiguous:
                    notes = f"{notes}; ambiguous" if notes else "ambiguous"
                record.parse_notes = notes

            use_llm = (
                llm_client is not None
                and llm_calls_used < llm_limit
                and (
                    parsed_model is None
                    or parsed_model.ambiguous
                    or parsed_model.confidence < llm_threshold
                )
            )
            if use_llm:
                llm_note = None
                try:
                    llm_parsed = llm_client.parse(record.source, record.sku, record.name or "")
                except Exception:
                    logger.exception(
                        "llm parse failed for record",
                        extra={"source": record.source, "sku": record.sku},
                    )
                    llm_parsed = None
                if llm_parsed:
                    llm_raw_json = llm_parsed.llm_raw_json
                    llm_audit_raw_json = llm_raw_json
                    llm_audit_model = llm_parsed.llm_model_name
                    llm_parsed, llm_note = _sanitize_llm_models(llm_parsed, item_name=record.name)
                    if llm_parsed:
                        llm_parsed.llm_raw_json = llm_raw_json
                if llm_parsed:
                    llm_calls_used += 1
                    parsed_model = llm_parsed
                    record.parsed_device_brand = llm_parsed.brand
                    record.parsed_device_model = llm_parsed.model
                    record.parsed_device_variant = llm_parsed.variant
                    record.parse_confidence = llm_parsed.confidence
                    notes = record.parse_notes or ""
                    if notes:
                        notes = f"{notes}; llm"
                    else:
                        notes = "llm"
                    if llm_note:
                        notes = f"{notes}; {llm_note}"
                    record.parse_notes = notes
                elif llm_note:
                    llm_audit_error = llm_note
                    if llm_note == "llm_blocked_wearable":
                        record.parsed_device_brand = None
                        record.parsed_device_model = None
                        record.parsed_device_variant = None
                        record.parse_confidence = None
                    notes = record.parse_notes or ""
                    record.parse_notes = f"{notes}; {llm_note}".strip("; ")

            # Upsert в каталог конкурента
            catalog_item = catalog_items.get((record.source, record.sku))
            is_latest_catalog_record = (
                latest_catalog_records.get((record.source, record.sku)) is record
            )
            sku_norm = _normalize_sku(record.sku)
            name_norm = " ".join(record.name.lower().split()) if record.name else None
            category_value = canonicalize_category(record.group_name)
            if category_llm_enabled:
                if catalog_item is None and record.name:
                    if category_classifier is None:
                        category_classifier = CategoryClassifier.from_env(force_llm=catalog_llm_new)
                    llm_category = category_classifier.classify(record.name)
                    if llm_category:
                        category_value = canonicalize_category(llm_category) or category_value
                elif not category_value and record.name and not catalog_item.category:
                    if category_classifier is None:
                        category_classifier = CategoryClassifier.from_env()
                    llm_category = category_classifier.classify(record.name)
                    if llm_category:
                        category_value = canonicalize_category(llm_category)
            if catalog_item is None:
                catalog_item = CompetitorItem(
                    competitor=record.source,
                    external_id=record.sku,
                    sku_norm=sku_norm,
                    name=record.name,
                    name_norm=name_norm,
                    category=category_value,
                    category_group=category_group(category_value),
                    price_opt=record.price_opt,
                    price_roz=record.price_roz,
                    availability=record.in_stock,
                    url=record.link,
                    scraped_at=record.observed_at,
                    first_seen_at=record.file_date,
                    last_seen_at=record.file_date,
                    parsed_device_brand=record.parsed_device_brand,
                    parsed_device_model=record.parsed_device_model,
                    parsed_device_variant=record.parsed_device_variant,
                    parse_confidence=record.parse_confidence,
                    parse_notes=record.parse_notes,
                )
                if parsed_model and parsed_model.parse_origin == "llm":
                    catalog_item.llm_model = parsed_model.llm_model_name
                    catalog_item.llm_raw_json = parsed_model.llm_raw_json
                    catalog_item.parse_status = CompetitorItemParseStatus.OK
                    catalog_item.parse_version = "llm_parse_v2"
                    catalog_item.parse_error = None
                elif llm_audit_raw_json or llm_audit_error:
                    catalog_item.llm_model = llm_audit_model
                    catalog_item.llm_raw_json = llm_audit_raw_json
                    catalog_item.parse_status = CompetitorItemParseStatus.CONFLICT
                    catalog_item.parse_error = llm_audit_error
                    catalog_item.parse_version = "llm_parse_v2"
                session.add(catalog_item)
                session.flush()
                catalog_items[(record.source, record.sku)] = catalog_item
                catalog_created += 1
            else:
                if is_latest_catalog_record and not catalog_only_new:
                    catalog_item.name = record.name
                    catalog_item.name_norm = name_norm
                    if not catalog_item.category:
                        if record.group_name:
                            catalog_item.category = record.group_name
                        elif category_value:
                            catalog_item.category = category_value
                    if catalog_item.category:
                        catalog_item.category_group = category_group(catalog_item.category)
                    catalog_item.price_opt = record.price_opt
                    catalog_item.price_roz = record.price_roz
                    catalog_item.availability = record.in_stock
                    catalog_item.url = record.link
                    catalog_item.scraped_at = record.observed_at
                    catalog_item.last_seen_at = record.file_date
                    if not catalog_item.sku_norm:
                        catalog_item.sku_norm = sku_norm
                    # обновляем parsed_* если пришла уверенная новая инфа
                    if record.parsed_device_brand and not catalog_item.parsed_device_brand:
                        catalog_item.parsed_device_brand = record.parsed_device_brand
                    if record.parsed_device_model and not catalog_item.parsed_device_model:
                        catalog_item.parsed_device_model = record.parsed_device_model
                    if record.parsed_device_variant and not catalog_item.parsed_device_variant:
                        catalog_item.parsed_device_variant = record.parsed_device_variant
                    if record.parse_confidence is not None and (
                        catalog_item.parse_confidence is None
                        or float(record.parse_confidence) > float(catalog_item.parse_confidence)
                    ):
                        catalog_item.parse_confidence = record.parse_confidence
                        catalog_item.parse_notes = record.parse_notes
                    if parsed_model and parsed_model.parse_origin == "llm":
                        catalog_item.llm_model = parsed_model.llm_model_name
                        catalog_item.llm_raw_json = parsed_model.llm_raw_json
                        catalog_item.parse_status = CompetitorItemParseStatus.OK
                        catalog_item.parse_version = "llm_parse_v2"
                        catalog_item.parse_error = None
                    elif llm_audit_raw_json or llm_audit_error:
                        catalog_item.llm_model = llm_audit_model
                        catalog_item.llm_raw_json = llm_audit_raw_json
                        catalog_item.parse_status = CompetitorItemParseStatus.CONFLICT
                        catalog_item.parse_error = llm_audit_error
                        catalog_item.parse_version = "llm_parse_v2"
                    catalog_updated += 1

            if catalog_item.id is None:
                session.flush()
            snapshot_key = (catalog_item.id, record.observed_at)
            if snapshot_key not in existing_snapshot_keys:
                snapshot = CompetitorItemSnapshot(
                    item=catalog_item,
                    price_opt=record.price_opt,
                    price_roz=record.price_roz,
                    availability=record.in_stock,
                    scraped_at=record.observed_at,
                )
                session.add(snapshot)
                existing_snapshot_keys.add(snapshot_key)
                catalog_snapshots += 1
            quality = _competitor_quality_value(catalog_item, record.name)

            sku_norm = _normalize_sku(record.sku)
            override = overrides.get((record.source, sku_norm)) or overrides.get(
                (record.source, None)
            )
            if override:
                if override.product_id:
                    product = session.get(Product, override.product_id)
                if override.phone_model_id:
                    phone_model = session.get(PhoneModel, override.phone_model_id)
                if override.brand and override.model and not phone_model:
                    phone_model_key = (override.brand, override.model, None)
                    pm = phone_models_cache.get(phone_model_key)
                    if pm is None:
                        pm = session.execute(
                            select(PhoneModel).where(
                                PhoneModel.brand == override.brand,
                                PhoneModel.model_name == override.model,
                            )
                        ).scalar_one_or_none()
                        if pm is not None:
                            phone_models_cache[phone_model_key] = pm
                    if pm:
                        phone_model = pm
                if override.quality:
                    quality = _normalize_quality_value(override.quality) or override.quality
                is_manual = True

            if product is None and sku_norm:
                candidates = products_by_sku.get(sku_norm) or []
                if candidates:
                    if len(candidates) > 1:
                        stats.ambiguous += 1
                        if len(ambiguous_samples) < max_samples:
                            ambiguous_samples.append(
                                {"source": record.source, "sku": record.sku, "name": record.name}
                            )
                        continue
                    product = candidates[0]

            if phone_model is None and _should_canonicalize_competitor_model(parsed_model):
                canonical = canonicalizer.canonicalize(
                    source="competitor_parser",
                    raw_value=record.name,
                    brand=parsed_model.brand,
                    model_name=parsed_model.model,
                    variant=parsed_model.variant,
                    confidence=parsed_model.confidence,
                )
                phone_model = canonical.phone_model

            quality_token = quality
            display_type_token = _extract_display_type(record.name)
            in_frame_token = _extract_in_frame(record.name)

            if product is None and phone_model is not None:
                phone_candidates = products_by_phone_model.get(phone_model.id, [])
                phone_candidates = _filter_product_candidates(
                    phone_candidates,
                    quality_token=quality_token,
                    display_type_token=display_type_token,
                    in_frame_token=in_frame_token,
                )
                if len(phone_candidates) == 1:
                    product = phone_candidates[0]
                elif len(phone_candidates) > 1:
                    stats.ambiguous += 1
                    if len(ambiguous_samples) < max_samples:
                        ambiguous_samples.append(
                            {
                                "source": record.source,
                                "sku": record.sku,
                                "name": record.name,
                                "reason": "phone_model_overlap",
                            }
                        )
                    continue

            if product is None:
                brand = parsed_model.brand if parsed_model and not parsed_model.ambiguous else None
                models = []
                if parsed_model.model:
                    models.append(_normalize_model_key(parsed_model.model))
                variant = (
                    parsed_model.variant if parsed_model and not parsed_model.ambiguous else None
                )
                if brand and models and parsed_model.confidence >= 0.7:
                    product_candidates = products_by_brand_model.get(brand, [])
                    matched_products: list[Product] = []
                    seen_ids = set()
                    for cand_model, cand_product in product_candidates:
                        if any(
                            cand_model == model or cand_model in model or model in cand_model
                            for model in models
                        ):
                            if cand_product.id not in seen_ids:
                                matched_products.append(cand_product)
                                seen_ids.add(cand_product.id)
                    matched_products = _filter_product_candidates(
                        matched_products,
                        quality_token=quality_token,
                        display_type_token=display_type_token,
                        in_frame_token=in_frame_token,
                    )
                    if len(matched_products) == 1:
                        product = matched_products[0]
                    elif len(matched_products) > 1:
                        stats.ambiguous += 1
                        if len(ambiguous_samples) < max_samples:
                            ambiguous_samples.append(
                                {"source": record.source, "sku": record.sku, "name": record.name}
                            )
                        continue
                    # create/find phone model with variant if possible
                    phone_model_name = parsed_model.model if parsed_model.model else None
                    if phone_model_name:
                        phone_model_name = " ".join(phone_model_name.split()).strip()
                    if phone_model_name and not _is_reasonable_phone_model_name(phone_model_name):
                        phone_model_name = None
                    if variant:
                        variant = (
                            " ".join(str(variant).split()).strip()[:PHONE_MODEL_VARIANT_MAX_LEN]
                            or None
                        )
                    if (
                        phone_model_name
                        and phone_model is None
                        and _should_canonicalize_competitor_model(parsed_model)
                    ):
                        phone_model_key = (brand, phone_model_name, variant)
                        phone_model = phone_models_cache.get(phone_model_key)
                        if phone_model is None:
                            canonical = canonicalizer.canonicalize(
                                source="competitor_parser",
                                raw_value=record.name,
                                brand=brand,
                                model_name=phone_model_name,
                                variant=variant,
                                confidence=parsed_model.confidence if parsed_model else None,
                            )
                            phone_model = canonical.phone_model
                            if phone_model is not None:
                                phone_models_cache[phone_model_key] = phone_model
                else:
                    if parsed_model and parsed_model.ambiguous:
                        stats.ambiguous += 1
                        if len(ambiguous_samples) < max_samples:
                            ambiguous_samples.append(
                                {"source": record.source, "sku": record.sku, "name": record.name}
                            )
                    else:
                        stats.skipped_low_conf += 1
                        if len(low_conf_samples) < max_samples:
                            low_conf_samples.append(
                                {"source": record.source, "sku": record.sku, "name": record.name}
                            )

            if product is None:
                stats.unmatched += 1
                if len(unmatched_samples) < max_samples:
                    unmatched_samples.append(
                        {"source": record.source, "sku": record.sku, "name": record.name}
                    )
                continue

            if phone_model is None and len(product.phone_model_links) == 1:
                phone_model = product.phone_model_links[0].phone_model

            competitor = competitors[record.source]

            price = record.price_roz if record.price_roz is not None else record.price_opt
            if price is None:
                stats.skipped_no_price += 1
                continue

            price_key = (product.id, competitor.id, record.observed_at)
            if price_key not in existing_price_keys:
                cp = CompetitorPrice(
                    product_id=product.id,
                    competitor_id=competitor.id,
                    price=price,
                    in_stock=record.in_stock,
                    collected_at=record.observed_at,
                )
                session.add(cp)
                existing_price_keys.add(price_key)
                stats.prices_created += 1

            match_key = (product.id, competitor.id)
            pm = existing_matches.get(match_key)
            if not pm:
                match = ProductMatch(
                    product_id=product.id,
                    competitor_id=competitor.id,
                    competitor_sku=record.sku,
                    confidence=1.0,
                    is_manual=is_manual,
                    phone_model_id=phone_model.id if phone_model else None,
                    quality=quality,
                )
                session.add(match)
                existing_matches[match_key] = match
                stats.matches_created += 1
            else:
                updated = False
                if phone_model and not pm.phone_model_id:
                    pm.phone_model_id = phone_model.id
                    updated = True
                if quality and not pm.quality:
                    pm.quality = quality
                    updated = True
                if is_manual and not pm.is_manual:
                    pm.is_manual = True
                    updated = True
                if updated:
                    session.add(pm)

            stats.matched += 1

        session.commit()
        result = {
            "skipped": False,
            **stats.as_dict(),
            "unmatched_samples": unmatched_samples,
            "ambiguous_samples": ambiguous_samples,
            "low_conf_samples": low_conf_samples,
            "catalog_created": catalog_created,
            "catalog_updated": catalog_updated,
            "catalog_snapshots": catalog_snapshots,
            "llm_calls_used": llm_calls_used,
        }
        logger.info(
            "competitor matching completed",
            extra={
                "processed": stats.processed,
                "matched": stats.matched,
                "ambiguous": stats.ambiguous,
                "skipped_low_conf": stats.skipped_low_conf,
                "unmatched": stats.unmatched,
            },
        )
        return result
    finally:
        if category_classifier is not None:
            category_classifier.close()


__all__ = [
    "LlmParseClient",
    "match_competitor_ftp_records",
    "parse_model_name",
    "_sanitize_llm_models",
]
