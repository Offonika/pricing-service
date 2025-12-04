from datetime import date, datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import (
    Base,
    Competitor,
    CompetitorFtpRecord,
    CompetitorPrice,
    Product,
    ProductMatch,
)
from app.services.competitor_matching import match_competitor_ftp_records


def setup_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


def test_match_inserts_price_and_match():
    engine = setup_db()
    with Session(engine) as session:
        product = Product(sku="LCD-PMI54", name="Test", brand="Brand")
        session.add(product)
        session.commit()

        record = CompetitorFtpRecord(
            raw_row_id=1,
            file_id=1,
            source="moba",
            file_date=date.today(),
            group_name="grp",
            sku="lcd-pmi54",
            name="Name",
            price_opt=None,
            price_roz=100,
            link="http://x",
            in_stock=True,
            amount=5,
            observed_at=datetime(2025, 11, 30, 0, 0, 0, tzinfo=timezone.utc),
        )
        session.add(record)
        session.commit()

        result = match_competitor_ftp_records(session, days_back=10)
        assert result["matched"] == 1
        assert result["prices_created"] == 1
        assert result["matches_created"] == 1

        cp = session.query(CompetitorPrice).one()
        assert cp.price == 100
        assert cp.in_stock is True

        pm = session.query(ProductMatch).one()
        assert pm.competitor_sku == "lcd-pmi54"

        comp = session.query(Competitor).one()
        assert comp.name == "moba"


def test_unmatched_counts():
    engine = setup_db()
    with Session(engine) as session:
        # No products, so record will be unmatched
        record = CompetitorFtpRecord(
            raw_row_id=1,
            file_id=1,
            source="moba",
            file_date=date.today(),
            group_name="grp",
            sku="UNKNOWN",
            name="Name",
            price_opt=10,
            price_roz=None,
            link="http://x",
            in_stock=True,
            amount=0,
            observed_at=datetime.now(timezone.utc),
        )
        session.add(record)
        session.commit()

        result = match_competitor_ftp_records(session, days_back=10)
        assert result["unmatched"] == 1
        assert result["matched"] == 0


def test_match_by_name():
    engine = setup_db()
    with Session(engine) as session:
        product = Product(sku="123", name="Дисплей для Apple iPhone 6S в сборе (чёрный)")
        session.add(product)
        session.commit()

        record = CompetitorFtpRecord(
            raw_row_id=1,
            file_id=1,
            source="moba",
            file_date=date.today(),
            group_name="Дисплеи",
            sku="",
            name="Дисплей для iPhone 6S в сборе с тачскрином Черный - Оптима",
            price_opt=None,
            price_roz=150,
            link="http://x",
            in_stock=True,
            amount=5,
            observed_at=datetime.now(timezone.utc),
        )
        session.add(record)
        session.commit()

        result = match_competitor_ftp_records(session, days_back=10)
        assert result["matched"] == 1
        assert result["prices_created"] == 1
        assert result["matches_created"] == 1


def test_match_by_name_with_variant():
    engine = setup_db()
    with Session(engine) as session:
        product = Product(sku="321", name="Дисплей для Samsung Galaxy S23 Ultra + тачскрин (черный)")
        session.add(product)
        session.commit()

        record = CompetitorFtpRecord(
            raw_row_id=1,
            file_id=1,
            source="moba",
            file_date=date.today(),
            group_name="Дисплеи",
            sku="",
            name="Дисплей для Galaxy S23 Ultra (OLED) Black",
            price_opt=None,
            price_roz=250,
            link="http://x",
            in_stock=True,
            amount=2,
            observed_at=datetime.now(timezone.utc),
        )
        session.add(record)
        session.commit()

        result = match_competitor_ftp_records(session, days_back=10)
        assert result["matched"] == 1


def test_match_iphone_with_a_code_in_name():
    engine = setup_db()
    with Session(engine) as session:
        product = Product(sku="IP11-OR", name="Дисплей для Apple iPhone 11 + тачскрин (черный)")
        session.add(product)
        session.commit()

        record = CompetitorFtpRecord(
            raw_row_id=1,
            file_id=1,
            source="moba",
            file_date=date.today(),
            group_name="Дисплеи",
            sku="",
            name="Дисплей для iPhone 11 (A2221) в сборе с тачскрином Черный - OR",
            price_opt=None,
            price_roz=120,
            link="http://x",
            in_stock=True,
            amount=3,
            observed_at=datetime.now(timezone.utc),
        )
        session.add(record)
        session.commit()

        result = match_competitor_ftp_records(session, days_back=10)
        assert result["matched"] == 1
        assert result["unmatched"] == 0


def test_match_without_dlya_prefix():
    engine = setup_db()
    with Session(engine) as session:
        product = Product(sku="IP4-BLK", name="Дисплей iPhone 4 в сборе с тачскрином (Black 1-я категория IC)")
        session.add(product)
        session.commit()

        record = CompetitorFtpRecord(
            raw_row_id=1,
            file_id=1,
            source="moba",
            file_date=date.today(),
            group_name="Дисплеи",
            sku="",
            name="Дисплей iPhone 4 в сборе с тачскрином (Black 1-я категория IC)",
            price_opt=None,
            price_roz=90,
            link="http://x",
            in_stock=True,
            amount=3,
            observed_at=datetime.now(timezone.utc),
        )
        session.add(record)
        session.commit()

        result = match_competitor_ftp_records(session, days_back=10)
        assert result["matched"] == 1
        assert result["unmatched"] == 0


def test_match_product_with_multiple_models_in_name():
    engine = setup_db()
    with Session(engine) as session:
        product = Product(
            sku="IP12-12PRO",
            name="Дисплей для Apple iPhone 12 / iPhone 12 Pro + тачскрин (черный)",
        )
        session.add(product)
        session.commit()

        record = CompetitorFtpRecord(
            raw_row_id=1,
            file_id=1,
            source="moba",
            file_date=date.today(),
            group_name="Дисплеи",
            sku="",
            name="Дисплей для Apple iPhone 12 Pro + тачскрин (черный) (GX ORIG) (Hard Oled)",
            price_opt=None,
            price_roz=200,
            link="http://x",
            in_stock=True,
            amount=2,
            observed_at=datetime.now(timezone.utc),
        )
        session.add(record)
        session.commit()

        result = match_competitor_ftp_records(session, days_back=10)
        assert result["matched"] == 1
        assert result["unmatched"] == 0


def test_disambiguate_by_quality():
    engine = setup_db()
    with Session(engine) as session:
        product1 = Product(sku="IP11-OR", name="Дисплей для Apple iPhone 11 + тачскрин (черный)", quality="orig")
        product2 = Product(sku="IP11-COPY", name="Дисплей для Apple iPhone 11 + тачскрин (черный)", quality="copy")
        session.add_all([product1, product2])
        session.commit()

        record = CompetitorFtpRecord(
            raw_row_id=1,
            file_id=1,
            source="moba",
            file_date=date.today(),
            group_name="Дисплеи",
            sku="",
            name="Дисплей для iPhone 11 (A2221) в сборе с тачскрином Черный - ORIG100",
            price_opt=None,
            price_roz=200,
            link="http://x",
            in_stock=True,
            amount=3,
            observed_at=datetime.now(timezone.utc),
        )
        session.add(record)
        session.commit()

        result = match_competitor_ftp_records(session, days_back=10)
        assert result["matched"] == 1
        pm = session.query(ProductMatch).one()
        assert pm.product_id == product1.id
