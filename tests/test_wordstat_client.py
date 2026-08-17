import json
from datetime import date
from types import SimpleNamespace

import httpx

from app.services.market_research import wordstat as wordstat_module
from app.services.market_research.wordstat import WordstatClient


def test_wordstat_client_uses_search_api_contract():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"totalCount": "321", "results": []})

    client = WordstatClient(
        api_key="secret-api-key",
        folder_id="b1g-folder",
        devices=["phone", "desktop"],
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    stats = client.get_stats(["дисплей iphone 17 pro max"], region="225")

    assert len(stats) == 1
    assert stats[0].phrase == "дисплей iphone 17 pro max"
    assert stats[0].region == "225"
    assert stats[0].impressions == 321
    assert stats[0].stat_date == date.today()
    assert stats[0].source == "wordstat"

    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == ("https://searchapi.api.cloud.yandex.net/v2/wordstat/topRequests")
    assert request.headers["Authorization"] == "Api-Key secret-api-key"
    assert request.headers["Content-Type"] == "application/json"
    assert json.loads(request.content) == {
        "phrase": "дисплей iphone 17 pro max",
        "numPhrases": "1",
        "regions": ["225"],
        "devices": ["DEVICE_DESKTOP", "DEVICE_PHONE"],
        "folderId": "b1g-folder",
    }


def test_wordstat_client_skips_requests_without_credentials():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request: {request.url}")

    client = WordstatClient(
        api_key="",
        folder_id="",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert client.get_stats(["дисплей iphone 17 pro max"], region="225") == []


def test_wordstat_client_normalizes_all_devices():
    client = WordstatClient(api_key="key", folder_id="folder", devices=["all", "phone"])

    assert client.devices == ["DEVICE_ALL"]


def test_wordstat_factory_uses_dedicated_credentials(monkeypatch):
    settings = SimpleNamespace(
        yandex_wordstat_api_key="wordstat-key",
        yandex_wordstat_folder_id="b1g-folder",
        yandex_wordstat_base_url="https://searchapi.api.cloud.yandex.net",
        yandex_wordstat_devices="all",
        yandex_wordstat_rps_limit=1.0,
        yandex_wordstat_timeout=12.0,
        yandex_direct_api_token="direct-token-must-not-be-used",
        yandex_direct_rps_limit=99.0,
        yandex_direct_timeout=99.0,
    )
    monkeypatch.setattr(wordstat_module, "get_settings", lambda: settings)

    client = wordstat_module.build_wordstat_client_from_settings()

    assert client.api_key == "wordstat-key"
    assert client.folder_id == "b1g-folder"
    assert client.devices == ["DEVICE_ALL"]
    assert client.rps_limit == 1.0
    assert client.timeout == 12.0


def test_wordstat_dynamics_uses_rfc3339_dates_and_returns_raw_response():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "date": "2026-06-01T00:00:00Z",
                        "count": "797",
                        "share": 0.000008114,
                    }
                ]
            },
        )

    client = WordstatClient(
        api_key="secret-api-key",
        folder_id="b1g-folder",
        devices=["all"],
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    response = client.get_dynamics_response(
        phrase="дисплей iphone 17 pro max",
        region="225",
        from_date=date(2026, 1, 1),
        to_date=date(2026, 7, 31),
    )

    assert response == {
        "results": [
            {
                "date": "2026-06-01T00:00:00Z",
                "count": "797",
                "share": 0.000008114,
            }
        ]
    }
    request = requests[0]
    assert str(request.url) == "https://searchapi.api.cloud.yandex.net/v2/wordstat/dynamics"
    assert json.loads(request.content) == {
        "phrase": "дисплей iphone 17 pro max",
        "period": "PERIOD_MONTHLY",
        "fromDate": "2026-01-01T00:00:00Z",
        "toDate": "2026-07-31T23:59:59Z",
        "regions": ["225"],
        "devices": ["DEVICE_ALL"],
        "folderId": "b1g-folder",
    }


def test_wordstat_dynamics_rejects_unknown_period_without_request():
    client = WordstatClient(api_key="key", folder_id="folder")

    try:
        client.get_dynamics_response(
            phrase="дисплей iphone 17 pro max",
            region="225",
            from_date=date(2026, 1, 1),
            to_date=date(2026, 7, 31),
            period="quarterly",
        )
    except ValueError as error:
        assert str(error) == "unsupported_wordstat_period:quarterly"
    else:
        raise AssertionError("ValueError was not raised")
