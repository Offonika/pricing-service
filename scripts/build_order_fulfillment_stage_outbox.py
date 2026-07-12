#!/usr/bin/env python3
"""Build dry-run Bitrix stage outbox CSV for site order fulfillment.

The script never updates CRM. It prepares server-side decisions for review and
blocks rows whose target stage is not present in the Bitrix deal pipeline yet.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
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
        "--target-stage",
        action="append",
        default=[],
        help="Only include this target stage. Can be passed multiple times.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory for outbox CSV artifact.",
    )
    parser.add_argument(
        "--env-file",
        action="append",
        type=Path,
        default=[],
        help="Optional .env file. Defaults include project and workspace orchestrator env.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = get_settings()
    env_values = load_env_files([*DEFAULT_ENV_FILES, *args.env_file])
    bitrix_webhook_url = resolve_bitrix_webhook_url(env_values)
    bitrix_client = (
        fulfillment.BitrixChatClient(bitrix_webhook_url) if bitrix_webhook_url else None
    )
    onec_engine = (
        build_engine(settings.onec_database_url, pool_pre_ping=True)
        if settings.onec_database_url
        else None
    )
    available_stage_ids = bitrix_client.list_deal_stage_ids() if bitrix_client else None
    engine = build_engine(settings.database_url, pool_pre_ping=True)
    with Session(engine) as session:
        review_rows = fulfillment.build_review_rows(
            session,
            limit=args.limit,
            status=args.status,
            bitrix_client=bitrix_client,
            onec_engine=onec_engine,
            settings=settings,
        )
    allowed_target_stages = set(args.target_stage) if args.target_stage else None
    outbox_rows = fulfillment.build_stage_outbox_rows(
        review_rows,
        available_stage_ids=available_stage_ids,
        allowed_target_stages=allowed_target_stages,
    )
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = args.output_dir / f"stage-outbox-{stamp}.csv"
    fulfillment.write_stage_outbox_csv(path, outbox_rows)
    state_counts = Counter(row.state for row in outbox_rows)
    print(
        f"wrote {path} rows={len(outbox_rows)} states={dict(state_counts)} "
        f"bitrix={'yes' if bitrix_client else 'no'} onec={'yes' if onec_engine else 'no'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
