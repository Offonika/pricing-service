from __future__ import annotations

from app.services.competitor_category import canonicalize_category

KIND_SMARTPHONE_DISPLAYS = "Дисплеи/сенсор/стекло"
KIND_SMARTPHONE_CAMERAS = "Камеры"
KIND_SMARTPHONE_AUDIO = "Акустика/вибро"
KIND_SMARTPHONE_FLEX = "Шлейфы/разъёмы/мелкие узлы"
KIND_BOARDS = "Платы и электронные компоненты"
KIND_POWER = "Питание и зарядка (розница + сервис)"
KIND_BODY = "Корпусные части и крепёж"
KIND_LAPTOP = "Ноутбук/ПК — запчасти и узлы"
KIND_CONSUMABLES = "Расходные материалы ремонта"
KIND_TOOLS = "Инструменты и оборудование (ремонт)"
KIND_ACCESSORIES = "Аксессуары (розничные товары)"
KIND_DEVICES = "Готовые устройства"
KIND_OTHER = "Прочее / Не классифицировано"

SMARTPHONE_DISPLAY_SUBJECTS = {
    "дисплей",
    "тачскрин",
    "подсветка",
    "стекло для переклейки",
}
SMARTPHONE_CAMERA_SUBJECTS = {
    "камера",
    "стекло камеры",
}
SMARTPHONE_AUDIO_SUBJECTS = {
    "динамик",
    "микрофон",
    "вибромотор",
    "сетка динамика",
}
SMARTPHONE_FLEX_SUBJECTS = {
    "шлейф",
    "разъем",
    "коннектор",
    "держатель сим-карты",
    "антенна",
    "сканер отпечатка",
    "кнопки",
    "изолятор",
    "магнит",
    "наклейка",
    "лента",
}
BOARDS_SUBJECTS = {
    "материнская плата",
    "плата",
    "микросхема",
    "контроллер питания",
}
POWER_SUBJECTS = {
    "аккумулятор",
    "батарейка",
    "зарядка",
    "кабель",
    "пауэрбанк",
    "адаптер",
}
BODY_SUBJECTS = {
    "корпус",
    "крышка",
    "винты",
    "крепежи",
    "ножки",
    "вентилятор",
    "фильтр",
}
LAPTOP_SUBJECTS = {
    "матрица для ноутбука",
    "клавиатура",
    "тачпад",
}
CONSUMABLE_SUBJECTS = {
    "клей",
    "скотч",
    "припой",
    "флюс",
    "химия",
    "жидкость для очистки",
    "салфетки",
    "пленка",
    "пленка oca",
    "упаковка",
}
TOOLS_SUBJECTS = {
    "инструмент",
    "микроскоп",
    "нагреватель",
    "сепаратор",
    "силомоновый ролик",
    "тестер",
    "программатор",
    "трафарет",
    "ножи",
}
ACCESSORY_SUBJECTS = {
    "чехол",
    "карта памяти",
    "стилус",
    "держатель для смартфона",
    "защитное стекло",
    "гарнитура",
}
DEVICE_SUBJECTS = {"смартфоны"}
UNKNOWN_SUBJECTS = {
    "",
    "-",
    "нет",
    "не определено",
    "неизвестно",
    "н/д",
    "unknown",
    "undefined",
    "прочее",
}
LAPTOP_CONTEXT_TOKENS = (
    "macbook",
    "laptop",
    "notebook",
    "thinkpad",
    "lenovo",
    "dell",
    "hp",
    "acer",
    "asus",
    "msi",
    "surface",
    "chromebook",
    "ideapad",
    "zenbook",
    "vivobook",
    "xps",
    "latitude",
    "inspiron",
    "pavilion",
    "elitebook",
    "probook",
    "matebook",
)


def nomenclature_kind(subject: str | None, name: str | None = None) -> str | None:
    if subject is None:
        return None
    normalized = canonicalize_category(subject) or subject
    lower = " ".join(str(normalized).strip().lower().split())
    if not lower or lower in UNKNOWN_SUBJECTS:
        return KIND_OTHER

    if lower in SMARTPHONE_DISPLAY_SUBJECTS:
        return KIND_SMARTPHONE_DISPLAYS
    if lower in SMARTPHONE_CAMERA_SUBJECTS:
        return KIND_SMARTPHONE_CAMERAS
    if lower in SMARTPHONE_AUDIO_SUBJECTS:
        return KIND_SMARTPHONE_AUDIO
    if lower in SMARTPHONE_FLEX_SUBJECTS:
        return KIND_SMARTPHONE_FLEX
    if lower in BOARDS_SUBJECTS:
        return KIND_BOARDS
    if lower in POWER_SUBJECTS:
        return KIND_POWER

    if lower == "вентилятор" and name:
        name_lower = name.lower()
        if any(token in name_lower for token in LAPTOP_CONTEXT_TOKENS):
            return KIND_LAPTOP
    if lower in LAPTOP_SUBJECTS:
        return KIND_LAPTOP
    if lower in BODY_SUBJECTS:
        return KIND_BODY

    if lower in CONSUMABLE_SUBJECTS:
        return KIND_CONSUMABLES
    if lower in TOOLS_SUBJECTS:
        return KIND_TOOLS
    if lower in ACCESSORY_SUBJECTS:
        return KIND_ACCESSORIES
    if lower in DEVICE_SUBJECTS:
        return KIND_DEVICES

    return KIND_OTHER
