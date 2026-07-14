from app.services.competitor_matching import _normalize_model_key, parse_model_name


def test_parse_apple_with_a_code_and_variant():
    parsed = parse_model_name("Дисплей для iPhone 11 Pro Max (A2218) в сборе")
    assert parsed.brand == "apple"
    assert parsed.model in {"11 pro max", "11 max", "iphone 11 pro max"}  # allow simplified variant
    assert parsed.ambiguous is False
    assert parsed.confidence >= 0.6


def test_parse_apple_multi_generation_is_ambiguous():
    parsed = parse_model_name("Дисплей для iPhone 11 12 13 Pro Max в сборе")
    assert parsed.ambiguous is True
    assert parsed.model is None
    assert parsed.confidence == 0.0


def test_parse_long_model_rejected():
    parsed = parse_model_name("Дисплей для Apple 10102022a2696a2757a2777or")
    assert parsed.ambiguous is True
    assert parsed.model is None
    assert parsed.confidence == 0.0


def test_parse_no_brand_is_ambiguous():
    parsed = parse_model_name("Дисплей без указания бренда")
    assert parsed.ambiguous is True
    assert parsed.brand is None
    assert parsed.confidence == 0.0


def test_normalize_model_key_strips_noise():
    key = _normalize_model_key("Galaxy S23 Ultra")
    assert key == "galaxys23ultra"


def test_parse_samsung_s23_ultra():
    parsed = parse_model_name("Дисплей для Samsung Galaxy S23 Ultra (OLED)")
    assert parsed.brand == "samsung"
    assert _normalize_model_key(parsed.model) == "s23ultra"
    assert parsed.variant in {None, "ultra", "Ultra".lower()}
    assert parsed.ambiguous is False
    assert parsed.confidence >= 0.8


def test_parse_samsung_a30s_keeps_suffix_and_code_out_of_model():
    parsed = parse_model_name("Шлейф/FLC Samsung Galaxy A30s (A307F) сканер отпечатка пальцев")
    assert parsed.brand == "samsung"
    assert parsed.model == "a 30s"
    assert parsed.ambiguous is False
    assert parsed.confidence >= 0.8


def test_parse_xiaomi_redmi_note():
    parsed = parse_model_name("Дисплей для Xiaomi Redmi Note 11 Pro 5G")
    assert parsed.brand == "xiaomi"
    assert "redmi note 11" in parsed.model
    assert parsed.ambiguous is False
    assert parsed.confidence >= 0.75


def test_parse_xiaomi_redmi_7a_ignores_long_device_code():
    parsed = parse_model_name("Шлейф для Xiaomi Redmi 7A (M1903C3EE) на кнопки громкости")
    assert parsed.brand == "xiaomi"
    assert parsed.model == "redmi 7a"
    assert parsed.ambiguous is False


def test_parse_huawei_p50_combined_series_token():
    parsed = parse_model_name("Шлейф для Huawei P50 (ABR-LX9) плата на системный разъем")
    assert parsed.brand == "huawei"
    assert parsed.model == "p 50"
    assert parsed.ambiguous is False


def test_parse_slash_multi_models_for_competitor_compatibility():
    parsed = parse_model_name("Аккумулятор для Infinix Hot 60 Pro/60 Pro+/60i 4G (BL-50FX)")
    assert parsed.brand == "infinix"
    assert parsed.models == ["hot 60 pro", "hot 60 pro plus", "hot 60i 4g"]
    assert parsed.ambiguous is False


def test_parse_slash_multi_models_keeps_cross_brand_model():
    parsed = parse_model_name(
        "Дисплей для Tecno Spark 30 Pro/Infinix Hot 50 Pro (KL7/X6881) "
        "в сборе с тачскрином Черный - (In-Cell)"
    )
    assert parsed.brand == "tecno"
    assert parsed.models == ["spark 30 pro", "infinix hot 50 pro"]
    assert parsed.ambiguous is False


def test_parse_slash_multi_models_ignores_samsung_device_code_suffix():
    parsed = parse_model_name(
        "LCD дисплей для Samsung Galaxy A10/M10 SM-A105/M105 в рамке (черный) SP_Ref"
    )
    assert parsed.brand == "samsung"
    assert parsed.models == ["galaxy a10", "galaxy m10"]
    assert parsed.ambiguous is False


def test_parse_xiaomi_numeric_flagship_without_family_token():
    parsed = parse_model_name("Дисплей для Xiaomi 12 Pro (2201122G) модуль с рамкой Черный - OR")
    assert parsed.brand == "xiaomi"
    assert parsed.model in {"12 pro", "12 pro 5g"}
    assert parsed.ambiguous is False
    assert parsed.confidence >= 0.75


def test_parse_xiaomi_14t_without_family_token():
    parsed = parse_model_name("Дисплей для Xiaomi 14T (2406APNFAG) модуль с рамкой Черный - OR")
    assert parsed.brand == "xiaomi"
    assert parsed.model == "14t"
    assert parsed.ambiguous is False
    assert parsed.confidence >= 0.75


def test_parse_xiaomi_poco_f7_pro():
    parsed = parse_model_name(
        "Дисплей для Xiaomi Poco F7 Pro (24117RK2CG) модуль с рамкой Черный - OR"
    )
    assert parsed.brand == "xiaomi"
    assert parsed.model == "poco f7 pro"
    assert parsed.variant in {None, "pro"}
    assert parsed.ambiguous is False
    assert parsed.confidence >= 0.75


def test_parse_xiaomi_13_ignores_quality_percent_noise():
    parsed = parse_model_name(
        "LCD дисплей для Xiaomi 13 (2211133G) с тачскрином в рамке (серебристый) 100% OR"
    )
    assert parsed.brand == "xiaomi"
    assert parsed.model == "13"
    assert parsed.ambiguous is False
    assert parsed.confidence >= 0.75


def test_parse_xiaomi_redmi_note_14_pro_4g():
    parsed = parse_model_name(
        "LCD дисплей для Xiaomi Redmi Note 14 Pro 4G с тачскрином (черный) 100% OR"
    )
    assert parsed.brand == "xiaomi"
    assert parsed.model == "redmi note 14 pro 4g"
    assert parsed.ambiguous is False
    assert parsed.confidence >= 0.75


def test_parse_xiaomi_redmi_note_10_pro_4g():
    parsed = parse_model_name(
        "LCD дисплей для Xiaomi Redmi Note 10 Pro 4G с тачскрином в рамке (черный) 100% OR SP"
    )
    assert parsed.brand == "xiaomi"
    assert parsed.model == "redmi note 10 pro 4g"
    assert parsed.ambiguous is False
    assert parsed.confidence >= 0.75


def test_parse_honor_with_variant():
    parsed = parse_model_name("Дисплей для Honor 90 Pro OLED")
    assert parsed.brand == "honor"
    assert "90" in parsed.model
    assert parsed.variant in {None, "pro"}
    assert parsed.ambiguous is False
    assert parsed.confidence >= 0.75


def test_parse_huawei_honor_x8b():
    parsed = parse_model_name(
        "LCD дисплей для Huawei Honor X8b (LLY-LX1) с тачскрином в рамке (серебро) 100% OR"
    )
    assert parsed.brand == "huawei"
    assert parsed.model == "honor x8b"
    assert parsed.ambiguous is False
    assert parsed.confidence >= 0.75


def test_parse_realme_numeric():
    parsed = parse_model_name("Дисплей для Realme 10 Pro Plus")
    assert parsed.brand == "realme"
    assert "10" in parsed.model
    assert parsed.variant in {None, "plus", "pro plus", "pro"}
    assert parsed.ambiguous is False
    assert parsed.confidence >= 0.72


def test_parse_oppo_reno():
    parsed = parse_model_name("Дисплей для Oppo Reno 10 Pro OLED")
    assert parsed.brand == "oppo"
    assert "reno" in parsed.model
    assert "10" in parsed.model
    assert parsed.variant in {None, "pro"}
    assert parsed.ambiguous is False
    assert parsed.confidence >= 0.7


def test_parse_vivo_x_series():
    parsed = parse_model_name("Дисплей для Vivo X90 Pro")
    assert parsed.brand == "vivo"
    assert "90" in parsed.model
    assert parsed.variant in {None, "pro"}
    assert parsed.ambiguous is False
    assert parsed.confidence >= 0.7


def test_parse_vivo_y_suffix_model() -> None:
    parsed = parse_model_name("Аккумулятор для Vivo Y33s 4G (B-S2)")
    assert parsed.brand == "vivo"
    assert parsed.model == "y 33s 4g"
    assert parsed.ambiguous is False
    assert parsed.confidence >= 0.7


def test_parse_oppo_a_series_with_variant_and_network() -> None:
    parsed = parse_model_name("Задняя крышка для OPPO A5 Pro 4G (CPH2711)")
    assert parsed.brand == "oppo"
    assert parsed.model == "a 5 pro 4g"
    assert parsed.ambiguous is False
    assert parsed.confidence >= 0.7


def test_parse_oneplus_11r():
    parsed = parse_model_name("Дисплей для OnePlus 11R AMOLED")
    assert parsed.brand == "oneplus"
    assert "11" in parsed.model
    assert parsed.variant in {None, "r", "t"}
    assert parsed.ambiguous is False
    assert parsed.confidence >= 0.7


def test_parse_huawei_watch_gt():
    parsed = parse_model_name("Дисплей для Huawei Watch GT 4 41mm")
    assert parsed.brand == "huawei"
    assert "gt" in parsed.model or "41" in parsed.model
    assert parsed.ambiguous is False
    assert parsed.confidence >= 0.7


def test_parse_apple_iphone_16e() -> None:
    parsed = parse_model_name("Дисплей для iPhone 16e (A3408/A3409/A3410/A3212) в сборе")
    assert parsed.brand == "apple"
    assert parsed.model in {"16e", "iphone 16e"}
    assert parsed.ambiguous is False
    assert parsed.confidence >= 0.6


def test_parse_apple_iphone_17e() -> None:
    parsed = parse_model_name("Дисплей для iPhone 17e (A3521) в сборе")
    assert parsed.brand == "apple"
    assert parsed.model in {"17e", "iphone 17e"}
    assert parsed.ambiguous is False
    assert parsed.confidence >= 0.6


def test_parse_apple_iphone_esim_is_not_17e() -> None:
    parsed = parse_model_name("Материнская плата для iPhone 17 (A3520) E-Sim 256Gb (iCloud locked)")
    assert parsed.brand == "apple"
    assert parsed.model == "iphone 17"
    assert parsed.ambiguous is False
    assert parsed.confidence >= 0.6


def test_parse_apple_iphone_board_keyword() -> None:
    parsed = parse_model_name(
        "Материнская плата для iPhone 17 Pro Max (A3526) E-Sim 256Gb (iCloud locked)"
    )
    assert parsed.brand == "apple"
    assert parsed.model == "iphone 17 pro max"
    assert parsed.ambiguous is False
    assert parsed.confidence >= 0.6


def test_parse_apple_ipad_air_4() -> None:
    parsed = parse_model_name(
        'Дисплей для iPad Air 4 10.9" 2020 (A2316/A2324/A2325/A2072) в сборе с тачскрином Черный - OR'
    )
    assert parsed.brand == "apple"
    assert parsed.model == "ipad air 4"
    assert parsed.ambiguous is False
    assert parsed.confidence >= 0.8


def test_parse_apple_ipad_mini_5() -> None:
    parsed = parse_model_name(
        "Дисплей для iPad mini 5 2019 (A2133/A2124/A2126/A2125) в сборе с тачскрином Черный - OR"
    )
    assert parsed.brand == "apple"
    assert parsed.model == "ipad mini 5"
    assert parsed.ambiguous is False
    assert parsed.confidence >= 0.8


def test_parse_apple_branded_ipad_air_2() -> None:
    parsed = parse_model_name("LCD дисплей для Apple iPad Air 2 (черный) Оригинал с тачскрином")
    assert parsed.brand == "apple"
    assert parsed.model == "ipad air 2"
    assert parsed.ambiguous is False
    assert parsed.confidence >= 0.8


def test_parse_apple_branded_ipad_air_3() -> None:
    parsed = parse_model_name(
        "LCD дисплей для Apple iPad Air 3 (10.5'') 2019 (A2123/A2152/A2153) Оригинал с тачскрином (черный)"
    )
    assert parsed.brand == "apple"
    assert parsed.model == "ipad air 3"
    assert parsed.ambiguous is False
    assert parsed.confidence >= 0.8


def test_parse_apple_ipad_pro_97() -> None:
    parsed = parse_model_name(
        "LCD дисплей для Apple iPad Pro (9.7) Оригинал с тачскрином (A1673, A1675, A1674) (черный)"
    )
    assert parsed.brand == "apple"
    assert parsed.model == "ipad pro 9.7"
    assert parsed.ambiguous is False
    assert parsed.confidence >= 0.8


def test_parse_apple_ipad_10_109() -> None:
    parsed = parse_model_name('Дисплей для iPad 10 10.9" 2022 (A2696/A2757/A2777) - OR')
    assert parsed.brand == "apple"
    assert parsed.model == "ipad 10 10.9"
    assert parsed.ambiguous is False
    assert parsed.confidence >= 0.8


def test_parse_apple_ipad_11_110() -> None:
    parsed = parse_model_name('Дисплей для iPad 11 11" 2025 (A3355) - OR')
    assert parsed.brand == "apple"
    assert parsed.model == "ipad 11"
    assert parsed.ambiguous is False
    assert parsed.confidence >= 0.8
