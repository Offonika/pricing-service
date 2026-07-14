from __future__ import annotations

from datetime import datetime

from app.models import CompetitorItem, Product
from app.models.competitor_item_match import (
    CompetitorItemMatch,
    CompetitorItemMatchMethod,
    CompetitorItemMatchStatus,
)
from tasks.accept_competitor_match_suggestions import accept_suggestions


def test_accept_suggestions_dry_run_does_not_update(db_session):
    product = Product(article="P1", name="Дисплей iPhone 17")
    item = CompetitorItem(
        competitor="moba",
        external_id="LCD-PMI-17",
        name="Дисплей iPhone 17",
        first_seen_at=datetime(2026, 5, 2, 10, 0, 0),
    )
    db_session.add_all([product, item])
    db_session.flush()
    match = CompetitorItemMatch(
        competitor_item_id=item.id,
        product_id=product.id,
        status=CompetitorItemMatchStatus.SUGGESTED,
        method=CompetitorItemMatchMethod.EMBEDDING_AUTO,
        final_score=0.9,
    )
    db_session.add(match)
    db_session.commit()

    report = accept_suggestions(db_session, first_seen_after="2026-05-02", dry_run=True)

    db_session.refresh(match)
    assert report["would_accept"] == 1
    assert report["accepted"] == 0
    assert match.status == CompetitorItemMatchStatus.SUGGESTED


def test_accept_suggestions_apply_updates_generated_only(db_session):
    product = Product(article="P1", name="Дисплей iPhone 17")
    item = CompetitorItem(
        competitor="moba",
        external_id="LCD-PMI-17",
        name="Дисплей iPhone 17",
        first_seen_at=datetime(2026, 5, 2, 10, 0, 0),
    )
    manual_item = CompetitorItem(
        competitor="moba",
        external_id="MANUAL",
        name="Manual suggestion",
        first_seen_at=datetime(2026, 5, 2, 10, 0, 0),
    )
    db_session.add_all([product, item, manual_item])
    db_session.flush()
    generated = CompetitorItemMatch(
        competitor_item_id=item.id,
        product_id=product.id,
        status=CompetitorItemMatchStatus.SUGGESTED,
        method=CompetitorItemMatchMethod.EMBEDDING_AUTO,
        final_score=0.9,
    )
    manual = CompetitorItemMatch(
        competitor_item_id=manual_item.id,
        product_id=product.id,
        status=CompetitorItemMatchStatus.SUGGESTED,
        method=CompetitorItemMatchMethod.MANUAL,
        final_score=1,
    )
    db_session.add_all([generated, manual])
    db_session.commit()

    report = accept_suggestions(
        db_session,
        first_seen_after="2026-05-02",
        dry_run=False,
        batch_id="test-batch",
    )

    db_session.refresh(generated)
    db_session.refresh(manual)
    assert report["accepted"] == 1
    assert generated.status == CompetitorItemMatchStatus.ACCEPTED
    assert generated.rationale_json["bulk_accept_suggested"]["batch_id"] == "test-batch"
    assert manual.status == CompetitorItemMatchStatus.SUGGESTED


def test_accept_suggestions_respects_min_score(db_session):
    product = Product(article="P1", name="Дисплей iPhone 17")
    item = CompetitorItem(
        competitor="moba",
        external_id="LOW",
        name="Low score",
        first_seen_at=datetime(2026, 5, 2, 10, 0, 0),
    )
    db_session.add_all([product, item])
    db_session.flush()
    db_session.add(
        CompetitorItemMatch(
            competitor_item_id=item.id,
            product_id=product.id,
            status=CompetitorItemMatchStatus.SUGGESTED,
            method=CompetitorItemMatchMethod.EMBEDDING_AUTO,
            final_score=0.7,
        )
    )
    db_session.commit()

    report = accept_suggestions(
        db_session,
        first_seen_after="2026-05-02",
        min_score=0.8,
        dry_run=False,
    )

    assert report["accepted"] == 0
    assert report["skipped_low_score"] == 1
