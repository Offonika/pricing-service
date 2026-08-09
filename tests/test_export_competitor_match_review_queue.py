from __future__ import annotations

from datetime import datetime

from app.models import CompetitorItem, Product
from app.models.competitor_item_compatibility import CompetitorItemCompatibility
from app.models.competitor_item_match import (
    CompetitorItemMatch,
    CompetitorItemMatchMethod,
    CompetitorItemMatchStatus,
)
from tasks.export_competitor_match_review_queue import _review_bucket, build_review_queue


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
    assert payload["bucket_counts"] == {"display_attributes": 1}
    assert payload["priority_counts"] == {"2": 1}
    row = payload["items"][0]
    assert row["status"] == "ambiguous"
    assert row["review_bucket"] == "display_attributes"
    assert row["review_priority"] == 2
    assert row["business_value_score"] == 0.75
    assert row["uncertainty_score"] > 0
    assert row["training_examples"] == 0
    assert row["training_scarcity_score"] == 1.0
    assert row["family_group"] == "moba:display:iphone 17"
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
    assert payload["bucket_counts"] == {"compatibility_or_family": 1}
    row = payload["items"][0]
    assert row["status"] == "needs_review"
    assert row["review_bucket"] == "compatibility_or_family"
    assert row["review_priority"] == 1
    assert "low_score" in row["reason_codes"]
    assert "missing_compatibility" in row["reason_codes"]


def test_review_bucket_keeps_other_family_mismatch_low_priority():
    bucket = _review_bucket(
        reasons=["status:needs_review", "family_mismatch:adhesive->phone_camera_glass"],
        item_type="other",
    )

    assert bucket == "other_low_priority"


def test_review_bucket_keeps_low_score_ahead_of_non_ambiguous_close_candidates():
    bucket = _review_bucket(
        reasons=["status:suggested", "low_score", "multiple_close_candidates"],
        item_type="battery",
    )

    assert bucket == "low_score"


def test_review_bucket_keeps_small_gap_as_candidate_tie_even_with_low_score():
    bucket = _review_bucket(
        reasons=["status:ambiguous", "low_score", "small_gap", "multiple_close_candidates"],
        item_type="housing",
    )

    assert bucket == "candidate_tie"


def test_review_queue_does_not_bucket_domain_suggested_small_gap_as_tie(db_session):
    product = Product(
        article="P-BTT-14PM",
        name=(
            "Аккумулятор для Apple iPhone 14 Pro Max (F5ENERGY) (усиленный) "
            "(4770 мАч) (SPECIAL EDITION) (SYSTEM DIAGNOSABLE)"
        ),
        brand="Apple",
        category="АКБ",
        subject="аккумулятор",
    )
    item = CompetitorItem(
        competitor="moba",
        external_id="BTT-PMIPRM140-VRF-HC-NEW",
        name=(
            "Аккумулятор для iPhone 14 Pro Max - Battery Collection с верификацией "
            '"Новая запчасть" - усиленная 4750 mAh'
        ),
        item_type="battery",
        parsed_device_brand="apple",
        parsed_device_model="iphone 14 pro max",
        attrs_json={"item_type": "battery"},
        first_seen_at=datetime(2026, 5, 2, 10, 0, 0),
    )
    db_session.add_all([product, item])
    db_session.flush()
    db_session.add(
        CompetitorItemCompatibility(
            competitor_item_id=item.id,
            device_brand="apple",
            device_model="iphone 14 pro max",
            source="test",
        )
    )
    db_session.add(
        CompetitorItemMatch(
            competitor_item_id=item.id,
            product_id=product.id,
            status=CompetitorItemMatchStatus.SUGGESTED,
            method=CompetitorItemMatchMethod.EMBEDDING_AUTO,
            final_score=0.755,
            score_embed_gap=0.006,
            rationale_json={
                "best_score": 0.755,
                "gap": 0.006,
                "battery_verification_suggest": {
                    "reason": ("battery_verification_signal_with_system_diagnosable_model_overlap")
                },
                "filtered_candidates": [
                    {
                        "product_id": product.id,
                        "article": "P-BTT-14PM",
                        "name": product.name,
                        "score": 0.755,
                    },
                    {
                        "product_id": 999,
                        "article": "P-ALT",
                        "name": "Аккумулятор для Apple iPhone 14 Pro Max (Premium)",
                        "score": 0.749,
                    },
                ],
            },
        )
    )
    db_session.commit()

    payload = build_review_queue(db_session, first_seen_after="2026-05-01")

    row = payload["items"][0]
    assert row["review_bucket"] == "low_score"
    assert "low_score" in row["reason_codes"]
    assert "small_gap" not in row["reason_codes"]
    assert "multiple_close_candidates" not in row["reason_codes"]


def test_review_queue_exports_effective_item_type_for_display_glue(db_session):
    product = Product(
        article="P-GLUE",
        name="Клей-герметик Zhanlida B-7000, 3 мл",
        category="tools",
        subject="клей",
    )
    item = CompetitorItem(
        competitor="liberti",
        external_id="474118",
        name="Клей для сборки рамок с тачскрином B-7000 (3 мл.)",
        normalized_title="Клей для сборки рамок с тачскрином B-7000 3 мл",
        item_type="display",
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
            final_score=0.71,
            score_embed_gap=0.03,
            rationale_json={"filtered_candidates": []},
        )
    )
    db_session.commit()

    payload = build_review_queue(db_session, first_seen_after="2026-05-01")

    row = payload["items"][0]
    assert row["competitor_item_type"] == "other"
    assert row["competitor_raw_item_type"] == "display"
    assert row["review_bucket"] == "other_low_priority"
