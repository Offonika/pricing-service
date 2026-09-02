from __future__ import annotations

import argparse
import json
from typing import Any

from app.core.config import get_settings
from app.infrastructure.db import get_application_session_factory
from app.services.procurement_product_cards import sync_product_cards


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Synchronize pricing-service metrics into native Bitrix product properties."
    )
    parser.add_argument("--scope", choices=("displays", "all"), default="displays")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def run(*, scope: str, limit: int | None, apply: bool) -> dict[str, Any]:
    with get_application_session_factory()() as db:
        return sync_product_cards(
            db,
            scope=scope,
            limit=limit,
            apply=apply,
            settings=get_settings(),
        )


def main() -> int:
    args = parse_args()
    result = run(scope=args.scope, limit=args.limit, apply=args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 1 if result["blocked"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
