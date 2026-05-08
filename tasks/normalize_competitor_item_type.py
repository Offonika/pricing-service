"""Нормализация поля item_type (предмет) в каталоге конкурентов."""

from __future__ import annotations

import argparse
import json
import logging
import os

import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import CompetitorItem
from app.services.prompts import get_llm_item_type_prompt

CLASS_MAP = {
    "display": [
        "дисплей",
        "экран",
        "lcd",
        "oled",
        "amoled",
        "super amoled",
        "dynamic amoled",
        "ltps",
        "ltpo",
        "ips",
        "incell",
        "in-cell",
        "module",
        "модуль",
        "тачскрин",
        "тач",
    ],
    "battery": ["аккумулятор", "battery", "акб", "аккум", "batt"],
    "camera": ["камера", "camera"],
    "flex": ["шлейф", "flex", "шлейфа", "кнопк", "датчик", "сенсорный шлейф"],
    "housing": ["корпус", "крышка", "рамка", "back cover", "frame", "bezel", "панель"],
    "connector": ["разъем", "разъём", "коннектор", "port", "гнездо"],
    "cable": ["кабель", "usb", "type-c", "micro-usb", "lightning", "зарядный", "провод"],
    "board": ["плата", "board", "pcb", "нижняя плата", "материнская плата"],
    "other": [],
}

LLM_CLASSES = [
    "display",
    "battery",
    "camera",
    "flex",
    "housing",
    "connector",
    "cable",
    "board",
    "other",
]


def rule_classify(text: str) -> str | None:
    lower = text.lower()
    for cls, tokens in CLASS_MAP.items():
        if any(tok in lower for tok in tokens):
            return cls
    return None


def llm_classify(client: httpx.Client, base_url: str, model: str, name: str) -> str | None:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": get_llm_item_type_prompt()},
            {"role": "user", "content": name},
        ],
        "temperature": 0.0,
        "max_tokens": 50,
    }
    resp = client.post(f"{base_url}/v1/chat/completions", json=payload, timeout=30.0)
    resp.raise_for_status()
    data = resp.json()
    content = data["choices"][0]["message"]["content"]
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return None
    item_type = parsed.get("item_type")
    if item_type in LLM_CLASSES:
        return item_type
    return None


def normalize_item_types(
    session: Session,
    source: str | None,
    name_contains: str | None,
    category_contains: str | None,
    missing_only: bool,
    overwrite: bool,
    limit: int | None,
    use_llm: bool,
    llm_limit: int,
    llm_threshold: float,
    llm_only: bool,
) -> dict:
    query = select(CompetitorItem)
    if source:
        query = query.where(CompetitorItem.competitor == source)
    if name_contains:
        query = query.where(CompetitorItem.name.ilike(f"%{name_contains}%"))
    if category_contains:
        query = query.where(CompetitorItem.category.ilike(f"%{category_contains}%"))
    if missing_only:
        query = query.where(CompetitorItem.item_type.is_(None))
    if limit:
        query = query.limit(limit)

    items = list(session.execute(query).scalars())

    base_url = os.environ.get("LOCAL_LLM_BASE_URL")
    model = os.environ.get("LOCAL_LLM_CHAT_MODEL")
    llm_client = None
    if use_llm and base_url and model:
        llm_client = httpx.Client()
        logging.info(
            "LLM classification enabled: %s %s (limit=%s threshold=%.2f)",
            base_url,
            model,
            llm_limit,
            llm_threshold,
        )
    elif use_llm:
        logging.warning("LLM requested but LOCAL_LLM_BASE_URL or LOCAL_LLM_CHAT_MODEL not set")

    processed = 0
    updated = 0
    llm_used = 0
    llm_failed = 0

    for item in items:
        processed += 1
        if not item.name:
            continue
        rule_type = None if llm_only else rule_classify(item.name)
        item_type = rule_type
        if not item_type and use_llm and llm_client and (llm_limit == 0 or llm_used < llm_limit):
            try:
                item_type = llm_classify(llm_client, base_url, model, item.name)
                if item_type:
                    llm_used += 1
            except Exception:
                llm_failed += 1
                logging.exception(
                    "LLM classify failed for %s/%s", item.competitor, item.external_id
                )
        if not item_type:
            continue
        if item.item_type and not overwrite:
            continue
        item.item_type = item_type
        session.add(item)
        updated += 1
        logging.info(
            "item_type set %s/%s -> %s (rule=%s)",
            item.competitor,
            item.external_id,
            item_type,
            bool(rule_type),
        )
    session.commit()
    if llm_client:
        llm_client.close()
    return {
        "processed": processed,
        "updated": updated,
        "llm_used": llm_used,
        "llm_failed": llm_failed,
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(
        description="Normalize competitor_item.item_type (subject/category)."
    )
    parser.add_argument("--source", help="Filter by competitor")
    parser.add_argument("--name-contains", help="ILIKE on name")
    parser.add_argument("--category-contains", help="ILIKE on category")
    parser.add_argument("--missing-only", action="store_true", help="Only items without item_type")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing item_type")
    parser.add_argument("--limit", type=int, help="Limit records")
    parser.add_argument("--llm", action="store_true", help="Use LLM fallback")
    parser.add_argument("--llm-limit", type=int, default=0, help="Max LLM calls (0 = no limit)")
    parser.add_argument(
        "--llm-threshold", type=float, default=0.0, help="(reserved) threshold not used now"
    )
    parser.add_argument(
        "--llm-only", action="store_true", help="Skip rule-based classification, use only LLM"
    )
    args = parser.parse_args()

    settings = get_settings()
    engine = create_engine(settings.database_url)
    with Session(engine) as session:
        stats = normalize_item_types(
            session,
            source=args.source,
            name_contains=args.name_contains,
            category_contains=args.category_contains,
            missing_only=args.missing_only,
            overwrite=args.overwrite,
            limit=args.limit,
            use_llm=args.llm,
            llm_limit=args.llm_limit,
            llm_threshold=args.llm_threshold,
            llm_only=args.llm_only,
        )
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
