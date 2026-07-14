from __future__ import annotations

from app.models import Product
from app.services.product_display_modification import (
    STATUS_CONFIRMED,
    STATUS_CONFLICT,
    STATUS_DERIVED_FROM_NAME,
    STATUS_DERIVED_FROM_ONEC,
    STATUS_UNKNOWN,
    analyze_product_display_modification,
    normalize_onec_in_frame,
)


def test_normalize_onec_in_frame() -> None:
    assert normalize_onec_in_frame("Да") is True
    assert normalize_onec_in_frame("Нет") is False
    assert normalize_onec_in_frame("без рамки") is False
    assert normalize_onec_in_frame(None) is None


def test_display_frame_confirmed_true() -> None:
    product = Product(article="P1", name="Дисплей iPhone 12 с тачскрином в рамке", in_frame="Да")

    result = analyze_product_display_modification(product)

    assert result.status == STATUS_CONFIRMED
    assert result.display_has_frame is True
    assert result.confidence == 1.0


def test_display_frame_confirmed_false() -> None:
    product = Product(article="P2", name="Дисплей iPhone 12 без рамки", in_frame="Нет")

    result = analyze_product_display_modification(product)

    assert result.status == STATUS_CONFIRMED
    assert result.display_has_frame is False
    assert result.confidence == 1.0


def test_display_frame_derived_from_name() -> None:
    product = Product(article="P3", name="Дисплей iPhone 12 в рамке", in_frame=None)

    result = analyze_product_display_modification(product)

    assert result.status == STATUS_DERIVED_FROM_NAME
    assert result.display_has_frame is True
    assert result.confidence == 0.85


def test_display_frame_derived_from_touch_kit_without_frame() -> None:
    product = Product(article="P3A", name="Дисплей iPhone 12 + тачскрин", in_frame=None)

    result = analyze_product_display_modification(product)

    assert result.status == STATUS_DERIVED_FROM_NAME
    assert result.display_has_frame is False
    assert result.confidence == 0.85


def test_display_frame_touch_kit_without_frame_does_not_override_onec_true() -> None:
    product = Product(article="P3B", name="Дисплей iPhone 12 + тачскрин", in_frame="Да")

    result = analyze_product_display_modification(product)

    assert result.status == STATUS_DERIVED_FROM_ONEC
    assert result.display_has_frame is True
    assert result.confidence == 0.8


def test_display_frame_conflict() -> None:
    product = Product(article="P4", name="Дисплей iPhone 12 без рамки", in_frame="Да")

    result = analyze_product_display_modification(product)

    assert result.status == STATUS_CONFLICT
    assert result.display_has_frame is False
    assert result.confidence == 0.9
    assert "in_frame_conflict:name_wins" in result.notes


def test_display_frame_conflict_trusts_name_with_frame() -> None:
    product = Product(article="P4A", name="Дисплей iPhone 12 в рамке", in_frame="Нет")

    result = analyze_product_display_modification(product)

    assert result.status == STATUS_CONFLICT
    assert result.display_has_frame is True
    assert result.confidence == 0.9


def test_display_frame_unknown() -> None:
    product = Product(article="P5", name="Дисплей iPhone 12", in_frame=None)

    result = analyze_product_display_modification(product)

    assert result.status == STATUS_UNKNOWN
    assert result.display_has_frame is None
    assert result.confidence is None
