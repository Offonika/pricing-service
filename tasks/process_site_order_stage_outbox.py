from __future__ import annotations

import argparse
import json

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.infrastructure.db import get_application_engine
from app.services.site_order_fulfillment import BitrixChatClient
from app.services.site_order_stage_outbox import process_stage_outbox


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Process transactional site-order stage outbox (dry-run by default)."
    )
    parser.add_argument(
        "--apply", action="store_true", help="Update Bitrix when feature flag permits."
    )
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    settings = get_settings()
    if not settings.order_fulfillment_bitrix_webhook_url:
        raise SystemExit("ORDER_FULFILLMENT_BITRIX_WEBHOOK_URL is not configured")
    client = BitrixChatClient(settings.order_fulfillment_bitrix_webhook_url)
    with Session(get_application_engine()) as session:
        results = process_stage_outbox(
            session,
            client=client,
            apply=args.apply,
            limit=args.limit,
            settings=settings,
        )
    print(
        json.dumps(
            {
                "mode": "apply" if args.apply else "dry-run",
                "count": len(results),
                "results": [result.to_dict() for result in results],
            },
            ensure_ascii=False,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
