from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.infrastructure.db.engines import build_engine
from app.models import Competitor, Product, ProductMatch
from app.models.competitor_item import CompetitorItem
from app.services.competitor_matching import (
    _competitor_quality_value,
    _normalize_model_key,
    _product_quality_value,
    parse_model_name,
)
from app.services.display_quality_raw_mapping import (
    extract_quality_token_as_in_name,
    map_competitor_raw_quality_to_1c_raw,
)

NON_DISPLAY_NAME_TOKENS = (
    "шлейф",
    "камера",
    "стекло камеры",
    "speaker",
    "динам",
    "buzz",
    "buzzer",
    "аккумулятор",
    "battery",
    "sim",
    "корпус",
    "задняя крышка",
)


def _is_display_product(product: Product) -> bool:
    rendered = " ".join(
        str(value or "")
        for value in (product.subject_1c, product.subject, product.category, product.name)
    ).lower()
    if any(token in rendered for token in NON_DISPLAY_NAME_TOKENS):
        return False
    return any(token in rendered for token in ("дисп", "lcd", "oled", "экран"))


def _parsed_product_key(product: Product) -> tuple[str, str] | None:
    parsed = parse_model_name(product.name)
    if (
        not parsed
        or parsed.ambiguous
        or not parsed.brand
        or not parsed.model
        or parsed.confidence < 0.7
    ):
        return None
    model_key = _normalize_model_key(parsed.model)
    if not model_key:
        return None
    return parsed.brand, model_key


def _product_quality_raw_value(product: Product) -> str | None:
    for value in (
        product.display_quality_raw,
        product.quality_raw,
        product.display_quality,
        product.quality,
    ):
        if value:
            normalized = str(value).strip()
            if normalized:
                return normalized
    return None


def _competitor_quality_price_value(
    item: CompetitorItem | None,
    match_quality: str | None,
) -> str | None:
    price_value = extract_quality_token_as_in_name(item.name if item else None)
    if price_value:
        return price_value
    if match_quality:
        return str(match_quality).strip()
    if item and item.attrs_quality:
        return str(item.attrs_quality).strip()
    if item and item.screen_quality_grade:
        return str(item.screen_quality_grade).strip()
    return None


def build_report(session: Session) -> tuple[dict[str, int], list[dict[str, object]]]:
    competitors = {row.id: row.name for row in session.execute(select(Competitor)).scalars()}
    items = {
        (row.competitor, row.external_id): row
        for row in session.execute(select(CompetitorItem)).scalars()
    }

    display_products = [
        row for row in session.execute(select(Product)).scalars() if _is_display_product(row)
    ]
    products_by_key: dict[tuple[str, str], list[Product]] = {}
    for product in display_products:
        key = _parsed_product_key(product)
        if key is None:
            continue
        products_by_key.setdefault(key, []).append(product)

    report_rows: list[dict[str, object]] = []
    stats = {
        "display_matches": 0,
        "quality_mismatches": 0,
        "with_catalog_item": 0,
        "with_candidate_pool": 0,
        "with_matching_quality_candidate": 0,
        "with_unique_matching_quality_candidate": 0,
    }

    rows = session.execute(
        select(ProductMatch, Product).join(Product, ProductMatch.product_id == Product.id)
    ).all()
    for match, product in rows:
        if not _is_display_product(product):
            continue
        stats["display_matches"] += 1

        product_quality = _product_quality_value(product)
        if (
            not match.quality
            or not product_quality
            or product_quality.lower() == match.quality.lower()
        ):
            continue
        stats["quality_mismatches"] += 1

        competitor_name = competitors.get(match.competitor_id)
        item = (
            items.get((competitor_name, match.competitor_sku))
            if competitor_name and match.competitor_sku
            else None
        )
        competitor_quality = (
            _competitor_quality_value(item, item.name if item else None) or match.quality
        )
        competitor_quality_price = _competitor_quality_price_value(item, match.quality)
        if item is not None:
            stats["with_catalog_item"] += 1

        key = _parsed_product_key(product)
        candidate_pool = (
            [candidate for candidate in products_by_key.get(key, []) if candidate.id != product.id]
            if key
            else []
        )
        if candidate_pool:
            stats["with_candidate_pool"] += 1

        matching_quality_candidates = [
            candidate
            for candidate in candidate_pool
            if (_product_quality_value(candidate) or "").lower() == competitor_quality.lower()
        ]
        if matching_quality_candidates:
            stats["with_matching_quality_candidate"] += 1
        if len(matching_quality_candidates) == 1:
            stats["with_unique_matching_quality_candidate"] += 1

        report_rows.append(
            {
                "product_match_id": match.id,
                "competitor": competitor_name,
                "competitor_sku": match.competitor_sku,
                "current_product_id": product.id,
                "current_article": product.article,
                "current_quality_raw": _product_quality_raw_value(product),
                "current_quality": product_quality,
                "competitor_quality_price": competitor_quality_price,
                "competitor_quality": competitor_quality,
                "mapped_1c_quality_raw": map_competitor_raw_quality_to_1c_raw(
                    competitor_name,
                    competitor_quality_price,
                ),
                "current_name": product.name,
                "competitor_name": item.name if item else None,
                "parsed_key": "|".join(key) if key else None,
                "candidate_count": len(candidate_pool),
                "matching_quality_candidate_count": len(matching_quality_candidates),
                "candidate_pool": [
                    {
                        "product_id": candidate.id,
                        "article": candidate.article,
                        "quality": _product_quality_value(candidate),
                        "name": candidate.name,
                        "quality_matches_competitor": (
                            (_product_quality_value(candidate) or "").lower()
                            == competitor_quality.lower()
                        ),
                    }
                    for candidate in candidate_pool
                ],
                "matching_quality_candidates": [
                    {
                        "product_id": candidate.id,
                        "article": candidate.article,
                        "quality": _product_quality_value(candidate),
                        "name": candidate.name,
                    }
                    for candidate in matching_quality_candidates
                ],
            }
        )

    return stats, report_rows


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "product_match_id",
                "competitor",
                "competitor_sku",
                "current_product_id",
                "current_article",
                "current_quality",
                "competitor_quality",
                "parsed_key",
                "candidate_count",
                "matching_quality_candidate_count",
                "matching_quality_candidates",
                "current_name",
                "competitor_name",
            ],
        )
        writer.writeheader()
        for row in rows:
            serialized = dict(row)
            serialized["matching_quality_candidates"] = json.dumps(
                serialized["matching_quality_candidates"], ensure_ascii=False
            )
            writer.writerow(serialized)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Report display ProductMatch rows where competitor quality differs from product quality."
    )
    parser.add_argument(
        "--output",
        default="reports/display_quality_mismatch_candidates.csv",
        help="CSV output path",
    )
    parser.add_argument(
        "--only-candidates",
        action="store_true",
        help="Write only rows that have at least one matching-quality candidate",
    )
    args = parser.parse_args()

    engine = build_engine(get_settings().database_url)
    with Session(engine) as session:
        stats, rows = build_report(session)

    output_rows = rows
    if args.only_candidates:
        output_rows = [row for row in rows if int(row["matching_quality_candidate_count"]) > 0]

    output_path = Path(args.output)
    write_csv(output_path, output_rows)

    summary = dict(stats)
    summary["written_rows"] = len(output_rows)
    summary["output"] = str(output_path)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
