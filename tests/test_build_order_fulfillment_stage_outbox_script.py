from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from scripts import build_order_fulfillment_stage_outbox


def test_order_fulfillment_stage_outbox_uses_role_specific_read_only_db_access(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    session = object()
    scope_calls: list[bool] = []
    engine_calls: list[tuple[str, int, int]] = []
    env_calls: list[list[Path]] = []
    review_calls: list[tuple[object, int, str, object, object, object]] = []
    outbox_calls: list[tuple[list[object], set[str], set[str]]] = []
    write_calls: list[tuple[Path, list[object]]] = []

    class FakeOnecEngine:
        disposed = False

        def dispose(self) -> None:
            self.disposed = True

    class FakeBitrixClient:
        def __init__(self, webhook_url: str) -> None:
            self.webhook_url = webhook_url
            self.stage_reads = 0

        def list_deal_stage_ids(self) -> set[str]:
            self.stage_reads += 1
            return {"C1:PREPARATION", "C1:DELIVERY"}

    onec_engine = FakeOnecEngine()
    settings = SimpleNamespace(
        onec_database_url="mssql+pyodbc://onec-snapshot",
        onec_query_timeout_seconds=60,
        onec_login_timeout_seconds=8,
    )
    review_rows = [SimpleNamespace(action="update_stage")]
    outbox_rows = [
        SimpleNamespace(state="ready"),
        SimpleNamespace(state="blocked_missing_stage"),
        SimpleNamespace(state="ready"),
    ]

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

    def fake_load_env_files(paths: list[Path]) -> dict[str, str]:
        env_calls.append(paths)
        return {"BITRIX_BOX_WEBHOOK_BASE": "https://bitrix.invalid/rest"}

    def fake_build_review_rows(
        current_session: object,
        *,
        limit: int,
        status: str,
        bitrix_client: object,
        onec_engine: object,
        settings: object,
    ) -> list[object]:
        review_calls.append(
            (
                current_session,
                limit,
                status,
                bitrix_client,
                onec_engine,
                settings,
            )
        )
        return review_rows

    def fake_build_stage_outbox_rows(
        current_review_rows: list[object],
        *,
        available_stage_ids: set[str],
        allowed_target_stages: set[str],
    ) -> list[object]:
        outbox_calls.append((current_review_rows, available_stage_ids, allowed_target_stages))
        return outbox_rows

    def fake_write_stage_outbox_csv(path: Path, current_rows: list[object]) -> Path:
        write_calls.append((path, current_rows))
        return path

    extra_env_path = tmp_path / "stage-outbox.env"
    monkeypatch.setattr(build_order_fulfillment_stage_outbox, "get_settings", lambda: settings)
    monkeypatch.setattr(
        build_order_fulfillment_stage_outbox,
        "load_env_files",
        fake_load_env_files,
    )
    monkeypatch.setattr(
        build_order_fulfillment_stage_outbox,
        "resolve_bitrix_webhook_url",
        lambda env_values: env_values["BITRIX_BOX_WEBHOOK_BASE"],
    )
    monkeypatch.setattr(
        build_order_fulfillment_stage_outbox,
        "session_scope",
        fake_session_scope,
    )
    monkeypatch.setattr(
        build_order_fulfillment_stage_outbox,
        "build_onec_engine",
        fake_build_onec_engine,
    )
    monkeypatch.setattr(
        build_order_fulfillment_stage_outbox.fulfillment,
        "BitrixChatClient",
        FakeBitrixClient,
    )
    monkeypatch.setattr(
        build_order_fulfillment_stage_outbox.fulfillment,
        "build_review_rows",
        fake_build_review_rows,
    )
    monkeypatch.setattr(
        build_order_fulfillment_stage_outbox.fulfillment,
        "build_stage_outbox_rows",
        fake_build_stage_outbox_rows,
    )
    monkeypatch.setattr(
        build_order_fulfillment_stage_outbox.fulfillment,
        "write_stage_outbox_csv",
        fake_write_stage_outbox_csv,
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "build_order_fulfillment_stage_outbox",
            "--limit",
            "12",
            "--status",
            "delivery_ready",
            "--target-stage",
            "C1:PREPARATION",
            "--target-stage",
            "C1:DELIVERY",
            "--output-dir",
            str(tmp_path),
            "--env-file",
            str(extra_env_path),
        ],
    )

    assert build_order_fulfillment_stage_outbox.main() == 0

    assert scope_calls == [True]
    assert engine_calls == [("mssql+pyodbc://onec-snapshot", 60, 8)]
    assert env_calls == [
        [
            *build_order_fulfillment_stage_outbox.DEFAULT_ENV_FILES,
            extra_env_path,
        ]
    ]
    assert len(review_calls) == 1
    current_session, limit, status, bitrix_client, current_onec_engine, current_settings = (
        review_calls[0]
    )
    assert current_session is session
    assert limit == 12
    assert status == "delivery_ready"
    assert isinstance(bitrix_client, FakeBitrixClient)
    assert bitrix_client.webhook_url == "https://bitrix.invalid/rest"
    assert bitrix_client.stage_reads == 1
    assert current_onec_engine is onec_engine
    assert current_settings is settings
    assert outbox_calls == [
        (
            review_rows,
            {"C1:PREPARATION", "C1:DELIVERY"},
            {"C1:PREPARATION", "C1:DELIVERY"},
        )
    ]
    assert onec_engine.disposed is True
    assert len(write_calls) == 1
    output_path, output_rows = write_calls[0]
    assert output_path.parent == tmp_path
    assert output_path.name.startswith("stage-outbox-")
    assert output_path.suffix == ".csv"
    assert output_rows == outbox_rows
    assert "rows=3 states={'ready': 2, 'blocked_missing_stage': 1}" in capsys.readouterr().out
