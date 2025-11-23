from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from app.services.pricing_strategies.base import PricingResult


@dataclass
class ExportRow:
    sku: str
    price: float
    comment: str = ""


def export_prices_to_csv(rows: Iterable[ExportRow], output_path: Path) -> Path:
    """
    Формирует CSV для 1С (Установка цен): SKU, PRICE, COMMENT.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["SKU", "PRICE", "COMMENT"])
        for row in rows:
            writer.writerow([row.sku, row.price, row.comment])
    return output_path


def export_recommendations(
    recommendations: dict[str, PricingResult],
    output_path: Path,
    price_comment: str = "",
) -> Path:
    rows = [
        ExportRow(sku=sku, price=float(rec.recommended_price), comment=price_comment)
        for sku, rec in recommendations.items()
    ]
    return export_prices_to_csv(rows, output_path)
