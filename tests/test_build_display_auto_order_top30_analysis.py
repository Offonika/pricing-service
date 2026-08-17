from decimal import Decimal

from tasks.build_display_auto_order_top30_analysis import (
    _diagnose,
    _family,
    _is_multiple,
)


def test_family_classifies_shared_product_brands() -> None:
    assert _family("Дисплей Xiaomi Redmi Note 14 / Poco M7") == "Xiaomi/Redmi/Poco"
    assert _family("Дисплей Apple iPhone 11") == "Apple iPhone"
    assert _family("Дисплей Honor X7c") == "Huawei/Honor"


def test_multiple_requires_positive_quantity() -> None:
    assert _is_multiple(Decimal("500"), 100)
    assert not _is_multiple(Decimal("525"), 100)
    assert not _is_multiple(Decimal("0"), 100)


def test_confirmed_underforecast_has_priority_over_batch_hypothesis() -> None:
    category, _label, confidence, _check = _diagnose(
        name="Дисплей Redmi A5",
        actual_qty=Decimal("500"),
        model_qty=Decimal("250"),
        delta_qty=Decimal("-250"),
        forecast_bias=Decimal("-0.20"),
    )

    assert category == "forecast_understatement_confirmed"
    assert confidence == "высокая"


def test_zero_model_order_does_not_claim_supplier_block() -> None:
    category, label, confidence, _check = _diagnose(
        name="Дисплей Samsung A15",
        actual_qty=Decimal("300"),
        model_qty=Decimal("0"),
        delta_qty=Decimal("-300"),
        forecast_bias=Decimal("0.15"),
    )

    assert category == "opening_stock_or_forward_buffer_gap"
    assert "имеющемся запасе" in label
    assert confidence == "механизм подтверждён; бизнес-причина не подтверждена"
