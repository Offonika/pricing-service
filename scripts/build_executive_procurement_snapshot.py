#!/usr/bin/env python3
"""Build an atomic read-only snapshot of all open executive procurement orders."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.ensure_procurement_bitrix_process import DEFAULT_ENV_FILE, load_env  # noqa: E402
from scripts.sync_open_cargo_supplier_orders_to_bitrix import (  # noqa: E402
    clean,
    fetch_open_supplier_orders,
    json_default,
)

DEFAULT_OUTPUT = Path(
    "/var/lib/mm-data-contracts/procurement/procurement_open_orders_snapshot.json"
)
ALLOWED_CONTOURS = frozenset({"cargo", "ved_import"})
MOSCOW_TZ = ZoneInfo("Europe/Moscow")
MAX_LIMIT = 5000


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--limit", type=int, default=MAX_LIMIT)
    parser.add_argument("--as-of", type=date.fromisoformat)
    return parser.parse_args(argv)


def validate_orders(orders: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
    if len(orders) >= limit:
        raise RuntimeError(
            f"procurement snapshot may be truncated: fetched {len(orders)} rows at limit {limit}"
        )

    normalized_refs: set[str] = set()
    validated: list[dict[str, Any]] = []
    for index, raw_order in enumerate(orders, start=1):
        if not isinstance(raw_order, dict):
            raise ValueError(f"procurement order {index} is not an object")
        order = dict(raw_order)
        onec_ref = clean(order.get("onec_ref"))
        if not onec_ref:
            raise ValueError(f"procurement order {index} has empty onec_ref")
        normalized_ref = onec_ref.casefold()
        if normalized_ref in normalized_refs:
            raise ValueError(f"duplicate procurement onec_ref: {onec_ref}")
        normalized_refs.add(normalized_ref)

        contour = clean(order.get("procurement_contour_key"))
        if contour not in ALLOWED_CONTOURS:
            raise ValueError(f"unsupported procurement contour {contour!r} for order {onec_ref}")
        validated.append(order)

    return sorted(validated, key=lambda item: clean(item.get("onec_ref")).casefold())


def build_payload(
    orders: list[dict[str, Any]],
    *,
    limit: int,
    as_of: date,
    generated_at: datetime,
) -> dict[str, Any]:
    validated_orders = validate_orders(orders, limit=limit)
    return {
        "schema_version": 1,
        "generated_at": generated_at.astimezone(UTC).isoformat(),
        "as_of": as_of.isoformat(),
        "source_status": "ready",
        "contours": sorted(ALLOWED_CONTOURS),
        "order_count": len(validated_orders),
        "orders": validated_orders,
    }


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2, default=json_default)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def build_snapshot(
    onec_database_url: str,
    *,
    output: Path,
    limit: int = MAX_LIMIT,
    as_of: date | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    effective_as_of = as_of or datetime.now(MOSCOW_TZ).date()
    effective_generated_at = generated_at or datetime.now(UTC)
    orders = fetch_open_supplier_orders(
        onec_database_url,
        limit=limit,
        date_from="",
        date_to="",
        contours=set(ALLOWED_CONTOURS),
        blank_contour_cargo_dropoff_only=False,
        filter_contours_in_sql=True,
        fail_on_query_limit=True,
    )
    payload = build_payload(
        orders,
        limit=limit,
        as_of=effective_as_of,
        generated_at=effective_generated_at,
    )
    atomic_write_json(output, payload)
    return payload


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    env = load_env(args.env_file)
    onec_database_url = clean(env.get("ONEC_DATABASE_URL"))
    if not onec_database_url:
        raise SystemExit(f"ONEC_DATABASE_URL is not configured in {args.env_file}")

    payload = build_snapshot(
        onec_database_url,
        output=args.output,
        limit=args.limit,
        as_of=args.as_of,
    )
    print(
        json.dumps(
            {
                "status": "ok",
                "output": str(args.output),
                "as_of": payload["as_of"],
                "order_count": payload["order_count"],
                "contours": payload["contours"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
