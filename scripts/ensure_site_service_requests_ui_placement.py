#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

from app.core.config import get_settings
from app.services.expertise_bitrix import BitrixRestClient

PLACEMENT = "CRM_DYNAMIC_1134_DETAIL_TAB"
TITLE = "Переписка с клиентом"
APP_ACCESS_TOKEN_ENV = "SITE_SERVICE_REQUESTS_UI_BITRIX_APP_ACCESS_TOKEN"


class _JsonApi(Protocol):
    def call_json(self, method: str, payload: dict[str, object]) -> dict[str, object]: ...


class _ApplicationApi:
    def __init__(self, *, portal_domain: str, access_token: str):
        self._client = BitrixRestClient(f"https://{portal_domain}/rest")
        self._access_token = access_token

    def call_json(self, method: str, payload: dict[str, object]) -> dict[str, object]:
        if "auth" in payload:
            raise RuntimeError("site_service_requests_ui_application_payload_invalid")
        return self._client.call_json(method, {**payload, "auth": self._access_token})


def _normalized_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme.lower() != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("site_service_requests_ui_handler_invalid")
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path.rstrip("/") + "/",
            "",
            "",
        )
    )


def _portal_domain(webhook_url: str) -> str:
    parsed = urlsplit(webhook_url.strip())
    try:
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise RuntimeError("site_service_requests_ui_portal_invalid") from exc
    if (
        parsed.scheme.lower() != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("site_service_requests_ui_portal_invalid")
    try:
        hostname.encode("ascii")
    except UnicodeEncodeError as exc:
        raise RuntimeError("site_service_requests_ui_portal_invalid") from exc
    return hostname.lower()


def _application_api(*, webhook_url: str, access_token: str) -> _JsonApi:
    if not access_token or access_token != access_token.strip():
        raise RuntimeError("site_service_requests_ui_application_context_not_configured")
    return _ApplicationApi(
        portal_domain=_portal_domain(webhook_url),
        access_token=access_token,
    )


def _placements(api: _JsonApi) -> list[dict[str, object]]:
    payload = api.call_json("placement.get", {})
    result = payload.get("result")
    if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
        raise RuntimeError("site_service_requests_placement_readback_invalid")
    return result


def ensure(*, apply: bool, api: _JsonApi, handler: str) -> dict[str, object]:
    handler = handler.strip()
    if not handler:
        raise RuntimeError("site_service_requests_ui_placement_not_configured")
    normalized_handler = _normalized_url(handler)

    def matches(item: dict[str, object]) -> bool:
        placement = item.get("placement") or item.get("PLACEMENT")
        raw_handler = item.get("handler") or item.get("HANDLER")
        return (
            placement == PLACEMENT
            and isinstance(raw_handler, str)
            and _normalized_url(raw_handler) == normalized_handler
        )

    before = _placements(api)
    placement_rows = [
        item for item in before if (item.get("placement") or item.get("PLACEMENT")) == PLACEMENT
    ]
    matching_rows = [item for item in placement_rows if matches(item)]
    if len(matching_rows) > 1 or (placement_rows and not matching_rows):
        raise RuntimeError("site_service_requests_ui_placement_conflict")
    already_bound = len(matching_rows) == 1
    if apply and not already_bound:
        api.call_json(
            "placement.bind",
            {
                "PLACEMENT": PLACEMENT,
                "HANDLER": handler,
                "TITLE": TITLE,
                "DESCRIPTION": "Диалог с клиентом по обращению сайта",
                "LANG_ALL": {
                    "ru": {"TITLE": TITLE, "DESCRIPTION": "Переписка по обращению сайта"},
                    "en": {"TITLE": "Customer conversation", "DESCRIPTION": "Site request chat"},
                },
            },
        )
    after = _placements(api) if apply else before
    after_matches = [item for item in after if matches(item)]
    bound = len(after_matches) == 1
    if apply and not bound:
        raise RuntimeError("site_service_requests_placement_readback_failed")
    return {
        "placement": PLACEMENT,
        "handler": handler,
        "alreadyBound": already_bound,
        "bound": bound,
        "applied": apply,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Ensure the #3223 CRM chat placement.")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--handler",
        help="Public HTTPS handler. Defaults to SITE_SERVICE_REQUESTS_UI_HANDLER_URL.",
    )
    args = parser.parse_args()
    settings = get_settings()
    webhook = str(settings.site_service_requests_bitrix_webhook_url or "").strip()
    handler = args.handler or str(settings.site_service_requests_ui_handler_url or "")
    api = _application_api(
        webhook_url=webhook,
        access_token=os.environ.get(APP_ACCESS_TOKEN_ENV, ""),
    )
    print(
        json.dumps(
            ensure(apply=args.apply, api=api, handler=handler),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
