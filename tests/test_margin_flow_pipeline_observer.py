from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.services.margin_flow_pipeline_observer import (
    PIPELINE_LOTS_SQL,
    file_sha256,
    load_observer_config,
    load_scope_codes,
    stable_lot_identity,
    validate_observer_bundle,
    write_observation,
)


def _scope_csv(path: Path) -> Path:
    path.write_text("nomenclature_code\nSKU-1\nSKU-2\n", encoding="utf-8")
    return path


def _config(path: Path, scope_path: Path, *, onec_writes: bool = False) -> Path:
    payload = {
        "schema": "margin_flow_pipeline_forward_observer.v1",
        "observer_id": "observer-test-v1",
        "timezone": "Europe/Moscow",
        "minimum_matured_consecutive_days": 105,
        "scope": {
            "source_bundle": "test-bundle",
            "expected_sha256": file_sha256(scope_path),
            "expected_code_count": 2,
        },
        "safety": {
            "onec_read_only_required": True,
            "application_database_writes": False,
            "onec_writes": onec_writes,
            "bitrix_writes": False,
            "telegram_writes": False,
            "external_api_calls": False,
            "recommended_order_qty_calculation": False,
            "recommended_order_qty_writes": False,
            "order_creation": False,
            "status_changes": False,
            "release_changes": False,
            "production_cron_changes": False,
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _raw_lots(quantity: str = "4") -> list[dict[str, object]]:
    return [
        {
            "nomenclature_code": "SKU-1",
            "order_ref_hex": "0xAA01",
            "product_ref_hex": "0xBB01",
            "quantity": Decimal(quantity),
            "register_row_count": 1,
            "order_revision_fingerprint": "0x0102",
            "order_created_at_raw": datetime(2026, 8, 1, 10, 0),
            "expected_receipt_at_raw": datetime(2026, 8, 25, 10, 0),
            "cargo_handoff_at_raw": datetime(2026, 8, 15, 10, 0),
            "marked_raw": "0x00",
            "posted_raw": "0x01",
        }
    ]


def _permissions() -> dict[str, object]:
    return {
        "status": "pass_effective_object_permissions_read_only",
        "objects": [],
    }


def test_config_refuses_any_source_write_capability(tmp_path: Path) -> None:
    scope = _scope_csv(tmp_path / "scope.csv")
    config_path = _config(tmp_path / "config.json", scope, onec_writes=True)

    with pytest.raises(ValueError, match="side_effects_not_disabled"):
        load_observer_config(config_path)


def test_scope_is_hash_bound_and_lot_identity_is_stable(tmp_path: Path) -> None:
    scope = _scope_csv(tmp_path / "scope.csv")
    config = load_observer_config(_config(tmp_path / "config.json", scope))

    assert load_scope_codes(scope, config) == ["SKU-1", "SKU-2"]
    assert stable_lot_identity(order_ref_hex="0xaa01", product_ref_hex="0xbb01") == (
        stable_lot_identity(order_ref_hex="0xAA01", product_ref_hex="0xBB01")
    )
    scope.write_text("nomenclature_code\nSKU-1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="scope_checksum_mismatch"):
        load_scope_codes(scope, config)


def test_observation_is_append_only_hash_chained_and_pipeline_credit_stays_zero(
    tmp_path: Path,
) -> None:
    scope = _scope_csv(tmp_path / "scope.csv")
    config = load_observer_config(_config(tmp_path / "config.json", scope))
    codes = load_scope_codes(scope, config)
    output_root = tmp_path / "observer"
    common = {
        "output_root": output_root,
        "config": config,
        "scope_path": scope,
        "scope_codes": codes,
        "raw_lots": _raw_lots(),
        "permission_evidence": _permissions(),
        "command": ["python", "-m", "tasks.observe_margin_flow_pipeline", "capture"],
    }

    first = write_observation(
        observation_slot="2026-08-17",
        source_read_started_at="2026-08-17T07:00:00Z",
        source_read_completed_at="2026-08-17T07:00:01Z",
        **common,
    )
    reused = write_observation(
        observation_slot="2026-08-17",
        source_read_started_at="2026-08-17T07:00:00Z",
        source_read_completed_at="2026-08-17T07:00:01Z",
        **common,
    )
    second = write_observation(
        observation_slot="2026-08-18",
        source_read_started_at="2026-08-18T07:00:00Z",
        source_read_completed_at="2026-08-18T07:00:01Z",
        **common,
    )

    assert first.reused is False
    assert reused.reused is True
    assert second.reused is False
    lot = json.loads(
        (first.observation_dir / "lot-observations.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    assert lot["available_at"] == "2026-08-17T07:00:01Z"
    assert lot["order_revision_at"] is None
    assert lot["order_revision_at_status"] == "unavailable_in_source"
    assert lot["reliability_status"] == "unproven"
    assert lot["reliable_quantity"] == "0"
    assert lot["reliability_evidence"]["raw_cargo_handoff_at_present"] is True
    assert validate_observer_bundle(output_root) == {
        "schema": "margin_flow_pipeline_observer_validation.v1",
        "observer_id": "observer-test-v1",
        "status": "collecting",
        "observation_count": 2,
        "first_observation_slot": "2026-08-17",
        "latest_observation_slot": "2026-08-18",
        "longest_consecutive_day_count": 2,
        "minimum_matured_consecutive_days": 105,
        "remaining_consecutive_days": 103,
        "total_lot_observation_count": 2,
        "latest_manifest_sha256": second.manifest_sha256,
        "chain_valid": True,
        "production_rollout": "NO_GO",
    }


def test_validation_detects_out_of_band_observation_change(tmp_path: Path) -> None:
    scope = _scope_csv(tmp_path / "scope.csv")
    config = load_observer_config(_config(tmp_path / "config.json", scope))
    result = write_observation(
        output_root=tmp_path / "observer",
        observation_slot="2026-08-17",
        config=config,
        scope_path=scope,
        scope_codes=load_scope_codes(scope, config),
        raw_lots=_raw_lots(),
        permission_evidence=_permissions(),
        source_read_started_at="2026-08-17T07:00:00Z",
        source_read_completed_at="2026-08-17T07:00:01Z",
        command=["capture"],
    )
    lots_path = result.observation_dir / "lot-observations.jsonl"
    lots_path.chmod(0o640)
    lots_path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="file_checksum_mismatch"):
        validate_observer_bundle(tmp_path / "observer")


def test_source_query_is_select_only_and_does_not_use_dirty_reads() -> None:
    normalized = f" {PIPELINE_LOTS_SQL.upper()} "

    assert normalized.lstrip().startswith("SELECT")
    assert " NOLOCK " not in normalized
    for keyword in (" INSERT ", " UPDATE ", " DELETE ", " MERGE ", " EXEC "):
        assert keyword not in normalized
