from __future__ import annotations

import urllib.error
from pathlib import Path

from app.services.master_mobile_catalog import MasterMobileCatalogResolver

FIXTURES = Path(__file__).parent / "fixtures" / "master_mobile_catalog"
SEARCH_URL = "https://master-mobile.ru/catalog/?q=044702"
CARD_URL = "https://master-mobile.ru/catalog/zapchasti/akkumulyatory/40699/"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _resolver(search_fixture: str, card_fixture: str = "card_exact.html"):
    calls: list[str] = []

    def fetch(url: str) -> str:
        calls.append(url)
        return _fixture(search_fixture if "/catalog/?" in url else card_fixture)

    return MasterMobileCatalogResolver(fetch_html=fetch, retry_sleep=lambda _: None), calls


def test_resolver_accepts_one_exact_article_and_first_large_gallery_photo() -> None:
    resolver, calls = _resolver("search_exact.html")

    result = resolver.resolve("044702")
    cached = resolver.resolve("044702")

    assert result.status == "found"
    assert result.product_id == "40699"
    assert result.product_card_url == CARD_URL
    assert result.photo_original_url == "https://master-mobile.ru/upload/original/40699.webp"
    assert result.photo_thumbnail_url == "https://master-mobile.ru/upload/thumb/40699.webp"
    assert cached == result
    assert calls == [SEARCH_URL, CARD_URL]


def test_resolver_does_not_match_by_name_when_exact_article_is_absent() -> None:
    resolver, calls = _resolver("search_none.html")

    result = resolver.resolve("044702")

    assert result.status == "not_found"
    assert calls == [SEARCH_URL]


def test_resolver_rejects_multiple_exact_article_cards() -> None:
    resolver, calls = _resolver("search_ambiguous.html")

    result = resolver.resolve("044702")

    assert result.status == "ambiguous"
    assert calls == [SEARCH_URL]


def test_resolver_rechecks_article_inside_product_card() -> None:
    resolver, _calls = _resolver("search_exact.html", "card_mismatch.html")

    result = resolver.resolve("044702")

    assert result.status == "article_mismatch"


def test_resolver_rejects_original_photo_outside_trusted_https_host() -> None:
    resolver, _calls = _resolver("search_exact.html", "card_unsafe.html")

    result = resolver.resolve("044702")

    assert result.status == "unsafe_url"


def test_resolver_retries_bounded_transient_failures() -> None:
    attempts = 0

    def fetch(url: str) -> str:
        nonlocal attempts
        if "/catalog/?" in url:
            attempts += 1
            if attempts < 3:
                raise urllib.error.URLError("temporary failure")
            return _fixture("search_exact.html")
        return _fixture("card_exact.html")

    resolver = MasterMobileCatalogResolver(
        fetch_html=fetch,
        max_attempts=3,
        retry_sleep=lambda _: None,
    )

    assert resolver.resolve("044702").status == "found"
    assert attempts == 3
