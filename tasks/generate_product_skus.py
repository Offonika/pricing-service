from __future__ import annotations

import argparse
import json
import os
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.services.sku import generate_sku_batch


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate planned SKU for products from DB attributes."
    )
    parser.add_argument("--write", action="store_true", help="Persist planned SKU values to DB.")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Process all products, not only products with missing SKU.",
    )
    parser.add_argument(
        "--product-id",
        action="append",
        dest="product_ids",
        type=int,
        default=None,
        help="Restrict generation to specific product IDs.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL is not set", file=sys.stderr)
        raise SystemExit(1)

    engine = create_engine(db_url)
    with Session(engine) as session:
        result = generate_sku_batch(
            session,
            product_ids=args.product_ids,
            dry_run=not args.write,
            only_missing=not args.all,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
