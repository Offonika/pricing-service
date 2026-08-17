from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import tasks.build_assortment_signal_ingestion_dry_run as ingestion_task
from app.services.assortment_lifecycle_signal_ingestion import (
    display_family_registry_snapshot_from_mapping,
)


def _source_payload() -> dict[str, object]:
    return {
        "schema": "assortment_signal_source_bundle.v1",
        "bundle_id": "task-test",
        "as_of": "2026-08-17T12:00:00+00:00",
        "items": [
            {
                "signal_type": "customer_sale",
                "source": "test",
                "source_event_id": "sale-1",
                "occurred_at": "2026-08-17T09:00:00+00:00",
                "available_at": "2026-08-17T09:01:00+00:00",
                "reliability": "1",
                "reliability_reason": "test_fixture",
                "nomenclature_code": "SKU-1",
                "name": "Дисплей iPhone 17 Pro Max",
                "quantity": "2",
            }
        ],
    }


def _registry_payload() -> dict[str, object]:
    return {
        "schema": "display_family_registry_snapshot.v1",
        "version_number": 2,
        "status": "active",
        "members": [
            {
                "product_id": 1,
                "family_key": "iphone-17-pro-max",
                "nomenclature_code": "SKU-1",
                "aliases": ["ARTICLE-1"],
                "name": "Дисплей iPhone 17 Pro Max",
            }
        ],
    }


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_task_with_registry_fixture_writes_only_requested_artifact(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source.json"
    registry = tmp_path / "registry.json"
    output = tmp_path / "result" / "reconciliation.json"
    _write_json(source, _source_payload())
    _write_json(registry, _registry_payload())
    source_before = source.read_bytes()
    registry_before = registry.read_bytes()

    def unexpected_session_scope(*, read_only: bool = False):
        raise AssertionError(f"database session opened: read_only={read_only}")

    monkeypatch.setattr(ingestion_task, "session_scope", unexpected_session_scope)
    args = ingestion_task.build_parser().parse_args(
        [
            "--input-json",
            str(source),
            "--family-registry-json",
            str(registry),
            "--output-json",
            str(output),
        ]
    )

    exit_code, result = ingestion_task.run(args)

    assert exit_code == 0
    assert result["status"] == "ready"
    assert output.is_file()
    assert (
        json.loads(output.read_text(encoding="utf-8"))["prepared_signals"][0]["nomenclature_code"]
        == "SKU-1"
    )
    assert source.read_bytes() == source_before
    assert registry.read_bytes() == registry_before
    assert sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*")) == [
        "registry.json",
        "result",
        "result/reconciliation.json",
        "source.json",
    ]


def test_task_uses_only_read_only_session_for_active_registry(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.json"
    output = tmp_path / "result.json"
    _write_json(source, _source_payload())
    calls: list[bool] = []
    snapshot = display_family_registry_snapshot_from_mapping(_registry_payload())

    @contextmanager
    def fake_session_scope(*, read_only: bool = False):
        calls.append(read_only)
        yield object()

    monkeypatch.setattr(ingestion_task, "session_scope", fake_session_scope)
    monkeypatch.setattr(
        ingestion_task,
        "load_active_display_family_registry_snapshot",
        lambda _session: snapshot,
    )
    args = ingestion_task.build_parser().parse_args(
        ["--input-json", str(source), "--output-json", str(output)]
    )

    exit_code, result = ingestion_task.run(args)

    assert exit_code == 0
    assert result["status"] == "ready"
    assert calls == [True]
    assert output.is_file()


def test_task_uses_embedded_registry_without_opening_application_session(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source.json"
    output = tmp_path / "result.json"
    payload = _source_payload()
    payload["family_registry_snapshot"] = _registry_payload()
    _write_json(source, payload)

    def unexpected_session_scope(*, read_only: bool = False):
        raise AssertionError(f"database session opened: {read_only=}")

    monkeypatch.setattr(ingestion_task, "session_scope", unexpected_session_scope)
    args = ingestion_task.build_parser().parse_args(
        ["--input-json", str(source), "--output-json", str(output)]
    )

    exit_code, result = ingestion_task.run(args)

    assert exit_code == 0
    assert result["status"] == "ready"
    assert result["family_registry"]["version_number"] == 2


def test_task_writes_blocked_artifact_for_inactive_registry_fixture(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.json"
    registry = tmp_path / "registry.json"
    output = tmp_path / "result.json"
    _write_json(source, _source_payload())
    inactive_registry = _registry_payload()
    inactive_registry["status"] = "superseded"
    _write_json(registry, inactive_registry)
    args = ingestion_task.build_parser().parse_args(
        [
            "--input-json",
            str(source),
            "--family-registry-json",
            str(registry),
            "--output-json",
            str(output),
        ]
    )

    exit_code, result = ingestion_task.run(args)

    assert exit_code == 2
    assert result["status"] == "blocked"
    assert result["error"] == "family_registry_not_active:superseded"
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "blocked"


def test_task_refuses_to_overwrite_an_input_artifact(tmp_path: Path) -> None:
    source = tmp_path / "source.json"
    registry = tmp_path / "registry.json"
    _write_json(source, _source_payload())
    _write_json(registry, _registry_payload())
    source_before = source.read_bytes()
    args = ingestion_task.build_parser().parse_args(
        [
            "--input-json",
            str(source),
            "--family-registry-json",
            str(registry),
            "--output-json",
            str(source),
        ]
    )

    exit_code, result = ingestion_task.run(args)

    assert exit_code == 2
    assert result["status"] == "blocked"
    assert result["error"] == "output_json_must_not_overwrite_an_input_artifact"
    assert source.read_bytes() == source_before


def test_cli_has_no_apply_or_persistence_switch() -> None:
    parser = ingestion_task.build_parser()
    option_strings = {option for action in parser._actions for option in action.option_strings}

    assert "--apply" not in option_strings
    assert "--persist" not in option_strings
