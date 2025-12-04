from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Iterable, List
from bs4 import BeautifulSoup

from app.services.scraper.models import CompetitorOffer

logger = logging.getLogger("app.scraper.parsers")


def _to_decimal(value: str) -> Decimal:
    try:
        return Decimal(value.replace(",", "."))
    except (InvalidOperation, AttributeError):
        return Decimal("0")


def _parse_bool(value: str) -> bool:
    normalized = str(value).strip().lower()
    return normalized in {"1", "true", "yes", "y", "in_stock", "available", "есть"}


def parse_html_offers(content: str, competitor: str, collected_at: datetime) -> List[CompetitorOffer]:
    """
    Простая HTML-выборка: ищем блоки с class="offer" и data-* атрибутами.
    Используется для отладки, реальные правила будут уточняться под каждую витрину.
    """
    offers: List[CompetitorOffer] = []
    pattern = re.compile(
        r'<div[^>]*class="offer"[^>]*data-sku="(?P<sku>[^"]+)"[^>]*data-price="(?P<price>[^"]+)"'
        r'[^>]*data-availability="(?P<availability>[^"]+)"[^>]*data-url="(?P<url>[^"]+)"'
        r'[^>]*data-category="(?P<category>[^"]*)"[^>]*>(?P<name>[^<]+)</div>',
        re.IGNORECASE,
    )
    for match in pattern.finditer(content):
        data = match.groupdict()
        offers.append(
            CompetitorOffer(
                competitor=competitor,
                external_sku=data["sku"],
                name=data["name"].strip(),
                price_roz=_to_decimal(data["price"]),
                price_opt=None,
                availability=_parse_bool(data["availability"]),
                url=data["url"],
                category=data.get("category") or None,
                collected_at=collected_at,
            )
        )
    return offers


def parse_json_offers(content: str, competitor: str, collected_at: datetime) -> List[CompetitorOffer]:
    """
    Поддержка формата JSON-списка словарей с полями sku/name/price/availability/url/category.
    """
    offers: List[CompetitorOffer] = []
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return offers
    if not isinstance(payload, Iterable):
        return offers
    for item in payload:
        try:
            offers.append(
                CompetitorOffer(
                    competitor=competitor,
                    external_sku=str(item.get("sku") or item.get("id") or ""),
                    name=item.get("name") or "",
                    price_roz=_to_decimal(str(item.get("price") or item.get("price_roz") or "0")),
                    price_opt=_to_decimal(str(item["price_opt"])) if item.get("price_opt") else None,
                    availability=_parse_bool(item.get("availability") or item.get("in_stock") or "1"),
                    url=item.get("url") or "",
                    category=item.get("category"),
                    collected_at=collected_at,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("skip malformed offer: %s (%s)", item, exc)
            continue
    return offers


def parse_offers(content: str, competitor: str, collected_at: datetime) -> List[CompetitorOffer]:
    """
    Универсальный парсер: сначала пытается JSON, затем HTML-шаблон.
    """
    offers = parse_json_offers(content, competitor, collected_at)
    if offers:
        return offers
    html_offers = parse_html_offers(content, competitor, collected_at)
    if html_offers:
        return html_offers
    # Moba (Bitrix) разметка: таблица, price в .price[data-value], ссылка в .title a
    soup = BeautifulSoup(content, "html.parser")
    results: List[CompetitorOffer] = []
    for row in soup.select("table.list_item tr"):  # грубо, ищем строки каталога
        title_el = row.select_one(".item-name-cell .title a")
        price_el = row.select_one(".price[data-value]")
        if not title_el or not price_el:
            continue
        name = title_el.get_text(strip=True)
        url = title_el.get("href") or ""
        if url and url.startswith("/"):
            url = f"https://moba.ru{url}"
        price_val = price_el.get("data-value") or price_el.get_text(strip=True).replace(" ", "")
        try:
            price = _to_decimal(str(price_val))
        except Exception:
            price = Decimal("0")
        results.append(
            CompetitorOffer(
                competitor=competitor,
                external_sku=url,  # у moba нет явного SKU, используем URL как идентификатор
                name=name,
                price_roz=price,
                price_opt=None,
                availability=True,
                url=url,
                category=None,
                collected_at=collected_at,
            )
        )
    return results
