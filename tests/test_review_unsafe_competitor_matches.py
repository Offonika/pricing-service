from __future__ import annotations

from datetime import UTC, date, datetime

from app.models import CompetitorItem, Product
from app.models.competitor_item_match import (
    CompetitorItemMatch,
    CompetitorItemMatchMethod,
    CompetitorItemMatchStatus,
)
from tasks.review_unsafe_competitor_matches import move_unsafe_matches_to_review


def _accepted_match(
    *,
    item: CompetitorItem,
    product: Product,
    rationale_json: dict | None = None,
    final_score: float = 0.88,
) -> CompetitorItemMatch:
    return CompetitorItemMatch(
        competitor_item_id=item.id,
        product_id=product.id,
        status=CompetitorItemMatchStatus.ACCEPTED,
        method=CompetitorItemMatchMethod.EMBEDDING_AUTO,
        final_score=final_score,
        rationale_json=rationale_json,
    )


def _run_review(db_session):
    return move_unsafe_matches_to_review(
        db_session,
        first_seen_after=date(2026, 5, 1),
        all_accepted_without_compat=False,
        dry_run=False,
        sample_limit=10,
    )


def test_review_unsafe_keeps_safe_other_family_auto_accept(db_session):
    product = Product(
        article="076251",
        name="Держатель сим-карты для Samsung A075 Galaxy A07 4G (зеленый)",
    )
    item = CompetitorItem(
        competitor="moba",
        external_id="HLD-SIM-SSG-A075F-GN",
        name="Держатель SIM для Samsung Galaxy A07 4G (A075F) Зеленый",
        normalized_title="Держатель SIM Samsung Galaxy A07 4G A075F Зеленый",
        item_type="other",
        first_seen_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    db_session.add_all([product, item])
    db_session.flush()
    match = _accepted_match(
        item=item,
        product=product,
        rationale_json={
            "auto_accept_other_safe_family": {
                "reason": "phone_sim_tray_family_model_or_code_color_match",
                "query_min_score": 0.60,
                "min_score": 0.80,
            }
        },
        final_score=0.88,
    )
    db_session.add(match)
    db_session.flush()

    result = _run_review(db_session)

    assert result["processed"] == 1
    assert result["safe_auto_accept_kept"] == 1
    assert result["unsafe_auto_accept_moved_to_review"] == 0
    assert result["samples"][0]["action"] == "kept_safe_auto_accept"
    assert result["samples"][0]["rationale_key"] == "auto_accept_other_safe_family"
    db_session.refresh(match)
    assert match.status == CompetitorItemMatchStatus.ACCEPTED


def test_review_unsafe_keeps_safe_housing_auto_accept(db_session):
    product = Product(
        article="056223",
        name="Задняя крышка для Realme C33 (RMX3624) (золотистый)",
    )
    item = CompetitorItem(
        competitor="moba",
        external_id="BC-RME-C33-GLD",
        name="Задняя крышка для Realme C33 (RMX3624) Золотистый",
        normalized_title="Задняя крышка Realme C33 RMX3624 Золотистый",
        item_type="housing",
        first_seen_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    db_session.add_all([product, item])
    db_session.flush()
    match = _accepted_match(
        item=item,
        product=product,
        rationale_json={
            "auto_accept_housing_part": {
                "reason": "housing_part_model_or_code_color_kind_match",
                "query_min_score": 0.75,
                "min_score": 0.80,
            }
        },
        final_score=0.79,
    )
    db_session.add(match)
    db_session.flush()

    result = _run_review(db_session)

    assert result["processed"] == 1
    assert result["safe_auto_accept_kept"] == 1
    db_session.refresh(match)
    assert match.status == CompetitorItemMatchStatus.ACCEPTED


def test_review_unsafe_moves_accepted_without_safe_rationale(db_session):
    product = Product(
        article="076251",
        name="Держатель сим-карты для Samsung A075 Galaxy A07 4G (зеленый)",
    )
    item = CompetitorItem(
        competitor="moba",
        external_id="HLD-SIM-SSG-A075F-GN",
        name="Держатель SIM для Samsung Galaxy A07 4G (A075F) Зеленый",
        normalized_title="Держатель SIM Samsung Galaxy A07 4G A075F Зеленый",
        item_type="other",
        first_seen_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    db_session.add_all([product, item])
    db_session.flush()
    match = _accepted_match(item=item, product=product, rationale_json={})
    db_session.add(match)
    db_session.flush()

    result = _run_review(db_session)

    assert result["processed"] == 1
    assert result["safe_auto_accept_kept"] == 0
    assert result["unsafe_auto_accept_moved_to_review"] == 1
    assert result["unsafe_reason_counts"] == {"missing_attrs_or_compatibility": 1}
    db_session.refresh(match)
    assert match.status == CompetitorItemMatchStatus.NEEDS_REVIEW
    assert "unsafe_auto_accept_review" in match.rationale_json


def test_review_unsafe_moves_safe_rationale_when_guardrail_conflicts(db_session):
    product = Product(
        article="055763",
        name="Аккумулятор для Apple iPhone 12 / iPhone 12 Pro (без шлейфа)",
    )
    item = CompetitorItem(
        competitor="moba",
        external_id="HLD-SIM-SSG-A075F-GN",
        name="Держатель SIM для Samsung Galaxy A07 4G (A075F) Зеленый",
        normalized_title="Держатель SIM Samsung Galaxy A07 4G A075F Зеленый",
        item_type="other",
        first_seen_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    db_session.add_all([product, item])
    db_session.flush()
    match = _accepted_match(
        item=item,
        product=product,
        rationale_json={
            "auto_accept_other_safe_family": {
                "reason": "phone_sim_tray_family_model_or_code_color_match",
                "query_min_score": 0.60,
            }
        },
        final_score=0.88,
    )
    db_session.add(match)
    db_session.flush()

    result = _run_review(db_session)

    assert result["processed"] == 1
    assert result["safe_auto_accept_kept"] == 0
    assert result["unsafe_auto_accept_moved_to_review"] == 1
    assert result["samples"][0]["rationale_key"] == "auto_accept_other_safe_family"
    db_session.refresh(match)
    assert match.status == CompetitorItemMatchStatus.NEEDS_REVIEW
