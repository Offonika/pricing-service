"""Verified readers for neutral cross-project JSON contracts."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.infrastructure.contract_policies import CONTRACT_POLICIES, ContractPolicy

CONTRACT_ROOT = Path("/var/lib/mm-data-contracts")
logger = logging.getLogger(__name__)


class ContractIntegrityError(RuntimeError):
    """Raised when a published contract or manifest is invalid."""


class ContractStaleError(ContractIntegrityError):
    """Raised when a valid contract exceeds its declared freshness policy."""


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _policy_for(path: Path) -> ContractPolicy:
    try:
        relative = path.resolve().relative_to(CONTRACT_ROOT.resolve()).as_posix()
    except ValueError as exc:
        raise ContractIntegrityError("contract path is outside the canonical root") from exc
    policy = CONTRACT_POLICIES.get(relative)
    if policy is None:
        raise ContractIntegrityError(f"contract is not allowlisted: {relative}")
    return policy


def read_json_contract(
    path: Path,
    *,
    policy: ContractPolicy | None = None,
    now: datetime | None = None,
    require_schema_sha256: bool | None = None,
) -> dict[str, Any]:
    content = path.read_bytes()
    payload = json.loads(content.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ContractIntegrityError("contract root must be a JSON object")

    manifest_path = path.with_suffix(path.suffix + ".manifest.json")
    require_manifest = path.is_absolute() and path.resolve().is_relative_to(CONTRACT_ROOT.resolve())
    if not manifest_path.exists():
        if require_manifest:
            raise ContractIntegrityError(f"contract manifest is missing: {manifest_path}")
        return payload
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ContractIntegrityError("contract manifest root must be a JSON object")

    expected_sha256 = str(manifest.get("content_sha256") or "")
    actual_sha256 = hashlib.sha256(content).hexdigest()
    if expected_sha256 != actual_sha256:
        raise ContractIntegrityError("contract content hash does not match manifest")
    if str(manifest.get("artifact") or "") != path.name:
        raise ContractIntegrityError("contract artifact name does not match manifest")

    selected_policy = policy or (_policy_for(path) if require_manifest else None)
    if selected_policy is None:
        return payload
    _validate_manifest_policy(
        manifest,
        selected_policy,
        now=now,
        require_schema_sha256=require_schema_sha256,
    )
    return payload


def _validate_manifest_policy(
    manifest: dict[str, Any],
    policy: ContractPolicy,
    *,
    now: datetime | None,
    require_schema_sha256: bool | None,
) -> None:
    expected = {
        "contract_version": policy.contract_version,
        "source_project": policy.source_project,
        "schema": policy.schema,
    }
    for field, expected_value in expected.items():
        if str(manifest.get(field) or "") != expected_value:
            raise ContractIntegrityError(f"contract {field} does not match allowlisted policy")

    schema_sha256 = str(manifest.get("schema_sha256") or "")
    require_schema = (
        _bool_env("MM_CONTRACT_REQUIRE_SCHEMA_SHA256")
        if require_schema_sha256 is None
        else require_schema_sha256
    )
    if not schema_sha256:
        if require_schema:
            raise ContractIntegrityError("contract schema_sha256 is missing")
        logger.warning(
            "contract manifest is missing schema_sha256; compatibility window is active",
            extra={"contract_version": policy.contract_version},
        )
    elif schema_sha256 != policy.schema_sha256:
        raise ContractIntegrityError("contract schema hash does not match allowlisted policy")

    generated_raw = str(manifest.get("generated_at") or "")
    try:
        generated_at = datetime.fromisoformat(generated_raw)
    except ValueError as exc:
        raise ContractIntegrityError("contract generated_at is invalid") from exc
    if generated_at.tzinfo is None:
        raise ContractIntegrityError("contract generated_at must include a timezone")
    checked_at = now or datetime.now(UTC)
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=UTC)
    age = checked_at.astimezone(UTC) - generated_at.astimezone(UTC)
    if age > policy.max_age:
        raise ContractStaleError(f"contract is stale: age={age}, max_age={policy.max_age}")
