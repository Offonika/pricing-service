from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
from urllib.parse import urlparse, urlunparse

import httpx
from sqlalchemy.orm import Session

from app.models import CompetitorItem, CompetitorItemUrlAlias


@dataclass(frozen=True)
class CompetitorUrlParts:
    normalized_url: str
    catalog_id: str | None = None
    redirect_id: str | None = None


def normalize_competitor_url(value: str | None) -> str | None:
    raw = (value or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    if not parsed.netloc:
        return re.sub(r"\s+", "", raw.lower()).rstrip("/")
    scheme = (parsed.scheme or "https").lower()
    host = (parsed.hostname or parsed.netloc).lower()
    path = re.sub(r"/+", "/", parsed.path or "/").rstrip("/")
    return urlunparse((scheme, host, path, "", "", ""))


def parse_competitor_url(value: str | None) -> CompetitorUrlParts | None:
    normalized = normalize_competitor_url(value)
    if not normalized:
        return None
    parsed = urlparse(normalized)
    host = (parsed.hostname or "").lower()
    catalog_id = None
    redirect_id = None
    if host.endswith("moba.ru"):
        match = re.search(r"/catalog/[^/]+/(\d+)$", parsed.path, flags=re.IGNORECASE)
        if match:
            catalog_id = match.group(1)
    if host.endswith("poiskzip.ru"):
        match = re.search(r"/redirect/(\d+)$", parsed.path, flags=re.IGNORECASE)
        if match:
            redirect_id = match.group(1)
    return CompetitorUrlParts(
        normalized_url=normalized,
        catalog_id=catalog_id,
        redirect_id=redirect_id,
    )


def upsert_competitor_item_url_alias(
    session: Session,
    item: CompetitorItem,
    alias_url: str | None,
    *,
    url_kind: str = "stored",
    resolved_from_url: str | None = None,
    resolved_at: datetime | None = None,
) -> CompetitorItemUrlAlias | None:
    parts = parse_competitor_url(alias_url)
    if parts is None:
        return None
    alias = (
        session.query(CompetitorItemUrlAlias)
        .filter(
            CompetitorItemUrlAlias.competitor == item.competitor,
            CompetitorItemUrlAlias.normalized_url == parts.normalized_url,
        )
        .first()
    )
    if alias is None:
        alias = CompetitorItemUrlAlias(
            competitor_item_id=item.id,
            competitor=item.competitor,
            alias_url=alias_url or parts.normalized_url,
            normalized_url=parts.normalized_url,
            url_kind=url_kind,
        )
    else:
        alias.competitor_item_id = item.id
        alias.alias_url = alias_url or alias.alias_url
        alias.url_kind = url_kind or alias.url_kind
    alias.catalog_id = parts.catalog_id or alias.catalog_id
    alias.redirect_id = parts.redirect_id or alias.redirect_id
    alias.resolved_from_url = resolved_from_url or alias.resolved_from_url
    alias.resolved_at = resolved_at or alias.resolved_at
    session.add(alias)
    return alias


def resolve_poiskzip_redirect_url(
    url: str,
    *,
    client: httpx.Client | None = None,
    timeout: float = 8.0,
) -> str | None:
    parts = parse_competitor_url(url)
    if parts is None or parts.redirect_id is None:
        return None
    close_client = client is None
    http = client or httpx.Client(timeout=timeout, follow_redirects=False)
    try:
        response = http.get(url)
        location = response.headers.get("location")
        if location:
            return location
        match = re.search(
            r"url=['\"]?([^'\">\s]+)",
            unescape(response.text or ""),
            flags=re.IGNORECASE,
        )
        return match.group(1) if match else None
    finally:
        if close_client:
            http.close()


def upsert_resolved_poiskzip_alias(
    session: Session,
    item: CompetitorItem,
    *,
    client: httpx.Client | None = None,
    timeout: float = 8.0,
) -> CompetitorItemUrlAlias | None:
    source_parts = parse_competitor_url(item.url)
    direct_url = resolve_poiskzip_redirect_url(item.url or "", client=client, timeout=timeout)
    direct_parts = parse_competitor_url(direct_url)
    if direct_parts is None or direct_parts.catalog_id is None:
        return None
    alias = upsert_competitor_item_url_alias(
        session,
        item,
        direct_url,
        url_kind="resolved",
        resolved_from_url=item.url,
        resolved_at=datetime.now(timezone.utc),
    )
    if alias is not None and source_parts and source_parts.redirect_id and not alias.redirect_id:
        alias.redirect_id = source_parts.redirect_id
        session.add(alias)
    return alias
