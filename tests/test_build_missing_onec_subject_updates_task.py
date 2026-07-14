from __future__ import annotations

from datetime import date

from tasks.build_missing_onec_subject_updates import build_missing_subject_update_rows


class FakeClassifier:
    def __init__(self, mapping: dict[str, str | None]) -> None:
        self.mapping = mapping
        self.calls: list[str] = []

    def classify(self, name: str | None) -> str | None:
        self.calls.append(name or "")
        return self.mapping.get(name or "")


def test_build_missing_subject_update_rows_reuses_generated_subject_and_classifies_gaps() -> None:
    classifier = FakeClassifier(
        {
            "Аккумулятор iPhone 12": "аккумулятор",
            "Непонятная позиция": "неизвестно",
            "Редкая позиция": "редкий предмет",
        }
    )

    result = build_missing_subject_update_rows(
        [
            {
                "article": "A1",
                "nomenclature_code": "РБ0000001",
                "name": "Дисплей iPhone 12",
            },
            {
                "article": "A2",
                "nomenclature_code": "РБ0000002",
                "name": "Аккумулятор iPhone 12",
            },
            {
                "article": "A3",
                "nomenclature_code": "РБ0000003",
                "name": "Кулер ноутбука",
            },
            {
                "article": "A4",
                "nomenclature_code": "РБ0000004",
                "name": "Непонятная позиция",
            },
            {
                "article": "A5",
                "nomenclature_code": "РБ0000005",
                "name": "Редкая позиция",
            },
        ],
        {
            "A1": "Дисплей",
            "A3": "кулер",
        },
        {"дисплей", "аккумулятор"},
        classifier=classifier,
        run_date=date(2026, 6, 27),
    )

    assert [row.nomenclature_code for row in result.rows] == ["РБ0000001", "РБ0000002"]
    assert [row.property_name for row in result.rows] == ["Предмет", "Предмет"]
    assert [row.value_type for row in result.rows] == ["property_value", "property_value"]
    assert [row.new_value_name for row in result.rows] == ["дисплей", "аккумулятор"]
    assert result.rows[0].idempotency_key == "nom-prop:РБ0000001:Предмет:2026-06-27:r1"
    assert result.source_counts == {"classifier": 1, "subject_generated": 1}
    assert result.subject_counts == {"аккумулятор": 1, "дисплей": 1}
    assert classifier.calls == [
        "Аккумулятор iPhone 12",
        "Непонятная позиция",
        "Редкая позиция",
    ]

    skipped_reasons = {item["article"]: item["reason"] for item in result.skipped}
    assert skipped_reasons == {
        "A3": "subject_catalog_value_missing",
        "A4": "subject_unknown",
        "A5": "subject_catalog_value_missing",
    }


def test_build_missing_subject_update_rows_honors_limit_after_valid_rows() -> None:
    classifier = FakeClassifier({"Аккумулятор iPhone 12": "аккумулятор"})

    result = build_missing_subject_update_rows(
        [
            {
                "article": "A1",
                "nomenclature_code": "РБ0000001",
                "name": "Дисплей iPhone 12",
            },
            {
                "article": "A2",
                "nomenclature_code": "РБ0000002",
                "name": "Аккумулятор iPhone 12",
            },
        ],
        {"A1": "дисплей"},
        {"дисплей", "аккумулятор"},
        classifier=classifier,
        run_date=date(2026, 6, 27),
        limit=1,
    )

    assert len(result.rows) == 1
    assert result.rows[0].nomenclature_code == "РБ0000001"
    assert classifier.calls == []
