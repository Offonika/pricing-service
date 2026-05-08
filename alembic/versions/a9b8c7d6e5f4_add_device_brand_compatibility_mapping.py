"""add device brand compatibility mapping"""

from __future__ import annotations

import re
from datetime import datetime

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "a9b8c7d6e5f4"
down_revision = "7b6c5d4e3f2a"
branch_labels = None
depends_on = None


BRAND_SEEDS = [
    ("apple", "apple", "Apple", "apple"),
    ("samsung", "samsung", "Samsung", "samsung"),
    ("xiaomi", "xiaomi", "Xiaomi", "xiaomi"),
    ("redmi", "redmi", "Redmi", "xiaomi"),
    ("poco", "poco", "POCO", "xiaomi"),
    ("huawei", "huawei", "Huawei", "huawei_honor"),
    ("honor", "honor", "Honor", "huawei_honor"),
    ("realme", "realme", "Realme", "realme"),
    ("oppo", "oppo", "OPPO", "oppo"),
    ("vivo", "vivo", "Vivo", "vivo"),
    ("oneplus", "oneplus", "OnePlus", "oneplus"),
    ("tecno", "tecno", "Tecno", "tecno"),
    ("infinix", "infinix", "Infinix", "infinix"),
    ("itel", "itel", "Itel", "itel"),
    ("nokia", "nokia", "Nokia", "nokia"),
    ("sony", "sony", "Sony", "sony"),
    ("motorola", "motorola", "Motorola", "motorola"),
    ("google", "google", "Google", "google"),
    ("lenovo", "lenovo", "Lenovo", "lenovo"),
    ("asus", "asus", "Asus", "asus"),
    ("zte", "zte", "ZTE", "zte"),
    ("meizu", "meizu", "Meizu", "meizu"),
    ("alcatel", "alcatel", "Alcatel", "alcatel"),
    ("nothing", "nothing", "Nothing", "nothing"),
    ("bq", "bq", "BQ", "bq"),
    ("doogee", "doogee", "Doogee", "doogee"),
    ("oukitel", "oukitel", "Oukitel", "oukitel"),
    ("blackview", "blackview", "Blackview", "blackview"),
    ("ulefone", "ulefone", "Ulefone", "ulefone"),
    ("cubot", "cubot", "Cubot", "cubot"),
    ("fly", "fly", "Fly", "fly"),
    ("philips", "philips", "Philips", "philips"),
    ("lg", "lg", "LG", "lg"),
    ("htc", "htc", "HTC", "htc"),
]

BRAND_SYNONYMS = {
    "iphone": "apple",
    "ipad": "apple",
    "ipod": "apple",
    "apple": "apple",
    "samsung": "samsung",
    "galaxy": "samsung",
    "xiaomi": "xiaomi",
    "mi": "xiaomi",
    "mipad": "xiaomi",
    "redmi": "redmi",
    "poco": "poco",
    "huawei": "huawei",
    "honor": "honor",
    "realme": "realme",
    "oppo": "oppo",
    "vivo": "vivo",
    "oneplus": "oneplus",
    "one plus": "oneplus",
    "tecno": "tecno",
    "infinix": "infinix",
    "infinx": "infinix",
    "itel": "itel",
    "nokia": "nokia",
    "sony": "sony",
    "motorola": "motorola",
    "google": "google",
    "pixel": "google",
    "lenovo": "lenovo",
    "xiaoxin": "lenovo",
    "asus": "asus",
    "zte": "zte",
    "meizu": "meizu",
    "alcatel": "alcatel",
    "nothing": "nothing",
    "cmf": "nothing",
    "bq": "bq",
    "doogee": "doogee",
    "oukitel": "oukitel",
    "blackview": "blackview",
    "ulefone": "ulefone",
    "cubot": "cubot",
    "fly": "fly",
    "philips": "philips",
    "lg": "lg",
    "htc": "htc",
}

BRAND_TOKEN_PRIORITY = (
    "redmi",
    "poco",
)


def _normalize_key(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower().replace("ё", "е")
    normalized = re.sub(r"[_\-]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized or None


def _code_for(value: str | None, model_name: str | None = None) -> str | None:
    normalized = _normalize_key(value)
    model = _normalize_key(model_name) or ""
    if normalized == "xiaomi":
        if model.startswith("redmi"):
            return "redmi"
        if model.startswith("poco"):
            return "poco"
    if not normalized:
        return None

    code = BRAND_SYNONYMS.get(normalized)
    if code:
        return code

    compact = normalized.replace(" ", "")
    code = BRAND_SYNONYMS.get(compact)
    if code:
        return code

    tokens = re.findall(r"[a-z0-9а-я]+", normalized)
    for token in BRAND_TOKEN_PRIORITY:
        if token in tokens:
            return BRAND_SYNONYMS[token]
    for token in tokens:
        code = BRAND_SYNONYMS.get(token)
        if code:
            return code

    code = normalized
    return re.sub(r"[^a-z0-9]+", "_", code).strip("_") or None


def _display_for(code: str) -> str:
    for seed_code, _name, display_name, _group in BRAND_SEEDS:
        if seed_code == code:
            return display_name
    return code.replace("_", " ").title()


def _group_for(code: str) -> str:
    if code in {"xiaomi", "redmi", "poco"}:
        return "xiaomi"
    if code in {"huawei", "honor"}:
        return "huawei_honor"
    return code


def _seed_brand(bind, brand_table, code: str, name: str | None = None) -> int:
    row = bind.execute(sa.select(brand_table.c.id).where(brand_table.c.code == code)).first()
    if row:
        return int(row.id)
    brand_id = bind.execute(
        brand_table.insert()
        .values(
            code=code,
            name=name or code,
            display_name=_display_for(code),
            group_code=_group_for(code),
            is_active=True,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        .returning(brand_table.c.id)
    )
    return int(brand_id.scalar_one())


def _seed_alias(bind, alias_table, brand_id: int, source: str, raw_value: str) -> None:
    normalized_key = _normalize_key(raw_value)
    if not normalized_key:
        return
    exists_row = bind.execute(
        sa.select(alias_table.c.id).where(
            alias_table.c.source == source,
            alias_table.c.normalized_key == normalized_key,
            alias_table.c.brand_id == brand_id,
        )
    ).first()
    if exists_row:
        return
    bind.execute(
        alias_table.insert().values(
            brand_id=brand_id,
            source=source,
            raw_value=str(raw_value).strip(),
            normalized_key=normalized_key,
            confidence=1.0,
            is_manual=False,
            decision_reason="migration_seed",
            first_seen_at=datetime.utcnow(),
            last_seen_at=datetime.utcnow(),
        )
    )


def upgrade() -> None:
    op.create_table(
        "device_brands",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("group_code", sa.String(length=64), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_device_brand_code"),
        sa.UniqueConstraint("name", name="uq_device_brand_name"),
    )
    op.create_index(op.f("ix_device_brands_code"), "device_brands", ["code"])
    op.create_index(op.f("ix_device_brands_group_code"), "device_brands", ["group_code"])
    op.create_index(op.f("ix_device_brands_is_active"), "device_brands", ["is_active"])

    op.create_table(
        "device_brand_aliases",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("brand_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("raw_value", sa.String(length=255), nullable=False),
        sa.Column("normalized_key", sa.String(length=255), nullable=False),
        sa.Column("confidence", sa.Numeric(4, 3), nullable=True),
        sa.Column("is_manual", sa.Boolean(), nullable=False),
        sa.Column("decision_reason", sa.String(length=100), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["brand_id"], ["device_brands.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source",
            "normalized_key",
            "brand_id",
            name="uq_device_brand_alias_source_key_brand",
        ),
    )
    op.create_index(op.f("ix_device_brand_aliases_brand_id"), "device_brand_aliases", ["brand_id"])
    op.create_index(
        op.f("ix_device_brand_aliases_normalized_key"),
        "device_brand_aliases",
        ["normalized_key"],
    )
    op.create_index(op.f("ix_device_brand_aliases_source"), "device_brand_aliases", ["source"])

    op.create_table(
        "compatibility_mapping_decisions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=50), nullable=True),
        sa.Column("raw_value", sa.String(length=255), nullable=False),
        sa.Column("normalized_key", sa.String(length=255), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("brand_id", sa.Integer(), nullable=True),
        sa.Column("phone_model_ids_json", sa.JSON(), nullable=True),
        sa.Column("actor", sa.String(length=100), nullable=True),
        sa.Column("notes", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["brand_id"], ["device_brands.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_compatibility_mapping_decisions_action"),
        "compatibility_mapping_decisions",
        ["action"],
    )
    op.create_index(
        op.f("ix_compatibility_mapping_decisions_entity_id"),
        "compatibility_mapping_decisions",
        ["entity_id"],
    )
    op.create_index(
        op.f("ix_compatibility_mapping_decisions_entity_type"),
        "compatibility_mapping_decisions",
        ["entity_type"],
    )
    op.create_index(
        op.f("ix_compatibility_mapping_decisions_normalized_key"),
        "compatibility_mapping_decisions",
        ["normalized_key"],
    )
    op.create_index(
        op.f("ix_compatibility_mapping_decisions_source"),
        "compatibility_mapping_decisions",
        ["source"],
    )

    op.add_column("phone_models", sa.Column("brand_id", sa.Integer(), nullable=True))
    op.create_index(op.f("ix_phone_models_brand_id"), "phone_models", ["brand_id"])
    op.create_foreign_key(
        "fk_phone_models_brand_id_device_brands",
        "phone_models",
        "device_brands",
        ["brand_id"],
        ["id"],
    )
    op.add_column(
        "competitor_item_compatibility",
        sa.Column("device_brand_id", sa.Integer(), nullable=True),
    )
    op.create_index(
        op.f("ix_competitor_item_compatibility_device_brand_id"),
        "competitor_item_compatibility",
        ["device_brand_id"],
    )
    op.create_foreign_key(
        "fk_competitor_item_compatibility_device_brand_id",
        "competitor_item_compatibility",
        "device_brands",
        ["device_brand_id"],
        ["id"],
    )

    bind = op.get_bind()
    brand_table = sa.table(
        "device_brands",
        sa.column("id", sa.Integer),
        sa.column("code", sa.String),
        sa.column("name", sa.String),
        sa.column("display_name", sa.String),
        sa.column("group_code", sa.String),
        sa.column("is_active", sa.Boolean),
        sa.column("created_at", sa.DateTime),
        sa.column("updated_at", sa.DateTime),
    )
    alias_table = sa.table(
        "device_brand_aliases",
        sa.column("id", sa.Integer),
        sa.column("brand_id", sa.Integer),
        sa.column("source", sa.String),
        sa.column("raw_value", sa.String),
        sa.column("normalized_key", sa.String),
        sa.column("confidence", sa.Numeric),
        sa.column("is_manual", sa.Boolean),
        sa.column("decision_reason", sa.String),
        sa.column("first_seen_at", sa.DateTime),
        sa.column("last_seen_at", sa.DateTime),
    )
    phone_models = sa.table(
        "phone_models",
        sa.column("id", sa.Integer),
        sa.column("brand", sa.String),
        sa.column("brand_id", sa.Integer),
        sa.column("model_name", sa.String),
    )
    compat_table = sa.table(
        "competitor_item_compatibility",
        sa.column("id", sa.Integer),
        sa.column("device_brand", sa.String),
        sa.column("device_brand_id", sa.Integer),
        sa.column("device_model", sa.String),
    )
    phone_alias_table = sa.table(
        "phone_model_alias",
        sa.column("raw_brand", sa.String),
        sa.column("source", sa.String),
    )

    brand_ids: dict[str, int] = {}
    for code, name, _display_name, _group_code in BRAND_SEEDS:
        brand_ids[code] = _seed_brand(bind, brand_table, code, name)

    for row in bind.execute(
        sa.select(phone_models.c.id, phone_models.c.brand, phone_models.c.model_name)
    ):
        code = _code_for(row.brand, row.model_name)
        if not code:
            continue
        brand_id = brand_ids.get(code) or _seed_brand(bind, brand_table, code)
        brand_ids[code] = brand_id
        bind.execute(
            phone_models.update().where(phone_models.c.id == row.id).values(brand_id=brand_id)
        )
        _seed_alias(bind, alias_table, brand_id, "phone_models", row.brand)

    for row in bind.execute(
        sa.select(compat_table.c.id, compat_table.c.device_brand, compat_table.c.device_model)
    ):
        code = _code_for(row.device_brand, row.device_model)
        if not code:
            continue
        brand_id = brand_ids.get(code) or _seed_brand(bind, brand_table, code)
        brand_ids[code] = brand_id
        bind.execute(
            compat_table.update()
            .where(compat_table.c.id == row.id)
            .values(device_brand_id=brand_id)
        )
        _seed_alias(bind, alias_table, brand_id, "competitor_compatibility", row.device_brand)

    for row in bind.execute(sa.select(phone_alias_table.c.raw_brand, phone_alias_table.c.source)):
        code = _code_for(row.raw_brand)
        if not code:
            continue
        brand_id = brand_ids.get(code) or _seed_brand(bind, brand_table, code)
        brand_ids[code] = brand_id
        _seed_alias(bind, alias_table, brand_id, row.source or "phone_model_alias", row.raw_brand)


def downgrade() -> None:
    op.drop_constraint(
        "fk_competitor_item_compatibility_device_brand_id",
        "competitor_item_compatibility",
        type_="foreignkey",
    )
    op.drop_index(
        op.f("ix_competitor_item_compatibility_device_brand_id"),
        table_name="competitor_item_compatibility",
    )
    op.drop_column("competitor_item_compatibility", "device_brand_id")
    op.drop_constraint("fk_phone_models_brand_id_device_brands", "phone_models", type_="foreignkey")
    op.drop_index(op.f("ix_phone_models_brand_id"), table_name="phone_models")
    op.drop_column("phone_models", "brand_id")

    op.drop_index(
        op.f("ix_compatibility_mapping_decisions_source"),
        table_name="compatibility_mapping_decisions",
    )
    op.drop_index(
        op.f("ix_compatibility_mapping_decisions_normalized_key"),
        table_name="compatibility_mapping_decisions",
    )
    op.drop_index(
        op.f("ix_compatibility_mapping_decisions_entity_type"),
        table_name="compatibility_mapping_decisions",
    )
    op.drop_index(
        op.f("ix_compatibility_mapping_decisions_entity_id"),
        table_name="compatibility_mapping_decisions",
    )
    op.drop_index(
        op.f("ix_compatibility_mapping_decisions_action"),
        table_name="compatibility_mapping_decisions",
    )
    op.drop_table("compatibility_mapping_decisions")

    op.drop_index(op.f("ix_device_brand_aliases_source"), table_name="device_brand_aliases")
    op.drop_index(
        op.f("ix_device_brand_aliases_normalized_key"),
        table_name="device_brand_aliases",
    )
    op.drop_index(op.f("ix_device_brand_aliases_brand_id"), table_name="device_brand_aliases")
    op.drop_table("device_brand_aliases")

    op.drop_index(op.f("ix_device_brands_is_active"), table_name="device_brands")
    op.drop_index(op.f("ix_device_brands_group_code"), table_name="device_brands")
    op.drop_index(op.f("ix_device_brands_code"), table_name="device_brands")
    op.drop_table("device_brands")
