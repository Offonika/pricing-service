from __future__ import annotations

import pytest

from scripts import ensure_site_service_requests_ui_placement as placement


class _Api:
    def __init__(self, rows):
        self.rows = list(rows)
        self.calls = []

    def call_json(self, method, payload):
        self.calls.append((method, payload))
        if method == "placement.get":
            return {"result": list(self.rows)}
        if method == "placement.bind":
            self.rows.append(
                {
                    "PLACEMENT": payload["PLACEMENT"],
                    "HANDLER": payload["HANDLER"],
                }
            )
            return {"result": True}
        raise AssertionError(method)


HANDLER = "https://pricing.example/bitrix/site-service-requests/"


def test_placement_dry_run_accepts_one_exact_binding():
    api = _Api(
        [
            {
                "PLACEMENT": placement.PLACEMENT,
                "HANDLER": "https://pricing.example/bitrix/site-service-requests",
            }
        ]
    )

    result = placement.ensure(apply=False, api=api, handler=HANDLER)

    assert result["alreadyBound"] is True
    assert result["bound"] is True
    assert [method for method, _payload in api.calls] == ["placement.get"]


def test_placement_refuses_conflicting_handler():
    api = _Api(
        [
            {
                "PLACEMENT": placement.PLACEMENT,
                "HANDLER": "https://old.example/bitrix/site-service-requests/",
            }
        ]
    )

    with pytest.raises(RuntimeError, match="placement_conflict"):
        placement.ensure(apply=True, api=api, handler=HANDLER)

    assert [method for method, _payload in api.calls] == ["placement.get"]


def test_placement_apply_binds_then_reads_back():
    api = _Api([])

    result = placement.ensure(apply=True, api=api, handler=HANDLER)

    assert result == {
        "placement": placement.PLACEMENT,
        "handler": HANDLER,
        "alreadyBound": False,
        "bound": True,
        "applied": True,
    }
    assert [method for method, _payload in api.calls] == [
        "placement.get",
        "placement.bind",
        "placement.get",
    ]


@pytest.mark.parametrize("result", [None, {}, ["malformed"]])
def test_placement_readback_is_fail_closed(result):
    class _MalformedApi:
        def call_json(self, method, payload):
            assert method == "placement.get"
            return {"result": result}

    with pytest.raises(RuntimeError, match="placement_readback_invalid"):
        placement.ensure(apply=False, api=_MalformedApi(), handler=HANDLER)


def test_application_context_uses_portal_host_and_injects_auth(monkeypatch):
    captured = {}

    class _Transport:
        def call_json(self, method, payload):
            captured["call"] = (method, payload)
            return {"result": []}

    def client(base_url):
        captured["base_url"] = base_url
        return _Transport()

    monkeypatch.setattr(placement, "BitrixRestClient", client)

    api = placement._application_api(
        webhook_url="https://portal.example/rest/7/webhook-token",
        access_token="application-access-token",
    )
    assert api.call_json("placement.get", {}) == {"result": []}

    assert captured == {
        "base_url": "https://portal.example/rest",
        "call": (
            "placement.get",
            {"auth": "application-access-token"},
        ),
    }


@pytest.mark.parametrize("access_token", ["", " ", " token", "token\n"])
def test_application_context_requires_clean_ephemeral_token(access_token):
    with pytest.raises(RuntimeError, match="application_context_not_configured"):
        placement._application_api(
            webhook_url="https://portal.example/rest/7/webhook-token",
            access_token=access_token,
        )


@pytest.mark.parametrize(
    "webhook_url",
    [
        "",
        "http://portal.example/rest/7/token",
        "https://user@portal.example/rest/7/token",
        "https://portal.example:8443/rest/7/token",
        "https://portal.example:bad/rest/7/token",
        "https://portal.example/rest/7/token?query=1",
        "https://portal.example/rest/7/token#fragment",
    ],
)
def test_application_context_rejects_untrusted_portal_url(webhook_url):
    with pytest.raises(RuntimeError, match="portal_invalid"):
        placement._application_api(
            webhook_url=webhook_url,
            access_token="application-access-token",
        )


def test_application_context_refuses_caller_supplied_auth(monkeypatch):
    monkeypatch.setattr(placement, "BitrixRestClient", lambda _base_url: object())
    api = placement._application_api(
        webhook_url="https://portal.example/rest/7/webhook-token",
        access_token="application-access-token",
    )

    with pytest.raises(RuntimeError, match="application_payload_invalid"):
        api.call_json("placement.get", {"auth": "different-token"})
