from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime

from app.core.config import get_settings
from app.infrastructure.db import get_application_session_factory
from app.services.customer_settlements import normalize_site_user_id, set_pilot_access


def _user_hash(site_user_id: str, salt: str | None) -> str:
    if not salt:
        raise SystemExit("CUSTOMER_SETTLEMENTS_CORRELATION_SALT is not configured")
    return hashlib.sha256(f"{salt}:{site_user_id}".encode()).hexdigest()[:16]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Safely preview or update the customer-settlement pilot whitelist."
    )
    parser.add_argument("--site-user-id", required=True)
    state = parser.add_mutually_exclusive_group(required=True)
    state.add_argument("--enable", action="store_true")
    state.add_argument("--disable", action="store_true")
    parser.add_argument("--reason")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    settings = get_settings()
    site_user_id = normalize_site_user_id(args.site_user_id)
    summary = {
        "audit_at": datetime.now(UTC).isoformat(),
        "mode": "apply" if args.apply else "dry-run",
        "user_hash": _user_hash(site_user_id, settings.customer_settlements_correlation_salt),
        "enabled": bool(args.enable),
        "reason_present": bool(args.reason),
    }
    if not args.apply:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 0

    session = get_application_session_factory()()
    try:
        item, created = set_pilot_access(
            session,
            site_user_id=site_user_id,
            enabled=bool(args.enable),
            reason=args.reason,
        )
        session.commit()
        session.refresh(item)
        summary.update(
            {
                "created": created,
                "readback_enabled": bool(item.enabled),
                "readback_ok": bool(item.enabled) == bool(args.enable),
            }
        )
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 0 if summary["readback_ok"] else 1
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
