from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import analyze_pickup_contract_settlements


def test_onec_connection_uses_role_specific_factory_and_disposes(
    monkeypatch,
) -> None:
    connection = object()
    engine_calls: list[tuple[str, int, int]] = []

    class FakeEngine:
        disposed = False

        @contextmanager
        def connect(self):
            yield connection

        def dispose(self) -> None:
            self.disposed = True

    engine = FakeEngine()
    settings = SimpleNamespace(
        onec_database_url="mssql+pyodbc://onec-snapshot",
        onec_query_timeout_seconds=60,
        onec_login_timeout_seconds=8,
    )

    def fake_build_onec_engine(
        database_url: str,
        *,
        query_timeout_seconds: int,
        login_timeout_seconds: int,
    ) -> FakeEngine:
        engine_calls.append((database_url, query_timeout_seconds, login_timeout_seconds))
        return engine

    monkeypatch.setattr(
        analyze_pickup_contract_settlements,
        "get_settings",
        lambda: settings,
    )
    monkeypatch.setattr(
        analyze_pickup_contract_settlements,
        "build_onec_engine",
        fake_build_onec_engine,
    )

    with pytest.raises(RuntimeError, match="query failed"):
        with analyze_pickup_contract_settlements._onec_connection() as current_connection:
            assert current_connection is connection
            raise RuntimeError("query failed")

    assert engine_calls == [("mssql+pyodbc://onec-snapshot", 60, 8)]
    assert engine.disposed is True


def test_pickup_contract_settlements_main_preserves_cli_and_artifact_contract(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    input_csv = tmp_path / "pickup.csv"
    output_dir = tmp_path / "reports"
    candidates = [{"заказ": " 241500 "}, {"заказ": ""}]
    onec_rows = {"241500": {"site_order_number": "241500"}}
    payments = [object()]
    report_rows = [{"заказ": "241500"}]
    load_calls: list[tuple[Path, str]] = []
    balance_calls: list[list[str]] = []
    payment_calls: list[object] = []
    report_calls: list[tuple[object, object, object]] = []
    csv_calls: list[tuple[Path, object]] = []
    markdown_calls: list[tuple[Path, object, Path, datetime]] = []

    def fake_load_candidates(path: Path, reconcile_group: str):
        load_calls.append((path, reconcile_group))
        return candidates

    def fake_fetch_contract_balances(order_numbers: list[str]):
        balance_calls.append(order_numbers)
        return onec_rows

    def fake_find_nearby_payments(current_rows: object):
        payment_calls.append(current_rows)
        return payments

    def fake_build_report_rows(
        current_candidates: object,
        current_onec_rows: object,
        current_payments: object,
    ):
        report_calls.append((current_candidates, current_onec_rows, current_payments))
        return report_rows

    def fake_write_csv(path: Path, rows: object) -> None:
        csv_calls.append((path, rows))

    def fake_write_markdown(
        path: Path,
        *,
        rows: object,
        input_csv: Path,
        as_of: datetime,
    ) -> None:
        markdown_calls.append((path, rows, input_csv, as_of))

    monkeypatch.setattr(
        analyze_pickup_contract_settlements,
        "_load_candidates",
        fake_load_candidates,
    )
    monkeypatch.setattr(
        analyze_pickup_contract_settlements,
        "_fetch_contract_balances",
        fake_fetch_contract_balances,
    )
    monkeypatch.setattr(
        analyze_pickup_contract_settlements,
        "_find_nearby_payments",
        fake_find_nearby_payments,
    )
    monkeypatch.setattr(
        analyze_pickup_contract_settlements,
        "_build_report_rows",
        fake_build_report_rows,
    )
    monkeypatch.setattr(
        analyze_pickup_contract_settlements,
        "_write_csv",
        fake_write_csv,
    )
    monkeypatch.setattr(
        analyze_pickup_contract_settlements,
        "_write_markdown",
        fake_write_markdown,
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "analyze_pickup_contract_settlements",
            str(input_csv),
            "--output-dir",
            str(output_dir),
            "--reconcile-group",
            "selected-group",
            "--as-of",
            "2026-08-28T19:30:00",
        ],
    )

    assert analyze_pickup_contract_settlements.main() is None

    expected_as_of = datetime(2026, 8, 28, 19, 30)
    expected_csv = output_dir / "pickup-contract-settlement-check-20260828-193000.csv"
    expected_markdown = output_dir / "pickup-contract-settlement-check-20260828-193000.md"
    assert output_dir.is_dir()
    assert load_calls == [(input_csv, "selected-group")]
    assert balance_calls == [["241500"]]
    assert payment_calls == [onec_rows]
    assert report_calls == [(candidates, onec_rows, payments)]
    assert csv_calls == [(expected_csv, report_rows)]
    assert markdown_calls == [(expected_markdown, report_rows, input_csv, expected_as_of)]
    stdout = capsys.readouterr().out
    assert "Rows: 1" in stdout
    assert f"CSV: {expected_csv}" in stdout
    assert f"Markdown: {expected_markdown}" in stdout
