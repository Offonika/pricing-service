"""Импорт номенклатуры из БД TopControl в таблицу product."""
from __future__ import annotations

import logging
import os
import sys
import re
from typing import Optional

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Product

logger = logging.getLogger("tasks.import_topcontrol_products_db")

SUBJECT_RULES = [
    ("Дисплеи", ["дисплей", "lcd", "oled", "тачскрин", "touchscreen"]),
    ("Шлейфы", ["шлейф", "flex", "fpc"]),
    ("Аккумуляторы", ["аккумулятор", "battery", "акб", "аккум"]),
    ("Разъемы", ["разъем", "разъём", "разьем", "разем", "connector", "коннектор", "usb", "charge", "заряд", "гнездо"]),
    ("Стёкла", ["стекло", "glass"]),
    ("Кнопки", ["кнопка", "button"]),
    ("Динамики", ["динамик", "speaker"]),
    ("Микрофоны", ["микрофон", "microphone"]),
    ("Камеры", ["камера", "camera"]),
    ("Пленки", ["пленка", "плёнка", "film"]),
]


def pick_sku(row: dict) -> Optional[str]:
    for key in ("articul", "kod_infsys", "kod_infsys1"):
        val = row.get(key)
        if val:
            return str(val).strip()
    return None


def fetch_topcontrol_rows(engine_top) -> tuple[list[dict], dict[int, str]]:
    query = text(
        """
        SELECT
            id,
            id_tovar,
            articul,
            kod_infsys,
            kod_infsys1,
            naim,
            naim_long,
            id_tovar_cat,
            id_proizvod
        FROM tovar
        """
    )
    cat_query = text("SELECT id, naim FROM tovar_cat")
    with engine_top.connect() as conn:
        result = conn.execute(query)
        rows = [dict(row._mapping) for row in result]
        cats = {row.id: row.naim for row in conn.execute(cat_query)}
        return rows, cats


def parse_category_whitelist(raw: Optional[str]) -> set[int]:
    if not raw:
        return set()
    items: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            items.add(int(part))
        except ValueError:
            logger.warning("skip category id (not int): %s", part)
    return items


def classify_subject(name: str) -> Optional[str]:
    lower = name.lower()
    for subject, keywords in SUBJECT_RULES:
        for kw in keywords:
            if kw in lower:
                return subject
    return None


def has_duplicate_marker(name: str) -> bool:
    lower = re.sub(r"[^\w\s]+", " ", name.lower())
    return "дубл" in lower or "дублик" in lower or "duplicate" in lower or "dupl" in lower


def import_topcontrol_products(engine_app, engine_top) -> dict:
    settings = get_settings()
    whitelist = parse_category_whitelist(settings.topcontrol_category_whitelist)
    rows, categories = fetch_topcontrol_rows(engine_top)
    created = 0
    updated = 0
    skipped = 0
    skipped_category = 0
    skipped_marked_duplicates = 0

    with Session(engine_app) as session:
        for row in rows:
            sku = pick_sku(row)
            name = row.get("naim") or row.get("naim_long")
            if not sku or not name:
                skipped += 1
                continue

            if has_duplicate_marker(name):
                skipped_marked_duplicates += 1
                existing = session.execute(select(Product).where(Product.sku == sku)).scalar_one_or_none()
                if existing:
                    existing.is_active = False
                continue

            cat_id = row.get("id_tovar_cat")
            if whitelist and (cat_id not in whitelist):
                skipped_category += 1
                continue

            product = session.execute(select(Product).where(Product.sku == sku)).scalar_one_or_none()
            if product is None:
                product = Product(sku=sku, name=name)
                session.add(product)
                created += 1
            else:
                product.name = name
                updated += 1

            # Доп. атрибуты (храним как строки, если понадобятся)
            if row.get("id_tovar_cat") is not None:
                cat_name = categories.get(row["id_tovar_cat"])
                product.category = cat_name or str(row["id_tovar_cat"])
                product.subject = product.category
            if row.get("id_proizvod") is not None:
                product.brand = str(row["id_proizvod"])

            # Классификация предмета по названию
            subject = classify_subject(name)
            if subject:
                product.subject = subject

        session.commit()

    return {
        "rows": len(rows),
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "skipped_category": skipped_category,
        "skipped_marked_duplicates": skipped_marked_duplicates,
        "category_whitelist": sorted(list(whitelist)),
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    topcontrol_url = os.environ.get("TOPCONTROL_DATABASE_URL")
    app_url = os.environ.get("DATABASE_URL")
    if not topcontrol_url or not app_url:
        logger.error("TOPCONTROL_DATABASE_URL or DATABASE_URL is not set")
        sys.exit(1)

    engine_top = create_engine(topcontrol_url)
    engine_app = create_engine(app_url)
    result = import_topcontrol_products(engine_app, engine_top)
    print(result)


if __name__ == "__main__":
    main()
