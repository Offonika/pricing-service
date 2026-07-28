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
    assert payload["process"]["title"] == "Дебиторка Решение"
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
        "connector_error",
    } <= fields
