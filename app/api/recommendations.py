from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.dependencies import get_db
from app.models import Product
from app.schemas.recommendations import RecommendationResponse
from app.services.pricing import calculate_recommendation

router = APIRouter()


@router.get("/products/{article}/recommendation", response_model=RecommendationResponse)
def get_recommendation(
    article: str,
    min_margin: Decimal = Decimal("0.1"),
    db: Session = Depends(get_db),
):
    product = db.query(Product).filter_by(article=article).first()
    if not product:
        raise HTTPException(status_code=404, detail="product not found")
    rec = calculate_recommendation(product, competitor_min_price=None, min_margin_pct=min_margin)
    return RecommendationResponse(
        article=product.article,
        recommended_price=rec.recommended_price,
        floor_price=rec.floor_price,
        reasons=rec.reasons,
    )
