from __future__ import annotations

import re
from dataclasses import dataclass

from app.models import CompetitorItem, Product

COMPATIBILITY_TARGET_ITEM_TYPES = frozenset(
    {
        "display",
        "battery",
        "camera",
        "flex",
        "housing",
        "connector",
        "board",
    }
)

COMPATIBILITY_NON_TARGET_ITEM_TYPES = frozenset({"other", "cable"})

COMPATIBILITY_NOISE_TOKENS = (
    "автомобильное зарядное устройство",
    "беспроводное зарядное устройство",
    "зарядное устройство",
    "блок питания",
    "сетевой адаптер",
    "power bank",
    "powerbank",
    "зарядная станция",
    "внешний аккумулятор",
    "плата активации",
    "все модели",
    "пинцет",
    "трафарет",
    "паяльная станция",
    "термовоздушная",
    "микроскоп",
    "тепловизор",
    "наушник",
    "гарнитур",
    "bluetooth",
    "защитное стекло",
    "tempered glass",
    "screen protector",
    "чехол",
    "футляр",
    "скотч",
    "наклейк",
    "набор винтов",
)

PHONE_OR_TABLET_HINT_TOKENS = (
    "iphone",
    "ipad",
    "galaxy",
    "redmi",
    "poco",
    "xiaomi",
    "realme",
    "honor",
    "huawei",
    "pixel",
    "oneplus",
    "oppo",
    "vivo",
    "tecno",
    "infinix",
    "itel",
    "nokia",
    "sony",
    "motorola",
    "meizu",
    "zte",
    "nubia",
    "tcl",
    "lenovo tab",
    "tab ",
    "tablet",
    "планшет",
)

BRAND_ALIASES = {
    "apple": "apple",
    "iphone": "apple",
    "ipad": "apple",
    "samsung": "samsung",
    "galaxy": "samsung",
    "xiaomi": "xiaomi",
    "redmi": "xiaomi",
    "poco": "xiaomi",
    "huawei": "huawei_honor",
    "honor": "huawei_honor",
    "realme": "realme",
    "oppo": "oppo",
    "vivo": "vivo",
    "oneplus": "oneplus",
    "tecno": "tecno",
    "infinix": "infinix",
    "itel": "itel",
    "lenovo": "lenovo",
    "xiaoxin": "lenovo",
    "motorola": "motorola",
    "google": "google",
    "pixel": "google",
    "sony": "sony",
    "nokia": "nokia",
    "meizu": "meizu",
    "zte": "zte_nubia",
    "nubia": "zte_nubia",
    "tcl": "tcl",
    "asus": "asus",
    "rog": "asus",
}


def _text(*values: object | None) -> str:
    return " ".join(str(value) for value in values if value).lower().replace("ё", "е")


def brand_group(text: str | None) -> str | None:
    value = _text(text)
    if not value:
        return None
    for token in re.findall(r"[a-zа-я0-9]+", value):
        brand = BRAND_ALIASES.get(token)
        if brand:
            return brand
    return None


def brand_group_conflict(left: str | None, right: str | None) -> bool:
    left_brand = brand_group(left)
    right_brand = brand_group(right)
    return left_brand is not None and right_brand is not None and left_brand != right_brand


def device_group(text: str | None) -> str | None:
    value = _text(text)
    if not value:
        return None
    if any(
        token in value
        for token in (
            "nintendo",
            "switch",
            "playstation",
            "ps4",
            "ps5",
            "xbox",
            "rog ally",
            "legion go",
            "meta quest",
            "oculus",
            "quest 2",
            "quest 3",
            "quest 3s",
        )
    ):
        return "console"
    if any(
        token in value
        for token in (
            "macbook",
            "ноутбук",
            "ноутбука",
            "ноутбуков",
            "laptop",
            "notebook",
            "матрица ноут",
            "thinkpad",
            "pavilion",
            "satellite",
            "aspire",
            "extensa",
            "msi gl",
            "msi gp",
        )
    ):
        return "notebook"
    if "xiaoxin" not in value and re.search(
        r"\bideapad\s+(?:(?:[a-z0-9]+)\s*[- ]?)?1[34567][a-z0-9]*",
        value,
    ):
        return "notebook"
    if re.search(r"\bnp\d{3,4}[a-z0-9]*\b", value):
        return "notebook"
    if any(token in value for token in ("монитор", "monitor")):
        return "monitor"
    if any(
        token in value
        for token in (
            "ipad",
            "tablet",
            "планшет",
            "tablet pc",
            "tab ",
            " tab",
            "pad ",
            " pad",
            "p11",
            "tb-j",
            "tb3",
            "tb-",
            "legion y700",
        )
    ):
        return "tablet"
    if any(token in value for token in ("watch", "часы")):
        return "watch"
    if any(
        token in value for token in ("airpods", "наушник", "гарнитур", "tws", "bluetooth headset")
    ):
        return "headphones"
    if any(
        token in value
        for token in (
            "iphone",
            "galaxy",
            "redmi",
            "poco",
            "xiaomi",
            "realme",
            "honor",
            "huawei",
            "pixel",
            "oneplus",
            "oppo",
            "vivo",
            "tecno",
            "infinix",
            "itel",
            "xperia",
            "ideaphone",
            "zenfone",
            "rog phone",
            "blade",
            "motorola",
            "moto ",
            "nokia",
            "meizu",
            "zte",
            "nubia",
            "tcl",
        )
    ):
        return "phone"
    if "samsung" in value and re.search(r"\b(?:sm[-\s]?)?[agmfs]\d{2,4}[a-z]?\b", value):
        return "phone"
    return None


def device_group_conflict(left: str | None, right: str | None) -> bool:
    left_group = device_group(left)
    right_group = device_group(right)
    return left_group is not None and right_group is not None and left_group != right_group


def iphone_model_keys(text: str | None) -> set[str]:
    value = _text(text)
    keys: set[str] = set()
    if re.search(r"\biphone\s+air\b", value):
        keys.add("iphone_air")
    for match in re.finditer(
        r"\biphone\s+(\d{1,2}|x|xs|xr)(?:\s*(pro\s+max|pro|max|plus|mini|air|e))?\b",
        value,
    ):
        model, variant = match.groups()
        base = f"iphone_{model}"
        if variant:
            base = f"{base}_{variant.replace(' ', '_')}"
        keys.add(base)
    return keys


def _add_key_with_variant(keys: set[str], brand: str, model: str, variant: str | None) -> None:
    base = f"{brand}_{model.replace(' ', '_')}"
    if variant:
        base = f"{base}_{variant.replace('+', 'plus').replace(' ', '_')}"
    keys.add(base)


_SAMSUNG_MODEL_CODE_VARIANTS = {
    "g980": "s20",
    "g981": "s20",
    "g985": "s20_plus",
    "g986": "s20_plus",
    "g988": "s20_ultra",
    "g780": "s20_fe",
    "g781": "s20_fe",
}


def _normalize_samsung_code(raw_code: str) -> str:
    code = raw_code.lower()
    match = re.match(r"([a-z]\d{3,4})", code)
    return match.group(1) if match else code


def _add_samsung_code_key(keys: set[str], raw_code: str) -> None:
    code = _normalize_samsung_code(raw_code)
    keys.add(f"samsung_{code}")
    variant = _SAMSUNG_MODEL_CODE_VARIANTS.get(code)
    if variant:
        keys.add(f"samsung_{variant}")


def _add_samsung_s_model_key(keys: set[str], model: str, variant: str | None) -> None:
    normalized_variant = None
    if variant:
        normalized_variant = "plus" if variant == "+" else variant
    base = f"samsung_s{model}"
    if normalized_variant:
        base = f"{base}_{normalized_variant}"
    keys.add(base)


def phone_model_keys(text: str | None) -> set[str]:
    value = _text(text)
    keys = iphone_model_keys(value)

    for match in re.finditer(
        r"\bpoco\s+([a-z]\d{1,2}[a-z]?)(?:\s+(pro|max|plus|ultra|lite|se|gt))?",
        value,
    ):
        _add_key_with_variant(keys, "poco", match.group(1), match.group(2))

    for match in re.finditer(
        r"\bredmi\s+note\s+(\d{1,2}[a-z]?)(?:\s+(pro\+?|pro|max|plus|ultra|lite|se))?",
        value,
    ):
        _add_key_with_variant(keys, "redmi_note", match.group(1), match.group(2))

    for match in re.finditer(
        r"\brealme\s+(\d{1,2}[a-z]?)(?:\s+(pro\+?|pro|max|plus|ultra|lite|se|gt))?",
        value,
    ):
        _add_key_with_variant(keys, "realme", match.group(1), match.group(2))

    for match in re.finditer(
        r"\binfinix\s+(hot|note|smart)\s+(\d{1,2}i?)(?:\s+(pro\+?|pro|max|plus|ultra))?",
        value,
    ):
        line, model, variant = match.groups()
        _add_key_with_variant(keys, f"infinix_{line}", model, variant)

    for match in re.finditer(
        r"\btecno\s+(spark|camon)\s+(\d{1,2}[a-z]?)(?:\s+(pro|max|plus|ultra|go))?",
        value,
    ):
        line, model, variant = match.groups()
        _add_key_with_variant(keys, f"tecno_{line}", model, variant)

    for match in re.finditer(
        r"\bhuawei\s+nova\s+([a-z]?\d{1,3}[a-z]?)(?:\s+(pro|max|plus|ultra|lite|se))?",
        value,
    ):
        _add_key_with_variant(keys, "huawei_nova", match.group(1), match.group(2))

    for match in re.finditer(
        r"\boppo\s+a(\d{1,3}[a-z]?)(?:\s+(lite|pro|max|plus))?(?:\s+(4g|5g))?",
        value,
    ):
        model, variant, network = match.groups()
        _add_key_with_variant(keys, "oppo_a", model, variant)
        if network:
            _add_key_with_variant(keys, "oppo_a", model, network)

    for match in re.finditer(
        r"\boppo\s+([fkx]\d{1,3}[a-z]?)(?:\s+(lite|pro|max|plus))?(?:\s+(4g|5g))?",
        value,
    ):
        model, variant, network = match.groups()
        _add_key_with_variant(keys, "oppo", model, variant)
        if network:
            _add_key_with_variant(keys, "oppo", model, network)

    for match in re.finditer(
        r"\boppo\s+reno\s+(\d{1,2}[a-z]?)(?:\s+(lite|pro|max|plus|f|t))?(?:\s+(4g|5g))?",
        value,
    ):
        model, variant, network = match.groups()
        _add_key_with_variant(keys, "oppo_reno", model, variant)
        if network:
            _add_key_with_variant(keys, "oppo_reno", model, network)

    for match in re.finditer(
        r"\boppo\s+find\s+x(\d{1,2})(?:\s+(lite|pro|max|plus|ultra))?(?:\s+(4g|5g))?",
        value,
    ):
        model, variant, network = match.groups()
        _add_key_with_variant(keys, "oppo_find_x", model, variant)
        if network:
            _add_key_with_variant(keys, "oppo_find_x", model, network)

    for match in re.finditer(
        r"\b(?:zte\s+)?(?:nubia\s+)?red\s+magic\s+(\d{1,2}s?)"
        r"(?:\s+(lite|pro|max|plus|ultra))?(?:\s+(4g|5g))?",
        value,
    ):
        model, variant, network = match.groups()
        _add_key_with_variant(keys, "zte_nubia_red_magic", model, variant)
        if network:
            _add_key_with_variant(keys, "zte_nubia_red_magic", model, network)

    for match in re.finditer(
        r"\boneplus\s+(?!(?:nord|ace)\b)(\d{1,2}|x)([rt])?(?:\s+(pro|max|plus|ultra|lite|se))?",
        value,
    ):
        model, suffix, variant = match.groups()
        normalized_model = f"{model}{suffix or ''}"
        _add_key_with_variant(keys, "oneplus", normalized_model, variant)

    for match in re.finditer(
        r"\boneplus\s+nord\s+(?:ce\s*)?(\d{1,2})([rt])?(?:\s+(pro|max|plus|ultra|lite|se))?",
        value,
    ):
        model, suffix, variant = match.groups()
        normalized_model = f"{model}{suffix or ''}"
        prefix = "oneplus_nord_ce" if "nord ce" in match.group(0) else "oneplus_nord"
        _add_key_with_variant(keys, prefix, normalized_model, variant)

    for pattern in (
        r"\bsamsung\s+(?:galaxy\s+)?([agm]\d{3,4}[a-z]?)\b",
        r"\bgalaxy\s+([agm]\d{3,4}[a-z]?)\b",
        r"\bsm[-\s]?([agm]\d{3,4}[a-z]?)\b",
    ):
        for match in re.finditer(pattern, value):
            _add_samsung_code_key(keys, match.group(1))

    if "samsung" in value or "galaxy" in value:
        for match in re.finditer(r"\b([agm]\d{3,4}[a-z]?)\b", value):
            _add_samsung_code_key(keys, match.group(1))

    samsung_s_pattern = (
        r"\b(?:samsung\s+)?(?:galaxy\s+)?s(\d{1,2})" r"(?:(\+)|\s+(lite|fe|plus|ultra|edge))?(?!\w)"
    )
    for match in re.finditer(samsung_s_pattern, value):
        model, plus_variant, named_variant = match.groups()
        if "samsung" not in value and "galaxy" not in value:
            continue
        _add_samsung_s_model_key(keys, model, plus_variant or named_variant)

    for match in re.finditer(
        r"\b(?:samsung\s+)?galaxy\s+note\s+(\d{1,2})(?:\s+(lite|fe|plus|ultra))?\b",
        value,
    ):
        model, variant = match.groups()
        base = f"samsung_note_{model}"
        if variant:
            base = f"{base}_{variant}"
        keys.add(base)

    return keys


def strict_model_conflict(left: str | None, right: str | None) -> bool:
    left_keys = phone_model_keys(left)
    right_keys = phone_model_keys(right)
    if left_keys and right_keys:
        return left_keys.isdisjoint(right_keys)
    return False


def _drop_shadowed_variant_base_keys(keys: set[str]) -> set[str]:
    shadowed: set[str] = set()
    for key in keys:
        for other in keys:
            if other != key and other.startswith(f"{key}_"):
                shadowed.add(key)
    return keys - shadowed


def candidate_model_conflict(
    candidate_title: str | None,
    product_title: str | None,
    candidate_text: str | None,
    product_text_value: str | None,
) -> bool:
    candidate_title_keys = _drop_shadowed_variant_base_keys(phone_model_keys(candidate_title))
    product_title_keys = _drop_shadowed_variant_base_keys(phone_model_keys(product_title))
    if candidate_title_keys and product_title_keys:
        return candidate_title_keys.isdisjoint(product_title_keys)
    return strict_model_conflict(candidate_text, product_text_value)


def external_internal_display_conflict(left: str | None, right: str | None) -> bool:
    left_text = _text(left)
    right_text = _text(right)
    external_tokens = ("внешн", "external", "outer", "out ")
    internal_tokens = ("внутрен", "internal", "inner", "in ")
    left_external = any(token in left_text for token in external_tokens)
    right_external = any(token in right_text for token in external_tokens)
    left_internal = any(token in left_text for token in internal_tokens)
    right_internal = any(token in right_text for token in internal_tokens)
    return (left_external and right_internal) or (right_external and left_internal)


def display_module_component_conflict(candidate: str | None, product: str | None) -> bool:
    candidate_text = _text(candidate)
    product_text_value = _text(product)
    display_module_tokens = (
        "дисплей",
        "lcd",
        "oled",
        "amoled",
        "экран",
        "display",
    )
    component_tokens = (
        "g+oca",
        "oca",
        "стекло",
        "тачскрин",
        "touchscreen",
        "touch screen",
        "touch panel",
        "сенсорное стекло",
    )
    product_is_display_module = any(token in product_text_value for token in display_module_tokens)
    candidate_has_component = any(token in candidate_text for token in component_tokens)
    candidate_has_display_module = any(token in candidate_text for token in display_module_tokens)
    return (
        product_is_display_module and candidate_has_component and not candidate_has_display_module
    )


def catalog_family(text: str | None) -> str | None:
    value = _text(text)
    if not value:
        return None

    if re.search(r"\b(usb[-\s]*флеш|флешк|flash\s*drive|usb\s*flash)\b", value):
        return "usb_storage"
    if any(token in value for token in ("стекло камеры", "стекло задней камеры", "camera glass")):
        return "phone_camera_glass"
    if any(token in value for token in ("сеточка динамика", "сетка динамика", "speaker mesh")):
        return "phone_speaker_mesh"
    if any(
        token in value
        for token in (
            "прокладка передней камеры",
            "прокладка камеры",
            "датчика сенсора",
            "camera sensor gasket",
        )
    ):
        return "phone_camera_gasket"
    if any(token in value for token in ("держатель sim", "держатель сим", "sim tray")):
        return "phone_sim_tray"
    if any(token in value for token in ("винты", "винт ", "набор винтов", "screw")):
        return "phone_screws"
    if any(token in value for token in ("трафарет", "bga")):
        return "stencil"
    if any(
        token in value for token in ("микросхема", "аудио-контроллер", "контроллер", "pmic", "ic ")
    ):
        return "ic"
    if any(token in value for token in ("колодка теста", "isocket", "тест платы")):
        return "test_socket"
    if any(
        token in value
        for token in (
            "автодержатель",
            "автоадаптер",
            "bluetooth адаптер",
            "в автомобиль",
            "автомобиль",
        )
    ):
        return "adapter"
    if any(token in value for token in ("magsafe", "магнит magsafe")):
        return "magsafe"
    if any(token in value for token in ("праймер", "адгезии", "oca")) or re.search(
        r"\bклей\b", value
    ):
        return "adhesive"
    if any(token in value for token in ("средняя часть", "middle frame")):
        return "middle_frame"
    if any(token in value for token in ("защитное стекло", "tempered glass", "screen protector")):
        return "screen_protector"
    if any(
        token in value for token in ("airpods", "наушник", "гарнитур", "tws", "bluetooth headset")
    ):
        return "headphones"
    if any(
        token in value
        for token in ("микроскоп", "тепловизор", "паяльник", "станция", "отвертка", "screwdriver")
    ):
        return "tool"
    if any(token in value for token in ("клавиатур", "keyboard")):
        return "laptop_keyboard"
    if any(
        token in value for token in ("проверочного аппарата", "dl400", "тестер", "test fixture")
    ):
        return "test_fixture"
    if re.search(r"\b(rj[-\s]?45|ethernet)\b", value) and any(
        token in value for token in ("коннектор", "разъем", "разъём", "сквозной")
    ):
        return "network_connector"
    if any(token in value for token in ("патч-корд", "patch cord", "ethernet cable")):
        return "network_cable"
    if re.search(r"\busb[-\s]*a\b", value) and any(
        token in value for token in ("под пайку", "разъем", "разъём", "коннектор")
    ):
        return "component_connector"
    if any(token in value for token in ("автомобильн",)):
        return "adapter"
    if re.search(r"\b(6f22|6lr61|крона|9v1?)\b", value):
        return "battery_9v"
    if re.search(r"\b(lr20|d)\b", value):
        return "battery_d"
    if re.search(r"\b27a\b", value):
        return "battery_27a"
    if re.search(r"\b(cr20\d{2}|cr16\d{2}|cr12\d{2}|ag13|lr44h?|357a)\b", value):
        return "battery_coin"
    if re.search(r"\b(aaa|lr03)\b", value):
        return "battery_aaa"
    if re.search(r"\b(aa|lr6)\b", value):
        return "battery_aa"
    if re.search(
        r"(?:сетев\w*\s+)?зарядн\w*\s+устройств\w*\s+для\s+аккумулятор\w*",
        value,
    ) or (
        re.search(r"\b(?:makita|hitachi|greenworks|bosch)\b", value)
        and re.search(r"\b(?:12v|14,?4|18v|21v|24v|ni-cd|li-ion)\b", value)
        and "заряд" in value
    ):
        return "tool_battery_charger"
    if any(
        token in value
        for token in (
            "блок питания",
            "сетевой адаптер",
            "адаптер питания",
            "power supply",
            "зарядное устройство",
        )
    ):
        group = device_group(value)
        if group == "notebook":
            return "laptop_power_supply"
        if group == "console":
            return "console_power_supply"
        if group in {"phone", "tablet"}:
            return "phone_power_supply"
        return "power_supply"
    if any(token in value for token in ("дата-кабель", "кабель", "type-c", "typec", "lightning")):
        return "cable"

    group = device_group(value)
    if group == "notebook" and any(
        token in value for token in ("разъем", "разъём", "коннектор", "pj0")
    ):
        return "laptop_connector"
    if group == "notebook" and "шлейф" in value:
        return "laptop_flex"
    if group == "notebook" and any(token in value for token in ("крышка матрицы", "матрицы")):
        return "laptop_cover"
    if group == "notebook" and any(
        token in value
        for token in (
            "крышка матрицы",
            "матрицы",
            "петл",
            "клавиатур",
        )
    ):
        return "laptop_part"
    if group == "console" and any(
        token in value
        for token in ("шлейф", "разъем", "разъём", "крышка", "блок питания", "адаптер")
    ):
        return "console_part"
    return None


def catalog_family_conflict(left: str | None, right: str | None) -> bool:
    left_family = catalog_family(left)
    right_family = catalog_family(right)
    isolated_specific_families = {
        "usb_storage",
        "test_fixture",
        "adapter",
        "tool",
        "tool_battery_charger",
        "laptop_connector",
        "laptop_flex",
        "laptop_cover",
        "laptop_part",
        "console_part",
        "network_connector",
        "network_cable",
        "component_connector",
    }
    if not left_family or not right_family:
        return bool(
            left_family in isolated_specific_families or right_family in isolated_specific_families
        )
    if left_family == right_family:
        return False

    battery_families = {
        "battery_9v",
        "battery_d",
        "battery_27a",
        "battery_coin",
        "battery_aaa",
        "battery_aa",
    }
    if left_family in battery_families and right_family in battery_families:
        return True

    power_families = {"laptop_power_supply", "console_power_supply", "phone_power_supply"}
    if left_family in power_families and right_family in power_families:
        return True
    if (left_family in power_families and right_family == "power_supply") or (
        right_family in power_families and left_family == "power_supply"
    ):
        return True
    if (left_family in power_families and right_family == "cable") or (
        right_family in power_families and left_family == "cable"
    ):
        return True

    if left_family.startswith("laptop_") and right_family.startswith("laptop_"):
        return left_family != right_family
    if left_family.startswith("phone_") and right_family.startswith("phone_"):
        return left_family != right_family

    specific_families = {
        "usb_storage",
        "cable",
        "screen_protector",
        "headphones",
        "tool",
        "tool_battery_charger",
        "test_fixture",
        "test_socket",
        "adapter",
        "magsafe",
        "network_connector",
        "network_cable",
        "component_connector",
        "stencil",
        "ic",
        "adhesive",
        "middle_frame",
        "phone_camera_glass",
        "phone_camera_gasket",
        "phone_speaker_mesh",
        "phone_sim_tray",
        "phone_screws",
        "laptop_connector",
        "laptop_flex",
        "laptop_keyboard",
        "laptop_cover",
        "laptop_part",
        "console_part",
    }
    return left_family in specific_families or right_family in specific_families


@dataclass(frozen=True)
class CandidateGuardrailResult:
    allowed: bool
    reason: str | None = None


@dataclass(frozen=True)
class CompatibilityTargetResult:
    requires_compatibility: bool
    reason: str


def competitor_item_text(item: CompetitorItem) -> str:
    return _text(
        item.name,
        item.normalized_title,
        item.item_type,
        item.category,
        item.category_group,
        item.item_brand,
        item.attrs_model,
        item.parsed_device_brand,
        item.parsed_device_model,
        item.parsed_device_variant,
    )


def competitor_item_requires_compatibility(item: CompetitorItem) -> CompatibilityTargetResult:
    """Return whether missing compatibility should be treated as an actionable gap."""

    item_text = competitor_item_text(item)
    item_type = (item.item_type or "").strip().lower()
    group = device_group(item_text)

    if item_type in COMPATIBILITY_NON_TARGET_ITEM_TYPES:
        return CompatibilityTargetResult(False, f"non_target_item_type:{item_type}")
    if item_type not in COMPATIBILITY_TARGET_ITEM_TYPES:
        return CompatibilityTargetResult(False, "unknown_item_type")
    if group in {"notebook", "monitor", "watch"}:
        return CompatibilityTargetResult(False, f"non_phone_device_group:{group}")
    if any(token in item_text for token in COMPATIBILITY_NOISE_TOKENS):
        return CompatibilityTargetResult(False, "catalog_noise")
    if group in {"phone", "tablet"}:
        return CompatibilityTargetResult(True, "target_device_group")
    if any(token in item_text for token in PHONE_OR_TABLET_HINT_TOKENS):
        return CompatibilityTargetResult(True, "target_device_hint")
    return CompatibilityTargetResult(False, "no_phone_or_tablet_hint")


def product_text(product: Product) -> str:
    return _text(
        product.name,
        product.brand,
        product.category,
        product.subject,
        product.subject_1c,
        product.subject_generated,
    )


def _product_phone_model_ids(product: Product) -> set[int]:
    return {
        link.phone_model_id
        for link in getattr(product, "phone_model_links", []) or []
        if link.phone_model_id is not None
    }


def _item_phone_model_ids(item: CompetitorItem) -> set[int]:
    return {
        compat.phone_model_id
        for compat in getattr(item, "compatibilities", []) or []
        if compat.phone_model_id is not None
    }


def _compatibility_text(item: CompetitorItem) -> str:
    parts: list[object | None] = []
    for compat in getattr(item, "compatibilities", []) or []:
        parts.extend(
            [
                compat.device_brand,
                compat.device_brand_ref.code if getattr(compat, "device_brand_ref", None) else None,
                (
                    compat.device_brand_ref.group_code
                    if getattr(compat, "device_brand_ref", None)
                    else None
                ),
                compat.device_model,
                compat.device_variant,
                compat.phone_model.model_name if compat.phone_model else None,
                compat.phone_model.variant if compat.phone_model else None,
            ]
        )
    return _text(*parts)


def _product_compatibility_text(product: Product) -> str:
    parts: list[object | None] = [product_text(product)]
    for compat in getattr(product, "compatibilities", []) or []:
        parts.append(compat.value)
    for link in getattr(product, "phone_model_links", []) or []:
        if link.phone_model:
            parts.extend(
                [
                    link.phone_model.brand,
                    (
                        link.phone_model.device_brand.code
                        if getattr(link.phone_model, "device_brand", None)
                        else None
                    ),
                    (
                        link.phone_model.device_brand.group_code
                        if getattr(link.phone_model, "device_brand", None)
                        else None
                    ),
                    link.phone_model.model_name,
                    link.phone_model.variant,
                ]
            )
        else:
            parts.append(link.raw_value)
    return _text(*parts)


def basic_candidate_guardrails(item: CompetitorItem, product: Product) -> CandidateGuardrailResult:
    item_text = competitor_item_text(item)
    prod_text = product_text(product)
    item_title_text = _text(item.name, item.normalized_title, item.external_id)
    prod_title_text = _text(
        product.name,
        product.subject,
        product.subject_1c,
        product.subject_generated,
    )
    if device_group_conflict(item_text, prod_text):
        return CandidateGuardrailResult(False, "device_group_conflict")
    if catalog_family_conflict(item_text, prod_text):
        return CandidateGuardrailResult(False, "catalog_family_conflict")
    if brand_group_conflict(item_text, prod_text):
        return CandidateGuardrailResult(False, "brand_group_conflict")
    if candidate_model_conflict(item_title_text, prod_title_text, item_text, prod_text):
        return CandidateGuardrailResult(False, "strict_model_conflict")
    if external_internal_display_conflict(item_text, prod_text):
        return CandidateGuardrailResult(False, "external_internal_display_conflict")
    if display_module_component_conflict(item_title_text, prod_title_text):
        return CandidateGuardrailResult(False, "display_module_component_conflict")
    product_model_ids = _product_phone_model_ids(product)
    item_model_ids = _item_phone_model_ids(item)
    if product_model_ids and item_model_ids and product_model_ids.isdisjoint(item_model_ids):
        product_model_keys = phone_model_keys(_product_compatibility_text(product))
        item_model_keys = phone_model_keys(_text(item_title_text, _compatibility_text(item)))
        if not (product_model_keys and item_model_keys and product_model_keys & item_model_keys):
            return CandidateGuardrailResult(False, "compatibility_phone_model_conflict")
    compat_text = _compatibility_text(item)
    if compat_text:
        product_compat_text = _product_compatibility_text(product)
        if device_group_conflict(compat_text, product_compat_text):
            return CandidateGuardrailResult(False, "compatibility_device_group_conflict")
        if strict_model_conflict(compat_text, product_compat_text):
            return CandidateGuardrailResult(False, "compatibility_model_conflict")
    return CandidateGuardrailResult(True)
