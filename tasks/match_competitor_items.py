"""Матчинг/LLM по каталогу конкурентов (competitor_item), без повторного прохода по FTP staging."""

from __future__ import annotations

import argparse
import json
import logging
import re
from datetime import datetime

from sqlalchemy import create_engine, exists, func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import CompetitorItem, CompetitorItemCompatibility
from app.models.competitor_item import CompetitorItemParseStatus
from app.services.competitor_matching import (
    LlmParseClient,
    _sanitize_llm_models,
    parse_model_name,
)
from app.services.matching_guardrails import competitor_item_requires_compatibility
from app.services.phone_model_canonicalization import PhoneModelCanonicalizer

logger = logging.getLogger(__name__)
COMPAT_DEVICE_VARIANT_MAX_LEN = 50


def _llm_model_valid(model: str, name: str, brand: str | None) -> bool:
    if not model:
        return False
    model = model.strip().lower()
    if re.fullmatch(r"\d+(?:\.\d+)?", model):
        return False
    name_lower = (name or "").lower()
    years = re.findall(r"\b20(?:1[6-9]|2[0-9])\b", name_lower)
    if years and not any(year in model for year in years):
        return False
    if brand in {"oppo", "realme", "vivo", "oneplus"}:
        series_words = ["reno", "find", "gt", "neo", "note"]
        letter_series = re.findall(r"\b([afrxyvg])\d{1,3}[a-z]?\b", name_lower)
        has_series_in_name = any(word in name_lower for word in series_words) or bool(letter_series)
        has_series_in_model = any(word in model for word in series_words) or any(
            model.startswith(letter) or f" {letter}" in model for letter in letter_series
        )
        if has_series_in_name and not has_series_in_model:
            return False
    if brand in {"huawei", "honor"}:
        series_tokens = ["honor", "nova", "mate", "mediapad", "p smart", "enjoy", "y", "p "]
        has_series_in_name = any(tok in name_lower for tok in series_tokens)
        has_series_in_model = any(
            tok.strip() in model
            for tok in ["honor", "nova", "mate", "mediapad", "p smart", "enjoy", "y", "p"]
        )
        if has_series_in_name and not has_series_in_model:
            return False
    if ("mediapad" in name_lower or "tab" in name_lower) and re.fullmatch(r"\d+(?:\.\d+)?", model):
        return False
    return True


def _codes_from_parentheses(name: str | None) -> set[str]:
    if not name:
        return set()
    found: set[str] = set()
    for match in re.finditer(r"\(([^)]*)\)", name):
        block = match.group(1)
        for code in _extract_device_codes(block):
            found.add(code.upper())
    return found


_CODE_BLACKLIST = {
    "OR",
    "OR100",
    "OR1",
    "OR2",
    "OR3",
    "ORX",
    "SP",
    "HQ",
    "AAA",
    "OEM",
    "PREMIUM",
    "OPTIMA",
    "COPY",
    "ORIG",
}


def _override_iphone_8_se(parsed, name: str | None):
    if not name or not parsed:
        return parsed
    lower = name.lower()
    if "iphone" not in lower:
        return parsed
    if "8/se" not in lower and "8 / se" not in lower:
        return parsed
    if "se (2020)" not in lower and "se 2020" not in lower:
        return parsed
    if "se (2022)" not in lower and "se 2022" not in lower:
        return parsed
    parsed.brand = "apple"
    parsed.models = ["iphone 8", "iphone se 2020", "iphone se 2022"]
    parsed.model = parsed.models[0]
    parsed.items = None
    parsed.ambiguous = False
    parsed.reason = "override_iphone_8_se"
    return parsed


def _normalize_brand_from_name(name: str | None, brand: str | None) -> str | None:
    if not name:
        return brand
    lower = name.lower()
    brand_norm = brand.strip().lower() if isinstance(brand, str) else brand
    # В БД/LLM иногда встречается "Unknown" — считаем это эквивалентом generic.
    if brand_norm == "unknown":
        brand_norm = "generic"
    if brand_norm in {None, "generic"}:
        if any(tok in lower for tok in ("iphone", "ipad", "ipod", "watch", "apple")):
            return "apple"
        if any(tok in lower for tok in ("samsung", "galaxy")):
            return "samsung"
        if any(tok in lower for tok in ("xiaomi", "redmi", "poco", "mi ")):
            return "xiaomi"
        if "huawei" in lower:
            return "huawei"
        if "honor" in lower:
            return "honor"
        if "zte" in lower:
            return "zte"
        if "sony" in lower or "ericsson" in lower:
            return "sony"
        if "nokia" in lower:
            return "nokia"
        if "meizu" in lower or "mblu" in lower:
            return "meizu"
        if "alcatel" in lower:
            return "alcatel"
        if "blackview" in lower:
            return "blackview"
        if "infinix" in lower:
            return "infinix"
        if "tecno" in lower:
            return "tecno"
        if "itel" in lower:
            return "itel"
        if "doogee" in lower:
            return "doogee"
        if "oukitel" in lower:
            return "oukitel"
        if "ulefone" in lower:
            return "ulefone"
        if "cubot" in lower:
            return "cubot"
        if "tcl" in lower:
            return "tcl"
        if "google" in lower or "pixel" in lower:
            return "google"
        if "wiko" in lower:
            return "wiko"
        if "nothing" in lower:
            return "nothing"
        if "umidigi" in lower:
            return "umidigi"
        if re.search(r"\bfly\b", lower):
            return "fly"
        if "philips" in lower:
            return "philips"
        if "leeco" in lower:
            return "leeco"
        if "motorola" in lower:
            return "motorola"
        if "lenovo" in lower:
            return "lenovo"
        if "lg" in lower:
            return "lg"
        if "acer" in lower or "iconia" in lower:
            return "acer"
        if "asus" in lower or "zenfone" in lower:
            return "asus"
        if "htc" in lower:
            return "htc"
        if "oneplus" in lower or "one plus" in lower or "1+" in lower:
            return "oneplus"
        if "oppo" in lower:
            return "oppo"
        if "vivo" in lower:
            return "vivo"
        if "realme" in lower:
            return "realme"
    return brand_norm


def _infer_brand_from_model(model: str | None) -> str | None:
    if not model:
        return None
    lower = model.strip().lower()
    if not lower:
        return None
    checks: list[tuple[tuple[str, ...], str]] = [
        (("iphone", "ipad", "ipod", "watch"), "apple"),
        (("samsung", "galaxy"), "samsung"),
        (("xiaomi", "redmi", "poco"), "xiaomi"),
        (("huawei",), "huawei"),
        (("honor",), "honor"),
        (("oneplus", "one plus"), "oneplus"),
        (("oppo",), "oppo"),
        (("vivo",), "vivo"),
        (("realme",), "realme"),
        (("nokia",), "nokia"),
        (("meizu", "mblu"), "meizu"),
        (("zte",), "zte"),
        (("sony", "ericsson"), "sony"),
        (("motorola",), "motorola"),
        (("lenovo",), "lenovo"),
        (("asus", "zenfone"), "asus"),
        (("htc",), "htc"),
        (("blackview",), "blackview"),
        (("infinix",), "infinix"),
        (("tecno",), "tecno"),
        (("itel",), "itel"),
        (("oukitel",), "oukitel"),
        (("ulefone",), "ulefone"),
        (("cubot",), "cubot"),
        (("tcl",), "tcl"),
        (("google pixel", "pixel", "google"), "google"),
        (("nothing",), "nothing"),
        (("wiko",), "wiko"),
        (("umidigi",), "umidigi"),
        (("philips",), "philips"),
        (("fly",), "fly"),
        (("siemens",), "siemens"),
        (("explay",), "explay"),
        (("acer", "iconia"), "acer"),
    ]
    for tokens, brand in checks:
        if any(tok in lower for tok in tokens):
            return brand
    return None


def _brand_for_compat(
    item_name: str | None, parsed_brand: str | None, model_name: str | None
) -> str:
    inferred = _infer_brand_from_model(model_name)
    if inferred:
        return inferred
    normalized = _normalize_brand_from_name(item_name, parsed_brand)
    if normalized and normalized != "unknown":
        return normalized
    return "generic"


def _should_canonicalize_competitor_compat(
    parsed_brand: str | None,
    model_name: str | None,
    parse_notes: str | None,
    confidence: float | None,
    raw_name: str | None = None,
    model_variant: str | None = None,
) -> bool:
    brand = (parsed_brand or "").strip().lower()
    if not model_name or not brand or brand == "generic":
        return False
    notes = (parse_notes or "").lower()
    if "ambiguous" in notes or "no keyword" in notes:
        return False
    if confidence is not None and float(confidence) < 0.75:
        return False
    return True


def _extract_device_codes(value: str | None) -> list[str]:
    if not value:
        return []
    patterns = [
        r"\bSM-[A-Z]\d{3,5}[A-Z]?\b",
        r"\b[A-Z]\d{3,5}[A-Z]?\b",
        r"\b\d{6,}[A-Z]{1,3}\b",
        r"\b\d{4,}[A-Z]{1,4}\d[A-Z0-9]*\b",
        r"\b\d{4,}[A-Z]{4,}\b",
    ]
    strict_patterns = [
        r"\b[A-Z]{2}\d{1,4}[A-Z]\b",
        r"\b[A-Z]{3,4}\d{1,2}[A-Z0-9]?\b",
        r"\b[A-Z]{2,4}\d{3,5}[A-Z]?\b",
        r"\bM\d{3,4}[A-Z]\d{1,2}[A-Z]{1,2}\b",
        r"\b[A-Z]{2,4}-[A-Z]{1,2}\d{1,2}[A-Z]?\b",
    ]
    codes: list[str] = []
    for pattern in patterns:
        codes.extend(re.findall(pattern, value, flags=re.IGNORECASE))
    for pattern in strict_patterns:
        codes.extend(re.findall(pattern, value))
    seen = set()
    result: list[str] = []
    for code in codes:
        up = code.upper()
        if up not in seen:
            result.append(up)
            seen.add(up)
    return result


def _model_search_patterns(model_name: str) -> list[str]:
    patterns = [model_name]
    for prefix in ("iphone ", "ipad ", "ipod ", "watch "):
        if model_name.startswith(prefix):
            short = model_name[len(prefix) :].strip()
            if short:
                patterns.append(short)
            break
    if model_name.startswith("galaxy "):
        short = model_name[len("galaxy ") :].strip()
        if short:
            patterns.append(short)
    return patterns


def _pattern_to_regex(pattern: str) -> str:
    tokens = pattern.split()
    if not tokens:
        return ""
    return r"\b" + r"\s*".join(re.escape(tok) for tok in tokens) + r"\b"


def _find_local_device_codes(raw_name: str | None, model_name: str, window: int = 80) -> list[str]:
    if not raw_name:
        return []
    lower_name = raw_name.lower()
    best_match = None
    for pattern in _model_search_patterns(model_name.lower()):
        regex = _pattern_to_regex(pattern)
        if not regex:
            continue
        match = re.search(regex, lower_name)
        if match and (best_match is None or match.start() < best_match.start()):
            best_match = match
    if not best_match:
        return []
    end = best_match.end()
    snippet = lower_name[end : min(len(lower_name), end + window)]
    for paren_match in re.finditer(r"\(([^)]*)\)", snippet):
        codes = _extract_device_codes(paren_match.group(1))
        if codes:
            return codes
    return _extract_device_codes(snippet)


def _local_parentheses_mapping(raw_name: str | None, models: list[str]) -> dict | None:
    if not raw_name:
        return None
    mapping = {}
    for idx, model in enumerate(models):
        codes = _find_local_device_codes(raw_name, model)
        if codes:
            mapping[idx] = codes
    if len(mapping) < 2:
        return None
    assigned = {}
    for idx, codes in mapping.items():
        for code in codes:
            if code in assigned:
                return None
            assigned[code] = idx
    return mapping


def _resolve_model_variants(
    raw_name: str | None,
    brand: str | None,
    models: list[str],
    raw_variant: str | None,
) -> tuple[list[tuple[str | None, str | None]], str]:
    if not models:
        return [], "no_models"
    codes = _extract_device_codes(raw_name)
    if not codes and raw_variant:
        codes = _extract_device_codes(raw_variant)
    if not codes:
        return [(None, None) for _ in models], "no_codes"
    if len(models) == 1:
        return [("/".join(codes), None)], "single_model_all_codes"

    mapping = _local_parentheses_mapping(raw_name, models)
    if mapping is not None:
        result = []
        for idx in range(len(models)):
            codes = mapping.get(idx)
            result.append(("/".join(codes), None) if codes else (None, None))
        return result, "per_model_parentheses"

    if len(codes) == len(models):
        return [(codes[idx], None) for idx in range(len(models))], "by_order"

    notes = f"device_codes={'/'.join(codes)}"
    return [(None, notes) for _ in models], "ambiguous_null"


def _fit_compat_device_variant(value: str | None) -> str | None:
    cleaned = value.strip() if isinstance(value, str) else value
    if not cleaned:
        return None
    if len(cleaned) <= COMPAT_DEVICE_VARIANT_MAX_LEN:
        return cleaned
    return None


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(
        description="Parse/LLM/normalize competitor_item without touching FTP staging."
    )
    parser.add_argument("--source", action="append", help="Filter by competitor (can repeat)")
    parser.add_argument("--name-contains", help="ILIKE filter on name")
    parser.add_argument("--category-contains", help="ILIKE filter on category")
    parser.add_argument(
        "--parsed-brand",
        action="append",
        help="Filter by exact parsed_device_brand value (can repeat)",
    )
    parser.add_argument(
        "--only-generic",
        action="store_true",
        help="Shortcut for --parsed-brand generic",
    )
    parser.add_argument(
        "--compat-brand",
        action="append",
        help="Filter by exact competitor_item_compatibility.device_brand value (can repeat)",
    )
    parser.add_argument(
        "--only-generic-compat",
        action="store_true",
        help="Shortcut for --compat-brand generic",
    )
    parser.add_argument(
        "--parsed-model-contains", help="ILIKE filter on parsed_device_model (to пере-заполнить)"
    )
    parser.add_argument(
        "--missing-parsed",
        action="store_true",
        help="Process only items with empty parsed_device_brand/model",
    )
    parser.add_argument(
        "--only-missing-compat",
        action="store_true",
        help="Process only items without competitor_item_compatibility rows",
    )
    parser.add_argument(
        "--first-seen-after",
        help="Process only items with first_seen_at date >= YYYY-MM-DD",
    )
    parser.add_argument(
        "--last-seen-after",
        help="Process only items with last_seen_at date >= YYYY-MM-DD",
    )
    parser.add_argument("--item-type", help="Process only items with exact item_type")
    parser.add_argument(
        "--missing-compat-brand",
        action="store_true",
        help="Process only items with empty/unknown competitor_item_compatibility.device_brand",
    )
    parser.add_argument(
        "--random-order",
        action="store_true",
        help="Process items in random order (useful for sampling)",
    )
    parser.add_argument(
        "--overwrite", action="store_true", help="Overwrite existing parsed_* even if уже заполнено"
    )
    parser.add_argument(
        "--force-llm", action="store_true", help="Вызывать LLM даже при высокой уверенности парсера"
    )
    parser.add_argument("--limit", type=int, help="Limit items to process")
    parser.add_argument("--llm", action="store_true", help="Use LOCAL_LLM_* for low-conf/ambiguous")
    parser.add_argument("--llm-limit", type=int, default=0, help="Max LLM calls (default 0 = off)")
    parser.add_argument(
        "--llm-threshold", type=float, default=0.7, help="Call LLM if confidence below threshold"
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=1000,
        help="Commit transaction every N processed items (default 1000)",
    )
    args = parser.parse_args()
    first_seen_after = (
        datetime.strptime(args.first_seen_after, "%Y-%m-%d").date()
        if args.first_seen_after
        else None
    )
    last_seen_after = (
        datetime.strptime(args.last_seen_after, "%Y-%m-%d").date() if args.last_seen_after else None
    )

    llm_client = None
    if args.llm or args.llm_limit or args.force_llm:
        llm_client = LlmParseClient.auto()
        if not llm_client.has_providers:
            logging.warning("LLM requested but no local/OpenAI providers are configured")
            llm_client = None
        else:
            logging.info(
                "LLM enabled with provider fallback: providers=%s limit=%s threshold=%.2f force=%s",
                llm_client.provider_names,
                args.llm_limit,
                args.llm_threshold,
                args.force_llm,
            )

    settings = get_settings()
    engine = create_engine(settings.database_url)

    processed = 0
    updated = 0
    llm_used = 0
    llm_skipped_no_client = 0
    llm_skipped_limit = 0

    with Session(engine) as session:
        canonicalizer = PhoneModelCanonicalizer(session)
        query = select(CompetitorItem)
        if args.source:
            query = query.where(CompetitorItem.competitor.in_(args.source))
        if first_seen_after:
            query = query.where(func.date(CompetitorItem.first_seen_at) >= first_seen_after)
        if last_seen_after:
            query = query.where(func.date(CompetitorItem.last_seen_at) >= last_seen_after)
        if args.item_type:
            query = query.where(CompetitorItem.item_type == args.item_type)
        if args.name_contains:
            query = query.where(CompetitorItem.name.ilike(f"%{args.name_contains}%"))
        if args.category_contains:
            query = query.where(CompetitorItem.category.ilike(f"%{args.category_contains}%"))
        parsed_brands = list(args.parsed_brand or [])
        if args.only_generic:
            parsed_brands.append("generic")
        if parsed_brands:
            # Сравниваем case-insensitive (встречается 'Unknown')
            lowered = [str(b).strip().lower() for b in parsed_brands if b is not None]
            query = query.where(func.lower(CompetitorItem.parsed_device_brand).in_(lowered))
        compat_brands = list(args.compat_brand or [])
        if args.only_generic_compat:
            compat_brands.append("generic")
        if compat_brands:
            lowered = [str(b).strip().lower() for b in compat_brands if b is not None]
            query = query.where(
                exists().where(
                    (CompetitorItemCompatibility.competitor_item_id == CompetitorItem.id)
                    & func.lower(CompetitorItemCompatibility.device_brand).in_(lowered)
                )
            )
        if args.parsed_model_contains:
            query = query.where(
                CompetitorItem.parsed_device_model.ilike(f"%{args.parsed_model_contains}%")
            )
        if args.missing_parsed:
            query = query.where(
                (CompetitorItem.parsed_device_brand.is_(None))
                | (CompetitorItem.parsed_device_model.is_(None))
            )
        if args.only_missing_compat:
            query = query.where(
                ~exists().where(CompetitorItemCompatibility.competitor_item_id == CompetitorItem.id)
            )
        if args.missing_compat_brand:
            query = query.where(
                exists().where(
                    (CompetitorItemCompatibility.competitor_item_id == CompetitorItem.id)
                    & (
                        CompetitorItemCompatibility.device_brand.is_(None)
                        | (func.btrim(CompetitorItemCompatibility.device_brand) == "")
                        | (func.lower(CompetitorItemCompatibility.device_brand) == "unknown")
                    )
                )
            )
        if args.random_order:
            query = query.order_by(func.random())
        else:
            query = query.order_by(CompetitorItem.id)
        if args.limit:
            query = query.limit(args.limit)

        items = list(session.execute(query).scalars())
        for item in items:
            processed += 1
            if (
                args.only_missing_compat
                and not competitor_item_requires_compatibility(item).requires_compatibility
            ):
                continue
            parsed = parse_model_name(item.name)
            parsed = _override_iphone_8_se(parsed, item.name)
            used_llm = False
            if parsed and not parsed.models and parsed.model:
                parsed.models = [parsed.model]
            use_llm = (
                llm_client is not None
                and (args.llm_limit == 0 or llm_used < args.llm_limit)
                and (
                    args.force_llm
                    or parsed is None
                    or parsed.ambiguous
                    or parsed.confidence < args.llm_threshold
                )
            )
            if llm_client is None and (args.llm or args.force_llm or args.llm_limit):
                llm_skipped_no_client += 1
            if llm_client is not None and args.llm_limit and llm_used >= args.llm_limit:
                llm_skipped_limit += 1
            if use_llm:
                llm_parsed = None
                try:
                    llm_parsed = llm_client.parse(
                        item.competitor, item.external_id, item.name or ""
                    )
                except Exception:
                    logging.exception(
                        "LLM parse failed",
                        extra={
                            "competitor": item.competitor,
                            "external_id": item.external_id,
                            "item_name": item.name,
                        },
                    )
                    llm_parsed = None
                if llm_parsed:
                    llm_raw_json = llm_parsed.llm_raw_json
                    llm_parsed.brand = _normalize_brand_from_name(item.name, llm_parsed.brand)
                    if llm_parsed.items:
                        raw_name = item.name or ""
                        raw_lower = raw_name.lower()
                        paren_codes = _codes_from_parentheses(raw_name)
                        for entry in llm_parsed.items:
                            if entry.codes:
                                filtered: list[str] = []
                                for code in entry.codes:
                                    up = code.upper()
                                    if up in _CODE_BLACKLIST:
                                        continue
                                    if paren_codes and up not in paren_codes:
                                        continue
                                    if up.lower() not in raw_lower:
                                        continue
                                    filtered.append(up)
                                entry.codes = filtered
                            if entry.model and not _llm_model_valid(
                                entry.model, raw_name, llm_parsed.brand
                            ):
                                entry.model = None
                        llm_parsed.items = [entry for entry in llm_parsed.items if entry.model]
                    llm_parsed = _override_iphone_8_se(llm_parsed, item.name)
                    llm_parsed, llm_note = _sanitize_llm_models(llm_parsed, item_name=item.name)
                    item.llm_model = llm_parsed.llm_model_name or llm_client.model
                    item.llm_raw_json = llm_raw_json
                    item.parse_version = "llm_parse_v2"
                    if llm_parsed:
                        parsed = llm_parsed
                        llm_used += 1
                        used_llm = True
                        item.parse_status = CompetitorItemParseStatus.OK
                        item.parse_error = None
                        logging.info(
                            "LLM parsed item",
                            extra={
                                "competitor": item.competitor,
                                "external_id": item.external_id,
                                "brand": llm_parsed.brand,
                                "model": llm_parsed.model,
                                "variant": llm_parsed.variant,
                            },
                        )
                    else:
                        item.parse_status = CompetitorItemParseStatus.CONFLICT
                        item.parse_error = llm_note
                        if llm_note == "llm_blocked_wearable":
                            item.parsed_device_brand = None
                            item.parsed_device_model = None
                            item.parsed_device_variant = None
                            item.parse_confidence = None
                        current_notes = item.parse_notes or ""
                        item.parse_notes = f"{current_notes}; {llm_note}".strip("; ")

            if not parsed:
                continue

            # Если парсер не определил бренд, пробуем опереться на уже сохранённый parsed_device_brand.
            # Это позволяет корректно конвертировать legacy "Unknown" -> generic и при наличии бренда в названии.
            parsed.brand = _normalize_brand_from_name(
                item.name, parsed.brand or item.parsed_device_brand
            )
            changed = False
            if parsed.brand and (args.overwrite or item.parsed_device_brand != parsed.brand):
                item.parsed_device_brand = parsed.brand
                changed = True
            primary_model = parsed.models[0] if parsed.models else parsed.model
            if primary_model and (args.overwrite or item.parsed_device_model != primary_model):
                item.parsed_device_model = primary_model
                changed = True
            if parsed.variant and (args.overwrite or item.parsed_device_variant != parsed.variant):
                item.parsed_device_variant = parsed.variant
                changed = True
            parse_succeeded = bool(primary_model or parsed.items) and not parsed.ambiguous
            if parse_succeeded and item.parse_status != CompetitorItemParseStatus.OK:
                item.parse_status = CompetitorItemParseStatus.OK
                changed = True
            if parse_succeeded and item.parse_error is not None:
                item.parse_error = None
                changed = True
            if parsed.confidence is not None:
                if (
                    args.overwrite
                    or item.parse_confidence is None
                    or float(parsed.confidence) > float(item.parse_confidence)
                ):
                    item.parse_confidence = parsed.confidence
                    changed = True
            notes = parsed.reason or None
            if parsed.ambiguous:
                notes = f"{notes}; ambiguous" if notes else "ambiguous"
            if used_llm:
                notes = f"{notes}; llm" if notes else "llm"
            if notes and (args.overwrite or item.parse_notes != notes):
                item.parse_notes = notes
                changed = True

            # Обновляем таблицу совместимостей
            if parsed.items:
                if args.overwrite:
                    session.query(CompetitorItemCompatibility).filter(
                        CompetitorItemCompatibility.competitor_item_id == item.id
                    ).delete(synchronize_session=False)
                    changed = True
                has_multiple_items = len(parsed.items) > 1
                for entry in parsed.items:
                    model_name = entry.model
                    if not model_name:
                        continue
                    item_brand = _brand_for_compat(
                        item.name,
                        parsed.brand or item.parsed_device_brand,
                        model_name,
                    )
                    codes = entry.codes or (
                        [] if has_multiple_items else _extract_device_codes(item.name)
                    )
                    if has_multiple_items and codes and len(codes) > 1:
                        codes = []
                    model_variant = _fit_compat_device_variant("/".join(codes) if codes else None)
                    existing_compat = (
                        session.query(CompetitorItemCompatibility)
                        .filter(
                            CompetitorItemCompatibility.competitor_item_id == item.id,
                            CompetitorItemCompatibility.device_brand == item_brand,
                            CompetitorItemCompatibility.device_model == model_name,
                            CompetitorItemCompatibility.device_variant == model_variant,
                        )
                        .first()
                    )
                    if existing_compat:
                        if (
                            existing_compat.phone_model_id is None
                            and _should_canonicalize_competitor_compat(
                                item_brand,
                                model_name,
                                item.parse_notes,
                                parsed.confidence,
                                raw_name=item.name,
                                model_variant=model_variant,
                            )
                        ):
                            canonical = canonicalizer.canonicalize(
                                source="competitor_parser",
                                raw_value=item.name,
                                brand=item_brand,
                                model_name=model_name,
                                variant=model_variant,
                                confidence=parsed.confidence,
                            )
                            if canonical.phone_model:
                                existing_compat.phone_model_id = canonical.phone_model.id
                            note = canonical.reason if canonical else None
                            if note:
                                existing_compat.notes = note
                            session.add(existing_compat)
                        continue
                    canonical = None
                    if _should_canonicalize_competitor_compat(
                        item_brand,
                        model_name,
                        item.parse_notes,
                        parsed.confidence,
                        raw_name=item.name,
                        model_variant=model_variant,
                    ):
                        canonical = canonicalizer.canonicalize(
                            source="competitor_parser",
                            raw_value=item.name,
                            brand=item_brand,
                            model_name=model_name,
                            variant=model_variant,
                            confidence=parsed.confidence,
                        )
                    comp = CompetitorItemCompatibility(
                        competitor_item_id=item.id,
                        phone_model_id=(
                            canonical.phone_model.id
                            if canonical and canonical.phone_model
                            else None
                        ),
                        device_brand=item_brand,
                        device_model=model_name,
                        device_variant=model_variant,
                        source="llm" if used_llm else "parser",
                        notes=(canonical.reason if canonical else None),
                    )
                    session.add(comp)
                    changed = True
            elif parsed.models:
                brand_for_mapping = _brand_for_compat(
                    item.name,
                    parsed.brand or item.parsed_device_brand,
                    parsed.models[0] if parsed.models else parsed.model,
                )
                model_variants, strategy = _resolve_model_variants(
                    item.name,
                    brand_for_mapping,
                    parsed.models,
                    parsed.variant,
                )
                shared_codes = _extract_device_codes(parsed.variant)
                if parsed.variant and len(parsed.models) > 1 and len(shared_codes) > 1:
                    shared_variant = parsed.variant.strip()
                    model_variants = [
                        (None, f"shared_variant={shared_variant}") for _ in parsed.models
                    ]
                    strategy = "shared_variant_null"
                codes_count = len(_extract_device_codes(item.name))
                logger.debug(
                    "compatibility mapping item=%s/%s models=%s device_codes=%s strategy=%s",
                    item.competitor,
                    item.external_id,
                    len(parsed.models),
                    codes_count,
                    strategy,
                )
                if strategy in {"ambiguous_null", "shared_variant_null"} and model_variants:
                    logger.debug(
                        "compatibility mapping ambiguous item=%s/%s notes=%s",
                        item.competitor,
                        item.external_id,
                        model_variants[0][1],
                    )
                if args.overwrite:
                    session.query(CompetitorItemCompatibility).filter(
                        CompetitorItemCompatibility.competitor_item_id == item.id
                    ).delete(synchronize_session=False)
                    changed = True
                for model_index, model_name in enumerate(parsed.models):
                    if not model_name:
                        continue
                    item_brand = _brand_for_compat(
                        item.name,
                        parsed.brand or item.parsed_device_brand,
                        model_name,
                    )
                    if model_index < len(model_variants):
                        model_variant, model_notes = model_variants[model_index]
                    else:
                        model_variant, model_notes = None, None
                    model_variant = _fit_compat_device_variant(model_variant)
                    existing_compat = (
                        session.query(CompetitorItemCompatibility)
                        .filter(
                            CompetitorItemCompatibility.competitor_item_id == item.id,
                            CompetitorItemCompatibility.device_brand == item_brand,
                            CompetitorItemCompatibility.device_model == model_name,
                            CompetitorItemCompatibility.device_variant == model_variant,
                        )
                        .first()
                    )
                    if existing_compat:
                        if (
                            existing_compat.phone_model_id is None
                            and _should_canonicalize_competitor_compat(
                                item_brand,
                                model_name,
                                item.parse_notes,
                                parsed.confidence,
                                raw_name=item.name,
                                model_variant=model_variant,
                            )
                        ):
                            canonical = canonicalizer.canonicalize(
                                source="competitor_parser",
                                raw_value=item.name,
                                brand=item_brand,
                                model_name=model_name,
                                variant=model_variant,
                                confidence=parsed.confidence,
                            )
                            if canonical.phone_model:
                                existing_compat.phone_model_id = canonical.phone_model.id
                            existing_compat.notes = canonical.reason
                            session.add(existing_compat)
                        continue
                    canonical = None
                    if _should_canonicalize_competitor_compat(
                        item_brand,
                        model_name,
                        item.parse_notes,
                        parsed.confidence,
                        raw_name=item.name,
                        model_variant=model_variant,
                    ):
                        canonical = canonicalizer.canonicalize(
                            source="competitor_parser",
                            raw_value=item.name,
                            brand=item_brand,
                            model_name=model_name,
                            variant=model_variant,
                            confidence=parsed.confidence,
                        )
                    comp = CompetitorItemCompatibility(
                        competitor_item_id=item.id,
                        phone_model_id=(
                            canonical.phone_model.id
                            if canonical and canonical.phone_model
                            else None
                        ),
                        device_brand=item_brand,
                        device_model=model_name,
                        device_variant=model_variant,
                        source="llm" if used_llm else "parser",
                        notes=(
                            f"{model_notes}; {canonical.reason}"
                            if model_notes and canonical and canonical.reason
                            else model_notes or (canonical.reason if canonical else None)
                        ),
                    )
                    session.add(comp)
                    changed = True
            elif parsed.model:
                brand_for_mapping = _brand_for_compat(
                    item.name,
                    parsed.brand or item.parsed_device_brand,
                    parsed.model,
                )
                extracted_codes = _extract_device_codes(item.name)
                variant_codes = _extract_device_codes(parsed.variant)
                model_variant = _fit_compat_device_variant(
                    "/".join(extracted_codes or variant_codes)
                    if (extracted_codes or variant_codes)
                    else parsed.variant
                )
                if args.overwrite:
                    session.query(CompetitorItemCompatibility).filter(
                        CompetitorItemCompatibility.competitor_item_id == item.id
                    ).delete(synchronize_session=False)
                    changed = True
                existing_compat = (
                    session.query(CompetitorItemCompatibility)
                    .filter(
                        CompetitorItemCompatibility.competitor_item_id == item.id,
                        CompetitorItemCompatibility.device_brand == brand_for_mapping,
                        CompetitorItemCompatibility.device_model == parsed.model,
                        CompetitorItemCompatibility.device_variant == model_variant,
                    )
                    .first()
                )
                canonical = None
                if _should_canonicalize_competitor_compat(
                    brand_for_mapping,
                    parsed.model,
                    item.parse_notes,
                    parsed.confidence,
                    raw_name=item.name,
                    model_variant=model_variant,
                ):
                    canonical = canonicalizer.canonicalize(
                        source="competitor_parser",
                        raw_value=item.name,
                        brand=brand_for_mapping,
                        model_name=parsed.model,
                        variant=model_variant,
                        confidence=parsed.confidence,
                    )
                if existing_compat:
                    if canonical and canonical.phone_model:
                        existing_compat.phone_model_id = canonical.phone_model.id
                    if canonical and canonical.reason:
                        existing_compat.notes = canonical.reason
                    session.add(existing_compat)
                else:
                    comp = CompetitorItemCompatibility(
                        competitor_item_id=item.id,
                        phone_model_id=(
                            canonical.phone_model.id
                            if canonical and canonical.phone_model
                            else None
                        ),
                        device_brand=brand_for_mapping,
                        device_model=parsed.model,
                        device_variant=model_variant,
                        source="llm" if used_llm else "parser",
                        notes=(canonical.reason if canonical else None),
                    )
                    session.add(comp)
                changed = True
            else:
                # Если модель не извлеклась, но у позиции уже есть compatibility-строки,
                # приводим пустой/unknown brand к вычисленному значению.
                existing_compats = (
                    session.query(CompetitorItemCompatibility)
                    .filter(CompetitorItemCompatibility.competitor_item_id == item.id)
                    .all()
                )
                for comp in existing_compats:
                    current = (comp.device_brand or "").strip().lower()
                    if current and current != "unknown":
                        continue
                    computed_brand = _brand_for_compat(
                        item.name,
                        parsed.brand or item.parsed_device_brand,
                        comp.device_model,
                    )
                    if comp.device_brand != computed_brand:
                        comp.device_brand = computed_brand
                        session.add(comp)
                        changed = True

            if changed:
                updated += 1
                session.add(item)
                logging.info(
                    "parsed item %s/%s -> brand=%s model=%s variant=%s via=%s",
                    item.competitor,
                    item.external_id,
                    parsed.brand if parsed else None,
                    parsed.models if parsed else None,
                    parsed.variant if parsed else None,
                    "llm" if used_llm else "parser",
                )
            if args.batch_size > 0 and processed % args.batch_size == 0:
                session.commit()
        session.commit()

    if llm_client is not None:
        llm_client.close()

    print(
        json.dumps(
            {
                "processed": processed,
                "updated": updated,
                "llm_calls_used": llm_used,
                "llm_skipped_no_client": llm_skipped_no_client,
                "llm_skipped_limit": llm_skipped_limit,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
