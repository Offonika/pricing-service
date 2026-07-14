from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.infrastructure.contracts import ContractIntegrityError, read_json_contract


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
