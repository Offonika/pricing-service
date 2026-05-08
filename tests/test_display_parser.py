import pytest

from app.services.display_parser import (
    Backlight,
    ScreenConstruction,
    ScreenKit,
    ScreenMatrixType,
    ScreenQualityGrade,
    parse_display_attributes,
)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("LCD дисплей iPhone 11", ScreenMatrixType.LCD_IPS),
        ("IPS LCD модуль", ScreenMatrixType.LCD_IPS),
        ("TFT LCD экран", ScreenMatrixType.LCD_TFT),
        ("LTPS LCD", ScreenMatrixType.LTPS_LCD),
        ("LTPO AMOLED дисплей", ScreenMatrixType.LTPO_AMOLED),
        ("AMOLED дисплей", ScreenMatrixType.AMOLED),
        ("OLED модуль", ScreenMatrixType.OLED),
    ],
)
def test_matrix_type(name: str, expected: ScreenMatrixType) -> None:
    result = parse_display_attributes(name)
    assert result.screen_matrix_type == expected


@pytest.mark.parametrize(
    ("name", "expected_kit", "has_frame"),
    [
        ("LCD дисплей в сборе с тачскрином", ScreenKit.DISPLAY_WITH_TOUCH, False),
        ("LCD дисплей + тачскрин", ScreenKit.DISPLAY_WITH_TOUCH, False),
        ("дисплей с рамкой крепления", ScreenKit.DISPLAY_WITH_FRAME, True),
        ("дисплей в рамке", ScreenKit.DISPLAY_WITH_FRAME, True),
        ("дисплей рамке", ScreenKit.DISPLAY_WITH_FRAME, True),
        ("дисплей без рамки", ScreenKit.UNKNOWN, False),
        ("дисплей с рамкой и тачскрином", ScreenKit.DISPLAY_TOUCH_FRAME, True),
        ("дисплей без тачскрина", ScreenKit.DISPLAY_ONLY, None),
        ("дисплей с тачскрином и рамкой крепления", ScreenKit.DISPLAY_TOUCH_FRAME, True),
    ],
)
def test_screen_kit(name: str, expected_kit: ScreenKit, has_frame: object) -> None:
    result = parse_display_attributes(name)
    assert result.screen_kit == expected_kit
    assert result.has_frame == has_frame


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("дисплей яркая подсветка", Backlight.BRIGHT_BACKLIGHT),
        ("дисплей без подсветки", Backlight.NO_BACKLIGHT),
        ("дисплей с подсветкой", Backlight.WITH_BACKLIGHT),
    ],
)
def test_backlight(name: str, expected: Backlight) -> None:
    result = parse_display_attributes(name)
    assert result.backlight == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("SOFT OLED 120Hz", ScreenConstruction.SOFT_OLED),
        ("Hard OLED модуль", ScreenConstruction.HARD_OLED),
        ("In-Cell дисплей", ScreenConstruction.INCELL),
        ("On-Cell экран", ScreenConstruction.ONCELL),
        ("COF дисплей", ScreenConstruction.COF),
        ("COG дисплей", ScreenConstruction.COG),
    ],
)
def test_construction(name: str, expected: ScreenConstruction) -> None:
    result = parse_display_attributes(name)
    assert result.screen_construction == expected
    if expected in {ScreenConstruction.SOFT_OLED, ScreenConstruction.HARD_OLED}:
        assert result.screen_matrix_type == ScreenMatrixType.OLED


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("AAA", ScreenQualityGrade.AAA),
        ("HQ", ScreenQualityGrade.HQ),
        ("original", ScreenQualityGrade.ORIGINAL),
        ("original change glass", ScreenQualityGrade.ORIGINAL_REFURB),
        ("OR заменено только стекло", ScreenQualityGrade.ORIGINAL_REFURB),
        ("OR100", ScreenQualityGrade.OR100),
        ("100% OR", ScreenQualityGrade.OR100),
        ("OR 100%", ScreenQualityGrade.OR100),
        ("100% OR SP", ScreenQualityGrade.OR100),
        ("OR", ScreenQualityGrade.OR),
        ("OR_LCD", ScreenQualityGrade.OR),
        ("ORLCD", ScreenQualityGrade.OR),
        ("OEM", ScreenQualityGrade.OEM),
        ("премиум", ScreenQualityGrade.PREMIUM),
        ("1-я категория", ScreenQualityGrade.FIRST_CLASS),
        ("copy high", ScreenQualityGrade.COPY_HIGH),
        ("copy low", ScreenQualityGrade.COPY_LOW),
    ],
)
def test_quality(name: str, expected: ScreenQualityGrade) -> None:
    result = parse_display_attributes(name)
    assert result.screen_quality_grade == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("модуль с рамкой Лавандовый", "Фиолетовый"),
        ("модуль с рамкой Желтый", "Желтый"),
        ("модуль с рамкой Оранжевый", "Оранжевый"),
        ("модуль с рамкой Мятный", "Мятный"),
        ("модуль с рамкой Серебро", "Серебристый"),
        ("модуль с рамкой Бронзовый", "Бронзовый"),
        ("модуль с рамкой Коричневый", "Коричневый"),
        ("модуль с рамкой Титановый", "Титановый"),
    ],
)
def test_color_extended(name: str, expected: str) -> None:
    result = parse_display_attributes(name)
    assert result.color == expected


def test_refresh_rate() -> None:
    result = parse_display_attributes("дисплей SOFT OLED 120Hz")
    assert result.refresh_rate_hz == 120


def test_matrix_tags() -> None:
    result = parse_display_attributes("OLED JCID матрица ZY Zetton GJX")
    assert sorted(result.matrix_tags) == ["GJX", "JCID", "ZY", "Zetton"]


def test_manufacturer() -> None:
    result = parse_display_attributes("LCD Apple Co (SP) GX ORIG FOG")
    assert result.manufacturer == "Apple Co (SP)"

    result = parse_display_attributes("OLED GX ORIG")
    assert result.manufacturer == "GX ORIG"

    result = parse_display_attributes("OLED MOSHI ZY")
    assert result.manufacturer == "MOSHI"

    result = parse_display_attributes("OLED GX")
    assert result.manufacturer == "GX"
    assert result.screen_quality_grade == ScreenQualityGrade.UNKNOWN


def test_ic_pad() -> None:
    result = parse_display_attributes("дисплей площадка под IC")
    assert result.has_ic_pad is True

    result = parse_display_attributes("LCD ic pad")
    assert result.has_ic_pad is True


def test_binding_no_solder() -> None:
    result = parse_display_attributes("OLED JCID (привязка без пайки)")
    assert result.has_binding_no_solder is True

    result = parse_display_attributes("JCID без пайки")
    assert result.has_binding_no_solder is True


def test_notes_for_forbidden_tokens() -> None:
    result = parse_display_attributes("дисплей для смартфона / для iphone")
    assert result.screen_matrix_type == ScreenMatrixType.UNKNOWN
    assert "для смартфона" in result.notes_raw_tokens
    assert "для iphone" in result.notes_raw_tokens
