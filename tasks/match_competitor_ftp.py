"""CLI для матчинга FTP-цен конкурентов с товарами TopControl."""

import argparse
import json
import logging
import os
import sys
from datetime import date, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.services.competitor_matching import LlmParseClient, match_competitor_ftp_records


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Match competitor FTP records to products.")
    parser.add_argument(
        "--days-back",
        type=int,
        default=3,
        help="Look back this many days by file_date (default: 3)",
    )
    parser.add_argument("--source", action="append", help="Filter by source (can be repeated)")
    parser.add_argument("--name-contains", help="Filter records by substring in name (ILIKE)")
    parser.add_argument("--limit", type=int, help="Limit number of records to process")
    parser.add_argument(
        "--llm", action="store_true", help="Use LOCAL_LLM_* for low-conf/ambiguous records"
    )
    parser.add_argument(
        "--llm-limit", type=int, default=0, help="Max LLM calls (default: 0 = disabled)"
    )
    parser.add_argument(
        "--llm-threshold", type=float, default=0.7, help="Call LLM if confidence below this value"
    )
    parser.add_argument(
        "--catalog-only-new",
        action="store_true",
        help="Only insert new competitor items; do not update existing catalog rows",
    )
    parser.add_argument(
        "--skip-display-attrs",
        action="store_true",
        help="Skip auto LLM attribute extraction for display items after matching",
    )
    parser.add_argument(
        "--disable-category-llm",
        action="store_true",
        help="Disable LLM category classification for competitor catalog updates",
    )
    args = parser.parse_args()

    llm_client = None
    if args.llm or args.llm_limit:
        base_url = os.environ.get("LOCAL_LLM_BASE_URL")
        model = os.environ.get("LOCAL_LLM_CHAT_MODEL")
        if not base_url or not model:
            raise RuntimeError(
                "LOCAL_LLM_BASE_URL and LOCAL_LLM_CHAT_MODEL must be set to use --llm"
            )
        llm_client = LlmParseClient(base_url, model)

    settings = get_settings()
    engine = create_engine(settings.database_url)
    with Session(engine) as session:
        result = match_competitor_ftp_records(
            session,
            days_back=args.days_back,
            sources=args.source,
            name_contains=args.name_contains,
            limit=args.limit,
            llm_client=llm_client,
            llm_limit=args.llm_limit,
            llm_threshold=args.llm_threshold,
            catalog_only_new=args.catalog_only_new,
            category_llm_enabled=not args.disable_category_llm,
        )
        if not args.skip_display_attrs:
            try:
                from tasks.extract_competitor_attrs import extract_attrs

                display_stats = extract_attrs(
                    session,
                    source=None,
                    category="дисплей",
                    name_contains=None,
                    first_seen_date=None,
                    first_seen_after=date.today() - timedelta(days=args.days_back),
                    only_null=True,
                    only_bad=False,
                    only_parse_version_missing=False,
                    overwrite=False,
                    rerun_errors=False,
                    limit=None,
                    offset=None,
                    min_llm_confidence=settings.matching_min_llm_confidence,
                    min_confidence_bump=0.1,
                    repair_attempts=1,
                    llm_timeout=30.0,
                    dry_run=False,
                    parse_version="v1",
                    sample_limit=10,
                    samples_file=None,
                )
                result["display_attrs"] = display_stats
            except RuntimeError as exc:
                result["display_attrs_error"] = str(exc)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    exit_code = 0 if not result.get("errors") else 1
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
