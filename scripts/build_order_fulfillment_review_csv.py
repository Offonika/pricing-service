#!/usr/bin/env python3
"""Build read-only review CSV for site order fulfillment pilot."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_settings  # noqa: E402
from app.infrastructure.db.engines import build_engine  # noqa: E402
from app.services import site_order_fulfillment as fulfillment  # noqa: E402

DEFAULT_OUTPUT_DIR = Path(".local/order-fulfillment-pilot")
DEFAULT_ENV_FILES = (Path(".env"), Path("/etc/mm-management-orchestrator.env"))


def load_env_files(paths: list[Path]) -> dict[str, str]:
    values: dict[str, str] = {}
    for path in paths:
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def resolve_bitrix_webhook_url(env_values: dict[str, str]) -> str | None:
    settings = get_settings()
    return (
        settings.order_fulfillment_bitrix_webhook_url
        or env_values.get("ORDER_FULFILLMENT_BITRIX_WEBHOOK_URL")
        or env_values.get("BITRIX_BOX_WEBHOOK_BASE")
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=500, help="Max cases to include.")
    parser.add_argument("--status", default=None, help="Filter by current derived status.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory for review CSV artifact.",
    )
    parser.add_argument(
        "--env-file",
        action="append",
        type=Path,
        default=[],
        help="Optional .env file. Defaults also include project .env and mm-compensation .env.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = get_settings()
    env_values = load_env_files([*DEFAULT_ENV_FILES, *args.env_file])
    bitrix_webhook_url = resolve_bitrix_webhook_url(env_values)
    bitrix_client = fulfillment.BitrixChatClient(bitrix_webhook_url) if bitrix_webhook_url else None
    onec_engine = (
        build_engine(settings.onec_database_url, pool_pre_ping=True)
        if settings.onec_database_url
        else None
    )
    engine = build_engine(settings.database_url, pool_pre_ping=True)
    with Session(engine) as session:
        rows = fulfillment.build_review_rows(
            session,
            limit=args.limit,
            status=args.status,
            bitrix_client=bitrix_client,
            onec_engine=onec_engine,
            settings=settings,
        )
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = args.output_dir / f"review-{stamp}.csv"
    fulfillment.write_review_csv(path, rows)
    manual_review_count = sum(1 for row in rows if row.action == "manual_review")
    print(
        f"wrote {path} rows={len(rows)} manual_review={manual_review_count} "
        f"bitrix={'yes' if bitrix_client else 'no'} onec={'yes' if onec_engine else 'no'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
