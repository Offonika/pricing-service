from app.services.display_quality_raw_mapping import (
    extract_quality_token_as_in_name,
    map_competitor_raw_quality_to_1c_raw,
)


def test_extract_quality_token_as_in_name_for_moba_raw_values() -> None:
    assert (
        extract_quality_token_as_in_name(
            "Дисплей для Samsung Galaxy A6 2018 (A600F) в сборе с тачскрином Черный - OR (SP)"
        )
        == "OR (SP)"
    )
    assert (
        extract_quality_token_as_in_name(
            "Дисплей для Realme C55 (RMX3710) в сборе с тачскрином Черный - OR100"
        )
        == "OR100"
    )
    assert (
        extract_quality_token_as_in_name(
            "Дисплей для Huawei P20 (EML-L29) в сборе с тачскрином Черный - Оптима"
        )
        == "Оптима"
    )
    assert (
        extract_quality_token_as_in_name(
            "Дисплей для Huawei P30 Lite в сборе с тачскрином Черный - Стандарт (COG)"
        )
        == "Стандарт"
    )
    assert (
        extract_quality_token_as_in_name(
            "Дисплей для Samsung Galaxy A52 модуль с рамкой Черный - (OLED) (Full Size)"
        )
        == "(OLED) (Full Size)"
    )
    assert (
        extract_quality_token_as_in_name(
            "Дисплей для Samsung Galaxy J4 2018 (J400F) в сборе с тачскрином Черный - (In-Cell)"
        )
        == "(In-Cell)"
    )
    assert (
        extract_quality_token_as_in_name(
            "LCD дисплей для Apple iPhone 12 Mini original (change glass) без ошибки"
        )
        == "Original Refurbished"
    )
    assert extract_quality_token_as_in_name("LCD дисплей для Huawei Honor Pad OR 100%") == "100% OR"
    assert (
        extract_quality_token_as_in_name(
            "LCD дисплей для Apple iPhone 7 (яркая подсветка) (AAA) 1-я категория"
        )
        == "1-я категория"
    )
    assert (
        extract_quality_token_as_in_name("LCD дисплей для Apple iPhone 6 с рамкой крепления HQ")
        == "HQ"
    )


def test_map_competitor_raw_quality_to_1c_raw_for_moba() -> None:
    assert map_competitor_raw_quality_to_1c_raw("moba", "OR") == "(ORIG)"
    assert map_competitor_raw_quality_to_1c_raw("moba", "OR100") == "(ORIG100)"
    assert map_competitor_raw_quality_to_1c_raw("moba", "OR (SP)") == "(ORIG100) (SP)"
    assert map_competitor_raw_quality_to_1c_raw("moba", "Стандарт") == "(Medium)"
    assert map_competitor_raw_quality_to_1c_raw("moba", "Оптима") == "(Medium)"
    assert map_competitor_raw_quality_to_1c_raw("moba", "Премиум") == "(Premium)"


def test_map_competitor_raw_quality_to_1c_raw_ignores_non_quality_tokens() -> None:
    assert map_competitor_raw_quality_to_1c_raw("moba", "(OLED)") is None
    assert map_competitor_raw_quality_to_1c_raw("moba", "(In-Cell)") is None
    assert map_competitor_raw_quality_to_1c_raw("liberti", "OR100") is None
