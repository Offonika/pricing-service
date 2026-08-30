from __future__ import annotations

import pytest

from app.core.config import Settings
from scripts import ensure_site_service_requests_ui_placement as placement


class _Api:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def call_json(self, method, payload):
        self.calls.append((method, payload))
        if method == "placement.get":
            return {"result": self.rows}
        raise AssertionError(method)


def _configure(monkeypatch, api):
    monkeypatch.setattr(
        placement,
        "get_settings",
        lambda: Settings(
            site_service_requests_bitrix_webhook_url=("https://portal.example/rest/1/token"),
            site_service_requests_ui_handler_url=(
                "https://pricing.example/bitrix/site-service-requests/"
            ),
        ),
    )
    monkeypatch.setattr(placement, "BitrixRestClient", lambda _webhook: api)


def test_placement_dry_run_accepts_one_exact_binding(monkeypatch):
    api = _Api(
        [
            {
                "PLACEMENT": placement.PLACEMENT,
                "HANDLER": "https://pricing.example/bitrix/site-service-requests",
            }
        ]
    )
    _configure(monkeypatch, api)

    result = placement.ensure(apply=False)

    assert result["alreadyBound"] is True
    assert result["bound"] is True
    assert [method for method, _payload in api.calls] == ["placement.get"]


def test_placement_refuses_conflicting_handler(monkeypatch):
    api = _Api(
        [
            {
                "PLACEMENT": placement.PLACEMENT,
                "HANDLER": "https://old.example/bitrix/site-service-requests/",
            }
        ]
    )
    _configure(monkeypatch, api)

    with pytest.raises(RuntimeError, match="placement_conflict"):
        placement.ensure(apply=True)

    assert [method for method, _payload in api.calls] == ["placement.get"]
