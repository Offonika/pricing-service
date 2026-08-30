#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from urllib.parse import urlsplit, urlunsplit

from app.core.config import get_settings
from app.services.expertise_bitrix import BitrixRestClient

PLACEMENT = "CRM_DYNAMIC_1134_DETAIL_TAB"
TITLE = "Переписка с клиентом"


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


def _placements(api: BitrixRestClient) -> list[dict[str, object]]:
    payload = api.call_json("placement.get", {})
    result = payload.get("result")
    if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
        raise RuntimeError("site_service_requests_placement_readback_invalid")
    return result


def ensure(*, apply: bool) -> dict[str, object]:
    settings = get_settings()
    webhook = str(settings.site_service_requests_bitrix_webhook_url or "").strip()
    handler = str(settings.site_service_requests_ui_handler_url or "").strip()
    if not webhook or not handler.startswith("https://"):
        raise RuntimeError("site_service_requests_ui_placement_not_configured")
    api = BitrixRestClient(webhook)
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
    args = parser.parse_args()
    print(json.dumps(ensure(apply=args.apply), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
