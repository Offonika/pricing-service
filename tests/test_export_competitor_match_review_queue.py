from __future__ import annotations

from datetime import datetime

from app.models import CompetitorItem, Product
from app.models.competitor_item_compatibility import CompetitorItemCompatibility
from app.models.competitor_item_match import (
    CompetitorItemMatch,
    CompetitorItemMatchMethod,
    CompetitorItemMatchStatus,
)
from tasks.export_competitor_match_review_queue import build_review_queue


def test_review_queue_exports_reason_codes_and_alternatives(db_session):
    product = Product(
        article="P-IPH17",
        name="Дисплей для Apple iPhone 17 + тачскрин (черный) (ORIG100)",
        brand="Apple",
        category="Дисплеи для телефонов",
        subject="дисплей",
        color="черный",
        quality="Original",
    )
    item = CompetitorItem(
        competitor="moba",
        external_id="LCD-PMI-17",
        name="Дисплей для iPhone 17 Черный - OR",
        item_type="display",
        parsed_device_brand="apple",
        parsed_device_model="iphone 17",
        attrs_json={"item_type": "display"},
        first_seen_at=datetime(2026, 5, 2, 10, 0, 0),
    )
    db_session.add_all([product, item])
    db_session.flush()
    db_session.add(
        CompetitorItemMatch(
            competitor_item_id=item.id,
            product_id=product.id,
            status=CompetitorItemMatchStatus.AMBIGUOUS,
            method=CompetitorItemMatchMethod.EMBEDDING_AUTO,
            final_score=0.86,
            score_embed_gap=0.005,
            rationale_json={
                "best_score": 0.86,
                "gap": 0.005,
                "display_quality_review": {"reason": "display_quality_unknown_on_one_side"},
                "filtered_candidates": [
                    {
                        "product_id": product.id,
                        "article": "P-IPH17",
                        "name": product.name,
                        "score": 0.86,
                    },
                    {
                        "product_id": 999,
                        "article": "P-ALT",
                        "name": "Дисплей iPhone 17 Copy",
                        "score": 0.855,
                    },
                ],
            },
        )
    )
    db_session.add(
        CompetitorItemCompatibility(
            competitor_item_id=item.id,
            device_brand="apple",
            device_model="iphone 17",
            source="test",
        )
    )
    db_session.commit()

    payload = build_review_queue(db_session, first_seen_after="2026-05-01")

    assert payload["total"] == 1
    row = payload["items"][0]
    assert row["status"] == "ambiguous"
    assert row["competitor_name"] == "Дисплей для iPhone 17 Черный - OR"
    assert row["product_article"] == "P-IPH17"
    assert row["has_compatibility"] is True
    assert "small_gap" in row["reason_codes"]
    assert "multiple_close_candidates" in row["reason_codes"]
    assert "display_quality_review" in row["reason_codes"]
    assert row["alternatives"][1]["article"] == "P-ALT"


def test_review_queue_marks_actionable_missing_compatibility(db_session):
    product = Product(
        article="P-BTT-16",
        name="Аккумулятор для Apple iPhone 16",
        brand="Apple",
        category="АКБ",
        subject="аккумулятор",
    )
    item = CompetitorItem(
        competitor="moba",
        external_id="BTT-PMI-16",
        name="Аккумулятор для iPhone 16",
        item_type="battery",
        parsed_device_brand="apple",
        parsed_device_model="iphone 16",
        attrs_json={"item_type": "battery"},
        first_seen_at=datetime(2026, 5, 2, 10, 0, 0),
    )
    db_session.add_all([product, item])
    db_session.flush()
    db_session.add(
        CompetitorItemMatch(
            competitor_item_id=item.id,
            product_id=product.id,
            status=CompetitorItemMatchStatus.NEEDS_REVIEW,
            method=CompetitorItemMatchMethod.EMBEDDING_AUTO,
            final_score=0.72,
            score_embed_gap=0.03,
            rationale_json={"filtered_candidates": []},
        )
    )
    db_session.commit()

    payload = build_review_queue(db_session, first_seen_after="2026-05-01")

    assert payload["total"] == 1
    row = payload["items"][0]
    assert row["status"] == "needs_review"
    assert "low_score" in row["reason_codes"]
    assert "missing_compatibility" in row["reason_codes"]
