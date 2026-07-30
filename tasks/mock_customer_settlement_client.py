from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request

from app.core.config import get_settings
from app.services.customer_settlement_auth import (
    create_customer_settlement_assertion,
)

ENDPOINT_PATH = "/api/customer/settlements/summary"


def _response_status(raw_body: bytes) -> str | None:
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("status")
    return str(value) if value is not None else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build or send a server-side customer-settlement contract probe."
    )
    parser.add_argument("--site-user-id", required=True)
    parser.add_argument("--base-url")
    parser.add_argument("--send", action="store_true")
    args = parser.parse_args(argv)
    if args.send and not args.base_url:
        parser.error("--base-url is required with --send")

    settings = get_settings()
    assertion, expires_at = create_customer_settlement_assertion(
        site_user_id=args.site_user_id,
        settings=settings,
    )
    result: dict[str, object] = {
        "mode": "send" if args.send else "dry-run",
        "method": "GET",
        "path": ENDPOINT_PATH,
        "assertion_expires_at": expires_at,
        "assertion_exposed": False,
    }
    if not args.send:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0

    url = f"{str(args.base_url).rstrip('/')}{ENDPOINT_PATH}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {assertion}",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            raw_body = response.read()
            result["http_status"] = int(response.status)
    except urllib.error.HTTPError as exc:
        raw_body = exc.read()
        result["http_status"] = int(exc.code)
    except (urllib.error.URLError, TimeoutError):
        result["http_status"] = 0
        result["response_status"] = "transport_error"
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 1
    result["response_status"] = _response_status(raw_body)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["http_status"] == 200 else 1


if __name__ == "__main__":
    raise SystemExit(main())
