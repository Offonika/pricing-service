from __future__ import annotations

import base64
import json

from app.core.config import Settings
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
        "expected_user_names_incomplete",
        "bitrix_field_map_incomplete",
        "bitrix_enum_map_incomplete",
        "bitrix_new_stage_missing",
        "bitrix_success_stage_missing",
        "bitrix_failure_stage_missing",
    ]
    assert _cli_exit_code(checked) == 2
    assert _cli_exit_code({"mode": "check", "ready": True}) == 0
