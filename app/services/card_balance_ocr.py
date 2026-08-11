from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from openai import OpenAI

from app.core.config import Settings, get_settings
from app.services.card_balance_onec import clean_string, decimal_or_none

OCR_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "balance": {
            "type": ["string", "null"],
            "description": (
                "Текущий итоговый баланс карты в рублях, только число с точкой в качестве "
                "десятичного разделителя, например 1060.00 или 89390.00."
            ),
        },
        "confidence": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
            "description": "Уверенность модели в том, что баланс определён верно.",
        },
        "evidence": {
            "type": "string",
            "description": (
                "Короткий фрагмент текста или объяснение, по которому выбран именно этот баланс."
            ),
        },
    },
    "required": ["balance", "confidence", "evidence"],
}


@dataclass(slots=True)
class CardBalanceOCRResult:
    recognized_balance: Decimal | None
    confidence: Decimal | None
    evidence: str | None = None
    raw_response_text: str | None = None


class CardBalanceOCRClient:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        client: OpenAI | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.model = self.settings.card_balance_ocr_model or self.settings.openai_model
        self.client = client or OpenAI(
            api_key=self.settings.openai_api_key,
            base_url=self.settings.openai_api_base,
        )

    def extract_balance(
        self,
        *,
        image_bytes: bytes,
        mime_type: str,
        item_title: str | None = None,
    ) -> CardBalanceOCRResult:
        if not image_bytes:
            raise ValueError("image_bytes is empty")
        payload = base64.b64encode(image_bytes).decode("utf-8")
        response = self.client.responses.create(
            model=self.model,
            temperature=0,
            max_output_tokens=250,
            timeout=self.settings.card_balance_ocr_timeout_seconds,
            text={
                "format": {
                    "type": "json_schema",
                    "name": "card_balance_ocr_result",
                    "strict": True,
                    "schema": OCR_JSON_SCHEMA,
                }
            },
            input=[
                {
                    "role": "system",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "Ты распознаешь банковские скриншоты и возвращаешь только "
                                "структурированный результат. Нужно определить именно текущий "
                                "итоговый баланс карты в рублях. Игнорируй суммы переводов, "
                                "отдельных операций, кэшбэк, бонусы, лимиты, задолженность и "
                                "другие числа, если они не являются итоговым балансом. Если "
                                "баланс неочевиден, верни balance=null и низкую confidence."
                            ),
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "Распознай текущий баланс карты на этом скриншоте."
                                + (
                                    f" Название карточки сверки: {item_title}."
                                    if clean_string(item_title)
                                    else ""
                                )
                            ),
                        },
                        {
                            "type": "input_image",
                            "image_url": f"data:{mime_type};base64,{payload}",
                            "detail": "high",
                        },
                    ],
                },
            ],
        )
        raw_text = response.output_text or ""
        parsed = json.loads(raw_text)
        balance = _normalize_balance(parsed.get("balance"))
        confidence = _normalize_confidence(parsed.get("confidence"))
        min_confidence = Decimal(str(self.settings.card_balance_ocr_min_confidence))
        if confidence is not None and confidence < min_confidence:
            balance = None
        return CardBalanceOCRResult(
            recognized_balance=balance,
            confidence=confidence,
            evidence=clean_string(parsed.get("evidence")),
            raw_response_text=raw_text,
        )


def ocr_is_available(settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    return bool(settings.card_balance_ocr_enabled and clean_string(settings.openai_api_key))


def _normalize_balance(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value.quantize(Decimal("0.01"))
    normalized = clean_string(str(value))
    if not normalized:
        return None
    sanitized = (
        normalized.replace("\u00a0", "")
        .replace(" ", "")
        .replace("руб.", "")
        .replace("руб", "")
        .replace("₽", "")
        .replace(",", ".")
    )
    candidate = decimal_or_none(sanitized)
    if candidate is None:
        return None
    return candidate.quantize(Decimal("0.01"))


def _normalize_confidence(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        candidate = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if candidate < 0:
        candidate = Decimal("0")
    if candidate > 1:
        candidate = Decimal("1")
    return candidate.quantize(Decimal("0.0001"))
