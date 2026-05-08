from __future__ import annotations

import json
from datetime import date
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
from app.services.matching_guardrails import basic_candidate_guardrails
from tasks.match_competitor_items_embeddings import (
    _competitor_display_has_frame,
    _competitor_display_mapped_1c_quality_raw,
    _competitor_display_quality,
    _competitor_display_quality_raw,
    _effective_item_type,
    _extract_device_model_keys,
    _product_display_quality,
    match_items,
)


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
    assert "google_pixel_4_xl" in _extract_device_model_keys("Google Pixel 4 XL")
    assert "huawei_mate_10" in _extract_device_model_keys("Huawei Mate 10")
    assert "huawei_p_smart_2019" in _extract_device_model_keys("Huawei P Smart 2019")
    assert "nokia_5_3" in _extract_device_model_keys("Nokia 5.3")
    assert "nokia_6230" in _extract_device_model_keys("Nokia 6230")
    assert "lg_kg800" in _extract_device_model_keys("LG KG800")
    assert "nintendo_3ds_xl" in _extract_device_model_keys("Nintendo 3DS XL")
    assert "oneplus_x" in _extract_device_model_keys("OnePlus X")
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
    assert match.status == CompetitorItemMatchStatus.SUGGESTED


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
    assert match.status == CompetitorItemMatchStatus.SUGGESTED
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
    assert match.status == CompetitorItemMatchStatus.SUGGESTED
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
