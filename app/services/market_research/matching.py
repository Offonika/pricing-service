from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from sqlalchemy.orm import Session

from app.models import PhoneModel, Product


@dataclass
class MatchResult:
    product_id: int
    phone_model_id: int
    confidence: float
    reason: str


class ProductModelMatcher:
    """
    Простая эвристика сопоставления Product ↔ PhoneModel:
    - сравнение бренда (точное, регистронезависимое);
    - поиск названия модели в названии товара;
    - поддержка варианта (Pro/Plus/Ultra) через подстроку.
    """

    def __init__(self, db: Session):
        self.db = db

    def match_product(self, product: Product) -> Optional[MatchResult]:
        if not product.brand or not product.name:
            return None
        brand_norm = product.brand.lower()
        products_query = (
            self.db.query(PhoneModel)
            .filter(PhoneModel.brand.ilike(brand_norm))
            .order_by(PhoneModel.created_at.desc())
        )
        name_norm = product.name.lower()
        best: Optional[MatchResult] = None
        for model in products_query.all():
            if model.model_name:
                pattern = re.escape(model.model_name.lower())
                if re.search(pattern, name_norm):
                    confidence = 0.9
                else:
                    continue
            else:
                continue
            if model.variant and model.variant.lower() in name_norm:
                confidence += 0.05
            result = MatchResult(
                product_id=product.id,
                phone_model_id=model.id,
                confidence=confidence,
                reason=f"brand match and model substring '{model.model_name}'",
            )
            if best is None or result.confidence > best.confidence:
                best = result
        return best
