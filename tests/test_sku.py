from __future__ import annotations

from app.models import PhoneModel, Product, ProductPhoneModel, ProductSkuPlan
from app.services.sku import (
    apply_sku_generation_result,
    build_sku,
    generate_sku_batch,
    generate_sku_for_product,
    sync_product_sku_status,
    validate_sku,
)


def test_build_sku_and_validate() -> None:
    sku = build_sku("f5", "dsp", "iph11pm", "oled-blk", "aaa")
    assert sku == "F5-DSP-IPH11PM-OLED-BLK-AAA"
    assert validate_sku("f5-dsp-iph11pm-oled-blk") == "F5-DSP-IPH11PM-OLED-BLK"


def test_validate_sku_rejects_invalid_value() -> None:
    try:
        validate_sku("F5-ДSP-IPH11")
    except Exception as exc:
        assert "invalid" in str(exc) or "empty" in str(exc)
    else:
        raise AssertionError("validate_sku must reject cyrillic values")


def test_generate_display_sku(db_session) -> None:
    product = Product(
        article="1001",
        name="Дисплей для Apple iPhone 11 Pro Max",
        brand="Apple",
        manufacturer="F5ENERGY",
        category="Дисплеи",
        display_type="OLED",
        display_quality="Copy High",
        color="Black",
    )
    phone_model = PhoneModel(brand="apple", model_name="iphone 11", variant="pro max")
    db_session.add_all([product, phone_model])
    db_session.flush()
    db_session.add(
        ProductPhoneModel(
            product_id=product.id,
            phone_model_id=phone_model.id,
            source="onec",
            raw_value="Apple iPhone 11 Pro Max",
        )
    )
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.planned_sku == "F5-DSP-IPH11PM-OLD-BLK-CPH"


def test_generate_display_sku_from_subject(db_session) -> None:
    product = Product(
        article="1001-subject",
        name="Дисплей для Samsung J510 Galaxy J5 (2016) + тачскрин (черный) (OLED)",
        manufacturer="F5ENERGY",
        subject="Дисплей",
        display_type="OLED",
        color="Black",
        quality="Copy High",
    )
    phone_model = PhoneModel(brand="samsung", model_name="j5 2016", variant=None)
    db_session.add_all([product, phone_model])
    db_session.flush()
    db_session.add(
        ProductPhoneModel(product_id=product.id, phone_model_id=phone_model.id, source="onec")
    )
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.category_code == "DSP"
    assert result.planned_sku == "F5-DSP-SMG-J510-OLD-BLK-CPH"


def test_generate_touchscreen_only_display_sku_has_touch_marker(db_session) -> None:
    product = Product(
        article="1001-touch-only",
        name="Тачскрин для Apple iPad Air 2 (A1566/A1567) (черный)",
        subject="Тачскрин",
        category="Дисплеи",
        color="черный",
        quality="Medium",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.category_code == "DSP"
    assert result.key_code == "BLK-CPM-TCH"
    assert result.planned_sku == "OEM-DSP-IPDA2-BLK-CPM-TCH"


def test_generate_display_sku_uses_series_as_rev(db_session) -> None:
    product = Product(
        article="1001-jk",
        name="Дисплей для Apple iPhone 11 + тачскрин (черный) (JK) (In-Cell)",
        manufacturer="JK",
        subject="Дисплей",
        color="черный",
        quality_raw="Optima",
    )
    phone_model = PhoneModel(brand="apple", model_name="iphone 11", variant=None)
    db_session.add_all([product, phone_model])
    db_session.flush()
    db_session.add(
        ProductPhoneModel(product_id=product.id, phone_model_id=phone_model.id, source="onec")
    )
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.key_code == "INL-BLK-CPM"
    assert result.rev == "JK"
    assert result.planned_sku == "OEM-DSP-IPH11-INL-BLK-CPM-JK"


def test_generate_display_sku_detects_refurbished_from_name(db_session) -> None:
    product = Product(
        article="1001-ref",
        name="Дисплей для Apple iPhone 11 + тачскрин (черный) (ORIG) (Переклейка)",
        manufacturer="Apple Co",
        subject="Дисплей",
        color="черный",
        quality_raw="ORIG",
    )
    phone_model = PhoneModel(brand="apple", model_name="iphone 11", variant=None)
    db_session.add_all([product, phone_model])
    db_session.flush()
    db_session.add(
        ProductPhoneModel(product_id=product.id, phone_model_id=phone_model.id, source="onec")
    )
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.key_code == "BLK-RF"
    assert result.rev is None
    assert result.planned_sku == "OEM-DSP-IPH11-BLK-RF"


def test_generate_display_sku_detects_hard_oled_and_series(db_session) -> None:
    product = Product(
        article="1001-hard",
        name="Дисплей для Apple iPhone 11 Pro Max + тачскрин (черный) (GX ORIG) (Hard Oled)",
        manufacturer="GX ORIG",
        subject="Дисплей",
        color="черный",
        quality_raw="Medium",
    )
    phone_model = PhoneModel(brand="apple", model_name="iphone 11", variant="pro max")
    db_session.add_all([product, phone_model])
    db_session.flush()
    db_session.add(
        ProductPhoneModel(product_id=product.id, phone_model_id=phone_model.id, source="onec")
    )
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.key_code == "HLD-BLK-OR"
    assert result.rev == "GX"
    assert result.planned_sku == "OEM-DSP-IPH11PM-HLD-BLK-OR-GX"


def test_generate_display_sku_detects_orig100_and_als_rev(db_session) -> None:
    product = Product(
        article="1001-or1",
        name="Дисплей для Apple iPhone 13 Pro + тачскрин + ALS шлейф (черный) (ORIG100) (Снятый)",
        manufacturer="Apple Co",
        subject="Дисплей",
        color="черный",
        display_type="OLED",
        quality_raw="ORIG100",
    )
    phone_model = PhoneModel(brand="apple", model_name="iphone 13", variant="pro")
    db_session.add_all([product, phone_model])
    db_session.flush()
    db_session.add(
        ProductPhoneModel(product_id=product.id, phone_model_id=phone_model.id, source="onec")
    )
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.key_code == "OLD-BLK-OR1"
    assert result.rev == "ALS"
    assert result.planned_sku == "OEM-DSP-IPH13P-OLD-BLK-OR1-ALS"


def test_generate_display_sku_detects_dismantled_grade(db_session) -> None:
    product = Product(
        article="1001-pul",
        name="Дисплей для Apple iPhone 11 (в сборе с тачскрином) (черный) (биток) (ORIG)",
        subject="Дисплей",
        color="черный",
        quality_raw="Биток",
    )
    phone_model = PhoneModel(brand="apple", model_name="iphone 11", variant=None)
    db_session.add_all([product, phone_model])
    db_session.flush()
    db_session.add(
        ProductPhoneModel(product_id=product.id, phone_model_id=phone_model.id, source="onec")
    )
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.key_code == "BLK-BTK"
    assert result.planned_sku == "OEM-DSP-IPH11-BLK-BTK"


def test_generate_f5_display_keeps_soft_oled_service_variants(db_session) -> None:
    existing = Product(
        article="079824",
        name=(
            "Дисплей для Apple iPhone 14 Plus + тачскрин (черный) "
            "(F5ENERGY) (Ultra Soft Oled) (площадка под IC)"
        ),
        manufacturer="F5ENERGY",
        subject="Дисплей",
        color="черный",
    )
    db_session.add(existing)
    db_session.flush()
    db_session.add(
        ProductSkuPlan(
            product_id=existing.id,
            planned_sku="F5-DSP-IPH14PL-SLD-BLK",
            status="generated",
            is_active=True,
        )
    )
    products = [
        Product(
            article="079825",
            name=(
                "Дисплей для Apple iPhone 14 Plus + тачскрин (черный) "
                "(F5ENERGY) (Ultra Soft Oled) (с верификацией)"
            ),
            manufacturer="F5ENERGY",
            subject="Дисплей",
            color="черный",
        ),
        Product(
            article="081705",
            name=(
                "Дисплей для Apple iPhone 14 Plus + тачскрин (черный) "
                "(F5ENERGY) (Soft Oled) (с верификацией) (REGULAR)"
            ),
            manufacturer="F5ENERGY",
            subject="Дисплей",
            color="черный",
        ),
        Product(
            article="081706",
            name=(
                "Дисплей для Apple iPhone 14 Plus + тачскрин (черный) "
                "(F5ENERGY) (Soft Oled) (площадка под IC) (REGULAR)"
            ),
            manufacturer="F5ENERGY",
            subject="Дисплей",
            color="черный",
        ),
    ]
    db_session.add_all(products)
    db_session.commit()

    results = {
        product.article: generate_sku_for_product(db_session, product) for product in products
    }

    assert results["079825"].planned_sku == "F5-DSP-IPH14PL-SLD-BLK-USO-VER"
    assert results["081705"].planned_sku == "F5-DSP-IPH14PL-SLD-BLK-REG-VER"
    assert results["081706"].planned_sku == "F5-DSP-IPH14PL-SLD-BLK-REG-ICP"


def test_generate_display_sku_uses_multi_model_count_in_rev(db_session) -> None:
    product = Product(
        article="1001-multi",
        name="Дисплей для Apple iPhone 5s / iPhone SE (в сборе с тачскрином) (черный) (переклейка) (ORIG)",
        subject="Дисплей",
        color="черный",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.rev == "2"
    assert result.planned_sku == "OEM-DSP-IPH5-BLK-RF-2"


def test_generate_apple_ipad_display_uses_compact_device_code(db_session) -> None:
    product = Product(
        article="1001-apple-ipad-pro105",
        name="Дисплей для Apple iPad Pro 10.5 (2017) (A1701/A1709) + тачскрин (черный) (Medium)",
        subject="дисплей",
        color="черный",
        quality_raw="Medium",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "IPDP105"
    assert result.planned_sku == "OEM-DSP-IPDP105-BLK-CPM"


def test_generate_touchscreen_keeps_ipad_generation_and_bundle_modifiers(
    db_session,
) -> None:
    products = [
        Product(
            article="057827",
            name="Тачскрин для Apple iPad 9 10.2 (2021) (A2602/A2603/A2604) (черный) (Premium)",
            subject="тачскрин",
            category="Тачскрины для планшетов",
        ),
        Product(
            article="067785",
            name="Тачскрин для Apple iPad 7 10.2 (2019) (A2197/A2198/A2200) / iPad 8 10.2 (2020) (A2428/A2429/A2270) + кнопка Home (черный) (Musttby) (Premium)",
            subject="тачскрин",
            category="Тачскрины для планшетов",
        ),
        Product(
            article="081479",
            name="Тачскрин для Apple iPad 7 10.2 (2019) (A2197/A2198/A2200) / 8 10.2 (2020) (A2428/A2429/A2270) + OCA (черный) (медный) (Feaglet)",
            subject="тачскрин",
            category="Тачскрины для планшетов",
            quality_raw="Premium",
        ),
        Product(
            article="038546",
            name="Тачскрин для Apple iPad mini 3 (A1599/A1600) + коннектор (черный) (Premium)",
            subject="тачскрин",
            category="Тачскрины для планшетов",
        ),
        Product(
            article="052124",
            name="Тачскрин для Apple iPad mini (A1432/A1454/A1455) / iPad mini 2 (A1489/A1490/A1491) (под пайку) (черный) (Premium)",
            subject="тачскрин",
            category="Тачскрины для планшетов",
        ),
        Product(
            article="081473",
            name="Тачскрин для Apple iPad mini 6 (A2567/A2568) + OCA (черный) (медный) (Feaglet)",
            subject="тачскрин",
            category="Тачскрины для планшетов",
            quality_raw="Premium",
        ),
        Product(
            article="060807",
            name="Тачскрин для Apple iPhone 11 + OCA + коннектор (без микросхемы) (черный) (Medium)",
            subject="тачскрин",
            category="Тачскрины для телефонов",
        ),
        Product(
            article="068157",
            name="Тачскрин для Apple iPhone 11 + OCA + коннектор (черный) (Musttby)",
            subject="тачскрин",
            category="Тачскрины для телефонов",
            quality_raw="Medium",
        ),
    ]
    db_session.add_all(products)
    db_session.commit()

    results = {
        product.article: generate_sku_for_product(db_session, product) for product in products
    }

    assert results["057827"].planned_sku == "OEM-DSP-IPD9102-BLK-CPH-TCH"
    assert results["067785"].planned_sku == "OEM-DSP-IPD78102-BLK-CPH-HOME-MST"
    assert results["081479"].planned_sku == "OEM-DSP-IPD78102-BLK-CPH-OCA-FEA"
    assert results["038546"].planned_sku == "OEM-DSP-IPDMN3-BLK-CPH-CON"
    assert results["052124"].planned_sku == "OEM-DSP-IPDMN12-BLK-CPH-SOLD"
    assert results["081473"].planned_sku == "OEM-DSP-IPDMN6-BLK-CPH-OCA-FEA"
    assert results["060807"].planned_sku == "OEM-DSP-IPH11-BLK-CPM-OCA-CON-NOIC"
    assert results["068157"].planned_sku == "OEM-DSP-IPH11-BLK-CPM-OCA-CON-MST"


def test_generate_apple_legacy_iphone_xs_max_uses_compact_device_code(db_session) -> None:
    product = Product(
        article="1001-apple-xsm",
        name="Дисплей для Apple iPhone Xs Max + тачскрин (черный) (JK) (In-Cell)",
        subject="дисплей",
        color="черный",
        quality_raw="Medium",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "IPHXSM"
    assert result.planned_sku == "OEM-DSP-IPHXSM-INL-BLK-CPM-JK"


def test_generate_apple_ipad_mini_2_3_family_uses_compact_device_code(db_session) -> None:
    product = Product(
        article="1001-apple-ipad-mini23",
        name="Дисплей для Apple iPad mini 2 (A1489/A1490/A1491) / iPad mini 3 (A1599/A1600) (Medium)",
        subject="дисплей",
        quality_raw="Medium",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "IPDMN23"
    assert result.planned_sku == "OEM-DSP-IPDMN23-CPM"


def test_generate_apple_iphone_17_strips_sim_esim_from_device_code(db_session) -> None:
    product = Product(
        article="1001-apple-17pm",
        name="Дисплей для Apple iPhone 17 Pro Max (SIM + eSIM) / iPhone 17 Pro Max (eSIM) + тачскрин + ALS шлейф (черный) (ORIG100) (SP)",
        subject="дисплей",
        color="черный",
        quality_raw="ORIG100",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "IPH17PM"
    assert result.rev == "SA2"
    assert result.planned_sku == "OEM-DSP-IPH17PM-BLK-OR1-SA2"


def test_generate_apple_super_retina_is_compact(db_session) -> None:
    product = Product(
        article="1001-apple-super-retina",
        name="Дисплей для Apple iPhone 11 Pro + тачскрин + ALS шлейф (черный) (ORIG100) (SP)",
        subject="дисплей",
        display_type="Super Retina",
        color="черный",
        quality_raw="ORIG100",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.key_code == "OLD-BLK-OR1"
    assert result.planned_sku == "OEM-DSP-IPH11P-OLD-BLK-OR1-SP-ALS"


def test_generate_flex_for_display_cable_name(db_session) -> None:
    product = Product(
        article="1001-apple-display-flex",
        name="Шлейф для Apple iPad Air 6 11.0 (2024) / iPad Air 7 11.0 (2025) с комп. (на дисплей) (ORIG100)",
        subject="дисплей",
        quality_raw="ORIG100",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.category_code == "FLX"


def test_generate_sim_tray_key_from_name(db_session) -> None:
    product = Product(
        article="flex-simtray-vivo",
        name="Держатель сим-карты для Vivo V25 Pro 5G (V2158) (черный)",
        subject="держатель сим-карты",
        category="Держатели сим-карт для телефонов",
        vid_nomenklatury="Шлейфы/разъёмы/мелкие узлы",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.category_code == "FLX"
    assert result.key_code == "SIMTRAY-BLK"
    assert result.planned_sku == "OEM-FLX-VVO-V25P-SIMTRAY-BLK"


def test_generate_back_cover_key_from_name(db_session) -> None:
    product = Product(
        article="part-back-cover-iphone",
        name="Задняя крышка для Apple iPhone Xs Max (золотистый) (Premium)",
        subject="крышка",
        category="Задние крышки для Apple iPhone",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.category_code == "PRT"
    assert result.key_code == "BCOV-GLD-PR"
    assert result.planned_sku == "OEM-PRT-IPHXSM-BCOV-GLD-PR"


def test_generate_back_cover_prefers_part_category_with_camera_glass_text(db_session) -> None:
    product = Product(
        article="part-back-cover-camera-glass",
        name="Задняя крышка для Samsung A025 Galaxy A02s (черный) (в сборе со стеклом камеры)",
        subject="крышка",
        category="Задние крышки для телефонов",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.category_code == "PRT"
    assert result.key_code == "BCOV-BLK-CG"
    assert result.planned_sku == "OEM-PRT-SMG-A025-BCOV-BLK-CG"


def test_generate_camera_glass_back_cover_uses_short_cg_flag(db_session) -> None:
    product = Product(
        article="part-iphone-camera-glass-cover",
        name=(
            "Задняя крышка для Apple iPhone 15 Pro Max (черный) "
            "(в сборе со стеклом камеры) (Premium)"
        ),
        subject="крышка",
        category="Задние крышки для телефонов",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.category_code == "PRT"
    assert result.key_code == "BCOV-BLK-PR-CG"
    assert result.planned_sku == "OEM-PRT-IPH15PM-BCOV-BLK-PR-CG"


def test_generate_phone_loudspeaker_key_from_category(db_session) -> None:
    product = Product(
        article="part-phone-speaker",
        name="Динамик (полифонический) для Apple iPhone 12 Pro Max",
        subject="динамик",
        category="Динамики для телефонов",
        vid_nomenklatury="Акустика/вибро",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.category_code == "PRT"
    assert result.key_code == "SPK"
    assert result.planned_sku == "OEM-PRT-IPH12PM-SPK"


def test_generate_phone_earpiece_key_from_name(db_session) -> None:
    product = Product(
        article="part-phone-earpiece",
        name="Динамик (слуховой) для Apple iPhone 13 Pro",
        subject="динамик",
        category="Динамики для телефонов",
        vid_nomenklatury="Акустика/вибро",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.category_code == "PRT"
    assert result.key_code == "EARSPK"
    assert result.planned_sku == "OEM-PRT-IPH13P-EARSPK"


def test_generate_speaker_mesh_key_from_name(db_session) -> None:
    product = Product(
        article="part-speaker-mesh",
        name="Сеточка динамика (полифонический) для Apple iPhone 12 Pro с комп.",
        subject="сетка динамика",
        category="Сеточки динамиков для телефонов",
        vid_nomenklatury="Акустика/вибро",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.category_code == "PRT"
    assert result.key_code == "SPK-MESH-POLY-KIT"
    assert result.planned_sku == "OEM-PRT-IPH12P-SPK-MESH-POLY-KIT"


def test_generate_back_cover_adhesive_key_from_name(db_session) -> None:
    product = Product(
        article="part-back-cover-adhesive",
        name="Проклейка задней крышки для Apple iPhone 15 Pro",
        subject="наклейка",
        category="Проклейки задних крышек для телефонов",
        vid_nomenklatury="Шлейфы/разъёмы/мелкие узлы",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.category_code == "PRT"
    assert result.key_code == "BCOV-ADH"
    assert result.planned_sku == "OEM-PRT-IPH15P-BCOV-ADH"


def test_generate_front_camera_gasket_key_even_with_back_cover_category(db_session) -> None:
    product = Product(
        article="part-front-camera-gasket",
        name="Прокладка передней камеры и датчика сенсора для Apple iPhone 11 Pro Max",
        subject="изолятор",
        category="Проклейки задних крышек для телефонов",
        vid_nomenklatury="Шлейфы/разъёмы/мелкие узлы",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.category_code == "PRT"
    assert result.key_code == "FCAM-GSK"
    assert result.planned_sku == "OEM-PRT-IPH11PM-FCAM-GSK"


def test_generate_apple_watch_ultra_adhesive_uses_watch_device_code(db_session) -> None:
    product = Product(
        article="watch-ultra-adhesive",
        name="Проклейка дисплейного модуля для Apple Watch Ultra 2 (49 мм)",
        subject="наклейка",
        category="Проклейки дисплейных модулей для смарт-часов",
        vid_nomenklatury="Шлейфы/разъёмы/мелкие узлы",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.category_code == "PRT"
    assert result.device_code == "IPHWU249"
    assert result.key_code == "DSP-ADH"
    assert result.planned_sku == "OEM-PRT-IPHWU249-DSP-ADH"


def test_generate_cooler_uses_name_and_category_despite_bad_speaker_subject(
    db_session,
) -> None:
    product = Product(
        article="bad-subject-cooler",
        name="Вентилятор (кулер) для Apple MacBook Pro 13 A1278",
        subject="динамик",
        category="Кулеры для ноутбуков",
        vid_nomenklatury="Акустика/вибро",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.planned_sku == "OEM-PRT-MBP13A1278-FAN"


def test_generate_chip_key_from_parenthesized_code(db_session) -> None:
    product = Product(
        article="chip-power-controller",
        name="Микросхема контроллер питания (BQ24157A) для Huawei Honor 7C (AUM-L41)",
        subject="микросхема",
        category="Микросхемы для Android",
        vid_nomenklatury="Платы и электронные компоненты",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.category_code == "IC"
    assert result.key_code == "BQ24157A"
    assert result.planned_sku == "OEM-IC-HWE-H7C-BQ24157A"


def test_generate_battery_repair_flex_prefers_flex_category(db_session) -> None:
    product = Product(
        article="flex-battery-repair",
        name="Шлейф для восстановления аккумулятора Apple iPhone 15 Pro / iPhone 15 Pro Max (JCID) (в сборе с коннектором)",
        subject="шлейф",
        category="Шлейфы для телефонов",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.category_code == "FLX"
    assert result.key_code == "BAT-JCID-CON"
    assert result.planned_sku == "OEM-FLX-IPH15PM-BAT-JCID-CON"


def test_generate_iphone_repair_flex_uses_specific_purpose_key(db_session) -> None:
    products = [
        Product(
            article="067504",
            name="Шлейф для восстановления передней камеры Apple iPhone 14 Pro Max (JCID) (в сборе с коннектором)",
            subject="шлейф",
            category="Шлейфы для телефонов",
        ),
        Product(
            article="062074",
            name="Шлейф для восстановления задней камеры Apple iPhone 13 Pro / iPhone 13 Pro Max (JCID) (в сборе с коннектором)",
            subject="шлейф",
            category="Шлейфы для телефонов",
        ),
        Product(
            article="073426",
            name="Шлейф для восстановления Face ID Apple iPhone 14 Pro Max (JCID) (в сборе с коннектором)",
            subject="шлейф",
            category="Шлейфы для телефонов",
        ),
        Product(
            article="067512",
            name="Шлейф для восстановления сенсора Apple iPhone 14 Pro Max (JCID)",
            subject="шлейф",
            category="Шлейфы для телефонов",
        ),
        Product(
            article="052414",
            name="Шлейф для Apple iPhone 13 mini с комп. + вспышка (ORIG100)",
            subject="шлейф",
            category="Шлейфы для телефонов",
        ),
        Product(
            article="065826",
            name="Шлейф для Apple iPhone 15 Plus с комп. + считыватель eSIM (US Version) (ORIG100)",
            subject="шлейф",
            category="Шлейфы для телефонов",
        ),
        Product(
            article="059720",
            name="Шлейф для Apple iPhone 11 с комп. (усилитель сигнала Wi-Fi)",
            subject="шлейф",
            category="Шлейфы для телефонов",
        ),
    ]
    db_session.add_all(products)
    db_session.commit()

    results = {
        product.article: generate_sku_for_product(db_session, product) for product in products
    }

    assert results["067504"].key_code == "FCAM-JCID-CON"
    assert results["067504"].planned_sku == "OEM-FLX-IPH14PM-FCAM-JCID-CON"
    assert results["062074"].key_code == "RCAM-JCID-CON"
    assert results["062074"].planned_sku == "OEM-FLX-IPH13PM-RCAM-JCID-CON"
    assert results["073426"].key_code == "FACEID-JCID-CON"
    assert results["073426"].planned_sku == "OEM-FLX-IPH14PM-FACEID-JCID-CON"
    assert results["067512"].key_code == "SENS-JCID"
    assert results["067512"].planned_sku == "OEM-FLX-IPH14PM-SENS-JCID"
    assert results["052414"].key_code == "FLASH"
    assert results["052414"].planned_sku == "OEM-FLX-IPH13MN-FLASH"
    assert results["065826"].key_code == "ESIM"
    assert results["065826"].planned_sku == "OEM-FLX-IPH15PL-ESIM"
    assert results["059720"].key_code == "WFIAMP"
    assert results["059720"].planned_sku == "OEM-FLX-IPH11-WFIAMP"


def test_generate_iphone_repair_flex_keeps_quality_and_connector_variants(
    db_session,
) -> None:
    products = [
        Product(
            article="059923",
            name="Шлейф для Apple iPhone 14 Pro с комп. + сенсор (ORIG100)",
            subject="шлейф",
            category="Шлейфы для телефонов",
        ),
        Product(
            article="067511",
            name="Шлейф для восстановления сенсора Apple iPhone 14 Pro (JCID)",
            subject="шлейф",
            category="Шлейфы для телефонов",
        ),
        Product(
            article="078181",
            name="Шлейф для Apple iPhone 14 Pro с комп. + сенсор",
            subject="шлейф",
            category="Шлейфы для телефонов",
        ),
        Product(
            article="054718",
            name="Шлейф для восстановления Face ID Apple iPhone 11 (JCID)",
            subject="шлейф",
            category="Шлейфы для телефонов",
        ),
        Product(
            article="060084",
            name="Шлейф для восстановления Face ID Apple iPhone 11 (JCID) (в сборе с коннектором)",
            subject="шлейф",
            category="Шлейфы для телефонов",
        ),
        Product(
            article="068643",
            name="Шлейф для Apple iPhone 11 с комп. (усилитель сигнала Wi-Fi) (ORIG100)",
            subject="шлейф",
            category="Шлейфы для телефонов",
        ),
        Product(
            article="054731",
            name="Шлейф для восстановления аккумулятора Apple iPhone 11 Pro Max (JCID)",
            subject="шлейф",
            category="Шлейфы для телефонов",
        ),
        Product(
            article="054733",
            name="Шлейф для восстановления аккумулятора Apple iPhone 11 Pro / iPhone 11 Pro Max (JCID) (в сборе с коннектором)",
            subject="шлейф",
            category="Шлейфы для телефонов",
        ),
        Product(
            article="067506",
            name="Шлейф для восстановления передней камеры Apple iPhone 15 (JCID) (в сборе с коннектором)",
            subject="шлейф",
            category="Шлейфы для телефонов",
        ),
        Product(
            article="068147",
            name="Шлейф для восстановления передней камеры Apple iPhone 15 (JCID)",
            subject="шлейф",
            category="Шлейфы для телефонов",
        ),
    ]
    db_session.add_all(products)
    db_session.commit()

    results = {
        product.article: generate_sku_for_product(db_session, product) for product in products
    }

    assert results["059923"].planned_sku == "OEM-FLX-IPH14P-SENS-OR1"
    assert results["067511"].planned_sku == "OEM-FLX-IPH14P-SENS-JCID"
    assert results["078181"].planned_sku == "OEM-FLX-IPH14P-SENS"
    assert results["054718"].planned_sku == "OEM-FLX-IPH11-FACEID-JCID"
    assert results["060084"].planned_sku == "OEM-FLX-IPH11-FACEID-JCID-CON"
    assert results["068643"].planned_sku == "OEM-FLX-IPH11-WFIAMP-OR1"
    assert results["054731"].planned_sku == "OEM-FLX-IPH11PM-BAT-JCID"
    assert results["054733"].planned_sku == "OEM-FLX-IPH11PM-BAT-JCID-CON"
    assert results["067506"].planned_sku == "OEM-FLX-IPH15-FCAM-JCID-CON"
    assert results["068147"].planned_sku == "OEM-FLX-IPH15-FCAM-JCID"


def test_generate_iphone_antenna_uses_signal_type(db_session) -> None:
    products = [
        Product(
            article="038017",
            name="Антенна Wi-Fi для Apple iPhone 6",
            subject="антенна",
            category="Шлейфы для телефонов",
        ),
        Product(
            article="038550",
            name="Антенна GSM для Apple iPhone 6",
            subject="антенна",
            category="Шлейфы для телефонов",
        ),
        Product(
            article="042110",
            name="Антенна NFC для Apple iPhone 6",
            subject="антенна",
            category="Шлейфы для телефонов",
        ),
        Product(
            article="044974",
            name="Антенна GPS для Apple iPhone 6 Plus",
            subject="антенна",
            category="Шлейфы для телефонов",
        ),
        Product(
            article="047565",
            name="Антенна NFC/Bluetooth для Apple iPhone 8 Plus",
            subject="антенна",
            category="Шлейфы для телефонов",
        ),
    ]
    db_session.add_all(products)
    db_session.commit()

    results = {
        product.article: generate_sku_for_product(db_session, product) for product in products
    }

    assert results["038017"].key_code == "ANTWIFI"
    assert results["038017"].planned_sku == "OEM-FLX-IPH6-ANTWIFI"
    assert results["038550"].key_code == "ANTGSM"
    assert results["038550"].planned_sku == "OEM-FLX-IPH6-ANTGSM"
    assert results["042110"].key_code == "ANTNFC"
    assert results["042110"].planned_sku == "OEM-FLX-IPH6-ANTNFC"
    assert results["044974"].key_code == "ANTGPS"
    assert results["044974"].planned_sku == "OEM-FLX-IPH6PL-ANTGPS"
    assert results["047565"].key_code == "ANTNFCBT"
    assert results["047565"].planned_sku == "OEM-FLX-IPH8PL-ANTNFCBT"


def test_generate_iphone_sim_parts_keep_version_and_precise_color(db_session) -> None:
    products = [
        Product(
            article="040081",
            name="Держатель сим-карты для Apple iPhone Xr (желтый)",
            subject="держатель сим-карты",
            category="Шлейфы для телефонов",
        ),
        Product(
            article="040083",
            name="Держатель сим-карты для Apple iPhone Xr (коралловый)",
            subject="держатель сим-карты",
            category="Шлейфы для телефонов",
        ),
        Product(
            article="040358",
            name="Держатель сим-карты для Apple iPhone 7 (черный глянец)",
            subject="держатель сим-карты",
            category="Шлейфы для телефонов",
        ),
        Product(
            article="026277",
            name="Держатель сим-карты для Apple iPhone 5 (серебристый)",
            subject="держатель сим-карты",
            category="Шлейфы для телефонов",
        ),
        Product(
            article="032089",
            name="Держатель сим-карты для Apple iPhone 5s / iPhone SE (серебристый)",
            subject="держатель сим-карты",
            category="Шлейфы для телефонов",
        ),
        Product(
            article="078014",
            name="Разъем SIM для Apple iPhone 15 / iPhone 15 Plus (1 SIM Version)",
            subject="разъем sim",
            category="Шлейфы для телефонов",
        ),
        Product(
            article="081697",
            name="Держатель сим-карты для Apple iPhone 15 Pro / iPhone 15 Pro Max (титановый) (2 SIM Version)",
            subject="держатель сим-карты",
            category="Шлейфы для телефонов",
        ),
        Product(
            article="075599",
            name="Шлейф для Apple Watch Ultra (49 мм) с комп. + Digital Crown (титановый) (ORIG100)",
            subject="шлейф",
            category="Шлейфы для часов",
        ),
    ]
    db_session.add_all(products)
    db_session.commit()

    results = {
        product.article: generate_sku_for_product(db_session, product) for product in products
    }

    assert results["040081"].key_code == "SIMTRAY-YLW"
    assert results["040081"].planned_sku == "OEM-FLX-IPHXR-SIMTRAY-YLW"
    assert results["040083"].key_code == "SIMTRAY-COR"
    assert results["040083"].planned_sku == "OEM-FLX-IPHXR-SIMTRAY-COR"
    assert results["040358"].key_code == "SIMTRAY-BLKGL"
    assert results["040358"].planned_sku == "OEM-FLX-IPH7-SIMTRAY-BLKGL"
    assert results["026277"].key_code == "SIMTRAY-IP5-SLV"
    assert results["026277"].planned_sku == "OEM-FLX-IPH5-SIMTRAY-IP5-SLV"
    assert results["032089"].key_code == "SIMTRAY-SLV"
    assert results["032089"].planned_sku == "OEM-FLX-IPH5-SIMTRAY-SLV"
    assert results["078014"].key_code == "SIMCON-1SIM"
    assert results["078014"].planned_sku == "OEM-FLX-IPH15PL-SIMCON-1SIM"
    assert results["081697"].key_code == "SIMTRAY-2SIM-TIT"
    assert results["081697"].planned_sku == "OEM-FLX-IPH15PM-SIMTRAY-2SIM-TIT"
    assert results["075599"].key_code == "DCRWN-TIT"
    assert results["075599"].planned_sku == "OEM-FLX-IPHWU49-DCRWN-TIT"


def test_generate_tester_uses_tester_category(db_session) -> None:
    product = Product(
        article="069954",
        name="Тестер DL400 Pro + шлейфы iPhone X - 16 Pro Max",
        subject="тестер",
        category="Инструменты для ремонта",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.category_code == "TST"
    assert result.device_code == "UNV"
    assert result.key_code == "DL400"
    assert result.planned_sku == "OEM-TST-UNV-DL400"


def test_generate_tester_key_keeps_device_and_purpose_variants(db_session) -> None:
    products = [
        Product(article="077130", name="Тестер плат XZZ iPhone 11 / 11 Pro / 11 Pro Max"),
        Product(article="077131", name="Тестер плат XZZ iPhone 12 / 12 mini / 12 Pro / 12 Pro Max"),
        Product(article="077132", name="Тестер плат XZZ iPhone 13 / 13 mini / 13 Pro / 13 Pro Max"),
        Product(article="077133", name="Тестер плат XZZ iPhone 14 / 14 Plus / 14 Pro / 14 Pro Max"),
        Product(
            article="077134",
            name="Тестер плат XZZ iPhone 15 / 15 Plus / 15 Pro / 15 Pro Max (SIM Version)",
        ),
        Product(article="077135", name="Тестер плат XZZ iPhone 16 / 16 Plus / 16 Pro / 16 Pro Max"),
        Product(article="067606", name="Тестер для проверки зарядного гнезда Relife XA1"),
        Product(article="057229", name="Тестер для проверки дисплеев S300"),
        Product(article="051985", name="Тестер Home (Версия 2) + Насадки"),
    ]
    db_session.add_all(products)
    db_session.commit()

    results = {
        product.article: generate_sku_for_product(db_session, product) for product in products
    }

    assert results["077130"].planned_sku == "OEM-TST-UNV-XZZ-IP11"
    assert results["077131"].planned_sku == "OEM-TST-UNV-XZZ-IP12"
    assert results["077132"].planned_sku == "OEM-TST-UNV-XZZ-IP13"
    assert results["077133"].planned_sku == "OEM-TST-UNV-XZZ-IP14"
    assert results["077134"].planned_sku == "OEM-TST-UNV-XZZ-IP15-SIM"
    assert results["077135"].planned_sku == "OEM-TST-UNV-XZZ-IP16"
    assert results["067606"].planned_sku == "OEM-TST-UNV-XA1-CHG"
    assert results["057229"].planned_sku == "OEM-TST-UNV-S300-DSP"
    assert results["051985"].planned_sku == "OEM-TST-UNV-HOME-V2"


def test_generate_camera_key_from_name(db_session) -> None:
    product = Product(
        article="cam-vivo-rear",
        name="Камера для Vivo V25 Pro 5G (V2158) (задняя) (ORIG100)",
        subject="камера",
        category="Камеры для телефонов",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.category_code == "CAM"
    assert result.key_code == "RCAM-OR1"
    assert result.planned_sku == "OEM-CAM-VVO-V25P-RCAM-OR1"


def test_generate_camera_category_wins_over_pad_pd_text(db_session) -> None:
    products = [
        Product(
            article="cam-oneplus-pad-rear",
            name="Камера для OnePlus Pad (OPD2203) (13 MP) (задняя)",
            subject="камера",
            category="Камеры для планшетов",
            vid_nomenklatury="Камеры",
        ),
        Product(
            article="cam-oneplus-pad-front",
            name="Камера для OnePlus Pad (OPD2203) (передняя)",
            subject="камера",
            category="Камеры для планшетов",
            vid_nomenklatury="Камеры",
        ),
    ]
    db_session.add_all(products)
    db_session.commit()

    results = {
        product.article: generate_sku_for_product(db_session, product) for product in products
    }

    assert results["cam-oneplus-pad-rear"].category_code == "CAM"
    assert results["cam-oneplus-pad-rear"].planned_sku == "OEM-CAM-ONE-PAD-RCAM-13MP"
    assert results["cam-oneplus-pad-front"].category_code == "CAM"
    assert results["cam-oneplus-pad-front"].planned_sku == "OEM-CAM-ONE-PAD-FCAM"


def test_generate_protective_glass_key_from_name(db_session) -> None:
    product = Product(
        article="glass-rem-gl86",
        name="Защитное стекло Remax Corning Glass GL-86 для Apple iPhone 14 Pro Max (антибликовое) (черный)",
        subject="защитное стекло",
        category="Защитные стекла для телефонов",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.category_code == "GLS"
    assert result.key_code == "PROT-GL86-AG-BLK"
    assert result.planned_sku == "OEM-GLS-IPH14PM-PROT-GL86-AG-BLK"


def test_generate_realme_3_in_1_glass_keeps_real_model_code(db_session) -> None:
    products = [
        Product(
            article="051405",
            name="Защитное стекло OG Glass (3 в 1) для Realme X50 Pro 5G (RMX2071) (черный)",
            subject="защитное стекло",
            category="Защитные стекла для телефонов",
        ),
        Product(
            article="077051",
            name="Защитное стекло OG Glass (3 в 1) для Realme 15T (RMX5111) (черный)",
            subject="защитное стекло",
            category="Защитные стекла для телефонов",
        ),
        Product(
            article="077884",
            name="Защитное стекло OG Glass (3 в 1) для Realme P3 (RMX5079) (черный)",
            subject="защитное стекло",
            category="Защитные стекла для телефонов",
        ),
        Product(
            article="077901",
            name="Защитное стекло OG Glass (3 в 1) для Realme P3 Ultra (RMX5031) (черный)",
            subject="защитное стекло",
            category="Защитные стекла для телефонов",
        ),
        Product(
            article="081074",
            name="Защитное стекло OG Glass (3 в 1) для Realme 16 (RMX5171) (черный)",
            subject="защитное стекло",
            category="Защитные стекла для телефонов",
        ),
        Product(
            article="082124",
            name="Защитное стекло OG Glass (3 в 1) для Realme C100i (RMX5377) (черный)",
            subject="защитное стекло",
            category="Защитные стекла для телефонов",
        ),
    ]
    db_session.add_all(products)
    db_session.commit()

    results = {
        product.article: generate_sku_for_product(db_session, product) for product in products
    }

    assert results["051405"].device_code == "RLM-X50P5"
    assert results["051405"].planned_sku == "OEM-GLS-RLM-X50P5-PROT-3IN1-BLK"
    assert results["077051"].device_code == "RLM-15T"
    assert results["077051"].planned_sku == "OEM-GLS-RLM-15T-PROT-3IN1-BLK"
    assert results["077884"].device_code == "RLM-P3"
    assert results["077884"].planned_sku == "OEM-GLS-RLM-P3-PROT-3IN1-BLK"
    assert results["077901"].device_code == "RLM-P3U"
    assert results["077901"].planned_sku == "OEM-GLS-RLM-P3U-PROT-3IN1-BLK"
    assert results["081074"].device_code == "RLM-16"
    assert results["081074"].planned_sku == "OEM-GLS-RLM-16-PROT-3IN1-BLK"
    assert results["082124"].device_code == "RLM-C100I"
    assert results["082124"].planned_sku == "OEM-GLS-RLM-C100I-PROT-3IN1-BLK"


def test_generate_nothing_cmf_phone_glass_uses_short_device_code(db_session) -> None:
    product = Product(
        article="066075",
        name="Защитное стекло OG Glass (3 в 1) для Nothing CMF Phone 1 (A015) (черный)",
        subject="защитное стекло",
        category="Защитные стекла для телефонов",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "NOT-CMF1"
    assert result.key_code == "PROT-3IN1-BLK"
    assert result.planned_sku == "OEM-GLS-NOT-CMF1-PROT-3IN1-BLK"


def test_generate_samsung_note_module_glass_keeps_note_variant_and_feaglet(
    db_session,
) -> None:
    products = [
        Product(
            article="057479",
            name="Стекло модуля для Samsung N970 Galaxy Note 10 + OCA (черный)",
            subject="стекло модуля",
            category="Стекла модуля для телефонов",
        ),
        Product(
            article="057480",
            name="Стекло модуля для Samsung N975 Galaxy Note 10+ + OCA (черный)",
            subject="стекло модуля",
            category="Стекла модуля для телефонов",
        ),
        Product(
            article="081600",
            name="Стекло модуля для Samsung N770 Galaxy Note 10 Lite + OCA (черный) (Feaglet)",
            subject="стекло модуля",
            category="Стекла модуля для телефонов",
        ),
        Product(
            article="081618",
            name="Стекло модуля для Samsung N970 Galaxy Note 10 + OCA (черный) (Feaglet)",
            subject="стекло модуля",
            category="Стекла модуля для телефонов",
        ),
    ]
    db_session.add_all(products)
    db_session.commit()

    results = {
        product.article: generate_sku_for_product(db_session, product) for product in products
    }

    assert results["057479"].planned_sku == "OEM-GLS-SMG-N10-MODG-OCA-BLK"
    assert results["057480"].planned_sku == "OEM-GLS-SMG-N10P-MODG-OCA-BLK"
    assert results["081600"].planned_sku == "OEM-GLS-SMG-N10LT-MODG-OCA-FEA-BLK"
    assert results["081618"].planned_sku == "OEM-GLS-SMG-N10-MODG-OCA-FEA-BLK"


def test_generate_otao_protective_glass_keeps_series(db_session) -> None:
    products = [
        Product(
            article="082303",
            name="Защитное стекло OTAO Rock Glass для Apple iPhone 16 Pro / iPhone 17 (ультрапрочное)",
            subject="защитное стекло",
            category="Защитные стекла для телефонов",
        ),
        Product(
            article="082307",
            name="Защитное стекло OTAO Sapphire Glass для Apple iPhone 16 Pro / iPhone 17 (сапфировое)",
            subject="защитное стекло",
            category="Защитные стекла для телефонов",
        ),
        Product(
            article="082311",
            name="Защитное стекло OTAO Edge Armor для Apple iPhone 16 Pro / iPhone 17 (металлическая рамка)",
            subject="защитное стекло",
            category="Защитные стекла для телефонов",
        ),
        Product(
            article="082315",
            name="Защитное стекло OTAO Dragon Armor для Apple iPhone 16 Pro / iPhone 17 (устойчивое к царапинам)",
            subject="защитное стекло",
            category="Защитные стекла для телефонов",
        ),
    ]
    db_session.add_all(products)
    db_session.commit()

    results = {
        product.article: generate_sku_for_product(db_session, product) for product in products
    }

    assert results["082303"].planned_sku == "OEM-GLS-IPH17-PROT-ROCK"
    assert results["082307"].planned_sku == "OEM-GLS-IPH17-PROT-SAPH"
    assert results["082311"].planned_sku == "OEM-GLS-IPH17-PROT-EDGE"
    assert results["082315"].planned_sku == "OEM-GLS-IPH17-PROT-DRAG"


def test_generate_bottom_board_key_from_name(db_session) -> None:
    product = Product(
        article="board-vivo-bottom",
        name="Нижняя плата для Vivo V25 Pro 5G (V2158) с комп. + разъем зарядки + микрофон",
        subject="плата",
        category="Нижние платы для телефонов",
        vid_nomenklatury="Платы и электронные компоненты",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.category_code == "IC"
    assert result.key_code == "SUB-CHG-MIC"
    assert result.planned_sku == "OEM-IC-VVO-V25P-SUB-CHG-MIC"


def test_generate_bottom_board_name_overrides_wrong_sim_holder_subject(db_session) -> None:
    product = Product(
        article="078972",
        name=(
            "Нижняя плата для Infinix Note 60 (X6879) с комп. "
            "+ разъем зарядки + микрофон + разъем SIM (ORIG100)"
        ),
        subject="держатель сим-карты",
        category="Нижние платы для телефонов",
        vid_nomenklatury="Шлейфы/разъёмы/мелкие узлы",
        quality_raw="ORIG100",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.category_code == "IC"
    assert result.planned_sku == "OEM-IC-INF-N60-SUB-CHG-MIC-SIM-OR1"


def test_generate_bottom_board_keeps_orig100_with_compact_long_key(db_session) -> None:
    product = Product(
        article="070694",
        name=(
            "Нижняя плата для Huawei Honor 400 Lite (ABR-NX1) с комп. "
            "+ разъем зарядки + микрофон + разъем SIM (ORIG100)"
        ),
        subject="плата",
        category="Нижние платы для телефонов",
        quality_raw="ORIG100",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.planned_sku == "OEM-IC-HWE-H400LT-SUB-CHG-M-SIM-OR1"


def test_generate_lenovo_legion_y700_board_uses_compact_device_code(db_session) -> None:
    product = Product(
        article="1001-lenovo-y700-board",
        name=(
            "Нижняя плата для Lenovo Legion Y700 5 (TB323FC/TB323FU) с комп. "
            "+ разъем зарядки + микрофон"
        ),
        subject="плата",
        category="Нижние платы для планшетов",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "LEN-Y700G5"
    assert result.key_code == "SUB-CHG-MIC"
    assert result.planned_sku == "OEM-IC-LEN-Y700G5-SUB-CHG-MIC"


def test_generate_bottom_board_keeps_model_quality_and_pro_variants(db_session) -> None:
    products = [
        Product(
            article="080887",
            name=(
                "Нижняя плата для Nothing Phone 4a (A069) с комп. "
                "+ разъем зарядки + микрофон + разъем SIM"
            ),
            subject="плата",
            category="Нижние платы для телефонов",
        ),
        Product(
            article="080888",
            name=(
                "Нижняя плата для Nothing Phone 4a (A069) с комп. "
                "+ разъем зарядки + микрофон + разъем SIM (ORIG100)"
            ),
            subject="плата",
            category="Нижние платы для телефонов",
            quality_raw="ORIG100",
        ),
        Product(
            article="080912",
            name=(
                "Нижняя плата для Nothing Phone 4a Pro (A069P) с комп. "
                "+ разъем зарядки + микрофон + разъем SIM"
            ),
            subject="плата",
            category="Нижние платы для телефонов",
        ),
        Product(
            article="080913",
            name=(
                "Нижняя плата для Nothing Phone 4a Pro (A069P) с комп. "
                "+ разъем зарядки + микрофон + разъем SIM (ORIG100)"
            ),
            subject="плата",
            category="Нижние платы для телефонов",
            quality_raw="ORIG100",
        ),
    ]
    db_session.add_all(products)
    db_session.commit()

    results = {
        product.article: generate_sku_for_product(db_session, product) for product in products
    }

    assert results["080887"].planned_sku == "OEM-IC-NOT-PH4A-SUB-CHG-MIC-SIM"
    assert results["080888"].planned_sku == "OEM-IC-NOT-PH4A-SUB-CHG-MIC-SIM-OR1"
    assert results["080912"].planned_sku == "OEM-IC-NOT-PH4P-SUB-CHG-MIC-SIM"
    assert results["080913"].planned_sku == "OEM-IC-NOT-PH4P-SUB-CHG-MIC-SIM-OR1"


def test_generate_bottom_board_keeps_exact_device_variants(db_session) -> None:
    products = [
        Product(
            article="040477",
            name=(
                "Нижняя плата для Asus ZenFone 3 Max (ZC553KL) с комп. "
                "+ разъем зарядки + микрофон"
            ),
            subject="плата",
            category="Нижние платы для телефонов",
        ),
        Product(
            article="043759",
            name=(
                "Нижняя плата для Asus ZenFone 3 Deluxe (ZS550KL) с комп. "
                "+ разъем зарядки + микрофон"
            ),
            subject="плата",
            category="Нижние платы для телефонов",
        ),
        Product(
            article="069497",
            name=(
                "Нижняя плата для Huawei Honor Magic 7 Pro (PTP-N49) с комп. "
                "+ разъем зарядки + микрофон + разъем SIM (ORIG100)"
            ),
            subject="плата",
            category="Нижние платы для телефонов",
            quality_raw="ORIG100",
        ),
        Product(
            article="069547",
            name=(
                "Нижняя плата для Huawei Honor X9c Smart (BRC-NX1) с комп. "
                "+ разъем зарядки + микрофон + разъем SIM (ORIG100)"
            ),
            subject="плата",
            category="Нижние платы для телефонов",
            quality_raw="ORIG100",
        ),
    ]
    db_session.add_all(products)
    db_session.commit()

    results = {
        product.article: generate_sku_for_product(db_session, product) for product in products
    }

    assert results["040477"].planned_sku == "OEM-IC-ASU-ZF3M-SUB-CHG-MIC"
    assert results["043759"].planned_sku == "OEM-IC-ASU-ZF3D-SUB-CHG-MIC"
    assert results["069497"].planned_sku == "OEM-IC-HWE-HM7P-SUB-CHG-MIC-SIM-OR1"
    assert results["069547"].planned_sku == "OEM-IC-HWE-HX9S-SUB-CHG-MIC-SIM-OR1"


def test_generate_tecno_and_xiaomi_board_and_flex_variants(db_session) -> None:
    products = [
        Product(
            article="063532",
            name="Шлейф для Tecno Phantom V Fold (AD10) с комп. (межплатный) (вертикальный)",
            subject="шлейф",
            category="Шлейфы для телефонов",
        ),
        Product(
            article="066672",
            name="Шлейф для Tecno Phantom V Fold 2 5G (AE10) с комп. (межплатный)",
            subject="шлейф",
            category="Шлейфы для телефонов",
        ),
        Product(
            article="068358",
            name=(
                "Шлейф для Tecno Phantom V Fold (AD10) с комп. "
                "(межплатный) (горизонтальный) (тип 2)"
            ),
            subject="шлейф",
            category="Шлейфы для телефонов",
        ),
        Product(
            article="064952",
            name=(
                "Нижняя плата для Tecno Camon 30 5G (CL7) с комп. "
                "+ разъем зарядки + микрофон (ORIG100)"
            ),
            subject="плата",
            category="Нижние платы для телефонов",
            quality_raw="ORIG100",
        ),
        Product(
            article="077984",
            name=(
                "Нижняя плата для Xiaomi Poco Pad M1 (2509ARPBDG) с комп. "
                "+ разъем зарядки + разъем гарнитуры (ORIG100)"
            ),
            subject="плата",
            category="Нижние платы для планшетов",
            quality_raw="ORIG100",
        ),
        Product(
            article="080647",
            name=(
                "Нижняя плата для Xiaomi Poco X8 Pro Max (2602BPC18G) с комп. "
                "+ разъем зарядки + разъем SIM + микрофон (ORIG100)"
            ),
            subject="плата",
            category="Нижние платы для телефонов",
            quality_raw="ORIG100",
        ),
    ]
    db_session.add_all(products)
    db_session.commit()

    results = {
        product.article: generate_sku_for_product(db_session, product) for product in products
    }

    assert results["063532"].planned_sku == "OEM-FLX-TEC-PVF-SUB-VERT"
    assert results["066672"].planned_sku == "OEM-FLX-TEC-PVF2-SUB"
    assert results["068358"].planned_sku == "OEM-FLX-TEC-PVF-SUB-HOR-T2"
    assert results["064952"].planned_sku == "OEM-IC-TEC-C305-SUB-CHG-MIC-OR1"
    assert results["077984"].planned_sku == "OEM-IC-XMI-PPADM1-SUB-CHG-AUX-OR1"
    assert results["080647"].planned_sku == "OEM-IC-XMI-PX8M-SUB-CHG-MIC-SIM-OR1"


def test_generate_flex_keeps_year_quality_position_and_dark_color(db_session) -> None:
    products = [
        Product(
            article="063814",
            name="Шлейф для Huawei MatePad 11 (2023) (DBR-W09) с комп. (на дисплей)",
            subject="шлейф",
            category="Шлейфы для планшетов",
        ),
        Product(
            article="063880",
            name=("Шлейф для Huawei MatePad 11 2021 (DBY-W09) с комп. " "(на дисплей) (Rev.C)"),
            subject="шлейф",
            category="Шлейфы для планшетов",
        ),
        Product(
            article="082363",
            name=(
                "Шлейф для Apple iPad 4 (A1458/A1459/A1460) с комп. " "+ разъем зарядки (ORIG100)"
            ),
            subject="шлейф",
            category="Шлейфы для планшетов",
            quality_raw="ORIG100",
        ),
        Product(
            article="057751",
            name="Шлейф для Sony Xperia Tablet Z3 Compact 8.0 с комп. (на нижний микрофон)",
            subject="шлейф",
            category="Шлейфы для планшетов",
        ),
        Product(
            article="053642",
            name=(
                "Шлейф для Infinix Hot 12i (X665B) / Hot 12 Play NFC (X6816D) "
                "/ Hot 20i (X665E) и др. с комп. + сканер отпечатка пальца "
                "(темно-зеленый)"
            ),
            subject="шлейф",
            category="Шлейфы для телефонов",
        ),
    ]
    db_session.add_all(products)
    db_session.commit()

    results = {
        product.article: generate_sku_for_product(db_session, product) for product in products
    }

    assert results["063814"].planned_sku == "OEM-FLX-HWE-MP1123-DSP"
    assert results["063880"].planned_sku == "OEM-FLX-HWE-MP1121-DSP"
    assert results["082363"].planned_sku == "OEM-FLX-IPD4-CHG-OR1"
    assert results["057751"].planned_sku == "OEM-FLX-SON-XTZ3C8-MIC-LOW"
    assert results["053642"].planned_sku == "OEM-FLX-INF-H12PLN-FPR-DGR"


def test_generate_remaining_duplicate_groups_keep_visible_variants(db_session) -> None:
    products = [
        Product(
            article="054554",
            name="Шлейф для Huawei P50 (ABR-LX9) с комп. (межплатный) (HN1AMBFL)",
            subject="шлейф",
            category="Шлейфы для телефонов",
        ),
        Product(
            article="057755",
            name="Шлейф для Huawei P50 (ABR-LX9) с комп. (межплатный) (HL2ABRFU)",
            subject="шлейф",
            category="Шлейфы для телефонов",
        ),
        Product(
            article="078624",
            name=(
                "Шлейф для Apple iPad Pro 12.9 (2018) (A1876/A1895/A2014) "
                "с комп. + микрофон (ORIG100)"
            ),
            subject="шлейф",
            category="Шлейфы для планшетов",
            quality_raw="ORIG100",
        ),
        Product(
            article="078629",
            name=(
                "Шлейф для Apple iPad Pro 12.9 (2020) (A2069/A2229/A2232) "
                "с комп. + микрофон (ORIG100)"
            ),
            subject="шлейф",
            category="Шлейфы для планшетов",
            quality_raw="ORIG100",
        ),
        Product(
            article="078636",
            name=(
                "Шлейф для Apple iPad Pro 11.0 (2021) (A2301/A2377) / "
                "iPad Pro 12.9 (2021) (A2379/A2461) с комп. "
                "(на кнопку включения) (Cellular Version) (ORIG100)"
            ),
            subject="шлейф",
            category="Шлейфы для планшетов",
            quality_raw="ORIG100",
        ),
        Product(
            article="078644",
            name=(
                "Шлейф для Apple iPad Pro 12.9 (2022) (A2436/A2437/A2764) "
                "с комп. (на кнопку включения) (Cellular Version) (ORIG100)"
            ),
            subject="шлейф",
            category="Шлейфы для планшетов",
            quality_raw="ORIG100",
        ),
        Product(
            article="061382",
            name="Шлейф для Realme C35 (RMX3511) / Narzo 50A Prime (RMX3516) с комп. (на кнопку включения)",
            subject="шлейф",
            category="Шлейфы для телефонов",
        ),
        Product(
            article="054409",
            name="Шлейф для Realme Narzo 50A (RMX3430) с комп. (на кнопку включения)",
            subject="шлейф",
            category="Шлейфы для телефонов",
        ),
        Product(
            article="075279",
            name="Стекло модуля для Google Pixel 10 (GK2MP/GLBW0/GL066) + OCA (черный) (Musttby) (Premium)",
            subject="стекло",
            category="Стекла камер",
        ),
        Product(
            article="078327",
            name="Стекло модуля для Google Pixel 9 Pro XL (GGX8B/GZC4K/GQ57S) + OCA (черный) (Musttby) (Premium)",
            subject="стекло",
            category="Стекла камер",
        ),
        Product(
            article="079177",
            name="Защитное стекло для Lenovo Legion Go 2 / Y700 Gen.5 (25 шт)",
            subject="стекло",
            category="Защитные стекла",
        ),
        Product(
            article="081608",
            name="Стекло модуля для Samsung G977 Galaxy S10 5G + OCA (черный) (Feaglet)",
            subject="стекло",
            category="Стекла камер",
        ),
        Product(
            article="047641",
            name=(
                "Стекло задней камеры для Huawei Honor 30S (CDY-NX9A) / "
                "P40 Lite 5G (CDY-NX9A) (без рамки) (черный)"
            ),
            subject="стекло",
            category="Стекла камер",
        ),
        Product(
            article="059913",
            name="Стекло задней камеры для Infinix Note 12 Turbo (G96) (X670) (без рамки) (черный)",
            subject="стекло",
            category="Стекла камер",
        ),
        Product(
            article="038883",
            name="Стекло модуля для Apple iPhone 5 + OCA + Polaris (черный) (в рамке)",
            subject="стекло",
            category="Стекла модулей",
        ),
        Product(
            article="064411",
            name="Стекло задней камеры для Xiaomi Redmi Pad Wi-Fi (22081283G) (без рамки) (черный)",
            subject="стекло",
            category="Стекла камер",
        ),
        Product(
            article="066595",
            name="Стекло задней камеры для Xiaomi Redmi Pad Wi-Fi (22081283G) (черный) (в рамке)",
            subject="стекло",
            category="Стекла камер",
        ),
        Product(
            article="040449",
            name="Нижняя плата для Asus ZenFone 4 Max (ZC554KL) с комп. + разъем зарядки + микрофон + вибро",
            subject="плата",
            category="Нижние платы для ASUS",
        ),
        Product(
            article="054299",
            name=(
                "Нижняя плата для Xiaomi Poco F4 GT (21121210G) с комп. "
                "+ разъем зарядки + микрофон (ORIG100)"
            ),
            subject="плата",
            category="Нижние платы для телефонов",
            quality_raw="ORIG100",
        ),
        Product(
            article="079156",
            name="Кнопки A / B / X / Y для ASUS ROG Ally X 2024 (цветные)",
            subject="кнопки",
            category="Кнопки для ASUS",
        ),
        Product(
            article="079159",
            name="Кнопки RB / LB для ASUS ROG Ally X 2024 (черные)",
            subject="кнопки",
            category="Кнопки для ASUS",
        ),
        Product(
            article="081645",
            name=(
                "Задняя крышка для Huawei Honor 400 Pro China (DNP-AN00) "
                "(черный) (в сборе со стеклом камеры) (ORIG100)"
            ),
            subject="крышка",
            category="Задние крышки для телефонов",
            color="черный",
            quality_raw="ORIG100",
        ),
        Product(
            article="075575",
            name=(
                "Средняя часть для Apple Watch Ultra 2 (49 мм) (титановый) "
                "(в сборе) (ORIG100) (Снятый) (возможен дефект ЛКП)"
            ),
            subject="корпус",
            category="Средние части для смарт-часов",
            color="черный",
            quality_raw="ORIG100",
        ),
        Product(
            article="048045",
            name="Задняя крышка для Apple iPhone Xr (желтый) (с широким отверстием) (Premium)",
            subject="крышка",
            category="Задние крышки для Apple iPhone",
            color="желтый",
        ),
        Product(
            article="053087",
            name=(
                "Клавиатура для Apple MacBook Air 11 A1370 / MacBook Air 11 A1465 "
                "(MID 2011 - EARLY 2017) (вертикальный Enter / русская раскладка)"
            ),
            subject="клавиатура",
            category="Клавиатуры для Apple",
        ),
        Product(
            article="053088",
            name=(
                "Клавиатура для Apple MacBook Air 11 A1370 / MacBook Air 11 A1465 "
                "(MID 2011 - MID 2017) (горизонтальный Enter / русская раскладка)"
            ),
            subject="клавиатура",
            category="Клавиатуры для Apple",
        ),
    ]
    db_session.add_all(products)
    db_session.commit()

    results = {
        product.article: generate_sku_for_product(db_session, product) for product in products
    }

    assert results["054554"].planned_sku == "OEM-FLX-HWE-P50-SUB-HN1AMBFL"
    assert results["057755"].planned_sku == "OEM-FLX-HWE-P50-SUB-HL2ABRFU"
    assert results["078624"].planned_sku == "OEM-FLX-IPDP12918-MIC-OR1"
    assert results["078629"].planned_sku == "OEM-FLX-IPDP12920-MIC-OR1"
    assert results["078636"].planned_sku == "OEM-FLX-IPDP12921-PWRBTN-CEL-OR1"
    assert results["078644"].planned_sku == "OEM-FLX-IPDP12922-PWRBTN-CEL-OR1"
    assert results["061382"].planned_sku == "OEM-FLX-RLM-C35-PWRBTN"
    assert results["054409"].planned_sku == "OEM-FLX-RLM-NZ50A-PWRBTN"
    assert results["075279"].planned_sku == "OEM-GLS-GGL-PIXEL10-MODG-MST-BLK-PR"
    assert results["078327"].planned_sku == "OEM-GLS-GGL-PIXEL9PX-MG-MST-BLK-PR"
    assert results["079177"].planned_sku == "OEM-GLS-LEN-LGO2Y700G5-PROT-Q25"
    assert results["081608"].planned_sku == "OEM-GLS-SMG-S105G-MODG-OCA-FEA-BLK"
    assert results["047641"].planned_sku == "OEM-GLS-HWE-P40LT5-CAMG-NFR-BLK"
    assert results["059913"].planned_sku == "OEM-GLS-INF-N12T-CAMG-NFR-BLK"
    assert results["038883"].planned_sku == "OEM-GLS-IPH5-MODG-FR-POL-BLK"
    assert results["064411"].planned_sku == "OEM-GLS-XMI-RPADW-CAMG-NFR-BLK"
    assert results["066595"].planned_sku == "OEM-GLS-XMI-RPADW-CAMG-FR-BLK"
    assert results["040449"].planned_sku == "OEM-IC-ASU-ZF4-SUB-CHG-MIC-VIB"
    assert results["054299"].planned_sku == "OEM-IC-XMI-PF4GT-SUB-CHG-MIC-OR1"
    assert results["079156"].planned_sku == "OEM-PRT-ASU-ALLYX24-BTN-ABXY"
    assert results["079159"].planned_sku == "OEM-PRT-ASU-ALLYX24-BTN-RBLB"
    assert results["081645"].planned_sku == "OEM-PRT-HWE-H400PCN-BCOV-BLK-OR1-CG"
    assert results["075575"].planned_sku == "OEM-PRT-IPHWU249-MID-TIT-OR1-ASM"
    assert results["048045"].planned_sku == "OEM-PRT-IPHXR-BCOV-YLW-PR-WIDE"
    assert results["053087"].planned_sku == "OEM-PRT-MBA11A1465-KBD-VERT"
    assert results["053088"].planned_sku == "OEM-PRT-MBA11A1465-KBD-HOR"


def test_generate_accessory_parts_and_universal_batteries(db_session) -> None:
    products = [
        Product(article="068842", name="Батарейки Kodak AA 4 шт.", subject="аккумулятор"),
        Product(
            article="063293",
            name="Батарейки Duracell LR6 AA 4 шт.",
            subject="аккумулятор",
        ),
        Product(
            article="063970",
            name="Батарейки GP Lithium CR2032 5 шт.",
            subject="аккумулятор",
        ),
        Product(
            article="064199",
            name="Аккумулятор универсальный 60 mAh (4*10*21 mm) (401021P)",
            subject="аккумулятор",
        ),
        Product(
            article="064185",
            name="Аккумулятор универсальный 60 mAh (5*10*12 mm) (501012P)",
            subject="аккумулятор",
        ),
        Product(
            article="053049",
            name="Вентилятор (кулер) для Apple MacBook Pro 13 A1278 / MacBook 13 A1342 (MID 2009 - MID 2012)",
            subject="динамик",
            category="Кулеры для ноутбуков",
        ),
        Product(
            article="053052",
            name="Вентилятор (кулер) для Apple MacBook Pro 15 A1286 (левый) (LATE 2008 - MID 2012)",
            subject="динамик",
            category="Кулеры для ноутбуков",
        ),
        Product(
            article="079108",
            name="Вентилятор охлаждения для Sony PS5 (17 лопастей)",
            subject="динамик",
            category="Кулеры для Sony",
        ),
        Product(
            article="065231",
            name="Матрица для Apple MacBook Pro 13 Retina A1502 (LATE 2013 - EARLY 2014) (AASP)",
            subject="матрица для ноутбука",
            category="Матрицы для ноутбуков",
            color="черный",
        ),
        Product(
            article="058431",
            name="Матрица в сборе для Apple MacBook Air 13 M1 Retina A2337 (LATE 2020) (серебристый) (AASP)",
            subject="матрица для ноутбука",
            category="Матрицы для ноутбуков",
        ),
        Product(
            article="066379",
            name="Матрица для Apple MacBook Air 13 M1 Retina A2337 (LATE 2020) (OEM)",
            subject="матрица для ноутбука",
            category="Матрицы для ноутбуков",
            color="черный",
        ),
        Product(
            article="065499",
            name="Форма дисплея для Apple iPad mini 4 (A1538/A1550) / iPad mini 5 (2019) (A2126/A2124/A2133)",
            subject="форма",
            category="Формы дисплеев",
        ),
        Product(
            article="065166",
            name="Форма дисплея Apple iPhone 13 / 13 Pro (металлическая)",
            subject="форма",
            category="Формы дисплеев",
        ),
        Product(
            article="051707",
            name="Форма вакуумного подогрева Apple iPhone 11 Pro (пластиковая)",
            subject="форма",
            category="Формы дисплеев",
        ),
        Product(
            article="038234",
            name="Подсветка дисплея для Apple iPhone 5c / iPhone 5s",
            subject="подсветка дисплея",
            category="Подсветки дисплеев",
        ),
        Product(
            article="050852",
            name="Подсветка дисплея для Apple iPhone 6s (без функции 3D Touch)",
            subject="подсветка дисплея",
            category="Подсветки дисплеев",
        ),
        Product(
            article="053083",
            name=(
                "Резиновые ножки для Apple MacBook Air 13 A1369 / MacBook Air 11 "
                "A1370 / MacBook Air 11 A1465 и др. (LATE 2010 - MID 2017) "
                "(комплект 4 шт.)"
            ),
            subject="запчасть",
            category="Запчасти для ноутбуков",
        ),
    ]
    db_session.add_all(products)
    db_session.commit()

    results = {
        product.article: generate_sku_for_product(db_session, product) for product in products
    }

    assert results["068842"].planned_sku == "OEM-BAT-UNV-AA-4-KODAK"
    assert results["063293"].planned_sku == "OEM-BAT-UNV-AA-4-DURACELL"
    assert results["063970"].planned_sku == "OEM-BAT-UNV-CR2032-5-GP"
    assert results["064199"].planned_sku == "OEM-BAT-UNV-60-401021P"
    assert results["064185"].planned_sku == "OEM-BAT-UNV-60-501012P"
    assert results["053049"].planned_sku == "OEM-PRT-MBP13A1342-FAN"
    assert results["053052"].planned_sku == "OEM-PRT-MBP15A1286-FAN-L"
    assert results["079108"].planned_sku == "OEM-PRT-SON-PS5-FAN-B17"
    assert results["065231"].planned_sku == "OEM-DSP-MBP13A1502-BLK-ASP-Y1314"
    assert results["058431"].planned_sku == "OEM-DSP-MBA13A2337-SLV-ASP-ASM-Y20"
    assert results["066379"].planned_sku == "OEM-DSP-MBA13A2337-MAT-BLK-OEM-Y20"
    assert results["065499"].planned_sku == "OEM-PRT-IPDMN5-DSPMOLD"
    assert results["065166"].planned_sku == "OEM-PRT-IPH13-DSPMOLD-MET"
    assert results["051707"].planned_sku == "OEM-PRT-IPH11P-VACMOLD-PL"
    assert results["038234"].planned_sku == "OEM-PRT-IPH5C-BKL"
    assert results["050852"].planned_sku == "OEM-PRT-IPH6S-BKL-N3D"
    assert results["053083"].planned_sku == "OEM-PRT-MBA13A1465-FEET-K4"


def test_generate_game_console_and_vr_accessory_sku(db_session) -> None:
    products = [
        Product(
            article="079051",
            name="Кабель для Meta Quest 2 VR (переключатель камеры) (нижний) (левый)",
            subject="кабель",
            category="Кабели для Meta",
        ),
        Product(
            article="079067",
            name="Дисплей для Meta Quest 3 VR",
            subject="дисплей",
            category="Дисплеи для Meta",
        ),
        Product(
            article="079120",
            name="Дисплей для Nintendo Switch 2 + тачскрин (черный)",
            subject="дисплей",
            category="Дисплеи для Nintendo",
        ),
        Product(
            article="079193",
            name="Шлейф для Nintendo Switch Joy-Con + слайдер (2 шт.) (левый + правый)",
            subject="шлейф",
            category="Шлейфы для Nintendo",
        ),
        Product(
            article="079149",
            name="Док-станция YCE-V279 для Steam Deck (10 в 1) + разъем M.2 SSD + вентилятор (синий)",
            subject="зарядка",
            category="Док-станции для Steam Deck",
        ),
        Product(
            article="079347",
            name="Комплект кнопок и стиков для контроллера Xbox Series X (черный)",
            subject="микросхема",
            category="Кнопки для Xbox",
        ),
        Product(
            article="079363",
            name="Набор для замены кнопок для Sony PS5 V3 (L1 , R1 , L2 , R2, Стики)",
            subject="неизвестно",
            category="Кнопки для Sony",
        ),
        Product(
            article="079278",
            name=(
                "Беспроводной геймпад GameSir Nova Lite T4N для Nintendo Switch "
                "(темно-фиолетовый)"
            ),
            subject="неизвестно",
            category="Джостики для Nintendo",
        ),
    ]
    db_session.add_all(products)
    db_session.commit()

    results = {
        product.article: generate_sku_for_product(db_session, product) for product in products
    }

    assert results["079051"].planned_sku == "OEM-CBL-META-Q2-CAMSW-DN-L"
    assert results["079067"].planned_sku == "OEM-DSP-META-Q3-STD"
    assert results["079120"].planned_sku == "OEM-DSP-NIN-SW2-BLK-TCH"
    assert results["079193"].planned_sku == "OEM-FLX-NIN-SW-JC-SLD-L-R-Q2"
    assert results["079149"].planned_sku == "OEM-CHR-STM-DECK-DOCK-YCEV279-BLU"
    assert results["079347"].planned_sku == "OEM-PRT-XBX-SX-BTNSTK-BLK"
    assert results["079363"].planned_sku == "OEM-PRT-SON-PS5-BTNSTK-V3"
    assert results["079278"].planned_sku == "OEM-PRT-NIN-SW-GPAD-GNLT4N-DPR-WL"


def test_generate_touchpad_magsafe_films_and_macbook_flex(db_session) -> None:
    products = [
        Product(
            article="044935",
            name=("Поляризационная пленка для Apple iPhone Xs Max / " "iPhone 11 Pro Max"),
            subject="пленка",
            category="Пленки для дисплеев",
        ),
        Product(
            article="052011",
            name="Пленка OCA для проклейки дисплея Apple iPhone 11 / iPhone Xr (150UM)",
            subject="пленка",
            category="Пленки для дисплеев",
        ),
        Product(
            article="055129",
            name="Пленка OCA для Apple iPhone Xs Max",
            subject="пленка",
            category="Пленки для дисплеев",
        ),
        Product(
            article="053055",
            name=(
                "Тачпад для Apple MacBook Pro 13 Retina A1502 "
                "(LATE 2013 - MID 2014) (в сборе со шлейфом)"
            ),
            subject="тачпад",
            category="Тачпады для ноутбуков",
        ),
        Product(
            article="058457",
            name=(
                "Тачпад для Apple MacBook Pro 13 Retina A1706/A1708/A1989/A2159 "
                "(LATE 2016, MID 2019) (серебристый)"
            ),
            subject="тачпад",
            category="Тачпады для ноутбуков",
        ),
        Product(
            article="065436",
            name=("Тачпад для Apple MacBook Pro 13 M1 Retina A2338 " "(MID 2022) (серый)"),
            subject="тачпад",
            category="Тачпады для ноутбуков",
        ),
        Product(
            article="065151",
            name="Магнит MagSafe для Apple iPhone 12 mini",
            subject="магнит",
            category="Запчасти для телефонов",
        ),
        Product(
            article="065152",
            name="Магнит MagSafe для Apple iPhone 13",
            subject="магнит",
            category="Запчасти для телефонов",
        ),
        Product(
            article="053058",
            name=(
                "Шлейф для Apple MacBook 12 Retina A1534 "
                "(EARLY 2016 - MID 2017) с комп. (на тачпад)"
            ),
            subject="шлейф",
            category="Шлейфы для ноутбуков",
        ),
        Product(
            article="053062",
            name=(
                "Шлейф для Apple MacBook Pro 13 A1278 "
                "(MID 2009 - MID 2010) с комп. (на жесткий диск)"
            ),
            subject="шлейф",
            category="Шлейфы для ноутбуков",
        ),
        Product(
            article="053064",
            name=("Шлейф для Apple MacBook Pro 13 A1278 " "(MID 2012) с комп. (на жесткий диск)"),
            subject="шлейф",
            category="Шлейфы для ноутбуков",
        ),
        Product(
            article="053075",
            name=(
                "Шлейф для Apple MacBook Air 13 A1466 "
                "(MID 2013 - MID 2017) (на I/O плату ввода/вывода)"
            ),
            subject="шлейф",
            category="Шлейфы для ноутбуков",
        ),
        Product(
            article="053077",
            name=(
                "Шлейф для Apple MacBook Air 13 A1369 / MacBook Air 13 A1466 "
                "(MID 2011 - MID 2012) с комп. (на тачпад)"
            ),
            subject="шлейф",
            category="Шлейфы для ноутбуков",
        ),
        Product(
            article="053078",
            name=("Шлейф для Apple MacBook Air 13 A1466 " "(MID 2012) (на I/O плату ввода/вывода)"),
            subject="шлейф",
            category="Шлейфы для ноутбуков",
        ),
    ]
    db_session.add_all(products)
    db_session.commit()

    results = {
        product.article: generate_sku_for_product(db_session, product) for product in products
    }

    assert results["044935"].planned_sku == "OEM-PRT-IPHXSM-POLFLM"
    assert results["052011"].planned_sku == "OEM-PRT-IPHXR-OCA-150UM"
    assert results["055129"].planned_sku == "OEM-PRT-IPHXSM-OCA"
    assert results["053055"].planned_sku == "OEM-PRT-MBP13A1502-TPD-ASM-Y1314"
    assert results["058457"].planned_sku == "OEM-PRT-MBP13A2159-TPD-SLV-Y1619"
    assert results["065436"].planned_sku == "OEM-PRT-MBP13A2338-TPD-GRY-Y22"
    assert results["065151"].planned_sku == "OEM-PRT-IPH12MN-MAGSAFE"
    assert results["065152"].planned_sku == "OEM-PRT-IPH13-MAGSAFE"
    assert results["053058"].planned_sku == "OEM-FLX-MB12A1534-TPD-Y1617"
    assert results["053062"].planned_sku == "OEM-FLX-MBP13A1278-HDD-Y0910"
    assert results["053064"].planned_sku == "OEM-FLX-MBP13A1278-HDD-Y12"
    assert results["053075"].planned_sku == "OEM-FLX-MBA13A1466-IO-Y1317"
    assert results["053077"].planned_sku == "OEM-FLX-MBA13A1466-TPD-Y1112"
    assert results["053078"].planned_sku == "OEM-FLX-MBA13A1466-IO-Y12"


def test_generate_matrix_film_and_mold_duplicate_pairs(db_session) -> None:
    products = [
        Product(
            article="065230",
            name="Матрица для Apple MacBook Pro 13 Retina A1502 (EARLY 2015) (AASP)",
            subject="матрица для ноутбука",
            category="Матрицы для ноутбуков",
            color="черный",
        ),
        Product(
            article="065231",
            name=(
                "Матрица для Apple MacBook Pro 13 Retina A1502 " "(LATE 2013 - EARLY 2014) (AASP)"
            ),
            subject="матрица для ноутбука",
            category="Матрицы для ноутбуков",
            color="черный",
        ),
        Product(
            article="053038",
            name=(
                "Матрица в сборе для Apple MacBook Pro 13 Retina A1502 "
                "(EARLY 2015) (серебристый) (AASP)"
            ),
            subject="матрица для ноутбука",
            category="Матрицы для ноутбуков",
        ),
        Product(
            article="064003",
            name=(
                "Матрица в сборе для Apple MacBook Pro 13 Retina A1502 "
                "(LATE 2013 - EARLY 2014) (серебристый) (AASP)"
            ),
            subject="матрица для ноутбука",
            category="Матрицы для ноутбуков",
        ),
        Product(
            article="063989",
            name=(
                "Матрица в сборе для Apple MacBook 12 Retina A1534 " "(EARLY 2015) (серый) (AASP)"
            ),
            subject="матрица для ноутбука",
            category="Матрицы для ноутбуков",
        ),
        Product(
            article="063993",
            name=(
                "Матрица в сборе для Apple MacBook 12 Retina A1534 "
                "(EARLY 2016 - MID 2017) (серый) (AASP)"
            ),
            subject="матрица для ноутбука",
            category="Матрицы для ноутбуков",
        ),
        Product(
            article="063990",
            name=(
                "Матрица в сборе для Apple MacBook 12 Retina A1534 "
                "(EARLY 2015) (золотистый) (AASP)"
            ),
            subject="матрица для ноутбука",
            category="Матрицы для ноутбуков",
        ),
        Product(
            article="063994",
            name=(
                "Матрица в сборе для Apple MacBook 12 Retina A1534 "
                "(EARLY 2016 - MID 2017) (золотистый) (AASP)"
            ),
            subject="матрица для ноутбука",
            category="Матрицы для ноутбуков",
        ),
        Product(
            article="063991",
            name=(
                "Матрица в сборе для Apple MacBook 12 Retina A1534 "
                "(EARLY 2015) (серебристый) (AASP)"
            ),
            subject="матрица для ноутбука",
            category="Матрицы для ноутбуков",
        ),
        Product(
            article="063992",
            name=(
                "Матрица в сборе для Apple MacBook 12 Retina A1534 "
                "(EARLY 2016 - MID 2017) (серебристый) (AASP)"
            ),
            subject="матрица для ноутбука",
            category="Матрицы для ноутбуков",
        ),
        Product(
            article="066743",
            name="Матрица для Apple MacBook 12 Retina A1534 (EARLY 2015) (AASP)",
            subject="матрица для ноутбука",
            category="Матрицы для ноутбуков",
            color="черный",
        ),
        Product(
            article="066744",
            name=("Матрица для Apple MacBook 12 Retina A1534 " "(EARLY 2016 - MID 2017) (AASP)"),
            subject="матрица для ноутбука",
            category="Матрицы для ноутбуков",
            color="черный",
        ),
        Product(
            article="051483",
            name=(
                "Поляризационная пленка для Apple iPad Air 2 12.9 " "(A1566 / A1567) (90 градусов)"
            ),
            subject="пленка",
            category="Пленки для дисплеев",
        ),
        Product(
            article="050865",
            name=("Поляризационная пленка для Apple iPad Air 2 9.7 " "(A1566/A1567) (90 градусов)"),
            subject="пленка",
            category="Пленки для дисплеев",
        ),
        Product(
            article="051026",
            name="Форма для склеивания дисплея и стекла Apple iPhone 11 Pro (резиновая)",
            subject="форма",
            category="Формы дисплеев",
        ),
        Product(
            article="051036",
            name="Форма склеивания дисплеев для Apple iPhone 11 Pro",
            subject="форма",
            category="Формы дисплеев",
        ),
    ]
    db_session.add_all(products)
    db_session.commit()

    results = {
        product.article: generate_sku_for_product(db_session, product) for product in products
    }

    assert results["065230"].planned_sku == "OEM-DSP-MBP13A1502-MAT-BLK-ASP-Y15"
    assert results["065231"].planned_sku == "OEM-DSP-MBP13A1502-BLK-ASP-Y1314"
    assert results["053038"].planned_sku == "OEM-DSP-MBP13A1502-SLV-ASP-ASM-Y15"
    assert results["064003"].planned_sku == "OEM-DSP-MBP13A1502-SLV-ASM-Y1314"
    assert results["063989"].planned_sku == "OEM-DSP-MB12A1534-GRY-ASP-ASM-Y15"
    assert results["063993"].planned_sku == "OEM-DSP-MB12A1534-GRY-ASP-ASM-Y1617"
    assert results["063990"].planned_sku == "OEM-DSP-MB12A1534-GLD-ASP-ASM-Y15"
    assert results["063994"].planned_sku == "OEM-DSP-MB12A1534-GLD-ASP-ASM-Y1617"
    assert results["063991"].planned_sku == "OEM-DSP-MB12A1534-SLV-ASP-ASM-Y15"
    assert results["063992"].planned_sku == "OEM-DSP-MB12A1534-SLV-ASP-ASM-Y1617"
    assert results["066743"].planned_sku == "OEM-DSP-MB12A1534-MAT-BLK-ASP-Y15"
    assert results["066744"].planned_sku == "OEM-DSP-MB12A1534-MAT-BLK-ASP-Y1617"
    assert results["051483"].planned_sku == "OEM-PRT-IPDA2-POLFLM-D90-S129"
    assert results["050865"].planned_sku == "OEM-PRT-IPDA2-POLFLM-D90-S97"
    assert results["051026"].planned_sku == "OEM-PRT-IPH11P-MOLD-RUB"
    assert results["051036"].planned_sku == "OEM-PRT-IPH11P-MOLD"


def test_generate_top_conflict_disambiguators(db_session) -> None:
    products = [
        Product(
            article="028645",
            name="Винты для Apple iPhone 5 (комплект 2 шт.) (серебристый)",
            subject="винты",
            category="Запчасти для телефонов",
        ),
        Product(
            article="052056",
            name="Винты для Apple iPhone 5с (комплект) (серебристый)",
            subject="винты",
            category="Запчасти для телефонов",
        ),
        Product(
            article="059028",
            name=(
                "Защитное стекло OG Glass (3 в 1) для Xiaomi 13 "
                "(2211133G) / 14 (23127PN0CG) (черный)"
            ),
            subject="защитное стекло",
            category="Защитные стекла для телефонов",
        ),
        Product(
            article="068035",
            name="Защитное стекло OG Glass (3 в 1) для Xiaomi 15 (24129PN74G) (черный)",
            subject="защитное стекло",
            category="Защитные стекла для телефонов",
        ),
        Product(
            article="075388",
            name="Корпус для Apple iPhone 17 Pro Max (eSIM) (синий) (Premium)",
            subject="корпус",
            category="Корпусы",
        ),
        Product(
            article="081795",
            name=(
                "Корпус для Apple iPhone 14 Pro Max в дизайне Apple "
                "iPhone 17 Pro Max (синий) (Premium)"
            ),
            subject="корпус",
            category="Корпусы",
        ),
        Product(
            article="078425",
            name="Корпус для Apple iPhone 17 Pro Max (SIM + eSIM) (бирюзовый) (Premium)",
            subject="корпус",
            category="Корпусы",
        ),
        Product(
            article="081802",
            name="Корпус для Apple iPhone 17 Pro Max (SIM + eSIM) (коричневый) (Premium)",
            subject="корпус",
            category="Корпусы",
        ),
        Product(
            article="078926",
            name=(
                "Материнская плата для Apple iPhone 14 / 14 Plus "
                "(SIM + eSIM) (без микросхем) (нижняя)"
            ),
            subject="плата",
            category="Платы и электронные компоненты",
        ),
        Product(
            article="078927",
            name=("Материнская плата для Apple iPhone 14 (SIM + eSIM) " "(128 Гб) (iCloud locked)"),
            subject="плата",
            category="Платы и электронные компоненты",
        ),
    ]
    db_session.add_all(products)
    db_session.commit()

    results = {
        product.article: generate_sku_for_product(db_session, product) for product in products
    }

    assert results["028645"].planned_sku == "OEM-PRT-IPH5-SCR-SLV-K2"
    assert results["052056"].planned_sku == "OEM-PRT-IPH5C-SCR-SLV"
    assert results["059028"].planned_sku == "OEM-GLS-XMI-13-PROT-3IN1-BLK"
    assert results["068035"].planned_sku == "OEM-GLS-XMI-15-PROT-3IN1-BLK"
    assert results["075388"].planned_sku == "OEM-PRT-IPH17PM-HOUS-BLU-PR-ESIM"
    assert results["081795"].planned_sku == "OEM-PRT-IPH14PM-HOUS-BLU-PR-D17PM"
    assert results["078425"].planned_sku == "OEM-PRT-IPH17PM-HOUS-TRQ-PR-SIME"
    assert results["081802"].planned_sku == "OEM-PRT-IPH17PM-HOUS-BRN-PR-SIME"
    assert results["078926"].planned_sku == "OEM-IC-IPH14-PCB-SIM-NOIC"
    assert results["078927"].planned_sku == "OEM-IC-IPH14-PCB-SIM-128G-ICL"


def test_generate_remaining_visible_conflict_markers(db_session) -> None:
    products = [
        Product(
            article="058915",
            name=("Рамка сенсорного экрана для Apple iPad Air 2 " "(A1566/A1567) (версия: Wi-Fi)"),
            subject="рамка",
            category="Рамки дисплеев",
        ),
        Product(
            article="058916",
            name=("Рамка сенсорного экрана для Apple iPad Air 2 " "(A1566/A1567) (версия: 4G)"),
            subject="рамка",
            category="Рамки дисплеев",
        ),
        Product(
            article="046335",
            name="Камера для Xiaomi Redmi Note 8T (M1908C3XG) (задняя) (2)",
            subject="камера",
            category="Камеры для телефонов",
        ),
        Product(
            article="050519",
            name="Камера для Xiaomi Redmi Note 8T (M1908C3XG) (задняя) (1)",
            subject="камера",
            category="Камеры для телефонов",
        ),
        Product(
            article="050951",
            name="Стекло модуля для Apple iPhone X / iPhone Xs (high copy)",
            subject="стекло модуля",
            category="Стекла модулей",
        ),
        Product(
            article="050952",
            name="Стекло модуля для Apple iPhone X / iPhone Xs (high copy+)",
            subject="стекло модуля",
            category="Стекла модулей",
        ),
        Product(
            article="057988",
            name="Защитное стекло UV для Samsung S918 Galaxy S23 Ultra",
            subject="защитное стекло",
            category="Защитные стекла для телефонов",
        ),
        Product(
            article="076959",
            name="Защитное стекло MOSSILY Full Glue для Samsung S918 Galaxy S23 Ultra",
            subject="защитное стекло",
            category="Защитные стекла для телефонов",
        ),
        Product(
            article="061139",
            name="Задняя крышка для Realme 9 5G (India) (белый)",
            subject="задняя крышка",
            category="Задние крышки",
        ),
        Product(
            article="053707",
            name="Задняя крышка для OnePlus 7T Pro (синий)",
            subject="задняя крышка",
            category="Задние крышки",
        ),
        Product(
            article="053520",
            name="Держатель сим-карты для ZTE Blade V30 (синий)",
            subject="держатель сим-карты",
            category="Держатели SIM-карт",
        ),
        Product(
            article="053535",
            name="Держатель сим-карты для ZTE Blade V30 Vita (синий)",
            subject="держатель сим-карты",
            category="Держатели SIM-карт",
        ),
        Product(
            article="053944",
            name="Задняя крышка для Tecno Camon 19 Neo (CH6i) (зеленый)",
            subject="задняя крышка",
            category="Задние крышки",
        ),
        Product(
            article="060207",
            name="Задняя крышка для Infinix Note 30 VIP (X6710) (голубой)",
            subject="задняя крышка",
            category="Задние крышки",
        ),
        Product(
            article="054550",
            name="Держатель сим-карты для Huawei P50 Pocket (BAL-L49) (черный)",
            subject="держатель сим-карты",
            category="Держатели SIM-карт",
        ),
        Product(
            article="061828",
            name="Задняя крышка для Infinix Hot 30 (X6831) (белый) (Low)",
            subject="задняя крышка",
            category="Задние крышки",
        ),
        Product(
            article="081004",
            name="Проклейка дисплейного модуля для Apple iPhone 12 Pro Max (2UUL) (комплект 5 шт.)",
            subject="проклейка",
            category="Проклейки дисплейных модулей",
        ),
        Product(
            article="043398",
            name="Задняя крышка для Huawei Mate 20 Pro (LYA-L29) (синий-сумеречный)",
            subject="задняя крышка",
            category="Задние крышки",
        ),
        Product(
            article="040751",
            name="Корпус для Apple iPhone 7 Plus (красно-черный)",
            subject="корпус",
            category="Корпусы",
        ),
        Product(
            article="061764",
            name="Задняя крышка для Samsung G780 Galaxy S20 FE (темно-красный)",
            subject="задняя крышка",
            category="Задние крышки",
        ),
        Product(
            article="035257",
            name="Задняя крышка для Sony D5503 Xperia Z1 Compact (белый)",
            subject="задняя крышка",
            category="Задние крышки",
        ),
        Product(
            article="052250",
            name="Защитное стекло тех. пак. для Sony E5803/E5823 Xperia Z5 Compact",
            subject="защитное стекло",
            category="Защитные стекла для телефонов",
        ),
        Product(
            article="050928",
            name="Сеточка динамика (слуховой) для Apple iPhone 11",
            subject="сетка динамика",
            category="Сеточки динамика",
        ),
        Product(
            article="063070",
            name="Сеточка динамика (полифонический) для Apple iPhone 11 с комп.",
            subject="сетка динамика",
            category="Сеточки динамика",
        ),
        Product(
            article="062999",
            name=(
                "Защитное стекло на заднюю камеру для Apple iPhone 14 Pro / "
                "iPhone 14 Pro Max (черный)"
            ),
            subject="защитное стекло",
            category="Защитные стекла для телефонов",
        ),
        Product(
            article="060284",
            name=(
                "Защитное стекло 3D тех. пак. для Google Pixel 7A "
                "(GWKK3/GHL1X/G0DZQ/G82U8) (черный)"
            ),
            subject="защитное стекло",
            category="Защитные стекла для телефонов",
        ),
        Product(
            article="046584",
            name="Держатель сим-карты для Huawei P40 Pro+ (ELS-N39) (черный)",
            subject="держатель сим-карты",
            category="Держатели SIM-карт",
        ),
        Product(
            article="040878",
            name="Держатель сим-карты для Huawei Nova 2 Plus (золотой)",
            subject="держатель сим-карты",
            category="Держатели SIM-карт",
        ),
        Product(
            article="060793",
            name="Держатель сим-карты для Vivo X70 Pro Plus (V2145A) (черный)",
            subject="держатель сим-карты",
            category="Держатели SIM-карт",
        ),
        Product(
            article="060757",
            name="Шлейф для Google Pixel 5 (межплатный) (тип 2)",
            subject="шлейф",
            category="Шлейфы для телефонов",
        ),
        Product(
            article="069061",
            name="Рамка дисплея для OPPO A96 (China Version) (черный)",
            subject="рамка дисплея",
            category="Рамки дисплеев",
        ),
        Product(
            article="050353",
            name="Задняя крышка для Nokia 7.1 Plus (черный)",
            subject="задняя крышка",
            category="Задние крышки",
        ),
        Product(
            article="068232",
            name="Держатель сим-карты для Infinix Zero X Pro (X6811) (голубой)",
            subject="держатель сим-карты",
            category="Держатели SIM-карт",
            color="черный",
        ),
        Product(
            article="047934",
            name="Держатель сим-карты для Xiaomi Mi 10T Lite 5G (M2007J17G) (синий)",
            subject="держатель сим-карты",
            category="Держатели SIM-карт",
        ),
        Product(
            article="051675",
            name="Держатель сим-карты для Xiaomi Redmi Note 10T 5G (M2103K19Y) (синий)",
            subject="держатель сим-карты",
            category="Держатели SIM-карт",
        ),
        Product(
            article="076350",
            name="Держатель сим-карты для OnePlus Nord CE 5 (CPH2719) (черный)",
            subject="держатель сим-карты",
            category="Держатели SIM-карт",
        ),
        Product(
            article="081141",
            name="Защитное стекло OG Glass (3 в 1) для Vivo V70 FE (V2550) (черный)",
            subject="защитное стекло",
            category="Защитные стекла для телефонов",
        ),
        Product(
            article="080610",
            name="Стекло модуля для Tecno Camon 50 Ultra (CN7c) + OCA (черный) (Premium)",
            subject="стекло модуля",
            category="Стекла модулей",
        ),
        Product(
            article="052147",
            name="Задняя крышка для OnePlus 8 Pro (зеленый матовый)",
            subject="задняя крышка",
            category="Задние крышки",
        ),
        Product(
            article="065204",
            name="Задняя крышка для OnePlus 8 Pro (зеленый глянец)",
            subject="задняя крышка",
            category="Задние крышки",
        ),
        Product(
            article="050593",
            name="Винты для Apple iPhone 7 / iPhone 7 Plus (комплект 2 шт.) (серебристый)",
            subject="винты",
            category="Винты",
        ),
        Product(
            article="076885",
            name="Комплект креплений платы для Apple iPhone 17 (SIM + eSIM)",
            subject="крепление",
            category="Крепления плат",
        ),
        Product(
            article="076884",
            name="Комплект креплений платы для Apple iPhone 17 (eSIM)",
            subject="крепление",
            category="Крепления плат",
        ),
        Product(
            article="078183",
            name="Средняя часть для Apple iPhone 17 (eSIM) (зеленый)",
            subject="корпус",
            category="Средние части",
        ),
        Product(
            article="082287",
            name="Защитное стекло OTAO 360° Privacy для Apple iPhone 16 Pro / iPhone 17 (антишпион)",
            subject="защитное стекло",
            category="Защитные стекла для телефонов",
        ),
        Product(
            article="082275",
            name="Защитное стекло OTAO 28° Privacy для Apple iPhone 16 Pro / iPhone 17 (антишпион)",
            subject="защитное стекло",
            category="Защитные стекла для телефонов",
        ),
        Product(
            article="082334",
            name="Защитное стекло для машинки OTAO Clear Glass для Apple iPhone 16 Pro / iPhone 17 (5 шт.)",
            subject="защитное стекло",
            category="Защитные стекла для телефонов",
        ),
        Product(
            article="069387",
            name="Защитное стекло OG Glass (3 в 1) для Nothing Phone 3a Lite (A001T) (черный)",
            subject="защитное стекло",
            category="Защитные стекла для телефонов",
        ),
        Product(
            article="077347",
            name="Стекло модуля для Huawei MatePad 11.5S PaperMatte Edition (TGR-W09) + OCA (черный) (Premium)",
            subject="стекло модуля",
            category="Стекла модулей",
        ),
        Product(
            article="072786",
            name="Задняя крышка для Apple iPhone 16 Pro (золотистый) (в сборе) (ORIG100) (SP)",
            subject="задняя крышка",
            category="Задние крышки",
        ),
        Product(
            article="079674",
            name="Дисплей для Huawei Honor Pad X9a 11.5 (ELN2-W29) + тачскрин (черный) (матовый) (ORIG)",
            subject="дисплей",
            category="Дисплеи",
        ),
        Product(
            article="076645",
            name="Держатель сим-карты для Huawei Honor X9d (MTN-NX1) (золотистый глянцевый)",
            subject="держатель сим-карты",
            category="Держатели SIM-карт",
        ),
        Product(
            article="072836",
            name="Динамик для Apple MacBook Pro 13 Retina A1706 (2016) правый малый (с разбора) (ORIG)",
            subject="динамик",
            category="Динамики для ноутбуков",
        ),
        Product(
            article="072827",
            name="Динамик для Apple MacBook Pro 13 A1278 (2011-2012) левый + правый с сабвуфером (ORIG)",
            subject="динамик",
            category="Динамики для ноутбуков",
        ),
        Product(
            article="080933",
            name=(
                "Корпус для Apple Watch 9 (45 мм) (черный) "
                "(в сборе с материнской платой) (ORIG100) (Снятый)"
            ),
            subject="корпус",
            category="Корпусы",
        ),
    ]
    db_session.add_all(products)
    db_session.commit()

    results = {
        product.article: generate_sku_for_product(db_session, product) for product in products
    }

    assert results["058915"].planned_sku == "OEM-PRT-IPDA2-FRM-WFI"
    assert results["058916"].planned_sku == "OEM-PRT-IPDA2-FRM-CEL"
    assert results["046335"].planned_sku == "OEM-CAM-XMI-RN8T-RCAM-N2"
    assert results["050519"].planned_sku == "OEM-CAM-XMI-RN8T-RCAM-N1"
    assert results["050951"].planned_sku == "OEM-GLS-IPHXS-MODG-HC"
    assert results["050952"].planned_sku == "OEM-GLS-IPHXS-MODG-HCP"
    assert results["057988"].planned_sku == "OEM-GLS-SMG-S23U-PROT-UV"
    assert results["076959"].planned_sku == "OEM-GLS-SMG-S23U-PROT-MOS-FGL"
    assert results["061139"].planned_sku == "OEM-PRT-RLM-95I-BCOV-WHT"
    assert results["053707"].planned_sku == "OEM-PRT-ONE-7TP-BCOV-BLU"
    assert results["053520"].planned_sku == "OEM-FLX-ZTE-BV30-SIMTRAY-BLU"
    assert results["053535"].planned_sku == "OEM-FLX-ZTE-BV30V-SIMTRAY-BLU"
    assert results["053944"].planned_sku == "OEM-PRT-TEC-C19N-BCOV-GRN"
    assert results["060207"].planned_sku == "OEM-PRT-INF-N30V-BCOV-CYN"
    assert results["054550"].planned_sku == "OEM-FLX-HWE-P50PK-SIMTRAY-BLK"
    assert results["061828"].planned_sku == "OEM-PRT-INF-H30-BCOV-WHT-LOW"
    assert results["081004"].planned_sku == "OEM-PRT-IPH12PM-DSP-ADH-2UUL-K5"
    assert results["043398"].planned_sku == "OEM-PRT-HWE-M20P-BCOV-TBLU"
    assert results["040751"].planned_sku == "OEM-PRT-IPH7PL-HOUS-RDBK"
    assert results["061764"].planned_sku == "OEM-PRT-SMG-S20FE-BCOV-DRED"
    assert results["035257"].planned_sku == "OEM-PRT-SON-Z1C-BCOV-WHT"
    assert results["052250"].planned_sku == "OEM-GLS-SON-Z5C-PROT-TPK"
    assert results["050928"].planned_sku == "OEM-PRT-IPH11-SPK-MESH-EAR"
    assert results["063070"].planned_sku == "OEM-PRT-IPH11-SPK-MESH-POLY-KIT"
    assert results["062999"].planned_sku == "OEM-GLS-IPH14PM-CAMG-BLK"
    assert results["060284"].planned_sku == "OEM-GLS-GGL-PIXEL7A-PROT-TPK-BLK"
    assert results["046584"].planned_sku == "OEM-FLX-HWE-P40PP-SIMTRAY-BLK"
    assert results["040878"].planned_sku == "OEM-FLX-HWE-N2P-SIMTRAY-GLD"
    assert results["060793"].planned_sku == "OEM-FLX-VVO-X70PP-SIMTRAY-BLK"
    assert results["060757"].planned_sku == "OEM-FLX-GGL-PIXEL5-SUB-T2"
    assert results["069061"].planned_sku == "OEM-PRT-OPP-A96-DFRM-BLK-CN"
    assert results["050353"].planned_sku == "OEM-PRT-NOK-71P-BCOV-BLK"
    assert results["068232"].planned_sku == "OEM-FLX-INF-X6811-SIMTRAY-CYN"
    assert results["047934"].planned_sku == "OEM-FLX-XMI-M10TL5-SIMTRAY-BLU"
    assert results["051675"].planned_sku == "OEM-FLX-XMI-RN10T5-SIMTRAY-BLU"
    assert results["076350"].planned_sku == "OEM-FLX-ONE-NCE5-SIMTRAY-BLK"
    assert results["081141"].planned_sku == "OEM-GLS-VVO-V70FE-PROT-3IN1-BLK"
    assert results["080610"].planned_sku == "OEM-GLS-TEC-C50U-MODG-OCA-BLK-PR"
    assert results["052147"].planned_sku == "OEM-PRT-ONE-8P-BCOV-GRN-MAT"
    assert results["065204"].planned_sku == "OEM-PRT-ONE-8P-BCOV-GRN-GLS"
    assert results["050593"].planned_sku == "OEM-PRT-IPH7PL-SCR-SLV-K2"
    assert results["076885"].planned_sku == "OEM-PRT-IPH17-MNT-SIME"
    assert results["076884"].planned_sku == "OEM-PRT-IPH17-MNT-ESIM"
    assert results["078183"].planned_sku == "OEM-PRT-IPH17-MID-GRN-ESIM"
    assert results["082287"].planned_sku == "OEM-GLS-IPH17-PROT-PRV-D360"
    assert results["082275"].planned_sku == "OEM-GLS-IPH17-PROT-PRV-D28"
    assert results["082334"].planned_sku == "OEM-GLS-IPH17-PROT-Q5-CLR"
    assert results["069387"].planned_sku == "OEM-GLS-NOT-PH3AL-PROT-3IN1-BLK"
    assert results["077347"].planned_sku == "OEM-GLS-HWE-MP115SPM-MG-OCA-BLK-PR"
    assert results["072786"].planned_sku == "OEM-PRT-IPH16P-BCOV-GLD-OR1-ASM-SP"
    assert results["079674"].planned_sku == "OEM-DSP-HWE-HPX9A-BLK-OR-MAT"
    assert results["076645"].planned_sku == "OEM-FLX-HWE-HX9D-SIMTRAY-GLD-GLS"
    assert results["072836"].planned_sku == "OEM-PRT-MBP13A1706-SPK-R-OR-SM"
    assert results["072827"].planned_sku == "OEM-PRT-MBP13A1278-SPK-L-OR-LR-SUB"
    assert results["080933"].planned_sku == "OEM-PRT-IPHW945-HOU-BLK-OR1-ASM-PCB"


def test_generate_ipad_home_button_uses_short_ipad_device_code(db_session) -> None:
    product = Product(
        article="1001-ipad3-home",
        name="Кнопка (толкатель) Home для Apple iPad 3 (A1416/A1430) (черный)",
        subject="кнопки",
        category="Кнопки HOME для Apple iPad",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.category_code == "PRT"
    assert result.device_code == "IPD3"
    assert result.planned_sku == "OEM-PRT-IPD3-BTN-BLK"


def test_generate_macbook_protective_glass_ignores_accessory_brand_words(db_session) -> None:
    product = Product(
        article="1001-macbook-otao",
        name=(
            "Защитное стекло магнитное OTAO Anti-Blue Light для Apple Macbook Air 15.3 "
            "(A2941/A3114)"
        ),
        subject="защитное стекло",
        category="Защитные стекла для телефонов",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "MBA153"
    assert result.planned_sku == "OEM-GLS-MBA153-PROT-BLU"


def test_generate_oneplus_ace_2v_glass_uses_compact_device_code(db_session) -> None:
    product = Product(
        article="1001-oneplus-ace2v-glass",
        name="Защитное стекло OG Glass (3 в 1) для OnePlus Ace 2V (PHP110) (черный)",
        subject="защитное стекло",
        category="Защитные стекла для телефонов",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "ONE-A2V"
    assert result.planned_sku == "OEM-GLS-ONE-A2V-PROT-3IN1-BLK"


def test_generate_huawei_y8p_camera_keeps_model_out_of_camera_specs(db_session) -> None:
    product = Product(
        article="1001-huawei-y8p-camera",
        name="Камера для Huawei Y8p (AQM-LX1) (48 MP) (задняя) (ORIG100)",
        subject="камера",
        category="Камеры для телефонов",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "HWE-Y8P"
    assert result.planned_sku == "OEM-CAM-HWE-Y8P-RCAM-48MP-OR1"


def test_generate_sony_tablet_z3_flex_uses_compact_device_code(db_session) -> None:
    product = Product(
        article="1001-sony-z3-flex",
        name="Шлейф для Sony Xperia Tablet Z3 Compact 8.0 с комп. + микрофон",
        subject="шлейф",
        category="Шлейфы для планшетов",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "SON-XTZ3C8"
    assert result.key_code == "MIC"
    assert result.planned_sku == "OEM-FLX-SON-XTZ3C8-MIC"


def test_generate_itel_vision_board_uses_compact_device_code(db_session) -> None:
    product = Product(
        article="1001-itel-vision3-board",
        name="Нижняя плата для Itel Vision 3 (S661LPN) с комп. + разъем зарядки + микрофон",
        subject="плата",
        category="Нижние платы для телефонов",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "ITE-V3"
    assert result.planned_sku == "OEM-IC-ITE-V3-SUB-CHG-MIC"


def test_generate_multi_brand_chip_uses_universal_device_code(db_session) -> None:
    product = Product(
        article="038364",
        name="Микросхема контроллер заряда (SMB1360) для Asus / Sony / Xiaomi (ORIG100)",
        subject="микросхема",
        category="Микросхемы для телефонов",
        vid_nomenklatury="Платы и электронные компоненты",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "UNV"
    assert result.key_code == "SMB1360-OR1"
    assert result.planned_sku == "OEM-IC-UNV-SMB1360-OR1"


def test_generate_asus_f52_k72_keyboard_uses_family_device_code(db_session) -> None:
    product = Product(
        article="049713",
        name=(
            "Клавиатура для Asus F52 / F90 / K50 / K51 / K60I / K60IJ / K61 / K62 / "
            "K70 / K71 / K72 / P50 / X5DIJ (черный) (в рамке)"
        ),
        subject="клавиатура",
        category="Клавиатуры для ноутбуков",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "ASU-F52K72"
    assert result.key_code == "KBD-FR-BLK"
    assert result.planned_sku == "OEM-PRT-ASU-F52K72-KBD-FR-BLK"


def test_generate_asus_k52_n60_keyboard_uses_family_device_code(db_session) -> None:
    product = Product(
        article="049837",
        name=(
            "Клавиатура для Asus K52 / K53 / K54 / N50 / N51 / N52 / N53 / "
            "N60 и др. (черный) (в рамке)"
        ),
        subject="клавиатура",
        category="Клавиатуры для ноутбуков",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "ASU-K52N60"
    assert result.key_code == "KBD-FR-BLK"
    assert result.planned_sku == "OEM-PRT-ASU-K52N60-KBD-FR-BLK"


def test_generate_display_sku_marks_duplicate_card_for_review(db_session) -> None:
    product = Product(
        article="1001-dup",
        name="Дубль! Дисплей для Apple iPhone 6 (в сборе с тачскрином) (белый) (переклейка) (ORIG)",
        subject="Дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "manual_review"
    assert result.reasons == ["duplicate_card"]


def test_generate_samsung_display_sku_uses_short_dev_and_frame_rev(db_session) -> None:
    product = Product(
        article="1001-samsung-short",
        name="Дисплей для Samsung G780 Galaxy S20 FE + тачскрин (зеленый) (в рамке) (ORIG100) (SP)",
        subject="Дисплей",
        color="зеленый",
        display_type="Super AMOLED",
        quality_raw="ORIG100",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "SMG-S20FE"
    assert result.key_code == "AMD-GRN-OR1"
    assert result.rev == "F"
    assert result.planned_sku == "OEM-DSP-SMG-S20FE-AMD-GRN-OR1-F"


def test_generate_samsung_display_sku_supports_flip_internal_and_short_dev(db_session) -> None:
    product = Product(
        article="1001-samsung-flip",
        name="Дисплей для Samsung F721 Galaxy Z Flip 4 + тачскрин (внутренний) (черный) (ORIG100) (SP)",
        subject="Дисплей",
        color="черный",
        display_type="Dynamic AMOLED",
        quality_raw="ORIG100",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "SMG-ZF4"
    assert result.rev == "SP-IN"
    assert result.planned_sku == "OEM-DSP-SMG-ZF4-AMD-BLK-OR1-SP-IN"


def test_generate_samsung_display_sku_supports_small_size_rev(db_session) -> None:
    product = Product(
        article="1001-samsung-ss",
        name="Дисплей для Samsung A705 Galaxy A70 + тачскрин (черный) (в рамке) (OLED) (Small Size)",
        subject="Дисплей",
        color="черный",
        quality_raw="High (Small Size)",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "SMG-A705"
    assert result.key_code == "OLD-BLK-CPH"
    assert result.rev == "FS"
    assert result.planned_sku == "OEM-DSP-SMG-A705-OLD-BLK-CPH-FS"


def test_generate_samsung_watch_display_keeps_samsung_device_code(db_session) -> None:
    product = Product(
        article="1001-samsung-watch6",
        name="Дисплей для Samsung R960 Galaxy Watch 6 Classic (47 мм) + тачскрин (черный) (ORIG)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "SMG-R960"
    assert result.planned_sku == "OEM-DSP-SMG-R960-BLK-OR"


def test_generate_samsung_display_adds_length_rev_for_a03_family(db_session) -> None:
    product = Product(
        article="1001-samsung-a035-164",
        name="Дисплей для Samsung A035 Galaxy A03 + тачскрин (черный) (в рамке) (ORIG100) (SP) (164mm)",
        subject="дисплей",
        color="черный",
        quality_raw="ORIG100",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "SMG-A035"
    assert result.rev == "F-64"
    assert result.planned_sku == "OEM-DSP-SMG-A035-BLK-OR1-F-64"


def test_generate_apple_watch_family_uses_compact_device_code(db_session) -> None:
    product = Product(
        article="1001-apple-watch-family",
        name="Дисплей для Apple Watch 5 (40 мм) / Watch SE 2020 (40 мм) / Watch SE 2022 (40 мм) + тачскрин (черный) (ORIG)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "IPHW5SE40"
    assert result.planned_sku == "OEM-DSP-IPHW5SE40-BLK-OR"


def test_generate_apple_ipod_touch_family_uses_compact_device_code(db_session) -> None:
    product = Product(
        article="1001-apple-ipod56",
        name="Дисплей для Apple iPod Touch 5 / iPod Touch 6 (в сборе с тачскрином) (черный) (Medium)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "IPOD56"
    assert result.planned_sku == "OEM-DSP-IPOD56-BLK-CPM"


def test_generate_apple_compatible_ipad_pro_11_uses_compact_device_code(db_session) -> None:
    product = Product(
        article="1001-apple-ipad11-compatible",
        name='Дисплей совместим с iPad Pro 11,0" A1980 / A2013 / A1934 (2018) с тачскрином (Черный) (Оригинал восстановленный) :',
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "IPDP1118"
    assert result.planned_sku == "OEM-DSP-IPDP1118-BLK-RF"


def test_generate_samsung_display_sku_uses_precise_s_series_dev(db_session) -> None:
    product = Product(
        article="1001-samsung-s10e",
        name="Дисплей для Samsung G970 Galaxy S10e + тачскрин (серебристый) (в рамке) (OLED)",
        subject="Дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "SMG-S10E"
    assert result.key_code == "OLD-SLV"
    assert result.planned_sku == "OEM-DSP-SMG-S10E-OLD-SLV-FR"


def test_generate_samsung_display_sku_distinguishes_s24_from_s24_plus(db_session) -> None:
    product = Product(
        article="1001-samsung-s24",
        name="Дисплей для Samsung S921 Galaxy S24 + тачскрин (черный) (ORIG100) (SP)",
        subject="дисплей",
        display_type="Dynamic AMOLED",
        quality_raw="ORIG100",
        color="черный",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "SMG-S24"
    assert result.planned_sku == "OEM-DSP-SMG-S24-AMD-BLK-OR1-SP"


def test_generate_samsung_display_sku_distinguishes_s21_from_s21_plus(db_session) -> None:
    product = Product(
        article="1001-samsung-s21",
        name="Дисплей для Samsung G991 Galaxy S21 + тачскрин (черный) (в рамке) (OLED) (Full Size)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "SMG-S21"
    assert result.planned_sku == "OEM-DSP-SMG-S21-OLD-BLK-FF"


def test_generate_samsung_display_sku_distinguishes_s25_from_s25_plus(db_session) -> None:
    product = Product(
        article="1001-samsung-s25",
        name="Дисплей для Samsung S931 Galaxy S25 + тачскрин (черный) (ORIG100) (SP)",
        subject="дисплей",
        display_type="Dynamic AMOLED",
        quality_raw="ORIG100",
        color="черный",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "SMG-S25"
    assert result.planned_sku == "OEM-DSP-SMG-S25-AMD-BLK-OR1-SP"


def test_generate_samsung_display_sku_adds_frame_and_connector_rev(db_session) -> None:
    product = Product(
        article="1001-samsung-a015-wide",
        name="Дисплей для Samsung A015 Galaxy A01 + тачскрин (черный) (Medium) (широкий коннектор) (в рамке)",
        subject="дисплей",
        color="черный",
        quality_raw="Medium",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "SMG-A015"
    assert result.rev == "FR-WC"
    assert result.planned_sku == "OEM-DSP-SMG-A015-BLK-CPM-FR-WC"


def test_generate_samsung_display_sku_distinguishes_blue_and_cyan(db_session) -> None:
    product = Product(
        article="1001-samsung-zf7-cyan",
        name="Дисплей для Samsung F766 Galaxy Z Flip 7 + тачскрин (внутренний) (голубой) (в рамке) (ORIG100) (SP)",
        subject="дисплей",
        display_type="Dynamic AMOLED",
        quality_raw="ORIG100",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.key_code == "AMD-CYN-OR1"
    assert result.rev == "FSI"
    assert result.planned_sku == "OEM-DSP-SMG-ZF7-AMD-CYN-OR1-FSI"


def test_generate_apple_display_sku_marks_clean_variant(db_session) -> None:
    product = Product(
        article="1001-apple-clean",
        name="Дисплей для Apple iPhone 16 + тачскрин + ALS шлейф (черный) (ORIG100) (Снятый) (CLEAN)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.rev == "AC"
    assert result.planned_sku == "OEM-DSP-IPH16-BLK-OR1-AC"


def test_generate_google_pixel_8a_has_distinct_device_code(db_session) -> None:
    product = Product(
        article="1001-google-pixel8a",
        name="Дисплей для Google Pixel 8A (в сборе с тачскрином) (черный) (In-Cell) (Low)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "GGL-PIXEL8A"
    assert result.planned_sku == "OEM-DSP-GGL-PIXEL8A-INL-BLK-CPL"


def test_generate_google_pixel_4a_5g_has_distinct_device_code(db_session) -> None:
    product = Product(
        article="1001-google-pixel4a5",
        name="Дисплей для Google Pixel 4A 5G (в сборе с тачскрином) (черный) (In-Cell) (Low)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "GGL-PIXEL4A5"
    assert result.planned_sku == "OEM-DSP-GGL-PIXEL4A5-INL-BLK-CPL"


def test_generate_vivo_fold_outer_uses_outer_rev(db_session) -> None:
    product = Product(
        article="1001-vivo-xfold5-outer",
        name="Дисплей для Vivo X Fold 5 (V2429) + тачскрин (внешний) (черный) (ORIG100)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "VVO-XFOLD5"
    assert result.rev == "OT"
    assert result.planned_sku == "OEM-DSP-VVO-XFOLD5-BLK-OR1-OT"


def test_generate_vivo_x200_ultra_has_distinct_device_code(db_session) -> None:
    product = Product(
        article="1001-vivo-x200u",
        name="Дисплей для Vivo X200 Ultra (V2454A) + тачскрин (черный) (In-Cell)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "VVO-X200U"
    assert result.planned_sku == "OEM-DSP-VVO-X200U-INL-BLK"


def test_generate_vivo_iqoo_12_uses_short_device_code(db_session) -> None:
    product = Product(
        article="1001-vivo-iq12",
        name="Дисплей для Vivo iQOO 12 (V2307A) + тачскрин (черный) (ORIG)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "VVO-IQ12"
    assert result.planned_sku == "OEM-DSP-VVO-IQ12-BLK-OR"


def test_generate_vivo_v30_lite_has_distinct_device_code(db_session) -> None:
    product = Product(
        article="1001-vivo-v30lite",
        name="Дисплей для Vivo V30 Lite 4G (V2342) (в сборе с тачскрином) (черный) (OLED) (High)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "VVO-V30L"
    assert result.planned_sku == "OEM-DSP-VVO-V30L-OLD-BLK"


def test_generate_vivo_x200_fe_has_distinct_device_code(db_session) -> None:
    product = Product(
        article="1001-vivo-x200fe",
        name="Дисплей для Vivo X200 FE (V2503) + тачскрин (черный) (In-Cell)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "VVO-X200F"
    assert result.planned_sku == "OEM-DSP-VVO-X200F-INL-BLK"


def test_generate_vivo_fold_inner_frame_uses_compact_fi_rev(db_session) -> None:
    product = Product(
        article="1001-vivo-xfold5-inner",
        name="Дисплей для Vivo X Fold 5 (V2429) + тачскрин (внутренний) (черный) (в рамке) (ORIG100)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.rev == "FI"
    assert result.planned_sku == "OEM-DSP-VVO-XFOLD5-BLK-OR1-FI"


def test_generate_tecno_frame_display_adds_fr_rev(db_session) -> None:
    product = Product(
        article="1001-tecno-pova5p",
        name="Дисплей для Tecno Pova 5 Pro 5G (LH8n) + тачскрин (черный) (в рамке) (ORIG)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "TEC-POVA5P5"
    assert result.rev == "FR"
    assert result.planned_sku == "OEM-DSP-TEC-POVA5P5-BLK-OR-FR"


def test_generate_tecno_pova6_pro_frame_sp_uses_compact_f_rev(db_session) -> None:
    product = Product(
        article="1001-tecno-pova6p-sp",
        name="Дисплей для Tecno Pova 6 Pro 5G (LI9) + тачскрин (черный) (в рамке) (ORIG100) (SP)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "TEC-POVA6P5"
    assert result.rev == "F"
    assert result.planned_sku == "OEM-DSP-TEC-POVA6P5-BLK-OR1-F"


def test_generate_tecno_camon_30s_pro_uses_short_device_code(db_session) -> None:
    product = Product(
        article="1001-tecno-c30sp",
        name="Дисплей для Tecno Camon 30S Pro (CLA6) (в сборе с тачскрином) (черный) (In-Cell) (Low)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "TEC-C30SP"
    assert result.planned_sku == "OEM-DSP-TEC-C30SP-INL-BLK-CPL"


def test_generate_tecno_camon_30_premier_has_distinct_device_code(db_session) -> None:
    product = Product(
        article="1001-tecno-c30pr",
        name="Дисплей для Tecno Camon 30 Premier 5G (CL9) + тачскрин (черный) (ORIG)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "TEC-C30PR5"
    assert result.planned_sku == "OEM-DSP-TEC-C30PR5-BLK-OR"


def test_generate_huawei_matepad_papermatte_adds_pm_rev(db_session) -> None:
    product = Product(
        article="1001-huawei-matepad-pm",
        name="Дисплей для Huawei MatePad 11.5S PaperMatte Edition (TGR-W09) + тачскрин (черный) (ORIG)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "HWE-MP115S"
    assert result.rev == "PM"
    assert result.planned_sku == "OEM-DSP-HWE-MP115S-BLK-OR-PM"


def test_generate_huawei_pad_x8_lite_has_distinct_device_code(db_session) -> None:
    product = Product(
        article="1001-huawei-padx8-lite",
        name="Дисплей для Huawei Honor Pad X8 Lite 9.7 (AGM-W09HN) + тачскрин (черный)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "HWE-HPX8L"
    assert result.planned_sku == "OEM-DSP-HWE-HPX8L-BLK"


def test_generate_huawei_p40_lite_e_has_distinct_device_code(db_session) -> None:
    product = Product(
        article="1001-huawei-p40-lite-e",
        name="Дисплей для Huawei P40 Lite E (ART-L29) / Honor 9C (AKA-L29) / Honor Play 3 / Y7p (ART-L28) + тачскрин (черный) (ORIG)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "HWE-P40LE"
    assert result.planned_sku == "OEM-DSP-HWE-P40LE-BLK-OR"


def test_generate_huawei_pad5_home_hole_adds_rev(db_session) -> None:
    product = Product(
        article="1001-huawei-pad5-home",
        name="Дисплей для Huawei Honor Pad 5 10.1 (AGS2-AL00HN) / MediaPad T5 10.1 (AGS2-AL00HN) (с отверстием под кнопку Home) + тачскрин (черный)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "HWE-HPAD5"
    assert result.rev == "HM"
    assert result.planned_sku == "OEM-DSP-HWE-HPAD5-BLK-HM"


def test_generate_samsung_display_sku_distinguishes_flip_fe_and_outer_screen(db_session) -> None:
    product = Product(
        article="1001-samsung-zf7fe",
        name="Дисплей для Samsung F761 Galaxy Z Flip 7 FE + тачскрин (внешний) (черный) (ORIG100) (SP)",
        subject="Дисплей",
        display_type="Super AMOLED",
        quality_raw="ORIG100",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "SMG-ZF7FE"
    assert result.rev == "SP-OT"
    assert result.planned_sku == "OEM-DSP-SMG-ZF7FE-AMD-BLK-OR1-SP-OT"


def test_generate_samsung_display_sku_parses_color_and_quality_from_name(db_session) -> None:
    product = Product(
        article="1001-samsung-name-only",
        name="Дисплей Samsung Galaxy A5 (2015) | SM-A500 в сборе с тачскрином (Белый) (Premium)",
        subject="Дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "SMG-A500"
    assert result.key_code == "WHT-CPH"
    assert result.planned_sku == "OEM-DSP-SMG-A500-WHT-CPH"


def test_generate_samsung_display_sku_parses_cof_from_name(db_session) -> None:
    product = Product(
        article="1001-samsung-cof",
        name="Дисплей для Samsung J330 Galaxy J3 (2017) + тачскрин (синий) (COF)",
        subject="Дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "SMG-J330"
    assert result.key_code == "COF-BLU"
    assert result.planned_sku == "OEM-DSP-SMG-J330-COF-BLU"


def test_generate_samsung_display_sku_prefers_display_category_over_bad_category(
    db_session,
) -> None:
    product = Product(
        article="1001-samsung-bad-category",
        name="Дисплей для Samsung A256E Galaxy A25 5G + тачскрин (черный) (In-Cell)",
        subject="дисплей",
        category="Аккумуляторы для телефонов",
        vid_nomenklatury="Дисплеи/сенсор/стекло",
        display_type="Super AMOLED",
        quality_raw="Low",
        color="черный",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.category_code == "DSP"
    assert result.device_code == "SMG-A256E"
    assert result.key_code == "INL-BLK-CPL"
    assert result.planned_sku == "OEM-DSP-SMG-A256E-INL-BLK-CPL"


def test_generate_samsung_display_sku_uses_short_tablet_code(db_session) -> None:
    product = Product(
        article="1001-samsung-tablet",
        name="Дисплей для Samsung X700/X706 Galaxy Tab S8 11.0 + тачскрин (черный)",
        subject="дисплей",
        quality_raw="Аналог (TFT)",
        color="черный",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "SMG-X700"
    assert result.key_code == "TFT-BLK-CPM"
    assert result.planned_sku == "OEM-DSP-SMG-X700-TFT-BLK-CPM"


def test_generate_samsung_display_sku_falls_back_to_std_for_old_models(db_session) -> None:
    product = Product(
        article="1001-samsung-std",
        name="Дисплей Samsung S3850",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "SMG-S3850"
    assert result.key_code == "STD"
    assert result.planned_sku == "OEM-DSP-SMG-S3850-STD"


def test_generate_xiaomi_display_sku_uses_short_commercial_model_code(db_session) -> None:
    product = Product(
        article="1001-xiaomi-short",
        name="Дисплей для Xiaomi Poco F6 Pro (23113RKC6G) + тачскрин (черный) (ORIG)",
        subject="дисплей",
        display_type="AMOLED",
        quality_raw="ORIG",
        color="черный",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "XMI-PF6P"
    assert result.key_code == "AMD-BLK-OR"
    assert result.planned_sku == "OEM-DSP-XMI-PF6P-AMD-BLK-OR"


def test_generate_xiaomi_display_sku_handles_family_models(db_session) -> None:
    product = Product(
        article="1001-xiaomi-family",
        name="Дисплей для Xiaomi 12T (22071212AG) / 12T Pro (22081212UG) + тачскрин (черный) (ORIG)",
        subject="дисплей",
        display_type="OLED",
        quality_raw="ORIG",
        color="черный",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "XMI-12TP"
    assert result.key_code == "OLD-BLK-OR"
    assert result.planned_sku == "OEM-DSP-XMI-12TP-OLD-BLK-OR"


def test_generate_xiaomi_display_family_gets_rev_when_conflicts_with_specific_model(
    db_session,
) -> None:
    specific = Product(
        article="1001-xiaomi-13tp-specific",
        name="Дисплей для Xiaomi 13T Pro (23078PND5G) (в сборе с тачскрином) (черный) (In-Cell) (Low)",
        subject="дисплей",
    )
    family = Product(
        article="1001-xiaomi-13tp-family",
        name="Дисплей для Xiaomi 13T (2306EPN60G) / 13T Pro (23078PND5G) + тачскрин (черный) (In-Cell)",
        subject="дисплей",
        quality_raw="Low",
    )
    db_session.add_all([specific, family])
    db_session.commit()
    db_session.refresh(specific)
    db_session.refresh(family)

    specific_result = generate_sku_for_product(db_session, specific)
    apply_sku_generation_result(db_session, specific, specific_result)
    db_session.commit()
    db_session.refresh(family)

    family_result = generate_sku_for_product(db_session, family)

    assert specific_result.status == "generated"
    assert specific_result.planned_sku == "OEM-DSP-XMI-13TP-INL-BLK-CPL"
    assert family_result.status == "generated"
    assert family_result.rev == "13T"
    assert family_result.planned_sku == "OEM-DSP-XMI-13TP-INL-BLK-CPL-13T"


def test_generate_xiaomi_display_family_gets_rev_for_note_14_conflict(
    db_session,
) -> None:
    specific = Product(
        article="1001-xiaomi-px7-specific",
        name="Дисплей для Xiaomi Poco X7 (24095PCADG) (в сборе с тачскрином) (черный) (In-Cell) (Low)",
        subject="дисплей",
    )
    family = Product(
        article="1001-xiaomi-px7-family",
        name="Дисплей для Xiaomi Poco X7 (24095PCADG) / Redmi Note 14 Pro 5G (24090RA29G) / Note 14 Pro+ 5G (24115RA8EG) + тачскрин (черный) (In-Cell)",
        subject="дисплей",
        quality_raw="Low",
    )
    db_session.add_all([specific, family])
    db_session.commit()
    db_session.refresh(specific)
    db_session.refresh(family)

    specific_result = generate_sku_for_product(db_session, specific)
    apply_sku_generation_result(db_session, specific, specific_result)
    db_session.commit()
    db_session.refresh(family)

    family_result = generate_sku_for_product(db_session, family)

    assert specific_result.status == "generated"
    assert specific_result.planned_sku == "OEM-DSP-XMI-PX7-INL-BLK-CPL"
    assert family_result.status == "generated"
    assert family_result.rev == "RN14"
    assert family_result.planned_sku == "OEM-DSP-XMI-PX7-INL-BLK-CPL-RN14"


def test_generate_xiaomi_display_sku_falls_back_to_std_for_old_models(db_session) -> None:
    product = Product(
        article="1001-xiaomi-std",
        name="Дисплей для Xiaomi MiPad 2 + тачскрин (черный)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "XMI-MPAD2"
    assert result.key_code == "BLK"
    assert result.planned_sku == "OEM-DSP-XMI-MPAD2-BLK"


def test_generate_xiaomi_display_sku_handles_plain_numeric_flagship(db_session) -> None:
    product = Product(
        article="1001-xiaomi-plain",
        name="Дисплей для Xiaomi 13 (2211133G) (в сборе с тачскрином) (черный) (OLED) (High)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "XMI-13"
    assert result.key_code == "OLD-BLK"
    assert result.planned_sku == "OEM-DSP-XMI-13-OLD-BLK"


def test_generate_xiaomi_display_sku_does_not_take_red_from_redmi(db_session) -> None:
    product = Product(
        article="1001-xiaomi-redmi",
        name="Дисплей для Xiaomi Redmi Note 5A Prime (в сборе с тачскрином) (черный) (COF)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "XMI-RN5AP"
    assert result.key_code == "COF-BLK"
    assert result.planned_sku == "OEM-DSP-XMI-RN5AP-COF-BLK"


def test_generate_xiaomi_display_sku_handles_watch_series(db_session) -> None:
    product = Product(
        article="1001-xiaomi-watch",
        name="Дисплей для Xiaomi Watch S2 (BHR8035GL) (46 мм) + тачскрин (черный) (ORIG)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "XMI-WS2"
    assert result.key_code == "BLK-OR"
    assert result.planned_sku == "OEM-DSP-XMI-WS2-BLK-OR"


def test_generate_xiaomi_display_sku_distinguishes_redmi_4x_from_note_4x(db_session) -> None:
    product = Product(
        article="1001-xiaomi-redmi-4x",
        name="Дисплей для Xiaomi Redmi Note 4X + тачскрин (черный) (Medium)",
        subject="дисплей",
        display_type="In-Cell",
        quality_raw="Optima",
        color="черный",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "XMI-RN4X"
    assert result.key_code == "INL-BLK-CPM"
    assert result.planned_sku == "OEM-DSP-XMI-RN4X-INL-BLK-CPM"


def test_generate_xiaomi_display_sku_distinguishes_redmi_5_from_5_plus(db_session) -> None:
    product = Product(
        article="1001-xiaomi-redmi-5p",
        name="Дисплей для Xiaomi Redmi 5 Plus (MEG7) + тачскрин (черный) (Medium)",
        subject="дисплей",
        display_type="In-Cell",
        quality_raw="Optima",
        color="черный",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "XMI-R5P"
    assert result.planned_sku == "OEM-DSP-XMI-R5P-INL-BLK-CPM"


def test_generate_xiaomi_display_sku_distinguishes_watch_variants(db_session) -> None:
    product = Product(
        article="1001-xiaomi-watch-lte",
        name="Дисплей для Xiaomi Watch 2 Pro LTE (M2233W1) + тачскрин (черный) (ORIG)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "XMI-W2PL"
    assert result.planned_sku == "OEM-DSP-XMI-W2PL-BLK-OR"


def test_generate_xiaomi_display_sku_uses_frame_rev(db_session) -> None:
    product = Product(
        article="1001-xiaomi-frame",
        name="Дисплей для Xiaomi 13 (2211133G) (в сборе с тачскрином) (черный) (в рамке) (ORIG100)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "XMI-13"
    assert result.rev == "FR"
    assert result.planned_sku == "OEM-DSP-XMI-13-BLK-OR1-FR"


def test_generate_xiaomi_display_sku_recomputes_name_before_wrong_phone_model_link(
    db_session,
) -> None:
    product = Product(
        article="1001-xiaomi-wrong-link",
        name="Дисплей для Xiaomi Poco X6 Pro (2311DRK48G) + тачскрин (черный) (в рамке) (ORIG100) (SP)",
        subject="дисплей",
        display_type="AMOLED",
        quality_raw="ORIG100",
        color="черный",
    )
    phone_model = PhoneModel(brand="xiaomi", model_name="poco f6 pro (23113rkc6g)", variant=None)
    db_session.add_all([product, phone_model])
    db_session.flush()
    db_session.add(
        ProductPhoneModel(product_id=product.id, phone_model_id=phone_model.id, source="onec")
    )
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "XMI-PX6P"
    assert result.planned_sku == "OEM-DSP-XMI-PX6P-AMD-BLK-OR1-FS"


def test_generate_xiaomi_display_sku_uses_sequence_rev_for_exact_duplicates(db_session) -> None:
    product1 = Product(
        article="1001-xiaomi-dup-a", name="Дисплей Xiaomi POCO X3 с тачскрином", subject="дисплей"
    )
    product2 = Product(
        article="1001-xiaomi-dup-b", name="Дисплей Xiaomi POCO X3 с тачскрином", subject="дисплей"
    )
    db_session.add_all([product1, product2])
    db_session.commit()
    db_session.refresh(product1)
    db_session.refresh(product2)

    result1 = generate_sku_for_product(db_session, product1)
    apply_sku_generation_result(db_session, product1, result1)
    db_session.commit()
    db_session.refresh(product2)

    result2 = generate_sku_for_product(db_session, product2)

    assert result1.status == "generated"
    assert result1.planned_sku == "OEM-DSP-XMI-PX3-STD"
    assert result2.status == "generated"
    assert result2.rev == "2"
    assert result2.planned_sku == "OEM-DSP-XMI-PX3-STD-2"


def test_generate_xiaomi_display_sku_distinguishes_redmi_10_2022(db_session) -> None:
    product = Product(
        article="1001-xiaomi-r1022",
        name="Дисплей для Xiaomi Redmi 10 2022 (22011119UY) + тачскрин (черный) (в рамке) (ORIG)",
        subject="дисплей",
        display_type="In-Cell",
        quality_raw="ORIG",
        color="черный",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "XMI-R1022"
    assert result.planned_sku == "OEM-DSP-XMI-R1022-INL-BLK-OR-FR"


def test_generate_xiaomi_display_sku_distinguishes_redmi_4_prime(db_session) -> None:
    product = Product(
        article="1001-xiaomi-r4p",
        name="Дисплей для Xiaomi Redmi 4 Prime (Pro) (в сборе с тачскрином) (белый) (COF)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "XMI-R4P"
    assert result.key_code == "COF-WHT"
    assert result.planned_sku == "OEM-DSP-XMI-R4P-COF-WHT"


def test_generate_huawei_display_sku_uses_compact_p10_lite_code(db_session) -> None:
    product = Product(
        article="1001-huawei-p10lt",
        name="Дисплей для Huawei P10 Lite (WAS-L03T/WAS-LX1) + тачскрин (золотистый) (Medium)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "HWE-P10LT"
    assert result.planned_sku == "OEM-DSP-HWE-P10LT-GLD-CPM"


def test_generate_huawei_display_sku_uses_compact_honor_x_series_code(db_session) -> None:
    product = Product(
        article="1001-huawei-hx9c",
        name="Дисплей для Huawei Honor X9c (BRP-NX1) (в сборе с тачскрином) (черный) (In-Cell) (Low)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "HWE-HX9C"
    assert result.planned_sku == "OEM-DSP-HWE-HX9C-INL-BLK-CPL"


def test_generate_huawei_display_sku_uses_compact_mediapad_code(db_session) -> None:
    product = Product(
        article="1001-huawei-mediapad",
        name="Дисплей для Huawei MediaPad T1 7.0 (T1-701U) + тачскрин (белый)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "HWE-MT17"
    assert result.planned_sku == "OEM-DSP-HWE-MT17-WHT"


def test_generate_huawei_display_sku_uses_compact_nova_family_code(db_session) -> None:
    product = Product(
        article="1001-huawei-n2i",
        name="Дисплей для Huawei Nova 2i (RNE-L21) / Mate 10 Lite (RNE-L21) + тачскрин (черный) (Medium)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "HWE-M10LT"
    assert result.planned_sku == "OEM-DSP-HWE-M10LT-BLK-CPM"


def test_generate_huawei_display_sku_falls_back_to_std_for_old_models(db_session) -> None:
    product = Product(
        article="1001-huawei-std",
        name="Дисплей для Huawei Nova Lite (PRA-LX2) / Y7 2017 (TRT-LX1) / Y7 Prime 2017 (TRT-L21A)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "HWE-NLT"
    assert result.key_code == "STD"
    assert result.planned_sku == "OEM-DSP-HWE-NLT-STD"


def test_generate_huawei_display_sku_uses_compact_watch_gt_code(db_session) -> None:
    product = Product(
        article="1001-huawei-watch-gt",
        name="Дисплей для Huawei Watch GT 2e (46 мм) (HCT-B19) + тачскрин (черный) (ORIG)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "HWE-WGT2E"
    assert result.planned_sku == "OEM-DSP-HWE-WGT2E-BLK-OR"


def test_generate_huawei_display_sku_uses_compact_honor_8c_code(db_session) -> None:
    product = Product(
        article="1001-huawei-h8c",
        name="Дисплей для Huawei Honor 8C (BKK-AL10) / Asus ZenFone Max M2 (ZB633KL) + тачскрин (черный) (ORIG)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "HWE-H8C"
    assert result.planned_sku == "OEM-DSP-HWE-H8C-BLK-OR"


def test_generate_huawei_display_sku_uses_compact_mate_xs_code(db_session) -> None:
    product = Product(
        article="1001-huawei-mxs2",
        name="Дисплей для Huawei Mate Xs 2 (PAL-LX9) + тачскрин (внутренний) (черный) (в рамке) (ORIG100)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "HWE-MXS2"
    assert result.planned_sku == "OEM-DSP-HWE-MXS2-BLK-OR1-IN"


def test_generate_huawei_display_sku_uses_compact_honor_pad_x9_code(db_session) -> None:
    product = Product(
        article="1001-huawei-hpx9",
        name="Дисплей для Huawei Honor Pad X9 11.5 (ELN-W09/ELN-L09) + тачскрин (черный) (ORIG)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "HWE-HPX9"
    assert result.planned_sku == "OEM-DSP-HWE-HPX9-BLK-OR"


def test_generate_huawei_display_sku_uses_compact_watch_fit_code(db_session) -> None:
    product = Product(
        article="1001-huawei-wf4p",
        name="Дисплей для Huawei Watch Fit 4 Pro (SYA-B29) + тачскрин (черный) (ORIG)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "HWE-WF4P"
    assert result.planned_sku == "OEM-DSP-HWE-WF4P-BLK-OR"


def test_generate_huawei_display_sku_uses_compact_enjoy_60x_code(db_session) -> None:
    product = Product(
        article="1001-huawei-e60x",
        name="Дисплей для Huawei Enjoy 60X (STG-AL00) (в сборе с тачскрином) (черный) (COF) (Medium)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "HWE-E60X"
    assert result.planned_sku == "OEM-DSP-HWE-E60X-COF-BLK-CPM"


def test_generate_huawei_display_conflict_uses_papermatte_rev(db_session) -> None:
    base = Product(
        article="1001-huawei-mp115-base",
        name="Дисплей для Huawei MatePad 11.5 (2025) (TXZ-W09) + тачскрин (черный) (ORIG)",
        subject="дисплей",
    )
    papermatte = Product(
        article="1001-huawei-mp115-pm",
        name="Дисплей для Huawei MatePad 11.5 (2025) PaperMatte Edition (TXZ-W09) + тачскрин (черный) (ORIG)",
        subject="дисплей",
    )
    db_session.add_all([base, papermatte])
    db_session.commit()
    db_session.refresh(base)
    db_session.refresh(papermatte)

    base_result = generate_sku_for_product(db_session, base)
    apply_sku_generation_result(db_session, base, base_result)
    db_session.commit()
    db_session.refresh(papermatte)

    papermatte_result = generate_sku_for_product(db_session, papermatte)

    assert base_result.planned_sku == "OEM-DSP-HWE-MP115-BLK-OR"
    assert papermatte_result.status == "generated"
    assert papermatte_result.rev == "PM"
    assert papermatte_result.planned_sku == "OEM-DSP-HWE-MP115-BLK-OR-PM"


def test_generate_huawei_display_distinguishes_matepad_11_5s(db_session) -> None:
    product = Product(
        article="1001-huawei-mp115s",
        name="Дисплей для Huawei MatePad 11.5S (TGR-W09) + тачскрин (черный) (ORIG)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "HWE-MP115S"
    assert result.planned_sku == "OEM-DSP-HWE-MP115S-BLK-OR"


def test_generate_huawei_display_distinguishes_matepad_10_4_year_without_parentheses(
    db_session,
) -> None:
    product = Product(
        article="1001-huawei-mp10422",
        name="Дисплей для Huawei MatePad 10.4 2022 + тачскрин (черный) (High)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "HWE-MP10422"
    assert result.planned_sku == "OEM-DSP-HWE-MP10422-BLK"


def test_generate_huawei_display_conflict_uses_lite_e_device_code(db_session) -> None:
    base = Product(
        article="1001-huawei-p40lt",
        name="Дисплей для Huawei P40 Lite (JNY-LX1) / Nova 6 SE (JNY-TL10) + тачскрин (черный) (Medium)",
        subject="дисплей",
    )
    lite_e = Product(
        article="1001-huawei-p40lte",
        name="Дисплей для Huawei P40 Lite E (ART-L29) / Honor 9C (AKA-L29) / Honor Play 3 / Y7p (ART-L28) + тачскрин (черный) (Medium)",
        subject="дисплей",
    )
    db_session.add_all([base, lite_e])
    db_session.commit()
    db_session.refresh(base)
    db_session.refresh(lite_e)

    base_result = generate_sku_for_product(db_session, base)
    apply_sku_generation_result(db_session, base, base_result)
    db_session.commit()
    db_session.refresh(lite_e)

    lite_e_result = generate_sku_for_product(db_session, lite_e)

    assert base_result.planned_sku == "OEM-DSP-HWE-P40LT-BLK-CPM"
    assert lite_e_result.status == "generated"
    assert lite_e_result.device_code == "HWE-P40LE"
    assert lite_e_result.rev is None
    assert lite_e_result.planned_sku == "OEM-DSP-HWE-P40LE-BLK-CPM"


def test_generate_huawei_display_conflict_uses_wifi_rev(db_session) -> None:
    lte = Product(
        article="1001-huawei-mt38-lte",
        name="Дисплей для Huawei MediaPad T3 8.0 LTE (KOB-L09) + тачскрин (черный) (High)",
        subject="дисплей",
    )
    wifi = Product(
        article="1001-huawei-mt38-wifi",
        name="Дисплей для Huawei MediaPad T3 8.0 Wi-Fi (KOB-W09) + тачскрин (черный) (High)",
        subject="дисплей",
    )
    db_session.add_all([lte, wifi])
    db_session.commit()
    db_session.refresh(lte)
    db_session.refresh(wifi)

    lte_result = generate_sku_for_product(db_session, lte)
    apply_sku_generation_result(db_session, lte, lte_result)
    db_session.commit()
    db_session.refresh(wifi)

    wifi_result = generate_sku_for_product(db_session, wifi)

    assert lte_result.planned_sku == "OEM-DSP-HWE-MT38-BLK"
    assert wifi_result.status == "generated"
    assert wifi_result.rev == "WFI"
    assert wifi_result.planned_sku == "OEM-DSP-HWE-MT38-BLK-WFI"


def test_generate_huawei_display_conflict_uses_frame_rev(db_session) -> None:
    base = Product(
        article="1001-huawei-p50-base",
        name="Дисплей для Huawei P50 (ABR-LX9) + тачскрин (черный) (ORIG)",
        subject="дисплей",
    )
    framed = Product(
        article="1001-huawei-p50-fr",
        name="Дисплей для Huawei P50 (ABR-LX9) + тачскрин (черный) (в рамке) (ORIG)",
        subject="дисплей",
    )
    db_session.add_all([base, framed])
    db_session.commit()
    db_session.refresh(base)
    db_session.refresh(framed)

    base_result = generate_sku_for_product(db_session, base)
    apply_sku_generation_result(db_session, base, base_result)
    db_session.commit()
    db_session.refresh(framed)

    framed_result = generate_sku_for_product(db_session, framed)

    assert base_result.planned_sku == "OEM-DSP-HWE-P50-BLK-OR"
    assert framed_result.status == "generated"
    assert framed_result.rev == "FR"
    assert framed_result.planned_sku == "OEM-DSP-HWE-P50-BLK-OR-FR"


def test_generate_huawei_display_marks_spisat_as_manual_review(db_session) -> None:
    product = Product(
        article="1001-huawei-spisat",
        name="Списать! Дисплей для Huawei Y3 II LTE + тачскрин (белый)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "manual_review"
    assert result.reasons == ["duplicate_card"]
    assert result.planned_sku is None


def test_generate_huawei_display_conflict_uses_watch_size_rev(db_session) -> None:
    watch46 = Product(
        article="1001-huawei-watch5-46",
        name="Дисплей для Huawei Watch 5 (46 мм) (RTS-AL00) + тачскрин (черный) (ORIG)",
        subject="дисплей",
    )
    watch42 = Product(
        article="1001-huawei-watch5-42",
        name="Дисплей для Huawei Watch 5 (42 мм) (SOC-AL00) + тачскрин (черный) (ORIG)",
        subject="дисплей",
    )
    db_session.add_all([watch46, watch42])
    db_session.commit()
    db_session.refresh(watch46)
    db_session.refresh(watch42)

    watch46_result = generate_sku_for_product(db_session, watch46)
    apply_sku_generation_result(db_session, watch46, watch46_result)
    db_session.commit()
    db_session.refresh(watch42)

    watch42_result = generate_sku_for_product(db_session, watch42)

    assert watch46_result.planned_sku == "OEM-DSP-HWE-W5-BLK-OR"
    assert watch42_result.status == "generated"
    assert watch42_result.rev == "42"
    assert watch42_result.planned_sku == "OEM-DSP-HWE-W5-BLK-OR-42"


def test_generate_huawei_display_distinguishes_nova_13_pro(db_session) -> None:
    product = Product(
        article="1001-huawei-n13p",
        name="Дисплей для Huawei Nova 13 Pro (MIS-LX9) + тачскрин (черный) (In-Cell)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "HWE-N13P"
    assert result.planned_sku == "OEM-DSP-HWE-N13P-INL-BLK"


def test_generate_huawei_display_distinguishes_y9_model_year(db_session) -> None:
    product = Product(
        article="1001-huawei-y919",
        name="Дисплей для Huawei Y9 2019 (JKM-LX1) + тачскрин (черный) (Medium)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "HWE-Y919"
    assert result.planned_sku == "OEM-DSP-HWE-Y919-BLK-CPM"


def test_generate_oppo_display_uses_compact_reno_code(db_session) -> None:
    product = Product(
        article="1001-oppo-reno11",
        name="Дисплей для OPPO Reno 11 5G (CPH2599) + тачскрин (черный) (OLED) (High)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "OPP-R115"
    assert result.planned_sku == "OEM-DSP-OPP-R115-OLD-BLK"


def test_generate_realme_display_supports_cog_key(db_session) -> None:
    product = Product(
        article="1001-realme-c65",
        name="Дисплей для Realme C65 4G (RMX3910) (в сборе с тачскрином) (черный) (COG) (Low)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "RLM-C654"
    assert result.planned_sku == "OEM-DSP-RLM-C654-COG-BLK-CPL"


def test_generate_oppo_display_uses_compact_find_n5_code(db_session) -> None:
    product = Product(
        article="1001-oppo-findn5",
        name="Дисплей для OPPO Find N5 (CPH2671) + тачскрин (внешний) (черный) (ORIG100)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "OPP-FN5"
    assert result.planned_sku == "OEM-DSP-OPP-FN5-BLK-OR1-OT"


def test_generate_oppo_display_falls_back_to_std_for_legacy_card(db_session) -> None:
    product = Product(
        article="1001-oppo-find5",
        name="Дисплей OPPO Find 5 ( 5.0 ) в сборе с тачскрином",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "OPP-F5"
    assert result.planned_sku == "OEM-DSP-OPP-F5-STD"


def test_generate_oneplus_display_uses_compact_nord_ce_code(db_session) -> None:
    product = Product(
        article="1001-oneplus-nce35",
        name="Дисплей для OnePlus Nord CE 3 Lite 5G + тачскрин (черный) (ORIG)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "ONE-NCE3L5"
    assert result.planned_sku == "OEM-DSP-ONE-NCE3L5-BLK-OR"


def test_generate_oneplus_display_uses_compact_pad_code(db_session) -> None:
    product = Product(
        article="1001-oneplus-pad2",
        name="Дисплей для OnePlus Pad 2 (OPD2403) + тачскрин (черный) (ORIG)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "ONE-PAD2"
    assert result.planned_sku == "OEM-DSP-ONE-PAD2-BLK-OR"


def test_generate_realme_display_uses_compact_pad_mini_code(db_session) -> None:
    product = Product(
        article="1001-realme-padmini",
        name="Дисплей для Realme Pad mini (RMP2105/RMP2106) + тачскрин (черный)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "RLM-PDMN"
    assert result.planned_sku == "OEM-DSP-RLM-PDMN-BLK"


def test_generate_realme_display_uses_compact_watch_code(db_session) -> None:
    product = Product(
        article="1001-realme-watch2",
        name="Дисплей для Realme Watch 2 (RMW2401) + тачскрин (черный) (ORIG)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "RLM-W201"
    assert result.planned_sku == "OEM-DSP-RLM-W201-BLK-OR"


def test_generate_realme_display_adds_frame_rev(db_session) -> None:
    base = Product(
        article="1001-realme-c31-base",
        name="Дисплей для Realme C31 (RMX3501) + тачскрин (черный) (ORIG)",
        subject="дисплей",
    )
    framed = Product(
        article="1001-realme-c31-fr",
        name="Дисплей для Realme C31 (RMX3501) + тачскрин (черный) (в рамке) (ORIG)",
        subject="дисплей",
    )
    db_session.add_all([base, framed])
    db_session.commit()
    db_session.refresh(base)
    db_session.refresh(framed)

    base_result = generate_sku_for_product(db_session, base)
    apply_sku_generation_result(db_session, base, base_result)
    db_session.commit()
    db_session.refresh(framed)

    framed_result = generate_sku_for_product(db_session, framed)

    assert base_result.planned_sku == "OEM-DSP-RLM-C31-BLK-OR"
    assert framed_result.status == "generated"
    assert framed_result.rev == "FR"
    assert framed_result.planned_sku == "OEM-DSP-RLM-C31-BLK-OR-FR"


def test_generate_oneplus_display_distinguishes_pro_model(db_session) -> None:
    product = Product(
        article="1001-oneplus-9pro",
        name="Дисплей для OnePlus 9 Pro (LE2121) + тачскрин (черный) (ORIG)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "ONE-9P"
    assert result.planned_sku == "OEM-DSP-ONE-9P-BLK-OR"


def test_generate_oneplus_display_distinguishes_10r_power_variant(db_session) -> None:
    product = Product(
        article="1001-oneplus-10r150",
        name="Дисплей для OnePlus 10R (150W) / 10T (CPH2415) / Ace (PGKM10) / Ace Pro (PGP110) + тачскрин (черный) (ORIG)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "ONE-10R150"
    assert result.planned_sku == "OEM-DSP-ONE-10R150-BLK-OR"


def test_generate_oppo_display_distinguishes_a5_pro_4g(db_session) -> None:
    product = Product(
        article="1001-oppo-a5pro4",
        name="Дисплей для OPPO A5 Pro 4G (CPH2711) + тачскрин (черный) (ORIG)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "OPP-A5P4"
    assert result.planned_sku == "OEM-DSP-OPP-A5P4-BLK-OR"


def test_generate_realme_display_distinguishes_pro_plus_model(db_session) -> None:
    product = Product(
        article="1001-realme-12pp",
        name="Дисплей для Realme 12 Pro+ (RMX3840) + тачскрин (бежевый) (в рамке) (ORIG100) (SP)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "RLM-12PP"
    assert result.planned_sku == "OEM-DSP-RLM-12PP-BEI-OR1-FR-SP"


def test_generate_realme_display_distinguishes_watch_2_rmw_code(db_session) -> None:
    product = Product(
        article="1001-realme-watch2-rmw2401",
        name="Дисплей для Realme Watch 2 (RMW2401) + тачскрин (черный) (ORIG)",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "RLM-W201"
    assert result.planned_sku == "OEM-DSP-RLM-W201-BLK-OR"


def test_generate_oppo_display_distinguishes_a5_2020_family(db_session) -> None:
    product = Product(
        article="1001-oppo-a520-family",
        name="Дисплей для OPPO A5 2020 (CPH1931) / A9 2020 (CPH1941) / A31 (CPH2015) / C3 (RMX2020) и др. + тачскрин (черный) (ORIG)",
        subject="дисплей",
        display_type="In-Cell",
        quality_raw="ORIG",
        color="черный",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "OPP-A520"
    assert result.planned_sku == "OEM-DSP-OPP-A520-INL-BLK-OR"


def test_generate_oppo_display_adds_revision_rev_suffix(db_session) -> None:
    product = Product(
        article="1001-oppo-rev05",
        name="Дисплей для Realme 8 4G (RMX3085) / OPPO A74 4G (CPH2219) / A95 5G (PELM00) / Reno 7Z 5G (CPH2343) и др. + тачскрин (черный) (OLED) (Rev. 05)",
        subject="дисплей",
        display_type="OLED",
        quality_raw="High",
        color="черный",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "OPP-A744"
    assert result.rev == "R5"
    assert result.planned_sku == "OEM-DSP-OPP-A744-OLD-BLK-CPH-R5"


def test_generate_battery_sku(db_session) -> None:
    product = Product(
        article="1002",
        name="Аккумулятор для iPhone 11",
        brand="Apple",
        manufacturer="F5ENERGY",
        category="Аккумуляторы",
        battery_capacity_mah=3470,
        battery_is_high_capacity=True,
    )
    phone_model = PhoneModel(brand="apple", model_name="iphone 11", variant=None)
    db_session.add_all([product, phone_model])
    db_session.flush()
    db_session.add(
        ProductPhoneModel(product_id=product.id, phone_model_id=phone_model.id, source="onec")
    )
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.planned_sku == "F5-BAT-IPH11-HC3470"


def test_generate_brand_code_falls_back_to_oem_from_name(db_session) -> None:
    product = Product(
        article="1002-oem",
        name="Аккумулятор для Meizu M5 (BA611)",
        category="Аккумуляторы",
        battery_capacity_mah=3070,
    )
    phone_model = PhoneModel(brand="meizu", model_name="m5", variant=None)
    db_session.add_all([product, phone_model])
    db_session.flush()
    db_session.add(
        ProductPhoneModel(product_id=product.id, phone_model_id=phone_model.id, source="onec")
    )
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.brand_code == "OEM"
    assert result.planned_sku == "OEM-BAT-MEI-M5-3070"


def test_generate_cable_and_charger_sku_without_device_model(db_session) -> None:
    cable = Product(
        article="1003",
        name="Кабель USB-C Lightning 1m White",
        manufacturer="F5ENERGY",
        category="Кабели",
        cable_connector_input="USB-C",
        cable_connector_output="Lightning",
        cable_length="1 m",
        color="White",
    )
    charger = Product(
        article="1004",
        name="Зарядка 65W GaN EU",
        manufacturer="F5ENERGY",
        category="Зарядные устройства",
        charger_power_w=65,
        charger_technology="GaN",
        charger_plug_type="EU",
    )
    db_session.add_all([cable, charger])
    db_session.commit()

    cable_result = generate_sku_for_product(db_session, cable)
    charger_result = generate_sku_for_product(db_session, charger)

    assert cable_result.planned_sku == "F5-CBL-USBC-LTN-1M-WHT"
    assert charger_result.planned_sku == "F5-CHR-65W-GAN-EU"


def test_generate_retail_cable_and_charger_sku_from_name(db_session) -> None:
    products = [
        Product(
            article="039550",
            name=(
                "Дата-кабель Apple USB-Lightning, 1.0 м "
                "(с поддержкой быстрой зарядки) (белый) (ORIG100)"
            ),
            category="Кабели для телефонов",
            subject="кабель",
        ),
        Product(
            article="061730",
            name=(
                "Дата-кабель Remax RC-C061 C-L Type-C-Lightning 20W (2.4 А), "
                "1.2 м (с поддержкой быстрой зарядки) (плетеный) (черный)"
            ),
            category="Кабели для телефонов",
            subject="кабель",
        ),
        Product(
            article="068309",
            name="Адаптер питания 2 USB-C 45 Вт + Кабель Type-C-Type-C (Premium)",
            category="Зарядки сетевые для телефонов",
            subject="зарядка",
        ),
        Product(
            article="066370",
            name="Накопитель для телефона Remax RPP-107 (5000 мАч) (ультратонкий) (MagSafe)",
            subject="пауэрбанк",
        ),
        Product(
            article="075473",
            name="Дата-кабель Baseus Cafule USB - TypeC 1 м (плетеный) (черный)",
            subject="кабель",
        ),
        Product(
            article="075481",
            name="Дата-кабель Baseus New Braided USB - TypeC 1 м (плетеный) (черный)",
            subject="кабель",
        ),
        Product(
            article="079641",
            name="Сетевое зарядное устройство Hoco N72 TypeC 20W + кабель TypeC - Lightning (черный)",
            subject="зарядка",
        ),
        Product(
            article="075516",
            name="Автомобильное зарядное устройство Baseus Tiny Star  USB + TypeC 30W (черный)",
            subject="зарядка",
        ),
        Product(
            article="075518",
            name="Автомобильное зарядное устройство Baseus Circular USB + TypeC 30W (черный)",
            subject="зарядка",
        ),
    ]
    db_session.add_all(products)
    db_session.commit()

    results = {
        product.article: generate_sku_for_product(db_session, product) for product in products
    }

    assert results["039550"].planned_sku == "OEM-CBL-USBA-LTN-1M-WHT-FCH-OR1"
    assert results["061730"].planned_sku == "OEM-CBL-USBC-LTN-1-2M-RCC061-20W"
    assert results["068309"].planned_sku == "OEM-CHR-45W-2USBC-CTC-PR"
    assert results["066370"].planned_sku == "OEM-CHR-5000MAH-MGS-RPP107"
    assert results["075473"].planned_sku == "OEM-CBL-USBA-USBC-1M-BLK-CAF"
    assert results["075481"].planned_sku == "OEM-CBL-USBA-USBC-1M-BLK-NBR"
    assert results["079641"].planned_sku == "OEM-CHR-20W-USBC-N72-BLK-CLTN"
    assert results["075516"].planned_sku == "OEM-CHR-30W-USBC-TSTAR-BLK"
    assert results["075518"].planned_sku == "OEM-CHR-30W-USBC-CIRC-BLK"


def test_generate_sku_marks_manual_review_when_missing_fields(db_session) -> None:
    product = Product(
        article="1005", name="Шлейф без модели", category="Шлейфы", manufacturer="F5ENERGY"
    )
    db_session.add(product)
    db_session.commit()

    result = generate_sku_for_product(db_session, product)

    assert result.status == "manual_review"
    assert "missing_device_code" in result.reasons


def test_generate_sku_detects_conflict(db_session) -> None:
    existing = Product(
        article="1006",
        planned_sku="F5-DSP-IPH11-OLD-BLK-CPH",
        manufacturer="F5ENERGY",
        category="Дисплеи",
        name="Existing",
    )
    candidate = Product(
        article="1007",
        name="Дисплей для Apple iPhone 11",
        brand="Apple",
        manufacturer="F5ENERGY",
        category="Дисплеи",
        display_type="OLED",
        display_quality="Copy High",
        color="Black",
    )
    phone_model = PhoneModel(brand="apple", model_name="iphone 11", variant=None)
    db_session.add_all([existing, candidate, phone_model])
    db_session.flush()
    db_session.add(
        ProductPhoneModel(product_id=candidate.id, phone_model_id=phone_model.id, source="onec")
    )
    db_session.add(
        ProductSkuPlan(
            product=existing,
            planned_sku="F5-DSP-IPH11-OLD-BLK-CPH",
            brand_code="F5",
            category_code="DSP",
            device_code="IPH11",
            key_code="OLD-BLK-CPH",
            status="generated",
            source="rules",
            is_active=True,
        )
    )
    db_session.commit()
    db_session.refresh(candidate)

    result = generate_sku_for_product(db_session, candidate)

    assert result.status == "conflict"
    assert result.planned_sku is None


def test_generate_sku_batch_dry_run_and_write(db_session) -> None:
    product = Product(
        article="1008",
        name="Дисплей для Apple iPhone 12",
        brand="Apple",
        manufacturer="F5ENERGY",
        category="Дисплеи",
        display_type="OLED",
        display_quality="Copy High",
        color="Black",
    )
    phone_model = PhoneModel(brand="apple", model_name="iphone 12", variant=None)
    db_session.add_all([product, phone_model])
    db_session.flush()
    db_session.add(
        ProductPhoneModel(product_id=product.id, phone_model_id=phone_model.id, source="onec")
    )
    db_session.commit()
    db_session.refresh(product)

    dry_run = generate_sku_batch(db_session, dry_run=True)
    assert dry_run["generated"] == 1
    assert product.planned_sku is None

    written = generate_sku_batch(db_session, dry_run=False)
    db_session.refresh(product)
    assert written["generated"] == 1
    assert product.planned_sku == "F5-DSP-IPH12-OLD-BLK-CPH"
    assert product.sku_sync_status == "missing_in_1c"
    assert product.sku_sync_error is None
    active_plan = (
        db_session.query(ProductSkuPlan).filter_by(product_id=product.id, is_active=True).one()
    )
    assert active_plan.planned_sku == "F5-DSP-IPH12-OLD-BLK-CPH"
    assert active_plan.status == "generated"


def test_generate_sku_batch_skips_inactive_and_deleted_by_default(db_session) -> None:
    products = [
        Product(
            article="1008-active",
            name="Дисплей для Apple iPhone 12",
            brand="Apple",
            manufacturer="F5ENERGY",
            category="Дисплеи",
            display_type="OLED",
            display_quality="Copy High",
            color="Black",
        ),
        Product(
            article="1008-inactive",
            name="Дисплей для Apple iPhone 12",
            brand="Apple",
            manufacturer="F5ENERGY",
            category="Дисплеи",
            display_type="OLED",
            display_quality="Copy High",
            color="Black",
            is_active=False,
        ),
        Product(
            article="1008-deleted",
            name="Дисплей для Apple iPhone 12",
            brand="Apple",
            manufacturer="F5ENERGY",
            category="Дисплеи",
            display_type="OLED",
            display_quality="Copy High",
            color="Black",
            is_marked_for_deletion=True,
        ),
    ]
    phone_model = PhoneModel(brand="apple", model_name="iphone 12", variant=None)
    db_session.add_all([*products, phone_model])
    db_session.flush()
    for product in products:
        db_session.add(
            ProductPhoneModel(product_id=product.id, phone_model_id=phone_model.id, source="onec")
        )
    db_session.commit()

    default_result = generate_sku_batch(db_session, dry_run=True)
    assert default_result["generated"] == 1
    assert [item["article"] for item in default_result["items"]] == ["1008-active"]

    all_result = generate_sku_batch(db_session, dry_run=True, active_only=False)
    assert all_result["generated"] == 3
    assert {item["article"] for item in all_result["items"]} == {
        "1008-active",
        "1008-inactive",
        "1008-deleted",
    }


def test_apply_sku_generation_result_updates_product(db_session) -> None:
    product = Product(article="1009", name="Test", manufacturer="F5ENERGY")
    db_session.add(product)
    db_session.commit()

    result = generate_sku_for_product(db_session, product)
    apply_sku_generation_result(db_session, product, result)
    db_session.commit()

    assert product.sku_sync_status == "manual_review"
    active_plan = (
        db_session.query(ProductSkuPlan).filter_by(product_id=product.id, is_active=True).one()
    )
    assert active_plan.status == "manual_review"
    assert active_plan.error_reason


def test_sync_product_sku_status_variants(db_session) -> None:
    product = Product(article="1010", name="Fact SKU Product", fact_sku="F5-BAT-IPH11-HC3470")
    db_session.add(product)
    db_session.commit()

    sync_product_sku_status(product, "generated")
    assert product.sku_sync_status == "missing_plan"

    product.planned_sku = "F5-BAT-IPH11-HC3470"
    sync_product_sku_status(product, "generated")
    assert product.sku_sync_status == "match"

    product.planned_sku = "F5-BAT-IPH11-3470"
    sync_product_sku_status(product, "generated")
    assert product.sku_sync_status == "mismatch"


def test_generate_infinix_hot_10_lite_uses_short_device_code(db_session) -> None:
    product = Product(
        article="inf-hot10l",
        name="Дисплей для Infinix Hot 10 Lite (X657B) / Smart 5 + тачскрин (черный) (Medium)",
        subject="дисплей",
        color="черный",
        quality_raw="Medium",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "INF-H10L"
    assert result.planned_sku == "OEM-DSP-INF-H10L-BLK-CPM"


def test_generate_infinix_smart_8_pro_uses_short_device_code(db_session) -> None:
    product = Product(
        article="inf-s8p",
        name="Дисплей для Infinix Smart 8 Pro (X6525B) (в сборе с тачскрином) (черный) (COF) (Medium)",
        subject="дисплей",
        color="черный",
        quality_raw="Medium",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "INF-S8P"
    assert result.planned_sku == "OEM-DSP-INF-S8P-COF-BLK-CPM"


def test_generate_infinix_zero_8i_uses_short_device_code(db_session) -> None:
    product = Product(
        article="inf-z8i",
        name="Дисплей для Infinix Zero 8 / Zero 8i (в сборе с тачскрином) (черный) (COF) (Medium)",
        subject="дисплей",
        color="черный",
        quality_raw="Medium",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "INF-Z8I"
    assert result.planned_sku == "OEM-DSP-INF-Z8I-COF-BLK-CPM"


def test_generate_infinix_note_40_pro_plus_uses_short_device_code(db_session) -> None:
    product = Product(
        article="inf-n40pp",
        name="Дисплей для Infinix Note 40 Pro+ 5G (X6851B) + тачскрин (черный) (ORIG)",
        subject="дисплей",
        color="черный",
        quality_raw="ORIG",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "INF-N40PP5"
    assert result.planned_sku == "OEM-DSP-INF-N40PP5-BLK-OR"


def test_generate_zte_nubia_flip_2_uses_short_device_code(db_session) -> None:
    product = Product(
        article="zte-nf25",
        name="Дисплей для ZTE Nubia Flip 2 5G (NX732J) + тачскрин (внутренний) (черный) (ORIG100)",
        subject="дисплей",
        color="черный",
        quality_raw="ORIG100",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "ZTE-NF25"
    assert result.planned_sku == "OEM-DSP-ZTE-NF25-BLK-OR1-IN"


def test_generate_zte_red_magic_10_air_uses_short_device_code(db_session) -> None:
    product = Product(
        article="zte-rm10a",
        name="Дисплей для ZTE Nubia Red Magic 10 Air (NX779J) + тачскрин (черный) (ORIG100)",
        subject="дисплей",
        color="черный",
        quality_raw="ORIG100",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "ZTE-RM10A"
    assert result.planned_sku == "OEM-DSP-ZTE-RM10A-BLK-OR1"


def test_generate_zte_blade_a610_uses_short_device_code(db_session) -> None:
    product = Product(
        article="zte-ba610",
        name="Дисплей для ZTE Blade A610 / Blade A610C (TXDS500SHDPA-318) (в сборе с тачскрином) (белый)",
        subject="дисплей",
        color="белый",
        quality_raw="Medium",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "ZTE-BA610"
    assert result.planned_sku == "OEM-DSP-ZTE-BA610-WHT-CPM"


def test_generate_zte_blade_v8_lite_uses_short_device_code(db_session) -> None:
    product = Product(
        article="zte-bv8l",
        name="Дисплей для ZTE Blade V8 Lite + тачскрин (черный)",
        subject="дисплей",
        color="черный",
        quality_raw="Medium",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "ZTE-BV8L"
    assert result.planned_sku == "OEM-DSP-ZTE-BV8L-BLK-CPM"


def test_generate_zte_blade_af3_family_uses_short_device_code(db_session) -> None:
    product = Product(
        article="zte-baf355",
        name="Дисплей для ZTE Blade AF3 / Blade AF5 / Blade A5 и др. (в сборе с тачскрином) (черный)",
        subject="дисплей",
        color="черный",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "ZTE-BAF355"
    assert result.planned_sku == "OEM-DSP-ZTE-BAF355-BLK"


def test_generate_zte_blade_af3_family_with_frame_adds_fr_rev(db_session) -> None:
    product = Product(
        article="zte-baf355-fr",
        name="Дисплей для ZTE Blade AF3 / Blade AF5 / Blade A5 и др. (в сборе с тачскрином) (в рамке) (черный)",
        subject="дисплей",
        color="черный",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "ZTE-BAF355"
    assert result.rev == "FR"
    assert result.planned_sku == "OEM-DSP-ZTE-BAF355-BLK-FR"


def test_generate_lenovo_p90_uses_short_device_code(db_session) -> None:
    product = Product(
        article="len-p90",
        name="Дисплей для Lenovo P90 (в сборе с тачскрином) (черный)",
        subject="дисплей",
        color="черный",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "LEN-P90"
    assert result.planned_sku == "OEM-DSP-LEN-P90-BLK"


def test_generate_lenovo_k4_note_family_uses_short_device_code(db_session) -> None:
    product = Product(
        article="len-k4n",
        name="Дисплей для Lenovo K4 Note / Vibe X3 Lite / A7010 (в сборе с тачскрином) (черный)",
        subject="дисплей",
        color="черный",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "LEN-K4N"
    assert result.planned_sku == "OEM-DSP-LEN-K4N-BLK"


def test_generate_lenovo_y700_gen3_uses_short_device_code(db_session) -> None:
    product = Product(
        article="len-y700g3",
        name="Дисплей для Lenovo TB321FU Legion Y700 Gen3 (2025) + тачскрин (черный) (ORIG)",
        subject="дисплей",
        color="черный",
        quality_raw="ORIG",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "LEN-Y700G3"
    assert result.planned_sku == "OEM-DSP-LEN-Y700G3-BLK-OR"


def test_generate_lenovo_a5000_uses_short_device_code(db_session) -> None:
    product = Product(
        article="len-a5000",
        name="Дисплей для Lenovo A5000 (в сборе с тачскрином) (белый)",
        subject="дисплей",
        color="белый",
        quality_raw="Medium",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "LEN-A5000"
    assert result.planned_sku == "OEM-DSP-LEN-A5000-WHT-CPM"


def test_generate_lenovo_vibe_k5_plus_uses_short_device_code(db_session) -> None:
    product = Product(
        article="len-vk5p",
        name="Дисплей для Lenovo Vibe K5 Plus (A6020a46) (в сборе с тачскрином) (черный)",
        subject="дисплей",
        color="черный",
        quality_raw="Medium",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "LEN-VK5P"
    assert result.planned_sku == "OEM-DSP-LEN-VK5P-BLK-CPM"


def test_generate_lenovo_s856_frame_adds_fr_rev(db_session) -> None:
    product = Product(
        article="len-s856-fr",
        name="Дисплей для Lenovo IdeaPhone S856 (в сборе с тачскрином) (черный) (в рамке)",
        subject="дисплей",
        color="черный",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "LEN-S856"
    assert result.rev == "FR"
    assert result.planned_sku == "OEM-DSP-LEN-S856-BLK-FR"


def test_generate_nokia_g21_uses_short_device_code(db_session) -> None:
    product = Product(
        article="nok-g21",
        name="Дисплей для Nokia G21 (TA-1405/TA-1418) + тачскрин (черный)",
        subject="дисплей",
        color="черный",
        quality_raw="Medium",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "NOK-G21"
    assert result.planned_sku == "OEM-DSP-NOK-G21-BLK-CPM"


def test_generate_nokia_g10_g20_family_uses_short_device_code(db_session) -> None:
    product = Product(
        article="nok-g10g20",
        name="Дисплей для Nokia G10 (TA-1334) / G20 (TA-1336) + тачскрин (черный)",
        subject="дисплей",
        color="черный",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "NOK-G10"
    assert result.planned_sku == "OEM-DSP-NOK-G10-BLK"


def test_generate_nokia_21_uses_short_device_code(db_session) -> None:
    product = Product(
        article="nok-21",
        name="Дисплей для Nokia 2.1 (TA-1080) (в сборе с тачскрином) (черный) (Medium)",
        subject="дисплей",
        color="черный",
        quality_raw="Medium",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "NOK-21"
    assert result.planned_sku == "OEM-DSP-NOK-21-BLK-CPM"


def test_generate_nokia_23_uses_short_device_code(db_session) -> None:
    product = Product(
        article="nok-23",
        name="Дисплей для Nokia 2.3 (TA-1206) (в сборе с тачскрином) (черный) (Medium)",
        subject="дисплей",
        color="черный",
        quality_raw="Medium",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "NOK-23"
    assert result.planned_sku == "OEM-DSP-NOK-23-BLK-CPM"


def test_generate_meizu_m5c_uses_distinct_device_code(db_session) -> None:
    product = Product(
        article="mei-m5c",
        name="Дисплей для Meizu M5c (в сборе с тачскрином) (черный) (Medium)",
        subject="дисплей",
        color="черный",
        quality_raw="Medium",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "MEI-M5C"
    assert result.planned_sku == "OEM-DSP-MEI-M5C-BLK-CPM"


def test_generate_meizu_mx6_uses_distinct_device_code(db_session) -> None:
    product = Product(
        article="mei-mx6",
        name="Дисплей для Meizu MX6 (в сборе с тачскрином) (черный) (Medium)",
        subject="дисплей",
        color="черный",
        quality_raw="Medium",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "MEI-MX6"
    assert result.planned_sku == "OEM-DSP-MEI-MX6-BLK-CPM"


def test_generate_meizu_m3_note_uses_short_device_code(db_session) -> None:
    product = Product(
        article="mei-m3n",
        name="Дисплей для Meizu M3 Note (M681H) + тачскрин (черный)",
        subject="дисплей",
        color="черный",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "MEI-M3N"
    assert result.planned_sku == "OEM-DSP-MEI-M3N-BLK"


def test_generate_meizu_mblu_22_family_uses_short_device_code(db_session) -> None:
    product = Product(
        article="mei-mb22",
        name="Дисплей для Meizu Mblu 22 (M2511) / Mblu 22 Pro (M2512) + тачскрин (черный) (ORIG)",
        subject="дисплей",
        color="черный",
        quality_raw="ORIG",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "MEI-MB22P"
    assert result.planned_sku == "OEM-DSP-MEI-MB22P-BLK-OR"


def test_generate_battery_key_falls_back_to_capacity_from_name(db_session) -> None:
    product = Product(
        article="bat-cap-fallback",
        name="Аккумулятор DEJI для iPhone 8 Plus, 3150 мАч, Повышенной емкости",
        subject="аккумулятор",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.key_code == "HC3150"


def test_generate_battery_key_falls_back_to_battery_code_from_name(db_session) -> None:
    product = Product(
        article="bat-code-fallback",
        name="Аккумулятор для Samsung S901 Galaxy S22 (EB-BS901ABY) (Premium)",
        subject="аккумулятор",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.key_code == "EB-BS901ABY"


def test_generate_battery_key_falls_back_to_huawei_code_from_name(db_session) -> None:
    product = Product(
        article="bat-huawei-code",
        name="Аккумулятор для Huawei Honor 50 (NTH-NX9) / Nova 9 (NAM-LX9) (HB476489EFW)",
        subject="аккумулятор",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.key_code == "HB476489EFW"


def test_generate_battery_key_falls_back_to_premium_grade(db_session) -> None:
    product = Product(
        article="bat-premium-fallback",
        name="Аккумулятор для Apple iPhone 16 Pro (Premium)",
        subject="аккумулятор",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "IPH16P"
    assert result.key_code == "PRM"


def test_generate_battery_key_falls_back_to_parenthesized_code(db_session) -> None:
    product = Product(
        article="bat-parenthesized-code",
        name="Аккумулятор для Apple MacBook 12 Retina A1534 (EARLY 2015) (A1527) (ORIG)",
        subject="аккумулятор",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "MB12A1527"
    assert result.key_code == "A1527"


def test_generate_battery_rev_distinguishes_premium_from_orig_sp(db_session) -> None:
    premium = Product(
        article="bat-rev-premium",
        name="Аккумулятор для Samsung A705 Galaxy A70 (EB-BA705ABU) (Premium)",
        subject="аккумулятор",
        battery_capacity_mah=4500,
    )
    orig_sp = Product(
        article="bat-rev-orig-sp",
        name="Аккумулятор для Samsung A705 Galaxy A70 (EB-BA705ABU) (ORIG100) (SP)",
        subject="аккумулятор",
        battery_capacity_mah=4500,
    )
    db_session.add_all([premium, orig_sp])
    db_session.commit()
    db_session.refresh(premium)
    db_session.refresh(orig_sp)

    premium_result = generate_sku_for_product(db_session, premium)
    orig_sp_result = generate_sku_for_product(db_session, orig_sp)

    assert premium_result.status == "generated"
    assert orig_sp_result.status == "generated"
    assert premium_result.planned_sku == "OEM-BAT-SMG-A705-4500-PR"
    assert orig_sp_result.planned_sku == "OEM-BAT-SMG-A705-4500-OR-SP"


def test_generate_battery_rev_captures_high_capacity_without_flex(db_session) -> None:
    product = Product(
        article="bat-rev-high-no-flex",
        name="Аккумулятор для Apple iPhone 17 Pro (SIM + eSIM) (без шлейфа) (High+)",
        subject="аккумулятор",
        battery_capacity_mah=3988,
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.planned_sku == "OEM-BAT-IPH17P-3988-HC-NF"


def test_generate_battery_rev_does_not_duplicate_generic_key_marker(db_session) -> None:
    premium = Product(
        article="bat-rev-key-prm",
        name="Аккумулятор для Samsung X516 Galaxy Tab S9 FE 5G (Premium)",
        subject="аккумулятор",
    )
    high = Product(
        article="bat-rev-key-hc",
        name="Аккумулятор для Apple iPhone 16 Pro (без шлейфа) (High+)",
        subject="аккумулятор",
    )
    db_session.add_all([premium, high])
    db_session.commit()
    db_session.refresh(premium)
    db_session.refresh(high)

    premium_result = generate_sku_for_product(db_session, premium)
    high_result = generate_sku_for_product(db_session, high)

    assert premium_result.status == "generated"
    assert premium_result.planned_sku == "OEM-BAT-SMG-X516-PRM"
    assert high_result.status == "generated"
    assert high_result.planned_sku == "OEM-BAT-IPH16P-HC-NF"


def test_generate_battery_rev_adds_system_diagnosable_variant(db_session) -> None:
    product = Product(
        article="bat-apple-f5-sd",
        name="Аккумулятор для Apple iPhone 13 (F5ENERGY) (усиленный) (3560 мАч) (SPECIAL EDITION) (SYSTEM DIAGNOSABLE) + двухсторонний скотч",
        subject="аккумулятор",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.planned_sku == "OEM-BAT-IPH13-HC3560-HC-SP-SD"


def test_generate_apple_battery_airpods_case_uses_case_rev(db_session) -> None:
    product = Product(
        article="bat-airpods-case",
        name="Аккумулятор для Apple AirPods (A1523/A1722) / AirPods 2 (A2031/A2032) (в кейс)",
        subject="аккумулятор",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.planned_sku == "OEM-BAT-AIRP2-A2031-A2032-CK"


def test_generate_apple_battery_watch10_uses_short_device_code(db_session) -> None:
    product = Product(
        article="bat-watch10",
        name="Аккумулятор для Apple Watch 10 (46 мм) (ORIG100) (Снятый)",
        subject="аккумулятор",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "IPHW1046"


def test_generate_battery_rev_uses_parenthesized_premium_only(db_session) -> None:
    family = Product(
        article="bat-huawei-family-not-premium",
        name="Аккумулятор для Huawei P10 (VTR-L29) / Honor 9/9 Premium (STF-L09) (HB386589ECW)",
        subject="аккумулятор",
        battery_capacity_mah=3200,
    )
    premium = Product(
        article="bat-huawei-real-premium",
        name="Аккумулятор для Huawei P10 (VTR-L29) / Honor 9/9 Premium (STF-L09) (HB386589ECW) (Premium)",
        subject="аккумулятор",
        battery_capacity_mah=3200,
    )
    db_session.add_all([family, premium])
    db_session.commit()
    db_session.refresh(family)
    db_session.refresh(premium)

    family_result = generate_sku_for_product(db_session, family)
    premium_result = generate_sku_for_product(db_session, premium)

    assert family_result.status == "generated"
    assert family_result.planned_sku == "OEM-BAT-HWE-P10-3200"
    assert premium_result.status == "generated"
    assert premium_result.planned_sku == "OEM-BAT-HWE-P10-3200-PR"


def test_generate_battery_rev_captures_exynos_variant(db_session) -> None:
    product = Product(
        article="bat-samsung-exynos",
        name="Аккумулятор для Samsung A146 Galaxy A14 5G (Exynos) (EB-BA146ABY) (Premium)",
        subject="аккумулятор",
        battery_capacity_mah=5000,
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.planned_sku == "OEM-BAT-SMG-A146-5000-PR-EXY"


def test_generate_samsung_battery_s10_lite_uses_short_device_code(db_session) -> None:
    product = Product(
        article="bat-samsung-s10-lite",
        name="Аккумулятор для Samsung G770 Galaxy S10 Lite (EB-BA907ABY) (ORIG100) (SP)",
        subject="аккумулятор",
        battery_capacity_mah=4500,
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "SMG-S10LT"


def test_generate_samsung_battery_np300e_uses_short_device_code(db_session) -> None:
    product = Product(
        article="bat-samsung-np300e",
        name="Аккумулятор для Samsung NP300E / NP300V / NP305E (AA-PB9NC6B) (11.1 В, 4400 мАч)",
        subject="аккумулятор",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "SMG-NP300E"


def test_generate_huawei_battery_watch_ultimate_design_typo_uses_short_device_code(
    db_session,
) -> None:
    product = Product(
        article="bat-huawei-watch-ultimate-disign",
        name="Аккумулятор для Huawei Watch Ultimate Disign (55020BET)",
        subject="аккумулятор",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "HWE-WUD"


def test_generate_battery_does_not_reuse_stale_active_rev(db_session) -> None:
    product = Product(
        article="bat-stale-rev",
        name="Аккумулятор для Huawei P10 (VTR-L09/VTR-L29) / Honor 9/9 Premium (STF-L09) (HB386280ECW)",
        subject="аккумулятор",
        battery_capacity_mah=3200,
    )
    db_session.add(product)
    db_session.flush()
    db_session.add(
        ProductSkuPlan(
            product=product,
            planned_sku="OEM-BAT-HWE-P10-3200-PR",
            brand_code="OEM",
            category_code="BAT",
            device_code="HWE-P10",
            key_code="3200",
            rev="PR",
            status="generated",
            source="rules",
            is_active=True,
        )
    )
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.rev is None
    assert result.planned_sku == "OEM-BAT-HWE-P10-3200"


def test_generate_huawei_battery_family_premium_gets_family_rev(db_session) -> None:
    family = Product(
        article="bat-huawei-h50-n9-family",
        name="Аккумулятор для Huawei Honor 50 (NTH-NX9) / Nova 9 (NAM-LX9) (HB476489EFW) (Premium)",
        subject="аккумулятор",
        battery_capacity_mah=4300,
    )
    specific = Product(
        article="bat-huawei-n9-premium",
        name="Аккумулятор для Huawei Nova 9 (NAM-LX9) (HB476489EFW) (Premium)",
        subject="аккумулятор",
        battery_capacity_mah=4300,
    )
    db_session.add_all([family, specific])
    db_session.commit()
    db_session.refresh(family)
    db_session.refresh(specific)

    family_result = generate_sku_for_product(db_session, family)
    specific_result = generate_sku_for_product(db_session, specific)

    assert family_result.status == "generated"
    assert family_result.planned_sku == "OEM-BAT-HWE-N9-4300-PR-H50"
    assert specific_result.status == "generated"
    assert specific_result.planned_sku == "OEM-BAT-HWE-N9-4300-PR"


def test_generate_infinix_battery_hot_60_pro_plus_uses_distinct_device_code(db_session) -> None:
    product = Product(
        article="bat-infinix-hot-60-pro-plus",
        name="Аккумулятор для Infinix Hot 60 Pro+ (X6886) (BL-50FX) (Premium)",
        subject="аккумулятор",
        battery_capacity_mah=5160,
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "INF-H60PP"


def test_generate_vivo_battery_v60_lite_5g_uses_distinct_device_code(db_session) -> None:
    product = Product(
        article="bat-vivo-v60-lite-5g",
        name="Аккумулятор для Vivo V60 Lite 5G (BA93) (Premium)",
        subject="аккумулятор",
        battery_capacity_mah=6500,
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "VVO-V60L5"


def test_generate_battery_reverse_polarity_gets_rev(db_session) -> None:
    product = Product(
        article="bat-jbl-reverse-polarity",
        name="Аккумулятор для JBL Charge 2 Plus / Charge 2+ (CS-JML310SL) (обратная полярность)",
        subject="аккумулятор",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.planned_sku == "OEM-BAT-JBL-CH2P-CS-JML310SL-RP"


def test_generate_lenovo_battery_vibe_p1_family_uses_short_device_code(db_session) -> None:
    product = Product(
        article="bat-lenovo-vibe-p1-family",
        name="Аккумулятор для Lenovo Vibe P1 / Vibe P1 Pro / Vibe P1 Turbo (BL244)",
        subject="аккумулятор",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "LEN-VP1P"


def test_generate_nokia_battery_7510_supernova_uses_short_device_code(db_session) -> None:
    product = Product(
        article="bat-nokia-7510",
        name="Аккумулятор для Nokia 7510 Supernova / 2600 Classic (BL-5BT)",
        subject="аккумулятор",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "NOK-7510"


def test_generate_oneplus_battery_nord_ce_uses_short_device_code(db_session) -> None:
    product = Product(
        article="bat-oneplus-nord-ce",
        name="Аккумулятор для OnePlus Nord CE 5G (EB2103) (BLP845)",
        subject="аккумулятор",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "ONE-NCE5"


def test_generate_oneplus_battery_nord_n30_se_uses_distinct_device_code(db_session) -> None:
    product = Product(
        article="bat-one-n30se",
        name="Аккумулятор для OnePlus Nord N30 SE (CPH2605) (Premium)",
        subject="аккумулятор",
        battery_capacity_mah=5000,
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "ONE-NN30SE"


def test_generate_asus_battery_zenfone_zoom_uses_short_device_code(db_session) -> None:
    product = Product(
        article="bat-asus-zenfone-zoom",
        name="Аккумулятор для Asus ZenFone Zoom (ZX551ML) (C11P1507)",
        subject="аккумулятор",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "ASU-ZFZOOM"


def test_generate_asus_battery_zenfone_go_family_uses_short_device_code(db_session) -> None:
    product = Product(
        article="bat-asu-zfgo45",
        name="Аккумулятор для Asus ZenFone Go (ZB450KL) / ZenFone Go (ZB452KG) (B11P1428)",
        subject="аккумулятор",
        battery_capacity_mah=2000,
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "ASU-ZFGO45"


def test_generate_asus_battery_a43_family_uses_short_device_code(db_session) -> None:
    product = Product(
        article="bat-asu-a43",
        name="Аккумулятор для Asus A43 / A53 / K43 / K53 / X43 / X44 / X53 / X54 (A43EI241SV-SL) (5200 мАч)",
        subject="аккумулятор",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "ASU-A43"


def test_generate_sony_battery_xperia_x_performance_uses_short_device_code(db_session) -> None:
    product = Product(
        article="bat-sony-x-performance",
        name="Аккумулятор для Sony F8131 Xperia X Perfomance/F8132 Xperia X Perfomance Dual (LIP1624ERPC)",
        subject="аккумулятор",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "SON-XPERF"


def test_generate_sony_battery_xperia_z_and_c_family_uses_short_device_code(db_session) -> None:
    product = Product(
        article="bat-son-xzc",
        name="Аккумулятор для Sony C6603/LT36i Xperia Z / C2305 Xperia C (LIS1502ERPC)",
        subject="аккумулятор",
        battery_capacity_mah=2330,
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "SON-XZC"


def test_generate_sony_battery_vpcs_family_uses_short_device_code(db_session) -> None:
    product = Product(
        article="bat-son-vpcs",
        name="Аккумулятор для Sony VPC-SA, VPC-SB, VPC-SE, SV-S (VGP-BPS24) (11.1 В, 4400-5200 мАч)",
        subject="аккумулятор",
        battery_capacity_mah=5200,
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "SON-VPCS"


def test_generate_zte_battery_blade_l4_uses_short_device_code(db_session) -> None:
    product = Product(
        article="bat-zte-blade-l4",
        name="Аккумулятор для ZTE Blade L4 (Li3822T43P3h736044)",
        subject="аккумулятор",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "ZTE-BL4"


def test_generate_zte_battery_blade20smart_family_uses_distinct_device_code(db_session) -> None:
    product = Product(
        article="bat-zte-b20s",
        name="Аккумулятор для ZTE Blade 20 Smart / Blade A6 / Blade A6 Lite / Blade V30 / Blade V30 Vita (Li3949T44P8h906450)",
        subject="аккумулятор",
        battery_capacity_mah=5000,
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    result = generate_sku_for_product(db_session, product)

    assert result.status == "generated"
    assert result.device_code == "ZTE-B20S"
