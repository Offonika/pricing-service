from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.infrastructure import contracts
from app.infrastructure.contract_policies import CONTRACT_POLICIES, ContractPolicy
from app.infrastructure.contracts import (
    ContractIntegrityError,
    ContractStaleError,
    read_json_contract,
)


def test_contract_reader_verifies_present_manifest(tmp_path: Path) -> None:
    path = tmp_path / "snapshot.json"
    content = b'{"source_status":"ready"}\n'
    path.write_bytes(content)
    path.with_suffix(".json.manifest.json").write_text(
        json.dumps(
            {
                "content_sha256": hashlib.sha256(content).hexdigest(),
                "artifact": "snapshot.json",
            }
        ),
        encoding="utf-8",
    )
    assert read_json_contract(path)["source_status"] == "ready"

    path.write_text('{"source_status":"changed"}\n', encoding="utf-8")
    with pytest.raises(ContractIntegrityError):
        read_json_contract(path)


def _write_policy_contract(root: Path, *, generated_at: datetime) -> tuple[Path, ContractPolicy]:
    path = root / "domain/snapshot.json"
    path.parent.mkdir(parents=True)
    content = b'{"source_status":"ready"}\n'
    path.write_bytes(content)
    policy = ContractPolicy(
        contract_version="snapshot.v1",
        source_project="producer",
        schema="snapshot.schema.json",
        schema_sha256="a" * 64,
        max_age=timedelta(hours=24),
    )
    path.with_suffix(".json.manifest.json").write_text(
        json.dumps(
            {
                "contract_version": "snapshot.v1",
                "generated_at": generated_at.isoformat(),
                "source_project": "producer",
                "content_sha256": hashlib.sha256(content).hexdigest(),
                "schema": "snapshot.schema.json",
                "schema_sha256": "a" * 64,
                "artifact": "snapshot.json",
            }
        ),
        encoding="utf-8",
    )
    return path, policy


def test_contract_reader_enforces_allowlisted_metadata_and_freshness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 7, 16, 12, tzinfo=UTC)
    path, policy = _write_policy_contract(tmp_path, generated_at=now - timedelta(hours=1))
    monkeypatch.setattr(contracts, "CONTRACT_ROOT", tmp_path)
    monkeypatch.setattr(contracts, "CONTRACT_POLICIES", {"domain/snapshot.json": policy})

    assert read_json_contract(path, now=now)["source_status"] == "ready"

    manifest_path = path.with_suffix(".json.manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source_project"] = "unexpected-producer"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ContractIntegrityError, match="source_project"):
        read_json_contract(path, now=now)


def test_contract_reader_rejects_stale_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 7, 16, 12, tzinfo=UTC)
    path, policy = _write_policy_contract(tmp_path, generated_at=now - timedelta(days=2))
    monkeypatch.setattr(contracts, "CONTRACT_ROOT", tmp_path)
    monkeypatch.setattr(contracts, "CONTRACT_POLICIES", {"domain/snapshot.json": policy})
    with pytest.raises(ContractStaleError):
        read_json_contract(path, now=now)


def test_schema_hash_has_one_release_compatibility_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = datetime(2026, 7, 16, 12, tzinfo=UTC)
    path, policy = _write_policy_contract(tmp_path, generated_at=now)
    manifest_path = path.with_suffix(".json.manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["schema_sha256"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(contracts, "CONTRACT_ROOT", tmp_path)
    monkeypatch.setattr(contracts, "CONTRACT_POLICIES", {"domain/snapshot.json": policy})

    assert read_json_contract(path, now=now)["source_status"] == "ready"
    assert "compatibility window" in caplog.text
    with pytest.raises(ContractIntegrityError, match="schema_sha256 is missing"):
        read_json_contract(path, now=now, require_schema_sha256=True)


def test_monthly_bp_tax_contract_path_is_allowlisted() -> None:
    policy = CONTRACT_POLICIES.get(
        "executive-dashboard/bp-tax-accruals/2026-06/bp-tax-accruals-2026-06.json"
    )

    assert policy is not None
    assert policy.contract_version == "executive-bp-tax-accrual-snapshot.v1"
    assert (
        CONTRACT_POLICIES.get(
            "executive-dashboard/bp-tax-accruals/2026-13/bp-tax-accruals-2026-13.json"
        )
        is None
    )


def test_monthly_retail_director_contract_path_is_allowlisted() -> None:
    policy = CONTRACT_POLICIES.get(
        "retail-director-monthly/2026-06/retail-director-summary-2026-06.json"
    )

    assert policy is not None
    assert policy.contract_version == "retail-director-monthly-snapshot.v2"
    assert (
        CONTRACT_POLICIES.get(
            "retail-director-monthly/2026-06/retail-director-summary-2026-05.json"
        )
        is None
    )
