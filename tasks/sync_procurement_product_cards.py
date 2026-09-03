from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.config import get_settings  # noqa: E402
from app.infrastructure.db import get_application_session_factory  # noqa: E402
from app.services.procurement_product_cards import sync_product_cards  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Synchronize pricing-service metrics into native Bitrix product properties."
    )
    parser.add_argument("--scope", choices=("displays", "all"), default="displays")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--product-id", help="Exact Bitrix product ID for a one-card pilot.")
    parser.add_argument(
        "--allow-multiple",
        action="store_true",
        help="Permit apply without --product-id; reserved for a separately approved rollout.",
    )
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def run(
    *,
    scope: str,
    limit: int | None,
    product_id: str | None,
    allow_multiple: bool,
    apply: bool,
) -> dict[str, Any]:
    with get_application_session_factory()() as db:
        return sync_product_cards(
            db,
            scope=scope,
            limit=limit,
            product_id=product_id,
            allow_multiple=allow_multiple,
            apply=apply,
            settings=get_settings(),
        )


def main() -> int:
    args = parse_args()
    result = run(
        scope=args.scope,
        limit=args.limit,
        product_id=args.product_id,
        allow_multiple=args.allow_multiple,
        apply=args.apply,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 1 if result["blocked"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
