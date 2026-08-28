"""Dry-run report for product compatibility sync across 1C, site export, and pricing DB."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.infrastructure.db import build_onec_engine, session_scope
from app.models import Product, ProductCompatibility
from tasks.sync_onec_product_catalog import (
    detect_item_folder_value,
    fetch_general_catalog_item_ids,
    fetch_onec_compatibility_models,
)


@dataclass(frozen=True)
class CompatibilitySyncRow:
    article: str
    onec_count: int
    site_count: int | None
    pricing_count: int
    onec_models: list[str]
    site_models: list[str] | None
    pricing_models: list[str]
    missing_in_pricing: list[str]
    extra_in_pricing: list[str]
    missing_on_site: list[str] | None
    extra_on_site: list[str] | None
    status: str


def _norm(value: str) -> str:
    return value.strip().lower().replace("ё", "е")


def _dedupe(values: Sequence[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = str(raw or "").strip()
        if not value:
            continue
        key = _norm(value)
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return sorted(result, key=_norm)


def _missing(source: Sequence[str], target: Sequence[str]) -> list[str]:
    target_keys = {_norm(value) for value in target}
    return [value for value in _dedupe(source) if _norm(value) not in target_keys]


def load_site_compatibility_json(path: str | Path) -> dict[str, list[str]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = payload.values() if isinstance(payload, dict) else payload
    result: dict[str, list[str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        article = str(row.get("article") or row.get("sku") or "").strip()
        models = (
            row.get("compatible_models")
            or row.get("compatibility_models")
            or row.get("models")
            or []
        )
        if isinstance(models, str):
            models = [models]
        if article:
            result[article] = _dedupe([str(value) for value in models])
    return result


def _pricing_models_by_article(
    session: Session,
    *,
    articles: set[str] | None = None,
) -> dict[str, list[str]]:
    query = (
        select(Product.article, ProductCompatibility.value)
        .join(ProductCompatibility, ProductCompatibility.product_id == Product.id)
        .where(ProductCompatibility.source == "onec")
    )
    if articles:
        query = query.where(Product.article.in_(articles))
    rows = session.execute(query).all()
    values: dict[str, list[str]] = {}
    for article, model in rows:
        if article and model:
            values.setdefault(str(article), []).append(str(model))
    return {article: _dedupe(models) for article, models in values.items()}


def _onec_models_by_article(
    engine_onec: Engine,
    *,
    articles: set[str] | None = None,
) -> dict[str, list[str]]:
    folder_value = detect_item_folder_value(engine_onec)
    allowed_item_ids = fetch_general_catalog_item_ids(engine_onec, folder_value)
    values = fetch_onec_compatibility_models(engine_onec, folder_value, sorted(allowed_item_ids))
    if articles:
        values = {article: models for article, models in values.items() if article in articles}
    return {article: _dedupe(models) for article, models in values.items()}


def build_report(
    session: Session,
    engine_onec: Engine,
    *,
    articles: set[str] | None = None,
    site_values: dict[str, list[str]] | None = None,
    only_mismatches: bool = False,
    limit: int | None = None,
) -> list[CompatibilitySyncRow]:
    onec_values = _onec_models_by_article(engine_onec, articles=articles)
    pricing_values = _pricing_models_by_article(session, articles=articles)
    site_values = {article: _dedupe(values) for article, values in (site_values or {}).items()}
    site_provided = bool(site_values)

    all_articles = set(onec_values) | set(pricing_values) | set(site_values)
    if articles:
        all_articles &= articles

    rows: list[CompatibilitySyncRow] = []
    for article in sorted(all_articles):
        onec_models = onec_values.get(article, [])
        pricing_models = pricing_values.get(article, [])
        site_models = site_values.get(article, []) if site_provided else None
        missing_in_pricing = _missing(onec_models, pricing_models)
        extra_in_pricing = _missing(pricing_models, onec_models)
        missing_on_site = _missing(onec_models, site_models or []) if site_provided else None
        extra_on_site = _missing(site_models or [], onec_models) if site_provided else None
        status_parts: list[str] = []
        if missing_in_pricing:
            status_parts.append("missing_in_pricing")
        if extra_in_pricing:
            status_parts.append("extra_in_pricing")
        if missing_on_site:
            status_parts.append("missing_on_site")
        if extra_on_site:
            status_parts.append("extra_on_site")
        status = ",".join(status_parts) if status_parts else "ok"
        if only_mismatches and status == "ok":
            continue
        rows.append(
            CompatibilitySyncRow(
                article=article,
                onec_count=len(onec_models),
                site_count=len(site_models) if site_models is not None else None,
                pricing_count=len(pricing_models),
                onec_models=onec_models,
                site_models=site_models,
                pricing_models=pricing_models,
                missing_in_pricing=missing_in_pricing,
                extra_in_pricing=extra_in_pricing,
                missing_on_site=missing_on_site,
                extra_on_site=extra_on_site,
                status=status,
            )
        )
        if limit is not None and len(rows) >= limit:
            break
    return rows


def _write_json(rows: Sequence[CompatibilitySyncRow], output: str | None) -> None:
    payload = [asdict(row) for row in rows]
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if output:
        Path(output).write_text(f"{text}\n", encoding="utf-8")
    else:
        print(text)


def _write_csv(rows: Sequence[CompatibilitySyncRow], output: str | None) -> None:
    fieldnames = (
        list(asdict(rows[0]).keys()) if rows else list(CompatibilitySyncRow.__annotations__)
    )
    stream = open(output, "w", newline="", encoding="utf-8") if output else sys.stdout
    try:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            data = asdict(row)
            for key, value in data.items():
                if isinstance(value, list):
                    data[key] = " | ".join(value)
            writer.writerow(data)
    finally:
        if output:
            stream.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--article", action="append", help="Limit report to one article; can repeat"
    )
    parser.add_argument("--only-mismatches", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--format", choices=("json", "csv"), default="json")
    parser.add_argument("--output", help="Write report to file instead of stdout")
    parser.add_argument("--site-json", help="Optional exported site compatibility JSON")
    args = parser.parse_args()

    settings = get_settings()
    app_url = os.environ.get("DATABASE_URL") or None
    onec_url = os.environ.get("ONEC_DATABASE_URL") or settings.onec_database_url
    if not (app_url or settings.database_url) or not onec_url:
        raise SystemExit("DATABASE_URL and ONEC_DATABASE_URL must be set")

    site_values = load_site_compatibility_json(args.site_json) if args.site_json else None
    onec_engine = build_onec_engine(
        onec_url,
        query_timeout_seconds=settings.onec_query_timeout_seconds,
        login_timeout_seconds=settings.onec_login_timeout_seconds,
    )
    try:
        with session_scope(read_only=True, database_url=app_url) as session:
            rows = build_report(
                session,
                onec_engine,
                articles=set(args.article or []) or None,
                site_values=site_values,
                only_mismatches=args.only_mismatches,
                limit=args.limit,
            )
    finally:
        onec_engine.dispose()
    if args.format == "csv":
        _write_csv(rows, args.output)
    else:
        _write_json(rows, args.output)


if __name__ == "__main__":
    main()
