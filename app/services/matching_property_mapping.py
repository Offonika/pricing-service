from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import CompetitorItem, Product
from app.models.matching_property_mapping import (
    MatchingPropertyProfile,
    MatchingPropertyRule,
    MatchingPropertyRuleAudit,
    MatchingPropertyValueMap,
)
from app.services.matching_attributes import (
    COLOR_ALIASES,
    QUALITY_ALIASES,
    competitor_attribute,
    display_value,
    normalize_mapping_text,
    product_attribute,
)

STATUS_MATCH = "match"
STATUS_MISSING = "missing"
STATUS_CONFLICT = "conflict"
STATUS_UNMAPPED = "unmapped"

DEFAULT_PROFILES = [
    {"code": "display", "name": "Дисплеи", "item_type": "display", "sort_order": 10},
    {"code": "battery", "name": "Аккумуляторы", "item_type": "battery", "sort_order": 20},
    {"code": "cable", "name": "Кабели/зарядки", "item_type": "cable", "sort_order": 30},
    {"code": "camera", "name": "Камеры", "item_type": "camera", "sort_order": 40},
    {"code": "flex", "name": "Шлейфы", "item_type": "flex", "sort_order": 50},
    {"code": "housing", "name": "Корпусные детали", "item_type": "housing", "sort_order": 60},
]

DEFAULT_RULES = {
    "display": [
        (
            "model",
            "Модель",
            "compatibility.model",
            "compatibility.model",
            "set_overlap",
            "block",
            10,
            {},
        ),
        ("subject", "Предмет", "subject", "subject", "exact", "block", 20, {}),
        (
            "quality",
            "Качество",
            "display.quality",
            "display.quality",
            "mapped_value",
            "review",
            30,
            {},
        ),
        ("color", "Цвет", "display.color", "display.color", "mapped_value", "review", 40, {}),
        (
            "has_frame",
            "В рамке",
            "display.has_frame",
            "display.has_frame",
            "boolean",
            "review",
            50,
            {},
        ),
        (
            "has_touch",
            "Тачскрин",
            "display.has_touch",
            "display.has_touch",
            "boolean",
            "review",
            60,
            {},
        ),
        (
            "type",
            "Тип матрицы",
            "display.type",
            "display.type",
            "mapped_value",
            "review",
            70,
            {},
        ),
        (
            "construction",
            "Конструкция",
            "display.construction",
            "display.construction",
            "mapped_value",
            "review",
            80,
            {},
        ),
        (
            "backlight",
            "Подсветка",
            "display.backlight",
            "display.backlight",
            "exact",
            "hint",
            90,
            {},
        ),
        (
            "refresh_rate",
            "Частота",
            "display.refresh_rate_hz",
            "display.refresh_rate_hz",
            "numeric_tolerance",
            "hint",
            100,
            {"tolerance": 0},
        ),
    ],
    "battery": [
        (
            "model",
            "Совместимость модели",
            "compatibility.model",
            "compatibility.model",
            "set_overlap",
            "block",
            10,
            {},
        ),
        ("subject", "Предмет", "subject", "subject", "exact", "block", 20, {}),
        (
            "capacity",
            "Ёмкость",
            "battery.capacity_mah",
            "battery.capacity_mah",
            "numeric_tolerance",
            "review",
            30,
            {"tolerance": 200},
        ),
    ],
    "cable": [
        ("subject", "Предмет", "subject", "subject", "exact", "block", 10, {}),
        (
            "connector",
            "Разъём",
            "connector.type",
            "connector.type",
            "mapped_value",
            "review",
            20,
            {},
        ),
    ],
    "camera": [
        (
            "model",
            "Совместимость модели",
            "compatibility.model",
            "compatibility.model",
            "set_overlap",
            "block",
            10,
            {},
        ),
        ("subject", "Предмет", "subject", "subject", "exact", "block", 20, {}),
    ],
    "flex": [
        (
            "model",
            "Совместимость модели",
            "compatibility.model",
            "compatibility.model",
            "set_overlap",
            "block",
            10,
            {},
        ),
        ("subject", "Предмет", "subject", "subject", "exact", "block", 20, {}),
    ],
    "housing": [
        (
            "model",
            "Совместимость модели",
            "compatibility.model",
            "compatibility.model",
            "set_overlap",
            "block",
            10,
            {},
        ),
        ("subject", "Предмет", "subject", "subject", "exact", "block", 20, {}),
        ("color", "Цвет", "display.color", "display.color", "mapped_value", "review", 30, {}),
    ],
}

DEFAULT_VALUE_MAPS = {
    ("display", "quality"): [
        (None, "Original", "Original", "Базовый словарь качества"),
        (None, "Original Refurbished", "Original Refurbished", "Базовый словарь качества"),
        (None, "OEM", "OEM", "Базовый словарь качества"),
        (None, "Copy High", "Copy High", "Базовый словарь качества"),
        (None, "Copy Medium", "Copy Medium", "Базовый словарь качества"),
        (None, "Copy Low", "Copy Low", "Базовый словарь качества"),
        ("moba", "or", "Original", "Текущий мапинг качества MOBA"),
        ("moba", "or100", "Original", "Текущий мапинг качества MOBA"),
        ("moba", "or (sp)", "Original", "Текущий мапинг качества MOBA"),
        ("moba", "стандарт", "Copy Medium", "Текущий мапинг качества MOBA"),
        ("moba", "оптима", "Copy Medium", "Текущий мапинг качества MOBA"),
        ("moba", "премиум", "Copy High", "Текущий мапинг качества MOBA"),
    ],
    ("display", "color"): [
        (None, alias, canonical, "Базовый словарь цветов")
        for canonical, aliases in COLOR_ALIASES.items()
        for alias in sorted(aliases | {canonical})
    ],
    ("housing", "color"): [
        (None, alias, canonical, "Базовый словарь цветов")
        for canonical, aliases in COLOR_ALIASES.items()
        for alias in sorted(aliases | {canonical})
    ],
    ("cable", "connector"): [
        (None, "USB-C", "USB-C", "Базовый словарь разъемов"),
        (None, "Type-C", "USB-C", "Базовый словарь разъемов"),
        (None, "type c", "USB-C", "Базовый словарь разъемов"),
        (None, "usb c", "USB-C", "Базовый словарь разъемов"),
        (None, "Lightning", "Lightning", "Базовый словарь разъемов"),
        (None, "Micro-USB", "Micro-USB", "Базовый словарь разъемов"),
        (None, "micro usb", "Micro-USB", "Базовый словарь разъемов"),
        (None, "USB-A", "USB-A", "Базовый словарь разъемов"),
        (None, "usb a", "USB-A", "Базовый словарь разъемов"),
    ],
    ("display", "type"): [
        (None, "LCD", "LCD", "Базовый словарь матриц"),
        (None, "IPS", "IPS", "Базовый словарь матриц"),
        (None, "TFT", "TFT", "Базовый словарь матриц"),
        (None, "OLED", "OLED", "Базовый словарь матриц"),
        (None, "AMOLED", "AMOLED", "Базовый словарь матриц"),
        (None, "LTPS", "LTPS", "Базовый словарь матриц"),
        (None, "incell", "IPS", "Базовый словарь матриц"),
        (None, "in-cell", "IPS", "Базовый словарь матриц"),
    ],
    ("display", "construction"): [
        (None, "In-Cell", "In-Cell", "Базовый словарь конструкции"),
        (None, "Incell", "In-Cell", "Базовый словарь конструкции"),
        (None, "On-Cell", "On-Cell", "Базовый словарь конструкции"),
        (None, "Oncell", "On-Cell", "Базовый словарь конструкции"),
        (None, "COF", "COF", "Базовый словарь конструкции"),
        (None, "COG", "COG", "Базовый словарь конструкции"),
        (None, "Hard OLED", "Hard OLED", "Базовый словарь конструкции"),
        (None, "Soft OLED", "Soft OLED", "Базовый словарь конструкции"),
    ],
}

DISPLAY_TYPE_SAFE_MAPS = {
    normalize_mapping_text(alias): mapped
    for _source, alias, mapped, _notes in DEFAULT_VALUE_MAPS[("display", "type")]
}
DISPLAY_CONSTRUCTION_SAFE_MAPS = {
    normalize_mapping_text(alias): mapped
    for _source, alias, mapped, _notes in DEFAULT_VALUE_MAPS[("display", "construction")]
}
CONNECTOR_SAFE_MAPS = {
    normalize_mapping_text(alias): mapped
    for _source, alias, mapped, _notes in DEFAULT_VALUE_MAPS[("cable", "connector")]
}
QUALITY_SAFE_VALUES = set(QUALITY_ALIASES.values()) | {
    mapped for _source, _alias, mapped, _notes in DEFAULT_VALUE_MAPS[("display", "quality")]
}
COLOR_SAFE_VALUES = set(COLOR_ALIASES)


@dataclass(frozen=True)
class DefaultRuleSpec:
    label: str
    product_field: str
    competitor_field: str
    comparison_mode: str
    severity: str
    sort_order: int
    config_json: dict[str, Any] | None


@dataclass(frozen=True)
class PropertyValueSuggestion:
    rule_id: int
    property_key: str
    competitor_source: str | None
    competitor_value: str
    count: int
    sample_competitor_item_id: int
    sample_name: str | None
    suggested_mapped_value: str | None = None
    safe_auto: bool = False
    safe_reason: str | None = None


@dataclass(frozen=True)
class AcceptSafePropertyValueMapsResult:
    created_count: int
    skipped_count: int
    created: list[PropertyValueSuggestion]


@dataclass(frozen=True)
class PropertySummary:
    total: int
    matched: int
    missing: int
    conflict: int
    unmapped: int
    status: str
    label: str
    conflicts: list[str]
    block_conflict: int = 0
    review_conflict: int = 0
    hint_conflict: int = 0


@dataclass(frozen=True)
class PropertyComparisonItem:
    property_key: str
    label: str
    product_value: object | None
    competitor_value: object | None
    mapped_value: object | None
    status: str
    severity: str
    comparison_mode: str


@dataclass(frozen=True)
class PropertyComparisonResult:
    profile_id: int
    profile_code: str
    profile_name: str
    summary: PropertySummary
    items: list[PropertyComparisonItem]


class DuplicateValueMapError(ValueError):
    pass


def _default_rule_index() -> dict[tuple[str, str], DefaultRuleSpec]:
    specs: dict[tuple[str, str], DefaultRuleSpec] = {}
    for profile_code, rules in DEFAULT_RULES.items():
        for (
            key,
            label,
            product_field,
            competitor_field,
            mode,
            severity,
            sort_order,
            config,
        ) in rules:
            specs[(profile_code, key)] = DefaultRuleSpec(
                label=label,
                product_field=product_field,
                competitor_field=competitor_field,
                comparison_mode=mode,
                severity=severity,
                sort_order=sort_order,
                config_json=config or None,
            )
    return specs


DEFAULT_RULE_INDEX = _default_rule_index()


def default_rule_spec(rule: MatchingPropertyRule) -> DefaultRuleSpec | None:
    profile = getattr(rule, "profile", None)
    profile_code = profile.code if profile else None
    if not profile_code:
        return None
    return DEFAULT_RULE_INDEX.get((profile_code, rule.property_key))


def rule_has_default_drift(rule: MatchingPropertyRule) -> bool:
    spec = default_rule_spec(rule)
    if spec is None:
        return False
    return any(
        (
            rule.label != spec.label,
            rule.product_field != spec.product_field,
            rule.competitor_field != spec.competitor_field,
            rule.comparison_mode != spec.comparison_mode,
            rule.severity != spec.severity,
            rule.sort_order != spec.sort_order,
            (rule.config_json or None) != spec.config_json,
        )
    )


def _profile_by_code(db: Session) -> dict[str, MatchingPropertyProfile]:
    profiles = db.execute(select(MatchingPropertyProfile)).scalars().all()
    return {profile.code: profile for profile in profiles}


def ensure_default_property_mapping(db: Session) -> None:
    profiles_by_code = _profile_by_code(db)
    created = False
    for profile_data in DEFAULT_PROFILES:
        profile = profiles_by_code.get(profile_data["code"])
        if profile is None:
            profile = MatchingPropertyProfile(**profile_data)
            db.add(profile)
            db.flush()
            profiles_by_code[profile.code] = profile
            created = True
        existing_rules = {
            rule.property_key: rule
            for rule in db.execute(
                select(MatchingPropertyRule).where(MatchingPropertyRule.profile_id == profile.id)
            ).scalars()
        }
        for (
            key,
            label,
            product_field,
            competitor_field,
            mode,
            severity,
            sort_order,
            config,
        ) in DEFAULT_RULES.get(profile.code, []):
            if key in existing_rules:
                rule = existing_rules[key]
                if _repair_legacy_model_rule(
                    rule,
                    label=label,
                    product_field=product_field,
                    competitor_field=competitor_field,
                    mode=mode,
                    severity=severity,
                    sort_order=sort_order,
                    config=config,
                ):
                    created = True
                if _repair_safe_default_rule(
                    rule,
                    profile_code=profile.code,
                    label=label,
                    product_field=product_field,
                    competitor_field=competitor_field,
                    mode=mode,
                    severity=severity,
                    sort_order=sort_order,
                    config=config,
                ):
                    created = True
                continue
            db.add(
                MatchingPropertyRule(
                    profile_id=profile.id,
                    property_key=key,
                    label=label,
                    product_field=product_field,
                    competitor_field=competitor_field,
                    comparison_mode=mode,
                    severity=severity,
                    sort_order=sort_order,
                    config_json=config or None,
                )
            )
            created = True
    created = _ensure_default_value_maps(db, profiles_by_code) or created
    if created:
        db.commit()


def _repair_legacy_model_rule(
    rule: MatchingPropertyRule,
    *,
    label: str,
    product_field: str,
    competitor_field: str,
    mode: str,
    severity: str,
    sort_order: int,
    config: dict[str, Any],
) -> bool:
    profile = getattr(rule, "profile", None)
    if profile is None or rule.property_key != "model":
        return False
    if product_field != "compatibility.model" or competitor_field != "compatibility.model":
        return False
    if (
        rule.label,
        rule.product_field,
        rule.competitor_field,
        rule.comparison_mode,
        rule.severity,
    ) != ("Модель", "display.model", "display.model", "exact", "block"):
        return False
    rule.label = label
    rule.product_field = product_field
    rule.competitor_field = competitor_field
    rule.comparison_mode = mode
    rule.severity = severity
    rule.sort_order = sort_order
    rule.config_json = config or None
    return True


def _repair_safe_default_rule(
    rule: MatchingPropertyRule,
    *,
    profile_code: str,
    label: str,
    product_field: str,
    competitor_field: str,
    mode: str,
    severity: str,
    sort_order: int,
    config: dict[str, Any],
) -> bool:
    if (profile_code, rule.property_key) not in {("display", "type"), ("display", "construction")}:
        return False
    if rule.product_field != product_field or rule.competitor_field != competitor_field:
        return False
    if rule.comparison_mode != "exact" or mode != "mapped_value":
        return False
    rule.label = label
    rule.comparison_mode = mode
    rule.severity = severity
    rule.sort_order = sort_order
    rule.config_json = config or None
    return True


def _ensure_default_value_maps(
    db: Session,
    profiles_by_code: dict[str, MatchingPropertyProfile],
) -> bool:
    rules = (
        db.execute(
            select(MatchingPropertyRule).where(
                MatchingPropertyRule.profile_id.in_(
                    [profile.id for profile in profiles_by_code.values()]
                )
            )
        )
        .scalars()
        .all()
    )
    rules_by_profile_key = {
        (rule.profile_id, rule.property_key): rule
        for rule in rules
        if rule.comparison_mode == "mapped_value"
    }
    created = False
    for (profile_code, property_key), entries in DEFAULT_VALUE_MAPS.items():
        profile = profiles_by_code.get(profile_code)
        if profile is None:
            continue
        rule = rules_by_profile_key.get((profile.id, property_key))
        if rule is None:
            continue
        existing = {
            (
                normalize_mapping_text(value_map.competitor_source),
                normalize_mapping_text(value_map.competitor_value),
            )
            for value_map in db.execute(
                select(MatchingPropertyValueMap).where(MatchingPropertyValueMap.rule_id == rule.id)
            ).scalars()
        }
        for source, competitor_value, mapped_value, notes in entries:
            key = (normalize_mapping_text(source), normalize_mapping_text(competitor_value))
            if key in existing:
                continue
            db.add(
                MatchingPropertyValueMap(
                    rule_id=rule.id,
                    competitor_source=source,
                    competitor_value=competitor_value,
                    mapped_value=mapped_value,
                    notes=notes,
                    is_active=True,
                )
            )
            existing.add(key)
            created = True
    return created


def list_profiles(db: Session) -> list[MatchingPropertyProfile]:
    ensure_default_property_mapping(db)
    return (
        db.execute(
            select(MatchingPropertyProfile).order_by(
                MatchingPropertyProfile.sort_order,
                MatchingPropertyProfile.id,
            )
        )
        .scalars()
        .all()
    )


def list_rules(
    db: Session,
    *,
    profile_id: int | None = None,
    profile_code: str | None = None,
) -> list[MatchingPropertyRule]:
    ensure_default_property_mapping(db)
    query = (
        select(MatchingPropertyRule)
        .options(selectinload(MatchingPropertyRule.profile))
        .order_by(MatchingPropertyRule.sort_order, MatchingPropertyRule.id)
    )
    if profile_id is not None:
        query = query.where(MatchingPropertyRule.profile_id == profile_id)
    if profile_code:
        query = query.join(MatchingPropertyProfile).where(
            MatchingPropertyProfile.code == profile_code
        )
    return db.execute(query).scalars().all()


def list_value_maps(
    db: Session,
    *,
    rule_id: int | None = None,
    profile_code: str | None = None,
) -> list[MatchingPropertyValueMap]:
    ensure_default_property_mapping(db)
    query = (
        select(MatchingPropertyValueMap)
        .options(
            selectinload(MatchingPropertyValueMap.rule).selectinload(MatchingPropertyRule.profile)
        )
        .order_by(MatchingPropertyValueMap.id)
    )
    if rule_id is not None:
        query = query.where(MatchingPropertyValueMap.rule_id == rule_id)
    if profile_code:
        query = (
            query.join(MatchingPropertyRule)
            .join(MatchingPropertyProfile)
            .where(MatchingPropertyProfile.code == profile_code)
        )
    return db.execute(query).scalars().all()


def _snapshot_rule(rule: MatchingPropertyRule) -> dict[str, object]:
    return {
        "profile_id": rule.profile_id,
        "property_key": rule.property_key,
        "label": rule.label,
        "product_field": rule.product_field,
        "competitor_field": rule.competitor_field,
        "comparison_mode": rule.comparison_mode,
        "severity": rule.severity,
        "config_json": rule.config_json,
        "sort_order": rule.sort_order,
        "is_active": rule.is_active,
    }


def audit_rule(
    db: Session,
    *,
    rule: MatchingPropertyRule,
    action: str,
    actor: str | None,
    before: dict[str, object] | None,
) -> None:
    db.add(
        MatchingPropertyRuleAudit(
            rule_id=rule.id,
            action=action,
            actor=actor,
            before_json=before,
            after_json=_snapshot_rule(rule),
        )
    )


def _resolve_profile(
    db: Session, profile_id: int | None, profile_code: str | None
) -> MatchingPropertyProfile:
    ensure_default_property_mapping(db)
    if profile_id is not None:
        profile = db.get(MatchingPropertyProfile, profile_id)
    elif profile_code:
        profile = db.scalar(
            select(MatchingPropertyProfile).where(MatchingPropertyProfile.code == profile_code)
        )
    else:
        profile = None
    if profile is None:
        raise ValueError("profile not found")
    return profile


def create_rule(
    db: Session,
    *,
    profile_id: int | None,
    profile_code: str | None,
    property_key: str,
    label: str,
    product_field: str,
    competitor_field: str,
    comparison_mode: str,
    severity: str,
    config_json: dict[str, Any] | None,
    sort_order: int,
    is_active: bool,
    actor: str | None,
) -> MatchingPropertyRule:
    profile = _resolve_profile(db, profile_id, profile_code)
    rule = MatchingPropertyRule(
        profile_id=profile.id,
        property_key=property_key,
        label=label,
        product_field=product_field,
        competitor_field=competitor_field,
        comparison_mode=comparison_mode,
        severity=severity,
        config_json=config_json,
        sort_order=sort_order,
        is_active=is_active,
    )
    db.add(rule)
    db.flush()
    audit_rule(db, rule=rule, action="create", actor=actor, before=None)
    db.commit()
    db.refresh(rule)
    return rule


def update_rule(
    db: Session,
    rule_id: int,
    *,
    actor: str | None,
    **updates: object,
) -> MatchingPropertyRule | None:
    rule = db.get(MatchingPropertyRule, rule_id)
    if rule is None:
        return None
    before = _snapshot_rule(rule)
    nullable_fields = {"config_json"}
    for key, value in updates.items():
        if value is not None or key in nullable_fields:
            setattr(rule, key, value)
    db.add(rule)
    db.flush()
    audit_rule(db, rule=rule, action="update", actor=actor, before=before)
    db.commit()
    db.refresh(rule)
    return rule


def restore_default_rule(
    db: Session,
    rule_id: int,
    *,
    actor: str | None,
) -> MatchingPropertyRule | None:
    rule = (
        db.execute(
            select(MatchingPropertyRule)
            .options(selectinload(MatchingPropertyRule.profile))
            .where(MatchingPropertyRule.id == rule_id)
        )
        .scalars()
        .first()
    )
    if rule is None:
        return None
    spec = default_rule_spec(rule)
    if spec is None:
        raise ValueError("default rule is not defined")
    before = _snapshot_rule(rule)
    rule.label = spec.label
    rule.product_field = spec.product_field
    rule.competitor_field = spec.competitor_field
    rule.comparison_mode = spec.comparison_mode
    rule.severity = spec.severity
    rule.sort_order = spec.sort_order
    rule.config_json = spec.config_json
    db.add(rule)
    db.flush()
    audit_rule(db, rule=rule, action="restore_default", actor=actor, before=before)
    db.commit()
    db.refresh(rule)
    return rule


def _clean_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


def _clean_required_text(value: str) -> str:
    return value.strip()


def _find_duplicate_value_map(
    db: Session,
    *,
    rule_id: int,
    competitor_source: str | None,
    competitor_value: str,
    exclude_id: int | None = None,
) -> MatchingPropertyValueMap | None:
    normalized_source = normalize_mapping_text(competitor_source)
    normalized_value = normalize_mapping_text(competitor_value)
    query = select(MatchingPropertyValueMap).where(MatchingPropertyValueMap.rule_id == rule_id)
    if exclude_id is not None:
        query = query.where(MatchingPropertyValueMap.id != exclude_id)
    for value_map in db.execute(query).scalars():
        if (
            normalize_mapping_text(value_map.competitor_source) == normalized_source
            and normalize_mapping_text(value_map.competitor_value) == normalized_value
        ):
            return value_map
    return None


def create_value_map(
    db: Session,
    *,
    rule_id: int,
    competitor_source: str | None,
    competitor_value: str,
    mapped_value: str,
    notes: str | None,
    is_active: bool,
) -> MatchingPropertyValueMap:
    rule = db.get(MatchingPropertyRule, rule_id)
    if rule is None:
        raise ValueError("rule not found")
    if "compatibility.model" in {rule.product_field, rule.competitor_field}:
        raise ValueError("compatibility model values are managed in compatibility mapping")
    competitor_source = _clean_optional_text(competitor_source)
    competitor_value = _clean_required_text(competitor_value)
    mapped_value = _clean_required_text(mapped_value)
    notes = _clean_optional_text(notes)
    if _find_duplicate_value_map(
        db,
        rule_id=rule_id,
        competitor_source=competitor_source,
        competitor_value=competitor_value,
    ):
        raise DuplicateValueMapError("property value map already exists")
    value_map = MatchingPropertyValueMap(
        rule_id=rule_id,
        competitor_source=competitor_source,
        competitor_value=competitor_value,
        mapped_value=mapped_value,
        notes=notes,
        is_active=is_active,
    )
    db.add(value_map)
    db.commit()
    db.refresh(value_map)
    return value_map


def update_value_map(
    db: Session,
    value_map_id: int,
    **updates: object,
) -> MatchingPropertyValueMap | None:
    value_map = db.get(MatchingPropertyValueMap, value_map_id)
    if value_map is None:
        return None
    rule = db.get(MatchingPropertyRule, value_map.rule_id)
    if rule and "compatibility.model" in {rule.product_field, rule.competitor_field}:
        raise ValueError("compatibility model values are managed in compatibility mapping")
    nullable_fields = {"competitor_source", "notes"}
    next_rule_id = int(updates.get("rule_id") or value_map.rule_id)
    next_source = (
        _clean_optional_text(updates["competitor_source"])
        if "competitor_source" in updates
        else value_map.competitor_source
    )
    next_value = (
        _clean_required_text(str(updates["competitor_value"]))
        if updates.get("competitor_value") is not None
        else value_map.competitor_value
    )
    if _find_duplicate_value_map(
        db,
        rule_id=next_rule_id,
        competitor_source=next_source,
        competitor_value=next_value,
        exclude_id=value_map.id,
    ):
        raise DuplicateValueMapError("property value map already exists")
    for key, value in updates.items():
        if value is None and key not in nullable_fields:
            continue
        if key in {"competitor_source", "notes"}:
            value = (
                _clean_optional_text(value) if isinstance(value, str) or value is None else value
            )
        elif key in {"competitor_value", "mapped_value"} and isinstance(value, str):
            value = _clean_required_text(value)
        setattr(value_map, key, value)
    db.add(value_map)
    db.commit()
    db.refresh(value_map)
    return value_map


def _safe_auto_value_for_rule(
    rule: MatchingPropertyRule,
    competitor_value: str,
) -> tuple[str | None, str | None]:
    normalized = normalize_mapping_text(competitor_value)
    profile_code = rule.profile.code if getattr(rule, "profile", None) else None
    if rule.product_field == "display.color" or rule.property_key in {"color", "finish"}:
        if normalized in COLOR_SAFE_VALUES:
            return normalized, "known_color"
    if profile_code == "display" and rule.property_key == "quality":
        if competitor_value in QUALITY_SAFE_VALUES:
            return competitor_value, "known_display_quality"
    if profile_code == "cable" and rule.property_key == "connector":
        mapped = CONNECTOR_SAFE_MAPS.get(normalized)
        if mapped:
            return mapped, "known_connector"
    if profile_code == "display" and rule.property_key == "type":
        mapped = DISPLAY_TYPE_SAFE_MAPS.get(normalized)
        if mapped:
            return mapped, "known_display_type"
    if profile_code == "display" and rule.property_key == "construction":
        mapped = DISPLAY_CONSTRUCTION_SAFE_MAPS.get(normalized)
        if mapped:
            return mapped, "known_display_construction"
    return None, None


def accept_safe_value_suggestions(
    db: Session,
    *,
    profile_code: str,
    rule_id: int | None = None,
    source: str | None = None,
    limit: int = 100,
) -> AcceptSafePropertyValueMapsResult:
    suggestions = list_value_suggestions(
        db,
        profile_code=profile_code,
        rule_id=rule_id,
        source=source,
        limit=limit,
    )
    created: list[PropertyValueSuggestion] = []
    skipped = 0
    for suggestion in suggestions:
        if not suggestion.safe_auto or not suggestion.suggested_mapped_value:
            skipped += 1
            continue
        try:
            create_value_map(
                db,
                rule_id=suggestion.rule_id,
                competitor_source=suggestion.competitor_source,
                competitor_value=suggestion.competitor_value,
                mapped_value=suggestion.suggested_mapped_value,
                notes=f"safe_auto:{suggestion.safe_reason}",
                is_active=True,
            )
        except DuplicateValueMapError:
            skipped += 1
            continue
        created.append(suggestion)
    return AcceptSafePropertyValueMapsResult(
        created_count=len(created),
        skipped_count=skipped,
        created=created,
    )


def _profile_for_pair(
    db: Session,
    product: Product,
    item: CompetitorItem,
    profile_code: str | None = None,
) -> MatchingPropertyProfile | None:
    ensure_default_property_mapping(db)
    if profile_code:
        return db.scalar(
            select(MatchingPropertyProfile).where(
                MatchingPropertyProfile.code == profile_code,
                MatchingPropertyProfile.is_active.is_(True),
            )
        )
    item_type = (item.item_type or "").strip().lower()
    product_text = " ".join(
        str(value or "").lower() for value in (product.name, product.category, product.subject)
    )
    if not item_type:
        if any(token in product_text for token in ("дисплей", "тачскрин", "lcd", "oled", "экран")):
            item_type = "display"
        elif any(token in product_text for token in ("аккумулятор", "акб", "battery")):
            item_type = "battery"
    return db.scalar(
        select(MatchingPropertyProfile)
        .where(
            MatchingPropertyProfile.item_type == item_type,
            MatchingPropertyProfile.is_active.is_(True),
        )
        .order_by(MatchingPropertyProfile.sort_order, MatchingPropertyProfile.id)
    )


def _mapped_value(
    rule: MatchingPropertyRule,
    item: CompetitorItem,
    competitor_value: object | None,
) -> object | None:
    if competitor_value is None:
        return None
    normalized_value = normalize_mapping_text(competitor_value)
    source = (item.competitor or "").casefold()
    global_match: object | None = None
    for value_map in rule.value_maps:
        if not value_map.is_active:
            continue
        map_source = (value_map.competitor_source or "").casefold()
        if map_source and map_source != source:
            continue
        if normalize_mapping_text(value_map.competitor_value) == normalized_value:
            if map_source:
                return value_map.mapped_value
            global_match = value_map.mapped_value
    return global_match if global_match is not None else competitor_value


def _as_set(value: object | None) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, (list, set, tuple)):
        return {normalize_mapping_text(item) for item in value if normalize_mapping_text(item)}
    return {normalize_mapping_text(value)}


def _as_decimal(value: object | None) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _as_bool(value: object | None) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)):
        return bool(value)
    normalized = normalize_mapping_text(value)
    if normalized in {"1", "true", "yes", "y", "да", "истина", "есть", "с"}:
        return True
    if normalized in {"0", "false", "no", "n", "нет", "ложь", "без", "none"}:
        return False
    return bool(normalized)


def _values_match(
    *,
    product_value: object | None,
    competitor_value: object | None,
    comparison_mode: str,
    tolerance: object | None,
) -> bool:
    if comparison_mode == "boolean":
        return _as_bool(product_value) == _as_bool(competitor_value)
    if comparison_mode == "set_overlap":
        return bool(_as_set(product_value) & _as_set(competitor_value))
    if comparison_mode == "numeric_tolerance":
        left = _as_decimal(product_value)
        right = _as_decimal(competitor_value)
        if left is None or right is None:
            return False
        threshold = _as_decimal(tolerance) or Decimal("0")
        return abs(left - right) <= threshold
    return normalize_mapping_text(product_value) == normalize_mapping_text(competitor_value)


def evaluate_property_comparison(
    db: Session,
    product: Product,
    item: CompetitorItem,
    *,
    profile_code: str | None = None,
) -> PropertyComparisonResult | None:
    profile = _profile_for_pair(db, product, item, profile_code=profile_code)
    if profile is None:
        return None
    rules = (
        db.execute(
            select(MatchingPropertyRule)
            .options(selectinload(MatchingPropertyRule.value_maps))
            .where(
                MatchingPropertyRule.profile_id == profile.id,
                MatchingPropertyRule.is_active.is_(True),
            )
            .order_by(MatchingPropertyRule.sort_order, MatchingPropertyRule.id)
        )
        .scalars()
        .all()
    )
    items: list[PropertyComparisonItem] = []
    for rule in rules:
        product_value = product_attribute(product, rule.product_field)
        competitor_value = competitor_attribute(item, rule.competitor_field)
        mapped_value = (
            _mapped_value(rule, item, competitor_value)
            if rule.comparison_mode == "mapped_value"
            else competitor_value
        )
        if not rule.product_field or not rule.competitor_field:
            status = STATUS_UNMAPPED
        elif product_value in (None, "", []) or competitor_value in (None, "", []):
            status = STATUS_MISSING
        elif _values_match(
            product_value=product_value,
            competitor_value=mapped_value,
            comparison_mode=rule.comparison_mode,
            tolerance=(rule.config_json or {}).get("tolerance"),
        ):
            status = STATUS_MATCH
        else:
            status = STATUS_CONFLICT
        items.append(
            PropertyComparisonItem(
                property_key=rule.property_key,
                label=rule.label,
                product_value=display_value(product_value),
                competitor_value=display_value(competitor_value),
                mapped_value=display_value(mapped_value),
                status=status,
                severity=rule.severity,
                comparison_mode=rule.comparison_mode,
            )
        )

    summary = build_summary(items)
    return PropertyComparisonResult(
        profile_id=profile.id,
        profile_code=profile.code,
        profile_name=profile.name,
        summary=summary,
        items=items,
    )


def build_summary(items: list[PropertyComparisonItem]) -> PropertySummary:
    total = len(items)
    matched = sum(1 for item in items if item.status == STATUS_MATCH)
    missing = sum(1 for item in items if item.status == STATUS_MISSING)
    conflict = sum(1 for item in items if item.status == STATUS_CONFLICT)
    unmapped = sum(1 for item in items if item.status == STATUS_UNMAPPED)
    conflicts = [item.label for item in items if item.status == STATUS_CONFLICT]
    block_conflict = sum(
        1 for item in items if item.status == STATUS_CONFLICT and item.severity == "block"
    )
    review_conflict = sum(
        1 for item in items if item.status == STATUS_CONFLICT and item.severity == "review"
    )
    hint_conflict = sum(
        1 for item in items if item.status == STATUS_CONFLICT and item.severity == "hint"
    )
    if conflict:
        status = STATUS_CONFLICT
        if block_conflict:
            label = f"Блокирующий конфликт: {', '.join(conflicts[:2])}"
        elif review_conflict:
            label = f"На проверку: {', '.join(conflicts[:2])}"
        else:
            label = f"Подсказка: {', '.join(conflicts[:2])}"
    elif missing:
        status = STATUS_MISSING
        label = f"Свойства {matched}/{total}"
    elif unmapped:
        status = STATUS_UNMAPPED
        label = "Есть ненастроенные правила"
    else:
        status = STATUS_MATCH
        label = f"Свойства {matched}/{total}"
    return PropertySummary(
        total=total,
        matched=matched,
        missing=missing,
        conflict=conflict,
        unmapped=unmapped,
        status=status,
        label=label,
        conflicts=conflicts,
        block_conflict=block_conflict,
        review_conflict=review_conflict,
        hint_conflict=hint_conflict,
    )


def list_value_suggestions(
    db: Session,
    *,
    profile_code: str,
    rule_id: int | None = None,
    source: str | None = None,
    limit: int = 100,
) -> list[PropertyValueSuggestion]:
    ensure_default_property_mapping(db)
    profile = db.scalar(
        select(MatchingPropertyProfile).where(
            MatchingPropertyProfile.code == profile_code,
            MatchingPropertyProfile.is_active.is_(True),
        )
    )
    if profile is None:
        raise ValueError("profile not found")
    rules_query = (
        select(MatchingPropertyRule)
        .options(selectinload(MatchingPropertyRule.profile))
        .where(
            MatchingPropertyRule.profile_id == profile.id,
            MatchingPropertyRule.is_active.is_(True),
        )
        .order_by(MatchingPropertyRule.sort_order, MatchingPropertyRule.id)
    )
    if rule_id is not None:
        rules_query = rules_query.where(MatchingPropertyRule.id == rule_id)
    rules = db.execute(rules_query).scalars().all()
    rules = [
        rule
        for rule in rules
        if rule.comparison_mode == "mapped_value" or rule.competitor_field == "compatibility.model"
    ]
    if not rules:
        return []

    existing_by_rule = {
        rule.id: {
            (
                normalize_mapping_text(value_map.competitor_source),
                normalize_mapping_text(value_map.competitor_value),
            )
            for value_map in db.execute(
                select(MatchingPropertyValueMap).where(
                    MatchingPropertyValueMap.rule_id == rule.id,
                    MatchingPropertyValueMap.is_active.is_(True),
                )
            ).scalars()
        }
        for rule in rules
    }
    source_filter = _clean_optional_text(source)
    items_query = select(CompetitorItem).where(CompetitorItem.is_active.is_(True))
    if profile.item_type:
        items_query = items_query.where(CompetitorItem.item_type == profile.item_type)
    if source_filter:
        items_query = items_query.where(CompetitorItem.competitor == source_filter)

    aggregates: dict[tuple[int, str, str], dict[str, object]] = {}
    for item in db.execute(items_query).scalars().yield_per(500):
        item_source = _clean_optional_text(item.competitor)
        normalized_source = normalize_mapping_text(item_source)
        for rule in rules:
            value = competitor_attribute(item, rule.competitor_field)
            for displayed in _iter_suggestion_values(value):
                normalized_value = normalize_mapping_text(displayed)
                mapped_keys = existing_by_rule.get(rule.id, set())
                if rule.comparison_mode == "mapped_value":
                    if (normalized_source, normalized_value) in mapped_keys:
                        continue
                    if ("", normalized_value) in mapped_keys:
                        continue
                key = (rule.id, normalized_source, normalized_value)
                entry = aggregates.get(key)
                if entry is None:
                    suggested_mapped_value, safe_reason = _safe_auto_value_for_rule(
                        rule,
                        displayed,
                    )
                    aggregates[key] = {
                        "rule_id": rule.id,
                        "property_key": rule.property_key,
                        "competitor_source": item_source,
                        "competitor_value": displayed,
                        "count": 1,
                        "sample_competitor_item_id": item.id,
                        "sample_name": item.name,
                        "suggested_mapped_value": suggested_mapped_value,
                        "safe_auto": suggested_mapped_value is not None,
                        "safe_reason": safe_reason,
                    }
                else:
                    entry["count"] = int(entry["count"]) + 1

    return [
        PropertyValueSuggestion(**entry)
        for entry in sorted(
            aggregates.values(),
            key=lambda item: (
                -int(item["count"]),
                str(item["property_key"]),
                str(item["competitor_value"]),
            ),
        )[:limit]
    ]


def _iter_suggestion_values(value: object | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, set, tuple)):
        values = [display_value(item) for item in value]
    else:
        values = [display_value(value)]
    return sorted({item for item in values if item})
