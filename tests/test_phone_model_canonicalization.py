from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, PhoneModel, PhoneModelAlias, Product
from app.services.device_brands import BrandResolver
from app.services.phone_model_canonicalization import (
    PhoneModelCanonicalizer,
    screen_product_phone_compatibility,
)


def test_brand_resolver_extracts_brands_and_preserves_groups():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        resolver = BrandResolver(session)
        resolver.ensure_seed_brands()

        apple = resolver.resolve("iPhone 12", source="test")
        redmi = resolver.resolve_for_model("Xiaomi", "Redmi Note 12")
        poco = resolver.resolve_for_model("Xiaomi", "POCO X3")
        honor = resolver.resolve("Honor 90", source="test")
        huawei = resolver.resolve("Huawei P40", source="test")

        assert apple is not None
        assert apple.brand.code == "apple"
        assert redmi is not None
        assert redmi.code == "redmi"
        assert redmi.group_code == "xiaomi"
        assert poco is not None
        assert poco.code == "poco"
        assert poco.group_code == "xiaomi"
        assert honor is not None
        assert honor.brand.code == "honor"
        assert honor.brand.group_code == "huawei_honor"
        assert huawei is not None
        assert huawei.brand.code == "huawei"
        assert huawei.brand.group_code == "huawei_honor"


def test_canonicalizer_merges_same_model_from_multiple_sources():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        canonicalizer = PhoneModelCanonicalizer(session)

        onec = canonicalizer.canonicalize(source="onec", raw_value="Samsung Galaxy S23 Ultra")
        news = canonicalizer.canonicalize(
            source="news_agent",
            brand="Samsung",
            model_name="Galaxy S23",
            variant="Ultra",
            confidence=1.0,
        )
        competitor = canonicalizer.canonicalize(
            source="competitor_parser",
            brand="samsung",
            model_name="galaxy s23 ultra",
            confidence=0.95,
        )
        session.commit()

        assert onec.phone_model is not None
        assert news.phone_model is not None
        assert competitor.phone_model is not None
        assert onec.phone_model.id == news.phone_model.id == competitor.phone_model.id
        assert session.query(PhoneModelAlias).count() == 3


def test_canonicalizer_blocks_low_conf_competitor_autocreate():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        canonicalizer = PhoneModelCanonicalizer(session)
        result = canonicalizer.canonicalize(
            source="competitor_parser",
            brand="apple",
            model_name="iphone 16",
            confidence=0.5,
        )
        assert result.phone_model is None
        assert result.reason == "creation_not_allowed"


def test_canonicalizer_allows_high_conf_competitor_autocreate():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        canonicalizer = PhoneModelCanonicalizer(session)
        result = canonicalizer.canonicalize(
            source="competitor_parser",
            brand="apple",
            model_name="iphone 16",
            confidence=0.95,
        )
        session.commit()
        assert result.phone_model is not None
        assert result.created_new is True


def test_canonicalizer_blocks_generic_competitor_brand():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        canonicalizer = PhoneModelCanonicalizer(session)
        result = canonicalizer.canonicalize(
            source="competitor_parser",
            brand="generic",
            model_name="usb кабель hoco x4",
            confidence=0.95,
        )
        assert result.phone_model is None
        assert result.reason == "blocked_generic_brand"


def test_canonicalizer_blocks_competitor_accessory_noise():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        canonicalizer = PhoneModelCanonicalizer(session)
        result = canonicalizer.canonicalize(
            source="competitor_parser",
            brand="samsung",
            model_name="автомобильное зарядное устройство",
            confidence=0.95,
        )
        assert result.phone_model is None
        assert result.reason == "blocked_accessory_noise"


def test_canonicalizer_blocks_competitor_multi_family_model():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        canonicalizer = PhoneModelCanonicalizer(session)
        result = canonicalizer.canonicalize(
            source="competitor_parser",
            brand="infinix",
            model_name="infinix note tecno camon 40 pro 4g",
            confidence=0.95,
        )
        assert result.phone_model is None
        assert result.reason == "blocked_multi_family_model"


def test_canonicalizer_blocks_competitor_part_number_noise() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        canonicalizer = PhoneModelCanonicalizer(session)
        result = canonicalizer.canonicalize(
            source="competitor_parser",
            brand="samsung",
            model_name="GH98123456A",
            confidence=0.95,
        )
        assert result.phone_model is None
        assert result.reason == "blocked_part_number_noise"


def test_canonicalizer_repairs_generic_competitor_brand_from_raw_value() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        canonicalizer = PhoneModelCanonicalizer(session)
        result = canonicalizer.canonicalize(
            source="competitor_parser",
            raw_value="generic bq 5022 bond",
            brand="generic",
            model_name="bq 5022 bond",
            confidence=0.95,
        )
        session.commit()

        assert result.phone_model is not None
        assert result.reason == "created_new_model"
        assert result.phone_model.brand == "bq"
        assert result.phone_model.model_name == "5022 bond"


def test_canonicalizer_repairs_competitor_brand_from_model_prefix() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        canonicalizer = PhoneModelCanonicalizer(session)
        result = canonicalizer.canonicalize(
            source="competitor_parser",
            raw_value="itel oukitel wp53s",
            brand="itel",
            model_name="oukitel wp53s",
            confidence=0.95,
        )
        session.commit()

        assert result.phone_model is not None
        assert result.reason == "created_new_model"
        assert result.phone_model.brand == "oukitel"
        assert result.phone_model.model_name == "wp53s"


def test_canonicalizer_allows_competitor_blackview_code_model() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        canonicalizer = PhoneModelCanonicalizer(session)
        result = canonicalizer.canonicalize(
            source="competitor_parser",
            brand="blackview",
            model_name="blackview bv5800 pro",
            confidence=0.95,
        )
        session.commit()

        assert result.phone_model is not None
        assert result.reason == "created_new_model"
        assert result.phone_model.brand == "blackview"
        assert result.phone_model.model_name == "bv5800"
        assert result.phone_model.variant == "pro"


def test_canonicalizer_strips_onec_apple_sim_esim_qualifiers() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        canonicalizer = PhoneModelCanonicalizer(session)

        base = canonicalizer.canonicalize(
            source="onec",
            raw_value="Apple iPhone 17 Pro",
        )
        esim = canonicalizer.canonicalize(
            source="onec",
            raw_value="Apple iPhone 17 Pro (SIM + eSIM)",
        )
        session.commit()

        assert base.phone_model is not None
        assert esim.phone_model is not None
        assert base.phone_model.id == esim.phone_model.id


def test_canonicalizer_normalizes_samsung_competitor_to_onec_code_model() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        canonicalizer = PhoneModelCanonicalizer(session)

        onec = canonicalizer.canonicalize(
            source="onec",
            brand="samsung",
            model_name="s918 galaxy s23",
            variant="ultra",
            confidence=1.0,
        )
        competitor = canonicalizer.canonicalize(
            source="competitor_parser",
            raw_value="Дисплей для Samsung Galaxy S23 Ultra (S918B) модуль с рамкой Черный - OR",
            brand="samsung",
            model_name="s 23 ultra",
            variant="s918b",
            confidence=0.95,
        )
        session.commit()

        assert onec.phone_model is not None
        assert competitor.phone_model is not None
        assert competitor.phone_model.id == onec.phone_model.id
        assert competitor.phone_model.model_name == "s918 galaxy s23"
        assert competitor.phone_model.variant == "ultra"


def test_canonicalizer_normalizes_samsung_s20_5g_competitor_to_onec_code_model() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        canonicalizer = PhoneModelCanonicalizer(session)

        onec = canonicalizer.canonicalize(
            source="onec",
            brand="samsung",
            model_name="g981 galaxy s20 5g",
            confidence=1.0,
        )
        competitor = canonicalizer.canonicalize(
            source="competitor_parser",
            raw_value="Дисплей для Samsung Galaxy S20 5G (G981B) модуль с рамкой Черный - OR",
            brand="samsung",
            model_name="s 20 5g",
            variant="g981b",
            confidence=0.95,
        )
        session.commit()

        assert onec.phone_model is not None
        assert competitor.phone_model is not None
        assert competitor.phone_model.id == onec.phone_model.id
        assert competitor.phone_model.model_name == "g981 galaxy s20 5g"
        assert competitor.phone_model.variant is None


def test_canonicalizer_family_matches_unique_existing_competitor_model() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(
            PhoneModel(
                brand="xiaomi",
                model_name="poco x7 pro (2412dpc0ag)",
                variant=None,
            )
        )
        session.commit()

        canonicalizer = PhoneModelCanonicalizer(session)
        result = canonicalizer.canonicalize(
            source="competitor_parser",
            raw_value="LCD дисплей для Xiaomi Poco X7 Pro 5G с тачскрином (черный) 100% OR SP",
            brand="xiaomi",
            model_name="poco x7 pro",
            confidence=0.78,
        )
        session.commit()

        assert result.phone_model is not None
        assert result.reason == "family_model_match"
        assert result.phone_model.model_name == "poco x7 pro (2412dpc0ag)"


def test_canonicalizer_family_match_prefers_exact_family_row() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        exact_family = PhoneModel(
            brand="xiaomi",
            model_name="13",
            variant=None,
        )
        coded_family = PhoneModel(
            brand="xiaomi",
            model_name="13 (2211133g)",
            variant=None,
        )
        session.add_all([exact_family, coded_family])
        session.commit()

        canonicalizer = PhoneModelCanonicalizer(session)
        result = canonicalizer.canonicalize(
            source="competitor_parser",
            raw_value="Дисплей для Xiaomi 13 (2211133C) в сборе с тачскрином Черный - OR",
            brand="xiaomi",
            model_name="13",
            variant="2211133C",
            confidence=0.78,
        )
        session.commit()

        assert result.phone_model is not None
        assert result.reason == "family_model_match"
        assert result.phone_model.id == exact_family.id


def test_canonicalizer_skips_family_match_for_slash_joined_competitor_name() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(
            PhoneModel(
                brand="xiaomi",
                model_name="mi 11 lite",
                variant="m2101k9g",
            )
        )
        session.commit()

        canonicalizer = PhoneModelCanonicalizer(session)
        result = canonicalizer.canonicalize(
            source="competitor_parser",
            raw_value="LCD дисплей для Xiaomi 11 Lite 5G NE/Mi 11 Lite 4G/5G с тачскрином (черный) 100% OR",
            brand="xiaomi",
            model_name="mi 11 lite",
            confidence=0.78,
        )

        assert result.phone_model is None
        assert result.reason == "creation_not_allowed"


def test_canonicalizer_uses_samsung_code_override_for_s23_plus() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        canonicalizer = PhoneModelCanonicalizer(session)

        onec = canonicalizer.canonicalize(
            source="onec",
            brand="samsung",
            model_name="s916 galaxy s23+",
            confidence=1.0,
        )
        competitor = canonicalizer.canonicalize(
            source="competitor_parser",
            raw_value="Дисплей для Samsung Galaxy S23 (S916B) модуль с рамкой Черный - OR",
            brand="samsung",
            model_name="s 23",
            variant="s916b",
            confidence=0.95,
        )
        session.commit()

        assert onec.phone_model is not None
        assert competitor.phone_model is not None
        assert competitor.phone_model.id == onec.phone_model.id
        assert competitor.phone_model.model_name == "s916 galaxy s23+"
        assert competitor.phone_model.variant is None


def test_canonicalizer_uses_xiaomi_code_override() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        canonicalizer = PhoneModelCanonicalizer(session)

        onec = canonicalizer.canonicalize(
            source="onec",
            brand="xiaomi",
            model_name="12 pro (2201122g)",
            confidence=1.0,
        )
        competitor = canonicalizer.canonicalize(
            source="competitor_parser",
            raw_value="Дисплей для Xiaomi 12 Pro (2201122G) модуль с рамкой Черный - OR",
            brand="xiaomi",
            model_name="12 pro",
            variant="pro",
            confidence=0.95,
        )
        session.commit()

        assert onec.phone_model is not None
        assert competitor.phone_model is not None
        assert competitor.phone_model.id == onec.phone_model.id
        assert competitor.phone_model.model_name == "12 pro (2201122g)"
        assert competitor.phone_model.variant is None


def test_canonicalizer_maps_huawei_honor_code_to_huawei_onec_model() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        canonicalizer = PhoneModelCanonicalizer(session)

        onec = canonicalizer.canonicalize(
            source="onec",
            brand="huawei",
            model_name="honor 200 lite (lly nx1)",
            confidence=1.0,
        )
        competitor = canonicalizer.canonicalize(
            source="competitor_parser",
            raw_value="Дисплей для Huawei Honor 200 Lite (LLY-NX1) модуль с рамкой Черный - OLED",
            brand="honor",
            model_name="200 lite",
            variant="lly nx1",
            confidence=0.95,
        )
        session.commit()

        assert onec.phone_model is not None
        assert competitor.phone_model is not None
        assert competitor.phone_model.id == onec.phone_model.id
        assert competitor.phone_model.brand == "huawei"
        assert competitor.phone_model.model_name == "honor 200 lite (lly nx1)"


def test_canonicalizer_does_not_pick_single_huawei_override_for_multi_code_item() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        canonicalizer = PhoneModelCanonicalizer(session)

        canonicalizer.canonicalize(
            source="onec",
            brand="huawei",
            model_name="honor 200 lite (lly nx1)",
            confidence=1.0,
        )
        canonicalizer.canonicalize(
            source="onec",
            brand="huawei",
            model_name="honor x8b (lly lx1)",
            confidence=1.0,
        )
        competitor = canonicalizer.canonicalize(
            source="competitor_parser",
            raw_value="LCD дисплей для Huawei Honor X8b/200 Lite (LLY-LX1/LLY-NX1) с тачскрином OLED",
            brand="honor",
            model_name="honor x8b lite",
            variant="lly lx1/lly nx1",
            confidence=0.5,
        )
        session.commit()

        assert competitor.phone_model is None
        assert competitor.reason == "creation_not_allowed"


def test_canonicalizer_maps_apple_iphone_competitor_hardware_code_to_marketing_variant() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        canonicalizer = PhoneModelCanonicalizer(session)

        onec = canonicalizer.canonicalize(
            source="onec",
            raw_value="Apple iPhone 17 Pro Max (SIM + eSIM)",
        )
        competitor = canonicalizer.canonicalize(
            source="competitor_parser",
            brand="apple",
            model_name="iphone 17 pro max",
            variant="A3526/OR100",
            confidence=0.95,
        )
        session.commit()

        assert onec.phone_model is not None
        assert competitor.phone_model is not None
        assert onec.phone_model.id == competitor.phone_model.id
        assert competitor.phone_model.model_name == "iphone 17"
        assert competitor.phone_model.variant == "pro max"


def test_canonicalizer_maps_apple_iphone_16e_competitor_to_same_model() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        canonicalizer = PhoneModelCanonicalizer(session)

        onec = canonicalizer.canonicalize(
            source="onec",
            raw_value="Apple iPhone 16e",
        )
        competitor = canonicalizer.canonicalize(
            source="competitor_parser",
            brand="apple",
            model_name="iphone 16e",
            variant="A3408/A3409/A3410/A3212",
            confidence=0.95,
        )
        session.commit()

        assert onec.phone_model is not None
        assert competitor.phone_model is not None
        assert onec.phone_model.id == competitor.phone_model.id
        assert competitor.phone_model.model_name == "iphone 16e"
        assert competitor.phone_model.variant is None


def test_canonicalizer_blocks_onec_non_target_device_type():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        canonicalizer = PhoneModelCanonicalizer(session)
        result = canonicalizer.canonicalize(
            source="onec",
            raw_value="Acer Aspire 7741",
        )
        assert result.phone_model is None
        assert result.reason == "blocked_non_target_device_type"


def test_canonicalizer_blocks_onec_low_confidence_autocreate():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        canonicalizer = PhoneModelCanonicalizer(session)
        result = canonicalizer.canonicalize(
            source="onec",
            brand="samsung",
            model_name="galaxy s24",
            confidence=0.2,
        )
        assert result.phone_model is None
        assert result.reason == "creation_not_allowed"


def test_canonicalizer_allows_onec_doogee_series_model() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        canonicalizer = PhoneModelCanonicalizer(session)
        result = canonicalizer.canonicalize(
            source="onec",
            raw_value="Doogee S200 Ultra",
        )
        session.commit()

        assert result.phone_model is not None
        assert result.reason == "created_new_model"
        assert result.phone_model.brand == "doogee"
        assert result.phone_model.model_name == "s200"
        assert result.phone_model.variant == "ultra"


def test_canonicalizer_allows_onec_nothing_brand() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        canonicalizer = PhoneModelCanonicalizer(session)
        result = canonicalizer.canonicalize(
            source="onec",
            raw_value="Nothing Phone (1)",
        )
        session.commit()

        assert result.phone_model is not None
        assert result.reason == "created_new_model"
        assert result.phone_model.brand == "nothing"
        assert result.phone_model.model_name == "phone (1)"


def test_canonicalizer_allows_onec_itel_brand() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        canonicalizer = PhoneModelCanonicalizer(session)
        result = canonicalizer.canonicalize(
            source="onec",
            raw_value="Itel P65 (P671L)",
        )
        session.commit()

        assert result.phone_model is not None
        assert result.reason == "created_new_model"
        assert result.phone_model.brand == "itel"
        assert result.phone_model.model_name == "p65 (p671l)"


def test_canonicalizer_normalizes_infinx_brand_to_infinix() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        canonicalizer = PhoneModelCanonicalizer(session)
        result = canonicalizer.canonicalize(
            source="onec",
            raw_value="Infinx Hot 40i (X6528B)",
        )
        session.commit()

        assert result.phone_model is not None
        assert result.reason == "created_new_model"
        assert result.phone_model.brand == "infinix"
        assert result.phone_model.model_name == "hot 40i (x6528b)"


def test_screen_product_phone_compatibility_keeps_phone_accessory() -> None:
    product = Product(
        article="p-1",
        name="Защитное стекло для Samsung G998 Galaxy S21 Ultra",
        subject_1c="защитное стекло",
        vid_nomenklatury_1c="Аксессуары (розничные товары)",
    )

    result = screen_product_phone_compatibility(
        product,
        "Samsung G998 Galaxy S21 Ultra",
        source="onec",
    )

    assert result.eligible_for_phone_canonicalization is True
    assert result.filter_reason is None


def test_screen_product_phone_compatibility_filters_watch_text() -> None:
    product = Product(
        article="p-2",
        name="Защитное стекло для Apple Watch 8",
        subject_1c="защитное стекло",
        vid_nomenklatury_1c="Аксессуары (розничные товары)",
    )

    result = screen_product_phone_compatibility(
        product,
        "Apple Watch S8 (41 мм)",
        source="onec",
    )

    assert result.eligible_for_phone_canonicalization is False
    assert result.filter_reason == "non_phone_text"


def test_screen_product_phone_compatibility_filters_magicwatch_text() -> None:
    product = Product(
        article="p-3",
        name="Дисплей для Huawei Honor MagicWatch 2",
        subject_1c="дисплей",
        vid_nomenklatury_1c="Дисплеи/сенсор/стекло",
    )

    result = screen_product_phone_compatibility(
        product,
        "Huawei Honor MagicWatch 2 (MNS-B39) (46 мм)",
        source="onec",
    )

    assert result.eligible_for_phone_canonicalization is False
    assert result.filter_reason == "non_phone_text"


def test_screen_product_phone_compatibility_filters_amazfit_text() -> None:
    product = Product(
        article="p-4",
        name="Аккумулятор для Amazfit GTR Mini (A2174)",
        subject_1c="аккумулятор",
        vid_nomenklatury_1c="Питание и зарядка/аккумуляторы/зарядные устройства",
    )

    result = screen_product_phone_compatibility(
        product,
        "Amazfit GTR Mini (A2174)",
        source="onec",
    )

    assert result.eligible_for_phone_canonicalization is False
    assert result.filter_reason == "non_phone_text"


def test_screen_product_phone_compatibility_filters_garmin_text() -> None:
    product = Product(
        article="p-5",
        name="Дисплей для Garmin Venu 2 Plus + тачскрин",
        subject_1c="дисплей",
        vid_nomenklatury_1c="Дисплеи/сенсор/стекло",
    )

    result = screen_product_phone_compatibility(
        product,
        "Garmin Venu 2 Plus",
        source="onec",
    )

    assert result.eligible_for_phone_canonicalization is False
    assert result.filter_reason == "non_phone_text"


def test_screen_product_phone_compatibility_filters_router_text() -> None:
    product = Product(
        article="p-6",
        name="Аккумулятор для Wi-Fi роутера МТС 8920 / MegaFon MR150-6 / Beeline S23",
        subject_1c="аккумулятор",
        vid_nomenklatury_1c="Питание и зарядка (розница + сервис)",
    )

    result = screen_product_phone_compatibility(
        product,
        "Beeline S23",
        source="onec",
    )

    assert result.eligible_for_phone_canonicalization is False
    assert result.filter_reason == "non_phone_text"


def test_screen_product_phone_compatibility_filters_aspire_text() -> None:
    product = Product(
        article="p-7",
        name="Аккумулятор для ноутбука Acer Aspire 4551 / Aspire 4741",
        subject_1c="аккумулятор",
        vid_nomenklatury_1c="Питание и зарядка (розница + сервис)",
    )

    result = screen_product_phone_compatibility(
        product,
        "Acer Aspire 7741",
        source="onec",
    )

    assert result.eligible_for_phone_canonicalization is False
    assert result.filter_reason == "non_phone_text"
