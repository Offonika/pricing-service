from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

DEFAULT_PARSE_PROMPT = """
Ты — помощник для нормализации товаров конкурентов по смартфонам.
На входе строка вида "Дисплей для <бренд> <модель> ..." (может быть на русском).
Нужно ответить кратким JSON {"brand": "...", "model": "...", "variant": "..."}
variant можно опустить или вернуть null, если его нет (например, Mini/Plus/Pro/Ultra/5G).
Используй общепринятые названия на английском: "Samsung", "Galaxy A54 5G", "iPhone 14 Pro Max".
Не добавляй пояснений — только JSON.
""".strip()


@lru_cache(maxsize=1)
def get_llm_parse_prompt() -> str:
    override_text = os.environ.get("PROMPT_LLM_PARSE_TEXT")
    if override_text:
        return override_text.strip()
    override_file = os.environ.get("PROMPT_LLM_PARSE_FILE")
    if override_file:
        path = Path(override_file)
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    default_path = Path("config/prompts/llm_parse_phone_model.txt")
    if default_path.exists():
        return default_path.read_text(encoding="utf-8").strip()
    return DEFAULT_PARSE_PROMPT


DEFAULT_ITEM_TYPE_PROMPT = """
Ты — помощник для классификации товаров конкурентов по смартфонам.
Определи тип предмета из фиксированного набора:
- display
- battery
- camera
- flex
- housing
- connector
- cable
- board
- other

Ответ строго JSON: {"item_type": "<одно из значений списка>"} без пояснений.
""".strip()


@lru_cache(maxsize=1)
def get_llm_item_type_prompt() -> str:
    override_text = os.environ.get("PROMPT_LLM_ITEM_TYPE_TEXT")
    if override_text:
        return override_text.strip()
    override_file = os.environ.get("PROMPT_LLM_ITEM_TYPE_FILE")
    if override_file:
        path = Path(override_file)
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    default_path = Path("config/prompts/llm_item_type.txt")
    if default_path.exists():
        return default_path.read_text(encoding="utf-8").strip()
    return DEFAULT_ITEM_TYPE_PROMPT


DEFAULT_COMPETITOR_CATEGORY_PROMPT = """
Ты — помощник для классификации товаров конкурентов по запчастям смартфонов.
Определи категорию товара. Если подходит одна из базовых — используй её:
- скотч
- наклейка
- клей
- химия
- лента
- пыльник
- дисплей
- тачскрин
- матрица для ноутбука
- аккумулятор
- батарейка
- пауэрбанк
- камера
- стекло камеры
- защитное стекло
- стекло для переклейки
- пленка
- пленка oca
- сетка динамика
- клавиатура
- кнопки
- сканер отпечатка
- шлейф
- корпус
- держатель для смартфона
- накладка
- ножки
- подставка
- органайзер
- чехол
- гарнитура
- колонка
- коннектор
- адаптер
- разъем
- кабель
- зарядка
- инструмент
- тестер
- инвертер
- плата
- прочее
- неизвестно

Правила:
- Если ни одна базовая категория не подходит — верни краткое название предмета (1–2 слова),
  например: "динамик", "микрофон", "сим-лоток", "питание", "гарнитура", "микросхема", "трафарет",
  "программатор", "вибромотор".
- Не используй "прочее", если предмет можно назвать конкретно.
- "неизвестно" используй только если из названия вообще нельзя понять предмет или оно пустое.
- Не используй слова "аксессуары", "инструменты", "запчасти" как категорию — это группы.
- Не выбирай "плата", если предмет — микросхема/процессор/контроллер, программатор или инструмент/расходник.
- Если в названии есть "скотч", "наклейка", "лента", "клей", "кабель", "зарядное устройство",
  "чехол", "гарнитура", "колонка", "инструмент", "плата", "коннектор", "адаптер", "шлейф" или "батарейка" —
  не ставь "аккумулятор".

Подсказки:
- дисплей: экран, LCD/OLED/AMOLED, модуль, дисплейный модуль.
- тачскрин: тачскрин, сенсорный экран, digitizer, touch panel.
- матрица для ноутбука: матрица для ноутбука, laptop/notebook, panel LP156/B156/N156, MTX-.
- аккумулятор: аккумулятор, battery, акб.
- батарейка: батарейка, CR2032/CR2450, LR03/LR6, AA/AAA, alkaline, zinc air, coin cell.
- пауэрбанк: power bank, powerbank, внешний аккумулятор.
- камера: камера, линза, объектив.
- стекло камеры: стекло камеры, стекло основной камеры, camera glass, lens glass.
- защитное стекло: защитное стекло, tempered glass, screen protector, 9H.
- стекло для переклейки: стекло для переклейки, стекло для ремонта, outer glass, glass only.
- пленка: пленка, protective film, hydrogel film.
- пленка oca: oca пленка, oca film.
- сетка динамика: сетка динамика, сетка для динамика, speaker mesh.
- химия: очиститель, обезжириватель, cleaner, cleaning solution, спрей, жидкость, гель.
- пыльник: пыльник, dust cover, dust cap.
- клавиатура: клавиатура, keyboard, kbd.
- кнопки: кнопка, клавиша, button, key, power key, volume key.
- сканер отпечатка: сканер отпечатка пальца, датчик отпечатка, fingerprint, touch id.
- шлейф: шлейф, flex, кнопка, датчик.
- корпус: корпус, крышка, задняя крышка, средняя часть, рамка, бампер, задняя панель, back cover.
- чехол: чехол, battery case, power case.
- гарнитура: гарнитура, наушники, headset, TWS.
- колонка: колонка, акустика, саундбар.
- коннектор: коннектор, connector.
- держатель для смартфона: держатель, holder, mount, stand.
- накладка: накладка на стик, thumb grip, stick grip.
- ножки: ножки, rubber feet, feet pad.
- подставка: подставка, dock, док-станция.
- органайзер: органайзер, organizer.
- адаптер: адаптер, переходник, adapter.
- разъем: разъем/разъём, порт.
- кабель: кабель, шнур, провод.
- зарядка: зарядное устройство, charger, ЗУ, АЗУ.
- плата: плата, board, PCB, субплата, материнская плата.
- микросхема: IC, кристалл, контроллер питания, чип, NAND/Flash.
- программатор: программатор/програматор, JCID, программатор для iPhone, плата для программатора.
- нагреватель: нагревательный элемент, подогрев, нагреватель/нагревательный стол, hot plate, heating mat.
- скотч: скотч, термоскотч.
- наклейка: наклейка, изоляция, изолятор, стикер.
- клей: клей, adhesive, glue.
- лента: лента, никелированная лента.
- тестер: тестер, измеритель, battery tester.
- инвертер: инвертер, inverter.
- вибромотор: вибромотор, виброзвонок, taptic engine.
- динамик: динамик, звуковой динамик, звонок, buzzer, ringer.
- трафарет: BGA трафарет, stencil.
- карта памяти: microSD, SD, память.
- накопитель: SSD, HDD, накопитель.
- зарядка: зарядное устройство, ЗУ, АЗУ.
- оперативная память: DDR, SODIMM, память для ноутбука.
- расходники: флюс, паста, жало, скотч, клей, термопрокладка, термоусадка, винты, болты.
- прочее: аксессуары/инструменты, если нельзя назвать предмет конкретно.

Ответ строго JSON: {"category": "<категория>"} без пояснений.
""".strip()


@lru_cache(maxsize=1)
def get_llm_competitor_category_prompt() -> str:
    override_text = os.environ.get("PROMPT_LLM_COMPETITOR_CATEGORY_TEXT")
    if override_text:
        return override_text.strip()
    override_file = os.environ.get("PROMPT_LLM_COMPETITOR_CATEGORY_FILE")
    if override_file:
        path = Path(override_file)
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    default_path = Path("config/prompts/llm_competitor_category.txt")
    if default_path.exists():
        return default_path.read_text(encoding="utf-8").strip()
    return DEFAULT_COMPETITOR_CATEGORY_PROMPT


DEFAULT_COMPETITOR_ATTRS_PROMPT = """
Ты — помощник для нормализации товаров конкурентов по запчастям смартфонов.
По названию нужно вернуть JSON по схеме:
{
  "item_type": "display|battery|camera|flex|housing|connector|cable|board|other",
  "normalized_title": "<краткое нормализованное название без лишних слов>",
  "attrs": {
    "brand": "...",
    "model": "...",
    "variant": "...",
    "color": "...",
    "capacity": "...",
    "size_inch": "...",
    "type": "...",
    "quality": "...",
    "construction": "...",
    "refresh_rate_hz": 0,
    "screen_matrix_type": "...",
    "screen_kit": "...",
    "backlight": "...",
    "screen_construction": "...",
    "screen_quality_grade": "...",
    "oleophobic": null,
    "has_frame": null
  },
  "confidence": 0.0,
  "uncertain_fields": ["field1", "field2"]
}
attrs может быть пустым объектом. confidence в диапазоне 0..1.
Ответ строго JSON, без пояснений.

Правила:
- Не выдумывай значения: бери только из названия.
- Если не уверен — возвращай UNKNOWN/null.
- item_type выбирай из списка.
- normalized_title делай коротким и понятным (без мусорных слов, скидок, комплектов).
- attrs.brand указывай только если в названии есть бренд товара/аксессуара/запчасти
  (например, HOCO/OR/LP). Не используй бренд устройства.
- attrs.model/variant указывай только если есть модель устройства в названии.
- attrs.type:
  - для connector/cable: тип порта (type-c, micro-usb, lightning);
  - для остальных категорий оставляй пустым.
- attrs.type/quality/construction/refresh_rate_hz:
  - для display не заполняй (используй screen_* поля);
  - для connector/cable можно заполнять type;
  - для остальных категорий оставляй пустым.
- attrs.screen_matrix_type (только display): значение из списка:
  LCD_TFT, LCD_IPS, LTPS_LCD, OLED, AMOLED, LTPO_AMOLED, UNKNOWN.
- attrs.screen_kit (только display): значение из списка:
  DISPLAY_ONLY, DISPLAY_WITH_TOUCH, DISPLAY_WITH_FRAME, DISPLAY_TOUCH_FRAME, UNKNOWN.
- attrs.backlight (только display): значение из списка:
  WITH_BACKLIGHT, BRIGHT_BACKLIGHT, NO_BACKLIGHT, UNKNOWN.
- attrs.screen_construction (только display): значение из списка:
  HARD_OLED, SOFT_OLED, INCELL, ONCELL, COF, COG, UNKNOWN.
- attrs.screen_quality_grade (только display): значение из списка:
  ORIGINAL, ORIGINAL_REFURB, OEM, GX, OR, OR100, PREMIUM, AAA, HQ, FIRST_CLASS, COPY_HIGH, COPY_MEDIUM, COPY_LOW, UNKNOWN.
- attrs.refresh_rate_hz (только display): целое число 60/90/120/144, если явно указано.
- attrs.oleophobic (только display): true/false/null.
- attrs.has_frame (только display): true/false/null.
- attrs.capacity: только для battery (например, "5000 mAh").
- attrs.size_inch: только если явно указан размер экрана.
- attrs.color: только если явно указан.
""".strip()


@lru_cache(maxsize=1)
def get_llm_competitor_attrs_prompt() -> str:
    override_text = os.environ.get("PROMPT_LLM_COMPETITOR_ATTRS_TEXT")
    if override_text:
        return override_text.strip()
    override_file = os.environ.get("PROMPT_LLM_COMPETITOR_ATTRS_FILE")
    if override_file:
        path = Path(override_file)
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    default_path = Path("config/prompts/llm_competitor_attrs.txt")
    if default_path.exists():
        return default_path.read_text(encoding="utf-8").strip()
    return DEFAULT_COMPETITOR_ATTRS_PROMPT


DEFAULT_MATCH_ARBITER_PROMPT = """
Ты — помощник для финального выбора товара из списка кандидатов.
Твоя задача: выбрать один product_id, который лучше всего соответствует товару конкурента.
Ответ строго JSON:
{"product_id": <int>, "confidence": 0.0, "rationale": "..." }
confidence от 0 до 1. Если нет уверенности, выбери наиболее близкий.
""".strip()


@lru_cache(maxsize=1)
def get_llm_match_arbiter_prompt() -> str:
    override_text = os.environ.get("PROMPT_LLM_MATCH_ARBITER_TEXT")
    if override_text:
        return override_text.strip()
    override_file = os.environ.get("PROMPT_LLM_MATCH_ARBITER_FILE")
    if override_file:
        path = Path(override_file)
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    default_path = Path("config/prompts/llm_match_arbiter.txt")
    if default_path.exists():
        return default_path.read_text(encoding="utf-8").strip()
    return DEFAULT_MATCH_ARBITER_PROMPT
