from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import Product
from app.services.product_classification import recompute_product_classification
from tasks.normalize_product_nomenclature_kind import normalize_kinds
from tasks.normalize_product_subject import normalize_subjects
from tasks.report_product_classification_diff import build_report


def test_recompute_product_classification_prefers_1c() -> None:
    product = Product(
        article="p-1",
        name="Test",
        subject_1c="Аккумулятор",
        subject_generated="Дисплей",
        vid_nomenklatury_1c="Питание и зарядка (розница + сервис)",
        vid_nomenklatury_generated="Дисплеи/сенсор/стекло",
    )

    recompute_product_classification(product)

    assert product.subject == "Аккумулятор"
    assert product.subject_source == "1c"
    assert product.vid_nomenklatury == "Питание и зарядка (розница + сервис)"
    assert product.vid_nomenklatury_source == "1c"


def test_normalize_subjects_writes_generated_and_keeps_1c(db_session: Session) -> None:
    product = Product(
        article="p-2",
        name="Дисплей для Apple iPhone 11",
        subject_1c="Аккумулятор",
        subject="Аккумулятор",
        subject_source="1c",
    )
    db_session.add(product)
    db_session.commit()

    stats = normalize_subjects(
        db_session,
        name_contains=None,
        name_not_startswith=None,
        subject_in=None,
        missing_only=True,
        overwrite=False,
        limit=None,
        use_llm=False,
        llm_limit=0,
        llm_only=False,
        force_llm=False,
        default_category=None,
        treat_unknown_as_missing=False,
        unknown_values=None,
        chunk_size=100,
        min_id=None,
        max_id=None,
        active_only=False,
        not_deleted_only=False,
    )

    db_session.refresh(product)
    assert stats["updated"] == 1
    assert product.subject_1c == "Аккумулятор"
    assert product.subject_generated == "дисплей"
    assert product.subject == "Аккумулятор"
    assert product.subject_source == "1c"
    assert product.vid_nomenklatury_generated == "Дисплеи/сенсор/стекло"


def test_normalize_kinds_generates_comparable_kind_from_1c_subject(db_session: Session) -> None:
    product = Product(
        article="p-3",
        name="Аккумулятор для Apple iPhone 11",
        subject_1c="Аккумулятор",
        subject="Аккумулятор",
        subject_source="1c",
        vid_nomenklatury_1c="Питание и зарядка (розница + сервис)",
        vid_nomenklatury="Питание и зарядка (розница + сервис)",
        vid_nomenklatury_source="1c",
    )
    db_session.add(product)
    db_session.commit()

    stats = normalize_kinds(
        db_session,
        subject_in=None,
        missing_only=True,
        overwrite=False,
        limit=None,
        chunk_size=100,
        min_id=None,
        max_id=None,
        active_only=False,
        not_deleted_only=False,
    )

    db_session.refresh(product)
    assert stats["updated"] == 1
    assert product.vid_nomenklatury_1c == "Питание и зарядка (розница + сервис)"
    assert product.vid_nomenklatury_generated == "Питание и зарядка (розница + сервис)"
    assert product.vid_nomenklatury == "Питание и зарядка (розница + сервис)"
    assert product.vid_nomenklatury_source == "1c"


def test_build_report_returns_only_differences(db_session: Session) -> None:
    same_product = Product(
        article="p-4",
        name="Same",
        subject_1c="Аккумулятор",
        subject_generated="Аккумулятор",
        subject="Аккумулятор",
        subject_source="1c",
        vid_nomenklatury_1c="Питание и зарядка (розница + сервис)",
        vid_nomenklatury_generated="Питание и зарядка (розница + сервис)",
        vid_nomenklatury="Питание и зарядка (розница + сервис)",
        vid_nomenklatury_source="1c",
    )
    diff_product = Product(
        article="p-5",
        name="Diff",
        subject_1c="Аккумулятор",
        subject_generated="дисплей",
        subject="Аккумулятор",
        subject_source="1c",
        vid_nomenklatury_1c="Питание и зарядка (розница + сервис)",
        vid_nomenklatury_generated="Дисплеи/сенсор/стекло",
        vid_nomenklatury="Питание и зарядка (розница + сервис)",
        vid_nomenklatury_source="1c",
    )
    db_session.add_all([same_product, diff_product])
    db_session.commit()

    report = build_report(db_session)

    assert report["count"] == 1
    assert report["items"][0]["article"] == "p-5"
