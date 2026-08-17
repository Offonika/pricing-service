from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import tasks.build_assortment_signal_source_bundle as source_task


def _registry_payload() -> dict[str, object]:
    return {
        "schema": "display_family_registry_snapshot.v1",
        "version_number": 7,
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


def _success_bundle() -> dict[str, object]:
    return {
        "schema": "assortment_signal_source_bundle.v1",
        "bundle_id": "test",
        "as_of": "2026-08-17T12:00:00+00:00",
        "items": [],
        "data_quality": {"status": "ready"},
    }


class _FakeEngine:
    def __init__(self) -> None:
        self.disposed = False

    def dispose(self) -> None:
        self.disposed = True


def test_task_with_registry_fixture_writes_only_output_and_never_opens_application_session(
    tmp_path: Path, monkeypatch
) -> None:
    registry = tmp_path / "registry.json"
    output = tmp_path / "result" / "bundle.json"
    registry.write_text(json.dumps(_registry_payload()), encoding="utf-8")
    engine = _FakeEngine()

    def unexpected_session_scope(*, read_only: bool = False):
        raise AssertionError(f"application session opened: {read_only=}")

    monkeypatch.setattr(source_task, "session_scope", unexpected_session_scope)
    monkeypatch.setattr(source_task, "build_onec_engine_from_settings", lambda: engine)
    monkeypatch.setattr(
        source_task,
        "extract_assortment_signal_source_bundle",
        lambda *_args, **_kwargs: _success_bundle(),
    )
    args = source_task.build_parser().parse_args(
        [
            "--date-from",
            "2026-08-01T00:00:00+03:00",
            "--as-of",
            "2026-08-17T15:00:00+03:00",
            "--family-registry-json",
            str(registry),
            "--output-json",
            str(output),
        ]
    )

    exit_code, result = source_task.run(args)

    assert exit_code == 0
    assert result["data_quality"]["status"] == "ready"
    assert json.loads(output.read_text(encoding="utf-8"))["schema"] == (
        "assortment_signal_source_bundle.v1"
    )
    assert engine.disposed is True
    assert sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*")) == [
        "registry.json",
        "result",
        "result/bundle.json",
    ]


def test_task_uses_read_only_application_session_for_active_registry(
    tmp_path: Path, monkeypatch
) -> None:
    output = tmp_path / "bundle.json"
    calls: list[bool] = []
    engine = _FakeEngine()

    @contextmanager
    def fake_session_scope(*, read_only: bool = False):
        calls.append(read_only)
        yield object()

    monkeypatch.setattr(source_task, "session_scope", fake_session_scope)
    monkeypatch.setattr(
        source_task,
        "load_active_display_family_registry_snapshot",
        lambda _session: object(),
    )
    monkeypatch.setattr(source_task, "build_onec_engine_from_settings", lambda: engine)
    monkeypatch.setattr(
        source_task,
        "extract_assortment_signal_source_bundle",
        lambda *_args, **_kwargs: _success_bundle(),
    )
    args = source_task.build_parser().parse_args(
        [
            "--date-from",
            "2026-08-01T00:00:00Z",
            "--as-of",
            "2026-08-17T12:00:00Z",
            "--output-json",
            str(output),
        ]
    )

    exit_code, _result = source_task.run(args)

    assert exit_code == 0
    assert calls == [True]
    assert engine.disposed is True


def test_cli_requires_timezone_and_has_no_write_switch() -> None:
    parser = source_task.build_parser()
    option_strings = {option for action in parser._actions for option in action.option_strings}

    assert "--apply" not in option_strings
    assert "--persist" not in option_strings
    assert "--write" not in option_strings
