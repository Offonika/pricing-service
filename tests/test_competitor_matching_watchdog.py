from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

from app.models import CompetitorFtpFile, CompetitorItem, Product, ProductLiveCandidateCache
from tasks.competitor_matching_watchdog import build_ftp_freshness_report, build_report


def test_ftp_freshness_report_marks_stale_source(db_session) -> None:
    old = datetime.now(UTC) - timedelta(days=4)
    db_session.add(
        CompetitorFtpFile(
            source="moba",
            filename="moba-old.csv",
            file_path="/tmp/moba-old.csv",
            file_date=date.today() - timedelta(days=4),
            ingested_at=old,
        )
    )
    db_session.commit()

    report = build_ftp_freshness_report(db_session, max_lag_days=1)

    assert report["ok"] is False
    assert report["checks"] == {"ftp": "bad"}
    assert report["ftp"]["moba"]["status"] == "stale"


def test_watchdog_uses_latest_activity_and_successful_run_for_freshness(
    db_session, tmp_path
) -> None:
    now = datetime.now(UTC)
    old = now - timedelta(days=4)
    latest_report = tmp_path / "latest.json"
    latest_report.write_text(
        json.dumps(
            {
                "status": "success",
                "finished_at": now.isoformat(),
                "embeddings_enabled": "0",
                "embedding_status": "disabled",
            }
        ),
        encoding="utf-8",
    )

    product = Product(article="watchdog-1", name="Watchdog test product")
    db_session.add(product)
    db_session.flush()
    db_session.add_all(
        [
            CompetitorFtpFile(
                source="moba",
                filename="moba.csv",
                file_path="/tmp/moba.csv",
                file_date=date.today(),
                ingested_at=now,
            ),
            CompetitorItem(
                competitor="moba",
                external_id="SKU-1",
                name="Freshly updated competitor item",
                availability=True,
                scraped_at=old,
                updated_at=now,
                last_seen_at=old,
            ),
            ProductLiveCandidateCache(
                product_id=product.id,
                live_candidate_count=1,
                computed_at=now,
            ),
        ]
    )
    db_session.commit()

    report = build_report(
        db_session,
        embeddings_dir=tmp_path / "embeddings",
        latest_report=latest_report,
        embeddings_enabled=True,
        max_ftp_lag_days=1,
        max_runtime_lag_hours=30,
    )

    assert report["ok"] is True
    assert report["checks"]["competitor_items"] == "fresh"
    assert report["checks"]["matches"] == "fresh"
    assert report["checks"]["embeddings"] == "disabled"
