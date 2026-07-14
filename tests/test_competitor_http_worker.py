from datetime import date

from app.workers.competitor_http import _candidate_dates, parse_http_sources


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
