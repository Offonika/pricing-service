from __future__ import annotations

from app.models import CompetitorItem, Product
from app.services.matching_battery import (
    battery_capacity_conflict,
    battery_pair_diagnostic_reasons,
    battery_part_code_conflict,
    battery_premium_tier_conflict,
    competitor_battery_part_codes,
)


def test_battery_diagnostics_detect_part_code_and_capacity_conflicts() -> None:
    item = CompetitorItem(
        competitor="moba",
        external_id="BTT-ZTE-A71",
        name="Аккумулятор для ZTE Blade A71 (Li3839T44P8h866445) 4000 mAh",
        normalized_title="Аккумулятор ZTE Blade A71 Li3839T44P8h866445 4000 mAh",
        item_type="battery",
    )
    product = Product(
        name="Аккумулятор для ZTE Blade L3 (Li3820T43P3h785439) 3000 mAh",
        subject="аккумулятор",
    )

    codes = competitor_battery_part_codes(item)

    assert battery_part_code_conflict(product, codes) is True
    assert battery_capacity_conflict(item.name, product.name, None) is True
    assert battery_pair_diagnostic_reasons(item, product) == [
        "battery_part_code_conflict",
        "battery_capacity_conflict",
    ]


def test_battery_premium_tier_requires_explicit_signal_on_both_sides() -> None:
    plain_item = CompetitorItem(
        competitor="moba",
        external_id="BTT-BM6H",
        name="Аккумулятор для Xiaomi 17 Pro (BM6H)",
        item_type="battery",
    )
    premium_item = CompetitorItem(
        competitor="moba",
        external_id="BTT-BM6H-PREMIUM",
        name="Аккумулятор для Xiaomi 17 Pro (BM6H) - Battery Collection (Премиум)",
        item_type="battery",
    )
    zevo_item = CompetitorItem(
        competitor="moba",
        external_id="BTT-BM6H-ZEVO",
        name="Аккумулятор для Xiaomi 17 Pro (BM6H) - Zevo",
        item_type="battery",
    )
    plain_product = Product(name="Аккумулятор для Xiaomi 17 Pro (BM6H)")
    premium_product = Product(name="Аккумулятор для Xiaomi 17 Pro (BM6H) (Premium)")

    assert battery_premium_tier_conflict(plain_item, plain_product) is False
    assert battery_premium_tier_conflict(premium_item, premium_product) is False
    assert battery_premium_tier_conflict(zevo_item, premium_product) is False
    assert battery_premium_tier_conflict(plain_item, premium_product) is True
    assert battery_premium_tier_conflict(premium_item, plain_product) is True


def test_battery_diagnostics_detect_battery_to_non_battery_product() -> None:
    item = CompetitorItem(
        competitor="liberti",
        external_id="BTT-SAM-A320",
        name="Аккумулятор Samsung A320 Galaxy A3 2017",
        item_type="battery",
    )
    product = Product(
        name="Держатель сим-карты Samsung A320 Galaxy A3 2017",
        subject="держатель сим-карты",
    )

    assert "battery_subject_conflict" in battery_pair_diagnostic_reasons(item, product)
