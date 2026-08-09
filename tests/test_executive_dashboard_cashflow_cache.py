from __future__ import annotations

import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.infrastructure.contracts import ContractStaleError
from app.services import executive_dashboard


def _write_payload(path: Path, *, revision: str = "one") -> None:
    path.write_text(
        json.dumps({"source_status": "ready", "revision": revision}),
        encoding="utf-8",
    )


def _write_manifest(path: Path, *, publication: str) -> None:
    content = path.read_bytes()
    path.with_suffix(".json.manifest.json").write_text(
        json.dumps(
            {
                "artifact": path.name,
                "content_sha256": hashlib.sha256(content).hexdigest(),
                "publication": publication,
            }
        ),
        encoding="utf-8",
    )


def _count_contract_reads(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    calls: list[Path] = []
    original_reader = executive_dashboard.read_json_contract

    def counting_reader(path: Path) -> dict:
        calls.append(path)
        return original_reader(path)

    monkeypatch.setattr(executive_dashboard, "read_json_contract", counting_reader)
    return calls


def test_cashflow_cache_decodes_artifact_once_per_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "cashflow_period_cache.json"
    _write_payload(path)
    monkeypatch.setattr(
        executive_dashboard,
        "_resolve_cashflow_period_cache_path",
        lambda: path,
    )
    calls = _count_contract_reads(monkeypatch)

    first = executive_dashboard._load_cashflow_period_cache()
    second = executive_dashboard._load_cashflow_period_cache()

    assert first[0] is second[0]
    assert calls == [path]


def test_cashflow_cache_invalidates_when_artifact_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "cashflow_period_cache.json"
    _write_payload(path)
    monkeypatch.setattr(
        executive_dashboard,
        "_resolve_cashflow_period_cache_path",
        lambda: path,
    )
    calls = _count_contract_reads(monkeypatch)

    first = executive_dashboard._load_cashflow_period_cache()
    _write_payload(path, revision="a-longer-second-revision")
    second = executive_dashboard._load_cashflow_period_cache()

    assert first[0] is not second[0]
    assert second[0]["revision"] == "a-longer-second-revision"
    assert calls == [path, path]


def test_cashflow_cache_invalidates_when_manifest_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "cashflow_period_cache.json"
    _write_payload(path)
    _write_manifest(path, publication="one")
    monkeypatch.setattr(
        executive_dashboard,
        "_resolve_cashflow_period_cache_path",
        lambda: path,
    )
    calls = _count_contract_reads(monkeypatch)

    executive_dashboard._load_cashflow_period_cache()
    _write_manifest(path, publication="a-longer-second-publication")
    executive_dashboard._load_cashflow_period_cache()

    assert calls == [path, path]


def test_cached_cashflow_revalidates_manifest_freshness_without_decoding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "cashflow_period_cache.json"
    _write_payload(path)
    _write_manifest(path, publication="one")
    monkeypatch.setattr(
        executive_dashboard,
        "_resolve_cashflow_period_cache_path",
        lambda: path,
    )
    reads = _count_contract_reads(monkeypatch)
    original_validator = executive_dashboard.validate_json_contract_manifest
    validations = 0

    def freshness_validator(cache_path: Path, *, actual_content_sha256: str) -> dict | None:
        nonlocal validations
        validations += 1
        if validations == 2:
            raise ContractStaleError("contract is stale")
        return original_validator(
            cache_path,
            actual_content_sha256=actual_content_sha256,
        )

    monkeypatch.setattr(
        executive_dashboard,
        "validate_json_contract_manifest",
        freshness_validator,
    )

    first = executive_dashboard._load_cashflow_period_cache()
    second = executive_dashboard._load_cashflow_period_cache()

    assert first[1] == "ready"
    assert second[0] is None
    assert second[1] == "source_error"
    assert "contract is stale" in second[2]
    assert reads == [path]


def test_parallel_cashflow_cache_load_is_single_flight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "cashflow_period_cache.json"
    _write_payload(path)
    monkeypatch.setattr(
        executive_dashboard,
        "_resolve_cashflow_period_cache_path",
        lambda: path,
    )
    original_reader = executive_dashboard.read_json_contract
    calls = 0
    calls_lock = threading.Lock()

    def slow_reader(cache_path: Path) -> dict:
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.05)
        return original_reader(cache_path)

    monkeypatch.setattr(executive_dashboard, "read_json_contract", slow_reader)

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(
            executor.map(lambda _: executive_dashboard._load_cashflow_period_cache(), range(8))
        )

    assert calls == 1
    assert all(result[0] is results[0][0] for result in results)
