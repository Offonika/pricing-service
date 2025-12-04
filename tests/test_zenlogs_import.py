from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO

from openpyxl import Workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, CompetitorItem, CompetitorItemSnapshot
from app.services.importers.competitor_catalog import upsert_catalog_records
from app.services.importers.zenlogs_moba import (
    CompetitorCatalogRecord,
    parse_zenlogs_xlsx,
)
from app.workers.zenlogs import run_zenlogs_moba_import


def _make_workbook() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["group", "sku", "name", "price_opt", "link", "stock"])
    ws.append(["Дисплеи", "SKU-1", "Display 1", "120.5", "https://example/item", "True"])
    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def test_parse_zenlogs_xlsx_normalizes_rows():
    content = _make_workbook()
    records = parse_zenlogs_xlsx(
        content,
        competitor="moba",
        scraped_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )

    assert len(records) == 1
    record = records[0]
    assert record.competitor == "moba"
    assert record.external_id == "SKU-1"
    assert record.category == "Дисплеи"
    assert record.price_opt and float(record.price_opt) == 120.5
    assert record.availability is True


def test_upsert_catalog_records_creates_and_updates():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime.now(tz=timezone.utc)

    with Session(engine) as session:
        first_batch = [
            CompetitorCatalogRecord(
                competitor="moba",
                external_id="SKU-1",
                name="Display 1",
                category="Дисплеи",
                price_opt=None,
                price_roz=None,
                availability=True,
                url="https://example/item1",
                scraped_at=now,
            )
        ]
        stats = upsert_catalog_records(session, first_batch)
        session.commit()

        assert stats.items_created == 1
        assert stats.snapshots_created == 1

        second_batch = [
            CompetitorCatalogRecord(
                competitor="moba",
                external_id="SKU-1",
                name="Display 1 updated",
                category="Дисплеи",
                price_opt=None,
                price_roz=None,
                availability=False,
                url="https://example/item1",
                scraped_at=now,
            )
        ]
        stats_second = upsert_catalog_records(session, second_batch)
        session.commit()

        assert stats_second.items_created == 0
        assert stats_second.items_updated == 1
        assert stats_second.snapshots_created == 1

        items = session.query(CompetitorItem).all()
        assert items[0].name == "Display 1 updated"
        snapshots = session.query(CompetitorItemSnapshot).all()
        assert len(snapshots) == 2


def test_run_zenlogs_moba_import(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    class DummySettings:
        competitor_source_mode = "zenno"
        zenlogs_import_enabled = True
        zenlogs_moba_url = "https://zenlogs/moba.xlsx"
        zenlogs_sources = None
        zenlogs_http_timeout_sec = 5.0
        zenlogs_verify_ssl = True

    records = [
        CompetitorCatalogRecord(
            competitor="moba",
            external_id="SKU-1",
            name="Display 1",
            category="Дисплеи",
            price_opt=None,
            price_roz=None,
            availability=True,
            url="https://example/item1",
            scraped_at=datetime.now(tz=timezone.utc),
        )
    ]

    monkeypatch.setattr("app.workers.zenlogs.get_settings", lambda: DummySettings())
    monkeypatch.setattr(
        "app.workers.zenlogs.load_zenlogs_catalog", lambda **kwargs: records
    )

    with Session(engine) as session:
        result = run_zenlogs_moba_import(session)
        session.commit()

        assert result["processed"] == 1
        assert session.query(CompetitorItem).count() == 1


def test_run_zenlogs_moba_import_skips_when_internal(monkeypatch):
    class DummySettings:
        competitor_source_mode = "internal"
        zenlogs_import_enabled = True
        zenlogs_moba_url = None
        zenlogs_sources = None
        zenlogs_http_timeout_sec = 5.0
        zenlogs_verify_ssl = True

    monkeypatch.setattr("app.workers.zenlogs.get_settings", lambda: DummySettings())
    result = run_zenlogs_moba_import()
    assert result["skipped"] is True


def test_run_zenlogs_multi_source(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    class DummySettings:
        competitor_source_mode = "zenno"
        zenlogs_import_enabled = True
        zenlogs_moba_url = None
        zenlogs_sources = "moba:https://zen/moba.xlsx,green:https://zen/green.xlsx"
        zenlogs_http_timeout_sec = 5.0
        zenlogs_verify_ssl = True

    def fake_load(competitor: str, **kwargs):
        return [
            CompetitorCatalogRecord(
                competitor=competitor,
                external_id=f"{competitor}-SKU",
                name=f"{competitor} item",
                category="Дисплеи",
                price_opt=None,
                price_roz=None,
                availability=True,
                url=f"https://example/{competitor}",
                scraped_at=datetime.now(tz=timezone.utc),
            )
        ]

    monkeypatch.setattr("app.workers.zenlogs.get_settings", lambda: DummySettings())
    monkeypatch.setattr("app.workers.zenlogs.load_zenlogs_catalog", fake_load)

    with Session(engine) as session:
        result = run_zenlogs_moba_import(session)
        session.commit()

        assert result["processed"] == 2
        assert len(result["sources"]) == 2
        assert session.query(CompetitorItem).count() == 2

