"""Verified readers for neutral cross-project JSON contracts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

CONTRACT_ROOT = Path("/var/lib/mm-data-contracts")


class ContractIntegrityError(RuntimeError):
    """Raised when a published contract or manifest is invalid."""


def read_json_contract(path: Path) -> dict[str, Any]:
    content = path.read_bytes()
    payload = json.loads(content.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ContractIntegrityError("contract root must be a JSON object")

    manifest_path = path.with_suffix(path.suffix + ".manifest.json")
    require_manifest = path.is_absolute() and path.is_relative_to(CONTRACT_ROOT)
    if not manifest_path.exists():
        if require_manifest:
            raise ContractIntegrityError(f"contract manifest is missing: {manifest_path}")
        return payload
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_sha256 = str(manifest.get("content_sha256") or "")
    actual_sha256 = hashlib.sha256(content).hexdigest()
    if expected_sha256 != actual_sha256:
        raise ContractIntegrityError("contract content hash does not match manifest")
    if str(manifest.get("artifact") or "") != path.name:
        raise ContractIntegrityError("contract artifact name does not match manifest")
    return payload
