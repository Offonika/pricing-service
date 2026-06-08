#!/usr/bin/env python3
"""Apply Bitrix stage updates from a reviewed site-order outbox CSV.

Dry-run by default. Use --apply only after checking the generated result CSV.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_settings  # noqa: E402
from app.services import site_order_fulfillment as fulfillment  # noqa: E402

DEFAULT_OUTPUT_DIR = Path(".local/order-fulfillment-pilot")
DEFAULT_ENV_FILES = (Path(".env"), Path("/opt/MM/mm-compensation/.env"))
DEFAULT_TARGET_STAGE = "PICKUP_WAITING"


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
    parser.add_argument("input_csv", type=Path, help="Stage outbox CSV to apply.")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Apply/check only the first N rows from the input CSV.",
    )
    parser.add_argument(
        "--target-stage",
        default=DEFAULT_TARGET_STAGE,
        help=f"Allowed target stage. Default: {DEFAULT_TARGET_STAGE}.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory for apply result CSV artifact.",
    )
    parser.add_argument(
        "--env-file",
        action="append",
        type=Path,
        default=[],
        help="Optional .env file. Defaults also include project .env and mm-compensation .env.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually update Bitrix deal stages. Default is dry-run.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    env_values = load_env_files([*DEFAULT_ENV_FILES, *args.env_file])
    bitrix_webhook_url = resolve_bitrix_webhook_url(env_values)
    if not bitrix_webhook_url:
        raise SystemExit("Bitrix webhook URL is not configured")
    client = fulfillment.BitrixChatClient(bitrix_webhook_url)
    rows = fulfillment.load_stage_outbox_csv(args.input_csv)
    results = fulfillment.apply_stage_outbox_rows(
        rows,
        client=client,
        apply=args.apply,
        limit=args.limit,
        target_stage=args.target_stage,
    )
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = args.output_dir / f"stage-apply-result-{stamp}.csv"
    fulfillment.write_stage_apply_result_csv(path, results)
    result_counts = Counter(row.result for row in results)
    applied_count = sum(1 for row in results if row.applied)
    mode = "apply" if args.apply else "dry-run"
    print(
        f"wrote {path} mode={mode} rows={len(results)} applied={applied_count} "
        f"results={dict(result_counts)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
