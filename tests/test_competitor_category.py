from app.services.competitor_category import rule_classify


def test_rule_classify_display_module():
    assert rule_classify("Дисплей для Xiaomi Redmi Note 10 Pro OLED") == "дисплей"


def test_rule_classify_protective_film():
    assert rule_classify("OCA пленка для iPhone 6/6s") == "пленка oca"


def test_rule_classify_camera_glass():
    assert rule_classify("Стекло задней камеры для Samsung A72") == "стекло камеры"


def test_rule_classify_cleaning_chemistry():
    name = "Гель для чистки линз камер 2UUL GL03 Camera Lens Cleaner Synthetic Resin Gel (30 г)"
    assert rule_classify(name) == "химия"


def test_rule_classify_buttons():
    assert rule_classify("Кнопка включения iPhone 8") == "кнопки"


def test_rule_classify_keyboard():
    assert rule_classify("Клавиатура для MacBook Air A1466") == "клавиатура"


def test_rule_classify_dust_cover():
    assert rule_classify("Пыльник для камеры iPhone 12") == "пыльник"


def test_rule_classify_display_feature_not_part():
    name = "Bluetooth FM модулятор BT-891 (большой дисплей/USB/SD/Line-in)"
    assert rule_classify(name) == "прочее"


def test_rule_classify_stencil_as_tool():
    assert rule_classify("BGA трафарет Amaoe (LCD3, V6.0)") == "инструмент"


def test_rule_classify_connector():
    assert rule_classify("Коннектор АКБ для iPhone 11 Pro Max") == "коннектор"


def test_rule_classify_adapter():
    assert rule_classify("Переходник HDMI (F) - VGA (M) Черный") == "адаптер"


def test_rule_classify_display_with_frame_stays_display():
    name = "Дисплей для Samsung S942 Galaxy S26 (в сборе с тачскрином) (черный) (в рамке) (ORIG100) (SP)"
    assert rule_classify(name) == "дисплей"


def test_rule_classify_huawei_mate_is_not_tool():
    assert (
        rule_classify("Аккумулятор для Huawei Mate 80 (VYG-AL00) (HB496880EXW-11) (Premium)")
        == "аккумулятор"
    )
    assert (
        rule_classify("Дисплей для Huawei Mate 80 (VYG-AL00) + тачскрин (черный) (In-Cell)")
        == "дисплей"
    )


def test_rule_classify_laptop_matrix_is_not_tool():
    name = "Матрица для Apple MacBook Air 15 M4 Retina A3241 (2025) (AASP)"
    assert rule_classify(name) == "матрица для ноутбука"


def test_rule_classify_silicone_grease_as_chemistry():
    assert rule_classify("Смазка силиконовая Solins СИ-350 (2 г.)") == "химия"


def test_rule_classify_joystick_without_catalog_value_as_other():
    assert rule_classify("Джостик для Meta Quest 3S VR (черный)") == "прочее"


def test_rule_classify_sticker_with_tape_stays_sticker():
    assert rule_classify("Наклейка для задней крышки iPhone 15 двухсторонний скотч") == "наклейка"


def test_rule_classify_bottom_board_with_sim_connector_stays_board():
    name = "Нижняя плата для Xiaomi 17 Ultra (25128PNA1G) с комп. + разъем зарядки + микрофон + разъем SIM"
    assert rule_classify(name) == "плата"


def test_rule_classify_flex_for_display_stays_flex():
    name = "Шлейф для Samsung S942 Galaxy S26 с комп. (на дисплей)"
    assert rule_classify(name) == "шлейф"


def test_rule_classify_flex_for_touchpad_stays_flex():
    name = "Шлейф для Apple MacBook Pro 13 M1 Retina A2338 (с разбора) (на тачпад)"
    assert rule_classify(name) == "шлейф"


def test_rule_classify_case_with_keyboard_stays_case():
    name = "Корпус для Apple MacBook Pro 13 M1 Retina A2338 (в сборе с клавиатурой и тачбаром)"
    assert rule_classify(name) == "корпус"


def test_rule_classify_screws_with_board_holders_stays_screws():
    name = (
        "Винты для Apple MacBook Pro 13 M1 Retina A2338 (на крепление платы) (с держателями платы)"
    )
    assert rule_classify(name) == "винты"


def test_rule_classify_motherboard_stays_board():
    name = "Материнская плата для Apple Watch 9 (41 мм) (снятая) (не активированная)"
    assert rule_classify(name) == "плата"


def test_rule_classify_fan_with_cooler_note_stays_fan():
    name = "Вентилятор (кулер) для Apple MacBook Pro 13 M1 Retina A2338 (с разбора) (ORIG100)"
    assert rule_classify(name) == "вентилятор"
