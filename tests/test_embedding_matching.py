from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import numpy as np
from sqlalchemy import select

from app.models import CompetitorItem, Product
from app.models.competitor_item_compatibility import CompetitorItemCompatibility
from app.models.competitor_item_match import (
    CompetitorItemMatch,
    CompetitorItemMatchMethod,
    CompetitorItemMatchStatus,
)
from app.models.device_model import PhoneModel
from app.models.product_phone_model import ProductPhoneModel
from app.services.matching_guardrails import basic_candidate_guardrails, device_group_conflict
from tasks.match_competitor_items_embeddings import (
    _auto_accept_display_construction_matches,
    _auto_accept_display_matrix_tag_matches,
    _auto_accept_display_matrix_type_matches,
    _auto_accept_display_original_quality_matches,
    _auto_accept_explicit_code_overlap_matches,
    _auto_accept_explicit_model_text_matches,
    _auto_reject_battery_part_code_conflicts,
    _auto_reject_camera_position_conflicts,
    _auto_reject_display_attribute_conflicts,
    _auto_reject_display_color_conflicts,
    _auto_reject_display_frame_conflicts,
    _auto_reject_display_long_model_code_conflicts,
    _auto_reject_display_matrix_tag_conflicts,
    _auto_reject_display_module_component_conflicts,
    _auto_reject_display_subject_conflicts,
    _auto_reject_display_text_model_conflicts,
    _auto_reject_explicit_model_conflicts,
    _auto_reject_flex_role_conflicts,
    _auto_reject_guardrail_device_group_conflicts,
    _auto_reject_housing_device_code_conflicts,
    _auto_reject_housing_part_kind_conflicts,
    _auto_reject_laptop_matrix_flex_conflicts,
    _auto_reject_non_display_model_code_conflicts,
    _auto_reject_part_assembly_conflicts,
    _auto_reject_part_color_conflicts,
    _auto_reject_part_quality_conflicts,
    _competitor_display_has_frame,
    _competitor_display_mapped_1c_quality_raw,
    _competitor_display_matrix_tags,
    _competitor_display_matrix_vendor_tags,
    _competitor_display_quality,
    _competitor_display_quality_raw,
    _effective_item_type,
    _explicit_display_attribute_conflict_reason,
    _explicit_display_subject_conflict_reason,
    _explicit_model_conflict_reason,
    _extract_device_codes,
    _extract_device_model_keys,
    _flex_button_control_conflict,
    _flex_fingerprint_conflict,
    _json_report_default,
    _phone_sim_tray_model_or_code_conflict,
    _product_display_matrix_tags,
    _product_display_matrix_vendor_tags,
    _product_display_quality,
    _safe_battery_part_code_model_suggest,
    _safe_battery_verification_suggest,
    _safe_disposable_battery_suggest,
    _safe_flex_suggest,
    _safe_housing_part_suggest,
    _safe_iphone_battery_model_capacity_suggest,
    _safe_phone_camera_glass_suggest,
    _safe_phone_sim_tray_suggest,
    _safe_stencil_suggest,
    match_items,
)


def test_json_report_default_serializes_decimal_and_dates():
    payload = {
        "score": Decimal("0.8179"),
        "seen_at": date(2026, 5, 10),
    }

    dumped = json.dumps(payload, default=_json_report_default)

    assert json.loads(dumped) == {"score": 0.8179, "seen_at": "2026-05-10"}


def test_basic_guardrails_reject_usb_flash_against_cable():
    item = CompetitorItem(
        competitor="moba",
        external_id="USBF-30-128GB-HCO-UD5-SL",
        name="USB-флеш (USB 3.0) 128GB Hoco UD5 Wisdom Серебро",
        normalized_title="USB-флеш USB 3.0 128GB Hoco UD5 Wisdom Серебро",
        item_type="other",
    )
    product = Product(
        name="Дата-кабель Hoco X88 USB - TypeC 1 м 3A (белый)",
        article="075499",
    )

    result = basic_candidate_guardrails(item, product)

    assert result.allowed is False
    assert result.reason == "catalog_family_conflict"


def test_device_group_conflict_handles_notebook_vs_phone_tokens():
    assert device_group_conflict(
        "Крышка матрицы для ноутбука Lenovo IdeaPad 3-15IML05 Серебро",
        "Задняя крышка для Lenovo IdeaPhone S850 (белый)",
    )
    assert device_group_conflict(
        "Аккумулятор Samsung S25+/S25 FE (S936B/S731B)",
        "Аккумулятор для Samsung NP300E / NP300V / NP305E (AA-PB9NC6B)",
    )
    assert device_group_conflict(
        "Шлейф/FLC Tecno PHANTOM V FOLD на системный разъём/микрофон",
        "Шлейф для Meta Quest 3S VR (на микрофон и динамики)",
    )


def test_basic_guardrails_reject_oca_touchscreen_against_display_repair_product():
    product = Product(
        name="Дисплей для Apple iPhone 12 mini + тачскрин (черный) (ORIG) (Переклейка)",
        article="045420",
        category="Дисплеи для телефонов",
        subject="дисплей",
    )
    item = CompetitorItem(
        competitor="moba",
        external_id="GLS-PMIMI-OCA-B-OR",
        name="Стекло для переклейки iPhone 12 mini (A2399) с OCA пленкой + тачскрин Черный - OR",
        normalized_title="Стекло для переклейки iPhone 12 mini с OCA пленкой тачскрин Черный OR",
        item_type="display",
        category_group="display",
    )

    result = basic_candidate_guardrails(item, product)

    assert result.allowed is False
    assert result.reason == "display_module_component_conflict"


def test_basic_guardrails_allow_lcd_change_glass_display_candidate():
    product = Product(
        name="Дисплей для Apple iPhone 12 mini + тачскрин (черный) (ORIG) (Переклейка)",
        article="045420",
        category="Дисплеи для телефонов",
        subject="дисплей",
    )
    item = CompetitorItem(
        competitor="liberti",
        external_id="460667",
        name="LCD дисплей для Apple iPhone 12 Mini (черный) с тачскрином original (change glass)",
        normalized_title="LCD дисплей Apple iPhone 12 Mini черный тачскрин original change glass",
        item_type="display",
        category_group="display",
    )

    result = basic_candidate_guardrails(item, product)

    assert result.allowed is True


def test_basic_guardrails_reject_battery_form_factor_conflict():
    item = CompetitorItem(
        competitor="moba",
        external_id="BAT-KDK-6F22-CR",
        name='Батарейка "Крона" 6F22 Kodak 9V',
        normalized_title='Батарейка "Крона" 6F22 Kodak 9V',
        item_type="battery",
    )
    product = Product(name="Батарейки Kodak AAA 4 шт.", article="068843")

    result = basic_candidate_guardrails(item, product)

    assert result.allowed is False
    assert result.reason == "catalog_family_conflict"


def test_basic_guardrails_reject_battery_coin_form_factor_conflict():
    item = CompetitorItem(
        competitor="moba",
        external_id="BAT-KDK-6F22-CR",
        name='Батарейка "Крона" 6F22 Kodak 9V',
        normalized_title='Батарейка "Крона" 6F22 Kodak 9V',
        item_type="battery",
    )
    product = Product(name="Батарейки AG13/LR44H/357A 10 шт.", article="064417")

    result = basic_candidate_guardrails(item, product)

    assert result.allowed is False
    assert result.reason == "catalog_family_conflict"


def test_basic_guardrails_reject_9v_battery_against_23a_battery():
    item = CompetitorItem(
        competitor="moba",
        external_id="BAT-GP-6LR61-CR",
        name='Батарейка "Крона" 6LR61 GP Super Alkaline 9V',
        normalized_title='Батарейка "Крона" 6LR61 GP Super Alkaline 9V',
        item_type="battery",
    )
    product = Product(name="Батарейки GP Super 23A 5 шт.", article="069943")

    result = basic_candidate_guardrails(item, product)

    assert result.allowed is False
    assert result.reason == "catalog_family_conflict"


def test_basic_guardrails_reject_laptop_power_supply_against_console_power_supply():
    item = CompetitorItem(
        competitor="moba",
        external_id="PWS-LP-ACR-19V474A90W-5517",
        name="Блок питания (сетевой адаптер) для ноутбука Acer 19V, 4,74A, 90W",
        normalized_title="Блок питания сетевой адаптер для ноутбука Acer 19V 4.74A 90W",
        item_type="other",
    )
    product = Product(name="Блок питания для Sony PS4 (ADP-240AR) (черный)", article="079392")

    result = basic_candidate_guardrails(item, product)

    assert result.allowed is False
    assert result.reason == "device_group_conflict"


def test_basic_guardrails_reject_laptop_power_supply_against_phone_charger():
    item = CompetitorItem(
        competitor="moba",
        external_id="PWS-LP-ACR-19V474A90W-5517",
        name="Блок питания (сетевой адаптер) для ноутбука Acer 19V, 4,74A, 90W",
        normalized_title="Блок питания сетевой адаптер для ноутбука Acer 19V 4.74A 90W",
        item_type="other",
    )
    product = Product(
        name="Сетевое зарядное устройство Baseus Palm TypeC + USB 20W (черный)",
        article="075455",
    )

    result = basic_candidate_guardrails(item, product)

    assert result.allowed is False
    assert result.reason == "catalog_family_conflict"


def test_basic_guardrails_reject_laptop_power_supply_against_laptop_fan():
    item = CompetitorItem(
        competitor="moba",
        external_id="PWS-MC-202V43A87W-TPC",
        name="Блок питания (сетевой адаптер) для ноутбука Apple 20,2V, 4,3A, 87W (Type-C)",
        normalized_title="Блок питания сетевой адаптер для ноутбука Apple 20.2V 4.3A 87W Type-C",
        item_type="other",
    )
    product = Product(
        name="Вентилятор (кулер) для Apple MacBook Air 13 M1 Retina A2337 (LATE 2020)",
        article="058430",
    )

    result = basic_candidate_guardrails(item, product)

    assert result.allowed is False
    assert result.reason == "catalog_family_conflict"


def test_basic_guardrails_reject_tool_battery_charger_against_phone_charger():
    item = CompetitorItem(
        competitor="moba",
        external_id="SZU-BTT-14418V35А-MKT",
        name="Сетевое зарядное устройство для аккумуляторов 14,4-18V, 3,5А (Makita тип)",
        normalized_title="Сетевое зарядное устройство для аккумуляторов 14,4-18V 3,5А Makita тип",
        item_type="other",
    )
    product = Product(
        name="Сетевое зарядное устройство Baseus Palm TypeC + USB 20W (черный)",
        article="075455",
    )

    result = basic_candidate_guardrails(item, product)

    assert result.allowed is False
    assert result.reason == "catalog_family_conflict"


def test_basic_guardrails_reject_usb_a_solder_connector_against_phone_charge_port():
    item = CompetitorItem(
        competitor="moba",
        external_id="CC-USBA-20F",
        name="Разъем USB-A 2.0 (F) под пайку",
        normalized_title="Разъем USB-A 2.0 F под пайку",
        item_type="connector",
    )
    product = Product(name="Разъем зарядки Huawei P20 Lite", article="041498")

    result = basic_candidate_guardrails(item, product)

    assert result.allowed is False
    assert result.reason == "catalog_family_conflict"


def test_basic_guardrails_reject_rj45_connector_against_patch_cord():
    item = CompetitorItem(
        competitor="moba",
        external_id="CON-RJ45-CAT6-10PCS",
        name="Сквозной коннектор для витой пары RJ-45, CAT6 (10 шт)",
        normalized_title="Сквозной коннектор для витой пары RJ-45 CAT6",
        item_type="connector",
    )
    product = Product(name="Патч-корд Baseus RJ45 1,5 м (1 гБит) (черный)", article="075440")

    result = basic_candidate_guardrails(item, product)

    assert result.allowed is False
    assert result.reason == "catalog_family_conflict"


def test_basic_guardrails_reject_iphone_battery_against_airpods_battery():
    item = CompetitorItem(
        competitor="moba",
        external_id="BTT-PMI120-VRF-HC-NEW",
        name='Аккумулятор для iPhone 12/12 Pro с верификацией "Новая запчасть" - усиленная 3310 mAh',
        normalized_title="Аккумулятор iPhone 12 12 Pro усиленная 3310 mAh",
        item_type="battery",
    )
    product = Product(name="Аккумулятор для Apple AirPods Pro (A2083/A2084) (в кейс)")

    result = basic_candidate_guardrails(item, product)

    assert result.allowed is False
    assert result.reason == "device_group_conflict"


def test_basic_guardrails_reject_headset_against_wifi_antenna():
    item = CompetitorItem(
        competitor="liberti",
        external_id="244539",
        name="Bluetooth беспроводная гарнитура Samsung Level U (черная/коробка)",
        normalized_title="Bluetooth беспроводная гарнитура Samsung Level U черная коробка",
        item_type="other",
    )
    product = Product(name="Антена Wi-Fi Hoco HI37 USB (черный)", article="079629")

    result = basic_candidate_guardrails(item, product)

    assert result.allowed is False
    assert result.reason == "catalog_family_conflict"


def test_basic_guardrails_reject_ic_against_wifi_router():
    item = CompetitorItem(
        competitor="liberti",
        external_id="263138",
        name="Микросхема Hi1101 (Wi-Fi модуль для Huawei)",
        normalized_title="Микросхема Hi1101 Wi-Fi модуль Huawei",
        item_type="board",
    )
    product = Product(name="Роутер Wi-Fi Hoco HI36 2.4 Гц (белый)", article="079630")

    result = basic_candidate_guardrails(item, product)

    assert result.allowed is False
    assert result.reason == "catalog_family_conflict"


def test_basic_guardrails_reject_ic_against_spudger_set():
    item = CompetitorItem(
        competitor="moba",
        external_id="IC-74AVC1T45",
        name="Микросхема 74AVC1T45 (двунаправленный преобразователь уровней логики, 1-бит)",
        normalized_title="Микросхема 74AVC1T45 двунаправленный преобразователь уровней логики 1 бит",
        item_type="board",
    )
    product = Product(name="Набор лопаток для снятия микросхем 10 в 1", article="055997")

    result = basic_candidate_guardrails(item, product)

    assert result.allowed is False
    assert result.reason == "catalog_family_conflict"


def test_basic_guardrails_reject_buzzer_against_middle_frame():
    item = CompetitorItem(
        competitor="moba",
        external_id="BUZ-TCN-CMN-40-PR-4G-CP",
        name="Звонок (buzzer) для Tecno Camon 40 Pro 4G/5G (CM6/CM7) в сборе",
        normalized_title="Звонок buzzer Tecno Camon 40 Pro 4G 5G CM6 CM7 в сборе",
        item_type="other",
    )
    product = Product(
        name="Средняя часть для Tecno Camon 40 Pro 5G (CM7) (черный)",
        article="070492",
    )

    result = basic_candidate_guardrails(item, product)

    assert result.allowed is False
    assert result.reason == "catalog_family_conflict"


def test_basic_guardrails_reject_ic_against_display_backlight():
    item = CompetitorItem(
        competitor="moba",
        external_id="IC-343S00480",
        name="Микросхема 343S00480 (Контроллер зарядки для iPad Pro 12.9 2021)",
        normalized_title="Микросхема 343S00480 Контроллер зарядки iPad Pro 12.9 2021",
        item_type="board",
    )
    product = Product(
        name="Подсветка дисплея для Apple iPad Pro 12.9 (2018)",
        article="067697",
    )

    result = basic_candidate_guardrails(item, product)

    assert result.allowed is False
    assert result.reason == "catalog_family_conflict"


def test_basic_guardrails_reject_keyboard_backlight_against_keyboard():
    item = CompetitorItem(
        competitor="moba",
        external_id="BKL-KPD-LP-MB-PR-M1-13-A2338-B",
        name='Подсветка клавиатуры для ноутбука MacBook Pro M1 13"/M2 13" A2338 Черный',
        normalized_title="Подсветка клавиатуры MacBook Pro M1 13 M2 13 A2338 Черный",
        item_type="other",
    )
    product = Product(
        name="Клавиатура для Apple MacBook Pro 13 M1 Retina A2338 (вертикальный Enter / русская раскладка)",
        article="065445",
    )

    result = basic_candidate_guardrails(item, product)

    assert result.allowed is False
    assert result.reason == "catalog_family_conflict"


def test_safe_battery_verification_suggest_accepts_diagnosable_typo():
    item = CompetitorItem(
        competitor="moba",
        external_id="BTT-PMI130-MINI-VRF-HC-NEW",
        name='Аккумулятор для iPhone 13 mini с верификацией "Новая запчасть" - усиленная 2550 mAh',
        normalized_title="Аккумулятор iPhone 13 mini верификация Новая запчасть усиленная 2550 mAh",
        item_type="battery",
    )
    product = Product(
        name=(
            "Аккумулятор для Apple iPhone 13 mini (F5ENERGY) (усиленный) "
            "(2560 мАч) (SPECIAL EDITION) (SYSTEM DAIGNOSABLE) + двухсторонний скотч"
        )
    )

    assert _safe_battery_verification_suggest(item, product, score=0.73)


def test_safe_iphone_battery_model_capacity_suggest_accepts_battery_collection():
    item = CompetitorItem(
        competitor="moba",
        external_id="BTT-PMI140-PLS-HC",
        name="Аккумулятор для iPhone 14 Plus - Battery Collection - усиленная 4810 mAh",
        normalized_title="Аккумулятор iPhone 14 Plus Battery Collection усиленная 4810 mAh",
        item_type="battery",
    )
    product = Product(
        name=(
            "Аккумулятор для Apple iPhone 14 Plus (F5ENERGY) (усиленный) "
            "(4850 мАч) (SPECIAL EDITION) (SYSTEM DIAGNOSABLE) + двухсторонний скотч"
        )
    )
    wrong_capacity = Product(name="Аккумулятор для Apple iPhone 14 Plus (усиленный) (5200 мАч)")

    assert _safe_iphone_battery_model_capacity_suggest(item, product, score=0.70)
    assert not _safe_iphone_battery_model_capacity_suggest(
        item,
        wrong_capacity,
        score=0.90,
    )


def test_safe_battery_part_code_model_suggest_allows_close_alternatives():
    item = CompetitorItem(
        competitor="moba",
        external_id="BTT-ONE-BLP761",
        name="Аккумулятор для OnePlus 8 (BLP761) - Battery Collection",
        normalized_title="Аккумулятор OnePlus 8 BLP761 Battery Collection",
        item_type="battery",
    )
    product = Product(
        name="Аккумулятор для OnePlus 8 (BLP761)",
        article="055805",
        category="battery",
        subject="аккумулятор",
    )
    wrong_code = Product(
        name="Аккумулятор для OnePlus 8 Pro (BLP759)",
        article="047312",
        category="battery",
        subject="аккумулятор",
    )

    assert _safe_battery_part_code_model_suggest(
        item,
        product,
        filtered_count=2,
        score=0.70,
    )
    assert not _safe_battery_part_code_model_suggest(
        item,
        wrong_code,
        filtered_count=2,
        score=0.90,
    )


def test_safe_disposable_battery_suggest_requires_brand_size_and_pack_count():
    item = CompetitorItem(
        competitor="moba",
        external_id="BAT-VRT-LR6-AA-4PCS",
        name="Батарейка AA LR6 Varta ENERGY 1.5V (4 шт. в блистере)",
        normalized_title="Батарейка AA LR6 Varta ENERGY 1.5V 4 шт",
        item_type="other",
    )
    product = Product(name="Батарейки Varta LR6 AA 4 шт.")
    wrong_brand = Product(name="Батарейки Energizer LR6 AA 4 шт.")

    assert _safe_disposable_battery_suggest(item, product, score=0.81)
    assert not _safe_disposable_battery_suggest(item, wrong_brand, score=0.81)


def test_safe_stencil_suggest_requires_chipset_or_series_overlap():
    item = CompetitorItem(
        competitor="moba",
        external_id="TLS-BGA-XZZ-SDN-662",
        name="BGA трафарет XZZ Snapdragon 662 для Xiaomi Redmi Note 9 4G/Note 9 Pro",
        normalized_title="BGA трафарет XZZ Snapdragon 662 Xiaomi Redmi Note 9 4G Note 9 Pro",
        item_type="other",
    )
    product = Product(
        name="Трафарет BGA XZZ Snapdragon 662 для Xiaomi Redmi Note 9 4G / Note 9 Pro",
        article="077332",
    )
    wrong_chipset = Product(
        name="Трафарет BGA XZZ Snapdragon 665 для Xiaomi Redmi Note 10 / 10 Pro",
        article="077333",
    )

    assert _safe_stencil_suggest(item, product, score=0.90)
    assert not _safe_stencil_suggest(item, wrong_chipset, score=0.95)


def test_safe_stencil_suggest_allows_low_score_iphone_series_overlap():
    item = CompetitorItem(
        competitor="liberti",
        external_id="474050",
        name="Трафареты для реболлинга Mijing Z20 Pro iPhone 16 серии (16-16 Plus/16 Pro-16 Pro Max/16E)",
        normalized_title="Трафареты реболлинг Mijing Z20 Pro iPhone 16 серии 16 16 Plus 16 Pro 16 Pro Max 16E",
        item_type="other",
    )
    product = Product(
        name="Трафарет BGA XZZ для Apple iPhone 16 / 16 Plus / 16 Pro / 16 Pro Max",
        article="077308",
    )
    wrong_series = Product(
        name="Трафарет BGA XZZ для Apple iPhone 15 / 15 Plus / 15 Pro / 15 Pro Max",
        article="077309",
    )

    assert _safe_stencil_suggest(item, product, score=0.69)
    assert not _safe_stencil_suggest(item, wrong_series, score=0.90)


def test_basic_guardrails_reject_disposable_battery_size_conflict():
    item = CompetitorItem(
        competitor="moba",
        external_id="BAT-GP-6LR61-9V",
        name='Батарейка "Крона" 6LR61 GP Super Alkaline 9V',
        normalized_title="Батарейка Крона 6LR61 GP Super Alkaline 9V",
        item_type="other",
    )
    product = Product(name="Батарейки GP Super LR20 D 2 шт.")

    result = basic_candidate_guardrails(item, product)

    assert result.allowed is False
    assert result.reason == "catalog_family_conflict"


def test_basic_guardrails_reject_power_bank_against_external_storage():
    item = CompetitorItem(
        competitor="liberti",
        external_id="PB-XIAOMI-20000-PB2033-B",
        name="Внешний аккумулятор Xiaomi Mi 20000 33W встроенный кабель USB-C PB2033 (черный)",
        normalized_title="Внешний аккумулятор Xiaomi Mi 20000 33W встроенный кабель USB-C PB2033 черный",
        item_type="battery",
    )
    product = Product(
        name="Внешний накопитель Baseus Amblight 26800 mAh с кабелем TypeC - TypeC 100W (черный)"
    )

    result = basic_candidate_guardrails(item, product)

    assert result.allowed is False
    assert result.reason == "catalog_family_conflict"


def test_safe_phone_camera_glass_suggest_requires_model_color_and_frame_match():
    item = CompetitorItem(
        competitor="liberti",
        external_id="CAM-GLASS-RMX3286-B",
        name="Стекло задней камеры для Realme Narzo 50 (RMX3286) (без рамки) (черный)",
        normalized_title="Стекло задней камеры Realme Narzo 50 RMX3286 без рамки черный",
        item_type="other",
    )
    product = Product(
        name="Стекло задней камеры для Realme Narzo 50 4G (RMX3286) (без рамки) (черный)"
    )
    wrong_frame = Product(
        name="Стекло задней камеры для Realme Narzo 50 4G (RMX3286) (в рамке) (черный)"
    )

    assert _safe_phone_camera_glass_suggest(item, product, score=0.91)
    assert not _safe_phone_camera_glass_suggest(item, wrong_frame, score=0.91)


def test_safe_phone_camera_glass_suggest_rejects_pack_count_mismatch():
    item = CompetitorItem(
        competitor="moba",
        external_id="GLS-CAM-SSG-S936B-3PCS-B",
        name="Стекло камеры для Samsung Galaxy S25+ (S936B) (комплект 3 шт.) Черный",
        normalized_title="Стекло камеры Samsung Galaxy S25 Plus S936B комплект 3 шт Черный",
        item_type="other",
    )
    product = Product(name="Стекло задней камеры для Samsung S936 Galaxy S25+ (без рамки) (черный)")

    assert not _safe_phone_camera_glass_suggest(item, product, score=0.90)


def test_safe_phone_sim_tray_suggest_requires_model_or_code_and_color_match():
    item = CompetitorItem(
        competitor="moba",
        external_id="SIM-XIA-25118PC98G-B",
        name="Держатель SIM для Xiaomi Poco M8 5G (25118PC98G) Черный",
        normalized_title="Держатель SIM Xiaomi Poco M8 5G 25118PC98G черный",
        item_type="other",
    )
    product = Product(name="Держатель сим-карты для Xiaomi Poco M8 5G (25118PC98G) (черный)")
    wrong_color = Product(name="Держатель сим-карты для Xiaomi Poco M8 5G (25118PC98G) (зеленый)")

    assert _safe_phone_sim_tray_suggest(item, product, score=0.89)
    assert not _safe_phone_sim_tray_suggest(item, wrong_color, score=0.89)


def test_safe_phone_sim_tray_suggest_allows_lower_score_with_exact_model_and_color():
    item = CompetitorItem(
        competitor="moba",
        external_id="HLD-SIM-PMI-17-B",
        name="Держатель SIM для iPhone 17 (1 Sim) (A3520) Черный",
        normalized_title="Держатель SIM iPhone 17 1 Sim A3520 Черный",
        item_type="other",
    )
    product = Product(
        name="Держатель сим-карты для Apple iPhone 17 (SIM + eSIM) (черный)",
        article="071778",
    )

    assert _safe_phone_sim_tray_suggest(item, product, score=0.83)


def test_phone_sim_tray_model_or_code_conflict_rejects_old_iphone_candidate():
    item = CompetitorItem(
        competitor="moba",
        external_id="HLD-SIM-PMI-16-PN",
        name="Держатель SIM для iPhone 16/16 Plus (1 Sim) (A3287/A3286/A3290/A3289) Розовый",
        normalized_title="Держатель SIM iPhone 16 16 Plus 1 Sim A3287 A3286 A3290 A3289 Розовый",
        item_type="other",
    )
    product = Product(
        name="Держатель сим-карты для Apple iPhone 6s Plus (розовый)",
        article="050720",
    )

    assert _phone_sim_tray_model_or_code_conflict(item, product)


def test_safe_housing_part_suggest_requires_kind_model_or_code_and_color_match():
    item = CompetitorItem(
        competitor="liberti",
        external_id="BC-SAM-SM-S921-Y",
        name="Задняя крышка для Samsung Galaxy S24 SM-S921 (желтый), премиум",
        normalized_title="Задняя крышка Samsung Galaxy S24 SM-S921 желтый премиум",
        item_type="housing",
    )
    product = Product(name="Задняя крышка для Samsung S921 Galaxy S24 (желтый)")
    wrong_color = Product(name="Задняя крышка для Samsung S921 Galaxy S24 (фиолетовый)")
    original = Product(
        name="Задняя крышка для Samsung S921 Galaxy S24 (желтый) (ORIG100)",
        quality="Original",
    )

    assert _safe_housing_part_suggest(item, product, score=0.81)
    assert not _safe_housing_part_suggest(item, wrong_color, score=0.81)
    assert not _safe_housing_part_suggest(item, original, score=0.81)


def test_safe_flex_suggest_rejects_extra_fingerprint_component():
    item = CompetitorItem(
        competitor="liberti",
        external_id="456858",
        name="Шлейф/FLC Xiaomi Poco F3 на кнопки громкости/включения",
        normalized_title="Шлейф Xiaomi Poco F3 кнопки громкости включения",
        item_type="flex",
    )
    product = Product(
        name=(
            "Шлейф для Xiaomi Poco F3 (M2012K11AG) с комп. + сканер отпечатка пальца "
            "(кнопка включения) (черный)"
        )
    )

    assert _flex_fingerprint_conflict(item, product)
    assert not _safe_flex_suggest(item, product, score=0.82)


def test_safe_flex_suggest_rejects_partial_button_controls():
    item = CompetitorItem(
        competitor="liberti",
        external_id="447033",
        name="Шлейф/FLC Xiaomi Poco F3 на кнопки громкости/включения",
        normalized_title="Шлейф Xiaomi Poco F3 кнопки громкости включения",
        item_type="flex",
    )
    product = Product(name="Шлейф для Xiaomi Poco F3 (M2012K11AG) с комп. (на кнопки громкости)")

    assert _flex_button_control_conflict(item, product)
    assert not _safe_flex_suggest(item, product, score=0.82)


def test_safe_flex_suggest_accepts_buttons_model_and_color_match():
    item = CompetitorItem(
        competitor="liberti",
        external_id="FLC-XIA-REDMI-A5-B",
        name="Шлейф/FLC Xiaomi Redmi A5/Poco C71 на кнопки громкости/включения",
        normalized_title="Шлейф Xiaomi Redmi A5 Poco C71 кнопки громкости включения",
        item_type="flex",
    )
    product = Product(
        name=(
            "Шлейф для Xiaomi Redmi A5 (25028RN03A) / Poco C71 (25028PC03G) "
            "с комп. (на кнопку включения и кнопки громкости)"
        )
    )

    assert _safe_flex_suggest(item, product, score=0.81)


def test_basic_guardrails_reject_expanded_phone_brand_conflict():
    item = CompetitorItem(
        competitor="liberti",
        external_id="458537",
        name="Шлейф/FLC Tecno SPARK 20 Pro+ на кнопки громкости/включения",
        normalized_title="Шлейф FLC Tecno SPARK 20 Pro+ на кнопки громкости включения",
        item_type="flex",
    )
    product = Product(
        name="Шлейф для Meizu MX4 Pro с комп. (на кнопки громкости)",
        article="041272",
    )

    result = basic_candidate_guardrails(item, product)

    assert result.allowed is False
    assert result.reason == "brand_group_conflict"


def test_basic_guardrails_reject_laptop_cover_against_phone_cover():
    item = CompetitorItem(
        competitor="moba",
        external_id="SC-CVR-LP-ACR-SP31451-SL",
        name="Крышка матрицы для ноутбука Acer Spin 3 SP314-51/SP314-52 Серебро",
        normalized_title="Крышка матрицы для ноутбука Acer Spin 3 SP314-51 SP314-52 Серебро",
        item_type="housing",
    )
    product = Product(
        name="Задняя крышка для Meizu M5 Note (серебристый)",
        article="039316",
    )

    result = basic_candidate_guardrails(item, product)

    assert result.allowed is False
    assert result.reason == "device_group_conflict"


def test_basic_guardrails_reject_car_holder_against_magnetic_ring():
    item = CompetitorItem(
        competitor="liberti",
        external_id="470106",
        name="Держатель в автомобиль BOROFONE BH14 Journey магнитный, на панель (черный/красный)",
        normalized_title="Держатель в автомобиль BOROFONE BH14 Journey магнитный на панель",
        item_type="housing",
    )
    product = Product(
        name="Металлическое кольцо для магнитного держателя",
        article="065504",
    )

    result = basic_candidate_guardrails(item, product)

    assert result.allowed is False
    assert result.reason == "catalog_family_conflict"


def test_basic_guardrails_reject_laptop_power_supply_against_macbook_cable():
    item = CompetitorItem(
        competitor="moba",
        external_id="PWS-LP-ACR-19V474A90W-5517",
        name="Блок питания (сетевой адаптер) для ноутбука Acer 19V, 4,74A, 90W",
        normalized_title="Блок питания сетевой адаптер для ноутбука Acer 19V 4.74A 90W",
        item_type="other",
    )
    product = Product(
        name="Кабель зарядки для Macbook Hoco U141 (магнитный) TypeC - Magsafe 140W 1.8 м",
        article="079662",
    )

    result = basic_candidate_guardrails(item, product)

    assert result.allowed is False
    assert result.reason == "catalog_family_conflict"


def test_basic_guardrails_reject_laptop_connector_against_keyboard():
    item = CompetitorItem(
        competitor="moba",
        external_id="CC-PW-LP-SSG-N150",
        name="Разъем питания PJ077 (5.5*3.0, 7 pin) для ноутбука Samsung N150/R530/R540/R580",
        normalized_title="Разъем питания PJ077 для ноутбука Samsung N150 R530 R540 R580",
        item_type="connector",
    )
    product = Product(
        name="Клавиатура для Samsung NP300E5A / NP300E5A-A01RU и др. (черный)",
        article="049828",
    )

    result = basic_candidate_guardrails(item, product)

    assert result.allowed is False
    assert result.reason == "catalog_family_conflict"


def test_basic_guardrails_reject_phone_flex_against_test_fixture_flex():
    item = CompetitorItem(
        competitor="liberti",
        external_id="455394",
        name="Шлейф/FLC OPPO A72 4G (CPH2067) межплатный",
        normalized_title="Шлейф FLC OPPO A72 4G CPH2067 межплатный",
        item_type="flex",
    )
    product = Product(
        name="Шлейф проверочного аппарата DL400+ для OPPO Find X7",
        article="071465",
    )

    result = basic_candidate_guardrails(item, product)

    assert result.allowed is False
    assert result.reason == "catalog_family_conflict"


def test_basic_guardrails_reject_tablet_brand_conflict():
    item = CompetitorItem(
        competitor="moba",
        external_id="LCD-XMI-PAD-8-CP-B-OR",
        name='Дисплей для Xiaomi Pad 8/8 Pro 11.2" в сборе с тачскрином Черный - OR',
        normalized_title='Дисплей Xiaomi Pad 8 8 Pro 11.2" Черный OR',
        item_type="display",
        parsed_device_brand="xiaomi",
    )
    product = Product(
        name="Дисплей для Lenovo Xiaoxin IdeaPad Pro 12.7 (TB371FC) + тачскрин (черный) (ORIG)",
        article="067930",
    )

    result = basic_candidate_guardrails(item, product)

    assert result.allowed is False
    assert result.reason == "brand_group_conflict"


def test_basic_guardrails_reject_camera_glass_model_conflict():
    item = CompetitorItem(
        competitor="moba",
        external_id="GLS-CAM-XMI-PCO-X7-PR-CP-GN",
        name="Стекло камеры для Xiaomi Poco X7 Pro (2412DPC0AG) в сборе с рамкой Зеленый",
        normalized_title="Стекло камеры Xiaomi Poco X7 Pro 2412DPC0AG с рамкой Зеленый",
        item_type="other",
        parsed_device_brand="xiaomi",
    )
    product = Product(
        name="Стекло задней камеры для Xiaomi Poco M8 Pro (2510EPC8BG) (в рамке) (зеленый)",
        article="080474",
    )

    result = basic_candidate_guardrails(item, product)

    assert result.allowed is False
    assert result.reason == "strict_model_conflict"


def test_basic_guardrails_reject_speaker_mesh_against_magsafe():
    item = CompetitorItem(
        competitor="moba",
        external_id="SPK-MSH-PMI-16-PR-10PCS",
        name="Сетка динамика для iPhone 16 Pro/16 Pro Max (10 шт.)",
        normalized_title="Сетка динамика для iPhone 16 Pro 16 Pro Max 10 шт",
        item_type="other",
        parsed_device_brand="apple",
    )
    product = Product(
        name="Магнит MagSafe для Apple iPhone 16 Pro / 16 Pro Max",
        article="076495",
    )

    result = basic_candidate_guardrails(item, product)

    assert result.allowed is False
    assert result.reason == "catalog_family_conflict"


def test_basic_guardrails_reject_test_socket_against_stencil():
    item = CompetitorItem(
        competitor="moba",
        external_id="SCK-QNI-PMI-15",
        name="Колодка теста платы QianLi iSocket для iPhone 15/15 Plus/15 Pro/15 Pro Max",
        normalized_title="Колодка теста платы QianLi iSocket для iPhone 15 15 Plus 15 Pro 15 Pro Max",
        item_type="other",
        parsed_device_brand="apple",
    )
    product = Product(
        name="Трафарет BGA XZZ для Apple iPhone 15 / 15 Plus / 15 Pro / 15 Pro Max",
        article="077309",
    )

    result = basic_candidate_guardrails(item, product)

    assert result.allowed is False
    assert result.reason == "catalog_family_conflict"


def test_basic_guardrails_reject_ic_against_magsafe():
    item = CompetitorItem(
        competitor="moba",
        external_id="IC-PMI-338S01119",
        name="Микросхема PMIC 338S01119 (Управление питанием для iPhone 16/16 Plus/16 Pro/16 Pro Max)",
        normalized_title="Микросхема PMIC 338S01119 для iPhone 16 16 Plus 16 Pro 16 Pro Max",
        item_type="other",
        parsed_device_brand="apple",
    )
    product = Product(
        name="Магнит MagSafe для Apple iPhone 16 / 16 Plus",
        article="076496",
    )

    result = basic_candidate_guardrails(item, product)

    assert result.allowed is False
    assert result.reason == "catalog_family_conflict"


def test_basic_guardrails_reject_protective_glass_against_camera_glass():
    item = CompetitorItem(
        competitor="moba",
        external_id="TP-PRM-REAL-15-PR-5G-B",
        name='Защитное стекло "Премиум" для Realme 15 Pro 5G Черный',
        normalized_title="Защитное стекло Премиум для Realme 15 Pro 5G Черный",
        item_type="other",
        parsed_device_brand="realme",
    )
    product = Product(
        name="Стекло задней камеры для Realme 15 Pro 5G (черный)",
        article="TEST-CAM",
    )

    result = basic_candidate_guardrails(item, product)

    assert result.allowed is False
    assert result.reason == "catalog_family_conflict"


def test_basic_guardrails_allow_samsung_marketing_model_and_service_code():
    item = CompetitorItem(
        competitor="moba",
        external_id="LCD-SSG-A505F-FR-B-OR-S",
        name="Дисплей для Samsung Galaxy A50 (A505F) модуль с рамкой Черный",
        normalized_title="Дисплей Samsung Galaxy A50 A505F модуль с рамкой Черный",
        item_type="display",
        parsed_device_brand="samsung",
    )
    product = Product(
        name="Дисплей для Samsung A505 Galaxy A50 + тачскрин (черный) (в рамке)",
        article="042554",
    )

    result = basic_candidate_guardrails(item, product)

    assert result.allowed is True


def test_basic_guardrails_reject_infinix_flex_model_conflict():
    item = CompetitorItem(
        competitor="liberti",
        external_id="468408",
        name="Шлейф/FLC Infinix Hot 50i (X6531) на системный разъём/микрофон",
        normalized_title="Шлейф Infinix Hot 50i X6531 на системный разъем микрофон",
        item_type="flex",
        parsed_device_brand="infinix",
    )
    product = Product(
        name="Шлейф для Infinix Smart 10 (X6725) с комп. (на кнопку включения и кнопки громкости)",
        article="073198",
    )

    result = basic_candidate_guardrails(item, product)

    assert result.allowed is False
    assert result.reason == "strict_model_conflict"


def test_basic_guardrails_reject_oneplus_nord_ce5_against_oneplus_5():
    item = CompetitorItem(
        competitor="liberti",
        external_id="472505",
        name="LCD дисплей для OnePlus Nord CE5 в сборе с тачскрином (черный)OR 100%",
        normalized_title="LCD дисплей OnePlus Nord CE5 тачскрин черный OR 100%",
        item_type="display",
        category_group="display",
    )
    product = Product(
        name="Дисплей для OnePlus 5 + тачскрин (черный) (OLED)",
        article="039882",
        category="Дисплеи для телефонов",
        subject="дисплей",
    )

    result = basic_candidate_guardrails(item, product)

    assert result.allowed is False
    assert result.reason == "strict_model_conflict"


def test_basic_guardrails_reject_oppo_a18_against_oppo_a78():
    item = CompetitorItem(
        competitor="liberti",
        external_id="460996",
        name="LCD дисплей для Oppo A18/A38 (CPH2591/CPH2579) с тачскрином (черный) 100% OR",
        normalized_title="LCD дисплей Oppo A18 A38 CPH2591 CPH2579 тачскрин черный 100% OR",
        item_type="display",
        category_group="display",
    )
    product = Product(
        name="Дисплей для OPPO A78 4G (CPH2565) + тачскрин (черный) (In-Cell)",
        article="062490",
        category="Дисплеи для телефонов",
        subject="дисплей",
    )

    result = basic_candidate_guardrails(item, product)

    assert result.allowed is False
    assert result.reason == "strict_model_conflict"


def test_basic_guardrails_reject_test_socket_against_camera_gasket():
    item = CompetitorItem(
        competitor="moba",
        external_id="SCK-QNI-PMI-16",
        name="Колодка теста платы QianLi iSocket для iPhone 16/16 Plus/16 Pro/16 Pro Max",
        normalized_title="Колодка теста платы QianLi iSocket для iPhone 16 16 Plus 16 Pro 16 Pro Max",
        item_type="other",
        parsed_device_brand="apple",
    )
    product = Product(
        name="Прокладка передней камеры и датчика сенсора для Apple iPhone 16 / iPhone 16 Pro / iPhone 16 Pro Max",
        article="077166",
    )

    result = basic_candidate_guardrails(item, product)

    assert result.allowed is False
    assert result.reason == "catalog_family_conflict"


def test_basic_guardrails_reject_speaker_mesh_against_camera_gasket():
    item = CompetitorItem(
        competitor="moba",
        external_id="SPK-MSH-PMI-16-PR-10PCS",
        name="Сетка динамика для iPhone 16 Pro/16 Pro Max (10 шт.)",
        normalized_title="Сетка динамика для iPhone 16 Pro 16 Pro Max 10 шт",
        item_type="other",
        parsed_device_brand="apple",
    )
    product = Product(
        name="Прокладка передней камеры и датчика сенсора для Apple iPhone 16 / iPhone 16 Pro / iPhone 16 Pro Max",
        article="077166",
    )

    result = basic_candidate_guardrails(item, product)

    assert result.allowed is False
    assert result.reason == "catalog_family_conflict"


def test_basic_guardrails_reject_phone_charger_against_portable_fan():
    item = CompetitorItem(
        competitor="moba",
        external_id="CHG-REALME-80W-W",
        name="Сетевое зарядное устройство Realme SUPERVOOC 80W Power Adapter White (белое)",
        normalized_title="Сетевое зарядное устройство Realme SUPERVOOC 80W Power Adapter White белое",
        item_type="other",
    )
    product = Product(
        name="Вентилятор портативный Hoco HX63 (белый)",
        article="076348",
        category="Аксессуары",
        subject="вентилятор",
    )

    result = basic_candidate_guardrails(item, product)

    assert result.allowed is False
    assert result.reason == "catalog_family_conflict"


def _write_embeddings(dir_path: Path, prefix: str, matrix: np.ndarray, id_order: list[int]) -> None:
    matrix_file = f"{prefix}_test_{matrix.shape[1]}.npy"
    np.save(dir_path / matrix_file, matrix)
    index = {
        "meta": {
            "model": "test",
            "dim": int(matrix.shape[1]),
            "normalized": True,
            "matrix_file": matrix_file,
        },
        "items": {
            str(item_id): {"row": idx, "text_hash": "x"} for idx, item_id in enumerate(id_order)
        },
    }
    (dir_path / f"{prefix}_index.json").write_text(json.dumps(index), encoding="utf-8")


def test_match_items_suggested(db_session, tmp_path):
    prod1 = Product(name="Дисплей iPhone 12", brand="Apple", category="display", article="A1")
    prod2 = Product(name="Дисплей iPhone 13", brand="Apple", category="display", article="A2")
    db_session.add_all([prod1, prod2])
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="SKU-1",
        name="Дисплей iPhone 12",
        normalized_title="Дисплей iPhone 12",
        item_type="display",
        parsed_device_brand="apple",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0], [0.8, 0.2]], dtype=np.float32)
    product_matrix = product_matrix / np.linalg.norm(product_matrix, axis=1, keepdims=True)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = competitor_matrix / np.linalg.norm(competitor_matrix, axis=1, keepdims=True)

    _write_embeddings(tmp_path, "our_catalog", product_matrix, [prod1.id, prod2.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=2,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )
    assert stats["matched"] == 1

    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.status == CompetitorItemMatchStatus.SUGGESTED
    assert match.product_id == prod1.id


def test_match_items_can_process_specific_competitor_item_ids(db_session, tmp_path):
    prod1 = Product(name="Дисплей iPhone 12", brand="Apple", category="display", article="A1T")
    prod2 = Product(name="Дисплей iPhone 13", brand="Apple", category="display", article="A2T")
    db_session.add_all([prod1, prod2])
    db_session.flush()

    item1 = CompetitorItem(
        competitor="moba",
        external_id="SKU-T1",
        name="Дисплей iPhone 12",
        normalized_title="Дисплей iPhone 12",
        item_type="display",
        parsed_device_brand="apple",
    )
    item2 = CompetitorItem(
        competitor="moba",
        external_id="SKU-T2",
        name="Дисплей iPhone 13",
        normalized_title="Дисплей iPhone 13",
        item_type="display",
        parsed_device_brand="apple",
    )
    db_session.add_all([item1, item2])
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [prod1.id, prod2.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item1.id, item2.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=2,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
        competitor_item_ids=[item2.id],
    )

    assert stats["processed"] == 1
    assert (
        db_session.scalar(
            select(CompetitorItemMatch.id).where(CompetitorItemMatch.competitor_item_id == item1.id)
        )
        is None
    )
    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item2.id)
    ).scalar_one()
    assert match.product_id == prod2.id


def test_match_items_ignores_inactive_product_candidates(db_session, tmp_path):
    inactive_product = Product(
        name="Дисплей iPhone 12",
        brand="Apple",
        category="display",
        article="A0",
        is_active=False,
    )
    active_product = Product(
        name="Дисплей iPhone 12",
        brand="Apple",
        category="display",
        article="A1A",
        is_active=True,
    )
    db_session.add_all([inactive_product, active_product])
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="SKU-1A",
        name="Дисплей iPhone 12",
        normalized_title="Дисплей iPhone 12",
        item_type="display",
        parsed_device_brand="apple",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0], [0.99, 0.01]], dtype=np.float32)
    product_matrix = product_matrix / np.linalg.norm(product_matrix, axis=1, keepdims=True)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(
        tmp_path,
        "our_catalog",
        product_matrix,
        [inactive_product.id, active_product.id],
    )
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.001,
        top_k=2,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["matched"] == 1
    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.product_id == active_product.id


def test_match_items_ambiguous_gap(db_session, tmp_path):
    prod1 = Product(name="Дисплей iPhone 12", brand="Apple", category="display", article="B1")
    prod2 = Product(
        name="Дисплей iPhone 12 Premium",
        brand="Apple",
        category="display",
        article="B2",
    )
    db_session.add_all([prod1, prod2])
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="SKU-2",
        name="Дисплей iPhone 12",
        normalized_title="Дисплей iPhone 12",
        item_type="display",
        parsed_device_brand="apple",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0], [1.0, 0.0]], dtype=np.float32)
    product_matrix = product_matrix / np.linalg.norm(product_matrix, axis=1, keepdims=True)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = competitor_matrix / np.linalg.norm(competitor_matrix, axis=1, keepdims=True)

    _write_embeddings(tmp_path, "our_catalog", product_matrix, [prod1.id, prod2.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.5,
        top_k=2,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )
    assert stats["ambiguous"] == 1

    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.status == CompetitorItemMatchStatus.AMBIGUOUS


def test_match_items_rejects_display_frame_conflict(db_session, tmp_path):
    product = Product(
        name="Дисплей iPhone 12 в рамке",
        brand="Apple",
        category="display",
        article="C1",
        display_has_frame=True,
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="SKU-3",
        name="Дисплей iPhone 12 без рамки",
        normalized_title="Дисплей iPhone 12 без рамки",
        item_type="display",
        parsed_device_brand="apple",
        has_frame=False,
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["skipped_no_candidates"] == 1
    assert db_session.query(CompetitorItemMatch).count() == 0


def test_match_items_rejects_display_frame_conflict_from_competitor_name(db_session, tmp_path):
    product = Product(
        name="Дисплей iPhone 12 в рамке",
        brand="Apple",
        category="display",
        article="C2",
        display_has_frame=True,
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="SKU-3B",
        name="Дисплей iPhone 12 без рамки",
        normalized_title="Дисплей iPhone 12 без рамки",
        item_type="display",
        parsed_device_brand="apple",
        has_frame=None,
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["skipped_no_candidates"] == 1
    assert db_session.query(CompetitorItemMatch).count() == 0


def test_match_items_competitor_frame_text_overrides_stale_column(db_session, tmp_path):
    product = Product(
        name="Дисплей iPhone 12 в рамке",
        brand="Apple",
        category="display",
        article="C3",
        display_has_frame=True,
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="SKU-3C",
        name="Дисплей iPhone 12 без рамки",
        normalized_title="Дисплей iPhone 12",
        item_type="display",
        parsed_device_brand="apple",
        has_frame=True,
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["skipped_no_candidates"] == 1
    assert db_session.query(CompetitorItemMatch).count() == 0


def test_match_items_rejects_moba_cp_sku_against_frame_product(db_session, tmp_path):
    product = Product(
        name="Дисплей Samsung Galaxy Note 20 Ultra в рамке черный",
        brand="Samsung",
        category="display",
        article="C3B",
        display_has_frame=True,
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="LCD-SSG-N985F-CP-B-OR-SP",
        name="Дисплей для Samsung Galaxy Note 20 Ultra в сборе с тачскрином Черный - OR (SP)",
        normalized_title="Дисплей Samsung Galaxy Note 20 Ultra в сборе с тачскрином Черный OR SP",
        item_type="display",
        parsed_device_brand="samsung",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["skipped_no_candidates"] == 1
    assert db_session.query(CompetitorItemMatch).count() == 0


def test_match_items_rejects_display_color_conflict(db_session, tmp_path):
    product = Product(
        name="Дисплей iPhone 12 в рамке черный",
        brand="Apple",
        category="display",
        article="C4",
        display_has_frame=True,
        color="черный",
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="SKU-3D",
        name="Дисплей iPhone 12 модуль с рамкой Белый",
        normalized_title="Дисплей iPhone 12 модуль с рамкой Белый",
        item_type="display",
        parsed_device_brand="apple",
        has_frame=True,
        color="Белый",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["skipped_no_candidates"] == 1
    assert db_session.query(CompetitorItemMatch).count() == 0


def test_match_items_display_color_text_overrides_stale_columns(db_session, tmp_path):
    product = Product(
        name="Дисплей iPhone 12 в рамке черный",
        brand="Apple",
        category="display",
        article="C4B",
        display_has_frame=True,
        color="серый",
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="SKU-3D2",
        name="Дисплей iPhone 12 модуль с рамкой Белый",
        normalized_title="Дисплей iPhone 12 модуль с рамкой",
        item_type="display",
        parsed_device_brand="apple",
        has_frame=True,
        color="Черный",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["skipped_no_candidates"] == 1
    assert db_session.query(CompetitorItemMatch).count() == 0


def test_match_items_rejects_housing_color_conflict(db_session, tmp_path):
    product = Product(
        name="Задняя крышка для Apple iPhone 17 (черный) (Premium)",
        brand="Apple",
        category="Корпуса и крышки",
        subject="крышка",
        article="HOU-B",
        color="черный",
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="BTC-PMI-17-WH-OR",
        name="Задняя крышка для iPhone 17 Белый - Премиум",
        normalized_title="Задняя крышка iPhone 17 Белый Премиум",
        item_type="housing",
        parsed_device_brand="apple",
        color="черный",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["skipped_no_candidates"] == 1
    assert db_session.query(CompetitorItemMatch).count() == 0


def test_match_items_rejects_premium_housing_against_orig_product(db_session, tmp_path):
    product = Product(
        name="Задняя крышка для Apple iPhone 17 (черный) (ORIG100) (Снятый)",
        brand="Apple",
        category="Корпуса и крышки",
        subject="крышка",
        article="HOU-ORIG",
        color="черный",
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="BTC-PMI-17-B-OR",
        name="Задняя крышка для iPhone 17 Черный - Премиум",
        normalized_title="Задняя крышка iPhone 17 Черный Премиум",
        item_type="housing",
        parsed_device_brand="apple",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["skipped_no_candidates"] == 1
    assert db_session.query(CompetitorItemMatch).count() == 0


def test_match_items_rejects_housing_camera_glass_assembly_conflict(db_session, tmp_path):
    product = Product(
        name="Задняя крышка для Xiaomi Poco X7 (24095PCADG) (черный)",
        brand="Xiaomi",
        category="Корпуса и крышки",
        subject="крышка",
        article="HOU-X7",
        color="черный",
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="liberti",
        external_id="471581",
        name="Задняя крышка для Xiaomi Poco X7 (24095PCADG) со стеклом камеры (черный)",
        normalized_title="Задняя крышка Xiaomi Poco X7 24095PCADG со стеклом камеры черный",
        item_type="housing",
        parsed_device_brand="xiaomi",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["skipped_no_candidates"] == 1
    assert db_session.query(CompetitorItemMatch).count() == 0


def test_match_items_rejects_extra_product_camera_glass_assembly(db_session, tmp_path):
    wrong = Product(
        name="Задняя крышка для Samsung S911 Galaxy S23 (зеленый) (в сборе со стеклом камеры)",
        brand="Samsung",
        category="Корпуса и крышки",
        subject="крышка",
        article="S23-CAM-GLASS",
        color="зеленый",
    )
    right = Product(
        name="Задняя крышка для Samsung S911 Galaxy S23 (зеленый)",
        brand="Samsung",
        category="Корпуса и крышки",
        subject="крышка",
        article="S23-PLAIN",
        color="зеленый",
    )
    db_session.add_all([wrong, right])
    db_session.flush()

    item = CompetitorItem(
        competitor="liberti",
        external_id="473185",
        name="Задняя крышка для Samsung Galaxy S23 SM-S911 (зеленый), премиум",
        normalized_title="Задняя крышка Samsung Galaxy S23 SM-S911 зеленый премиум",
        item_type="housing",
        parsed_device_brand="samsung",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0], [0.99, 0.01]], dtype=np.float32)
    product_matrix = product_matrix / np.linalg.norm(product_matrix, axis=1, keepdims=True)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [wrong.id, right.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=2,
        top_k_llm=2,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["matched"] == 1
    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.product_id == right.id


def test_match_items_rejects_extra_product_flex_assembly(db_session, tmp_path):
    wrong = Product(
        name=(
            "Задняя крышка для Apple iPhone 17 Pro Max (SIM + eSIM) / "
            "iPhone 17 Pro Max (eSIM) (оранжевый) (в сборе со шлейфом) (Premium)"
        ),
        brand="Apple",
        category="Корпуса и крышки",
        subject="крышка",
        article="17PM-FLEX",
        color="оранжевый",
    )
    right = Product(
        name=(
            "Задняя крышка для Apple iPhone 17 Pro Max (SIM + eSIM) / "
            "iPhone 17 Pro Max (eSIM) (оранжевый) (Premium)"
        ),
        brand="Apple",
        category="Корпуса и крышки",
        subject="крышка",
        article="17PM-PLAIN",
        color="оранжевый",
    )
    db_session.add_all([wrong, right])
    db_session.flush()

    item = CompetitorItem(
        competitor="liberti",
        external_id="474239",
        name="Задняя крышка для iPhone 17 Pro Max (оранжевый) MagSafe",
        normalized_title="Задняя крышка iPhone 17 Pro Max оранжевый MagSafe",
        item_type="housing",
        parsed_device_brand="apple",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0], [0.99, 0.01]], dtype=np.float32)
    product_matrix = product_matrix / np.linalg.norm(product_matrix, axis=1, keepdims=True)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [wrong.id, right.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=2,
        top_k_llm=2,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["matched"] == 1
    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.product_id == right.id


def test_match_items_rejects_missing_product_flex_assembly_with_magsafe_wording(
    db_session,
    tmp_path,
):
    product = Product(
        name=(
            "Задняя крышка для Apple iPhone 17 (SIM + eSIM) / iPhone 17 (eSIM) "
            "(синий) (в сборе со стеклом камеры) (Premium)"
        ),
        brand="Apple",
        category="Корпуса и крышки",
        subject="крышка",
        article="17-CAM-ONLY",
        color="синий",
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="liberti",
        external_id="474255",
        name="Задняя крышка для iPhone 17 (синий) в сборе со стеклом камеры и шлейфом MagSafe",
        normalized_title="Задняя крышка iPhone 17 синий в сборе со стеклом камеры и шлейфом MagSafe",
        item_type="housing",
        parsed_device_brand="apple",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["skipped_no_candidates"] == 1
    assert db_session.query(CompetitorItemMatch).count() == 0


def test_match_items_rejects_housing_device_code_conflict(db_session, tmp_path):
    wrong = Product(
        name="Задняя крышка для Huawei Honor 400 Pro China (DNP-AN00) (черный)",
        brand="Huawei",
        category="Корпуса и крышки",
        subject="крышка",
        article="HONOR-400-CHINA",
        color="черный",
    )
    right = Product(
        name="Задняя крышка для Huawei Honor 400 Pro (DNP-NX9) (черный)",
        brand="Huawei",
        category="Корпуса и крышки",
        subject="крышка",
        article="HONOR-400-GLOBAL",
        color="черный",
    )
    db_session.add_all([wrong, right])
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="BTC-HUW-HNR-400-PR-B-OR",
        name="Задняя крышка для Huawei Honor 400 Pro (DNP-NX9) Черный - Премиум",
        normalized_title="Задняя крышка Huawei Honor 400 Pro DNP-NX9 Черный Премиум",
        item_type="housing",
        parsed_device_brand="huawei",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0], [0.99, 0.01]], dtype=np.float32)
    product_matrix = product_matrix / np.linalg.norm(product_matrix, axis=1, keepdims=True)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [wrong.id, right.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=2,
        top_k_llm=2,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["matched"] == 1
    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.product_id == right.id


def test_match_items_rejects_housing_part_kind_conflict(db_session, tmp_path):
    product = Product(
        name="Держатель сим-карты для Apple iPhone 16 / iPhone 16 Plus (черный)",
        brand="Apple",
        category="Держатели сим-карт",
        subject="держатель сим-карты",
        article="SIM-16",
        color="черный",
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="HOU-PMI-16-PLS-B-OR",
        name="Корпус для iPhone 16 Plus (A3290) (1 Sim) Черный - Премиум",
        normalized_title="Корпус iPhone 16 Plus A3290 1 Sim Черный Премиум",
        item_type="housing",
        parsed_device_brand="apple",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["skipped_no_candidates"] == 1
    assert db_session.query(CompetitorItemMatch).count() == 0


def test_match_items_rejects_camera_position_conflict(db_session, tmp_path):
    product = Product(
        name="Камера передняя для Apple iPhone 16E, ориг",
        brand="Apple",
        category="Камеры",
        subject="камера",
        article="CAM-FRONT",
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="liberti",
        external_id="467000",
        name="Камера основная Apple iPhone 16E, ориг",
        normalized_title="Камера основная Apple iPhone 16E ориг",
        item_type="camera",
        parsed_device_brand="apple",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["skipped_no_candidates"] == 1
    assert db_session.query(CompetitorItemMatch).count() == 0


def test_match_items_reclassifies_battery_adhesive_as_other(db_session, tmp_path):
    product = Product(
        name="Аккумулятор для Apple iPhone 16 Pro (GENUINE)",
        brand="Apple",
        category="Аккумуляторы",
        subject="аккумулятор",
        article="BTT-16P",
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="ADT-BTT-PMI-16-PR",
        name="Скотч Аккумулятора для iPhone 16 Pro (A3293)",
        normalized_title="Скотч Аккумулятора iPhone 16 Pro A3293",
        item_type="battery",
        parsed_device_brand="apple",
    )
    db_session.add(item)
    db_session.flush()

    assert _effective_item_type(item) == "other"

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["skipped_no_candidates"] == 1
    assert db_session.query(CompetitorItemMatch).count() == 0


def test_match_items_reclassifies_power_tool_battery_as_other(db_session, tmp_path):
    product = Product(
        name="Аккумулятор для Nokia 1202 / 1203 / 1661 и др. (BL-4C)",
        brand="Nokia",
        category="Аккумуляторы",
        subject="аккумулятор",
        article="BTT-NOKIA",
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="BTT-PSR-1200-12V-2K-HT",
        name="Аккумулятор для электроинструмента PSR 1200 12V 2000 mAh Ni-Cd (Hitachi тип)",
        normalized_title="Аккумулятор для электроинструмента PSR 1200 12V 2000 mAh Ni-Cd Hitachi тип",
        item_type="battery",
    )
    db_session.add(item)
    db_session.flush()

    assert _effective_item_type(item) == "other"

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["skipped_no_candidates"] == 1
    assert db_session.query(CompetitorItemMatch).count() == 0


def test_match_items_rejects_battery_part_code_conflict(db_session, tmp_path):
    wrong = Product(
        name="Аккумулятор для ZTE Nubia Red Magic 11 Pro (Li3874T90Ph596788) (Premium)",
        brand="ZTE",
        category="battery",
        subject="аккумулятор",
        article="077720",
    )
    right = Product(
        name="Аккумулятор для ZTE Nubia Red Magic 10 Pro (NX789J) (Premium)",
        brand="ZTE",
        category="battery",
        subject="аккумулятор",
        article="075190",
    )
    db_session.add_all([wrong, right])
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="BTT-ZT-LI3934T90P8H623486",
        name="Аккумулятор для ZTE Nubia Red Magic 10 Pro (Li3934T90P8h623486)",
        normalized_title="Аккумулятор ZTE Nubia Red Magic 10 Pro Li3934T90P8h623486",
        item_type="battery",
        parsed_device_brand="zte",
        first_seen_at=date(2026, 5, 2),
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0], [0.97, 0.03]], dtype=np.float32)
    product_matrix = product_matrix / np.linalg.norm(product_matrix, axis=1, keepdims=True)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [wrong.id, right.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=2,
        top_k_llm=2,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["matched"] == 1
    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.product_id == right.id


def test_match_items_suggests_battery_verification_system_diagnosable(
    db_session,
    tmp_path,
):
    best = Product(
        name=(
            "Аккумулятор для Apple iPhone 14 Pro Max (F5ENERGY) (усиленный) "
            "(4770 мАч) (SPECIAL EDITION) (SYSTEM DIAGNOSABLE) + двухсторонний скотч"
        ),
        brand="Apple",
        category="battery",
        subject="аккумулятор",
        article="070903",
    )
    alternative = Product(
        name=(
            "Аккумулятор для Apple iPhone 14 Pro Max (F5ENERGY) (усиленный) "
            "(4770 мАч) (SPECIAL EDITION) + двухсторонний скотч"
        ),
        brand="Apple",
        category="battery",
        subject="аккумулятор",
        article="063976",
    )
    db_session.add_all([best, alternative])
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="BTT-PMIPRM140-VRF-HC-NEW",
        name=(
            "Аккумулятор для iPhone 14 Pro Max - Battery Collection с верификацией "
            '"Новая запчасть" - усиленная 4750 mAh'
        ),
        normalized_title="Аккумулятор iPhone 14 Pro Max верификация Новая запчасть усиленная 4750 mAh",
        item_type="battery",
        parsed_device_brand="apple",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0], [0.995, 0.005]], dtype=np.float32)
    product_matrix = product_matrix / np.linalg.norm(product_matrix, axis=1, keepdims=True)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [best.id, alternative.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.02,
        top_k=2,
        top_k_llm=2,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["matched"] == 1
    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.status == CompetitorItemMatchStatus.SUGGESTED
    assert match.product_id == best.id
    assert match.rationale_json["battery_verification_suggest"]["reason"] == (
        "battery_verification_signal_with_system_diagnosable_model_overlap"
    )


def test_match_items_rejects_flex_role_conflict(db_session, tmp_path):
    product = Product(
        name="Шлейф Xiaomi Mi 5 на разъем зарядки и микрофон (Черный)",
        brand="Xiaomi",
        category="Шлейфы",
        subject="шлейф",
        article="FPC-CHARGE",
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="liberti",
        external_id="247843",
        name="Шлейф/FLC Xiaomi Mi 5 на кнопки громкости/включения",
        normalized_title="Шлейф FLC Xiaomi Mi 5 на кнопки громкости включения",
        item_type="flex",
        parsed_device_brand="xiaomi",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["skipped_no_candidates"] == 1
    assert db_session.query(CompetitorItemMatch).count() == 0


def test_match_items_rejects_flex_sensor_role_conflict(db_session, tmp_path):
    product = Product(
        name="Шлейф для Xiaomi 14 Ultra (24030PN60G) с комп. + сенсор",
        brand="Xiaomi",
        category="Шлейфы",
        subject="шлейф",
        article="FPC-SENSOR",
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="liberti",
        external_id="458849",
        name="Шлейф/FLC Xiaomi 14 Ultra на системный разъём/микрофон",
        normalized_title="Шлейф FLC Xiaomi 14 Ultra на системный разъем микрофон",
        item_type="flex",
        parsed_device_brand="xiaomi",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["skipped_no_candidates"] == 1
    assert db_session.query(CompetitorItemMatch).count() == 0


def test_match_items_updates_product_relationship_before_color_sweeper(db_session, tmp_path):
    wrong_product = Product(
        name="Задняя крышка для Apple iPhone 17 (белый) (Premium)",
        brand="Apple",
        category="Корпуса и крышки",
        subject="крышка",
        article="HOU-WRONG",
        color="белый",
    )
    correct_product = Product(
        name="Задняя крышка для Apple iPhone 17 (синий) (Premium)",
        brand="Apple",
        category="Корпуса и крышки",
        subject="крышка",
        article="HOU-BLUE",
        color="синий",
    )
    db_session.add_all([wrong_product, correct_product])
    db_session.flush()

    item = CompetitorItem(
        competitor="liberti",
        external_id="474247",
        name="Задняя крышка для iPhone 17 (синий) MagSafe",
        normalized_title="Задняя крышка iPhone 17 синий MagSafe",
        item_type="housing",
        parsed_device_brand="apple",
    )
    db_session.add(item)
    db_session.flush()
    db_session.add(
        CompetitorItemMatch(
            competitor_item_id=item.id,
            product_id=wrong_product.id,
            status=CompetitorItemMatchStatus.AMBIGUOUS,
            method=CompetitorItemMatchMethod.EMBEDDING_AUTO,
            final_score=0.74,
        )
    )
    db_session.flush()

    product_matrix = np.array([[0.9, 0.1], [1.0, 0.0]], dtype=np.float32)
    product_matrix = product_matrix / np.linalg.norm(product_matrix, axis=1, keepdims=True)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(
        tmp_path,
        "our_catalog",
        product_matrix,
        [wrong_product.id, correct_product.id],
    )
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=2,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=False,
        include_status=[CompetitorItemMatchStatus.AMBIGUOUS.value],
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
        competitor_item_ids=[item.id],
    )

    assert stats["matched"] == 1
    assert stats["auto_rejected_part_color_conflict"] == 0
    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.status == CompetitorItemMatchStatus.SUGGESTED
    assert match.product_id == correct_product.id


def test_match_items_can_skip_missing_competitor_embedding_without_live_api(db_session, tmp_path):
    product = Product(
        name="Задняя крышка для Apple iPhone 17 (черный) (Premium)",
        brand="Apple",
        category="Корпуса и крышки",
        subject="крышка",
        article="HOU-B2",
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="BTC-PMI-17-B-OR",
        name="Задняя крышка для iPhone 17 Черный - Премиум",
        normalized_title="Задняя крышка iPhone 17 Черный Премиум",
        item_type="housing",
        parsed_device_brand="apple",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
        live_embed_missing=False,
    )

    assert stats["skipped_no_embedding"] == 1
    assert db_session.query(CompetitorItemMatch).count() == 0


def test_match_items_rejects_explicit_display_touch_conflict(db_session, tmp_path):
    product = Product(
        name="Дисплей iPhone 12 без тачскрина черный",
        brand="Apple",
        category="display",
        article="C4C0",
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="SKU-3D0",
        name="Дисплей iPhone 12 с тачскрином черный",
        normalized_title="Дисплей iPhone 12 с тачскрином черный",
        item_type="display",
        parsed_device_brand="apple",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["skipped_no_candidates"] == 1
    assert db_session.query(CompetitorItemMatch).count() == 0


def test_match_items_does_not_reject_implicit_missing_touch(db_session, tmp_path):
    product = Product(
        name="Дисплей iPhone 12 черный",
        brand="Apple",
        category="display",
        article="C4C1",
        display_has_frame=False,
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="SKU-3D1",
        name="Дисплей iPhone 12 с тачскрином черный",
        normalized_title="Дисплей iPhone 12 с тачскрином черный",
        item_type="display",
        parsed_device_brand="apple",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["matched"] == 1


def test_match_items_rejects_display_backlight_conflict(db_session, tmp_path):
    product = Product(
        name="Дисплей iPhone 12 без подсветки черный",
        brand="Apple",
        category="display",
        article="C4C2",
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="SKU-3D2A",
        name="Дисплей iPhone 12 с подсветкой черный",
        normalized_title="Дисплей iPhone 12 с подсветкой черный",
        item_type="display",
        parsed_device_brand="apple",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["skipped_no_candidates"] == 1
    assert db_session.query(CompetitorItemMatch).count() == 0


def test_match_items_rejects_display_matrix_tags_conflict(db_session, tmp_path):
    product = Product(
        name="Дисплей iPhone 12 JCID черный",
        brand="Apple",
        category="display",
        article="C4C3",
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="SKU-3D2B",
        name="Дисплей iPhone 12 ZY черный",
        normalized_title="Дисплей iPhone 12 ZY черный",
        item_type="display",
        parsed_device_brand="apple",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["skipped_no_candidates"] == 1
    assert db_session.query(CompetitorItemMatch).count() == 0


def test_match_items_rejects_display_quality_conflict(db_session, tmp_path):
    product = Product(
        name="Дисплей iPhone 12 черный",
        brand="Apple",
        category="display",
        article="C4C4",
        display_quality="Original",
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="SKU-3D2C",
        name="Дисплей iPhone 12 Copy High черный",
        normalized_title="Дисплей iPhone 12 Copy High черный",
        item_type="display",
        parsed_device_brand="apple",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["skipped_no_candidates"] == 1
    assert db_session.query(CompetitorItemMatch).count() == 0


def test_match_items_reviews_display_quality_unknown_on_one_side(db_session, tmp_path):
    product = Product(
        name="Дисплей iPhone 12 биток черный",
        brand="Apple",
        category="display",
        article="C4C4Q",
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="SKU-3D2Q",
        name="Дисплей iPhone 12 черный",
        normalized_title="Дисплей iPhone 12 черный",
        item_type="display",
        parsed_device_brand="apple",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["needs_review"] == 1
    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.status == CompetitorItemMatchStatus.NEEDS_REVIEW
    assert match.rationale_json["display_quality_review"]["reason"] == (
        "display_quality_unknown_on_one_side"
    )


def test_match_items_filters_original_refurb_against_copy_display_construction(
    db_session, tmp_path
):
    product = Product(
        name="Дисплей для Apple iPhone 15 Pro Max (черный) (биток) (ORIG)",
        brand="Apple",
        category="display",
        article="C4C4R",
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="LCD-PMI-15-PR-MAX-CP-B-INCL-HD-PLS",
        name=(
            "Дисплей для iPhone 15 Pro Max (A3106) в сборе с тачскрином " "Черный - (In-Cell, HD+)"
        ),
        normalized_title="Дисплей для iPhone 15 Pro Max A3106 Черный In-Cell HD+",
        item_type="display",
        parsed_device_brand="apple",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["skipped_no_candidates"] == 1
    assert db_session.query(CompetitorItemMatch).count() == 0


def test_match_items_display_quality_text_overrides_stale_columns(db_session, tmp_path):
    product = Product(
        name="Дисплей iPhone 8 Plus Medium белый",
        brand="Apple",
        category="display",
        article="C4C4B",
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="SKU-3D2C2",
        name="Дисплей iPhone 8 Plus Оптима белый",
        normalized_title="Дисплей iPhone 8 Plus Оптима белый",
        item_type="display",
        parsed_device_brand="apple",
        attrs_quality="Оригинал",
        screen_quality_grade="ORIGINAL",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["matched"] == 1


def test_competitor_display_quality_maps_moba_or_and_optima():
    or_item = CompetitorItem(
        competitor="moba",
        external_id="SKU-QUALITY-OR",
        name="Дисплей Xiaomi Redmi A1 черный - OR",
        normalized_title="Дисплей Xiaomi Redmi A1 черный OR",
        item_type="display",
    )
    optima_item = CompetitorItem(
        competitor="moba",
        external_id="SKU-QUALITY-OPTIMA",
        name="Дисплей Xiaomi Redmi A1 черный - Оптима",
        normalized_title="Дисплей Xiaomi Redmi A1 черный Оптима",
        item_type="display",
    )
    standard_item = CompetitorItem(
        competitor="moba",
        external_id="SKU-QUALITY-STANDARD",
        name="Дисплей Xiaomi Redmi A1 черный - Стандарт (COG)",
        normalized_title="Дисплей Xiaomi Redmi A1 черный Стандарт COG",
        item_type="display",
    )

    assert _competitor_display_quality_raw(or_item) == "OR"
    assert _competitor_display_mapped_1c_quality_raw(or_item) == "(ORIG)"
    assert _competitor_display_quality(or_item) == "Original"
    assert _competitor_display_quality_raw(optima_item) == "Оптима"
    assert _competitor_display_mapped_1c_quality_raw(optima_item) == "(Medium)"
    assert _competitor_display_quality(optima_item) == "Copy Medium"
    assert _competitor_display_quality_raw(standard_item) == "Стандарт"
    assert _competitor_display_mapped_1c_quality_raw(standard_item) == "(Medium)"
    assert _competitor_display_quality(standard_item) == "Copy Medium"


def test_display_quality_treats_bitok_as_original_refurbished():
    product = Product(
        name="Дисплей для Apple iPhone 13 (в сборе с тачскрином) (черный) (биток) (ORIG)",
        quality_raw="Биток",
    )
    item = CompetitorItem(
        competitor="moba",
        external_id="SKU-QUALITY-OR-CHANGE-GLASS",
        name=(
            "Дисплей для iPhone 13 (A2635) в сборе с тачскрином Черный - "
            "OR (Снятый, заменено ТОЛЬКО стекло)"
        ),
        normalized_title="Дисплей для iPhone 13 в сборе с тачскрином Черный OR заменено стекло",
        item_type="display",
    )

    assert _product_display_quality(product) == "Original Refurbished"
    assert _competitor_display_quality(item) == "Original Refurbished"


def test_match_items_rejects_moba_optima_against_original_display(db_session, tmp_path):
    product = Product(
        name="Дисплей Xiaomi Redmi A1 черный (ORIG)",
        brand="Xiaomi",
        category="display",
        article="C4C4C",
        display_quality="Original",
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="SKU-3D2C3",
        name="Дисплей Xiaomi Redmi A1 черный - Оптима",
        normalized_title="Дисплей Xiaomi Redmi A1 черный Оптима",
        item_type="display",
        parsed_device_brand="xiaomi",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["skipped_no_candidates"] == 1
    assert db_session.query(CompetitorItemMatch).count() == 0


def test_match_items_rejects_display_construction_conflict(db_session, tmp_path):
    product = Product(
        name="Дисплей iPhone 12 черный",
        brand="Apple",
        category="display",
        article="C4C5",
        display_construction="In-Cell",
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="SKU-3D2D",
        name="Дисплей iPhone 12 On-Cell черный",
        normalized_title="Дисплей iPhone 12 On-Cell черный",
        item_type="display",
        parsed_device_brand="apple",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["skipped_no_candidates"] == 1
    assert db_session.query(CompetitorItemMatch).count() == 0


def test_match_items_rejects_incell_against_oled_display(db_session, tmp_path):
    product = Product(
        name="Дисплей Samsung J400 Galaxy J4 (2018) + тачскрин (черный) (OLED)",
        brand="Samsung",
        category="display",
        article="C4C5A",
        display_has_frame=False,
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="LCD-SSG-J400F-CP-B-TF",
        name="Дисплей для Samsung Galaxy J4 2018 (J400F) в сборе с тачскрином Черный - (In-Cell)",
        normalized_title="Дисплей Samsung Galaxy J4 2018 J400F в сборе с тачскрином Черный In-Cell",
        item_type="display",
        parsed_device_brand="samsung",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["skipped_no_candidates"] == 1
    assert db_session.query(CompetitorItemMatch).count() == 0


def test_match_items_rejects_oled_against_incell_display(db_session, tmp_path):
    product = Product(
        name="Дисплей Samsung A705 Galaxy A70 + тачскрин (черный) (In-Cell)",
        brand="Samsung",
        category="display",
        article="C4C5C",
        display_has_frame=False,
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="LCD-SSG-A705F-CP-B-LED",
        name="Дисплей для Samsung Galaxy A70 (A705F) в сборе с тачскрином Черный - (OLED)",
        normalized_title="Дисплей Samsung Galaxy A70 A705F в сборе с тачскрином Черный OLED",
        item_type="display",
        parsed_device_brand="samsung",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["skipped_no_candidates"] == 1
    assert db_session.query(CompetitorItemMatch).count() == 0


def test_match_items_display_construction_ignores_stale_columns(db_session, tmp_path):
    product = Product(
        name="Дисплей iPhone 11 Pro Max GX ORIG Hard OLED черный",
        brand="Apple",
        category="display",
        article="C4C5B",
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="SKU-3D2D2",
        name="Дисплей iPhone 11 Pro Max GX ORIG черный",
        normalized_title="Дисплей iPhone 11 Pro Max GX ORIG черный",
        item_type="display",
        parsed_device_brand="apple",
        attrs_construction="In-Cell",
        screen_construction="INCELL",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["matched"] == 1


def test_match_items_rejects_display_refresh_rate_conflict(db_session, tmp_path):
    product = Product(
        name="Дисплей iPhone 12 120Hz черный",
        brand="Apple",
        category="display",
        article="C4C6",
        display_refresh_rate_hz=120,
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="SKU-3D2E",
        name="Дисплей iPhone 12 60Hz черный",
        normalized_title="Дисплей iPhone 12 60Hz черный",
        item_type="display",
        parsed_device_brand="apple",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["skipped_no_candidates"] == 1
    assert db_session.query(CompetitorItemMatch).count() == 0


def test_match_items_rejects_display_model_code_conflict(db_session, tmp_path):
    product = Product(
        name="Дисплей Samsung Galaxy A23 SM-A235 черный",
        brand="Samsung",
        category="display",
        article="C4C",
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="SKU-3D3",
        name="Дисплей Samsung Galaxy A14 SM-A145 черный",
        normalized_title="Дисплей Samsung Galaxy A14 SM-A145 черный",
        item_type="display",
        parsed_device_brand="samsung",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["skipped_no_candidates"] == 1
    assert db_session.query(CompetitorItemMatch).count() == 0


def test_match_items_rejects_redmi_note_text_model_conflict(db_session, tmp_path):
    product = Product(
        name="Дисплей для Xiaomi Redmi Note 14 4G (24117RN76O) черный",
        brand="Xiaomi",
        category="display",
        article="C4C7",
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="SKU-3D7",
        name="Дисплей для Xiaomi Redmi Note 4X/4 Global Version черный",
        normalized_title="Дисплей для Xiaomi Redmi Note 4X/4 Global Version черный",
        item_type="display",
        parsed_device_brand="xiaomi",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["skipped_no_candidates"] == 1
    assert db_session.query(CompetitorItemMatch).count() == 0


def test_match_items_rejects_legacy_sony_ericsson_model_conflict(db_session, tmp_path):
    product = Product(
        name="Дисплей Sony-Ericsson LT30i Xperia T в сборе с тачскрином Черный",
        brand="Sony-Ericsson",
        category="display",
        article="C4C7SE",
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="liberti",
        external_id="SKU-3D7SE",
        name="LCD дисплей для Sony-Ericsson J210i/J200i",
        normalized_title="LCD дисплей для Sony-Ericsson J210i J200i",
        item_type="display",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["skipped_no_candidates"] == 1
    assert db_session.query(CompetitorItemMatch).count() == 0


def test_match_items_rejects_sony_xperia_letter_model_conflict(db_session, tmp_path):
    product = Product(
        name="Дисплей Sony-Ericsson LT30i Xperia T в сборе с тачскрином Черный",
        brand="Sony-Ericsson",
        category="display",
        article="C4C7SEL",
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="liberti",
        external_id="SKU-3D7SEL",
        name="LCD дисплей для Sony Xperia L C2105/C2104/S36h в сборе с тачскрином",
        normalized_title="LCD дисплей для Sony Xperia L C2105 C2104 S36h в сборе с тачскрином",
        item_type="display",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["skipped_no_candidates"] == 1
    assert db_session.query(CompetitorItemMatch).count() == 0


def test_match_items_rejects_tcl_against_sony_display(db_session, tmp_path):
    product = Product(
        name="Дисплей Sony-Ericsson LT30i Xperia T в сборе с тачскрином Черный",
        brand="Sony-Ericsson",
        category="display",
        article="C4C7TCL",
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="LCD-TCL-505-CP-B-OR",
        name="Дисплей для TCL 505 в сборе с тачскрином Черный - OR",
        normalized_title="Дисплей для TCL 505 в сборе с тачскрином Черный OR",
        item_type="display",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["skipped_no_candidates"] == 1
    assert db_session.query(CompetitorItemMatch).count() == 0


def test_match_items_rejects_legacy_nokia_model_conflict(db_session, tmp_path):
    product = Product(
        name="Дисплей для Nokia C30 (TA-1359) + тачскрин черный",
        brand="Nokia",
        category="display",
        article="C4C7NK",
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="liberti",
        external_id="SKU-3D7NK",
        name="LCD дисплей для Nokia 6030/2626/2600/2610/2650 1-я категория",
        normalized_title="LCD дисплей для Nokia 6030 2626 2600 2610 2650 1-я категория",
        item_type="display",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["skipped_no_candidates"] == 1
    assert db_session.query(CompetitorItemMatch).count() == 0


def test_match_items_rejects_redmi_note_vs_plain_redmi_conflict(db_session, tmp_path):
    product = Product(
        name="Дисплей для Xiaomi Redmi 13 4G (24040RN64Y) черный",
        brand="Xiaomi",
        category="display",
        article="C4C7B",
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="SKU-3D7B",
        name="Дисплей для Xiaomi Redmi Note 4X/4 Global Version черный",
        normalized_title="Дисплей для Xiaomi Redmi Note 4X/4 Global Version черный",
        item_type="display",
        parsed_device_brand="xiaomi",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["skipped_no_candidates"] == 1
    assert db_session.query(CompetitorItemMatch).count() == 0


def test_extract_device_model_keys_does_not_cross_brand_short_codes():
    assert "samsung_a830" not in _extract_device_model_keys("Тачскрин для Lenovo A830")
    assert "huawei_p70" not in _extract_device_model_keys("Тачскрин для Lenovo P70")
    assert "samsung_a17" in _extract_device_model_keys("Дисплей для Samsung Galaxy A17")
    assert "huawei_p40_lite" in _extract_device_model_keys("Дисплей для Huawei P40 Lite")


def test_extract_device_model_keys_common_display_families():
    keys = _extract_device_model_keys("Xiaomi Mi 9T / Mi 9T Pro / Redmi K20 Pro")
    assert {"xiaomi_mi_9t", "xiaomi_mi_9t_pro", "redmi_k20_pro"}.issubset(keys)
    assert "tecno_spark_10_pro" in _extract_device_model_keys("Tecno Spark 10 Pro")
    assert "zte_nubia_v70_max" in _extract_device_model_keys("ZTE Nubia V70 Max")
    assert "zte_nubia_red_magic_10_pro" in _extract_device_model_keys("ZTE Nubia Red Magic 10 Pro")
    assert "zte_nubia_red_magic_10s_pro" in _extract_device_model_keys("Nubia Red Magic 10S Pro")
    assert "oppo_a5i_pro_4g" in _extract_device_model_keys("OPPO A5i Pro 4G")
    assert "nova_11i" in _extract_device_model_keys("Huawei Nova 11i")
    assert "nova_base" not in _extract_device_model_keys("Huawei Nova 11i")
    assert "nova_y61" in _extract_device_model_keys("Huawei Nova Y61")
    assert "redmi_note_5a" in _extract_device_model_keys("Xiaomi Redmi Note 5А")
    assert "redmi_a1" in _extract_device_model_keys("Xiaomi Redmi A1")
    assert "redmi_s2" in _extract_device_model_keys("Xiaomi Redmi S2")
    assert "poco_f4_gt" in _extract_device_model_keys("Xiaomi Poco F4 GT")
    assert "honor_y5p" in _extract_device_model_keys("Huawei Honor Y5p")
    assert "zte_nubia_flip_2_5g" in _extract_device_model_keys("ZTE Nubia Flip 2 5G")
    assert "zte_blade_a6_max" in _extract_device_model_keys("ZTE Blade A6 Max")
    assert "vivo_iqoo_z9" in _extract_device_model_keys("Vivo iQOO Z9")
    assert "vivo_iqoo_z10k_5g" in _extract_device_model_keys("Vivo iQOO Z10К 5G")
    assert "iphone_x" in _extract_device_model_keys("Apple iPhone X")
    assert "iphone_12_pro_max" in _extract_device_model_keys("Apple iPhone 12 Pro Max")
    assert "iphone_xs_max" in _extract_device_model_keys("Apple iPhone XS Max")
    assert "iphone_16e" in _extract_device_model_keys("Apple iPhone 16e")
    assert "google_pixel_9_pro_xl" in _extract_device_model_keys("Google Pixel 9 Pro XL")
    assert "google_pixel_9_pro" in _extract_device_model_keys("Google Pixel 9 Pro")
    assert "motorola_g85" in _extract_device_model_keys("Motorola Moto G85")
    assert "tecno_pop_7_pro" in _extract_device_model_keys("Tecno Pop 7 Pro")
    assert "meizu_note_22_4g" in _extract_device_model_keys("Meizu Note 22 4G")
    assert "umidigi_g9c" in _extract_device_model_keys("Umidigi G9C")
    assert "ulefone_armor_x3" in _extract_device_model_keys("Ulefone Armor X3")
    assert "htc_u_ultra" in _extract_device_model_keys("HTC U Ultra")
    assert "asus_zenfone_11_ultra" in _extract_device_model_keys("Asus ZenFone 11 Ultra")
    assert "asus_zc520kl" in _extract_device_model_keys("Asus ZenFone 4 Max (ZC520KL)")
    assert "lg_x_max" in _extract_device_model_keys("LG X Max")
    assert "doogee_v_max" in _extract_device_model_keys("Doogee V Max")
    assert "tcl_20y" in _extract_device_model_keys("TCL 20Y")
    assert "tcl_505" in _extract_device_model_keys("TCL 505")
    assert "motorola_razr_60" in _extract_device_model_keys("Motorola Razr 60")
    assert "samsung_pixon_m8800" in _extract_device_model_keys("Samsung Pixon M8800")
    assert "alcatel_ot_710" in _extract_device_model_keys("Alcatel OT-710")
    assert "huawei_ascend_g630" in _extract_device_model_keys("Huawei Ascend G630")
    assert "huawei_y511" in _extract_device_model_keys("Huawei Ascend Y511")
    assert "honor_play" in _extract_device_model_keys("Huawei Honor Play")
    assert "honor_v9_play" in _extract_device_model_keys("Huawei Honor V9 Play")
    assert "lenovo_tab_m10_plus_gen3" in _extract_device_model_keys("Lenovo Tab M10 Plus Gen 3")
    assert "nokia_lumia_550" in _extract_device_model_keys("Nokia Lumia 550")
    assert "nokia_xl" in _extract_device_model_keys("Nokia XL")
    assert "nothing_phone_3a_lite" in _extract_device_model_keys("Nothing Phone 3a Lite")
    assert "sony_xperia_xz2_compact" in _extract_device_model_keys("Sony XZ2 Compact")
    assert "sony_xperia_l" in _extract_device_model_keys("Sony Xperia L C2105")
    assert "sony_xperia_l4" in _extract_device_model_keys("Sony Xperia L4")
    assert "sony_ericsson_c2105" in _extract_device_model_keys("Sony Xperia L C2105")
    assert "sony_ericsson_j210i" in _extract_device_model_keys("Sony-Ericsson J210i")
    assert "sony_ericsson_lt30i" in _extract_device_model_keys("Sony-Ericsson LT30i Xperia T")
    assert "xiaomi_12t_pro" in _extract_device_model_keys("Xiaomi 12T Pro")
    assert "samsung_s21_ultra" in _extract_device_model_keys("Samsung Galaxy S21 Ultra")
    assert "samsung_s25_edge" in _extract_device_model_keys("Samsung Galaxy S25 Edge (S937B)")
    assert "samsung_s937b" in _extract_device_model_keys("Samsung Galaxy S25 Edge (S937B)")
    assert "samsung_l700" in _extract_device_model_keys("Samsung L700/B3410/B5310")
    assert "meizu_m3_max" in _extract_device_model_keys("Meizu M3 Max")
    assert "infinix_note_12_pro" in _extract_device_model_keys("Infinix Note 12 Pro")
    assert "realme_gt_neo_2" in _extract_device_model_keys("Realme GT Neo 2")
    assert "itel_a49" in _extract_device_model_keys("Itel A49")
    assert "vivo_v7_plus" in _extract_device_model_keys("Vivo V7 Plus")
    assert "oppo_reno_4_pro" in _extract_device_model_keys("OPPO Reno 4 Pro")
    assert "oppo_k7" in _extract_device_model_keys("OPPO K7")
    assert "oppo_find_x7" in _extract_device_model_keys("OPPO Find X7")
    assert "oppo_a78_4g" in _extract_device_model_keys("OPPO A78 4G")
    assert "google_pixel_4_xl" in _extract_device_model_keys("Google Pixel 4 XL")
    assert "huawei_mate_10" in _extract_device_model_keys("Huawei Mate 10")
    assert "huawei_p_smart_2019" in _extract_device_model_keys("Huawei P Smart 2019")
    assert "nokia_5_3" in _extract_device_model_keys("Nokia 5.3")
    assert "nokia_6230" in _extract_device_model_keys("Nokia 6230")
    assert "lg_kg800" in _extract_device_model_keys("LG KG800")
    assert "nintendo_3ds_xl" in _extract_device_model_keys("Nintendo 3DS XL")
    assert "oneplus_x" in _extract_device_model_keys("OnePlus X")
    assert "oneplus_5" in _extract_device_model_keys("OnePlus 5")
    assert "oneplus_5t" in _extract_device_model_keys("OnePlus 5T")
    assert "oneplus_nord_ce_5" in _extract_device_model_keys("OnePlus Nord CE5")
    assert "oneplus_5" not in _extract_device_model_keys("OnePlus Nord CE5")
    assert "ipad_mini" in _extract_device_model_keys("Apple iPad mini")


def test_match_items_marks_same_model_different_region_code_for_review(db_session, tmp_path):
    product = Product(
        name="Дисплей для Huawei Honor 200 Pro (ELP-NX9) черный",
        brand="Huawei",
        category="display",
        article="C4C7R",
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="SKU-3D7R",
        name="Дисплей для Huawei Honor 200 Pro (ELP-AN00) черный",
        normalized_title="Дисплей для Huawei Honor 200 Pro (ELP-AN00) черный",
        item_type="display",
        parsed_device_brand="huawei",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["needs_review"] == 1
    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.product_id == product.id
    assert match.status == CompetitorItemMatchStatus.NEEDS_REVIEW
    assert match.rationale_json["display_model_code_review"]["reason"] == (
        "model_text_overlap_but_device_codes_differ"
    )


def test_match_items_prefers_display_code_overlap_over_higher_score_conflict(db_session, tmp_path):
    correct_product = Product(
        name="Дисплей для Samsung G980 Galaxy S20 черный",
        brand="Samsung",
        category="display",
        article="C4C7S1",
    )
    wrong_product = Product(
        name="Дисплей для Samsung G981 Galaxy S20 5G черный",
        brand="Samsung",
        category="display",
        article="C4C7S2",
    )
    db_session.add_all([correct_product, wrong_product])
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="SKU-3D7S",
        name="Дисплей для Samsung Galaxy S20 SM-G980 черный",
        normalized_title="Дисплей для Samsung Galaxy S20 SM-G980 черный",
        item_type="display",
        parsed_device_brand="samsung",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[0.98, 0.2], [1.0, 0.0]], dtype=np.float32)
    product_matrix = product_matrix / np.linalg.norm(product_matrix, axis=1, keepdims=True)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = competitor_matrix / np.linalg.norm(competitor_matrix, axis=1, keepdims=True)
    _write_embeddings(
        tmp_path,
        "our_catalog",
        product_matrix,
        [correct_product.id, wrong_product.id],
    )
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=2,
        top_k_llm=2,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["matched"] == 1
    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.product_id == correct_product.id
    assert match.status == CompetitorItemMatchStatus.ACCEPTED
    assert "auto_accept_explicit_model_code_overlap" in match.rationale_json


def test_match_items_rejects_display_phone_model_compatibility_conflict(
    db_session,
    tmp_path,
):
    redmi_note_4x = PhoneModel(brand="xiaomi", model_name="redmi note 4x")
    redmi_13 = PhoneModel(brand="xiaomi", model_name="redmi 13 4g (24040rn64y)")
    db_session.add_all([redmi_note_4x, redmi_13])
    db_session.flush()

    product = Product(
        name="Дисплей для Xiaomi Redmi 13 4G (24040RN64Y) черный",
        brand="Xiaomi",
        category="display",
        article="C4C7PM",
    )
    item = CompetitorItem(
        competitor="moba",
        external_id="SKU-3D7PM",
        name="Дисплей для Xiaomi Redmi Note 4X/4 Global Version черный",
        normalized_title="Дисплей для Xiaomi Redmi Note 4X/4 Global Version черный",
        item_type="display",
        parsed_device_brand="xiaomi",
    )
    db_session.add_all([product, item])
    db_session.flush()
    db_session.add_all(
        [
            ProductPhoneModel(
                product_id=product.id,
                phone_model_id=redmi_13.id,
                source="onec",
                raw_value="redmi 13 4g (24040rn64y)",
                confidence=0.95,
            ),
            CompetitorItemCompatibility(
                competitor_item_id=item.id,
                phone_model_id=redmi_note_4x.id,
                device_brand="xiaomi",
                device_model="redmi note 4x",
                source="parser",
            ),
        ]
    )
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["skipped_no_candidates"] == 1
    assert db_session.query(CompetitorItemMatch).count() == 0


def test_match_items_allows_display_phone_model_compatibility_overlap(db_session, tmp_path):
    redmi_note_4x = PhoneModel(brand="xiaomi", model_name="redmi note 4x")
    db_session.add(redmi_note_4x)
    db_session.flush()

    product = Product(
        name="Дисплей для Xiaomi Redmi Note 4X черный",
        brand="Xiaomi",
        category="display",
        article="C4C7PO",
    )
    item = CompetitorItem(
        competitor="moba",
        external_id="SKU-3D7PO",
        name="Дисплей для Xiaomi Redmi Note 4X/4 Global Version черный",
        normalized_title="Дисплей для Xiaomi Redmi Note 4X/4 Global Version черный",
        item_type="display",
        parsed_device_brand="xiaomi",
    )
    db_session.add_all([product, item])
    db_session.flush()
    db_session.add_all(
        [
            ProductPhoneModel(
                product_id=product.id,
                phone_model_id=redmi_note_4x.id,
                source="onec",
                raw_value="redmi note 4x",
                confidence=0.95,
            ),
            CompetitorItemCompatibility(
                competitor_item_id=item.id,
                phone_model_id=redmi_note_4x.id,
                device_brand="xiaomi",
                device_model="redmi note 4x",
                source="parser",
            ),
        ]
    )
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["matched"] == 1
    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.product_id == product.id


def test_match_items_allows_display_exact_text_when_phone_model_links_are_stale(
    db_session,
    tmp_path,
):
    pixel_9_pro = PhoneModel(brand="google", model_name="pixel 9 pro")
    stale_pixel_9_pro_xl = PhoneModel(brand="google", model_name="pixel 9 pro xl")
    db_session.add_all([pixel_9_pro, stale_pixel_9_pro_xl])
    db_session.flush()

    product = Product(
        name="Дисплей для Google Pixel 9 Pro (GR83Y/GEC77/GWVK6) + тачскрин (черный) (ORIG)",
        brand="Google",
        category="display",
        article="PIX9PRO",
        subject="дисплей",
    )
    item = CompetitorItem(
        competitor="moba",
        external_id="LCD-GGL-PXL-9-PR-CP-B-OR",
        name="Дисплей для Google Pixel 9 Pro (GR83Y) в сборе с тачскрином Черный - OR",
        normalized_title="Дисплей Google Pixel 9 Pro GR83Y тачскрин Черный OR",
        item_type="display",
        parsed_device_brand="google",
    )
    db_session.add_all([product, item])
    db_session.flush()
    db_session.add_all(
        [
            ProductPhoneModel(
                product_id=product.id,
                phone_model_id=stale_pixel_9_pro_xl.id,
                source="onec",
                raw_value="pixel 9 pro xl",
                confidence=0.95,
            ),
            CompetitorItemCompatibility(
                competitor_item_id=item.id,
                phone_model_id=pixel_9_pro.id,
                device_brand="google",
                device_model="pixel 9 pro",
                source="parser",
            ),
        ]
    )
    db_session.flush()

    assert basic_candidate_guardrails(item, product).allowed is False

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["needs_review"] == 1
    assert stats["auto_accepted_display_original_quality"] == 1
    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.product_id == product.id
    assert match.status == CompetitorItemMatchStatus.ACCEPTED
    assert match.rationale_json["auto_accept_display_original_quality"]["reason"] == (
        "display_original_quality_exact_model"
    )


def test_match_items_allows_redmi_pro_prime_text_model_synonyms(db_session, tmp_path):
    product = Product(
        name="Тачскрин для Xiaomi Redmi 4 Prime (Pro) черный",
        brand="Xiaomi",
        category="display",
        article="C4C7C",
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="SKU-3D7C",
        name="Тачскрин для Xiaomi Redmi 4 PRO / Prime черный",
        normalized_title="Тачскрин для Xiaomi Redmi 4 PRO Prime черный",
        item_type="display",
        parsed_device_brand="xiaomi",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["matched"] == 1
    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.product_id == product.id


def test_match_items_rejects_honor_text_model_conflict(db_session, tmp_path):
    product = Product(
        name="Дисплей для Huawei Honor 8X/8X Premium (JSN-L21) черный",
        brand="Huawei",
        category="display",
        article="C4C8",
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="SKU-3D8",
        name="Дисплей для Huawei Honor 8 Lite (PRA-TL10) черный",
        normalized_title="Дисплей для Huawei Honor 8 Lite черный",
        item_type="display",
        parsed_device_brand="huawei",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["skipped_no_candidates"] == 1
    assert db_session.query(CompetitorItemMatch).count() == 0


def test_match_items_rejects_huawei_nova_text_model_conflict(db_session, tmp_path):
    product = Product(
        name="Дисплей для Huawei Nova 8i (в сборе с тачскрином) (черный)",
        brand="Huawei",
        category="display",
        article="C4C8N",
        display_has_frame=False,
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="LCD-HUW-NVA-11I-CP-B-OR",
        name="Дисплей для Huawei Nova 11i (MAO-LX9N) в сборе с тачскрином Черный - OR",
        normalized_title="Дисплей для Huawei Nova 11i MAO-LX9N в сборе с тачскрином Черный OR",
        item_type="display",
        parsed_device_brand="huawei",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["skipped_no_candidates"] == 1
    assert db_session.query(CompetitorItemMatch).count() == 0


def test_match_items_rejects_iphone_pro_vs_pro_max_text_model_conflict(db_session, tmp_path):
    product = Product(
        name="Дисплей для Apple iPhone 12 Pro + тачскрин (черный)",
        brand="Apple",
        category="display",
        article="C4C8I",
        display_has_frame=False,
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="LCD-PMI-12-PR-MAX-CP-B-OR",
        name="Дисплей для iPhone 12 Pro Max (A2411) в сборе с тачскрином Черный - OR",
        normalized_title="Дисплей для iPhone 12 Pro Max A2411 в сборе с тачскрином Черный OR",
        item_type="display",
        parsed_device_brand="apple",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["skipped_no_candidates"] == 1
    assert db_session.query(CompetitorItemMatch).count() == 0


def test_match_items_allows_text_model_overlap_from_compatible_alias(db_session, tmp_path):
    product = Product(
        name="Дисплей Xiaomi Redmi Note 11 4G / Poco M4 Pro 4G черный",
        brand="Xiaomi",
        category="display",
        article="C4C9",
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="SKU-3D9",
        name="Дисплей Xiaomi Redmi Note 11S 4G/Poco M4 Pro 4G черный",
        normalized_title="Дисплей Xiaomi Redmi Note 11S 4G Poco M4 Pro 4G черный",
        item_type="display",
        parsed_device_brand="xiaomi",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["matched"] == 1
    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.product_id == product.id


def test_match_items_rejects_shared_base_model_with_different_leaf_variant(db_session, tmp_path):
    product = Product(
        name="Дисплей Xiaomi Poco M6 Pro 4G черный",
        brand="Xiaomi",
        category="display",
        article="C4D0",
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="SKU-3D10",
        name="Дисплей Xiaomi Poco M6 Pro 5G черный",
        normalized_title="Дисплей Xiaomi Poco M6 Pro 5G черный",
        item_type="display",
        parsed_device_brand="xiaomi",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["skipped_no_candidates"] == 1
    assert db_session.query(CompetitorItemMatch).count() == 0


def test_match_items_allows_display_model_code_overlap(db_session, tmp_path):
    product = Product(
        name="Дисплей Samsung Galaxy A14 SM-A145F черный",
        brand="Samsung",
        category="display",
        article="C4D",
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="SKU-3D4",
        name="Дисплей Samsung Galaxy A14 SM-A145 черный",
        normalized_title="Дисплей Samsung Galaxy A14 SM-A145 черный",
        item_type="display",
        parsed_device_brand="samsung",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["matched"] == 1
    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.status == CompetitorItemMatchStatus.ACCEPTED
    assert "auto_accept_explicit_model_code_overlap" in match.rationale_json
    assert match.product_id == product.id


def test_match_items_rejects_xiaomi_display_model_code_conflict(db_session, tmp_path):
    product = Product(
        name="Дисплей Xiaomi Redmi 9 M2003J15SC черный",
        brand="Xiaomi",
        category="display",
        article="C4E",
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="SKU-3D5",
        name="Дисплей Xiaomi Redmi Note 9S M2003J6A1G черный",
        normalized_title="Дисплей Xiaomi Redmi Note 9S M2003J6A1G черный",
        item_type="display",
        parsed_device_brand="xiaomi",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["skipped_no_candidates"] == 1
    assert db_session.query(CompetitorItemMatch).count() == 0


def test_match_items_allows_xiaomi_display_regional_code_overlap(db_session, tmp_path):
    product = Product(
        name="Дисплей Xiaomi Redmi 8 M1908C3IG/M1908C3KG черный",
        brand="Xiaomi",
        category="display",
        article="C4F",
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="SKU-3D6",
        name="Дисплей Xiaomi Redmi 8 M1908C3IC черный",
        normalized_title="Дисплей Xiaomi Redmi 8 M1908C3IC черный",
        item_type="display",
        parsed_device_brand="xiaomi",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["matched"] == 1
    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.status == CompetitorItemMatchStatus.ACCEPTED
    assert "auto_accept_explicit_model_code_overlap" in match.rationale_json
    assert match.product_id == product.id


def test_match_items_display_word_on_power_bank_is_not_screen_part(db_session, tmp_path):
    product = Product(
        name="Дисплей iPhone 12 черный",
        brand="Apple",
        category="display",
        article="C5",
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="SKU-3E",
        name="Внешний АКБ HOCO 10000mAh LED дисплей черный",
        normalized_title="Внешний АКБ HOCO 10000mAh LED дисплей черный",
        item_type="display",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["skipped_no_candidates"] == 1
    assert db_session.query(CompetitorItemMatch).count() == 0


def test_match_items_display_word_feature_uses_original_name(db_session, tmp_path):
    product = Product(
        name="Дисплей iPhone 12 черный",
        brand="Apple",
        category="display",
        article="C6",
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="SKU-3F",
        name="Беспроводное зарядное устройство Borofone BQ39 (15W, зарядка, дисплей) Черный",
        normalized_title="Беспроводное зарядное устройство Borofone BQ39 15W черный",
        item_type="display",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["skipped_no_candidates"] == 1
    assert db_session.query(CompetitorItemMatch).count() == 0


def test_match_items_reclassifies_non_display_accessory_with_lcd_word(db_session, tmp_path):
    product = Product(
        name="Дисплей для Nintendo Switch OLED черный",
        brand="Nintendo",
        category="display",
        article="C6B",
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="SKU-3F2",
        name="Защитная пленка Dobe iTNS-1195 для Nintendo Switch OLED (с фиксатором)",
        normalized_title="Защитная пленка Dobe iTNS-1195 для Nintendo Switch OLED",
        item_type="display",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["skipped_no_candidates"] == 1
    assert db_session.query(CompetitorItemMatch).count() == 0


def test_match_items_reclassifies_generic_screen_film_as_other(db_session, tmp_path):
    product = Product(
        name="Пленка OCA для проклейки дисплея Apple iPhone 11",
        category="other",
        article="C6B2",
    )
    display_product = Product(
        name="Дисплей Apple iPhone 11 черный",
        brand="Apple",
        category="display",
        article="C6B3",
    )
    db_session.add_all([product, display_product])
    db_session.flush()

    item = CompetitorItem(
        competitor="liberti",
        external_id="SKU-3F2B",
        name="Пленка (RINCO) на экран iPhone 11",
        normalized_title="Пленка RINCO на экран iPhone 11",
        item_type="display",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0], [0.99, 0.01]], dtype=np.float32)
    product_matrix = product_matrix / np.linalg.norm(product_matrix, axis=1, keepdims=True)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id, display_product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.001,
        top_k=2,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["matched"] == 1
    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.product_id == product.id


def test_match_items_reclassifies_non_display_tool_with_lcd_word(db_session, tmp_path):
    product = Product(
        name="Дисплей для Samsung Galaxy A51 черный",
        brand="Samsung",
        category="display",
        article="C6C",
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="SKU-3F3",
        name="Термовоздушная паяльная станция BAKU BK-857D (580W, 100-450°C, LCD)",
        normalized_title="Термовоздушная паяльная станция BAKU BK-857D LCD",
        item_type="display",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["skipped_no_candidates"] == 1
    assert db_session.query(CompetitorItemMatch).count() == 0


def test_effective_item_type_reclassifies_display_word_accessories_as_other():
    cases = [
        "Инструмент для демонтажа экрана RELIFE TD1-B",
        "Интеллектуальный модуль Aixun PM02 для источника питания",
        "TWS гарнитура Borofone с LED дисплеем",
        "Колонки портативные Borofone с LCD дисплеем",
        "Бесконтактный модуль NFC Samsung Galaxy S21",
        "Автоматический ламинатор экранов TBK-988",
        "Триммер для резки OCA пленки",
        'Светодиодная подсветка для телевизоров Philips 42" GJ-2K16',
        "Модуль памяти Kingston SODIMM DDR3 8GB 1600 MHz",
        "Ящик для запчастей модульный",
        "Ультразвуковая ванночка YAXUN YX2000A с цифровым дисплеем",
    ]

    for name in cases:
        item = CompetitorItem(
            competitor="moba",
            external_id=name,
            name=name,
            normalized_title=name,
            item_type="display",
        )

        assert _effective_item_type(item) == "other"


def test_effective_item_type_keeps_phone_model_nfc_display_as_display():
    item = CompetitorItem(
        competitor="moba",
        external_id="LCD-XIA-POCO-X3-NFC",
        name="Дисплей для Xiaomi Poco X3 NFC + тачскрин",
        normalized_title="Дисплей для Xiaomi Poco X3 NFC + тачскрин",
        item_type="display",
    )

    assert _effective_item_type(item) == "display"


def test_effective_item_type_prefers_back_cover_over_bundle_flex():
    item = CompetitorItem(
        name="Задняя крышка для iPhone 17 в сборе со стеклом камеры и шлейфом MagSafe",
        item_type="flex",
        category="корпус",
        category_group="запчасти",
    )

    assert _effective_item_type(item) == "housing"


def test_match_items_rejects_housing_text_model_conflict(db_session, tmp_path):
    product = Product(
        name="Задняя крышка для Xiaomi Redmi Note 13 Pro+ 5G (фиолетовый)",
        brand="Xiaomi",
        category="housing",
        article="BTC-WRONG",
    )
    item = CompetitorItem(
        competitor="moba",
        external_id="BTC-XMI-RMINT-7-BL-OR",
        name="Задняя крышка для Xiaomi Redmi Note 7/7 Pro (M1901F7H) Синий - Премиум",
        normalized_title="Задняя крышка для Xiaomi Redmi Note 7/7 Pro Синий",
        item_type="housing",
        parsed_device_brand="xiaomi",
    )
    db_session.add_all([product, item])
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["skipped_no_candidates"] == 1
    assert db_session.query(CompetitorItemMatch).count() == 0


def test_match_items_prunes_generated_match_when_guardrails_remove_candidates(
    db_session,
    tmp_path,
):
    product = Product(
        name="Задняя крышка для Xiaomi Redmi Note 13 Pro+ 5G (фиолетовый)",
        brand="Xiaomi",
        category="housing",
        article="BTC-WRONG",
    )
    item = CompetitorItem(
        competitor="moba",
        external_id="BTC-XMI-RMINT-7-BL-OR-EXISTING",
        name="Задняя крышка для Xiaomi Redmi Note 7/7 Pro (M1901F7H) Синий - Премиум",
        normalized_title="Задняя крышка для Xiaomi Redmi Note 7/7 Pro Синий",
        item_type="housing",
        parsed_device_brand="xiaomi",
    )
    db_session.add_all([product, item])
    db_session.flush()
    db_session.add(
        CompetitorItemMatch(
            competitor_item_id=item.id,
            product_id=product.id,
            status=CompetitorItemMatchStatus.SUGGESTED,
            method=CompetitorItemMatchMethod.EMBEDDING_AUTO,
        )
    )
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=False,
        include_status=["suggested"],
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["pruned_no_candidates"] == 1
    assert db_session.query(CompetitorItemMatch).count() == 0


def test_effective_item_type_reclassifies_standalone_display_frame_as_housing():
    item = CompetitorItem(
        competitor="liberti",
        external_id="242489",
        name="Рамка дисплея и тачскрина для iPad 2 (белая)",
        normalized_title="Рамка дисплея и тачскрина для iPad 2 белая",
        item_type="display",
    )

    assert _effective_item_type(item) == "housing"


def test_effective_item_type_reclassifies_display_backlight_as_other():
    item = CompetitorItem(
        competitor="liberti",
        external_id="iphone-5-backlight",
        name="Подсветка дисплея для Apple iPhone 5",
        normalized_title="Подсветка дисплея для Apple iPhone 5",
        item_type="display",
    )

    assert _effective_item_type(item) == "other"


def test_competitor_display_frame_prefers_moba_sku_over_touch_kit_inference():
    item = CompetitorItem(
        competitor="moba",
        external_id="LCD-APL-IP12-FR-B",
        name="Дисплей для iPhone 12 в сборе с тачскрином Черный",
        normalized_title="Дисплей для iPhone 12 в сборе с тачскрином Черный",
        item_type="display",
    )

    assert _competitor_display_has_frame(item) is True


def test_match_items_reclassifies_trackpad_with_touch_word(db_session, tmp_path):
    product = Product(
        name="Матрица в сборе для Apple MacBook Pro 16 Retina A2141 серебристый",
        brand="Apple",
        category="display",
        article="C6C2",
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="TPD-MB-PR-16-A2141-SL",
        name='Трекпад (тачпад) для MacBook Pro 16" A2141 (2019) Серебро',
        normalized_title="Трекпад тачпад для MacBook Pro 16 A2141 2019 Серебро",
        item_type="display",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["skipped_no_candidates"] == 1
    assert db_session.query(CompetitorItemMatch).count() == 0


def test_match_items_reclassifies_ic_controller_as_board(db_session, tmp_path):
    product = Product(
        name="Микросхема контроллер питания BQ24196M",
        category="board",
        article="C6C3",
    )
    display_product = Product(
        name="Дисплей для Lenovo A5000 черный",
        category="display",
        article="C6C4",
    )
    db_session.add_all([product, display_product])
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="IC-BQ24296M",
        name="Микросхема BQ24296M (Контроллер питания для Lenovo/Meizu/Philips)",
        normalized_title="Микросхема BQ24296M Контроллер питания для Lenovo Meizu Philips",
        item_type="display",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0], [0.99, 0.01]], dtype=np.float32)
    product_matrix = product_matrix / np.linalg.norm(product_matrix, axis=1, keepdims=True)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id, display_product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.001,
        top_k=2,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["matched"] == 1
    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.product_id == product.id


def test_match_items_reclassifies_gamepad_mechanism_with_oled_word(db_session, tmp_path):
    product = Product(
        name="Дисплей для Steam Deck OLED + тачскрин черный",
        category="display",
        article="C6C5",
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="TMR-GMD-GLK-ST-DC-LED-2PCS",
        name="TMR механизм геймпада Gulikit для Steam Deck OLED (2 шт.)",
        normalized_title="TMR механизм геймпада Gulikit для Steam Deck OLED 2 шт",
        item_type="display",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["skipped_no_candidates"] == 1
    assert db_session.query(CompetitorItemMatch).count() == 0


def test_match_items_rejects_notebook_matrix_against_portable_monitor(db_session, tmp_path):
    product = Product(
        name="Монитор портативный 15.6' HDR 1080p IPS",
        category="display",
        article="C6C6",
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="liberti",
        external_id="SKU-3F6",
        name='Матрица ноутбука 15.6" 1920x1080 Matte 40pin IPS 350mm',
        normalized_title="Матрица ноутбука 15.6 1920x1080 Matte 40pin IPS 350mm",
        item_type="display",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["skipped_no_candidates"] == 1
    assert db_session.query(CompetitorItemMatch).count() == 0


def test_match_items_rejects_laptop_flex_against_tablet_flex(db_session, tmp_path):
    product = Product(
        name="Шлейф межплатный для Lenovo Tab P11",
        brand="Lenovo",
        category="flex",
        subject="Шлейфы для планшетов",
        article="TAB-FLEX",
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="liberti",
        external_id="NOTE-FLEX",
        name="Шлейф матрицы для ноутбука Lenovo IdeaPad",
        normalized_title="Шлейф матрицы для ноутбука Lenovo IdeaPad",
        item_type="flex",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["skipped_no_candidates"] == 1
    assert db_session.query(CompetitorItemMatch).count() == 0


def test_match_items_new_item_without_attrs_or_compat_needs_review(db_session, tmp_path):
    product = Product(
        name="Аккумулятор для Samsung Galaxy A50",
        brand="Samsung",
        category="battery",
        article="BAT-A50",
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="BAT-A50-COMP",
        name="АКБ Samsung Galaxy A50",
        normalized_title="АКБ Samsung Galaxy A50",
        item_type="battery",
        first_seen_at=date(2026, 5, 2),
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
        auto_accept_unique=True,
    )

    assert stats["needs_review"] == 1
    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.status == CompetitorItemMatchStatus.NEEDS_REVIEW


def test_match_items_auto_accepts_touchscreen_with_shared_device_codes(db_session, tmp_path):
    product = Product(
        name="Тачскрин для Samsung T530/T531/T535 Galaxy Tab 4 10.1 (черный)",
        brand="Samsung",
        category="touchscreen",
        article="037531",
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="liberti",
        external_id="219617",
        name="Тачскрин для Samsung Galaxy Tab 4 10.1 SM-T531/T530 (черный)",
        normalized_title="Тачскрин для Samsung Galaxy Tab 4 10.1 SM-T531/T530 черный",
        item_type="display",
        parsed_device_brand="samsung",
        first_seen_at=date(2026, 5, 2),
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
        auto_accept_min_score=0.8,
    )

    assert stats["matched"] == 1
    assert stats["needs_review"] == 0
    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.status == CompetitorItemMatchStatus.ACCEPTED
    assert match.rationale_json["auto_accept_explicit_model_code_overlap"]["overlap_codes"] == [
        "T530",
        "T531",
    ]
    compatibilities = db_session.execute(
        select(CompetitorItemCompatibility).where(
            CompetitorItemCompatibility.competitor_item_id == item.id
        )
    ).scalars()
    assert {(row.device_brand, row.device_model) for row in compatibilities} == {
        ("samsung", "t530"),
        ("samsung", "t531"),
    }


def test_code_overlap_sweeper_accepts_suggested_and_creates_compatibility(db_session):
    product = Product(
        name="Тачскрин для Samsung T210/T211 Galaxy Tab 3 7.0 (черный)",
        brand="Samsung",
        category="touchscreen",
        article="033570",
    )
    item = CompetitorItem(
        competitor="liberti",
        external_id="232552",
        name="Тачскрин для Samsung Galaxy Tab 3 7.0 SM-T211 (черный)",
        normalized_title="Тачскрин для Samsung Galaxy Tab 3 7.0 SM-T211 черный",
        item_type="display",
        parsed_device_brand="samsung",
    )
    db_session.add_all([product, item])
    db_session.flush()
    db_session.add(
        CompetitorItemMatch(
            competitor_item_id=item.id,
            product_id=product.id,
            status=CompetitorItemMatchStatus.SUGGESTED,
            method=CompetitorItemMatchMethod.EMBEDDING_AUTO,
            final_score=0.8568,
        )
    )
    db_session.flush()

    assert _auto_accept_explicit_code_overlap_matches(db_session, min_score=0.80) == 1

    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.status == CompetitorItemMatchStatus.ACCEPTED
    compatibilities = db_session.execute(
        select(CompetitorItemCompatibility).where(
            CompetitorItemCompatibility.competitor_item_id == item.id
        )
    ).scalars()
    assert {(row.device_brand, row.device_model) for row in compatibilities} == {
        ("samsung", "t211")
    }


def test_short_tecno_codes_are_extracted_for_code_overlap():
    assert "LI6" in _extract_device_codes("Дисплей для Tecno Pova 6 Neo (LI6)")
    assert "24116RACCG" in _extract_device_codes(
        "Дисплей для Xiaomi Redmi Note 14 Pro 4G (24116RACCG)"
    )
    assert "AI2302" in _extract_device_codes("Дисплей для Asus ZenFone 10 (AI2302)")


def test_model_text_sweeper_accepts_xiaomi_regional_model_codes(db_session):
    product = Product(
        name="Дисплей для Xiaomi 14T Pro (MZB0HH9RU) + тачскрин (черный)",
        brand="Xiaomi",
        article="070131",
    )
    item = CompetitorItem(
        competitor="moba",
        external_id="LCD-XMI-14T-PR-FR-B-OR-SP",
        name="Дисплей для Xiaomi 14T Pro (2407FPN8EG) модуль с рамкой Черный - OR (SP)",
        normalized_title="Дисплей для Xiaomi 14T Pro 2407FPN8EG модуль с рамкой Черный",
        item_type="display",
        parsed_device_brand="xiaomi",
    )
    db_session.add_all([product, item])
    db_session.flush()
    db_session.add(
        CompetitorItemMatch(
            competitor_item_id=item.id,
            product_id=product.id,
            status=CompetitorItemMatchStatus.SUGGESTED,
            method=CompetitorItemMatchMethod.EMBEDDING_AUTO,
            final_score=0.8267,
        )
    )
    db_session.flush()

    assert _auto_accept_explicit_model_text_matches(db_session, min_score=0.80) == 1

    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.status == CompetitorItemMatchStatus.ACCEPTED
    assert match.rationale_json["auto_accept_explicit_model_text"]["model"] == "xiaomi 14t pro"
    compatibility = db_session.execute(
        select(CompetitorItemCompatibility).where(
            CompetitorItemCompatibility.competitor_item_id == item.id
        )
    ).scalar_one()
    assert (compatibility.device_brand, compatibility.device_model) == ("xiaomi", "14t pro")


def test_explicit_model_conflict_rules_cover_known_false_positives():
    assert (
        _explicit_model_conflict_reason(
            CompetitorItem(
                name="LCD дисплей для Xiaomi Mi Mix с тачскрином (черный)",
                item_type="display",
            ),
            Product(
                name=(
                    "Дисплей для Xiaomi Mix Flip (2405CPX3DG) + тачскрин " "(внутренний) (черный)"
                )
            ),
        )
        == "xiaomi_mi_mix_vs_mix_flip"
    )
    assert (
        _explicit_model_conflict_reason(
            CompetitorItem(
                name="LCD дисплей для Xiaomi Redmi Go с тачскрином в рамке (черный)",
                item_type="display",
            ),
            Product(name="Дисплей для Xiaomi Redmi 10 (21061119DG) + тачскрин (черный)"),
        )
        == "xiaomi_redmi_go_vs_numbered_redmi"
    )
    assert (
        _explicit_model_conflict_reason(
            CompetitorItem(
                name='Дисплей для Xiaomi Redmi Pad SE 8.7" (24075RP89G) Черный',
                item_type="display",
            ),
            Product(name="Дисплей для Xiaomi Redmi Pad SE (23073RPBFG) + тачскрин (черный)"),
        )
        == "xiaomi_redmi_pad_se_87_conflict"
    )
    assert (
        _explicit_model_conflict_reason(
            CompetitorItem(
                name="Дисплей для Xiaomi 14T Pro (2407FPN8EG) модуль с рамкой Черный",
                item_type="display",
            ),
            Product(name="Дисплей для Xiaomi 14T Pro (MZB0HH9RU) + тачскрин (черный)"),
        )
        is None
    )


def test_model_conflict_sweeper_rejects_obvious_false_positive(db_session):
    product = Product(
        name="Дисплей для Xiaomi Redmi 10 (21061119DG) + тачскрин (черный)",
        brand="Xiaomi",
        article="063542",
    )
    item = CompetitorItem(
        competitor="liberti",
        external_id="324982",
        name="LCD дисплей для Xiaomi Redmi Go с тачскрином в рамке (черный) 100% OR",
        item_type="display",
    )
    db_session.add_all([product, item])
    db_session.flush()
    db_session.add(
        CompetitorItemMatch(
            competitor_item_id=item.id,
            product_id=product.id,
            status=CompetitorItemMatchStatus.NEEDS_REVIEW,
            method=CompetitorItemMatchMethod.EMBEDDING_AUTO,
            final_score=0.805,
        )
    )
    db_session.flush()

    assert _auto_reject_explicit_model_conflicts(db_session) == 1

    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.status == CompetitorItemMatchStatus.REJECTED
    assert match.rationale_json["auto_reject_explicit_model_conflict"]["reason"] == (
        "xiaomi_redmi_go_vs_numbered_redmi"
    )


def test_guardrail_sweeper_rejects_notebook_vs_phone_false_positive(db_session):
    product = Product(
        name="Задняя крышка для Lenovo IdeaPhone S850 (белый)",
        brand="Lenovo",
        article="037502",
    )
    item = CompetitorItem(
        competitor="moba",
        external_id="SC-CVR-LP-LNV-315IML05-SL",
        name="Крышка матрицы для ноутбука Lenovo IdeaPad 3-15IML05 Серебро",
        item_type="housing",
    )
    db_session.add_all([product, item])
    db_session.flush()
    db_session.add(
        CompetitorItemMatch(
            competitor_item_id=item.id,
            product_id=product.id,
            status=CompetitorItemMatchStatus.NEEDS_REVIEW,
            method=CompetitorItemMatchMethod.EMBEDDING_AUTO,
            final_score=0.6177,
        )
    )
    db_session.flush()

    assert _auto_reject_guardrail_device_group_conflicts(db_session) == 1

    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.status == CompetitorItemMatchStatus.REJECTED
    assert match.rationale_json["auto_reject_guardrail_conflict"]["reason"] == (
        "device_group_conflict"
    )


def test_display_attribute_conflict_rules_cover_original_vs_copy_construction():
    assert (
        _explicit_display_attribute_conflict_reason(
            CompetitorItem(
                name=(
                    "Дисплей для iPhone 15 Pro Max (A3106) в сборе с тачскрином "
                    "Черный - (In-Cell, HD+)"
                ),
                normalized_title="Дисплей для iPhone 15 Pro Max A3106 Черный In-Cell HD+",
                item_type="display",
            ),
            Product(
                name="Дисплей для Apple iPhone 15 Pro Max (черный) (биток) (ORIG)",
            ),
        )
        == "display_original_refurb_vs_regular_competitor"
    )
    assert (
        _explicit_display_attribute_conflict_reason(
            CompetitorItem(
                name=(
                    "Дисплей для iPhone 15 Pro (A3102) в сборе с тачскрином "
                    "Черный - DD (Soft OLED, Full HD)"
                ),
                normalized_title="Дисплей для iPhone 15 Pro A3102 Черный DD Soft OLED",
                item_type="display",
            ),
            Product(name="Дисплей для Apple iPhone 15 Pro + тачскрин (черный) (FOG) (ORIG)"),
        )
        == "display_original_vs_copy_construction"
    )
    assert (
        _explicit_display_attribute_conflict_reason(
            CompetitorItem(
                name=(
                    "Дисплей для Samsung Galaxy A23 (A235F) в сборе с тачскрином "
                    "Черный Mecanico - AMP"
                ),
                normalized_title="Дисплей Samsung Galaxy A23 A235F Черный Mecanico AMP",
                item_type="display",
            ),
            Product(
                name=("Дисплей для Samsung A235 Galaxy A23 + тачскрин " "(черный) (ORIG100) (SP)"),
            ),
        )
        == "display_original_vs_copy_signal"
    )
    assert (
        _explicit_display_attribute_conflict_reason(
            CompetitorItem(
                name=(
                    "Дисплей для iPhone 13 (A2635) в сборе с тачскрином Черный - "
                    "OR (Снятый, заменено ТОЛЬКО стекло)"
                ),
                normalized_title="Дисплей для iPhone 13 Черный OR заменено стекло",
                item_type="display",
            ),
            Product(
                name="Дисплей для Apple iPhone 13 (черный) (биток) (ORIG)",
            ),
        )
        is None
    )
    assert (
        _explicit_display_attribute_conflict_reason(
            CompetitorItem(
                name=(
                    "LCD дисплей для Samsung Galaxy S24+ SM-S926 "
                    "в сборе в рамке OLED Full Size (черный)"
                ),
                normalized_title="LCD дисплей Samsung Galaxy S24+ SM-S926 OLED Full Size черный",
                item_type="display",
            ),
            Product(
                name=(
                    "Дисплей для Samsung S926 Galaxy S24+ + тачскрин "
                    "(черный) (в рамке) (ORIG100) (SP)"
                ),
            ),
        )
        == "display_original_vs_aftermarket_competitor"
    )
    assert (
        _explicit_display_attribute_conflict_reason(
            CompetitorItem(
                name=(
                    "Дисплей для Google Pixel 8 (GKWS6/G9BQD/GZPFO/GPJ41) "
                    "в сборе с тачскрином Черный - (OLED)"
                ),
                normalized_title="Дисплей Google Pixel 8 GKWS6 G9BQD GZPFO GPJ41 OLED",
                item_type="display",
            ),
            Product(
                name=(
                    "Дисплей для Google Pixel 8 (GKWS6/G9BQD/GZPFO/GPJ41) "
                    "+ тачскрин (черный) (OLED)"
                ),
                display_quality="Original",
            ),
        )
        is None
    )


def test_display_attribute_sweeper_rejects_obvious_copy_vs_bitok(db_session):
    product = Product(
        name="Дисплей для Apple iPhone 14 Pro Max (черный) (биток) (ORIG)",
        brand="Apple",
        article="063557",
    )
    item = CompetitorItem(
        competitor="moba",
        external_id="LCD-PMIPRM140-CP-B-INCL-HD-PLS",
        name=(
            "Дисплей для iPhone 14 Pro Max (A2895) в сборе с тачскрином " "Черный - (In-Cell, HD+)"
        ),
        item_type="display",
    )
    db_session.add_all([product, item])
    db_session.flush()
    db_session.add(
        CompetitorItemMatch(
            competitor_item_id=item.id,
            product_id=product.id,
            status=CompetitorItemMatchStatus.NEEDS_REVIEW,
            method=CompetitorItemMatchMethod.EMBEDDING_AUTO,
            final_score=0.835,
        )
    )
    db_session.flush()

    assert _auto_reject_display_attribute_conflicts(db_session) == 1

    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.status == CompetitorItemMatchStatus.REJECTED
    assert match.rationale_json["auto_reject_display_attribute_conflict"]["reason"] == (
        "display_original_refurb_vs_regular_competitor"
    )


def test_display_attribute_sweeper_rejects_aftermarket_display_against_explicit_orig(
    db_session,
):
    product = Product(
        name=(
            "Дисплей для Samsung S926 Galaxy S24+ + тачскрин " "(черный) (в рамке) (ORIG100) (SP)"
        ),
        brand="Samsung",
        article="062580",
        subject="дисплей",
    )
    item = CompetitorItem(
        competitor="liberti",
        external_id="471476",
        name="LCD дисплей для Samsung Galaxy S24+ SM-S926 в сборе в рамке OLED Full Size (черный)",
        normalized_title="LCD дисплей Samsung Galaxy S24+ SM-S926 OLED Full Size черный",
        item_type="display",
    )
    db_session.add_all([product, item])
    db_session.flush()
    db_session.add(
        CompetitorItemMatch(
            competitor_item_id=item.id,
            product_id=product.id,
            status=CompetitorItemMatchStatus.AMBIGUOUS,
            method=CompetitorItemMatchMethod.EMBEDDING_AUTO,
            final_score=0.8065,
        )
    )
    db_session.flush()

    assert _auto_reject_display_attribute_conflicts(db_session) == 1

    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.status == CompetitorItemMatchStatus.REJECTED
    assert match.rationale_json["auto_reject_display_attribute_conflict"]["reason"] == (
        "display_original_vs_aftermarket_competitor"
    )


def test_display_matrix_vendor_tags_are_extracted_from_names():
    item = CompetitorItem(
        name=(
            "Дисплей для iPhone 14 Pro (A2891) в сборе с тачскрином "
            "Черный - DD (Soft OLED, Full HD)"
        ),
        normalized_title="Дисплей iPhone 14 Pro A2891 DD Soft OLED",
        item_type="display",
    )
    product = Product(
        name="Дисплей для Apple iPhone 14 Pro + тачскрин (черный) (JCID) (Soft Oled)",
    )

    assert _competitor_display_matrix_tags(item) == set()
    assert _product_display_matrix_tags(product) == {"JCID"}
    assert _competitor_display_matrix_vendor_tags(item) == {"DD"}
    assert _product_display_matrix_vendor_tags(product) == {"JCID"}


def test_display_matrix_tag_sweeper_rejects_vendor_tag_conflict(db_session):
    product = Product(
        name="Дисплей для Apple iPhone 14 Pro + тачскрин (черный) (JCID) (Soft Oled)",
        brand="Apple",
        article="070917",
        subject="дисплей",
    )
    item = CompetitorItem(
        competitor="moba",
        external_id="LCD-PMIPR140-CP-B-DD-SOD-VRF",
        name=(
            "Дисплей для iPhone 14 Pro (A2891) в сборе с тачскрином "
            "Черный - DD (Soft OLED, Full HD, 120 Гц)"
        ),
        normalized_title="Дисплей iPhone 14 Pro A2891 DD Soft OLED 120 Гц",
        item_type="display",
    )
    db_session.add_all([product, item])
    db_session.flush()
    db_session.add(
        CompetitorItemMatch(
            competitor_item_id=item.id,
            product_id=product.id,
            status=CompetitorItemMatchStatus.AMBIGUOUS,
            method=CompetitorItemMatchMethod.EMBEDDING_AUTO,
            final_score=0.8619,
        )
    )
    db_session.flush()

    assert _auto_reject_display_matrix_tag_conflicts(db_session) == 1

    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.status == CompetitorItemMatchStatus.REJECTED
    assert match.rationale_json["auto_reject_display_matrix_tag_conflict"]["reason"] == (
        "display_matrix_tag_conflict"
    )
    assert match.rationale_json["auto_reject_display_matrix_tag_conflict"][
        "competitor_matrix_tags"
    ] == ["DD"]
    assert match.rationale_json["auto_reject_display_matrix_tag_conflict"][
        "product_matrix_tags"
    ] == ["JCID"]


def test_display_subject_sweeper_rejects_display_against_non_display_product(db_session):
    product = Product(
        name="Антенный блок Nokia 6700 (со звонком) Taiwan с качелькой",
        brand="Nokia",
        article="023915",
        subject="антенный блок",
    )
    item = CompetitorItem(
        competitor="liberti",
        external_id="216328",
        name="LCD дисплей для Nokia 6700 Slide 1-я категория",
        item_type="display",
    )
    db_session.add_all([product, item])
    db_session.flush()
    db_session.add(
        CompetitorItemMatch(
            competitor_item_id=item.id,
            product_id=product.id,
            status=CompetitorItemMatchStatus.AMBIGUOUS,
            method=CompetitorItemMatchMethod.EMBEDDING_AUTO,
            final_score=0.5351,
        )
    )
    db_session.flush()

    assert (
        _explicit_display_subject_conflict_reason(item, product, item_type="display")
        == "display_candidate_vs_non_display_product"
    )
    assert _auto_reject_display_subject_conflicts(db_session) == 1

    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.status == CompetitorItemMatchStatus.REJECTED
    assert match.rationale_json["auto_reject_display_subject_conflict"]["reason"] == (
        "display_candidate_vs_non_display_product"
    )


def test_display_original_quality_sweeper_accepts_or_against_orig_exact_model(db_session):
    product = Product(
        name="Дисплей для Xiaomi Poco F5 Pro (23013PC75G) + тачскрин (черный) (ORIG)",
        brand="Xiaomi",
        article="059639",
        subject="дисплей",
    )
    item = CompetitorItem(
        competitor="liberti",
        external_id="451448",
        name="LCD дисплей для Xiaomi POCO F5 Pro с тачскрином (черный) 100% OR",
        normalized_title="LCD дисплей Xiaomi POCO F5 Pro тачскрин черный 100% OR",
        item_type="display",
    )
    db_session.add_all([product, item])
    db_session.flush()
    db_session.add(
        CompetitorItemMatch(
            competitor_item_id=item.id,
            product_id=product.id,
            status=CompetitorItemMatchStatus.NEEDS_REVIEW,
            method=CompetitorItemMatchMethod.EMBEDDING_AUTO,
            final_score=0.8528,
        )
    )
    db_session.flush()

    assert _auto_accept_display_original_quality_matches(db_session, min_score=0.80) == 1

    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.status == CompetitorItemMatchStatus.ACCEPTED
    assert match.rationale_json["auto_accept_display_original_quality"]["reason"] == (
        "display_original_quality_exact_model"
    )
    compatibility = db_session.execute(
        select(CompetitorItemCompatibility).where(
            CompetitorItemCompatibility.competitor_item_id == item.id
        )
    ).scalar_one()
    assert compatibility.source == "auto_model_key"
    assert compatibility.device_model == "poco_f5_pro"


def test_display_original_quality_sweeper_accepts_change_glass_perekleyka(db_session):
    product = Product(
        name="Дисплей для Apple iPhone 15 Pro + тачскрин (черный) (ORIG) (Переклейка)",
        brand="Apple",
        article="063266",
        subject="дисплей",
    )
    item = CompetitorItem(
        competitor="liberti",
        external_id="474552",
        name="LCD дисплей для Apple iPhone 15 Pro (черный) original (change glass) без ошибки + шлейф",
        normalized_title="LCD дисплей Apple iPhone 15 Pro черный original change glass без ошибки шлейф",
        item_type="display",
    )
    db_session.add_all([product, item])
    db_session.flush()
    db_session.add(
        CompetitorItemMatch(
            competitor_item_id=item.id,
            product_id=product.id,
            status=CompetitorItemMatchStatus.NEEDS_REVIEW,
            method=CompetitorItemMatchMethod.EMBEDDING_AUTO,
            final_score=0.8376,
        )
    )
    db_session.flush()

    assert _auto_accept_display_original_quality_matches(db_session, min_score=0.80) == 1

    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.status == CompetitorItemMatchStatus.ACCEPTED
    assert match.rationale_json["auto_accept_display_original_quality"]["reason"] == (
        "display_original_refurb_quality_exact_model"
    )


def test_display_frame_sweeper_rejects_explicit_frame_conflict(db_session):
    product = Product(
        name="Дисплей для Google Pixel 6 Pro + тачскрин (черный) (в рамке) (ORIG)",
        brand="Google",
        article="061160",
        subject="дисплей",
        display_has_frame=True,
    )
    item = CompetitorItem(
        competitor="moba",
        external_id="LCD-GGL-PXL-6-PR-CP-B-OR",
        name="Дисплей для Google Pixel 6 Pro в сборе с тачскрином Черный - (OR)",
        normalized_title="Дисплей Google Pixel 6 Pro тачскрин Черный OR",
        item_type="display",
    )
    db_session.add_all([product, item])
    db_session.flush()
    db_session.add(
        CompetitorItemMatch(
            competitor_item_id=item.id,
            product_id=product.id,
            status=CompetitorItemMatchStatus.NEEDS_REVIEW,
            method=CompetitorItemMatchMethod.EMBEDDING_AUTO,
            final_score=0.84,
        )
    )
    db_session.flush()

    assert _auto_reject_display_frame_conflicts(db_session) == 1

    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.status == CompetitorItemMatchStatus.REJECTED
    assert match.rationale_json["auto_reject_display_frame_conflict"]["reason"] == (
        "display_frame_conflict"
    )


def test_display_color_sweeper_rejects_beige_against_silver(db_session):
    product = Product(
        name="Дисплей для Samsung S918 Galaxy S23 Ultra + тачскрин (серебристый) (в рамке) (ORIG100) (SP)",
        brand="Samsung",
        article="079035",
        subject="дисплей",
        display_has_frame=True,
    )
    item = CompetitorItem(
        competitor="moba",
        external_id="LCD-SSG-S918B-FR-BG-OR-S",
        name="Дисплей для Samsung Galaxy S23 Ultra (S918B) модуль с рамкой Бежевый - Сервисный Оригинал",
        normalized_title="Дисплей Samsung Galaxy S23 Ultra S918B модуль с рамкой Бежевый Сервисный Оригинал",
        item_type="display",
    )
    db_session.add_all([product, item])
    db_session.flush()
    db_session.add(
        CompetitorItemMatch(
            competitor_item_id=item.id,
            product_id=product.id,
            status=CompetitorItemMatchStatus.AMBIGUOUS,
            method=CompetitorItemMatchMethod.EMBEDDING_AUTO,
            final_score=0.821,
        )
    )
    db_session.flush()

    assert _auto_reject_display_color_conflicts(db_session) == 1

    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.status == CompetitorItemMatchStatus.REJECTED
    assert match.rationale_json["auto_reject_display_color_conflict"]["reason"] == (
        "display_color_conflict"
    )
    assert match.rationale_json["auto_reject_display_color_conflict"]["competitor_color"] == (
        "beige"
    )


def test_part_color_sweeper_rejects_housing_color_conflict(db_session):
    product = Product(
        name="Задняя крышка для Apple iPhone 17 (белый) (Premium)",
        brand="Apple",
        article="HOU-W",
        subject="крышка",
        color="белый",
    )
    item = CompetitorItem(
        competitor="liberti",
        external_id="474247",
        name="Задняя крышка для iPhone 17 (синий) MagSafe",
        normalized_title="Задняя крышка iPhone 17 синий MagSafe",
        item_type="housing",
        color="белый",
    )
    db_session.add_all([product, item])
    db_session.flush()
    db_session.add(
        CompetitorItemMatch(
            competitor_item_id=item.id,
            product_id=product.id,
            status=CompetitorItemMatchStatus.AMBIGUOUS,
            method=CompetitorItemMatchMethod.EMBEDDING_AUTO,
            final_score=0.744,
        )
    )
    db_session.flush()

    assert _auto_reject_part_color_conflicts(db_session) == 1

    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.status == CompetitorItemMatchStatus.REJECTED
    details = match.rationale_json["auto_reject_part_color_conflict"]
    assert details["reason"] == "part_color_conflict"
    assert details["item_type"] == "housing"
    assert details["competitor_colors"] == ["blue"]
    assert details["product_colors"] == ["white"]


def test_part_color_sweeper_rejects_coral_against_gray(db_session):
    product = Product(
        name="Задняя крышка для Samsung S931 Galaxy S25 (коралловый)",
        brand="Samsung",
        article="HOU-CORAL",
        subject="крышка",
    )
    item = CompetitorItem(
        competitor="liberti",
        external_id="473165",
        name="Задняя крышка для Samsung Galaxy S25 SM-S931 (серый), премиум",
        normalized_title="Задняя крышка Samsung Galaxy S25 SM-S931 серый премиум",
        item_type="housing",
    )
    db_session.add_all([product, item])
    db_session.flush()
    db_session.add(
        CompetitorItemMatch(
            competitor_item_id=item.id,
            product_id=product.id,
            status=CompetitorItemMatchStatus.SUGGESTED,
            method=CompetitorItemMatchMethod.EMBEDDING_AUTO,
            final_score=0.79,
        )
    )
    db_session.flush()

    assert _auto_reject_part_color_conflicts(db_session) == 1

    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    details = match.rationale_json["auto_reject_part_color_conflict"]
    assert details["competitor_colors"] == ["gray"]
    assert details["product_colors"] == ["coral"]


def test_part_quality_sweeper_rejects_premium_against_orig(db_session):
    product = Product(
        name="Шлейф для Apple iPhone 17E с комп. + разъем зарядки (черный) (ORIG100)",
        brand="Apple",
        article="FPC-ORIG",
        subject="шлейф",
    )
    item = CompetitorItem(
        competitor="moba",
        external_id="FPC-PMI-17E-CC-B-OR",
        name="Шлейф для iPhone 17e на системный разъем/микрофон Черный - Премиум",
        normalized_title="Шлейф iPhone 17e системный разъем микрофон Черный Премиум",
        item_type="flex",
    )
    db_session.add_all([product, item])
    db_session.flush()
    db_session.add(
        CompetitorItemMatch(
            competitor_item_id=item.id,
            product_id=product.id,
            status=CompetitorItemMatchStatus.AMBIGUOUS,
            method=CompetitorItemMatchMethod.EMBEDDING_AUTO,
            final_score=0.74,
        )
    )
    db_session.flush()

    assert _auto_reject_part_quality_conflicts(db_session) == 1

    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.status == CompetitorItemMatchStatus.REJECTED
    details = match.rationale_json["auto_reject_part_quality_conflict"]
    assert details["reason"] == "part_quality_conflict"
    assert details["item_type"] == "flex"
    assert details["competitor_quality"] == "premium"
    assert details["product_quality"] == "original"


def test_part_assembly_sweeper_rejects_missing_camera_glass(db_session):
    product = Product(
        name="Задняя крышка для Xiaomi Poco X7 (24095PCADG) (черный)",
        brand="Xiaomi",
        article="HOU-X7",
        subject="крышка",
    )
    item = CompetitorItem(
        competitor="liberti",
        external_id="471581",
        name="Задняя крышка для Xiaomi Poco X7 (24095PCADG) со стеклом камеры (черный)",
        normalized_title="Задняя крышка Xiaomi Poco X7 24095PCADG со стеклом камеры черный",
        item_type="housing",
    )
    db_session.add_all([product, item])
    db_session.flush()
    db_session.add(
        CompetitorItemMatch(
            competitor_item_id=item.id,
            product_id=product.id,
            status=CompetitorItemMatchStatus.AMBIGUOUS,
            method=CompetitorItemMatchMethod.EMBEDDING_AUTO,
            final_score=0.76,
        )
    )
    db_session.flush()

    assert _auto_reject_part_assembly_conflicts(db_session) == 1

    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.status == CompetitorItemMatchStatus.REJECTED
    details = match.rationale_json["auto_reject_part_assembly_conflict"]
    assert details["reason"] == "part_camera_glass_missing_on_product"
    assert details["item_type"] == "housing"


def test_part_assembly_sweeper_rejects_extra_product_camera_glass(db_session):
    product = Product(
        name="Задняя крышка для Samsung S911 Galaxy S23 (зеленый) (в сборе со стеклом камеры)",
        brand="Samsung",
        article="S23-CAM-GLASS",
        subject="крышка",
    )
    item = CompetitorItem(
        competitor="liberti",
        external_id="473185",
        name="Задняя крышка для Samsung Galaxy S23 SM-S911 (зеленый), премиум",
        normalized_title="Задняя крышка Samsung Galaxy S23 SM-S911 зеленый премиум",
        item_type="housing",
    )
    db_session.add_all([product, item])
    db_session.flush()
    db_session.add(
        CompetitorItemMatch(
            competitor_item_id=item.id,
            product_id=product.id,
            status=CompetitorItemMatchStatus.AMBIGUOUS,
            method=CompetitorItemMatchMethod.EMBEDDING_AUTO,
            final_score=0.79,
        )
    )
    db_session.flush()

    assert _auto_reject_part_assembly_conflicts(db_session) == 1

    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.status == CompetitorItemMatchStatus.REJECTED
    details = match.rationale_json["auto_reject_part_assembly_conflict"]
    assert details["reason"] == "part_camera_glass_extra_on_product"
    assert details["item_type"] == "housing"


def test_part_assembly_sweeper_rejects_extra_product_flex_assembly(db_session):
    product = Product(
        name=(
            "Задняя крышка для Apple iPhone 17 Pro Max (SIM + eSIM) / "
            "iPhone 17 Pro Max (eSIM) (оранжевый) (в сборе со шлейфом) (Premium)"
        ),
        brand="Apple",
        article="17PM-FLEX",
        subject="крышка",
    )
    item = CompetitorItem(
        competitor="liberti",
        external_id="474239",
        name="Задняя крышка для iPhone 17 Pro Max (оранжевый) MagSafe",
        normalized_title="Задняя крышка iPhone 17 Pro Max оранжевый MagSafe",
        item_type="housing",
    )
    db_session.add_all([product, item])
    db_session.flush()
    db_session.add(
        CompetitorItemMatch(
            competitor_item_id=item.id,
            product_id=product.id,
            status=CompetitorItemMatchStatus.AMBIGUOUS,
            method=CompetitorItemMatchMethod.EMBEDDING_AUTO,
            final_score=0.77,
        )
    )
    db_session.flush()

    assert _auto_reject_part_assembly_conflicts(db_session) == 1

    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.status == CompetitorItemMatchStatus.REJECTED
    details = match.rationale_json["auto_reject_part_assembly_conflict"]
    assert details["reason"] == "part_flex_assembly_extra_on_product"
    assert details["item_type"] == "housing"


def test_part_assembly_sweeper_rejects_missing_product_flex_assembly_with_magsafe_wording(
    db_session,
):
    product = Product(
        name=(
            "Задняя крышка для Apple iPhone 17 (SIM + eSIM) / iPhone 17 (eSIM) "
            "(синий) (в сборе со стеклом камеры) (Premium)"
        ),
        brand="Apple",
        article="17-CAM-ONLY",
        subject="крышка",
    )
    item = CompetitorItem(
        competitor="liberti",
        external_id="474255",
        name="Задняя крышка для iPhone 17 (синий) в сборе со стеклом камеры и шлейфом MagSafe",
        normalized_title="Задняя крышка iPhone 17 синий в сборе со стеклом камеры и шлейфом MagSafe",
        item_type="housing",
    )
    db_session.add_all([product, item])
    db_session.flush()
    db_session.add(
        CompetitorItemMatch(
            competitor_item_id=item.id,
            product_id=product.id,
            status=CompetitorItemMatchStatus.AMBIGUOUS,
            method=CompetitorItemMatchMethod.EMBEDDING_AUTO,
            final_score=0.77,
        )
    )
    db_session.flush()

    assert _auto_reject_part_assembly_conflicts(db_session) == 1

    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.status == CompetitorItemMatchStatus.REJECTED
    details = match.rationale_json["auto_reject_part_assembly_conflict"]
    assert details["reason"] == "part_flex_assembly_missing_on_product"
    assert details["item_type"] == "housing"


def test_housing_device_code_sweeper_rejects_same_model_different_code(db_session):
    product = Product(
        name="Задняя крышка для Huawei Honor 400 Pro China (DNP-AN00) (черный)",
        brand="Huawei",
        article="HONOR-400-CHINA",
        subject="крышка",
    )
    item = CompetitorItem(
        competitor="moba",
        external_id="BTC-HUW-HNR-400-PR-B-OR",
        name="Задняя крышка для Huawei Honor 400 Pro (DNP-NX9) Черный - Премиум",
        normalized_title="Задняя крышка Huawei Honor 400 Pro DNP-NX9 Черный Премиум",
        item_type="housing",
    )
    db_session.add_all([product, item])
    db_session.flush()
    db_session.add(
        CompetitorItemMatch(
            competitor_item_id=item.id,
            product_id=product.id,
            status=CompetitorItemMatchStatus.SUGGESTED,
            method=CompetitorItemMatchMethod.EMBEDDING_AUTO,
            final_score=0.83,
        )
    )
    db_session.flush()

    assert _auto_reject_housing_device_code_conflicts(db_session) == 1

    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.status == CompetitorItemMatchStatus.REJECTED
    details = match.rationale_json["auto_reject_housing_device_code_conflict"]
    assert details["reason"] == "housing_device_code_conflict"
    assert details["competitor_codes"] == ["DNP-NX9"]
    assert details["product_codes"] == ["DNP-AN00"]


def test_housing_part_kind_sweeper_rejects_housing_against_sim_tray(db_session):
    product = Product(
        name="Держатель сим-карты для Apple iPhone 16 / iPhone 16 Plus (черный)",
        brand="Apple",
        article="SIM-16",
        subject="держатель сим-карты",
    )
    item = CompetitorItem(
        competitor="moba",
        external_id="HOU-PMI-16-PLS-B-OR",
        name="Корпус для iPhone 16 Plus (A3290) (1 Sim) Черный - Премиум",
        normalized_title="Корпус iPhone 16 Plus A3290 1 Sim Черный Премиум",
        item_type="housing",
    )
    db_session.add_all([product, item])
    db_session.flush()
    db_session.add(
        CompetitorItemMatch(
            competitor_item_id=item.id,
            product_id=product.id,
            status=CompetitorItemMatchStatus.AMBIGUOUS,
            method=CompetitorItemMatchMethod.EMBEDDING_AUTO,
            final_score=0.77,
        )
    )
    db_session.flush()

    assert _auto_reject_housing_part_kind_conflicts(db_session) == 1

    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.status == CompetitorItemMatchStatus.REJECTED
    details = match.rationale_json["auto_reject_housing_part_kind_conflict"]
    assert details["reason"] == "housing_part_kind_conflict"
    assert details["competitor_kind"] == "housing"
    assert details["product_kind"] == "sim_tray"


def test_camera_position_sweeper_rejects_front_against_main(db_session):
    product = Product(
        name="Камера передняя для Apple iPhone 16E, ориг",
        brand="Apple",
        article="CAM-FRONT",
        subject="камера",
    )
    item = CompetitorItem(
        competitor="liberti",
        external_id="467000",
        name="Камера основная Apple iPhone 16E, ориг",
        normalized_title="Камера основная Apple iPhone 16E ориг",
        item_type="camera",
    )
    db_session.add_all([product, item])
    db_session.flush()
    db_session.add(
        CompetitorItemMatch(
            competitor_item_id=item.id,
            product_id=product.id,
            status=CompetitorItemMatchStatus.AMBIGUOUS,
            method=CompetitorItemMatchMethod.EMBEDDING_AUTO,
            final_score=0.81,
        )
    )
    db_session.flush()

    assert _auto_reject_camera_position_conflicts(db_session) == 1

    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.status == CompetitorItemMatchStatus.REJECTED
    details = match.rationale_json["auto_reject_camera_position_conflict"]
    assert details["reason"] == "camera_position_conflict"
    assert details["competitor_position"] == "rear"
    assert details["product_position"] == "front"


def test_battery_part_code_sweeper_rejects_different_li_code(db_session):
    product = Product(
        name="Аккумулятор для ZTE Nubia Red Magic 11 Pro (Li3874T90Ph596788) (Premium)",
        brand="ZTE",
        article="077720",
        subject="аккумулятор",
    )
    item = CompetitorItem(
        competitor="moba",
        external_id="BTT-ZT-LI3934T90P8H623486",
        name="Аккумулятор для ZTE Nubia Red Magic 10 Pro (Li3934T90P8h623486)",
        normalized_title="Аккумулятор ZTE Nubia Red Magic 10 Pro Li3934T90P8h623486",
        item_type="battery",
    )
    db_session.add_all([product, item])
    db_session.flush()
    db_session.add(
        CompetitorItemMatch(
            competitor_item_id=item.id,
            product_id=product.id,
            status=CompetitorItemMatchStatus.AMBIGUOUS,
            method=CompetitorItemMatchMethod.EMBEDDING_AUTO,
            final_score=0.84,
        )
    )
    db_session.flush()

    assert _auto_reject_battery_part_code_conflicts(db_session) == 1

    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.status == CompetitorItemMatchStatus.REJECTED
    details = match.rationale_json["auto_reject_battery_part_code_conflict"]
    assert details["reason"] == "battery_part_code_conflict"
    assert details["competitor_codes"] == ["li3934t90p8h623486"]
    assert details["product_codes"] == ["li3874t90ph596788"]


def test_flex_role_sweeper_rejects_buttons_against_charge_mic(db_session):
    product = Product(
        name="Шлейф Xiaomi Mi 5 на разъем зарядки и микрофон (Черный)",
        brand="Xiaomi",
        article="FPC-CHARGE",
        subject="шлейф",
    )
    item = CompetitorItem(
        competitor="liberti",
        external_id="247843",
        name="Шлейф/FLC Xiaomi Mi 5 на кнопки громкости/включения",
        normalized_title="Шлейф FLC Xiaomi Mi 5 на кнопки громкости включения",
        item_type="flex",
    )
    db_session.add_all([product, item])
    db_session.flush()
    db_session.add(
        CompetitorItemMatch(
            competitor_item_id=item.id,
            product_id=product.id,
            status=CompetitorItemMatchStatus.AMBIGUOUS,
            method=CompetitorItemMatchMethod.EMBEDDING_AUTO,
            final_score=0.79,
        )
    )
    db_session.flush()

    assert _auto_reject_flex_role_conflicts(db_session) == 1

    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.status == CompetitorItemMatchStatus.REJECTED
    details = match.rationale_json["auto_reject_flex_role_conflict"]
    assert details["reason"] == "flex_role_conflict"
    assert details["competitor_role"] == "buttons"
    assert details["product_role"] == "charge_mic"


def test_flex_role_sweeper_rejects_charge_mic_against_sensor(db_session):
    product = Product(
        name="Шлейф для Xiaomi 14 Ultra (24030PN60G) с комп. + сенсор",
        brand="Xiaomi",
        article="FPC-SENSOR",
        subject="шлейф",
    )
    item = CompetitorItem(
        competitor="liberti",
        external_id="458849",
        name="Шлейф/FLC Xiaomi 14 Ultra на системный разъём/микрофон",
        normalized_title="Шлейф FLC Xiaomi 14 Ultra на системный разъем микрофон",
        item_type="flex",
    )
    db_session.add_all([product, item])
    db_session.flush()
    db_session.add(
        CompetitorItemMatch(
            competitor_item_id=item.id,
            product_id=product.id,
            status=CompetitorItemMatchStatus.AMBIGUOUS,
            method=CompetitorItemMatchMethod.EMBEDDING_AUTO,
            final_score=0.79,
        )
    )
    db_session.flush()

    assert _auto_reject_flex_role_conflicts(db_session) == 1

    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.status == CompetitorItemMatchStatus.REJECTED
    details = match.rationale_json["auto_reject_flex_role_conflict"]
    assert details["reason"] == "flex_role_conflict"
    assert details["competitor_role"] == "charge_mic"
    assert details["product_role"] == "sensor"


def test_display_original_quality_sweeper_accepts_ambiguous_service_original(db_session):
    product = Product(
        name="Дисплей для Samsung S918 Galaxy S23 Ultra + тачскрин (зеленый) (в рамке) (ORIG100) (SP)",
        brand="Samsung",
        article="079038",
        subject="дисплей",
        display_has_frame=True,
    )
    item = CompetitorItem(
        competitor="moba",
        external_id="LCD-SSG-S918B-FR-GN-OR-S",
        name="Дисплей для Samsung Galaxy S23 Ultra (S918B) модуль с рамкой Зеленый - Сервисный Оригинал",
        normalized_title="Дисплей Samsung Galaxy S23 Ultra S918B модуль с рамкой Зеленый Сервисный Оригинал",
        item_type="display",
    )
    db_session.add_all([product, item])
    db_session.flush()
    db_session.add(
        CompetitorItemMatch(
            competitor_item_id=item.id,
            product_id=product.id,
            status=CompetitorItemMatchStatus.AMBIGUOUS,
            method=CompetitorItemMatchMethod.EMBEDDING_AUTO,
            final_score=0.8484,
        )
    )
    db_session.flush()

    assert _auto_accept_display_original_quality_matches(db_session, min_score=0.80) == 1

    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.status == CompetitorItemMatchStatus.ACCEPTED
    assert match.rationale_json["auto_accept_display_original_quality"]["reason"] == (
        "display_original_quality_exact_model"
    )


def test_display_text_model_sweeper_rejects_pro_xl_against_pro(db_session):
    product = Product(
        name="Дисплей для Google Pixel 9 Pro (GR83Y/GEC77/GWVK6) + тачскрин (черный) (ORIG)",
        brand="Google",
        article="066262",
        subject="дисплей",
    )
    item = CompetitorItem(
        competitor="moba",
        external_id="LCD-GGL-PXL-9-PR-XL-CP-B-OR",
        name="Дисплей для Google Pixel 9 Pro XL (GGX8B) в сборе с тачскрином Черный - OR",
        normalized_title="Дисплей Google Pixel 9 Pro XL GGX8B тачскрин Черный OR",
        item_type="display",
    )
    db_session.add_all([product, item])
    db_session.flush()
    db_session.add(
        CompetitorItemMatch(
            competitor_item_id=item.id,
            product_id=product.id,
            status=CompetitorItemMatchStatus.SUGGESTED,
            method=CompetitorItemMatchMethod.EMBEDDING_AUTO,
            final_score=0.8494,
        )
    )
    db_session.flush()

    assert _auto_reject_display_text_model_conflicts(db_session) == 1

    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.status == CompetitorItemMatchStatus.REJECTED
    assert match.rationale_json["auto_reject_display_text_model_conflict"]["reason"] == (
        "display_text_model_conflict"
    )


def test_display_text_model_sweeper_rejects_tecno_pro_plus_against_pro(db_session):
    product = Product(
        name="Дисплей для Tecno Spark 20 Pro (KJ6) + тачскрин (черный) (в рамке) (ORIG)",
        brand="Tecno",
        article="071335",
        subject="дисплей",
    )
    item = CompetitorItem(
        competitor="moba",
        external_id="LCD-TCN-SPR-20-PR-PLS-FR-B-OR",
        name="Дисплей для Tecno Spark 20 Pro+ (KJ7) модуль с рамкой Черный - OR",
        normalized_title="Дисплей Tecno Spark 20 Pro+ KJ7 модуль с рамкой Черный OR",
        item_type="display",
    )
    db_session.add_all([product, item])
    db_session.flush()
    db_session.add(
        CompetitorItemMatch(
            competitor_item_id=item.id,
            product_id=product.id,
            status=CompetitorItemMatchStatus.SUGGESTED,
            method=CompetitorItemMatchMethod.EMBEDDING_AUTO,
            final_score=0.8639,
        )
    )
    db_session.flush()

    assert _auto_reject_display_text_model_conflicts(db_session) == 1

    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.status == CompetitorItemMatchStatus.REJECTED
    assert match.rationale_json["auto_reject_display_text_model_conflict"]["reason"] == (
        "display_text_model_conflict"
    )


def test_display_text_model_sweeper_rejects_tecno_camon_19_against_neo(db_session):
    product = Product(
        name="Дисплей для Tecno Camon 19 Neo (CH6i) / Camon 17P (CG7N) + тачскрин (черный) (ORIG)",
        brand="Tecno",
        article="064211",
        subject="дисплей",
    )
    item = CompetitorItem(
        competitor="moba",
        external_id="LCD-TCN-CMN-19-CP-B-OR",
        name="Дисплей для Tecno Camon 19 (CI6n) в сборе с тачскрином Черный - OR",
        normalized_title="Дисплей Tecno Camon 19 CI6n тачскрин Черный OR",
        item_type="display",
    )
    db_session.add_all([product, item])
    db_session.flush()
    db_session.add(
        CompetitorItemMatch(
            competitor_item_id=item.id,
            product_id=product.id,
            status=CompetitorItemMatchStatus.SUGGESTED,
            method=CompetitorItemMatchMethod.EMBEDDING_AUTO,
            final_score=0.8317,
        )
    )
    db_session.flush()

    assert _auto_reject_display_text_model_conflicts(db_session) == 1

    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.status == CompetitorItemMatchStatus.REJECTED
    assert match.rationale_json["auto_reject_display_text_model_conflict"]["reason"] == (
        "display_text_model_conflict"
    )


def test_display_long_model_code_sweeper_rejects_xiaomi_pad_6s_against_pad_6(
    db_session,
):
    product = Product(
        name="Дисплей для Xiaomi Pad 6 (23043RP34G) / Pad 6 Pro (23046RP50C) + тачскрин (черный)",
        brand="Xiaomi",
        article="061386",
        subject="дисплей",
    )
    item = CompetitorItem(
        competitor="liberti",
        external_id="468534",
        name='LCD дисплей для Xiaomi Pad 6S Pro 12.4" (24018RPACG) в сборе с тачскрином (черный)',
        normalized_title='LCD дисплей Xiaomi Pad 6S Pro 12.4" 24018RPACG тачскрин черный',
        item_type="display",
    )
    db_session.add_all([product, item])
    db_session.flush()
    db_session.add(
        CompetitorItemMatch(
            competitor_item_id=item.id,
            product_id=product.id,
            status=CompetitorItemMatchStatus.NEEDS_REVIEW,
            method=CompetitorItemMatchMethod.EMBEDDING_AUTO,
            final_score=0.8453,
        )
    )
    db_session.flush()

    assert _auto_reject_display_long_model_code_conflicts(db_session) == 1

    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.status == CompetitorItemMatchStatus.REJECTED
    assert match.rationale_json["auto_reject_display_long_model_code_conflict"]["reason"] == (
        "display_long_model_code_conflict"
    )
    assert match.rationale_json["auto_reject_display_long_model_code_conflict"][
        "competitor_codes"
    ] == ["24018RPACG"]


def test_display_module_component_sweeper_rejects_touchscreen_against_display_module(
    db_session,
):
    product = Product(
        name="Дисплей совместим с iPad Pro 12.9 / A1584 / A1652 (2015) с тачскрином (Черный) Оригинал новый",
        brand="Apple",
        article="046052",
        subject="дисплей",
    )
    item = CompetitorItem(
        competitor="moba",
        external_id="TSN-PDIP129-2015-B-OR-FGT",
        name='Тачскрин для iPad Pro 12.9" 2015 (A1584/A1652) Черный - OR (Feaglet)',
        normalized_title='Тачскрин iPad Pro 12.9" 2015 A1584 A1652 Черный OR Feaglet',
        item_type="display",
    )
    db_session.add_all([product, item])
    db_session.flush()
    db_session.add(
        CompetitorItemMatch(
            competitor_item_id=item.id,
            product_id=product.id,
            status=CompetitorItemMatchStatus.SUGGESTED,
            method=CompetitorItemMatchMethod.EMBEDDING_AUTO,
            final_score=0.785,
        )
    )
    db_session.flush()

    assert _auto_reject_display_module_component_conflicts(db_session) == 1

    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.status == CompetitorItemMatchStatus.REJECTED
    assert match.rationale_json["auto_reject_display_module_component_conflict"]["reason"] == (
        "display_module_component_conflict"
    )


def test_laptop_matrix_flex_sweeper_rejects_against_console_display_flex(db_session):
    product = Product(
        name="Шлейф для Sony PSP GO (на дисплей)",
        brand="Sony",
        article="079396",
        subject="шлейф",
    )
    item = CompetitorItem(
        competitor="moba",
        external_id="FPC-MTX-LP-SNY-VPCEE-LCD",
        name="Шлейф матрицы для ноутбука Sony Vaio VPC-EE (LCD)",
        normalized_title="Шлейф матрицы ноутбука Sony Vaio VPC-EE LCD",
        item_type="flex",
    )
    db_session.add_all([product, item])
    db_session.flush()
    db_session.add(
        CompetitorItemMatch(
            competitor_item_id=item.id,
            product_id=product.id,
            status=CompetitorItemMatchStatus.AMBIGUOUS,
            method=CompetitorItemMatchMethod.EMBEDDING_AUTO,
            final_score=0.5429,
        )
    )
    db_session.flush()

    assert _auto_reject_laptop_matrix_flex_conflicts(db_session) == 1

    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.status == CompetitorItemMatchStatus.REJECTED
    assert match.rationale_json["auto_reject_laptop_matrix_flex_conflict"]["reason"] == (
        "laptop_matrix_flex_vs_other_product"
    )


def test_non_display_model_code_sweeper_rejects_powerbank_model_conflict(db_session):
    product = Product(
        name="Внешний накопитель Hoco J100A 20000 mAh (черный)",
        brand="Hoco",
        article="075469",
        subject="внешний накопитель",
    )
    item = CompetitorItem(
        competitor="liberti",
        external_id="472447",
        name=(
            "Внешний АКБ HOCO J150 Stream 20000 mAh, 1xUSB, 1xUSB-C, 3А, "
            "PD20W, 22.5W, LED дисплей, фонарь, Li-Pol (черный)"
        ),
        normalized_title="Внешний АКБ HOCO J150 Stream 20000 mAh LED дисплей черный",
        item_type="display",
    )
    db_session.add_all([product, item])
    db_session.flush()
    db_session.add(
        CompetitorItemMatch(
            competitor_item_id=item.id,
            product_id=product.id,
            status=CompetitorItemMatchStatus.AMBIGUOUS,
            method=CompetitorItemMatchMethod.EMBEDDING_AUTO,
            final_score=0.7319,
        )
    )
    db_session.flush()

    assert _auto_reject_non_display_model_code_conflicts(db_session) == 1

    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.status == CompetitorItemMatchStatus.REJECTED
    assert match.rationale_json["auto_reject_non_display_model_code_conflict"]["reason"] == (
        "non_display_model_code_conflict"
    )
    assert match.rationale_json["auto_reject_non_display_model_code_conflict"][
        "competitor_codes"
    ] == ["J150"]


def test_non_display_model_code_sweeper_rejects_trackpad_model_conflict(db_session):
    product = Product(
        name="Тачпад для Apple MacBook Pro 13 Retina A1502 (LATE 2013 - MID 2014) (серебристый)",
        brand="Apple",
        article="065432",
        subject="тачпад",
    )
    item = CompetitorItem(
        competitor="moba",
        external_id="TPD-MB-PR-15-A1398-2015-SL",
        name='Трекпад (тачпад) для MacBook Pro 15" A1398 (2015) Серебро',
        normalized_title='Трекпад тачпад MacBook Pro 15" A1398 2015 Серебро',
        item_type="other",
    )
    db_session.add_all([product, item])
    db_session.flush()
    db_session.add(
        CompetitorItemMatch(
            competitor_item_id=item.id,
            product_id=product.id,
            status=CompetitorItemMatchStatus.AMBIGUOUS,
            method=CompetitorItemMatchMethod.EMBEDDING_AUTO,
            final_score=0.6782,
        )
    )
    db_session.flush()

    assert _auto_reject_non_display_model_code_conflicts(db_session) == 1

    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.status == CompetitorItemMatchStatus.REJECTED
    assert match.rationale_json["auto_reject_non_display_model_code_conflict"]["product_codes"] == [
        "A1502"
    ]


def test_non_display_model_code_sweeper_ignores_quality_code_without_product_model_code(
    db_session,
):
    product = Product(
        name="Шлейф для Apple iPhone 16 с комп. + сенсор (ORIG100)",
        brand="Apple",
        article="065999",
        subject="шлейф",
    )
    item = CompetitorItem(
        competitor="moba",
        external_id="FPC-APL-IP16-SNS-PRM",
        name="Шлейф для iPhone 16 (A3287) на сенсор - Премиум",
        normalized_title="Шлейф iPhone 16 A3287 сенсор Премиум",
        item_type="flex",
    )
    db_session.add_all([product, item])
    db_session.flush()
    db_session.add(
        CompetitorItemMatch(
            competitor_item_id=item.id,
            product_id=product.id,
            status=CompetitorItemMatchStatus.AMBIGUOUS,
            method=CompetitorItemMatchMethod.EMBEDDING_AUTO,
            final_score=0.8124,
        )
    )
    db_session.flush()

    assert _auto_reject_non_display_model_code_conflicts(db_session) == 0

    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.status == CompetitorItemMatchStatus.AMBIGUOUS


def test_non_display_model_code_sweeper_keeps_same_text_model_regional_codes_for_review(
    db_session,
):
    product = Product(
        name="Шлейф для Huawei Honor 200 (ELI-NX9) системный",
        brand="Huawei",
        article="066123",
        subject="шлейф",
    )
    item = CompetitorItem(
        competitor="moba",
        external_id="FPC-HNR-200-ELI-AN00-SUB",
        name="Шлейф для Huawei Honor 200 (ELI-AN00) системный",
        normalized_title="Шлейф Huawei Honor 200 ELI-AN00 системный",
        item_type="flex",
    )
    db_session.add_all([product, item])
    db_session.flush()
    db_session.add(
        CompetitorItemMatch(
            competitor_item_id=item.id,
            product_id=product.id,
            status=CompetitorItemMatchStatus.NEEDS_REVIEW,
            method=CompetitorItemMatchMethod.EMBEDDING_AUTO,
            final_score=0.7018,
        )
    )
    db_session.flush()

    assert _auto_reject_non_display_model_code_conflicts(db_session) == 0

    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.status == CompetitorItemMatchStatus.NEEDS_REVIEW


def test_display_construction_sweeper_accepts_generic_incell_copy_exact_model(db_session):
    product = Product(
        name=("Дисплей для Apple iPhone 16 Pro Max + тачскрин (черный) " "(MNK) (In-Cell)"),
        brand="Apple",
        article="065280",
        subject="дисплей",
        display_quality="Copy Low",
    )
    item = CompetitorItem(
        competitor="moba",
        external_id="LCD-APL-IP16PM-CP-B-INCELL",
        name=(
            "Дисплей для iPhone 16 Pro Max (A3296) в сборе с тачскрином "
            "Черный - (In-Cell, Full HD, 120 Гц)"
        ),
        normalized_title=("Дисплей iPhone 16 Pro Max A3296 тачскрин Черный In-Cell Full HD 120 Гц"),
        item_type="display",
    )
    db_session.add_all([product, item])
    db_session.flush()
    db_session.add(
        CompetitorItemMatch(
            competitor_item_id=item.id,
            product_id=product.id,
            status=CompetitorItemMatchStatus.AMBIGUOUS,
            method=CompetitorItemMatchMethod.EMBEDDING_AUTO,
            final_score=0.8582,
        )
    )
    db_session.flush()

    assert _auto_accept_display_construction_matches(db_session, min_score=0.80) == 1

    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.status == CompetitorItemMatchStatus.ACCEPTED
    assert match.rationale_json["auto_accept_display_construction"]["reason"] == (
        "display_copy_construction_exact_model"
    )
    compatibility = db_session.execute(
        select(CompetitorItemCompatibility).where(
            CompetitorItemCompatibility.competitor_item_id == item.id
        )
    ).scalar_one()
    assert compatibility.source == "auto_model_key"
    assert compatibility.device_model == "iphone_16_pro_max"


def test_display_matrix_tag_sweeper_accepts_same_jcid_exact_model(db_session):
    product = Product(
        name=(
            "Дисплей для Apple iPhone 14 Pro + тачскрин (черный) "
            "(JCID) (Soft Oled) (SYSTEM DIAGNOSABLE)"
        ),
        brand="Apple",
        article="065146",
        subject="дисплей",
        display_quality="Copy High",
    )
    item = CompetitorItem(
        competitor="liberti",
        external_id="489245",
        name=(
            "LCD дисплей для Apple iPhone 14 Pro (черный) "
            "SOFT OLED JCID (привязка без пайки) 120Hz"
        ),
        normalized_title=(
            "LCD дисплей Apple iPhone 14 Pro черный SOFT OLED JCID привязка без пайки 120Hz"
        ),
        item_type="display",
    )
    db_session.add_all([product, item])
    db_session.flush()
    db_session.add(
        CompetitorItemMatch(
            competitor_item_id=item.id,
            product_id=product.id,
            status=CompetitorItemMatchStatus.NEEDS_REVIEW,
            method=CompetitorItemMatchMethod.EMBEDDING_AUTO,
            final_score=0.873,
        )
    )
    db_session.flush()

    assert _auto_accept_display_matrix_tag_matches(db_session, min_score=0.80) == 1

    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.status == CompetitorItemMatchStatus.ACCEPTED
    assert match.rationale_json["auto_accept_display_matrix_tag"]["reason"] == (
        "display_same_matrix_tag_exact_model"
    )
    assert match.rationale_json["auto_accept_display_matrix_tag"]["overlap_matrix_tags"] == ["JCID"]


def test_display_matrix_type_sweeper_accepts_copy_oled_exact_model(db_session):
    product = Product(
        name="Дисплей для OnePlus 5 + тачскрин (черный) (OLED)",
        brand="OnePlus",
        article="039882",
        subject="дисплей",
        display_quality="Copy High",
    )
    item = CompetitorItem(
        competitor="moba",
        external_id="LCD-OPL-5-CP-B-LED",
        name="Дисплей для OnePlus 5 (A5000) в сборе с тачскрином Черный - (OLED)",
        normalized_title="Дисплей OnePlus 5 A5000 тачскрин Черный OLED",
        item_type="display",
    )
    db_session.add_all([product, item])
    db_session.flush()
    db_session.add(
        CompetitorItemMatch(
            competitor_item_id=item.id,
            product_id=product.id,
            status=CompetitorItemMatchStatus.NEEDS_REVIEW,
            method=CompetitorItemMatchMethod.EMBEDDING_AUTO,
            final_score=0.8276,
        )
    )
    db_session.flush()

    assert _auto_accept_display_matrix_type_matches(db_session, min_score=0.80) == 1

    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.status == CompetitorItemMatchStatus.ACCEPTED
    assert match.rationale_json["auto_accept_display_matrix_type"]["reason"] == (
        "display_copy_matrix_type_exact_model"
    )
    assert match.rationale_json["auto_accept_display_matrix_type"]["product_display_type"] == (
        "OLED"
    )


def test_display_matrix_type_sweeper_keeps_small_size_for_review(db_session):
    product = Product(
        name="Дисплей для OnePlus 7T + тачскрин (черный) (OLED) (Small Size)",
        brand="OnePlus",
        article="049461",
        subject="дисплей",
        display_quality="Copy High",
    )
    item = CompetitorItem(
        competitor="moba",
        external_id="LCD-OPL-7T-CP-B-LED",
        name="Дисплей для OnePlus 7T (HD1900) в сборе с тачскрином Черный - (OLED)",
        normalized_title="Дисплей OnePlus 7T HD1900 тачскрин Черный OLED",
        item_type="display",
    )
    db_session.add_all([product, item])
    db_session.flush()
    db_session.add(
        CompetitorItemMatch(
            competitor_item_id=item.id,
            product_id=product.id,
            status=CompetitorItemMatchStatus.NEEDS_REVIEW,
            method=CompetitorItemMatchMethod.EMBEDDING_AUTO,
            final_score=0.8445,
        )
    )
    db_session.flush()

    assert _auto_accept_display_matrix_type_matches(db_session, min_score=0.80) == 0

    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.status == CompetitorItemMatchStatus.NEEDS_REVIEW


def test_match_items_reclassifies_standalone_touchscreen_as_other(db_session, tmp_path):
    product = Product(
        name="Тачскрин для Huawei Honor 8A / 8A Pro черный",
        brand="Huawei",
        category="touchscreen",
        article="C6D",
    )
    display_product = Product(
        name="Дисплей для Huawei Honor 8A в сборе с тачскрином черный",
        brand="Huawei",
        category="display",
        article="C6E",
    )
    db_session.add_all([product, display_product])
    db_session.flush()

    item = CompetitorItem(
        competitor="liberti",
        external_id="SKU-3F4",
        name="Тачскрин для Huawei Honor 8A / 8A Pro (черный)",
        normalized_title="Тачскрин для Huawei Honor 8A / 8A Pro черный",
        item_type="display",
        parsed_device_brand="huawei",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0], [0.99, 0.01]], dtype=np.float32)
    product_matrix = product_matrix / np.linalg.norm(product_matrix, axis=1, keepdims=True)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id, display_product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.001,
        top_k=2,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["matched"] == 1
    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.product_id == product.id


def test_match_items_reclassifies_fpc_display_part_as_flex(db_session, tmp_path):
    product = Product(
        name="Шлейф для Samsung S916 Galaxy S23+ с комп. на дисплей",
        brand="Samsung",
        category="flex",
        article="C6F",
    )
    display_product = Product(
        name="Дисплей для Samsung S916 Galaxy S23+ черный",
        brand="Samsung",
        category="display",
        article="C6G",
    )
    db_session.add_all([product, display_product])
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="SKU-3F5",
        name="Шлейф для Samsung Galaxy S23+ (S916B) на дисплей",
        normalized_title="Шлейф для Samsung Galaxy S23+ S916B на дисплей",
        item_type="display",
        parsed_device_brand="samsung",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0], [0.99, 0.01]], dtype=np.float32)
    product_matrix = product_matrix / np.linalg.norm(product_matrix, axis=1, keepdims=True)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id, display_product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.001,
        top_k=2,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["matched"] == 1
    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.product_id == product.id


def test_match_items_reclassifies_misclassified_battery_item(db_session, tmp_path):
    product = Product(
        name="Аккумуляторы для Philips",
        brand="Philips",
        category="battery",
        article="C7",
    )
    display_product = Product(
        name="Дисплей для Philips E570 черный",
        brand="Philips",
        category="display",
        article="C7D",
    )
    db_session.add_all([product, display_product])
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="SKU-3G",
        name="Аккумулятор для Philips E570 (AB3160AWMT)",
        normalized_title="Аккумулятор для Philips E570",
        item_type="display",
        parsed_device_brand="philips",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0], [0.99, 0.01]], dtype=np.float32)
    product_matrix = product_matrix / np.linalg.norm(product_matrix, axis=1, keepdims=True)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id, display_product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.001,
        top_k=2,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["matched"] == 1
    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.product_id == product.id


def test_match_items_reclassifies_display_repair_tool_as_other(db_session, tmp_path):
    product = Product(
        name="Струна для разделения дисплейных модулей 0.06 мм 100m",
        category="tool",
        article="C8",
    )
    display_product = Product(
        name="Дисплей iPhone 12 черный",
        category="display",
        article="C8D",
    )
    db_session.add_all([product, display_product])
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="SKU-3H",
        name="Струна для разделения дисплейных модулей Kaisi (0,04 мм, 100 м)",
        normalized_title="Струна для разделения дисплейных модулей Kaisi 0.04 мм 100 м",
        item_type="display",
        category="инструмент",
        category_group="инструменты",
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0], [0.99, 0.01]], dtype=np.float32)
    product_matrix = product_matrix / np.linalg.norm(product_matrix, axis=1, keepdims=True)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id, display_product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.001,
        top_k=2,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["matched"] == 1
    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.product_id == product.id


def test_match_items_unknown_display_frame_needs_review(db_session, tmp_path):
    product = Product(
        name="Дисплей iPhone 12 в рамке",
        brand="Apple",
        category="display",
        article="D1",
        display_has_frame=True,
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="SKU-4",
        name="Дисплей iPhone 12",
        normalized_title="Дисплей iPhone 12",
        item_type="display",
        parsed_device_brand="apple",
        has_frame=None,
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["needs_review"] == 1
    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.status == CompetitorItemMatchStatus.NEEDS_REVIEW
    assert match.rationale_json["display_frame_review"]["reason"] == "display_frame_unknown_side"


def test_match_items_product_display_conflict_needs_review(db_session, tmp_path):
    product = Product(
        name="Дисплей iPhone 12",
        brand="Apple",
        category="display",
        article="E1",
        display_has_frame=None,
        display_modification_status="conflict",
    )
    db_session.add(product)
    db_session.flush()

    item = CompetitorItem(
        competitor="moba",
        external_id="SKU-5",
        name="Дисплей iPhone 12 в рамке",
        normalized_title="Дисплей iPhone 12 в рамке",
        item_type="display",
        parsed_device_brand="apple",
        has_frame=True,
    )
    db_session.add(item)
    db_session.flush()

    product_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    competitor_matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    _write_embeddings(tmp_path, "our_catalog", product_matrix, [product.id])
    _write_embeddings(tmp_path, "competitor_items", competitor_matrix, [item.id])

    stats = match_items(
        db_session,
        embeddings_dir=tmp_path,
        min_embed_score=0.1,
        min_gap=0.01,
        top_k=1,
        top_k_llm=1,
        use_llm_arbiter=False,
        limit=None,
        only_null=True,
        include_status=None,
        force=False,
        dry_run=False,
        sample_limit=0,
        samples_file=None,
        report_file=None,
        report_limit=0,
        report_csv_file=None,
    )

    assert stats["needs_review"] == 1
    match = db_session.execute(
        select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
    ).scalar_one()
    assert match.status == CompetitorItemMatchStatus.NEEDS_REVIEW
    assert (
        match.rationale_json["display_frame_review"]["reason"]
        == "product_display_modification_conflict"
    )
