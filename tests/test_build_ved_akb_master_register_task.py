from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from tasks import build_ved_akb_master_register as task


def test_main_uses_central_read_only_scope_and_role_specific_onec_engine(
    monkeypatch,
    tmp_path: Path,
) -> None:
    session = object()
    scope_calls: list[bool] = []
    engine_calls: list[tuple[str, int, int]] = []
    saved_paths: list[Path] = []

    class FakeOnecEngine:
        disposed = False

        def dispose(self) -> None:
            self.disposed = True

    class FakeWorkbook:
        def save(self, path: Path) -> None:
            saved_paths.append(path)

    onec_engine = FakeOnecEngine()
    output_path = tmp_path / "ved-akb-register.xlsx"

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

    monkeypatch.setenv("DATABASE_URL", "postgresql://application-snapshot")
    monkeypatch.setenv("ONEC_DATABASE_URL", "mssql+pyodbc://onec-snapshot")
    monkeypatch.setattr(task, "_load_env_file", lambda _path: None)
    monkeypatch.setattr(
        task,
        "get_settings",
        lambda: SimpleNamespace(
            onec_query_timeout_seconds=45,
            onec_login_timeout_seconds=7,
        ),
    )
    monkeypatch.setattr(task, "session_scope", fake_session_scope)
    monkeypatch.setattr(task, "build_onec_engine", fake_build_onec_engine)
    monkeypatch.setattr(
        task,
        "parse_args",
        lambda: SimpleNamespace(
            order_number="РБГУ0000377",
            current_xlsx=tmp_path / "current.xlsx",
            old_sku_csv=tmp_path / "old-sku.csv",
            old_tech_csv=tmp_path / "old-tech.csv",
            old_normalization_csv=tmp_path / "old-normalization.csv",
            output=output_path,
        ),
    )
    monkeypatch.setattr(task, "_load_current_ds_rows", lambda _path: {})
    monkeypatch.setattr(task, "_load_csv_by_code", lambda _path: {})
    monkeypatch.setattr(
        task,
        "_fetch_onec_order_lines",
        lambda engine, order_number: (
            [] if engine is onec_engine and order_number == "РБГУ0000377" else None
        ),
    )

    def fake_load_products_by_code(
        current_session: object,
        *,
        codes,
        articles,
        skus,
    ) -> dict:
        assert current_session is session
        assert list(codes) == []
        assert list(articles) == []
        assert list(skus) == []
        return {}

    monkeypatch.setattr(task, "_load_products_by_code", fake_load_products_by_code)
    monkeypatch.setattr(task, "_build_master_rows", lambda **_kwargs: [])
    monkeypatch.setattr(task, "_build_workbook", lambda _rows: FakeWorkbook())

    task.main()

    assert scope_calls == [True]
    assert engine_calls == [("mssql+pyodbc://onec-snapshot", 45, 7)]
    assert onec_engine.disposed is True
    assert saved_paths == [output_path]
