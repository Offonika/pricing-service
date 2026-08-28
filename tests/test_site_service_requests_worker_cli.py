from __future__ import annotations

import base64
import json
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models import Base
from app.models.site_service_requests import SiteServiceRequestWorkerState
from app.services.expertise_bitrix import BitrixRestError
from tasks import site_service_requests_worker as worker_cli
from tasks.site_service_requests_worker import _cli_exit_code, main, parse_args

_ENCRYPTION_KEY = base64.urlsafe_b64encode(b"c" * 32).decode("ascii")


def _settings(**overrides) -> Settings:
    values = {
        "site_service_requests_bitrix_webhook_url": "https://example.invalid/rest/1/token",
        "site_service_requests_event_encryption_key": _ENCRYPTION_KEY,
        "site_service_requests_first_line_user_ids": [1001, 1002],
    }
    values.update(overrides)
    return Settings(**values)


def test_cli_defaults_to_dry_run_and_check_never_requires_writes() -> None:
    args = parse_args([])
    assert args.apply is False
    assert args.check is False

    result = main(["--check", "--compact"], settings_override=_settings())
    assert result["mode"] == "check"
    assert result["ready"] is True
    assert result["bitrixWritesEnabled"] is False


def test_cli_apply_check_requires_flags_and_mapping(capsys) -> None:
    result = main(["--check", "--compact"], settings_override=Settings())
    rendered = json.loads(capsys.readouterr().out)
    assert rendered == result
    assert result["ready"] is False
    assert result["errors"] == [
        "bitrix_webhook_missing",
        "encryption_key_missing",
        "first_line_users_missing",
    ]

    apply_settings = _settings(site_service_requests_bitrix_writes_enabled=True)
    checked = main(["--check", "--compact"], settings_override=apply_settings)
    assert checked["ready"] is False
    assert checked["errors"] == [
        "escalation_user_missing",
        "finance_user_missing",
        "bitrix_root_folder_missing",
        "expected_user_names_incomplete",
        "bitrix_field_map_incomplete",
        "bitrix_enum_map_incomplete",
        "bitrix_new_stage_missing",
        "bitrix_success_stage_missing",
        "bitrix_failure_stage_missing",
    ]
    assert _cli_exit_code(checked) == 2
    assert _cli_exit_code({"mode": "check", "ready": True}) == 0


def test_cli_apply_check_requires_daily_report_dialog_when_enabled() -> None:
    settings = _settings(
        site_service_requests_bitrix_writes_enabled=True,
        site_service_requests_daily_report_enabled=True,
    )

    checked = main(["--check", "--compact"], settings_override=settings)

    assert "daily_report_dialog_missing" in checked["errors"]


def test_apply_worker_heartbeat_records_failure_and_recovers(monkeypatch, tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'worker-heartbeat.db'}")
    Base.metadata.create_all(engine)

    @contextmanager
    def scope(*, read_only: bool = False):
        with Session(engine) as session:
            try:
                yield session
                if read_only:
                    session.rollback()
                else:
                    session.commit()
            except BaseException:
                session.rollback()
                raise

    def fail_tick(**_kwargs):
        raise BitrixRestError("PRIVATE ACCESS DETAIL", code="bitrix_access_denied")

    monkeypatch.setattr(worker_cli, "_run_worker", fail_tick)
    with pytest.raises(BitrixRestError):
        main(
            ["--apply", "--compact"],
            settings_override=_settings(),
            session_scope_factory=scope,
        )

    with Session(engine) as session:
        failed = session.scalar(select(SiteServiceRequestWorkerState))
        assert failed is not None
        assert failed.last_started_at is not None
        assert failed.last_failure_at is not None
        assert failed.last_success_at is None
        assert failed.last_error_code == "bitrix_access_denied"
        assert failed.consecutive_failures == 1
        assert "PRIVATE" not in str(failed.last_error_code)

    monkeypatch.setattr(
        worker_cli,
        "_run_worker",
        lambda **_kwargs: {"mode": "apply", "count": 0},
    )
    main(
        ["--apply", "--compact"],
        settings_override=_settings(),
        session_scope_factory=scope,
    )

    with Session(engine) as session:
        recovered = session.scalar(select(SiteServiceRequestWorkerState))
        assert recovered is not None
        assert recovered.last_success_at is not None
        assert recovered.last_error_code is None
        assert recovered.consecutive_failures == 0

    engine.dispose()
