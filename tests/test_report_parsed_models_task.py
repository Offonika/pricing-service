from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace

from tasks import report_parsed_models


class _Query:
    def __init__(self, records: list[SimpleNamespace]) -> None:
        self._records = records

    def filter(self, *_conditions: object) -> _Query:
        return self

    def all(self) -> list[SimpleNamespace]:
        return self._records


class _Session:
    def __init__(self, records: list[SimpleNamespace]) -> None:
        self._records = records

    def query(self, _model: object) -> _Query:
        return _Query(self._records)


def test_report_uses_read_only_scope_and_prints_summary(monkeypatch, capsys) -> None:
    records = [
        SimpleNamespace(
            source="supplier-a",
            sku="A-1",
            name="Display Model A",
            parsed_device_brand="Brand",
            parsed_device_model="Model A",
            parsed_device_variant=None,
            parse_confidence=0.95,
            parse_notes=None,
        ),
        SimpleNamespace(
            source="supplier-b",
            sku="B-1",
            name="Unknown display",
            parsed_device_brand=None,
            parsed_device_model=None,
            parsed_device_variant=None,
            parse_confidence=None,
            parse_notes="ambiguous",
        ),
    ]
    calls: list[bool] = []

    @contextmanager
    def fake_session_scope(*, read_only: bool = False):
        calls.append(read_only)
        yield _Session(records)

    monkeypatch.setattr(report_parsed_models, "session_scope", fake_session_scope)
    monkeypatch.setattr(
        "sys.argv",
        ["report_parsed_models", "--days-back", "0", "--limit-samples", "1"],
    )

    report_parsed_models.main()

    payload = json.loads(capsys.readouterr().out)
    assert calls == [True]
    assert payload["total"] == 2
    assert payload["with_parsed_device_model"] == 1
    assert payload["ambiguous"] == 1
    assert payload["confidence_buckets"] == {"0.90+": 1, "none": 1}
    assert payload["low_conf_samples"][0]["sku"] == "B-1"
    assert payload["unparsed_samples"][0]["sku"] == "B-1"
