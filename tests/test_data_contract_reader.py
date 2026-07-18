from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.infrastructure import contracts
from app.infrastructure.contract_policies import ContractPolicy
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


def test_retail_director_monthly_contract_path_family_is_allowlisted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 7, 17, 6, tzinfo=UTC)
    path = tmp_path / "retail-director-monthly/2026-06/retail-director-summary-2026-06.json"
    path.parent.mkdir(parents=True)
    content = b'{"schema_version":2,"shrinkage":{}}\n'
    path.write_bytes(content)
    path.with_suffix(".json.manifest.json").write_text(
        json.dumps(
            {
                "contract_version": "retail-director-monthly-snapshot.v2",
                "generated_at": now.isoformat(),
                "source_project": "mm-compensation",
                "content_sha256": hashlib.sha256(content).hexdigest(),
                "schema": "retail-director-monthly-snapshot.schema.json",
                "schema_sha256": (
                    "7306d10df7a489b985216ea856f1bbd7518a421bfd5deaeadd552e637cd70154"
                ),
                "artifact": path.name,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(contracts, "CONTRACT_ROOT", tmp_path)

    assert read_json_contract(path, now=now)["schema_version"] == 2


def test_retail_director_monthly_contract_rejects_mismatched_month_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 7, 17, 6, tzinfo=UTC)
    path = tmp_path / "retail-director-monthly/2026-06/retail-director-summary-2026-05.json"
    path.parent.mkdir(parents=True)
    content = b'{"schema_version":2,"shrinkage":{}}\n'
    path.write_bytes(content)
    path.with_suffix(".json.manifest.json").write_text(
        json.dumps(
            {
                "contract_version": "retail-director-monthly-snapshot.v2",
                "generated_at": now.isoformat(),
                "source_project": "mm-compensation",
                "content_sha256": hashlib.sha256(content).hexdigest(),
                "schema": "retail-director-monthly-snapshot.schema.json",
                "schema_sha256": (
                    "7306d10df7a489b985216ea856f1bbd7518a421bfd5deaeadd552e637cd70154"
                ),
                "artifact": path.name,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(contracts, "CONTRACT_ROOT", tmp_path)

    with pytest.raises(ContractIntegrityError, match="not allowlisted"):
        read_json_contract(path, now=now)


def _write_bp_tax_accrual_policy_contract(
    root: Path,
    *,
    directory_month: str,
    filename_month: str,
    generated_at: datetime,
) -> Path:
    path = (
        root / f"executive-dashboard/bp-tax-accruals/{directory_month}/"
        f"bp-tax-accruals-{filename_month}.json"
    )
    path.parent.mkdir(parents=True)
    content = b'{"schema_version":1,"source_status":"partial"}\n'
    path.write_bytes(content)
    path.with_suffix(".json.manifest.json").write_text(
        json.dumps(
            {
                "contract_version": "executive-bp-tax-accrual-snapshot.v1",
                "generated_at": generated_at.isoformat(),
                "source_project": "mm-compensation",
                "content_sha256": hashlib.sha256(content).hexdigest(),
                "schema": "executive-bp-tax-accrual-snapshot.schema.json",
                "schema_sha256": (
                    "12e2bb409c7aa468da086b2bf3a884633425cafae54b162f7734a22efe3188bf"
                ),
                "artifact": path.name,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_bp_tax_accrual_monthly_contract_path_family_is_allowlisted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 7, 18, 8, tzinfo=UTC)
    path = _write_bp_tax_accrual_policy_contract(
        tmp_path,
        directory_month="2026-06",
        filename_month="2026-06",
        generated_at=now,
    )
    monkeypatch.setattr(contracts, "CONTRACT_ROOT", tmp_path)

    assert read_json_contract(path, now=now)["schema_version"] == 1


def test_bp_tax_accrual_monthly_contract_rejects_mismatched_month_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 7, 18, 8, tzinfo=UTC)
    path = _write_bp_tax_accrual_policy_contract(
        tmp_path,
        directory_month="2026-06",
        filename_month="2026-05",
        generated_at=now,
    )
    monkeypatch.setattr(contracts, "CONTRACT_ROOT", tmp_path)

    with pytest.raises(ContractIntegrityError, match="not allowlisted"):
        read_json_contract(path, now=now)
