"""Отчёт-кандидатов на авто-детект "Эксклюзив" (нет ни одного сопоставления с конкурентом).

Read-only инструмент, ничего не пишет в БД и не меняет ``decide_commercial_marks``.

Контекст: в контуре статусов ассортимента есть коммерческая метка ``exclusive``
(``CommercialMark.EXCLUSIVE``), которая требует 7 вручную заполняемых полей
доказательной базы (``exclusive_kind``/``exclusive_reason``/``exclusive_approved_by``/
``exclusive_checked_at``/``exclusive_evidence_refs``/``exclusive_min_stock_qty``/
``exclusive_review_period_days``) — за всю историю базы (47595 карточек) заполнена
1 раз. Параллельно в ``docs/PRD.md``/``docs/price-strategies.md`` описан ВТОРОЙ,
независимый "Эксклюзив" — "есть у нас, нет у конкурентов", который должен был
определяться автоматически через сопоставление с конкурентами (``ProductMatch``),
но ``exclusive_markup``/``lost_exclusive`` из тех доков в коде не реализованы вообще.

Данные для автоопределения уже есть: ``ProductMatch`` хранит, с какими конкурентами
сопоставлен товар. Этот отчёт считает, у скольких карточек в scope "Дисплеи"
(единственный scope, где вообще работает контур статусов) НЕТ ни одного
сопоставления с конкурентом — то есть являются кандидатами на авто-детект
Эксклюзива по формуле "0 активных матчей = эксклюзив".

Ничего не решает и не применяет — только считает и показывает, чтобы можно было
обсудить реальные цифры перед тем, как менять формулу/писать в commercial_marks.
"""

from __future__ import annotations

import argparse
import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.infrastructure.db.engines import build_engine
from app.models.product import Product
from app.models.product_match import ProductMatch
from app.services.assortment_lifecycle_classification_store import (
    ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE as CLASSIFICATION_TABLE,
)


def build_report(
    session: Session, *, folder_filter: str = "дисплеи", limit: int | None = None
) -> dict:
    match_counts = dict(
        session.execute(
            select(ProductMatch.product_id, func.count()).group_by(ProductMatch.product_id)
        ).all()
    )

    classification_rows = session.execute(
        select(
            CLASSIFICATION_TABLE.c.nomenclature_code,
            CLASSIFICATION_TABLE.c.name,
            CLASSIFICATION_TABLE.c.folder,
            CLASSIFICATION_TABLE.c.status_label,
            CLASSIFICATION_TABLE.c.article,
            CLASSIFICATION_TABLE.c.commercial_marks,
        ).where(
            CLASSIFICATION_TABLE.c.folder.ilike(f"%{folder_filter}%"),
            CLASSIFICATION_TABLE.c.article != "",
        )
    ).all()

    articles = {row.article for row in classification_rows}
    products_by_article = {
        product.article: product
        for product in session.execute(
            select(Product).where(Product.article.in_(articles))
        ).scalars()
    }

    already_marked = 0
    candidates = []
    has_competitors = 0
    no_product_row = 0

    for row in classification_rows:
        marks = row.commercial_marks or []
        if "exclusive" in marks:
            already_marked += 1
            continue
        product = products_by_article.get(row.article)
        if product is None:
            no_product_row += 1
            continue
        match_count = match_counts.get(product.id, 0)
        if match_count > 0:
            has_competitors += 1
            continue
        candidates.append(
            {
                "nomenclature_code": row.nomenclature_code,
                "article": row.article,
                "name": row.name,
                "folder": row.folder,
                "status_label": row.status_label,
            }
        )

    if limit:
        candidates = candidates[:limit]

    return {
        "scope_folder_filter": folder_filter,
        "classification_rows_in_scope": len(classification_rows),
        "already_marked_exclusive": already_marked,
        "has_at_least_one_competitor_match": has_competitors,
        "no_matching_product_row": no_product_row,
        "candidate_count": len(candidates),
        "candidates": candidates,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", default="дисплеи", help="Фильтр по папке (подстрока)")
    parser.add_argument("--limit", type=int, help="Ограничить список кандидатов в выводе")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    settings = get_settings()
    engine = build_engine(settings.database_url)
    with Session(engine) as session:
        report = build_report(session, folder_filter=args.folder, limit=args.limit)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
