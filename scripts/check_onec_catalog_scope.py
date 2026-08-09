#!/usr/bin/env python3
"""Read-only parity check between the 1C catalog scope and active products."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Collection
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.infrastructure.db.engines import get_application_engine, get_onec_engine  # noqa: E402
from app.models import Product  # noqa: E402
from tasks.sync_onec_product_catalog import (  # noqa: E402
    _clean_str,
    detect_item_folder_value,
    fetch_general_catalog_item_ids,
    fetch_onec_products,
    has_duplicate_marker,
)


def evaluate_catalog_scope(
    source_articles: Collection[str],
    active_articles: Collection[str],
    *,
    baseline_source_count: int,
    max_missing: int,
    max_outside: int,
    max_drop_percent: float,
) -> dict[str, Any]:
    source = set(source_articles)
    active = set(active_articles)
    missing = sorted(source - active)
    outside = sorted(active - source)
    minimum_source_count = int(baseline_source_count * (1 - max_drop_percent / 100))
    checks = {
        "source_count_ok": len(source) >= minimum_source_count,
        "missing_count_ok": len(missing) <= max_missing,
        "outside_count_ok": len(outside) <= max_outside,
    }
    return {
        "status": "ok" if all(checks.values()) else "failed",
        "onec_catalog_scope": len(source),
        "pricing_products_active": len(active),
        "source_missing_in_active": len(missing),
        "active_outside_source": len(outside),
        "missing_examples": missing[:10],
        "outside_examples": outside[:10],
        "checks": checks,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-source-count", type=int, default=28719)
    parser.add_argument("--max-missing", type=int, default=2)
    parser.add_argument("--max-outside", type=int, default=0)
    parser.add_argument("--max-drop-percent", type=float, default=0.5)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    onec_engine = get_onec_engine()
    app_engine = get_application_engine()
    folder_value = detect_item_folder_value(onec_engine)
    allowed_ids = fetch_general_catalog_item_ids(onec_engine, folder_value)
    rows = fetch_onec_products(onec_engine, folder_value, sorted(allowed_ids))
    source_articles = {
        article
        for row in rows
        if (article := _clean_str(row.get("article")))
        and (name := _clean_str(row.get("name")))
        and not has_duplicate_marker(name)
    }
    with Session(app_engine) as session:
        active_articles = set(
            session.scalars(select(Product.article).where(Product.is_active.is_(True))).all()
        )
        session.rollback()
    result = evaluate_catalog_scope(
        source_articles,
        active_articles,
        baseline_source_count=args.baseline_source_count,
        max_missing=args.max_missing,
        max_outside=args.max_outside,
        max_drop_percent=args.max_drop_percent,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
