from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace

from tasks import build_missing_onec_subject_updates as task
from tasks.build_missing_onec_subject_updates import build_missing_subject_update_rows


class FakeClassifier:
    def __init__(self, mapping: dict[str, str | None]) -> None:
        self.mapping = mapping
        self.calls: list[str] = []

    def classify(self, name: str | None) -> str | None:
        self.calls.append(name or "")
        return self.mapping.get(name or "")


def test_main_uses_central_read_only_scope_and_role_specific_onec_engine(
    monkeypatch,
    capsys,
) -> None:
    session = object()
    scope_calls: list[bool] = []
    engine_calls: list[tuple[str, int, int]] = []

    class FakeOnecEngine:
        disposed = False

        def dispose(self) -> None:
            self.disposed = True

    class RuntimeClassifier:
        llm_calls = 0
        llm_failed = 0
        closed = False

        def classify(self, _name: str | None) -> str | None:
            return None

        def close(self) -> None:
            self.closed = True

    onec_engine = FakeOnecEngine()
    classifier = RuntimeClassifier()

    @contextmanager
    def fake_session_scope(*, read_only: bool = False):
        scope_calls.append(read_only)
        yield session

    def fake_build_onec_engine(
        database_url: str,
        *,
        query_timeout_seconds: int,
        login_timeout_seconds: int,
    ) -> FakeOnecEngine:
        engine_calls.append((database_url, query_timeout_seconds, login_timeout_seconds))
        return onec_engine

    candidates = [
        {
            "article": "A1",
            "nomenclature_code": "РБ0000001",
            "name": "Дисплей iPhone 12",
        }
    ]

    monkeypatch.setattr(task, "load_ut103_env_file", lambda: None)
    monkeypatch.setattr(
        task,
        "get_settings",
        lambda: SimpleNamespace(
            database_url="postgresql://settings-app",
            onec_database_url="mssql+pyodbc://onec-snapshot",
            onec_query_timeout_seconds=55,
            onec_login_timeout_seconds=9,
        ),
    )
    monkeypatch.setattr(task, "session_scope", fake_session_scope)
    monkeypatch.setattr(task, "build_onec_engine", fake_build_onec_engine)
    monkeypatch.setattr(
        task,
        "load_missing_onec_subject_candidates",
        lambda engine: candidates if engine is onec_engine else [],
    )
    monkeypatch.setattr(
        task,
        "load_onec_subject_catalog_values",
        lambda engine: {"дисплей"} if engine is onec_engine else set(),
    )

    def fake_load_generated_subjects(
        current_session: object,
        articles,
    ) -> dict[str, str]:
        assert current_session is session
        assert list(articles) == ["A1"]
        return {"A1": "дисплей"}

    monkeypatch.setattr(task, "load_generated_subjects", fake_load_generated_subjects)
    monkeypatch.setattr(
        task.CategoryClassifier,
        "from_env",
        lambda **_kwargs: classifier,
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "build_missing_onec_subject_updates",
            "--message-id",
            "missing-onec-subject-test",
            "--json",
        ],
    )

    assert task.main() == 0
    assert scope_calls == [True]
    assert engine_calls == [("mssql+pyodbc://onec-snapshot", 55, 9)]
    assert onec_engine.disposed is True
    assert classifier.closed is True
    payload = json.loads(capsys.readouterr().out)
    assert payload["message_id"] == "missing-onec-subject-test"
    assert payload["rows"] == 1


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
