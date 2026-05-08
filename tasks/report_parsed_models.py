"""CLI-отчёт по распаршенным моделям в competitor_ftp_record."""

import argparse
import json
from collections import Counter
from datetime import date, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import CompetitorFtpRecord


def _bucket(conf: float | None) -> str:
    if conf is None:
        return "none"
    value = float(conf)
    if value >= 0.9:
        return "0.90+"
    if value >= 0.8:
        return "0.80-0.89"
    if value >= 0.7:
        return "0.70-0.79"
    return "<0.70"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Report parsed phone models from competitor_ftp_record."
    )
    parser.add_argument("--source", help="filter by competitor source", default=None)
    parser.add_argument("--brand", help="filter by parsed_device_brand (ilike)", default=None)
    parser.add_argument(
        "--days-back",
        type=int,
        default=7,
        help="look back this many days by file_date (default: 7)",
    )
    parser.add_argument(
        "--limit-samples", type=int, default=20, help="how many sample rows to include"
    )
    args = parser.parse_args()

    settings = get_settings()
    engine = create_engine(settings.database_url)
    since_date = date.today() - timedelta(days=args.days_back) if args.days_back else None

    with Session(engine) as session:
        query = session.query(CompetitorFtpRecord)
        if since_date:
            query = query.filter(CompetitorFtpRecord.file_date >= since_date)
        if args.source:
            query = query.filter(CompetitorFtpRecord.source == args.source)
        if args.brand:
            query = query.filter(CompetitorFtpRecord.parsed_device_brand.ilike(args.brand))

        records = query.all()
        total = len(records)

        bucket_counts: Counter[str] = Counter()
        with_parsed_brand = 0
        with_parsed_model = 0
        ambiguous = 0
        low_conf_samples = []
        unparsed_samples = []

        for rec in records:
            bucket_counts[_bucket(rec.parse_confidence)] += 1
            if rec.parsed_device_brand:
                with_parsed_brand += 1
            if rec.parsed_device_model:
                with_parsed_model += 1
            if rec.parse_notes and "ambiguous" in rec.parse_notes:
                ambiguous += 1

        sorted_by_conf = sorted(
            records,
            key=lambda r: (float(r.parse_confidence) if r.parse_confidence is not None else -1.0),
        )
        for rec in sorted_by_conf:
            if len(low_conf_samples) >= args.limit_samples:
                break
            low_conf_samples.append(
                {
                    "source": rec.source,
                    "sku": rec.sku,
                    "name": rec.name,
                    "parsed_device_brand": rec.parsed_device_brand,
                    "parsed_device_model": rec.parsed_device_model,
                    "parsed_device_variant": rec.parsed_device_variant,
                    "parse_confidence": (
                        float(rec.parse_confidence) if rec.parse_confidence is not None else None
                    ),
                    "parse_notes": rec.parse_notes,
                }
            )

        for rec in records:
            if len(unparsed_samples) >= args.limit_samples:
                break
            if not rec.parsed_device_brand or not rec.parsed_device_model:
                unparsed_samples.append(
                    {
                        "source": rec.source,
                        "sku": rec.sku,
                        "name": rec.name,
                        "parse_confidence": (
                            float(rec.parse_confidence)
                            if rec.parse_confidence is not None
                            else None
                        ),
                        "parse_notes": rec.parse_notes,
                    }
                )

    report = {
        "filters": {
            "source": args.source,
            "brand": args.brand,
            "since_date": since_date.isoformat() if since_date else None,
            "limit_samples": args.limit_samples,
        },
        "total": total,
        "with_parsed_device_brand": with_parsed_brand,
        "with_parsed_device_model": with_parsed_model,
        "ambiguous": ambiguous,
        "confidence_buckets": dict(bucket_counts),
        "low_conf_samples": low_conf_samples,
        "unparsed_samples": unparsed_samples,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
