from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.services.margin_flow_pipeline_observer import file_sha256
from tasks import observe_margin_flow_pipeline as task


class _FakeEngine:
    def __init__(self) -> None:
        self.disposed = False

    def dispose(self) -> None:
        self.disposed = True


def _inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    scope = tmp_path / "scope.csv"
    scope.write_text("nomenclature_code\nSKU-1\n", encoding="utf-8")
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "schema": "margin_flow_pipeline_forward_observer.v1",
                "observer_id": "observer-task-test-v1",
                "timezone": "Europe/Moscow",
                "minimum_matured_consecutive_days": 105,
                "scope": {
                    "source_bundle": "test-bundle",
                    "expected_sha256": file_sha256(scope),
                    "expected_code_count": 1,
                },
                "safety": {
                    "onec_read_only_required": True,
                    "application_database_writes": False,
                    "onec_writes": False,
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
        ),
        encoding="utf-8",
    )
    return config, scope, tmp_path / "observer"


def _argv(config: Path, scope: Path, output_root: Path) -> list[str]:
    return [
        "capture",
        "--config",
        str(config),
        "--scope-csv",
        str(scope),
        "--output-root",
        str(output_root),
        "--observation-slot",
        "2026-08-17",
        "--json",
    ]


def test_capture_reads_source_once_then_reuses_immutable_daily_slot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config, scope, output_root = _inputs(tmp_path)
    engines: list[_FakeEngine] = []

    def engine_factory() -> _FakeEngine:
        engine = _FakeEngine()
        engines.append(engine)
        return engine

    def read_source(engine: _FakeEngine, *, codes: list[str]):
        assert codes == ["SKU-1"]
        return (
            [
                {
                    "nomenclature_code": "SKU-1",
                    "order_ref_hex": "0xAA",
                    "product_ref_hex": "0xBB",
                    "quantity": Decimal("3"),
                    "register_row_count": 1,
                    "order_revision_fingerprint": "0x01",
                    "order_created_at_raw": datetime(2026, 8, 1),
                    "expected_receipt_at_raw": datetime(2026, 8, 25),
                    "cargo_handoff_at_raw": None,
                    "marked_raw": "0x00",
                    "posted_raw": "0x01",
                }
            ],
            {"status": "pass_effective_object_permissions_read_only", "objects": []},
            "2026-08-17T07:00:00Z",
            "2026-08-17T07:00:01Z",
        )

    monkeypatch.setattr(task, "build_onec_engine_from_settings", engine_factory)
    monkeypatch.setattr(task, "read_source_snapshot", read_source)

    assert task.main(_argv(config, scope, output_root)) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["status"] == "captured"
    assert first["source"] == "onec_read_only"
    assert first["external_writes"] is False
    assert first["recommended_order_qty_calculated"] is False
    assert engines[0].disposed is True

    monkeypatch.setattr(
        task,
        "read_source_snapshot",
        lambda *_args, **_kwargs: pytest.fail("source must not be read for an existing slot"),
    )
    assert task.main(_argv(config, scope, output_root)) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["status"] == "reused"
    assert len(engines) == 1


def test_source_failure_does_not_publish_partial_observation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config, scope, output_root = _inputs(tmp_path)
    engine = _FakeEngine()
    monkeypatch.setattr(task, "build_onec_engine_from_settings", lambda: engine)
    monkeypatch.setattr(
        task,
        "read_source_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("source unavailable")),
    )

    with pytest.raises(RuntimeError, match="source unavailable"):
        task.main(_argv(config, scope, output_root))

    assert engine.disposed is True
    assert not (output_root / "observer.json").exists()
    assert not (output_root / "observations" / "2026-08-17").exists()
