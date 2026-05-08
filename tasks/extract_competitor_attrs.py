"""LLM-экстракция item_type/attrs/normalized_title для competitor_item."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from datetime import date, datetime, timezone
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import create_engine, func, or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.competitor_item import CompetitorItem, CompetitorItemParseStatus
from app.services.display_normalization import (
    normalize_display_construction,
    normalize_display_quality,
    normalize_display_type,
    normalize_refresh_rate_hz,
)
from app.services.display_parser import (
    SCREEN_CONSTRUCTION_RU,
    SCREEN_MATRIX_TYPE_RU,
    SCREEN_QUALITY_RU,
    parse_display_attributes,
)
from app.services.prompts import get_llm_competitor_attrs_prompt
from tasks.normalize_competitor_item_type import rule_classify

ITEM_TYPES = {
    "display",
    "battery",
    "camera",
    "flex",
    "housing",
    "connector",
    "cable",
    "board",
    "other",
}

REPAIR_PROMPT = """
Исправь JSON. Верни строго валидный JSON без пояснений.
""".strip()


class LlmAttrsPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    item_type: str
    normalized_title: str | None = None
    attrs: dict[str, Any] | None = None
    confidence: float | None = None
    uncertain_fields: list[str] | None = None


def _prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def _extract_content(data: dict[str, Any]) -> str:
    return data["choices"][0]["message"]["content"]


def _parse_json(raw: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            return None
        return parsed
    except json.JSONDecodeError:
        return None


def _llm_request(
    client: httpx.Client,
    base_url: str,
    model: str,
    prompt: str,
    content: str,
) -> str:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": content},
        ],
        "temperature": 0.0,
        "max_tokens": 400,
    }
    resp = client.post(f"{base_url}/v1/chat/completions", json=payload, timeout=30.0)
    resp.raise_for_status()
    return _extract_content(resp.json())


def _normalize_attrs(attrs: Any, uncertain_fields: Any) -> dict[str, Any]:
    attrs_dict: dict[str, Any] = {}
    if isinstance(attrs, dict):
        attrs_dict.update(attrs)
    if isinstance(uncertain_fields, list):
        attrs_dict["_uncertain_fields"] = [str(x) for x in uncertain_fields if x is not None]
    return attrs_dict


def _normalize_attr_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        parts = [str(part).strip() for part in value if part is not None]
        text = ", ".join([part for part in parts if part])
    else:
        text = str(value).strip()
    if not text:
        return None
    return " ".join(text.split())


def _normalize_display_attrs(attrs: Any) -> Any:
    if not isinstance(attrs, dict):
        return attrs
    normalized = dict(attrs)
    type_value = normalized.get("type")
    if type_value:
        normalized_type = normalize_display_type(type_value)
        if normalized_type:
            normalized["type"] = normalized_type
    quality_value = normalized.get("quality")
    if quality_value:
        normalized_quality = normalize_display_quality(quality_value)
        if normalized_quality:
            normalized["quality"] = normalized_quality
    construction_value = normalized.get("construction")
    if construction_value:
        normalized_construction = normalize_display_construction(construction_value)
        if normalized_construction:
            normalized["construction"] = normalized_construction
    refresh_value = normalized.get("refresh_rate_hz")
    if refresh_value is not None:
        normalized_refresh = normalize_refresh_rate_hz(refresh_value)
        if normalized_refresh is not None:
            normalized["refresh_rate_hz"] = normalized_refresh
    return normalized


def _is_display_item(item: CompetitorItem, item_type: str | None) -> bool:
    if item_type == "display":
        return True
    if item.item_type == "display":
        return True
    if item.category and "дисплей" in item.category.lower():
        return True
    if item.name and "дисплей" in item.name.lower():
        return True
    if item.normalized_title and "дисплей" in item.normalized_title.lower():
        return True
    return False


def _should_update(
    item: CompetitorItem,
    overwrite: bool,
    rerun_errors: bool,
    min_confidence_bump: float,
    new_confidence: float | None,
) -> bool:
    if overwrite:
        return True
    if item.processed_at is None:
        return True
    if rerun_errors and item.parse_status in {
        CompetitorItemParseStatus.INVALID_JSON,
        CompetitorItemParseStatus.TIMEOUT,
    }:
        return True
    if item.llm_confidence is not None and new_confidence is not None:
        return float(new_confidence) >= float(item.llm_confidence) + min_confidence_bump
    return False


def _apply_display_result(
    item: CompetitorItem,
    attrs_dict: dict[str, Any],
    display_result,
) -> None:
    attrs_dict.update(display_result.to_attrs())
    if display_result.screen_matrix_type.value != "UNKNOWN":
        attrs_dict["type"] = SCREEN_MATRIX_TYPE_RU[display_result.screen_matrix_type]
    if display_result.screen_quality_grade.value != "UNKNOWN":
        attrs_dict["quality"] = SCREEN_QUALITY_RU[display_result.screen_quality_grade]
    if display_result.screen_construction.value != "UNKNOWN":
        attrs_dict["construction"] = SCREEN_CONSTRUCTION_RU[display_result.screen_construction]
    if display_result.refresh_rate_hz is not None:
        attrs_dict["refresh_rate_hz"] = display_result.refresh_rate_hz
    if display_result.color:
        attrs_dict["color"] = display_result.color
    if display_result.screen_matrix_type.value != "UNKNOWN" or item.screen_matrix_type is None:
        item.screen_matrix_type = display_result.screen_matrix_type.value
    if display_result.screen_kit.value != "UNKNOWN" or item.screen_kit is None:
        item.screen_kit = display_result.screen_kit.value
    if display_result.backlight.value != "UNKNOWN" or item.backlight is None:
        item.backlight = display_result.backlight.value
    if display_result.screen_construction.value != "UNKNOWN" or item.screen_construction is None:
        item.screen_construction = display_result.screen_construction.value
    if display_result.screen_quality_grade.value != "UNKNOWN" or item.screen_quality_grade is None:
        item.screen_quality_grade = display_result.screen_quality_grade.value
    if display_result.refresh_rate_hz is not None:
        item.refresh_rate_hz = display_result.refresh_rate_hz
    if display_result.oleophobic is not None:
        item.oleophobic = display_result.oleophobic
    if display_result.has_frame is not None:
        item.has_frame = display_result.has_frame
    if display_result.has_touch is not None:
        item.has_touch = display_result.has_touch
    if display_result.has_ic_pad is not None:
        item.has_ic_pad = display_result.has_ic_pad
    if display_result.has_binding_no_solder is not None:
        item.has_binding_no_solder = display_result.has_binding_no_solder
    if display_result.manufacturer is not None:
        item.item_manufacturer = display_result.manufacturer
    if display_result.matrix_tags:
        item.matrix_tags = display_result.matrix_tags
    if display_result.color is not None:
        item.color = display_result.color
    if display_result.notes_raw_tokens:
        item.notes_raw_tokens = display_result.notes_raw_tokens


def _parser_only_attrs(item: CompetitorItem) -> dict[str, Any] | None:
    if not item.name:
        return None
    item_type = item.item_type or rule_classify(item.name) or "other"
    attrs: dict[str, Any] = {"item_type": item_type}
    if item_type:
        item.item_type = item_type
    if not item.normalized_title:
        item.normalized_title = " ".join(item.name.split())
    if _is_display_item(item, item_type):
        display_result = parse_display_attributes(item.name)
        _apply_display_result(item, attrs, display_result)
    return attrs


def extract_attrs(
    session: Session,
    *,
    source: str | None,
    category: str | None,
    name_contains: str | None,
    first_seen_date: date | None,
    first_seen_after: date | None,
    only_null: bool,
    only_bad: bool,
    only_parse_version_missing: bool,
    overwrite: bool,
    rerun_errors: bool,
    limit: int | None,
    offset: int | None,
    min_llm_confidence: float,
    min_confidence_bump: float,
    repair_attempts: int,
    llm_timeout: float,
    dry_run: bool,
    parse_version: str,
    sample_limit: int,
    samples_file: str | None,
    parser_only: bool = False,
) -> dict[str, int]:
    query = select(CompetitorItem).order_by(CompetitorItem.id)
    if source:
        query = query.where(CompetitorItem.competitor == source)
    if category:
        query = query.where(CompetitorItem.category == category)
    if name_contains:
        query = query.where(CompetitorItem.name.ilike(f"%{name_contains}%"))
    if first_seen_date:
        query = query.where(func.date(CompetitorItem.first_seen_at) == first_seen_date)
    if first_seen_after:
        query = query.where(func.date(CompetitorItem.first_seen_at) >= first_seen_after)
    if only_bad:
        query = query.where(
            (CompetitorItem.processed_at.is_(None))
            | (CompetitorItem.parse_status.is_(None))
            | (CompetitorItem.parse_status != CompetitorItemParseStatus.OK)
            | (CompetitorItem.llm_confidence.is_(None))
            | (CompetitorItem.llm_confidence < min_llm_confidence)
        )
    if only_parse_version_missing:
        query = query.where(
            or_(
                CompetitorItem.parse_version.is_(None),
                CompetitorItem.parse_version != parse_version,
            )
        )
    if only_null:
        query = query.where(CompetitorItem.processed_at.is_(None))
    if offset:
        query = query.offset(offset)
    if limit:
        query = query.limit(limit)

    items = list(session.execute(query).scalars())

    stats = {
        "processed": 0,
        "updated": 0,
        "skipped_not_improved": 0,
        "invalid_json": 0,
        "timeout": 0,
        "low_confidence": 0,
        "conflict": 0,
    }
    samples: dict[str, list] = {
        "invalid_json": [],
        "timeout": [],
        "low_confidence": [],
        "conflict": [],
    }

    if parser_only:
        now = datetime.now(timezone.utc)
        for item in items:
            if not item.name:
                continue
            stats["processed"] += 1
            attrs = _parser_only_attrs(item)
            if attrs is None:
                continue
            if dry_run:
                stats["updated"] += 1
                continue
            item.attrs_json = _normalize_attrs(attrs, None)
            item.parse_status = CompetitorItemParseStatus.OK
            item.parse_error = None
            item.processed_at = now
            item.parse_version = parse_version
            session.add(item)
            stats["updated"] += 1
        if not dry_run:
            session.commit()
        return stats

    base_url = os.environ.get("LOCAL_LLM_BASE_URL")
    model = os.environ.get("LOCAL_LLM_CHAT_MODEL")
    if not base_url or not model:
        raise RuntimeError("LOCAL_LLM_BASE_URL и LOCAL_LLM_CHAT_MODEL должны быть заданы")

    prompt = get_llm_competitor_attrs_prompt()
    prompt_hash = _prompt_hash(prompt)

    with httpx.Client(timeout=llm_timeout) as client:
        for item in items:
            if not item.name:
                continue
            stats["processed"] += 1
            raw_response = None
            parsed = None
            error_status = None
            error_message = None

            content = f"Source: {item.competitor} SKU: {item.external_id}\nName: {item.name}"
            try:
                raw_response = _llm_request(client, base_url, model, prompt, content)
                parsed = _parse_json(raw_response)
                if parsed is None and repair_attempts > 0:
                    repair_payload = {
                        "model": model,
                        "messages": [
                            {"role": "system", "content": REPAIR_PROMPT},
                            {"role": "user", "content": raw_response},
                        ],
                        "temperature": 0.0,
                        "max_tokens": 400,
                    }
                    resp = client.post(f"{base_url}/v1/chat/completions", json=repair_payload)
                    resp.raise_for_status()
                    raw_response = _extract_content(resp.json())
                    parsed = _parse_json(raw_response)
            except httpx.TimeoutException as exc:
                error_status = CompetitorItemParseStatus.TIMEOUT
                error_message = str(exc)
                stats["timeout"] += 1
            except Exception as exc:  # noqa: BLE001
                error_status = CompetitorItemParseStatus.TIMEOUT
                error_message = str(exc)
                stats["timeout"] += 1

            if parsed is None and error_status is None:
                error_status = CompetitorItemParseStatus.INVALID_JSON
                error_message = "invalid_json"
                stats["invalid_json"] += 1

            now = datetime.now(timezone.utc)
            if parsed is None:
                if not _should_update(item, overwrite, rerun_errors, min_confidence_bump, None):
                    stats["skipped_not_improved"] += 1
                    continue
                if not dry_run:
                    item.llm_raw_json = raw_response
                    item.parse_status = error_status
                    item.parse_error = error_message
                    item.processed_at = now
                    item.llm_model = model
                    item.prompt_hash = prompt_hash
                    item.parse_version = parse_version
                    session.add(item)
                if (
                    error_status == CompetitorItemParseStatus.INVALID_JSON
                    and len(samples["invalid_json"]) < sample_limit
                ):
                    samples["invalid_json"].append(
                        {
                            "competitor": item.competitor,
                            "external_id": item.external_id,
                            "name": item.name,
                            "parse_error": error_message,
                        }
                    )
                if (
                    error_status == CompetitorItemParseStatus.TIMEOUT
                    and len(samples["timeout"]) < sample_limit
                ):
                    samples["timeout"].append(
                        {
                            "competitor": item.competitor,
                            "external_id": item.external_id,
                            "name": item.name,
                            "parse_error": error_message,
                        }
                    )
                continue

            try:
                validated = LlmAttrsPayload.model_validate(parsed)
            except ValidationError as exc:
                error_status = CompetitorItemParseStatus.INVALID_JSON
                error_message = f"validation_error: {exc.errors()[0]['msg']}"
                stats["invalid_json"] += 1
                validated = None

            if validated is None:
                if not _should_update(item, overwrite, rerun_errors, min_confidence_bump, None):
                    stats["skipped_not_improved"] += 1
                    continue
                if not dry_run:
                    item.llm_raw_json = raw_response
                    item.parse_status = error_status
                    item.parse_error = error_message
                    item.processed_at = now
                    item.llm_model = model
                    item.prompt_hash = prompt_hash
                    item.parse_version = parse_version
                    session.add(item)
                continue

            item_type = validated.item_type
            normalized_title = validated.normalized_title
            attrs = _normalize_display_attrs(validated.attrs)
            confidence = validated.confidence
            uncertain_fields = validated.uncertain_fields

            if item_type not in ITEM_TYPES:
                error_status = CompetitorItemParseStatus.CONFLICT
                error_message = f"invalid item_type: {item_type}"
                stats["conflict"] += 1
                if len(samples["conflict"]) < sample_limit:
                    samples["conflict"].append(
                        {
                            "competitor": item.competitor,
                            "external_id": item.external_id,
                            "name": item.name,
                            "item_type": item_type,
                        }
                    )
            elif confidence is not None and float(confidence) < min_llm_confidence:
                error_status = CompetitorItemParseStatus.LOW_CONFIDENCE
                stats["low_confidence"] += 1
                if len(samples["low_confidence"]) < sample_limit:
                    samples["low_confidence"].append(
                        {
                            "competitor": item.competitor,
                            "external_id": item.external_id,
                            "name": item.name,
                            "confidence": confidence,
                        }
                    )
            else:
                error_status = CompetitorItemParseStatus.OK

            if not _should_update(item, overwrite, rerun_errors, min_confidence_bump, confidence):
                stats["skipped_not_improved"] += 1
                continue

            if dry_run:
                continue

            if item_type in ITEM_TYPES:
                item.item_type = item_type
            if normalized_title:
                item.normalized_title = normalized_title
            display_result = None
            if _is_display_item(item, item_type):
                display_name = item.name or normalized_title or ""
                if display_name:
                    display_result = parse_display_attributes(
                        display_name,
                        llm_attrs=attrs if isinstance(attrs, dict) else None,
                        llm_output=raw_response,
                    )
                    if attrs is None:
                        attrs = {}
                    if isinstance(attrs, dict):
                        _apply_display_result(item, attrs, display_result)
            attrs_dict = attrs if isinstance(attrs, dict) else None
            if attrs_dict is None and display_result is not None:
                attrs_dict = display_result.to_attrs()
                attrs = attrs_dict
            if attrs_dict is not None or isinstance(uncertain_fields, list):
                item.attrs_json = _normalize_attrs(attrs_dict, uncertain_fields)
                if attrs_dict is not None:
                    value = _normalize_attr_value(
                        attrs_dict.get("item_brand") or attrs_dict.get("brand")
                    )
                    if value is not None:
                        item.item_brand = value
                    value = _normalize_attr_value(attrs_dict.get("model"))
                    if value is not None:
                        item.attrs_model = value
                    value = _normalize_attr_value(attrs_dict.get("variant"))
                    if value is not None:
                        item.attrs_variant = value
                    value = _normalize_attr_value(attrs_dict.get("color"))
                    if value is not None:
                        item.attrs_color = value
                    value = _normalize_attr_value(attrs_dict.get("capacity"))
                    if value is not None:
                        item.attrs_capacity = value
                    value = _normalize_attr_value(attrs_dict.get("size_inch"))
                    if value is not None:
                        item.attrs_size_inch = value
                    value = _normalize_attr_value(attrs_dict.get("type"))
                    if value is not None:
                        item.attrs_type = value
                    value = _normalize_attr_value(attrs_dict.get("quality"))
                    if value is not None:
                        item.attrs_quality = value
                    value = _normalize_attr_value(attrs_dict.get("construction"))
                    if value is not None:
                        item.attrs_construction = value
                    refresh_rate = attrs_dict.get("refresh_rate_hz")
                    normalized_rate = normalize_refresh_rate_hz(refresh_rate)
                    if normalized_rate is not None:
                        item.attrs_refresh_rate_hz = normalized_rate
            if confidence is not None:
                item.llm_confidence = float(confidence)
            item.llm_raw_json = raw_response
            item.parse_status = error_status
            item.parse_error = error_message
            item.processed_at = now
            item.llm_model = model
            item.prompt_hash = prompt_hash
            item.parse_version = parse_version
            session.add(item)
            stats["updated"] += 1

    if not dry_run:
        session.commit()
    if samples_file:
        payload = {"stats": stats, "samples": samples}
        with open(samples_file, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
    return stats


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="LLM-extract competitor item attributes.")
    parser.add_argument("--source", help="Filter by competitor")
    parser.add_argument("--category", help="Filter by competitor category")
    parser.add_argument("--name-contains", help="ILIKE filter on name")
    parser.add_argument("--first-seen-date", help="Filter items by first_seen_at date (YYYY-MM-DD)")
    parser.add_argument(
        "--first-seen-after", help="Filter items by first_seen_at date >= YYYY-MM-DD"
    )
    parser.add_argument(
        "--only-null", action="store_true", help="Process only processed_at is null (default)"
    )
    parser.add_argument(
        "--only-bad", action="store_true", help="Process parse_status!=ok or low confidence"
    )
    parser.add_argument(
        "--only-parse-version-missing",
        action="store_true",
        help="Process only rows where parse_version differs from --parse-version",
    )
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing fields")
    parser.add_argument(
        "--rerun-errors", action="store_true", help="Retry invalid_json/timeout statuses"
    )
    parser.add_argument("--limit", type=int, help="Limit records")
    parser.add_argument("--batch-size", type=int, help="Process records in batches")
    parser.add_argument("--batch-offset", type=int, default=0, help="Start offset for batching")
    parser.add_argument("--min-llm-confidence", type=float, default=None, help="Min LLM confidence")
    parser.add_argument(
        "--min-confidence-bump", type=float, default=0.1, help="Overwrite if confidence grows by X"
    )
    parser.add_argument(
        "--repair-attempts", type=int, default=1, help="Repair attempts for invalid JSON"
    )
    parser.add_argument("--llm-timeout", type=float, default=30.0, help="LLM timeout")
    parser.add_argument("--dry-run", action="store_true", help="Do not write to DB")
    parser.add_argument("--parse-version", default="v1", help="Parse version label")
    parser.add_argument("--parser-only", action="store_true", help="Use deterministic parsers only")
    parser.add_argument("--no-llm", action="store_true", help="Alias for --parser-only")
    parser.add_argument("--sample-limit", type=int, default=10, help="Sample size for errors")
    parser.add_argument("--samples-file", help="Write samples JSON to file")
    args = parser.parse_args()

    settings = get_settings()
    first_seen_date = None
    first_seen_after = None
    if args.first_seen_date:
        first_seen_date = datetime.strptime(args.first_seen_date, "%Y-%m-%d").date()
    if args.first_seen_after:
        first_seen_after = datetime.strptime(args.first_seen_after, "%Y-%m-%d").date()
    min_conf = (
        args.min_llm_confidence
        if args.min_llm_confidence is not None
        else settings.matching_min_llm_confidence
    )

    engine = create_engine(settings.database_url)
    total_stats = {
        "processed": 0,
        "updated": 0,
        "skipped_not_improved": 0,
        "invalid_json": 0,
        "timeout": 0,
        "low_confidence": 0,
        "conflict": 0,
    }
    with Session(engine) as session:
        if args.batch_size:
            remaining = args.limit
            offset = args.batch_offset
            batch_index = 0
            while True:
                batch_index += 1
                current_limit = (
                    args.batch_size if remaining is None else min(args.batch_size, remaining)
                )
                stats = extract_attrs(
                    session,
                    source=args.source,
                    category=args.category,
                    name_contains=args.name_contains,
                    first_seen_date=first_seen_date,
                    first_seen_after=first_seen_after,
                    only_null=args.only_null or not args.overwrite,
                    only_bad=args.only_bad,
                    only_parse_version_missing=args.only_parse_version_missing,
                    overwrite=args.overwrite,
                    rerun_errors=args.rerun_errors,
                    limit=current_limit,
                    offset=offset,
                    min_llm_confidence=min_conf,
                    min_confidence_bump=args.min_confidence_bump,
                    repair_attempts=args.repair_attempts,
                    llm_timeout=args.llm_timeout,
                    dry_run=args.dry_run,
                    parse_version=args.parse_version,
                    sample_limit=args.sample_limit,
                    samples_file=args.samples_file,
                    parser_only=args.parser_only or args.no_llm,
                )
                for key in total_stats:
                    total_stats[key] += stats.get(key, 0)
                if stats.get("processed", 0) == 0:
                    break
                if remaining is not None:
                    remaining -= current_limit
                    if remaining <= 0:
                        break
                offset += current_limit
                if stats.get("processed", 0) < current_limit:
                    break
        else:
            stats = extract_attrs(
                session,
                source=args.source,
                category=args.category,
                name_contains=args.name_contains,
                first_seen_date=first_seen_date,
                first_seen_after=first_seen_after,
                only_null=args.only_null or not args.overwrite,
                only_bad=args.only_bad,
                only_parse_version_missing=args.only_parse_version_missing,
                overwrite=args.overwrite,
                rerun_errors=args.rerun_errors,
                limit=args.limit,
                offset=None,
                min_llm_confidence=min_conf,
                min_confidence_bump=args.min_confidence_bump,
                repair_attempts=args.repair_attempts,
                llm_timeout=args.llm_timeout,
                dry_run=args.dry_run,
                parse_version=args.parse_version,
                sample_limit=args.sample_limit,
                samples_file=args.samples_file,
                parser_only=args.parser_only or args.no_llm,
            )
            total_stats = stats
    print(json.dumps(total_stats, ensure_ascii=False, indent=2))
    logging.info("extract_competitor_attrs done: %s", total_stats)


if __name__ == "__main__":
    main()
