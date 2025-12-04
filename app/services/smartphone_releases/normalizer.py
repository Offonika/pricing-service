from __future__ import annotations

import json
import logging
import re
import time
from datetime import date
from typing import List, Optional

from openai import OpenAI

from app.core.config import get_settings
from app.services.smartphone_releases.types import NormalizedReleaseCandidate, RawNewsItem

SYSTEM_PROMPT = """
Ты анализируешь новость и определяешь, является ли она анонсом нового смартфона.
Ответь JSON-ом строго с полями:
- is_phone_announcement: true/false
- brand: строка или null
- model: строка или null (первая модель из списка, если их несколько)
- models: массив строк или null (каждый элемент — отдельная модель; если модель одна, можно вернуть массив из одного значения или null)
- announcement_date: дата YYYY-MM-DD или null
- release_status: one of ["rumor", "announced", "released"] или null
- market_release_date: дата начала продаж (глобально) YYYY-MM-DD или null
- market_release_date_ru: дата начала продаж в России (если указано) YYYY-MM-DD или null
- summary_ru: краткий пересказ новости на русском (до 400 символов)
"""


def _parse_date(raw: Optional[str]) -> Optional[date]:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw.split("T")[0])
    except Exception:
        return None


def _parse_model_list(raw: Optional[object]) -> List[str]:
    if not raw:
        return []
    if isinstance(raw, str):
        return _split_model_string(raw)
    if isinstance(raw, list):
        result = []
        for item in raw:
            if isinstance(item, str):
                result.extend(_split_model_string(item))
        return result
    return []


def _split_model_string(value: Optional[str]) -> List[str]:
    if not value:
        return []
    normalized = value
    normalized = re.sub(r"\s+(and|&|и)\s+", ",", normalized, flags=re.IGNORECASE)
    normalized = normalized.replace("/", ",").replace("|", ",").replace(";", ",")
    parts = [part.strip(" -") for part in normalized.split(",")]
    return [part for part in parts if part]


class SmartphoneReleaseNormalizer:
    """LLM-нормализация новостей о смартфонах."""

    def __init__(self, client: OpenAI, model: str, request_delay: float = 0.25) -> None:
        self.client = client
        self.model = model
        self.request_delay = request_delay
        self.logger = logging.getLogger("app.services.smartphone_release_normalizer")

    def normalize(self, item: RawNewsItem) -> Optional[NormalizedReleaseCandidate]:
        content = (item.description or "")[:2000]
        if self.request_delay and self.request_delay > 0:
            time.sleep(self.request_delay)
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                temperature=0.0,
                max_tokens=300,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": f"Title: {item.title}\n\nSummary: {content}",
                    },
                ],
            )
        except Exception:
            self.logger.exception("llm normalization failed", extra={"url": item.url})
            return None

        result_text = response.choices[0].message.content or ""
        try:
            payload = json.loads(result_text)
        except json.JSONDecodeError:
            self.logger.warning("llm returned non-json payload", extra={"url": item.url, "body": result_text})
            return None

        is_announcement = bool(payload.get("is_phone_announcement"))
        models = _parse_model_list(payload.get("models"))
        if not models and payload.get("model"):
            models = _split_model_string(payload.get("model"))
        return NormalizedReleaseCandidate(
            is_phone_announcement=is_announcement,
            brand=(payload.get("brand") or None),
            model=(payload.get("model") or None),
            models=models or None,
            announcement_date=_parse_date(payload.get("announcement_date")),
            release_status=(payload.get("release_status") or None),
            market_release_date=_parse_date(payload.get("market_release_date")),
            market_release_date_ru=_parse_date(payload.get("market_release_date_ru")),
            summary_ru=payload.get("summary_ru") or None,
        )


def build_normalizer_from_settings() -> Optional[SmartphoneReleaseNormalizer]:
    settings = get_settings()
    if not settings.openai_api_key:
        logging.getLogger("app.services.smartphone_release_normalizer").warning(
            "OPENAI_API_KEY is not set; smartphone release normalization disabled"
        )
        return None
    client = OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_api_base)
    model_name = settings.smartphone_release_llm_model or settings.openai_model
    return SmartphoneReleaseNormalizer(
        client=client,
        model=model_name,
        request_delay=settings.smartphone_release_request_delay_seconds,
    )
