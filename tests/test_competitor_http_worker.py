from datetime import date, datetime, timezone

import httpx
import pytest

from app.workers import competitor_http
from app.workers.competitor_http import (
    _candidate_dates,
    _response_mtime,
    parse_http_sources,
    run_competitor_http_import,
)


def test_parse_http_sources_accepts_https_date_patterns():
    sources = parse_http_sources(
        "moba:https://service.example/moba-{date}.xlsx,"
        "liberti:https://service.example/liberti-1-{date}.xlsx"
    )

    assert [(source.name, source.url_pattern) for source in sources] == [
        ("moba", "https://service.example/moba-{date}.xlsx"),
        ("liberti", "https://service.example/liberti-1-{date}.xlsx"),
    ]


def test_parse_http_sources_rejects_insecure_or_undated_urls():
    assert parse_http_sources("moba:http://service.example/moba-{date}.xlsx") == []
    assert parse_http_sources("moba:https://service.example/moba.xlsx") == []


def test_candidate_dates_include_today_and_previous_day():
    assert _candidate_dates(2, today=date(2026, 7, 15)) == [
        date(2026, 7, 15),
        date(2026, 7, 14),
    ]


def test_response_mtime_reads_last_modified_header():
    response = httpx.Response(
        200,
        headers={"Last-Modified": "Wed, 29 Jul 2026 10:00:00 GMT"},
        request=httpx.Request("GET", "https://service.example/file.xlsx"),
    )

    assert _response_mtime(response) == datetime(2026, 7, 29, 10, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("status_code", [404, 503])
def test_http_import_handles_not_found_and_http_errors(monkeypatch, db_session, status_code):
    settings = type(
        "Settings",
        (),
        {
            "competitor_http_import_enabled": True,
            "competitor_http_sources": "moba:https://service.example/moba-{date}.xlsx",
            "competitor_http_max_files_per_source": 1,
            "competitor_http_timeout_sec": 5,
        },
    )()
    monkeypatch.setattr(competitor_http, "get_settings", lambda: settings)

    def fake_get(url, timeout):
        return httpx.Response(status_code, request=httpx.Request("GET", url))

    monkeypatch.setattr(competitor_http.httpx, "get", fake_get)

    result = run_competitor_http_import(db_session)

    assert result["processed_files"] == 0
    assert result["errors"] == (0 if status_code == 404 else 1)


def test_http_import_ingests_successful_file(monkeypatch, db_session):
    settings = type(
        "Settings",
        (),
        {
            "competitor_http_import_enabled": True,
            "competitor_http_sources": "moba:https://service.example/moba-{date}.xlsx",
            "competitor_http_max_files_per_source": 1,
            "competitor_http_timeout_sec": 5,
        },
    )()
    monkeypatch.setattr(competitor_http, "get_settings", lambda: settings)
    monkeypatch.setattr(
        competitor_http.httpx,
        "get",
        lambda url, timeout: httpx.Response(
            200,
            content=b"xlsx",
            request=httpx.Request("GET", url),
        ),
    )
    monkeypatch.setattr(
        competitor_http,
        "ingest_ftp_file",
        lambda session, file_info, content: {
            "rows_total": 2,
            "rows_valid": 2,
            "rows_invalid": 0,
        },
    )

    result = run_competitor_http_import(db_session)

    assert result["processed_files"] == 1
    assert result["rows_valid"] == 2
    assert result["errors"] == 0
