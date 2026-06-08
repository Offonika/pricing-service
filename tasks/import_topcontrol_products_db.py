"""Импорт номенклатуры из 1С (УТ 10.3) в таблицу product."""

from __future__ import annotations

import logging
import os
import re
import sys
from collections.abc import Sequence

from sqlalchemy import bindparam, create_engine, select, text
from sqlalchemy.orm import Session

from app.models import Product, ProductCompatibility
from app.services.display_normalization import (
    normalize_display_construction,
    normalize_display_quality,
    normalize_display_type,
    normalize_refresh_rate_hz,
)
from app.services.phone_model_canonicalization import PhoneModelCanonicalizer
from app.services.product_classification import recompute_product_classification
from app.services.sku import sync_product_sku_status

logger = logging.getLogger("tasks.import_onec_products")

GENERAL_CATALOG_ROOT_NAMES: tuple[str, ...] = ("ОБЩИЙ КАТАЛОГ",)

PROPERTY_MAP: dict[str, str] = {
    "Предмет": "subject_1c",
    "Вид номенклатуры": "vid_nomenklatury_1c",
    "Вид_номенклатуры": "vid_nomenklatury_1c",
    "ВидНоменклатуры": "vid_nomenklatury_1c",
    "Категория": "category",
    "Качество": "quality_raw",
    "Тип дисплея": "display_type",
    "Класс дисплея": "display_quality_raw",
    "Конструкция": "display_construction",
    "Частота": "display_refresh_rate_hz",
    "Частота обновления": "display_refresh_rate_hz",
    "Производитель": "manufacturer",
    "В рамке": "in_frame",
    "Диагональ дисплея": "display_diagonal",
    "Разрешение": "display_resolution",
    "Цвет": "color",
    "SKU": "fact_sku",
    "Емкость": "battery_capacity_mah",
    "Повышенная емкость": "battery_is_high_capacity",
    "Повышенная ёмкость": "battery_is_high_capacity",
    "Напряжение": "battery_voltage",
    "Wh": "battery_energy_wh",
    "Интерфейс 1": "cable_connector_input",
    "Интерфейс 2": "cable_connector_output",
    "Длина": "cable_length",
    "Мощность": "charger_power_w",
    "Технология зарядки": "charger_technology",
    "Тип вилки": "charger_plug_type",
    "Позиция камеры": "camera_position",
    "Мегапиксели": "camera_megapixels",
    "Назначение": "flex_purpose",
    "Тип стекла": "glass_type",
    "Форма стекла": "glass_form",
    "Код микросхемы": "chip_code",
    "Модель микросхемы": "chip_code",
    "Тип детали": "part_type",
    "Тип запчасти": "part_type",
    "Состав": "set_composition",
    "Количество": "set_quantity",
}
PROPERTY_NAMES: tuple[str, ...] = tuple(PROPERTY_MAP.keys())
COMPATIBILITY_MODEL_NAMES: tuple[str, ...] = ("Совместим с моделью", "Совместимость с моделью")
COMPATIBILITY_MODEL_CODES: tuple[str, ...] = ("РБ0000086",)
COMPATIBILITY_BRAND_NAMES: tuple[str, ...] = ("Совместим с брендом", "Совместимость с брендом")
COMPATIBILITY_BRAND_CODES: tuple[str, ...] = ("РБ0000085",)


def _clean_str(value: str | None) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _parse_int(value: str | None) -> int | None:
    cleaned = _clean_str(value)
    if not cleaned:
        return None
    digits = re.search(r"\d+", cleaned)
    return int(digits.group()) if digits else None


def _parse_bool(value: str | None) -> bool | None:
    cleaned = _clean_str(value)
    if not cleaned:
        return None
    lowered = cleaned.lower()
    if lowered in {"1", "true", "yes", "да", "y"}:
        return True
    if lowered in {"0", "false", "no", "нет", "n"}:
        return False
    if any(token in lowered for token in ("увелич", "повыш", "high", "hc")):
        return True
    return None


def _parse_marked_flag(value) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (bytes, bytearray)):
        return any(byte != 0 for byte in value)
    if isinstance(value, (int, float)):
        return bool(value)
    cleaned = str(value).strip().lower()
    return cleaned in {"1", "true", "yes", "да"}


def has_duplicate_marker(name: str) -> bool:
    lower = re.sub(r"[^\w\s]+", " ", name.lower())
    return "дубл" in lower or "дублик" in lower or "duplicate" in lower or "dupl" in lower


def detect_item_folder_value(engine_onec) -> int:
    query = text("""
        SELECT CAST(_Folder AS INT) AS folder, COUNT(*) AS cnt
        FROM _Reference62
        WHERE _Marked = 0 AND _Fld836 IS NOT NULL
        GROUP BY _Folder
        ORDER BY cnt DESC
        """)
    with engine_onec.connect() as conn:
        rows = [dict(row._mapping) for row in conn.execute(query)]
    if not rows:
        logger.warning("no rows found in _Reference62; defaulting _Folder to 0")
        return 0
    folder = rows[0].get("folder")
    return int(folder or 0)


def fetch_general_catalog_item_ids(
    engine_onec,
    item_folder_value: int,
    root_names: Sequence[str] = GENERAL_CATALOG_ROOT_NAMES,
) -> set[str]:
    normalized_roots = {_clean_str(name) for name in root_names if _clean_str(name)}
    if not normalized_roots:
        return set()

    query = text("""
        SELECT
            _IDRRef AS idrref,
            _ParentIDRRef AS parent_idrref,
            _Description AS name,
            CAST(_Folder AS INT) AS folder,
            _Fld836 AS article,
            _Marked AS is_marked
        FROM _Reference62
        """)
    with engine_onec.connect() as conn:
        rows = [dict(row._mapping) for row in conn.execute(query)]

    children_by_parent: dict[str | None, list[dict]] = {}
    for row in rows:
        if _parse_marked_flag(row.get("is_marked")):
            continue
        parent = row.get("parent_idrref")
        children_by_parent.setdefault(parent, []).append(row)

    root_ids = {
        row["idrref"]
        for row in rows
        if not _parse_marked_flag(row.get("is_marked"))
        and _clean_str(row.get("name")) in normalized_roots
    }
    if not root_ids:
        logger.warning("1C import root groups not found: %s", sorted(normalized_roots))
        return set()

    allowed_item_ids: set[str] = set()
    stack = list(root_ids)
    visited: set[str] = set()
    while stack:
        current = stack.pop()
        if current in visited:
            continue
        visited.add(current)
        for child in children_by_parent.get(current, []):
            child_id = child.get("idrref")
            if not child_id:
                continue
            stack.append(child_id)
            if (
                int(child.get("folder") or -1) == item_folder_value
                and _clean_str(child.get("article"))
                and _clean_str(child.get("name"))
            ):
                allowed_item_ids.add(child_id)

    logger.info(
        "1C import scope resolved: roots=%s allowed_items=%s",
        sorted(normalized_roots),
        len(allowed_item_ids),
    )
    return allowed_item_ids


def fetch_onec_products(
    engine_onec,
    item_folder_value: int,
    allowed_item_ids: Sequence[str],
) -> list[dict]:
    if not allowed_item_ids:
        return []
    allowed_ids = set(allowed_item_ids)
    query = text("""
        SELECT
            child._IDRRef AS idrref,
            child._Fld836 AS article,
            child._Description AS name,
            child._ParentIDRRef AS parent_idrref,
            child._Code AS code_1c,
            child._Fld9175 AS info_system_code,
            kind._Description AS vid_nomenklatury_1c,
            child._Marked AS is_marked,
            parent._Description AS parent_name
        FROM _Reference62 child
        LEFT JOIN _Reference62 parent ON parent._IDRRef = child._ParentIDRRef
        LEFT JOIN _Reference26 kind ON kind._IDRRef = child._Fld857RRef
        WHERE child._Marked = 0
          AND child._Folder = :folder
          AND child._Fld836 IS NOT NULL
          AND child._Description IS NOT NULL
        """)
    with engine_onec.connect() as conn:
        rows = [dict(row._mapping) for row in conn.execute(query, {"folder": item_folder_value})]
    return [row for row in rows if row.get("idrref") in allowed_ids]


def fetch_latest_properties(
    engine_onec,
    item_folder_value: int,
    item_ids: Sequence[str],
    property_names: Sequence[str],
) -> dict[str, dict[str, str]]:
    if not item_ids:
        return {}
    allowed_ids = set(item_ids)
    prop_names = [name.strip() for name in property_names if name.strip()]
    if not prop_names:
        return {}

    query = text("""
            WITH props AS (
                SELECT
                    r._IDRRef AS idrref,
                    r._Fld836 AS article,
                    LTRIM(RTRIM(p._Fld8930)) AS prop_name,
                    LTRIM(RTRIM(p._Fld8934)) AS prop_value,
                    p._Fld8931 AS changed_at,
                    ROW_NUMBER() OVER (
                        PARTITION BY r._IDRRef, r._Fld836, p._Fld8930 ORDER BY p._Fld8931 DESC
                    ) AS rn
                FROM _InfoRg8928 p
                JOIN _Reference62 r ON r._IDRRef = p._Fld8929RRef
                WHERE r._Marked = 0
                  AND r._Folder = :folder
                  AND r._Fld836 IS NOT NULL
                  AND r._Description IS NOT NULL
                  AND LTRIM(RTRIM(p._Fld8930)) IN :prop_names
            )
            SELECT idrref, article, prop_name, prop_value
            FROM props
            WHERE rn = 1 AND prop_value IS NOT NULL AND LTRIM(RTRIM(prop_value)) <> ''
            """).bindparams(bindparam("prop_names", expanding=True))

    with engine_onec.connect() as conn:
        rows = [
            dict(row._mapping)
            for row in conn.execute(
                query,
                {
                    "folder": item_folder_value,
                    "prop_names": prop_names,
                },
            )
        ]

    props: dict[str, dict[str, str]] = {}
    for row in rows:
        article = _clean_str(row.get("article"))
        name = _clean_str(row.get("prop_name"))
        value = _clean_str(row.get("prop_value"))
        if row.get("idrref") not in allowed_ids:
            continue
        if not article or not name or not value:
            continue
        props.setdefault(article, {})[name] = value
    return props


def fetch_subject_values(
    engine_onec, item_folder_value: int, item_ids: Sequence[str]
) -> dict[str, str]:
    if not item_ids:
        return {}
    allowed_ids = set(item_ids)
    query = text("""
        SELECT
            r._IDRRef AS idrref,
            r._Fld836 AS article,
            LTRIM(RTRIM(COALESCE(v._Description, reg._Fld6312_S))) AS subject_value
        FROM _InfoRg6309 reg
        JOIN _Reference62 r ON r._IDRRef = reg._Fld6310_RRRef
        JOIN _Chrc401 ch ON ch._IDRRef = reg._Fld6311RRef
        LEFT JOIN _Reference42 v ON v._IDRRef = reg._Fld6312_RRRef
        WHERE r._Marked = 0
          AND r._Folder = :folder
          AND r._Fld836 IS NOT NULL
          AND r._Description IS NOT NULL
          AND ch._Description = 'Предмет'
          AND LTRIM(RTRIM(COALESCE(v._Description, reg._Fld6312_S))) <> ''
        """)
    with engine_onec.connect() as conn:
        rows = [dict(row._mapping) for row in conn.execute(query, {"folder": item_folder_value})]

    values: dict[str, str] = {}
    for row in rows:
        article = _clean_str(row.get("article"))
        subject = _clean_str(row.get("subject_value"))
        if row.get("idrref") not in allowed_ids:
            continue
        if article and subject:
            values[article] = subject
    return values


def fetch_property_values_multi(
    engine_onec,
    item_folder_value: int,
    item_ids: Sequence[str],
    property_names: Sequence[str],
) -> dict[str, list[str]]:
    if not item_ids:
        return {}
    allowed_ids = set(item_ids)
    prop_names = [name.strip() for name in property_names if name.strip()]
    if not prop_names:
        return {}

    query = text("""
            SELECT
                r._IDRRef AS idrref,
                r._Fld836 AS article,
                LTRIM(RTRIM(p._Fld8930)) AS prop_name,
                LTRIM(RTRIM(p._Fld8934)) AS prop_value
            FROM _InfoRg8928 p
            JOIN _Reference62 r ON r._IDRRef = p._Fld8929RRef
            WHERE r._Marked = 0
              AND r._Folder = :folder
              AND r._Fld836 IS NOT NULL
              AND r._Description IS NOT NULL
              AND LTRIM(RTRIM(p._Fld8930)) IN :prop_names
              AND p._Fld8934 IS NOT NULL
              AND LTRIM(RTRIM(p._Fld8934)) <> ''
            """).bindparams(bindparam("prop_names", expanding=True))

    with engine_onec.connect() as conn:
        rows = [
            dict(row._mapping)
            for row in conn.execute(
                query,
                {
                    "folder": item_folder_value,
                    "prop_names": prop_names,
                },
            )
        ]

    values: dict[str, list[str]] = {}
    for row in rows:
        article = _clean_str(row.get("article"))
        value = row.get("prop_value")
        if row.get("idrref") not in allowed_ids:
            continue
        if not article or value is None:
            continue
        values.setdefault(article, []).append(str(value))
    return values


def _dedupe_values(values: Sequence[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = _clean_str(raw)
        if not value:
            continue
        key = value.lower().replace("ё", "е")
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def fetch_object_property_values_multi(
    engine_onec,
    item_folder_value: int,
    item_ids: Sequence[str],
    *,
    property_names: Sequence[str],
    property_codes: Sequence[str] = (),
) -> dict[str, list[str]]:
    """Read multi-value 1C object properties from ValuesOfObjectProperties."""

    if not item_ids:
        return {}
    allowed_ids = set(item_ids)
    prop_names = [name.strip() for name in property_names if name.strip()]
    prop_codes = [code.strip() for code in property_codes if code.strip()]
    if not prop_names and not prop_codes:
        return {}

    query = text("""
        SELECT
            r._IDRRef AS idrref,
            r._Fld836 AS article,
            LTRIM(RTRIM(COALESCE(v._Description, reg._Fld6312_S))) AS prop_value
        FROM _InfoRg6309 reg
        JOIN _Reference62 r ON r._IDRRef = reg._Fld6310_RRRef
        JOIN _Chrc401 ch ON ch._IDRRef = reg._Fld6311RRef
        LEFT JOIN _Reference42 v ON v._IDRRef = reg._Fld6312_RRRef
        WHERE r._Marked = 0
          AND r._Folder = :folder
          AND r._Fld836 IS NOT NULL
          AND r._Description IS NOT NULL
          AND (
                LTRIM(RTRIM(ch._Description)) IN :prop_names
                OR LTRIM(RTRIM(ch._Code)) IN :prop_codes
          )
          AND LTRIM(RTRIM(COALESCE(v._Description, reg._Fld6312_S))) <> ''
        """).bindparams(
        bindparam("prop_names", expanding=True),
        bindparam("prop_codes", expanding=True),
    )

    with engine_onec.connect() as conn:
        rows = [
            dict(row._mapping)
            for row in conn.execute(
                query,
                {
                    "folder": item_folder_value,
                    "prop_names": prop_names or ["__none__"],
                    "prop_codes": prop_codes or ["__none__"],
                },
            )
        ]

    values: dict[str, list[str]] = {}
    for row in rows:
        article = _clean_str(row.get("article"))
        value = _clean_str(row.get("prop_value"))
        if row.get("idrref") not in allowed_ids:
            continue
        if not article or not value:
            continue
        values.setdefault(article, []).append(value)
    return {article: _dedupe_values(article_values) for article, article_values in values.items()}


def fetch_onec_compatibility_models(
    engine_onec,
    item_folder_value: int,
    item_ids: Sequence[str],
) -> dict[str, list[str]]:
    main_values = fetch_object_property_values_multi(
        engine_onec,
        item_folder_value,
        item_ids,
        property_names=COMPATIBILITY_MODEL_NAMES,
        property_codes=COMPATIBILITY_MODEL_CODES,
    )
    legacy_values = fetch_property_values_multi(
        engine_onec,
        item_folder_value,
        item_ids,
        COMPATIBILITY_MODEL_NAMES,
    )
    result = dict(main_values)
    for article, values in legacy_values.items():
        if article not in result:
            result[article] = _dedupe_values(values)
    return result


def fetch_onec_compatibility_brands(
    engine_onec,
    item_folder_value: int,
    item_ids: Sequence[str],
) -> dict[str, list[str]]:
    return fetch_object_property_values_multi(
        engine_onec,
        item_folder_value,
        item_ids,
        property_names=COMPATIBILITY_BRAND_NAMES,
        property_codes=COMPATIBILITY_BRAND_CODES,
    )


def _model_values_with_brand_hints(values: Sequence[str], brand_hints: Sequence[str]) -> list[str]:
    cleaned_values = _dedupe_values(values)
    cleaned_brands = _dedupe_values(brand_hints)
    if len(cleaned_brands) != 1:
        return cleaned_values
    brand = cleaned_brands[0]
    brand_key = brand.lower().replace("ё", "е")
    result: list[str] = []
    for value in cleaned_values:
        value_key = value.lower().replace("ё", "е")
        if brand_key in value_key:
            result.append(value)
        else:
            result.append(f"{brand} {value}")
    return result


def upsert_product_compatibility(
    session: Session,
    product: Product,
    values: list[str],
    source: str = "onec",
    brand_hints: Sequence[str] = (),
) -> None:
    cleaned = _dedupe_values(values)
    new_values = set(cleaned)
    existing = {c.value: c for c in product.compatibilities}
    canonicalizer = PhoneModelCanonicalizer(session)

    for val in new_values:
        if val not in existing:
            session.add(ProductCompatibility(product=product, value=val, source=source))

    for val, obj in existing.items():
        if val not in new_values:
            session.delete(obj)

    link_values = _model_values_with_brand_hints(sorted(new_values), brand_hints)
    canonicalizer.sync_product_links(product=product, source=source, raw_values=link_values)


def import_onec_products(engine_app, engine_onec) -> dict:
    folder_value = detect_item_folder_value(engine_onec)
    logger.info("using _Folder=%s as item flag", folder_value)
    allowed_item_ids = fetch_general_catalog_item_ids(engine_onec, folder_value)

    rows = fetch_onec_products(engine_onec, folder_value, sorted(allowed_item_ids))
    properties = fetch_latest_properties(
        engine_onec, folder_value, sorted(allowed_item_ids), PROPERTY_NAMES
    )
    subject_values = fetch_subject_values(engine_onec, folder_value, sorted(allowed_item_ids))
    compat_values = fetch_onec_compatibility_models(
        engine_onec, folder_value, sorted(allowed_item_ids)
    )
    compat_brands = fetch_onec_compatibility_brands(
        engine_onec, folder_value, sorted(allowed_item_ids)
    )

    created = 0
    updated = 0
    skipped_empty = 0
    skipped_marked_duplicates = 0
    deactivated_out_of_scope = 0
    allowed_articles = {
        article for article in (_clean_str(row.get("article")) for row in rows) if article
    }

    with Session(engine_app) as session:
        out_of_scope_products = (
            session.execute(
                select(Product).where(
                    Product.is_active.is_(True),
                    Product.article.notin_(allowed_articles or {"__none__"}),
                    (Product.code_1c.isnot(None) | Product.info_system_code.isnot(None)),
                )
            )
            .scalars()
            .all()
        )
        for product in out_of_scope_products:
            product.is_active = False
            deactivated_out_of_scope += 1

        for row in rows:
            article = _clean_str(row.get("article"))
            name = _clean_str(row.get("name"))
            if not article or not name:
                skipped_empty += 1
                continue

            if has_duplicate_marker(name):
                skipped_marked_duplicates += 1
                existing = session.execute(
                    select(Product).where(Product.article == article)
                ).scalar_one_or_none()
                if existing:
                    existing.is_active = False
                continue

            product = session.execute(
                select(Product).where(Product.article == article)
            ).scalar_one_or_none()
            is_new = product is None
            if is_new:
                product = Product(article=article, name=name)
                session.add(product)
                created += 1
            else:
                product.name = name
                product.is_active = True
                updated += 1

            product.is_marked_for_deletion = _parse_marked_flag(row.get("is_marked"))

            prop_map = properties.get(article, {})
            category = _clean_str(prop_map.get("Категория")) or _clean_str(row.get("parent_name"))
            if category:
                product.category = category

            product.subject_1c = None
            product.vid_nomenklatury_1c = _clean_str(row.get("vid_nomenklatury_1c"))

            for prop_name, attr in PROPERTY_MAP.items():
                if prop_name == "Категория":
                    continue
                value = _clean_str(prop_map.get(prop_name))
                if value:
                    if attr == "display_type":
                        setattr(product, attr, normalize_display_type(value) or value)
                    elif attr == "display_quality":
                        setattr(product, attr, normalize_display_quality(value) or value)
                    elif attr == "display_construction":
                        setattr(product, attr, normalize_display_construction(value) or value)
                    elif attr == "display_refresh_rate_hz":
                        normalized_rate = normalize_refresh_rate_hz(value)
                        if normalized_rate is not None:
                            setattr(product, attr, normalized_rate)
                    elif attr in {"quality_raw", "display_quality_raw"}:
                        setattr(product, attr, value)
                    elif attr in {
                        "battery_capacity_mah",
                        "charger_power_w",
                        "camera_megapixels",
                        "set_quantity",
                    }:
                        parsed_int = _parse_int(value)
                        if parsed_int is not None:
                            setattr(product, attr, parsed_int)
                    elif attr == "battery_is_high_capacity":
                        parsed_bool = _parse_bool(value)
                        if parsed_bool is not None:
                            setattr(product, attr, parsed_bool)
                    else:
                        setattr(product, attr, value)

            subject_value = subject_values.get(article)
            if subject_value:
                product.subject_1c = subject_value

            recompute_product_classification(product)

            if product.quality_raw:
                product.quality = (
                    normalize_display_quality(product.quality_raw) or product.quality_raw
                )
            elif product.quality is not None:
                product.quality = _clean_str(product.quality)

            display_quality_source = product.display_quality_raw or product.quality_raw
            if display_quality_source:
                product.display_quality = (
                    normalize_display_quality(display_quality_source) or display_quality_source
                )
            elif product.display_quality is not None:
                product.display_quality = _clean_str(product.display_quality)

            code_1c = _clean_str(row.get("code_1c"))
            if code_1c:
                product.code_1c = code_1c

            info_system_code = _clean_str(row.get("info_system_code"))
            if info_system_code:
                product.info_system_code = info_system_code

            compat_list = compat_values.get(article, [])
            upsert_product_compatibility(
                session,
                product,
                compat_list,
                brand_hints=compat_brands.get(article, []),
            )
            sync_product_sku_status(product)
        session.commit()

    return {
        "rows": len(rows),
        "created": created,
        "updated": updated,
        "skipped_empty": skipped_empty,
        "skipped_marked_duplicates": skipped_marked_duplicates,
        "deactivated_out_of_scope": deactivated_out_of_scope,
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    onec_url = os.environ.get("ONEC_DATABASE_URL")
    app_url = os.environ.get("DATABASE_URL")
    if not onec_url or not app_url:
        logger.error("ONEC_DATABASE_URL or DATABASE_URL is not set")
        sys.exit(1)

    engine_onec = create_engine(onec_url)
    engine_app = create_engine(app_url)
    result = import_onec_products(engine_app, engine_onec)
    print(result)


if __name__ == "__main__":
    main()
