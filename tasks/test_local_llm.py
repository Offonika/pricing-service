"""Простой тест локальной LLM: определяет модель телефона по названию товаров конкурентов."""

import json
import os
import time

import httpx

from app.services.prompts import get_llm_parse_prompt

SAMPLES: list[dict[str, str]] = [
    {
        "source": "moba",
        "sku": "LCD-PMIS1400-CP-B",
        "name": "Дисплей для iPhone 14 Pro Max в сборе с тачскрином (GX ORIG) Черный",
    },
    {
        "source": "liberti",
        "sku": "350987",
        "name": "Модуль дисплея Samsung Galaxy A54 5G (SM-A546) OLED, цвет черный",
    },
    {
        "source": "greenspark",
        "sku": "GS-REDMI-N13P",
        "name": "Дисплей для Xiaomi Redmi Note 13 Pro (OLED) с рамкой черный",
    },
]

SYSTEM_PROMPT = f"""
{get_llm_parse_prompt()}
"""


def call_llm(base_url: str, model: str, sample: dict[str, str]) -> dict[str, object]:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT.strip()},
            {
                "role": "user",
                "content": f"Source: {sample['source']} SKU: {sample['sku']}\nName: {sample['name']}",
            },
        ],
        "temperature": 0.0,
        "max_tokens": 200,
    }
    start = time.perf_counter()
    timeout = httpx.Timeout(30.0, connect=10.0)
    resp = httpx.post(f"{base_url}/v1/chat/completions", json=payload, timeout=timeout)
    elapsed = time.perf_counter() - start
    resp.raise_for_status()
    data = resp.json()
    content = data["choices"][0]["message"]["content"]
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        parsed = {"raw": content}
    return {"response": parsed, "latency_sec": elapsed, "usage": data.get("usage")}


def main() -> None:
    base_url = os.environ.get("LOCAL_LLM_BASE_URL")
    chat_model = os.environ.get("LOCAL_LLM_CHAT_MODEL")
    if not base_url or not chat_model:
        raise RuntimeError("LOCAL_LLM_BASE_URL и LOCAL_LLM_CHAT_MODEL должны быть заданы")
    results = []
    for sample in SAMPLES:
        try:
            res = call_llm(base_url, chat_model, sample)
        except Exception as exc:  # noqa: BLE001
            res = {"error": str(exc)}
        results.append({"sample": sample, **res})
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
