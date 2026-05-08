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
