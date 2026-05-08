from __future__ import annotations

import json
import logging
import os
import re
from urllib.parse import urlparse

import httpx

from app.core.config import get_settings
from app.services.prompts import get_llm_competitor_category_prompt

logger = logging.getLogger(__name__)


def _is_localhost_url(url: str | None) -> bool:
    if not url:
        return False
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host in {"localhost", "127.0.0.1", "::1"}


def _get_llm_timeout_seconds() -> float:
    value = os.environ.get("LOCAL_LLM_TIMEOUT_SECONDS", "").strip()
    if not value:
        return 90.0
    try:
        return float(value)
    except ValueError:
        return 90.0


LLM_TIMEOUT_SECONDS = _get_llm_timeout_seconds()

CATEGORY_TOKENS: dict[str, list[str]] = {
    "скотч": ["скотч", "термоскотч", "проклейк"],
    "салфетки": [
        "салфет",
        "wipe",
        "wipes",
        "cleaning kit",
        "cleaning wipes",
        "alcohol wipe",
        "wet wipe",
        "dry wipe",
    ],
    "наклейка": ["наклейк", "изоляц", "стикер", "sticker"],
    "изолятор": ["изолятор", "изоляция", "термопроклад"],
    "клей": ["клей", "adhesive", "glue"],
    "флюс": ["флюс", "паяльная паста", "паста паяльная", "solder paste", "flux"],
    "припой": ["припой", "solder", "wood metal", "rose metal", "сплав"],
    "химия": ["химия", "компаунд", "compound"],
    "лента": ["лента"],
    "картридер": ["картридер", "card reader", "cardreader"],
    "пыльник": ["пыльник", "dust cover", "dust cap"],
    "крепежи": ["крепеж", "крепёж", "креплен", "креплён"],
    "маркеры": ["маркер"],
    "винты": ["винт", "винты", "шуруп", "болт"],
    "брелок": ["брелок", "keychain"],
    "ножи": ["ножи для бритья", "нож", "knife", "utility knife", "лезв", "blade", "razor"],
    "батарейка": [
        "батарейк",
        "элемент питания",
        "coin cell",
        "button cell",
        "cr2032",
        "cr2025",
        "cr2016",
        "cr2430",
        "cr2450",
        "lr20",
        "lr1",
        "lr23",
        "mn21",
        "lr27",
        "mn27",
        "za13",
        "lr03",
        "lr6",
        "alkaline",
        "zinc air",
    ],
    "пауэрбанк": [
        "пауэрбанк",
        "power bank",
        "powerbank",
        "power-bank",
        "внешний аккумулятор",
        "внешний акб",
        "external battery",
        "portable charger",
        "battery bank",
        "стартер",
        "jump starter",
        "booster",
    ],
    "сканер отпечатка": [
        "сканер отпечат",
        "датчик отпечат",
        "fingerprint",
        "finger print",
        "touch id",
        "touchid",
    ],
    "вибромотор": [
        "вибромотор",
        "виброзвонок",
        "vibration motor",
        "vibrator",
        "taptic",
        "taptic engine",
    ],
    "динамик": [
        "динамик",
        "звуковой динамик",
        "speaker",
        "loudspeaker",
        "звонок",
        "buzzer",
        "ringer",
    ],
    "сетка динамика": [
        "сетка динамика",
        "сетка для динамика",
        "сетка для защиты динамика",
        "speaker mesh",
        "speaker grille",
        "grill mesh",
    ],
    "дисплей": [
        "дисплей",
        "экран",
        "матриц",
        "display",
        "lcd",
        "oled",
        "amoled",
        "ips",
        "ltps",
        "ltpo",
        "incell",
        "in-cell",
        "screen",
    ],
    "тачскрин": [
        "тачскрин",
        "touchscreen",
        "touch screen",
        "digitizer",
        "touch panel",
        "сенсорный экран",
        "сенсорная панель",
    ],
    "тачпад": [
        "тачпад",
        "touchpad",
        "trackpad",
        "трекпад",
    ],
    "матрица для ноутбука": [
        "матрица для ноутбука",
        "матрица ноутбука",
        "laptop matrix",
        "notebook matrix",
        "laptop screen",
        "notebook screen",
    ],
    "аккумулятор": [
        "аккумулятор",
        "battery",
        "акб",
        "аккум",
        "li-ion",
        "li ion",
        "li-pol",
        "li pol",
        "li-po",
        "li po",
        "lipoly",
        "li-poly",
    ],
    "камера": ["камера", "камер", "camera", "линз", "объектив"],
    "стекло камеры": [
        "стекло камеры",
        "стекло на камеру",
        "стекло задней камеры",
        "стекло основной камеры",
        "camera glass",
        "lens glass",
        "glass lens",
    ],
    "защитное стекло": [
        "защитное стекло",
        "tempered glass",
        "screen protector",
        "protective glass",
        "9h",
    ],
    "стекло для переклейки": [
        "стекло для переклейки",
        "стекло для ремонта",
        "стекло для смартфона",
        "glass only",
        "outer glass",
        "glass replacement",
    ],
    "пленка": ["пленк", "film", "protective film"],
    "пленка oca": ["oca пленка", "oca film"],
    "накопитель": [
        "накопитель",
        "ssd",
        "hdd",
        "solid state drive",
        "hard drive",
        "m.2",
        "nvme",
        "жестк",
        "жёстк",
    ],
    "штатив": [
        "штатив",
        "трипод",
        "tripod",
        "монопод",
        "monopod",
        "треугольник поддержки",
    ],
    "клавиатура": ["клавиатур", "keyboard", "kbd"],
    "кнопки": [
        "кнопк",
        "клавиш",
        "button",
        "key",
        "power key",
        "volume key",
        "home key",
    ],
    "шлейф": ["шлейф", "шлейфа", "датчик", "сенсорный шлейф"],
    "контроллер питания": [
        "контроллер питания",
        "контролер питания",
        "power controller",
        "power ic",
        "charging ic",
        "usb charging",
    ],
    "модуль nfc": [
        "модуль nfc",
        "nfc модул",
        "nfc-модул",
        "nfc module",
        "модуль rfid",
        "rfid модул",
        "rfid module",
        "бесконтактный модуль",
        "бесконтактн модул",
    ],
    "антенна": ["антенна", "антена", "antenna", "antena"],
    "процессор": [
        "процессор",
        "процессоры",
        "cpu",
        "processor",
    ],
    "микросхема": [
        "микросхем",
        "кристалл",
        "контроллер",
        "чип",
        "chip",
        "nand",
        "flash",
        "pmic",
        "pmi",
    ],
    "программатор": ["программатор", "програматор", "programmer", "jcid"],
    "сепаратор": ["сепаратор", "separator"],
    "инструмент": [
        "инструмент",
        "tool",
        "съемник",
        "съёмник",
        "трафарет",
        "stencil",
        "струн",
        "микроскоп",
        "microscope",
        "жало паяльника",
        "жало для паяльника",
        "жало для паялника",
        "жала паяльника",
        "паяльник",
        "паяльная станция",
        "паялник",
        "жало",
        "дымоулавливатель",
        "дымоуловитель",
        "fume extractor",
        "smoke absorber",
        "фен",
        "ультразвуковая ванна",
        "ванна",
    ],
    "подсветка": [
        "светодиодная подсветка",
        "подсветка для телевизоров",
        "подсветка",
        "led-smd",
        "led smd",
    ],
    "крышка": [
        "крышк",
        "back cover",
        "rear cover",
        "battery cover",
        "задняя крышка",
        "крышка задняя",
    ],
    "корпус": [
        "корпус",
        "рамка",
        "frame",
        "bezel",
        "панель",
        "бампер",
        "средняя часть",
    ],
    "держатель для смартфона": ["держател", "holder", "mount", "stand", "grip"],
    "держатель сим-карты": [
        "держатель сим",
        "держатель sim",
        "лоток sim",
        "лоток сим",
        "сим лоток",
        "сим-лоток",
        "sim tray",
        "sim-card tray",
        "sim card tray",
        "sim holder",
        "sim-card holder",
        "sim card holder",
        "sim slot",
    ],
    "смартфоны": ["смартфон", "smartphone", "mobile phone"],
    "накладка": ["накладка", "thumb grip", "stick grip"],
    "ножки": ["ножк", "rubber feet", "feet pad"],
    "подставка": ["подставк", "dock", "док", "док-станц", "stand base"],
    "органайзер": ["органайзер", "организатор", "organizer"],
    "чехол": ["чехол", "battery case", "power case"],
    "футляр": ["футляр", "case", "pouch"],
    "стилус": ["стилус", "stylus"],
    "гарнитура": ["гарнитур", "наушник", "headset", "tws", "earbuds"],
    "колонка": ["колонк", "акустик", "саундбар"],
    "коннектор": ["коннектор", "connector"],
    "разъем": ["port", "гнездо"],
    "кабель": ["кабель", "провод", "шнур"],
    "адаптер": ["адаптер", "adapter", "переходник", "hub", "хаб"],
    "зарядка": [
        "зарядное устройство",
        "charger",
        "charging",
        "azu",
        "азу",
        "зу",
        "сзу",
        "адаптер питания",
        "power adapter",
        "ac adapter",
        "зарядная станция",
        "зарядная",
    ],
    "нагреватель": [
        "нагреватель",
        "нагревательный элемент",
        "нагрев",
        "подогрев",
        "heater",
        "heating",
        "hot plate",
        "heat mat",
        "heating mat",
        "hot mat",
        "hot pad",
        "преднагреватель",
    ],
    "кулер": [
        "кулер",
        "cooler",
    ],
    "вентилятор": [
        "вентилятор",
        "fan",
        "cooling fan",
    ],
    "тестер": ["тестер", "tester", "измерител", "тестир"],
    "инвертер": ["инвертер", "inverter"],
    "оперативная память": ["оперативная память", "ddr", "sodimm", "sdram", "dimm"],
    "плата": [
        "плата",
        "board",
        "pcb",
        "нижняя плата",
        "материнская плата",
        "subboard",
        "sub-board",
    ],
    "прочее": ["мыш", "ламп"],
}

DEFAULT_FALLBACK_CATEGORY = "неизвестно"
UNKNOWN_CATEGORY_VALUES = {
    "unknown",
    "undefined",
    "неизвестно",
    "не определено",
    "н/д",
    "нет",
    "-",
}
ALLOWED_CATEGORIES = tuple(CATEGORY_TOKENS.keys()) + (DEFAULT_FALLBACK_CATEGORY,)

CATEGORY_ALIASES: dict[str, str] = {
    "дисплеи": "дисплей",
    "экраны": "дисплей",
    "тачскрин": "тачскрин",
    "тачскрины": "тачскрин",
    "тачскрин oca": "тачскрин",
    "тачскрин пленка oca": "тачскрин",
    "тачпад": "тачпад",
    "тачпады": "тачпад",
    "touchpad": "тачпад",
    "trackpad": "тачпад",
    "трекпад": "тачпад",
    "tester": "тестер",
    "testер": "тестер",
    "touchscreen": "тачскрин",
    "touch screen": "тачскрин",
    "digitizer": "тачскрин",
    "touch panel": "тачскрин",
    "сенсорный экран": "тачскрин",
    "сенсорная панель": "тачскрин",
    "матрица ноутбука": "матрица для ноутбука",
    "матрица для ноутбука": "матрица для ноутбука",
    "матрицы ноутбука": "матрица для ноутбука",
    "матрицы для ноутбука": "матрица для ноутбука",
    "матрицы для ноутбуков": "матрица для ноутбука",
    "laptop matrix": "матрица для ноутбука",
    "notebook matrix": "матрица для ноутбука",
    "laptop screen": "матрица для ноутбука",
    "notebook screen": "матрица для ноутбука",
    "аккумуляторы": "аккумулятор",
    "шлейфы": "шлейф",
    "шлейфа": "шлейф",
    "коннекторы": "коннектор",
    "разъемы": "разъем",
    "камеры": "камера",
    "пыльники": "пыльник",
    "кнопка": "кнопки",
    "кнопки": "кнопки",
    "кнопка включения": "кнопки",
    "кнопка громкости": "кнопки",
    "кнопка блокировки": "кнопки",
    "клавиша": "кнопки",
    "клавиши": "кнопки",
    "клавиша блокировки": "кнопки",
    "клавиатура": "клавиатура",
    "клавиатуры": "клавиатура",
    "keyboard": "клавиатура",
    "kbd": "клавиатура",
    "подложка клавиатуры": "клавиатура",
    "набор клавиш": "кнопки",
    "комплект кнопок": "кнопки",
    "комплект кнопок и держатель sim карты": "кнопки",
    "переключатель блокировки клавиатуры": "кнопки",
    "наклейка на клавиатуру": "наклейка",
    "наклейки для клавиатуры": "наклейка",
    "наклейка антистатик": "наклейка",
    "наклейка антистатическое покрытие": "наклейка",
    "наклейка антистатическое средство": "наклейка",
    "наклейка фольга": "наклейка",
    "наклейки стикеры": "наклейка",
    "держатель сим карты": "держатель сим-карты",
    "держатель сим-карты": "держатель сим-карты",
    "держатель sim карты": "держатель сим-карты",
    "держатель sim-карты": "держатель сим-карты",
    "сим лоток": "держатель сим-карты",
    "сим-лоток": "держатель сим-карты",
    "симлоток": "держатель сим-карты",
    "лоток sim": "держатель сим-карты",
    "лоток сим": "держатель сим-карты",
    "sim лоток": "держатель сим-карты",
    "sim-лоток": "держатель сим-карты",
    "simлоток": "держатель сим-карты",
    "sim tray": "держатель сим-карты",
    "sim-card tray": "держатель сим-карты",
    "sim card tray": "держатель сим-карты",
    "sim holder": "держатель сим-карты",
    "sim-card holder": "держатель сим-карты",
    "sim card holder": "держатель сим-карты",
    "телефон": "смартфоны",
    "телефоны": "смартфоны",
    "смартфон": "смартфоны",
    "смартфоны": "смартфоны",
    "smartphone": "смартфоны",
    "mobile phone": "смартфоны",
    "гидрогелевая пленка": "пленка",
    "гидрогельная пленка": "пленка",
    "футляр для фото": "футляр",
    "футляры": "футляр",
    "изоляция": "изолятор",
    "изолятор плюсового контакта": "изолятор",
    "очки защитные": "инструмент",
    "защитные очки": "инструмент",
    "дымоулавливатель": "инструмент",
    "дымоуловитель": "инструмент",
    "separator": "сепаратор",
    "сепараторы": "сепаратор",
    "антена": "антенна",
    "антенны": "антенна",
    "мулаж": "прочее",
    "муляж": "прочее",
    "сплав вуда": "припой",
    "сплав розе": "припой",
    "wood metal": "припой",
    "rose metal": "припой",
    "картридер": "адаптер",
    "card reader": "адаптер",
    "светодиодная подсветка": "подсветка",
    "паяльная паста": "флюс",
    "паста паяльная": "флюс",
    "solder paste": "флюс",
    "flux": "флюс",
    "ножи для бритья": "ножи",
    "лезвие": "ножи",
    "лезвия": "ножи",
    "razor blade": "ножи",
    "razor blades": "ножи",
    "рамка для камеры": "стекло камеры",
    "рамка стекла камеры": "стекло камеры",
    "рамка стекла задней камеры": "стекло камеры",
    "рамка для ремонта": "инструмент",
    "подсветка": "подсветка",
    "брелок": "брелок",
    "ножи": "ножи",
    "корпуса": "корпус",
    "сетка динамика": "сетка динамика",
    "сетки динамика": "сетка динамика",
    "сеточка динамика": "сетка динамика",
    "разъёмы": "разъем",
    "кабели": "кабель",
    "платы": "плата",
    "чехлы": "чехол",
    "гарнитуры": "гарнитура",
    "колонки": "колонка",
    "наклейки": "наклейка",
    "стекло": "стекло для переклейки",
    "стекла": "стекло для переклейки",
    "защитное стекло": "защитное стекло",
    "стекло для ремонта": "стекло для переклейки",
    "стекло для смартфона": "стекло для переклейки",
    "стекло с oca": "стекло для переклейки",
    "стекло с oca пленкой": "стекло для переклейки",
    "стекло oca": "стекло для переклейки",
    "стекло оса": "стекло для переклейки",
    "стекло для переклейки с oca": "стекло для переклейки",
    "стекло для переклейки с оса": "стекло для переклейки",
    "стекло камеры": "стекло камеры",
    "стекло для камеры": "стекло камеры",
    "стекло на камеру": "стекло камеры",
    "стекло задней камеры": "стекло камеры",
    "стекло основной камеры": "стекло камеры",
    "camera glass": "стекло камеры",
    "glass camera": "стекло камеры",
    "lens glass": "стекло камеры",
    "glass lens": "стекло камеры",
    "защитная пленка": "пленка",
    "защитная плёнка": "пленка",
    "пленка": "пленка",
    "плёнка": "пленка",
    "oca": "пленка oca",
    "oca пленка": "пленка oca",
    "сканер отпечатка пальца": "сканер отпечатка",
    "датчик отпечатка": "сканер отпечатка",
    "датчик отпечатка пальца": "сканер отпечатка",
    "fingerprint sensor": "сканер отпечатка",
    "fingerprint": "сканер отпечатка",
    "touch id": "сканер отпечатка",
    "touchid": "сканер отпечатка",
    "ленты": "лента",
    "клеи": "клей",
    "комплект болтов": "болты",
    "комплект болтов для корпуса": "болты",
    "комплект болтов инструменты": "болты",
    "комплект болтов части для ремонта": "болты",
    "комплект пластин": "комплект пластин",
    "комплект пластины": "комплект пластин",
    "контроллер": "контроллер питания",
    "контролер питания": "контроллер питания",
    "контроллер питания": "контроллер питания",
    "жало паяльника": "инструмент",
    "жала паяльника": "инструмент",
    "жало для паяльника": "инструмент",
    "жало для паялника": "инструмент",
    "динамики": "динамик",
    "сетка для динамика": "сетка динамика",
    "задняя крышка": "крышка",
    "средняя часть": "корпус",
    "держатель": "держатель для смартфона",
    "держатели": "держатель для смартфона",
    "держатель аксессуары": "держатель для смартфона",
    "держатель для велосипеда": "держатель для смартфона",
    "держатель для смартфона": "держатель для смартфона",
    "держатель зажим": "держатель для смартфона",
    "держатель зажим для смартфона": "держатель для смартфона",
    "держатель инструменты": "держатель для смартфона",
    "держатель подставка": "держатель для смартфона",
    "док станция": "зарядка",
    "док-станция": "зарядка",
    "dock station": "зарядка",
    "charging dock": "зарядка",
    "накладка": "накладка",
    "накладки": "накладка",
    "ножка": "ножки",
    "ножки": "ножки",
    "подставка": "подставка",
    "подставки": "подставка",
    "органайзер": "органайзер",
    "органайзеры": "органайзер",
    "организатор": "органайзер",
    "организаторы": "органайзер",
    "организатор для запчастей аксессуаров": "органайзер",
    "паяльная станция": "паяльник",
    "паяльные станции": "паяльник",
    "рамка для дисплея": "рамка",
    "питание": "зарядка",
    "скотч для дисплея": "скотч",
    "скотч для задней крышки": "скотч",
    "средняя часть корпуса": "корпус",
    "ssd накопитель": "накопитель",
    "ssd": "накопитель",
    "микрофоны": "микрофон",
    "чип": "микросхема",
    "чипы": "микросхема",
    "ic": "микросхема",
    "микросхема памяти": "микросхема",
    "ic кристалл": "микросхема",
    "ic-кристалл": "микросхема",
    "led ic": "микросхема",
    "led ic кристалл": "микросхема",
    "led ic-кристалл": "микросхема",
    "mic ic": "микросхема",
    "mic ic кристалл": "микросхема",
    "mic ic-кристалл": "микросхема",
    "nand flash": "микросхема",
    "rand flash": "микросхема",
    "usb charging ic": "контроллер питания",
    "charging ic": "контроллер питания",
    "power controller": "контроллер питания",
    "power ic": "контроллер питания",
    "nfc модуль": "модуль nfc",
    "модуль nfc": "модуль nfc",
    "бесконтактный модуль nfc": "модуль nfc",
    "nfc module": "модуль nfc",
    "програматор": "программатор",
    "программаторы": "программатор",
    "programmer": "программатор",
    "инструмент для вскрытия": "инструмент",
    "инструменты аксессуары": "инструмент",
    "инструменты клей": "инструмент",
    "инструменты химия": "инструмент",
    "импульсный лабораторный источник питания": "инструмент",
    "другое": "прочее",
    "прочий": "прочее",
    "прочая": "прочее",
    "прочие": "прочее",
    "другое аксессуары": "аксессуары",
    "аксессуары инструменты": "аксессуары",
    "аксессуары инструменты клей": "клей",
    "аксессуары инструменты клей химия": "клей",
    "аксессуары инструменты химия": "химия",
    "аксессуар": "аксессуары",
    "инструменты": "инструмент",
    "клей химия": "клей",
    "клей/химия": "клей",
    "зарядное устройство": "зарядка",
    "зарядные устройства": "зарядка",
    "зарядный устройство": "зарядка",
    "зарядный устройства": "зарядка",
    "батарейки": "батарейка",
    "элемент питания": "батарейка",
    "power bank": "пауэрбанк",
    "powerbank": "пауэрбанк",
    "power-bank": "пауэрбанк",
    "внешний аккумулятор": "пауэрбанк",
    "внешний акб": "пауэрбанк",
    "battery bank": "пауэрбанк",
    "переходник": "адаптер",
    "переходники": "адаптер",
    "адаптеры": "адаптер",
    "адаптер для карт памяти": "адаптер для карты памяти",
    "portable charger": "пауэрбанк",
    "battery case": "чехол",
    "power case": "чехол",
    "виброзвонок": "вибромотор",
    "вибромоторчик": "вибромотор",
    "вибродвигатель": "вибромотор",
    "vibration motor": "вибромотор",
    "vibrator": "вибромотор",
    "taptic": "вибромотор",
    "taptic engine": "вибромотор",
    "звонок": "динамик",
    "динамик звуковой": "динамик",
    "звуковой динамик": "динамик",
    "динамик звуковой динамик": "динамик",
    "звонок полифонический": "динамик",
    "buzzer": "динамик",
    "ringer": "динамик",
    "loudspeaker": "динамик",
    "speaker": "динамик",
    "сим карта": "sim карта",
    "sim card": "sim карта",
    "скотч аккумулятора": "скотч",
    "скотч аккумулятора универсальный": "скотч",
    "тестер акб": "тестер",
    "тестер аккумулятора": "тестер",
    "внешний аксессуар": "винты",
    "внешний аксессуар инструмент": "винты",
    "маркер": "маркеры",
    "маркер для рисования печатных плат": "маркеры",
    "нагревательный элемент": "нагреватель",
    "крепеж": "крепежи",
    "крепёж": "крепежи",
    "крепеж аксессуары": "крепежи",
    "крепление": "крепежи",
    "крепление для устройства": "крепежи",
    "крепления": "крепежи",
    "крепления планки": "крепежи",
    "монопод": "штатив",
    "монопод трипод": "штатив",
    "трипод": "штатив",
    "tripod": "штатив",
    "monopod": "штатив",
    "вентилятор": "вентилятор",
    "вентиляторы": "вентилятор",
    "fan": "вентилятор",
    "cooler": "кулер",
    "кулеры": "кулер",
    "надевающаяся на корпус": "чехол",
    "надевающаяся на клавиатуру подложка": "чехол",
    "надевающаяся на кнопки клавиатуры": "чехол",
    "надевающийся на клавиатуру аксессуар": "чехол",
    "чехол аксессуары": "чехол",
    "чехол кейс": "чехол",
    "чехол крышка": "чехол",
    "чехол футляр": "чехол",
    "футляр": "чехол",
}

CATEGORY_GROUP_TOKENS: dict[str, tuple[str, ...]] = {
    "расходники": (
        "трафарет",
        "жало",
        "флюс",
        "паста",
        "термопаста",
        "термопроклад",
        "термоусад",
        "скотч",
        "термоскотч",
        "клей",
        "наклейк",
        "изоляц",
        "изолятор",
        "изоляция",
        "лента",
        "салфет",
        "маркер",
        "винт",
        "болт",
        "шуруп",
        "крепеж",
        "крепёж",
        "комплект болтов",
        "припой",
        "химия",
        "скотч",
    ),
    "инструменты": (
        "инструмент",
        "программатор",
        "programmer",
        "jcid",
        "паяльник",
        "паяльная станция",
        "термовоздушная",
        "мультиметр",
        "микроскоп",
        "луп",
        "нагреватель",
        "преднагреватель",
        "платформа",
        "воздуходувк",
        "организатор",
        "монтажный стол",
        "подставка",
        "оплетк",
        "станция",
        "тестер",
    ),
    "аксессуары": (
        "аксессуар",
        "держатель",
        "чехол",
        "кейс",
        "защитное стекло",
        "ремешок",
        "браслет",
        "умные часы",
        "наушник",
        "гарнитур",
        "tws",
        "earbuds",
        "headset",
        "bluetooth",
        "акустик",
        "колонк",
        "саундбар",
        "геймпад",
        "стилус",
        "монопод",
        "штатив",
        "трекер",
        "батарейк",
        "sim карта",
        "карта памяти",
        "накопитель",
        "ssd",
        "hdd",
        "оперативная память",
        "адаптер",
        "азу",
        "заряд",
        "зарядка",
        "зарядное устройство",
        "пауэрбанк",
        "power bank",
        "блок питания",
        "кабель",
        "провод",
        "футляр",
        "инвертер",
        "battery case",
        "power case",
        "вентилятор",
        "кулер",
    ),
}

GENERIC_LLM_CATEGORIES = {
    "аккумулятор",
    "батарейка",
    "плата",
    "чехол",
    "прочее",
    DEFAULT_FALLBACK_CATEGORY,
}

STRICT_RULE_CATEGORIES = {
    "вентилятор",
    "кулер",
    "штатив",
}

PROTECTIVE_GLASS_TOKENS = (
    "защитн",
    "tempered",
    "protector",
    "protective",
)

PROTECTIVE_GLASS_9H_RE = re.compile(r"\\b9h\\b", re.IGNORECASE)

CLEANING_ACTION_TOKENS = (
    "cleaning",
    "clean",
    "очист",
    "чистк",
)

CLEANER_PRODUCT_TOKENS = (
    "cleaner",
    "очиститель",
    "degreas",
    "обезжир",
    "solvent",
)

CLEANING_CHEMISTRY_TOKENS = (
    "жидк",
    "спрей",
    "spray",
    "gel",
    "гель",
    "solution",
    "resin",
)

CAMERA_GLASS_TOKENS = (
    "стекл",
    "glass",
)

CAMERA_CONTEXT_TOKENS = (
    "камер",
    "camera",
    "линз",
    "lens",
    "объектив",
)

DISPLAY_CONTEXT_BLOCKLIST = (
    "модулятор",
    "bluetooth",
    "bt-",
    "fm модул",
    "fm-модул",
    "backlight",
    "led-tv",
    "led tv",
    "телевизор",
    "tv",
)

DISPLAY_MODULE_TOKENS = (
    "дисплей",
    "display",
    "lcd",
    "oled",
    "amoled",
    "ips",
    "ltps",
    "ltpo",
    "incell",
    "in-cell",
)

LAPTOP_MATRIX_CONTEXT_TOKENS = (
    "ноутбук",
    "laptop",
    "notebook",
    "macbook",
    "thinkpad",
    "thinkbook",
    "ideapad",
    "zenbook",
    "vivobook",
    "swift",
    "predator",
    "nitro",
    "legion",
    "latitude",
    "inspiron",
    "pavilion",
    "elitebook",
    "matebook",
    "surface",
    "chromebook",
    "xps",
    "rog",
    "tuf",
)

LAPTOP_MATRIX_BLOCKLIST = (
    "led-tv",
    "led tv",
    "tv",
    "телевизор",
    "backlight",
    "подсветк",
)

LAPTOP_MATRIX_SIZE_RE = re.compile(r"\\b(?:11\\.6|12\\.5|13\\.3|14\\.0|15\\.6|17\\.3|18\\.4)\\b")
LAPTOP_PANEL_CODE_RE = re.compile(r"\\b(?:lp|ltn|n|b|nv|lm|hn|hb|ne)\\d{3}[a-z0-9]{2,}\\b")


def _looks_like_protective_glass(text: str) -> bool:
    if PROTECTIVE_GLASS_9H_RE.search(text):
        return True
    return any(token in text for token in PROTECTIVE_GLASS_TOKENS)


def _looks_like_cleaning_chemistry(text: str) -> bool:
    if any(token in text for token in CLEANER_PRODUCT_TOKENS):
        return True
    if any(token in text for token in CLEANING_ACTION_TOKENS) and any(
        token in text for token in CLEANING_CHEMISTRY_TOKENS
    ):
        return True
    return False


def _looks_like_camera_glass(text: str) -> bool:
    if not any(token in text for token in CAMERA_GLASS_TOKENS):
        return False
    return any(token in text for token in CAMERA_CONTEXT_TOKENS)


def _display_context_blocked(text: str) -> bool:
    if not any(token in text for token in CATEGORY_TOKENS["дисплей"]):
        return False
    return any(token in text for token in DISPLAY_CONTEXT_BLOCKLIST)


def _looks_like_laptop_matrix(text: str) -> bool:
    if any(token in text for token in LAPTOP_MATRIX_BLOCKLIST):
        return False
    if "mtx-" in text or "mtx " in text:
        return True
    if LAPTOP_PANEL_CODE_RE.search(text):
        return True
    has_matrix_keyword = any(token in text for token in ("матриц", "matrix", "panel", "lcd"))
    if not has_matrix_keyword:
        return False
    if any(token in text for token in LAPTOP_MATRIX_CONTEXT_TOKENS):
        return True
    if LAPTOP_MATRIX_SIZE_RE.search(text):
        return True
    return False


def _looks_like_connector_item(text: str) -> bool:
    stripped = text.lstrip()
    return stripped.startswith(("коннектор", "connector"))


def rule_classify(text: str) -> str | None:
    lower = text.lower()
    stripped = lower.lstrip()
    shleif_context_only = "без шлейф" in lower or "на шлейф" in lower
    has_explicit_shleif = "шлейф" in lower and not shleif_context_only
    if re.match(r"^(телефон|смартфон)\b", stripped) or stripped.startswith(
        ("smartphone", "mobile phone")
    ):
        return "смартфоны"
    if "sim" in lower or "сим" in lower:
        if "держател" in lower or "лоток" in lower or "tray" in lower:
            return "держатель сим-карты"
    if "тачпад" in lower or "touchpad" in lower or "trackpad" in lower or "трекпад" in lower:
        return "тачпад"
    if "штатив" in lower:
        return "штатив"
    if (
        "задняя крышк" in lower
        or "крышка задняя" in lower
        or "back cover" in lower
        or "rear cover" in lower
        or "battery cover" in lower
    ):
        return "крышка"
    if "уплотнител" in lower or "прокладк" in lower:
        return "изолятор"
    if "коврик" in lower or "мат" in lower or "mat" in lower:
        return "инструмент"
    if "термопроклад" in lower:
        return "изолятор"
    if "губк" in lower and any(tok in lower for tok in ("очист", "чистк")):
        return "салфетки"
    if "камера беспыльн" in lower:
        return "инструмент"
    if "подсветк" in lower or "backlight" in lower:
        return "подсветка"
    if "шуруповерт" in lower or "шуруповёрт" in lower or "дрель" in lower or "drill" in lower:
        return "инструмент"
    if "рамк" in lower:
        if "камер" in lower:
            return "стекло камеры"
        if "переклей" in lower or "ремонт" in lower:
            return "инструмент"
        if "сенсорн" in lower or "диспле" in lower:
            return "корпус"
    if "разветвител" in lower or "usb hub" in lower or "usb-хаб" in lower or "хаб" in lower:
        return "адаптер"
    if (
        "разъем sim" in lower
        or "разъём sim" in lower
        or "разъем сим" in lower
        or "разъём сим" in lower
    ):
        return "держатель сим-карты"
    if "nfc" in lower or "rfid" in lower:
        if "крышк" in lower:
            return "крышка"
        if "шлейф" in lower and not shleif_context_only:
            return "шлейф"
        if (
            re.search(r"\bмодул[ья]\s*(nfc|rfid)\b", lower)
            or re.search(r"\b(nfc|rfid)\s*модул[ья]\b", lower)
            or "nfc module" in lower
            or "rfid module" in lower
            or "бесконтактн модул" in lower
        ):
            return "модуль nfc"
    if "коврик" in lower and any(tok in lower for tok in ("пайк", "подогрев", "нагрев")):
        return "нагреватель"
    if (
        ("держател" in lower and ("сим" in lower or "sim" in lower))
        or "лоток sim" in lower
        or "лоток сим" in lower
        or "сим лоток" in lower
        or "сим-лоток" in lower
        or "sim tray" in lower
        or "sim-card tray" in lower
        or "sim card tray" in lower
        or "sim holder" in lower
        or "sim-card holder" in lower
        or "sim card holder" in lower
        or "sim slot" in lower
    ):
        return "держатель сим-карты"
    if lower.lstrip().startswith("нижняя плата"):
        return "плата"
    if "форма дисплея" in lower:
        return "инструмент"
    if "сепаратор" in lower:
        return "сепаратор"
    if "машинк" in lower and "тест" in lower:
        return "тестер"
    if "форма" in lower and any(tok in lower for tok in ("склеив", "рамк", "стекл")):
        return "инструмент"
    if "подсветк" in lower:
        return "подсветка"
    if lower.lstrip().startswith("защитное стекло"):
        return "защитное стекло"
    if (
        "ремешок" in lower
        or "bracelet" in lower
        or "watch band" in lower
        or "smart band" in lower
        or "strap" in lower
        or "геймпад" in lower
    ):
        return "прочее"
    if re.search(r"\\b(fpc|flex)\\b", lower):
        if "коннектор" in lower or "connector" in lower:
            return "коннектор"
        if "cable" in lower or "шлейф" in lower:
            return "шлейф"
    if lower.lstrip().startswith("шлейф"):
        if any(tok in lower for tok in DISPLAY_MODULE_TOKENS) or "дисплей" in lower:
            return "дисплей"
        if "крышк" in lower:
            return "крышка"
        if "джойстик" in lower:
            return "джойстик"
        return "шлейф"
    if "монопод" in lower or "трипод" in lower or "tripod" in lower or "monopod" in lower:
        return "штатив"
    if "коннектор" in lower and (
        "тачскрин" in lower or "touchscreen" in lower or "touch screen" in lower
    ):
        return "коннектор"
    if "кулер" in lower or re.search(r"\\bcooler\\b", lower):
        return "кулер"
    if "вентилятор" in lower or re.search(r"\\bfan\\b", lower):
        return "вентилятор"
    if "aixun" in lower and ("pm02" in lower or "pm-02" in lower or "pm 02" in lower):
        return "инструмент"
    if "держатель плат" in lower or "держатель платы" in lower or "монтажный стол" in lower:
        return "инструмент"
    if "держател" in lower:
        return "держатель для смартфона"
    if "накладк" in lower:
        return "накладка"
    if "изолятор" in lower:
        return "изолятор"
    if "картридер" in lower or "card reader" in lower:
        return "адаптер"
    if "jcid" in lower:
        if any(tok in lower for tok in ("кабель", "провод", "шнур")):
            return "кабель"
        if (
            any(tok in lower for tok in DISPLAY_MODULE_TOKENS)
            or any(tok in lower for tok in CATEGORY_TOKENS["дисплей"])
            or "lcd" in lower
            or "oled" in lower
        ):
            return "дисплей"
        if "микросхем" in lower:
            return "микросхема"
        if "компаунд" in lower or "compound" in lower:
            return "химия"
    if (("led" in lower or "светодиод" in lower) and "smd" in lower) or "led-smd" in lower:
        return "подсветка"
    if "светодиодная подсветка" in lower or "подсветка для телевизоров" in lower:
        return "подсветка"
    if "брелок" in lower or "keychain" in lower:
        return "брелок"
    if "нож" in lower or "knife" in lower:
        return "ножи"
    if "очк" in lower and "защит" in lower:
        return "инструмент"
    if "дымоулавливател" in lower or "дымоуловител" in lower:
        return "инструмент"
    if "муляж" in lower:
        return "прочее"
    if "сплав" in lower and ("вуд" in lower or "роз" in lower):
        return "припой"
    if "припой" in lower:
        return "припой"
    if any(tok in lower for tok in CATEGORY_TOKENS["флюс"]):
        return "флюс"
    if any(tok in lower for tok in CATEGORY_TOKENS["салфетки"]):
        return "салфетки"
    if any(tok in lower for tok in CATEGORY_TOKENS["скотч"]):
        if lower.lstrip().startswith(("аккумулятор", "акб")):
            return "аккумулятор"
        return "скотч"
    if any(tok in lower for tok in CATEGORY_TOKENS["пыльник"]):
        return "пыльник"
    if "комплект" in lower and "болт" in lower:
        return "болты"
    if (
        re.search(r"\bкле[йея]\b", lower) or "glue" in lower or "adhesive" in lower
    ) and "переклей" not in lower:
        if lower.lstrip().startswith("клей") or "клей uv" in lower or "uv" in lower:
            return "клей"
        if _looks_like_protective_glass(lower):
            return "защитное стекло"
        return "клей"
    if any(tok in lower for tok in CATEGORY_TOKENS["наклейка"]):
        return "наклейка"
    if any(tok in lower for tok in CATEGORY_TOKENS["химия"]):
        return "химия"
    if _looks_like_cleaning_chemistry(lower):
        return "химия"
    if any(tok in lower for tok in CATEGORY_TOKENS["лента"]):
        return "лента"
    if any(tok in lower for tok in CATEGORY_TOKENS["батарейка"]):
        return "батарейка"
    if "azu" in lower or "азу" in lower:
        return "зарядка"
    if any(tok in lower for tok in CATEGORY_TOKENS["пауэрбанк"]):
        return "пауэрбанк"
    if ("ячейк" in lower or "банка" in lower) and (
        "аккумулятор" in lower
        or "акб" in lower
        or "iphone" in lower
        or "ipad" in lower
        or "samsung" in lower
        or "xiaomi" in lower
        or "huawei" in lower
        or "honor" in lower
        or re.search(r"\\bmah\\b", lower)
        or "li-" in lower
    ):
        return "аккумулятор"
    if any(tok in lower for tok in CATEGORY_TOKENS["контроллер питания"]):
        return "контроллер питания"
    if any(tok in lower for tok in CATEGORY_TOKENS["модуль nfc"]):
        return "модуль nfc"
    has_batt_abbrev = re.search(r"\\bbatt\\b", lower) is not None
    if "аккумулятор" in lower or "акб" in lower:
        if (
            re.search(r"\b(винт|шуруп|крепеж|крепёж)\b", lower)
            and "шуруповерт" not in lower
            and "шуруповёрт" not in lower
        ):
            return "винты"
        if "плата" in lower:
            if "тест" in lower:
                return "тестер"
            return "плата"
        if "адаптер" in lower:
            return "адаптер"
        if "скотч" in lower or "проклейк" in lower:
            return "скотч"
    if any(tok in lower for tok in CATEGORY_TOKENS["аккумулятор"]) or has_batt_abbrev:
        if any(tok in lower for tok in CATEGORY_TOKENS["чехол"]):
            return "чехол"
        if any(tok in lower for tok in CATEGORY_TOKENS["зарядка"]) or lower.startswith(
            ("зарядн", "charger")
        ):
            return "зарядка"
        if any(tok in lower for tok in CATEGORY_TOKENS["кабель"]):
            return "кабель"
        if any(tok in lower for tok in CATEGORY_TOKENS["гарнитура"]):
            return "гарнитура"
        if any(tok in lower for tok in CATEGORY_TOKENS["колонка"]):
            return "колонка"
        if any(tok in lower for tok in CATEGORY_TOKENS["тестер"]):
            return "тестер"
        if any(tok in lower for tok in CATEGORY_TOKENS["программатор"]):
            return "программатор"
        if any(tok in lower for tok in CATEGORY_TOKENS["батарейка"]):
            return "батарейка"
        if any(tok in lower for tok in CATEGORY_TOKENS["пауэрбанк"]):
            return "пауэрбанк"
        if (
            not has_explicit_shleif
            and not re.search(r"\\bflex\\b[-\\s]*cable", lower)
            and not any(tok in lower for tok in CATEGORY_TOKENS["адаптер"])
            and not _looks_like_connector_item(lower)
        ):
            return "аккумулятор"
    scanner_block = "one touch" in lower or "touch idol" in lower
    if not scanner_block and any(tok in lower for tok in CATEGORY_TOKENS["сканер отпечатка"]):
        return "сканер отпечатка"
    if "держател" in lower:
        return "держатель для смартфона"
    if "органайзер" in lower:
        return "органайзер"
    if "подставк" in lower:
        if "крышк" in lower:
            return "крышка"
        if any(tok in lower for tok in ("отвертк", "скальпел", "набор", "насадк")):
            return "инструмент"
        if any(tok in lower for tok in ("кабель", "usb", "type-c", "type c", "lightning")):
            return "кабель"
        if "dock" in lower or "docking station" in lower or "разветвител" in lower:
            return "адаптер"
    if (
        "док станц" in lower
        or "док-станц" in lower
        or "dock station" in lower
        or ("dock" in lower and "charg" in lower)
    ):
        return "зарядка"
    if "подставк" in lower:
        return "подставка"
    if "ножк" in lower:
        return "ножки"
    if "наклад" in lower and "стик" in lower:
        return "накладка"
    if "надевающ" in lower and ("клавиатур" in lower or "кнопк" in lower or "корпус" in lower):
        return "чехол"
    if any(tok in lower for tok in CATEGORY_TOKENS["чехол"]):
        return "чехол"
    if ("флип" in lower or "flip" in lower) and (
        "крышк" in lower or "cover" in lower or "case" in lower
    ):
        return "чехол"
    if any(tok in lower for tok in CATEGORY_TOKENS["гарнитура"]):
        return "гарнитура"
    if any(tok in lower for tok in CATEGORY_TOKENS["колонка"]):
        return "колонка"
    if any(tok in lower for tok in CATEGORY_TOKENS["нагреватель"]):
        return "нагреватель"
    if any(tok in lower for tok in CATEGORY_TOKENS["инструмент"]):
        return "инструмент"
    if any(tok in lower for tok in CATEGORY_TOKENS["тестер"]):
        return "тестер"
    if any(tok in lower for tok in CATEGORY_TOKENS["инвертер"]):
        return "инвертер"
    if any(tok in lower for tok in CATEGORY_TOKENS["программатор"]):
        return "программатор"
    if any(tok in lower for tok in CATEGORY_TOKENS["шлейф"]) and not shleif_context_only:
        if "коннектор" in lower or "connector" in lower:
            return "коннектор"
        if any(tok in lower for tok in DISPLAY_MODULE_TOKENS) or "дисплей" in lower:
            return "дисплей"
        if "крышк" in lower:
            return "крышка"
        if "джойстик" in lower:
            return "джойстик"
        return "шлейф"
    if any(tok in lower for tok in CATEGORY_TOKENS["клавиатура"]):
        return "клавиатура"
    if any(tok in lower for tok in CATEGORY_TOKENS["кнопки"]):
        return "кнопки"
    if any(tok in lower for tok in CATEGORY_TOKENS["плата"]):
        return "плата"
    if any(tok in lower for tok in CATEGORY_TOKENS["крышка"]):
        return "крышка"
    if any(tok in lower for tok in CATEGORY_TOKENS["корпус"]):
        return "корпус"
    if _looks_like_laptop_matrix(lower):
        return "матрица для ноутбука"
    if ("тачскрин" in lower or "touchscreen" in lower or "touch screen" in lower) and (
        any(tok in lower for tok in DISPLAY_MODULE_TOKENS)
        or any(tok in lower for tok in CATEGORY_TOKENS["дисплей"])
    ):
        return "дисплей"
    if any(tok in lower for tok in CATEGORY_TOKENS["тачскрин"]):
        if "тачскрин" in lower or "touchscreen" in lower or "touch screen" in lower:
            return "тачскрин"
        if not any(tok in lower for tok in DISPLAY_MODULE_TOKENS):
            return "тачскрин"
    if _looks_like_camera_glass(lower):
        return "стекло камеры"
    if "oca" in lower and ("пленк" in lower or "film" in lower):
        return "пленка oca"
    if "oca" in lower and "стекл" in lower:
        return "стекло для переклейки"
    if "screen protector" in lower and not any(
        tok in lower for tok in ("стекл", "glass", "tempered", "9h")
    ):
        return "пленка"
    if any(tok in lower for tok in CATEGORY_TOKENS["пленка"]):
        return "пленка"
    if _looks_like_protective_glass(lower):
        return "защитное стекло"
    if any(tok in lower for tok in CATEGORY_TOKENS["стекло для переклейки"]) or "стекл" in lower:
        return "стекло для переклейки"
    if any(tok in lower for tok in CATEGORY_TOKENS["зарядка"]) or lower.startswith(
        ("зарядн", "charger")
    ):
        return "зарядка"
    if any(tok in lower for tok in CATEGORY_TOKENS["кабель"]):
        if lower.startswith(("кабель", "провод", "шнур")) or (
            "аккумулятор" not in lower and "акб" not in lower
        ):
            return "кабель"
    if any(tok in lower for tok in CATEGORY_TOKENS["камера"]):
        return "камера"
    if any(tok in lower for tok in CATEGORY_TOKENS["дисплей"]):
        if _display_context_blocked(lower):
            return "прочее"
        return "дисплей"
    if any(tok in lower for tok in CATEGORY_TOKENS["антенна"]):
        return "антенна"
    if any(tok in lower for tok in CATEGORY_TOKENS["адаптер"]):
        return "адаптер"
    if any(tok in lower for tok in CATEGORY_TOKENS["коннектор"]):
        return "коннектор"
    if "сетка" in lower and "динамик" in lower:
        return "сетка динамика"
    if "наклад" in lower and "стик" in lower:
        return "накладка"
    if "разъем" in lower or "разъём" in lower:
        if lower.startswith(("разъем", "разъём")):
            return "разъем"
        if "аккумулятор" not in lower and "акб" not in lower:
            return "разъем"
    for category, tokens in CATEGORY_TOKENS.items():
        if category in {"дисплей", "камера", "аккумулятор"}:
            continue
        if any(tok in lower for tok in tokens):
            return category
    return None


def _normalize_category_value(value: object) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).strip().lower().split())
    if not text:
        return None
    text = re.sub(r"[^0-9a-zа-яё\\-\\s]+", " ", text, flags=re.IGNORECASE)
    text = " ".join(text.split())
    if not text or len(text) > 60:
        return None
    return text


def canonicalize_category(value: object) -> str | None:
    normalized = _normalize_category_value(value)
    if not normalized:
        return None
    if normalized in UNKNOWN_CATEGORY_VALUES:
        return None
    return CATEGORY_ALIASES.get(normalized, normalized)


def category_group(value: object) -> str | None:
    category = canonicalize_category(value)
    if not category or category == DEFAULT_FALLBACK_CATEGORY:
        return None
    for group, tokens in CATEGORY_GROUP_TOKENS.items():
        if any(tok in category for tok in tokens):
            return group
    return "запчасти"


def llm_classify(client: httpx.Client, base_url: str, model: str, name: str) -> str | None:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": get_llm_competitor_category_prompt()},
            {"role": "user", "content": name},
        ],
        "temperature": 0.0,
        "max_tokens": 50,
    }
    resp = client.post(
        f"{base_url}/v1/chat/completions",
        json=payload,
        timeout=LLM_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    data = resp.json()
    content = data["choices"][0]["message"]["content"]
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return None
    category = canonicalize_category(parsed.get("category"))
    if not category:
        return None
    if category in ALLOWED_CATEGORIES:
        return category
    return category


class CategoryClassifier:
    def __init__(
        self,
        *,
        base_url: str | None,
        model: str | None,
        use_llm: bool = True,
        llm_only: bool = False,
        force_llm: bool = False,
        llm_limit: int = 0,
        default_category: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/") if base_url else None
        self.model = model
        self.use_llm = use_llm
        self.llm_only = llm_only
        self.force_llm = force_llm
        self.llm_limit = llm_limit
        self.default_category = default_category
        self.llm_calls = 0
        self.llm_failed = 0
        self._cache: dict[str, str | None] = {}
        self._client: httpx.Client | None = None
        if self.use_llm and self.base_url and self.model:
            self._client = httpx.Client(
                timeout=LLM_TIMEOUT_SECONDS,
                trust_env=not _is_localhost_url(self.base_url),
            )
        elif self.use_llm:
            logger.warning(
                "LLM category classify requested but LOCAL_LLM_BASE_URL or LOCAL_LLM_CHAT_MODEL not set"
            )

    @classmethod
    def from_env(
        cls,
        *,
        use_llm: bool = True,
        llm_only: bool = False,
        force_llm: bool = False,
        llm_limit: int = 0,
        default_category: str | None = None,
    ) -> CategoryClassifier:
        settings = get_settings()
        fallback = default_category
        if fallback is None:
            fallback = os.environ.get(
                "COMPETITOR_CATEGORY_DEFAULT", DEFAULT_FALLBACK_CATEGORY
            ).strip()
            if not fallback:
                fallback = None
        base_url = os.environ.get("LOCAL_LLM_BASE_URL") or settings.local_llm_base_url
        model = os.environ.get("LOCAL_LLM_CHAT_MODEL") or settings.local_llm_chat_model
        return cls(
            base_url=base_url,
            model=model,
            use_llm=use_llm,
            llm_only=llm_only,
            force_llm=force_llm,
            llm_limit=llm_limit,
            default_category=fallback,
        )

    def classify(self, name: str | None) -> str | None:
        if not name:
            return self.default_category
        key = " ".join(name.strip().lower().split())
        if key in self._cache:
            return self._cache[key]
        rule_category = None if self.llm_only else rule_classify(name)
        category = None
        should_call_llm = self.force_llm or rule_category in {None, "дисплей"}
        if (
            should_call_llm
            and self._client
            and (self.llm_limit == 0 or self.llm_calls < self.llm_limit)
        ):
            self.llm_calls += 1
            try:
                category = llm_classify(self._client, self.base_url or "", self.model or "", name)
            except Exception:
                self.llm_failed += 1
                logger.exception("LLM category classify failed")
                category = None
        if rule_category in STRICT_RULE_CATEGORIES:
            category = rule_category
        if category is None and rule_category:
            category = rule_category
        if rule_category and category in GENERIC_LLM_CATEGORIES and rule_category != "дисплей":
            category = rule_category
        if category is None:
            category = self.default_category
        self._cache[key] = category
        return category

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> CategoryClassifier:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
