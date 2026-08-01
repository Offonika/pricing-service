from __future__ import annotations

import json

import pytest

from scripts import ensure_receivable_credit_decision_process as process


def test_default_command_only_prints_blueprint(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert process.main([]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["applied"] is False
    assert payload["process"]["title"] == "Кредитное решение"
    assert payload["process"]["code"] == "receivable_decision"
    assert [item["logical_key"] for item in payload["stages"]] == [
        "draft",
        "review",
        "approved",
        "onec_check",
        "applying",
        "applied",
        "rejected",
        "onec_error",
    ]
    assert payload["safety"]["existing_receivable_entity_type_id_1132_untouched"]


def test_apply_requires_explicit_webhook() -> None:
    with pytest.raises(SystemExit):
        process.parse_args(["--apply"])


def test_blueprint_uses_own_mapping_and_details_paths() -> None:
    args = process.parse_args([])
    assert args.mapping_path.name == "receivable_credit_decision_mapping.json"
    assert args.details_config_path.name == "receivable_credit_decision_details_configuration.json"
    assert "expertise" not in str(args.details_config_path)


def test_blueprint_contains_atomic_pair_and_readback_fields() -> None:
    fields = {item["logical_key"] for item in process.blueprint()["fields"]}
    assert {
        "current_limit",
        "current_depth",
        "proposed_limit",
        "proposed_depth",
        "decision_hash",
        "readback_limit",
        "readback_depth",
        "readback_control_enabled",
        "contract_ref",
        "contract_guid",
        "contract_code",
        "contract_name",
        "contract_organization_ref",
        "contract_organization_guid",
        "current_control_enabled",
        "proposed_control_enabled",
        "connector_error",
    } <= fields


def test_blueprint_requires_technical_fields_reset_before_worker_enable() -> None:
    blueprint = process.blueprint()
    assert blueprint["automation"]["reset_on_return_to_stages"] == ["draft", "review"]
    assert set(blueprint["automation"]["clear_logical_fields"]) == {
        "decision_hash",
        "approved_by",
        "approved_at",
        "readback_limit",
        "readback_depth",
        "readback_control_enabled",
        "connector_state",
        "connector_error",
    }
    assert blueprint["safety"]["worker_stays_disabled_until_reset_rule_is_verified"]


def test_live_metadata_apply_cannot_claim_reset_robot_is_ready(monkeypatch) -> None:
    monkeypatch.setattr(process, "_configure_generic_setup", lambda: None)
    monkeypatch.setattr(
        process.bitrix_setup,
        "bitrix_call",
        lambda *_args, **_kwargs: {"result": {"ID": "115204"}},
    )
    monkeypatch.setattr(
        process.bitrix_setup,
        "ensure_type",
        lambda *_args, **_kwargs: {"entityTypeId": 1200},
    )
    monkeypatch.setattr(
        process.bitrix_setup,
        "ensure_category",
        lambda *_args, **_kwargs: {"id": 44},
    )
    monkeypatch.setattr(process.bitrix_setup, "ensure_stages", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        process.bitrix_setup,
        "ensure_custom_fields",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        process.bitrix_setup,
        "discover_field_mapping",
        lambda *_args, **_kwargs: ({}, []),
    )
    monkeypatch.setattr(
        process.bitrix_setup,
        "build_mapping_payload",
        lambda **_kwargs: {"process": {"entity_type_id": 1200}},
    )
    monkeypatch.setattr(process.bitrix_setup, "save_mapping", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        process.bitrix_setup,
        "ensure_common_details_configuration",
        lambda *_args, **_kwargs: ([], process.DEFAULT_DETAILS_CONFIG_PATH),
    )

    payload = process.apply_blueprint(
        process.parse_args(["--apply", "--webhook-url", "https://example.invalid/rest/"])
    )

    assert payload["reset_automation_configured"] is False
    assert payload["worker_enable_blocked"] is True
