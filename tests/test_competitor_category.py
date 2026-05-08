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
