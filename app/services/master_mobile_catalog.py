"""Read-only exact-article resolver for the public Master Mobile catalog."""

from __future__ import annotations

import html as html_module
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Literal

TRUSTED_CATALOG_HOST = "master-mobile.ru"
PHOTO_SOURCE = "master_mobile_site"
MAX_HTML_BYTES = 5 * 1024 * 1024

ResolutionStatus = Literal[
    "found",
    "not_found",
    "ambiguous",
    "article_mismatch",
    "photo_missing",
    "unsafe_url",
    "fetch_error",
]


@dataclass(frozen=True)
class ProductMediaResolution:
    article: str
    status: ResolutionStatus
    product_card_url: str | None = None
    photo_original_url: str | None = None
    photo_thumbnail_url: str | None = None
    product_id: str | None = None
    detail: str | None = None

    @property
    def found(self) -> bool:
        return self.status == "found"


@dataclass(frozen=True)
class _SearchCandidate:
    card_url: str
    thumbnail_url: str | None
    product_id: str | None


class _TrustedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(  # type: ignore[override]
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> urllib.request.Request | None:
        if _trusted_https_url(newurl) is None:
            raise urllib.error.URLError("redirect target is outside trusted catalog host")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class MasterMobileCatalogResolver:
    """Resolve product card and original image using an exact public article."""

    def __init__(
        self,
        *,
        base_url: str = "https://master-mobile.ru",
        timeout_seconds: float = 15.0,
        max_attempts: int = 3,
        max_workers: int = 4,
        fetch_html: Callable[[str], str] | None = None,
        retry_sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        normalized_base = _trusted_https_url(base_url)
        if normalized_base is None:
            raise ValueError("Master Mobile catalog URL must use trusted HTTPS host")
        self.base_url = normalized_base.rstrip("/")
        self.timeout_seconds = max(float(timeout_seconds), 0.1)
        self.max_attempts = min(max(int(max_attempts), 1), 5)
        self.max_workers = min(max(int(max_workers), 1), 8)
        self._fetch_html_override = fetch_html
        self._retry_sleep = retry_sleep
        self._cache: dict[str, ProductMediaResolution] = {}
        self._cache_lock = threading.Lock()
        # The shared host proxy is intentionally bypassed only for the fixed,
        # validated public catalog host. Redirects cannot leave that host.
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _TrustedRedirectHandler(),
        )

    def resolve_many(self, articles: list[str]) -> dict[str, ProductMediaResolution]:
        unique = list(dict.fromkeys(article.strip() for article in articles if article.strip()))
        if not unique:
            return {}
        with ThreadPoolExecutor(max_workers=min(self.max_workers, len(unique))) as executor:
            resolutions = list(executor.map(self.resolve, unique))
        return {resolution.article: resolution for resolution in resolutions}

    def resolve(self, article: str) -> ProductMediaResolution:
        article = article.strip()
        if not article:
            return ProductMediaResolution(article=article, status="not_found")
        cache_key = _normalize_article(article)
        with self._cache_lock:
            cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        resolution = self._resolve_uncached(article)
        with self._cache_lock:
            self._cache[cache_key] = resolution
        return resolution

    def _resolve_uncached(self, article: str) -> ProductMediaResolution:
        search_url = f"{self.base_url}/catalog/?{urllib.parse.urlencode({'q': article})}"
        try:
            search_html = self._fetch_with_retries(search_url)
        except (OSError, RuntimeError, urllib.error.URLError) as exc:
            return ProductMediaResolution(
                article=article,
                status="fetch_error",
                detail=_safe_error_detail(exc),
            )
        candidates, unsafe_exact = _parse_exact_search_candidates(
            search_html,
            article=article,
            base_url=self.base_url,
        )
        if not candidates:
            return ProductMediaResolution(
                article=article,
                status="unsafe_url" if unsafe_exact else "not_found",
            )
        if len(candidates) != 1:
            return ProductMediaResolution(article=article, status="ambiguous")
        candidate = candidates[0]
        try:
            card_html = self._fetch_with_retries(candidate.card_url)
        except (OSError, RuntimeError, urllib.error.URLError) as exc:
            return ProductMediaResolution(
                article=article,
                status="fetch_error",
                product_id=candidate.product_id,
                detail=_safe_error_detail(exc),
            )
        return _parse_product_card(
            card_html,
            article=article,
            candidate=candidate,
            base_url=self.base_url,
        )

    def _fetch_with_retries(self, url: str) -> str:
        trusted_url = _trusted_https_url(url)
        if trusted_url is None:
            raise urllib.error.URLError("request URL is outside trusted catalog host")
        last_error: BaseException | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                return self._fetch_html(trusted_url)
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code < 500 and exc.code != 429:
                    break
            except (urllib.error.URLError, OSError) as exc:
                last_error = exc
            if attempt < self.max_attempts:
                self._retry_sleep(0.25 * attempt)
        if last_error is None:
            raise RuntimeError("catalog request failed")
        raise RuntimeError(_safe_error_detail(last_error)) from last_error

    def _fetch_html(self, url: str) -> str:
        if self._fetch_html_override is not None:
            return self._fetch_html_override(url)
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "ru-RU,ru;q=0.9",
                "User-Agent": "MasterMobileProcurementAssistant/1.0",
            },
            method="GET",
        )
        with self._opener.open(request, timeout=self.timeout_seconds) as response:  # noqa: S310
            if _trusted_https_url(response.geturl()) is None:
                raise urllib.error.URLError("response URL is outside trusted catalog host")
            raw = response.read(MAX_HTML_BYTES + 1)
            if len(raw) > MAX_HTML_BYTES:
                raise RuntimeError("catalog response exceeds size limit")
            encoding = response.headers.get_content_charset() or "utf-8"
            return raw.decode(encoding, errors="replace")


def _parse_exact_search_candidates(
    body: str,
    *,
    article: str,
    base_url: str,
) -> tuple[list[_SearchCandidate], bool]:
    start = body.find("data-items-container")
    listing = body[start:] if start >= 0 else body
    chunks = re.split(
        r"(?=<div(?=[^>]*\bdata-card=[\"']block[\"'])[^>]*>)",
        listing,
        flags=re.IGNORECASE,
    )
    expected = _normalize_article(article)
    candidates: dict[str, _SearchCandidate] = {}
    unsafe_exact = False
    for chunk in chunks:
        if not re.search(r"\bdata-card=[\"']block[\"']", chunk[:1200], re.IGNORECASE):
            continue
        articles = re.findall(r"Арт\.\s*:\s*([^<]+)<", chunk, flags=re.IGNORECASE)
        if expected not in {_normalize_article(value) for value in articles}:
            continue
        href_match = re.search(
            r"href=[\"']([^\"']*/catalog/[^\"']+/\d+/)[\"']",
            chunk,
            flags=re.IGNORECASE,
        )
        if href_match is None:
            continue
        card_url = _trusted_https_url(urllib.parse.urljoin(base_url, href_match.group(1)))
        if card_url is None or not _is_product_card_path(card_url):
            unsafe_exact = True
            continue
        thumbnail_match = re.search(
            r"<img[^>]+data-src=[\"']([^\"']+)[\"']",
            chunk,
            flags=re.IGNORECASE,
        )
        thumbnail_url = (
            _trusted_https_url(urllib.parse.urljoin(base_url, thumbnail_match.group(1)))
            if thumbnail_match
            else None
        )
        id_match = re.search(r"\bdata-id=[\"'](\d+)[\"']", chunk[:1200], re.IGNORECASE)
        candidates[card_url] = _SearchCandidate(
            card_url=card_url,
            thumbnail_url=thumbnail_url,
            product_id=id_match.group(1) if id_match else None,
        )
    return list(candidates.values()), unsafe_exact


def _parse_product_card(
    body: str,
    *,
    article: str,
    candidate: _SearchCandidate,
    base_url: str,
) -> ProductMediaResolution:
    canonical_raw = _tag_attribute(body, tag="link", selector=("rel", "canonical"), attr="href")
    canonical_url = (
        _trusted_https_url(urllib.parse.urljoin(base_url, canonical_raw)) if canonical_raw else None
    )
    if canonical_url is None or not _is_product_card_path(canonical_url):
        return ProductMediaResolution(
            article=article,
            status="unsafe_url",
            product_id=candidate.product_id,
        )
    articles = re.findall(
        r"product-info-sticker--article[\s\S]{0,700}?"
        r"product-info-sticker__value[^>]*>\s*([^<]+)",
        body,
        flags=re.IGNORECASE,
    )
    expected = _normalize_article(article)
    if expected not in {_normalize_article(value) for value in articles}:
        return ProductMediaResolution(
            article=article,
            status="article_mismatch",
            product_card_url=canonical_url,
            product_id=candidate.product_id,
        )
    gallery_tags = re.findall(r"<a\b[^>]*>", body, flags=re.IGNORECASE)
    unsafe_gallery = False
    original_url: str | None = None
    for tag in gallery_tags:
        if _attribute(tag, "data-fancybox") != "card-galleryFull":
            continue
        raw_url = _attribute(tag, "href")
        if not raw_url:
            continue
        resolved = _trusted_https_url(urllib.parse.urljoin(base_url, raw_url))
        if resolved is None:
            unsafe_gallery = True
            continue
        if not _is_webp_asset_url(resolved):
            continue
        original_url = resolved
        break
    if original_url is None:
        return ProductMediaResolution(
            article=article,
            status="unsafe_url" if unsafe_gallery else "photo_missing",
            product_card_url=canonical_url,
            product_id=candidate.product_id,
        )
    return ProductMediaResolution(
        article=article,
        status="found",
        product_card_url=canonical_url,
        photo_original_url=original_url,
        photo_thumbnail_url=candidate.thumbnail_url or original_url,
        product_id=candidate.product_id,
    )


def _tag_attribute(
    body: str,
    *,
    tag: str,
    selector: tuple[str, str],
    attr: str,
) -> str | None:
    for raw_tag in re.findall(rf"<{tag}\b[^>]*>", body, flags=re.IGNORECASE):
        if _attribute(raw_tag, selector[0]).casefold() == selector[1].casefold():
            return _attribute(raw_tag, attr) or None
    return None


def _attribute(tag: str, name: str) -> str:
    match = re.search(
        rf"\b{re.escape(name)}\s*=\s*([\"'])(.*?)\1",
        tag,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return html_module.unescape(match.group(2)).strip() if match else ""


def _normalize_article(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", html_module.unescape(value))
    return re.sub(r"\s+", " ", normalized).strip().casefold()


def _trusted_https_url(value: str) -> str | None:
    try:
        parsed = urllib.parse.urlsplit(value.strip())
        port = parsed.port
    except (ValueError, AttributeError):
        return None
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() != TRUSTED_CATALOG_HOST
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        return None
    return urllib.parse.urlunsplit(("https", TRUSTED_CATALOG_HOST, parsed.path, parsed.query, ""))


def _is_product_card_path(value: str) -> bool:
    path = urllib.parse.urlsplit(value).path
    return bool(re.fullmatch(r"/catalog/(?:[^/]+/)+\d+/", path))


def _is_webp_asset_url(value: str) -> bool:
    return urllib.parse.urlsplit(value).path.casefold().endswith(".webp")


def _safe_error_detail(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return str(exc.reason)[:200]
    return str(exc)[:200]
