from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace

from tasks import report_product_compatibility_sync


def test_product_compatibility_report_cli_uses_role_specific_read_only_db_access(
    tmp_path,
    monkeypatch,
) -> None:
    session = object()
    scope_calls: list[tuple[bool, str | None]] = []
    engine_calls: list[tuple[str, int, int]] = []
    report_calls: list[tuple[object, object, set[str], dict[str, list[str]], bool, int]] = []

    class FakeOnecEngine:
        disposed = False

        def dispose(self) -> None:
            self.disposed = True

    onec_engine = FakeOnecEngine()

    @contextmanager
    def fake_session_scope(
        *,
        read_only: bool = False,
        database_url: str | None = None,
    ):
        scope_calls.append((read_only, database_url))
        yield session

    def fake_build_onec_engine(
        database_url: str,
        *,
        query_timeout_seconds: int,
        login_timeout_seconds: int,
    ) -> FakeOnecEngine:
        engine_calls.append((database_url, query_timeout_seconds, login_timeout_seconds))
        return onec_engine

    def fake_build_report(
        current_session: object,
        current_onec_engine: object,
        *,
        articles: set[str],
        site_values: dict[str, list[str]],
        only_mismatches: bool,
        limit: int,
    ) -> list[report_product_compatibility_sync.CompatibilitySyncRow]:
        report_calls.append(
            (
                current_session,
                current_onec_engine,
                articles,
                site_values,
                only_mismatches,
                limit,
            )
        )
        return []

    site_path = tmp_path / "site.json"
    site_path.write_text(
        json.dumps(
            {
                "row": {
                    "article": "041567",
                    "compatible_models": ["Huawei E5573"],
                }
            }
        ),
        encoding="utf-8",
    )
    output_path = tmp_path / "compatibility.json"
    monkeypatch.setenv("DATABASE_URL", "sqlite:///app-snapshot.db")
    monkeypatch.setenv("ONEC_DATABASE_URL", "mssql+pyodbc://onec-snapshot")
    monkeypatch.setattr(
        report_product_compatibility_sync,
        "get_settings",
        lambda: SimpleNamespace(
            database_url="postgresql://settings-app",
            onec_database_url="mssql+pyodbc://settings-onec",
            onec_query_timeout_seconds=55,
            onec_login_timeout_seconds=9,
        ),
    )
    monkeypatch.setattr(
        report_product_compatibility_sync,
        "session_scope",
        fake_session_scope,
    )
    monkeypatch.setattr(
        report_product_compatibility_sync,
        "build_onec_engine",
        fake_build_onec_engine,
    )
    monkeypatch.setattr(
        report_product_compatibility_sync,
        "build_report",
        fake_build_report,
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "report_product_compatibility_sync",
            "--article",
            "041567",
            "--only-mismatches",
            "--limit",
            "7",
            "--format",
            "json",
            "--output",
            str(output_path),
            "--site-json",
            str(site_path),
        ],
    )

    report_product_compatibility_sync.main()

    assert scope_calls == [(True, "sqlite:///app-snapshot.db")]
    assert engine_calls == [("mssql+pyodbc://onec-snapshot", 55, 9)]
    assert report_calls == [
        (
            session,
            onec_engine,
            {"041567"},
            {"041567": ["Huawei E5573"]},
            True,
            7,
        )
    ]
    assert onec_engine.disposed is True
    assert json.loads(output_path.read_text(encoding="utf-8")) == []
