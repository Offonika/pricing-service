from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.models import Base
from app.models.customer_price_type import (
    CustomerPriceTypeProfile,
    CustomerPriceTypeReviewBatch,
    CustomerPriceTypeReviewBatchItem,
)
from app.services.customer_price_type_review_batches import (
    CustomerPriceTypeReviewBatchConflict,
    import_review_batch,
)


def _ref(value: int) -> str:
    return f"0x{value:032x}"


def _write_sources(tmp_path: Path) -> tuple[Path, Path]:
    working = tmp_path / "working.csv"
    review = tmp_path / "review.csv"
    working.write_text(
        "№;Код;Итоговый тип цены\n"
        + "\n".join(f"{index};РБ{index:06d};2.Бронзовый" for index in range(1, 51))
        + "\n",
        encoding="utf-8",
    )
    review_rows = []
    for index in range(51, 83):
        if index <= 68:
            expected_type = "Розница"
        elif index <= 71:
            expected_type = "2.Бронзовый бн"
        elif index == 72:
            expected_type = "3.Серебряный"
        elif index == 73:
            expected_type = "4.Золотой"
        else:
            expected_type = ""
        review_rows.append(f"{index - 50};РБ{index:06d};{expected_type}")
    review.write_text(
        "№;Код;Итоговый тип цены\n" + "\n".join(review_rows) + "\n",
        encoding="utf-8",
    )
    return working, review


def test_review_batch_dry_run_apply_and_idempotency(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'batch.db'}")
    Base.metadata.create_all(engine)
    working, review = _write_sources(tmp_path)
    try:
        with Session(engine) as session:
            session.add_all(
                [
                    CustomerPriceTypeProfile(
                        counterparty_ref=_ref(index),
                        counterparty_code=f"РБ{index:06d}",
                    )
                    for index in range(1, 83)
                ]
            )
            session.commit()
            preview = import_review_batch(
                session,
                working_bronze_csv=working,
                review_queue_csv=review,
            )
            assert preview.applied is False
            assert preview.counts == {"working_bronze": 50, "review_queue": 32}
            assert session.scalar(select(func.count(CustomerPriceTypeReviewBatch.id))) == 0

            applied = import_review_batch(
                session,
                working_bronze_csv=working,
                review_queue_csv=review,
                apply=True,
            )
            repeated = import_review_batch(
                session,
                working_bronze_csv=working,
                review_queue_csv=review,
                apply=True,
            )
            assert applied.created is True
            assert repeated.created is False
            assert repeated.applied is False
            assert session.scalar(select(func.count(CustomerPriceTypeReviewBatch.id))) == 1
            assert session.scalar(select(func.count(CustomerPriceTypeReviewBatchItem.id))) == 82

            review.write_text(
                review.read_text(encoding="utf-8").replace("РБ000051", "РБ900051", 1),
                encoding="utf-8",
            )
            with pytest.raises(CustomerPriceTypeReviewBatchConflict):
                import_review_batch(
                    session,
                    working_bronze_csv=working,
                    review_queue_csv=review,
                    apply=True,
                )
    finally:
        engine.dispose()


def test_review_batch_rejects_wrong_review_queue_distribution(tmp_path: Path) -> None:
    working, review = _write_sources(tmp_path)
    review.write_text(
        review.read_text(encoding="utf-8").replace("2.Бронзовый бн", "Розница", 1),
        encoding="utf-8",
    )
    engine = create_engine("sqlite://")
    try:
        with Session(engine) as session, pytest.raises(ValueError, match="18 retail"):
            import_review_batch(
                session,
                working_bronze_csv=working,
                review_queue_csv=review,
            )
    finally:
        engine.dispose()


def test_review_batch_rejects_unresolved_profile(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'unresolved.db'}")
    Base.metadata.create_all(engine)
    working, review = _write_sources(tmp_path)
    try:
        with Session(engine) as session:
            session.add_all(
                [
                    CustomerPriceTypeProfile(
                        counterparty_ref=_ref(index),
                        counterparty_code=f"РБ{index:06d}",
                    )
                    for index in range(1, 82)
                ]
            )
            session.commit()
            with pytest.raises(ValueError, match="РБ000082"):
                import_review_batch(
                    session,
                    working_bronze_csv=working,
                    review_queue_csv=review,
                )
    finally:
        engine.dispose()
