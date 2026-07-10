import pytest

from app.services.display_normalization import (
    normalize_display_construction,
    normalize_display_quality,
    normalize_display_type,
    normalize_refresh_rate_hz,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("LCD IPS", "LCD (IPS)"),
        ("IPS", "LCD (IPS)"),
        ("TFT LCD", "LCD (TFT)"),
        ("ltps lcd", "LTPS LCD"),
        ("ltpo amoled", "LTPO AMOLED"),
        ("Dynamic AMOLED", "Dynamic AMOLED"),
        ("Super AMOLED", "Super AMOLED"),
        ("AMOLED", "AMOLED"),
        ("OLED", "OLED"),
        ("hard oled", "OLED"),
    ],
)
def test_normalize_display_type(value: str, expected: str) -> None:
    assert normalize_display_type(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("ORIG", "Original"),
        ("оригинал", "Original"),
        ("original refurbished", "Original Refurbished"),
        ("Биток", "Original Refurbished"),
        ("OR (заменено только стекло)", "Original Refurbished"),
        ("original change glass", "Original Refurbished"),
        ("OEM", "OEM"),
        ("copy high", "Copy High"),
        ("premium AAA", "Copy High"),
        ("ААА", "Copy High"),
        ("Аналог", "Copy Medium"),
        ("copy low", "Copy Low"),
        ("copy", "Copy Medium"),
        ("optima", "Copy Medium"),
        ("Оптима", "Copy Medium"),
        ("Премиум", "Copy High"),
        ("Стандарт (COG)", "Copy Medium"),
        ("1-я категория", "Copy Medium"),
    ],
)
def test_normalize_display_quality(value: str, expected: str) -> None:
    assert normalize_display_quality(value) == expected


@pytest.mark.parametrize("value", ["OLED", "(OLED)", "In-Cell", "(In-Cell)", "AMOLED"])
def test_normalize_display_quality_does_not_treat_display_type_as_quality(value: str) -> None:
    assert normalize_display_quality(value) is None


def test_normalize_display_quality_does_not_treat_highscreen_brand_as_high() -> None:
    assert normalize_display_quality("LCD дисплей для Highscreen Power Rage") is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("incell", "In-Cell"),
        ("on-cell", "On-Cell"),
        ("COF", "COF"),
        ("cog", "COG"),
    ],
)
def test_normalize_display_construction(value: str, expected: str) -> None:
    assert normalize_display_construction(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("120 Hz", 120),
        ("90hz", 90),
        ("144 гц", 144),
        (60, 60),
        ("60", 60),
    ],
)
def test_normalize_refresh_rate_hz(value: object, expected: int) -> None:
    assert normalize_refresh_rate_hz(value) == expected


def test_refresh_rate_unknown() -> None:
    assert normalize_refresh_rate_hz("75 hz") is None
