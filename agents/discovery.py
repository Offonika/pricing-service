from __future__ import annotations

import argparse
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx
import yaml
from openai import OpenAI

DEFAULT_PROMPT = """
Ты — парсер пресс-релизов производителей смартфонов. Получаешь HTML или текст новости.
Извлеки:
- brand (строка)
- model_name (строка)
- variant (строка или null)
- announce_date (YYYY-MM-DD или null)
- release_date (YYYY-MM-DD или null)
- screen: size_inch (float или null), technology (строка или null), refresh_rate_hz (int или null)
Верни JSON строго в указанной структуре, без текста вокруг.
"""


@dataclass
class SourceConfig:
    brand: str
    urls: List[str]


def load_sources(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def fetch_url(url: str, client: httpx.Client) -> str:
    resp = client.get(url, timeout=20)
    resp.raise_for_status()
    return resp.text


def extract_release(html_text: str, brand_hint: str, client: OpenAI) -> Optional[Dict[str, Any]]:
    messages = [
        {"role": "system", "content": DEFAULT_PROMPT},
        {"role": "user", "content": f"Brand hint: {brand_hint}\n\nContent:\n{html_text[:15000]}"},
    ]
    result = client.chat.completions.create(
        model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        messages=messages,
        temperature=0.0,
        max_tokens=800,
    )
    content = result.choices[0].message.content
    try:
        data = json.loads(content)
        return data
    except json.JSONDecodeError:
        logging.warning("LLM response is not valid JSON")
        return None


def post_device_model(api_base: str, payload: Dict[str, Any]) -> httpx.Response:
    url = f"{api_base}/api/agents/devices/models"
    return httpx.post(url, json=payload, timeout=20)


def main():
    parser = argparse.ArgumentParser(description="Discovery agent for new smartphone models")
    parser.add_argument("--config", default="config/agents/sources.yaml")
    parser.add_argument("--api-base", default=os.getenv("AGENT_API_BASE", "http://localhost:19000"))
    parser.add_argument("--openai-api-key", default=os.getenv("OPENAI_API_KEY"))
    parser.add_argument("--limit", type=int, default=3, help="Max pages per brand")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if not args.openai_api_key:
        logging.error("OPENAI_API_KEY is not set; aborting agent run")
        return

    config = load_sources(args.config)
    sources = config.get("sources", [])
    criteria = config.get("criteria", {})

    llm_client = OpenAI(api_key=args.openai_api_key)
    http_client = httpx.Client()

    processed = 0
    saved = 0
    for item in sources:
        source = SourceConfig(brand=item["brand"], urls=item.get("urls", []))
        for url in source.urls[: args.limit]:
            processed += 1
            try:
                html = fetch_url(url, http_client)
                data = extract_release(html, source.brand, llm_client)
                if not data:
                    continue
                # минимальная валидация
                if criteria.get("require_brand_in_title") and source.brand.lower() not in json.dumps(data).lower():
                    logging.info("skip: brand not detected in data for %s", url)
                    continue
                resp = post_device_model(args.api_base, data)
                if resp.status_code >= 400:
                    logging.warning("backend rejected payload: %s %s", resp.status_code, resp.text)
                    continue
                saved += 1
                logging.info("saved model from %s (%s)", url, resp.json().get("id"))
            except Exception as exc:  # noqa: BLE001
                logging.warning("failed to process %s: %s", url, exc)
                continue

    logging.info("run complete: processed=%s saved=%s", processed, saved)


if __name__ == "__main__":
    main()
