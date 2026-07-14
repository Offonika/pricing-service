from __future__ import annotations

import httpx

from app.models import CompetitorItem
from app.services.competitor_url_aliases import (
    normalize_competitor_url,
    parse_competitor_url,
    resolve_poiskzip_redirect_url,
    upsert_competitor_item_url_alias,
)
from tasks.match_competitor_items import _extract_device_codes
from tasks.normalize_competitor_item_type import rule_classify


def test_moba_url_normalization_and_parts() -> None:
    parts = parse_competitor_url("https://moba.ru/catalog/displei/101185/?utm_referrer=poiskzip.ru")
    assert parts is not None
    assert parts.normalized_url == "https://moba.ru/catalog/displei/101185"
    assert parts.catalog_id == "101185"
    assert normalize_competitor_url("moba.ru/catalog/displei/101185/") == (
        "https://moba.ru/catalog/displei/101185"
    )


def test_poiskzip_redirect_can_be_resolved_from_location() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={
                "location": "https://moba.ru/catalog/displei/101185/?utm_referrer=poiskzip.ru"
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
        assert (
            resolve_poiskzip_redirect_url(
                "https://poiskzip.ru/redirect/1134761",
                client=client,
            )
            == "https://moba.ru/catalog/displei/101185/?utm_referrer=poiskzip.ru"
        )


def test_upsert_competitor_item_url_alias(db_session) -> None:
    item = CompetitorItem(
        competitor="moba",
        external_id="LCD-OPP-A78-4G-CP-B-INCL",
        name="Дисплей для OPPO A78 4G",
        url="https://poiskzip.ru/redirect/1134761",
        availability=True,
    )
    db_session.add(item)
    db_session.flush()

    alias = upsert_competitor_item_url_alias(
        db_session,
        item,
        "https://moba.ru/catalog/displei/101185/?utm_referrer=poiskzip.ru",
        url_kind="resolved",
        resolved_from_url=item.url,
    )
    db_session.commit()

    assert alias is not None
    assert alias.competitor_item_id == item.id
    assert alias.normalized_url == "https://moba.ru/catalog/displei/101185"
    assert alias.catalog_id == "101185"


def test_rule_classify_marks_accessory_noise_as_other() -> None:
    assert (
        rule_classify(
            "Защитное стекло линзы камеры для iPhone 12",
            sku="TP-MTL-EYS-CAM-PMI120-B",
            category="защитное стекло",
        )
        == "other"
    )
    assert (
        rule_classify(
            "Стекло для переклейки Samsung Galaxy A31 Черный",
            sku="GLS-SSG-A315F-B",
            category="стекло для переклейки",
        )
        == "other"
    )


def test_rule_classify_marks_microchips_as_other_before_display_tokens() -> None:
    assert (
        rule_classify(
            "Микросхема iPhone 339S0171 (Wi-Fi модуль iPhone 5)",
            category="Дисплеи для телефонов",
        )
        == "other"
    )


def test_rule_classify_marks_charger_display_feature_as_other() -> None:
    assert rule_classify("АЗУ HOCO Z3 2xUSB, 3.1А, LED дисплей (черный)") == "other"
    assert (
        rule_classify("Зарядная станция Mechanic iCharge 6M (40W, 5USB/USB-QC3.0, LCD)") == "other"
    )


def test_rule_classify_prefers_back_cover_over_bundle_flex() -> None:
    assert (
        rule_classify("Задняя крышка для iPhone 17 Pro Max (синий) со шлейфом, MagSafe")
        == "housing"
    )


def test_rule_classify_keeps_real_display_with_ic_pad_as_display() -> None:
    assert rule_classify("Дисплей площадка под IC iPhone 12", sku="LCD-PMI-12") == "display"


def test_device_code_extraction_covers_short_and_long_codes() -> None:
    text = "Tecno Spark 7 (KF6N) / Infinix Hot 10S (X689D) / Redmi 7A (M1903C3EE)"
    assert _extract_device_codes(text) == ["X689D", "KF6N", "M1903C3EE"]
