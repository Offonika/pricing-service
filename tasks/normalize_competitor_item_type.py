"""Нормализация поля item_type (предмет) в каталоге конкурентов."""

from __future__ import annotations

import argparse
import json
import logging

import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import CompetitorItem
from app.services.llm_fallback import FallbackChatClient
from app.services.prompts import get_llm_item_type_prompt

SKU_PREFIX_CLASS_MAP = (
    ("FRM-LCD-", "housing"),
    ("BTC-", "housing"),
    ("LCD-", "display"),
    ("BTB-", "battery"),
    ("BTL-", "battery"),
    ("BTT-", "battery"),
    ("AKB-", "battery"),
    ("FPC-", "flex"),
    ("FLC-", "flex"),
    ("FLX-", "flex"),
    ("CAM-", "camera"),
    ("CON-", "connector"),
    ("CHG-", "connector"),
    ("USB-", "cable"),
    ("CAB-", "cable"),
    ("PCB-", "board"),
    ("SUB-", "board"),
    ("TP-", "other"),
    ("GLS-", "other"),
    ("HLD-", "other"),
    ("TLS-", "other"),
    ("MTL-", "other"),
    ("SPK-", "other"),
    ("BUZ-", "other"),
    ("PWS-", "other"),
    ("PWSLP-", "other"),
    ("KPD-", "other"),
    ("IC-", "other"),
    ("MTX-", "other"),
    ("ADT-", "other"),
    ("EQP-", "other"),
    ("STIC-", "other"),
    ("FAN-", "other"),
    ("HR-", "other"),
    ("BAT-", "other"),
    ("SCR-", "other"),
    ("SCRSET-", "other"),
    ("SET-", "other"),
    ("PRF-", "other"),
    ("LED-", "other"),
    ("SLD-", "other"),
    ("HF-", "other"),
    ("BKL-", "other"),
    ("CHR-", "other"),
    ("MSD-", "other"),
    ("CRHR-", "other"),
    ("VBR-", "other"),
    ("RBR-", "other"),
    ("MIC-", "other"),
    ("SLR-", "other"),
    ("OGZ-", "other"),
    ("TCK-", "other"),
    ("SSD-", "other"),
    ("COO-", "other"),
    ("TST-", "other"),
    ("ADP-", "other"),
    ("ANTSTC-", "other"),
    ("TRMPD-", "other"),
    ("PHN-", "other"),
    ("MCI-", "other"),
    ("RAM-", "other"),
    ("SCL-", "other"),
    ("SP-", "other"),
    ("PTT-", "other"),
    ("NZS-", "other"),
    ("PRJ-", "other"),
    ("SLCN-", "other"),
)

PRIORITY_TEXT_CLASS_MAP = (
    (
        "housing",
        (
            "задняя крышка",
            "задней крышки",
            "back cover",
        ),
    ),
    (
        "other",
        (
            "микросхема",
            "wi-fi модуль",
            "wifi модуль",
            "wi-fi module",
            "wifi module",
            "контроллер питания",
            "контроллер заряда",
            "pmic",
            "азу",
            "зарядная станция",
            "автомобильное зарядное",
            "сетевое зарядное устройство",
            "беспроводное зарядное устройство",
            "зарядное устройство",
        ),
    ),
)

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
    "battery": ["аккумулятор", "battery", "акб", "аккум", "batt", "btl", "btt"],
    "camera": ["камера", "camera"],
    "flex": [
        "шлейф",
        "flex",
        "flc",
        "fpc",
        "шлейфа",
        "кнопк",
        "датчик",
        "сенсорный шлейф",
        "сканер отпечатка",
        "отпечатка пальца",
    ],
    "housing": ["корпус", "крышка", "рамка", "back cover", "frame", "bezel", "панель"],
    "connector": ["разъем", "разъём", "коннектор", "connector", "port", "гнездо", "charging"],
    "cable": ["кабель", "usb", "type-c", "micro-usb", "lightning", "зарядный", "провод"],
    "board": ["плата", "board", "pcb", "нижняя плата", "материнская плата", "sub board"],
    "other": [
        "защитное стекло",
        "защитный кейс",
        "стекло камеры",
        "стекло задней камеры",
        "стекло для переклейки",
        "g+oca",
        "oca пленка",
        "oca плёнка",
        "чехол",
        "case",
        "bumper",
        "гарнитура",
        "bluetooth",
        "наушник",
        "tws",
        "держатель sim",
        "держатель сим",
        "sim карты",
        "держатель в авто",
        "держатель для смартфона",
        "блок питания",
        "сетевой адаптер",
        "адаптер",
        "инструмент",
        "паяль",
        "микроскоп",
        "тестер",
        "клей",
        "скотч",
        "наклейка",
        "пленка",
        "плёнка",
        "динамик",
        "buzzer",
        "микрофон",
        "вибромотор",
        "клавиатура",
        "подсветка",
        "матрица",
        "ноутбука",
        "телевизор",
        "карта памяти",
        "microsd",
        "ssd",
        "оперативная память",
        "винты",
        "набор винтов",
        "микросхема",
        "термопрокладка",
        "батарейка",
        "крона",
        "зарядная станция",
        "зарядка",
        "зарядное устройство",
        "автомобильное зарядное",
        "крепеж",
        "крепёж",
        "крепежи",
        "комплект креплений",
        "болты",
        "ножки",
        "антенна",
        "стилус",
        "штатив",
        "нож ",
        "ножи",
        "брелок",
        "набор аксессуаров",
        "набор для ремонта",
        "флюс",
        "очиститель",
        "растворитель",
        "сплав розе",
        "силиконовая смазка",
        "химия",
        "нагреватель",
        "подогрев",
        "насадки для фена",
        "толкатель-дозатор",
        "сим-лоток",
        "sim-лоток",
        "лоток sim",
        "лоток для sim",
        "накладка",
        "колонка",
        "пружины для триггеров",
        "потенциометр",
        "проектор",
    ],
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


def rule_classify(text: str, sku: str | None = None, category: str | None = None) -> str | None:
    sku_upper = (sku or "").strip().upper()
    for prefix, item_type in SKU_PREFIX_CLASS_MAP:
        if sku_upper.startswith(prefix):
            return item_type
    lower = " ".join(part for part in (text, sku or "", category or "") if part).lower()
    for item_type, tokens in PRIORITY_TEXT_CLASS_MAP:
        if any(tok in lower for tok in tokens):
            return item_type
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


def llm_classify_with_fallback(client: FallbackChatClient, name: str) -> str | None:
    result = client.chat_completion(
        messages=[
            {"role": "system", "content": get_llm_item_type_prompt()},
            {"role": "user", "content": name},
        ],
        temperature=0.0,
        max_tokens=50,
        response_validator=lambda content: _json_item_type_is_valid(content),
    )
    try:
        parsed = json.loads(result.content)
    except json.JSONDecodeError:
        return None
    item_type = parsed.get("item_type")
    if item_type in LLM_CLASSES:
        return item_type
    return None


def _json_item_type_is_valid(content: str) -> bool:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return False
    return isinstance(parsed, dict) and parsed.get("item_type") in LLM_CLASSES


def normalize_item_types(
    session: Session,
    source: str | None,
    name_contains: str | None,
    category_contains: str | None,
    sku_prefix: str | None,
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
    if sku_prefix:
        query = query.where(CompetitorItem.external_id.ilike(f"{sku_prefix}%"))
    if missing_only:
        query = query.where(CompetitorItem.item_type.is_(None))
    if limit:
        query = query.limit(limit)

    items = list(session.execute(query).scalars())

    llm_client = None
    if use_llm:
        llm_client = FallbackChatClient.from_env(timeout=30.0)
    if use_llm and llm_client and llm_client.has_providers:
        logging.info(
            "LLM classification fallback enabled: providers=%s limit=%s threshold=%.2f",
            llm_client.provider_names,
            llm_limit,
            llm_threshold,
        )
    elif use_llm:
        logging.warning("LLM requested but no local/OpenAI providers are configured")
        llm_client = None

    processed = 0
    updated = 0
    llm_used = 0
    llm_failed = 0

    for item in items:
        processed += 1
        if not item.name:
            continue
        rule_type = None if llm_only else rule_classify(item.name, item.external_id, item.category)
        item_type = rule_type
        if not item_type and use_llm and llm_client and (llm_limit == 0 or llm_used < llm_limit):
            try:
                item_type = llm_classify_with_fallback(llm_client, item.name)
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
    parser.add_argument("--sku-prefix", help="Filter by external_id prefix")
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
            sku_prefix=args.sku_prefix,
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
