from datetime import date, datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import (
    Base,
    Competitor,
    CompetitorFtpRecord,
    CompetitorItem,
    CompetitorItemSnapshot,
    CompetitorPrice,
    PhoneModel,
    Product,
    ProductMatch,
    ProductPhoneModel,
)
from app.services.competitor_matching import match_competitor_ftp_records, parse_model_name

UTC = timezone.utc


def setup_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


def test_parse_model_name_handles_non_display_phone_parts():
    cases = [
        (
            "Аккумулятор (АКБ) Google Pixel 4 (G020I-B) Filling Capacity",
            "google",
            "4",
        ),
        (
            "Задняя крышка для iPhone 17 (белый) в сборе со стеклом камеры, MagSafe",
            "apple",
            "iphone 17",
        ),
        (
            "Шлейф/FLC Realme 14 Pro 5G на системный разъём/микрофон, ориг",
            "realme",
            "14 pro 5g",
        ),
        (
            "Дисплей для Xiaomi Poco C65 (2310FPCA4G) в сборе с тачскрином Черный",
            "xiaomi",
            "poco c65",
        ),
        (
            "Рамка дисплея для Xiaomi Redmi Note 12S (23030RAC7Y) Черный",
            "xiaomi",
            "redmi note 12s",
        ),
        (
            "LCD дисплей для Samsung Galaxy J7 2015 SM-J700 в сборе с тачскрином OLED",
            "samsung",
            "j 7",
        ),
        (
            "LCD дисплей для Nokia 6700 Slide 1-я категория",
            "nokia",
            "6700",
        ),
    ]

    for name, expected_brand, expected_model in cases:
        parsed = parse_model_name(name)

        assert parsed.brand == expected_brand
        assert parsed.model == expected_model
        assert parsed.ambiguous is False


def test_match_inserts_price_and_match():
    engine = setup_db()
    with Session(engine) as session:
        product = Product(article="LCD-PMI54", name="Test", brand="Brand")
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
            observed_at=datetime(2025, 11, 30, 0, 0, 0, tzinfo=UTC),
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
            observed_at=datetime.now(UTC),
        )
        session.add(record)
        session.commit()

        result = match_competitor_ftp_records(session, days_back=10)
        assert result["unmatched"] == 1
        assert result["matched"] == 0


def test_match_by_name():
    engine = setup_db()
    with Session(engine) as session:
        product = Product(article="123", name="Дисплей для Apple iPhone 6S в сборе (чёрный)")
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
            observed_at=datetime.now(UTC),
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
        product = Product(
            article="321", name="Дисплей для Samsung Galaxy S23 Ultra + тачскрин (черный)"
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
            name="Дисплей для Galaxy S23 Ultra (OLED) Black",
            price_opt=None,
            price_roz=250,
            link="http://x",
            in_stock=True,
            amount=2,
            observed_at=datetime.now(UTC),
        )
        session.add(record)
        session.commit()

        result = match_competitor_ftp_records(session, days_back=10)
        assert result["matched"] == 1


def test_match_iphone_with_a_code_in_name():
    engine = setup_db()
    with Session(engine) as session:
        product = Product(article="IP11-OR", name="Дисплей для Apple iPhone 11 + тачскрин (черный)")
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
            observed_at=datetime.now(UTC),
        )
        session.add(record)
        session.commit()

        result = match_competitor_ftp_records(session, days_back=10)
        assert result["matched"] == 1
        assert result["unmatched"] == 0


def test_match_without_dlya_prefix():
    engine = setup_db()
    with Session(engine) as session:
        product = Product(
            article="IP4-BLK", name="Дисплей iPhone 4 в сборе с тачскрином (Black 1-я категория IC)"
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
            name="Дисплей iPhone 4 в сборе с тачскрином (Black 1-я категория IC)",
            price_opt=None,
            price_roz=90,
            link="http://x",
            in_stock=True,
            amount=3,
            observed_at=datetime.now(UTC),
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
            article="IP12-12PRO",
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
            observed_at=datetime.now(UTC),
        )
        session.add(record)
        session.commit()

        result = match_competitor_ftp_records(session, days_back=10)
        assert result["matched"] == 1
        assert result["unmatched"] == 0


def test_match_prefers_canonical_phone_model_overlap():
    engine = setup_db()
    with Session(engine) as session:
        product = Product(article="ALT-123", name="Запчасть без явной модели", brand="Apple")
        phone_model = PhoneModel(brand="apple", model_name="iphone 15", variant="pro")
        session.add_all([product, phone_model])
        session.flush()
        session.add(
            ProductPhoneModel(
                product_id=product.id,
                phone_model_id=phone_model.id,
                source="onec",
                raw_value="Apple iPhone 15 Pro",
                confidence=1.0,
            )
        )
        session.commit()

        record = CompetitorFtpRecord(
            raw_row_id=1,
            file_id=1,
            source="moba",
            file_date=date.today(),
            group_name="Дисплеи",
            sku="",
            name="Дисплей для iPhone 15 Pro OLED",
            price_opt=None,
            price_roz=333,
            link="http://x",
            in_stock=True,
            amount=1,
            observed_at=datetime.now(UTC),
        )
        session.add(record)
        session.commit()

        result = match_competitor_ftp_records(session, days_back=10)
        assert result["matched"] == 1
        match = session.query(ProductMatch).one()
        assert match.product_id == product.id
        assert match.phone_model_id == phone_model.id


def test_match_uses_catalog_item_quality_to_pick_correct_display_product():
    engine = setup_db()
    with Session(engine) as session:
        original = Product(
            article="IP11-ORIG",
            name="Дисплей для Apple iPhone 11 в сборе (черный)",
            display_quality_raw="ORIG100",
        )
        copy = Product(
            article="IP11-OPT",
            name="Дисплей для Apple iPhone 11 в сборе (черный)",
            display_quality_raw="Optima",
        )
        session.add_all([original, copy])
        session.flush()

        session.add(
            CompetitorItem(
                competitor="moba",
                external_id="ip11-no-quality",
                name="Дисплей для iPhone 11 в сборе с тачскрином Черный",
                category="Дисплеи",
                attrs_quality="Original",
            )
        )
        session.commit()

        record = CompetitorFtpRecord(
            raw_row_id=1,
            file_id=1,
            source="moba",
            file_date=date.today(),
            group_name="Дисплеи",
            sku="ip11-no-quality",
            name="Дисплей для iPhone 11 в сборе с тачскрином Черный",
            price_opt=None,
            price_roz=199,
            link="http://x",
            in_stock=True,
            amount=2,
            observed_at=datetime.now(UTC),
        )
        session.add(record)
        session.commit()

        result = match_competitor_ftp_records(session, days_back=10)
        assert result["matched"] == 1
        assert result["unmatched"] == 0

        match = session.query(ProductMatch).one()
        assert match.product_id == original.id
        assert match.quality == "Original"


def test_match_uses_screen_quality_grade_when_attrs_quality_missing():
    engine = setup_db()
    with Session(engine) as session:
        original = Product(
            article="A50-ORIG",
            name="Дисплей для Samsung Galaxy A50 в сборе (черный)",
            quality_raw="ORIG100",
        )
        copy = Product(
            article="A50-COPY",
            name="Дисплей для Samsung Galaxy A50 в сборе (черный)",
            quality_raw="Medium",
        )
        session.add_all([original, copy])
        session.flush()

        session.add(
            CompetitorItem(
                competitor="moba",
                external_id="a50-grade-only",
                name="Дисплей для Samsung Galaxy A50 в сборе с тачскрином Черный",
                category="Дисплеи",
                screen_quality_grade="ORIGINAL",
            )
        )
        session.commit()

        record = CompetitorFtpRecord(
            raw_row_id=1,
            file_id=1,
            source="moba",
            file_date=date.today(),
            group_name="Дисплеи",
            sku="a50-grade-only",
            name="Дисплей для Samsung Galaxy A50 в сборе с тачскрином Черный",
            price_opt=None,
            price_roz=149,
            link="http://x",
            in_stock=True,
            amount=2,
            observed_at=datetime.now(UTC),
        )
        session.add(record)
        session.commit()

        result = match_competitor_ftp_records(session, days_back=10)
        assert result["matched"] == 1
        assert result["unmatched"] == 0

        match = session.query(ProductMatch).one()
        assert match.product_id == original.id
        assert match.quality == "Original"


def test_catalog_upsert_uses_newest_record_and_snapshots_are_idempotent():
    engine = setup_db()
    with Session(engine) as session:
        product = Product(article="LCD-IP17", name="Дисплей для Apple iPhone 17")
        session.add(product)
        session.flush()
        newest_file_date = date.today() - timedelta(days=1)
        oldest_file_date = newest_file_date - timedelta(days=1)

        old_record = CompetitorFtpRecord(
            raw_row_id=1,
            file_id=1,
            source="moba",
            file_date=oldest_file_date,
            group_name="Дисплеи",
            sku="LCD-IP17",
            name="Дисплей для iPhone 17",
            price_opt=None,
            price_roz=100,
            link="http://old",
            in_stock=False,
            amount=0,
            observed_at=datetime.combine(oldest_file_date, datetime.min.time(), tzinfo=UTC),
        )
        new_record = CompetitorFtpRecord(
            raw_row_id=2,
            file_id=2,
            source="moba",
            file_date=newest_file_date,
            group_name="Дисплеи",
            sku="LCD-IP17",
            name="Дисплей для iPhone 17",
            price_opt=None,
            price_roz=200,
            link="http://new",
            in_stock=True,
            amount=3,
            observed_at=datetime.combine(newest_file_date, datetime.min.time(), tzinfo=UTC),
        )
        session.add_all([old_record, new_record])
        session.commit()

        result = match_competitor_ftp_records(session, days_back=3)
        assert result["catalog_snapshots"] == 2

        item = (
            session.query(CompetitorItem).filter_by(competitor="moba", external_id="LCD-IP17").one()
        )
        last_seen_date = (
            item.last_seen_at.date()
            if isinstance(item.last_seen_at, datetime)
            else item.last_seen_at
        )
        assert last_seen_date == newest_file_date
        assert item.scraped_at == new_record.observed_at
        assert item.price_roz == 200
        assert item.availability is True

        result = match_competitor_ftp_records(session, days_back=3)
        assert result["catalog_snapshots"] == 0
        assert session.query(CompetitorItemSnapshot).count() == 2
