from __future__ import annotations

import logging
import time
from typing import Any, Optional

import httpx

from app.core.config import get_settings

logger = logging.getLogger("app.scraper.captcha")


class CaptchaSolveError(Exception):
    pass


def _poll_result(api_key: str, task_id: str, base_url: str = "https://2captcha.com") -> str:
    params = {"key": api_key, "action": "get", "id": task_id, "json": 1}
    for _ in range(24):  # ~60s max (2.5s * 24)
        time.sleep(2.5)
        resp = httpx.get(f"{base_url}/res.php", params=params, timeout=15)
        data: Any = resp.json()
        if data.get("status") == 1:
            return data["request"]
        if data.get("request") not in {"CAPCHA_NOT_READY", "ERROR_NO_SLOT_AVAILABLE"}:
            raise CaptchaSolveError(f"captcha error: {data}")
    raise CaptchaSolveError("captcha timeout waiting for solution")


def solve_recaptcha_v2(site_key: str, page_url: str, api_key: Optional[str] = None) -> str:
    """
    Solve reCAPTCHA v2 via 2captcha.
    Returns the g-recaptcha-response token.
    """
    settings = get_settings()
    key = api_key or settings.captcha_api_key
    if not key:
        raise CaptchaSolveError("captcha api key is not set")
    payload = {
        "key": key,
        "method": "userrecaptcha",
        "googlekey": site_key,
        "pageurl": page_url,
        "json": 1,
    }
    resp = httpx.post("https://2captcha.com/in.php", data=payload, timeout=15)
    data: Any = resp.json()
    if data.get("status") != 1:
        raise CaptchaSolveError(f"captcha submit error: {data}")
    task_id = data["request"]
    logger.info("captcha submitted", extra={"task_id": task_id, "page": page_url})
    return _poll_result(key, task_id)


def solve_hcaptcha(site_key: str, page_url: str, api_key: Optional[str] = None) -> str:
    """
    Solve hCaptcha via 2captcha.
    Returns the token.
    """
    settings = get_settings()
    key = api_key or settings.captcha_api_key
    if not key:
        raise CaptchaSolveError("captcha api key is not set")
    payload = {
        "key": key,
        "method": "hcaptcha",
        "sitekey": site_key,
        "pageurl": page_url,
        "json": 1,
    }
    resp = httpx.post("https://2captcha.com/in.php", data=payload, timeout=15)
    data: Any = resp.json()
    if data.get("status") != 1:
        raise CaptchaSolveError(f"captcha submit error: {data}")
    task_id = data["request"]
    logger.info("hcaptcha submitted", extra={"task_id": task_id, "page": page_url})
    return _poll_result(key, task_id)


def solve_yandex_smartcaptcha(site_key: str, page_url: str, api_key: Optional[str] = None) -> str:
    """
    Solve Yandex SmartCaptcha via 2captcha (method=yandexsmart).
    """
    settings = get_settings()
    key = api_key or settings.captcha_api_key
    if not key:
        raise CaptchaSolveError("captcha api key is not set")
    payload = {
        "key": key,
        "method": "yandexsmart",
        "sitekey": site_key,
        "pageurl": page_url,
        "json": 1,
    }
    resp = httpx.post("https://2captcha.com/in.php", data=payload, timeout=15)
    data: Any = resp.json()
    if data.get("status") != 1:
        raise CaptchaSolveError(f"captcha submit error: {data}")
    task_id = data["request"]
    logger.info("yandex smartcaptcha submitted", extra={"task_id": task_id, "page": page_url})
    return _poll_result(key, task_id)
