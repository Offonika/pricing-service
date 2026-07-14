"""rename_device_compat_fields

Revision ID: 4a8a48cdbf06
Revises: e2b3c4d5f6a7
Create Date: 2026-01-31 13:15:13.299227

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4a8a48cdbf06"
down_revision: str | None = "e2b3c4d5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _create_display_view(*, sqlite: bool, cols: set[str]) -> None:
    op.execute("DROP VIEW IF EXISTS vw_competitor_display_ru;")

    def q(name: str) -> str:
        return f'ci."{name}"'

    def pick(*names: str) -> str | None:
        for name in names:
            if name in cols:
                return name
        return None

    type_col = pick("Тип дисплея", "screen_matrix_type")
    type_expr = q(type_col) if type_col else "NULL"

    frame_col = pick("Наличие_Рамки (в рамке)", "В_рамке", "has_frame")
    if frame_col:
        frame_case = (
            f"CASE WHEN {q(frame_col)} = 1 THEN 'Да' "
            f"WHEN {q(frame_col)} = 0 THEN 'Нет' "
            "ELSE 'Не определено' END"
            if sqlite
            else f"CASE WHEN {q(frame_col)} IS TRUE THEN 'Да' "
            f"WHEN {q(frame_col)} IS FALSE THEN 'Нет' "
            "ELSE 'Не определено' END"
        )
    else:
        frame_case = "'Не определено'"

    touch_col = pick("Наличие_Тачскрина (с тачскрином)", "С тачскрином", "has_touch")
    if touch_col:
        touch_case = (
            f"CASE WHEN {q(touch_col)} = 1 THEN 'Да' "
            f"WHEN {q(touch_col)} = 0 THEN 'Нет' "
            "ELSE 'Не определено' END"
            if sqlite
            else f"CASE WHEN {q(touch_col)} IS TRUE THEN 'Да' "
            f"WHEN {q(touch_col)} IS FALSE THEN 'Нет' "
            "ELSE 'Не определено' END"
        )
    elif "screen_kit" in cols:
        touch_case = (
            "CASE "
            "WHEN ci.\"screen_kit\" IN ('DISPLAY_WITH_TOUCH', 'DISPLAY_TOUCH_FRAME') THEN 'Да' "
            "WHEN ci.\"screen_kit\" IN ('DISPLAY_ONLY', 'DISPLAY_WITH_FRAME') THEN 'Нет' "
            "ELSE 'Не определено' END"
        )
    else:
        touch_case = "'Не определено'"

    quality_col = pick("Качество", "screen_quality_grade")
    quality_expr = q(quality_col) if quality_col else "NULL"

    manufacturer_col = pick("item_manufacturer", "Производитель", "manufacturer")
    manufacturer_expr = q(manufacturer_col) if manufacturer_col else "NULL"

    color_col = pick("Цвет", "color")
    color_expr = q(color_col) if color_col else "NULL"

    tags_col = pick("Теги_Матрицы", "matrix_tags")
    tags_expr = q(tags_col) if tags_col else "NULL"

    if sqlite:
        op.execute(f"""
            CREATE VIEW vw_competitor_display_ru AS
            SELECT
                ci.id,
                ci.competitor,
                ci.external_id,
                ci.name,
                CASE {type_expr}
                    WHEN 'LCD_TFT' THEN 'LCD (TFT)'
                    WHEN 'LCD_IPS' THEN 'LCD (IPS)'
                    WHEN 'LTPS_LCD' THEN 'LTPS LCD'
                    WHEN 'OLED' THEN 'OLED'
                    WHEN 'AMOLED' THEN 'AMOLED'
                    WHEN 'LTPO_AMOLED' THEN 'LTPO AMOLED'
                    ELSE 'Не определено'
                END AS "Тип дисплея",
                {frame_case} AS "Наличие_Рамки (в рамке)",
                {touch_case} AS "Наличие_Тачскрина (с тачскрином)",
                CASE {quality_expr}
                    WHEN 'ORIGINAL' THEN 'Оригинал'
                    WHEN 'ORIGINAL_REFURB' THEN 'Оригинал (замена стекла)'
                    WHEN 'OEM' THEN 'OEM'
                    WHEN 'GX' THEN 'GX'
                    WHEN 'OR' THEN 'OR'
                    WHEN 'OR100' THEN 'OR 100%'
                    WHEN 'PREMIUM' THEN 'Премиум'
                    WHEN 'AAA' THEN 'AAA'
                    WHEN 'HQ' THEN 'HQ'
                    WHEN 'FIRST_CLASS' THEN '1-я категория'
                    WHEN 'COPY_HIGH' THEN 'Копия (высокая)'
                    WHEN 'COPY_MEDIUM' THEN 'Копия (средняя)'
                    WHEN 'COPY_LOW' THEN 'Копия (низкая)'
                    ELSE 'Не определено'
                END AS "Качество",
                {manufacturer_expr} AS "Производитель",
                {color_expr} AS "Цвет",
                {tags_expr} AS "Теги_Матрицы"
            FROM competitor_item ci;
            """)
    else:
        op.execute(f"""
            CREATE VIEW vw_competitor_display_ru AS
            SELECT
                ci.id,
                ci.competitor,
                ci.external_id,
                ci.name,
                CASE {type_expr}
                    WHEN 'LCD_TFT' THEN 'LCD (TFT)'
                    WHEN 'LCD_IPS' THEN 'LCD (IPS)'
                    WHEN 'LTPS_LCD' THEN 'LTPS LCD'
                    WHEN 'OLED' THEN 'OLED'
                    WHEN 'AMOLED' THEN 'AMOLED'
                    WHEN 'LTPO_AMOLED' THEN 'LTPO AMOLED'
                    ELSE 'Не определено'
                END AS "Тип дисплея",
                {frame_case} AS "Наличие_Рамки (в рамке)",
                {touch_case} AS "Наличие_Тачскрина (с тачскрином)",
                CASE {quality_expr}
                    WHEN 'ORIGINAL' THEN 'Оригинал'
                    WHEN 'ORIGINAL_REFURB' THEN 'Оригинал (замена стекла)'
                    WHEN 'OEM' THEN 'OEM'
                    WHEN 'GX' THEN 'GX'
                    WHEN 'OR' THEN 'OR'
                    WHEN 'OR100' THEN 'OR 100%'
                    WHEN 'PREMIUM' THEN 'Премиум'
                    WHEN 'AAA' THEN 'AAA'
                    WHEN 'HQ' THEN 'HQ'
                    WHEN 'FIRST_CLASS' THEN '1-я категория'
                    WHEN 'COPY_HIGH' THEN 'Копия (высокая)'
                    WHEN 'COPY_MEDIUM' THEN 'Копия (средняя)'
                    WHEN 'COPY_LOW' THEN 'Копия (низкая)'
                    ELSE 'Не определено'
                END AS "Качество",
                {manufacturer_expr} AS "Производитель",
                {color_expr} AS "Цвет",
                {tags_expr} AS "Теги_Матрицы"
            FROM competitor_item ci;
            """)


def upgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"
    insp = sa.inspect(bind)
    comp_item_cols = {col["name"] for col in insp.get_columns("competitor_item")}

    op.execute("DROP VIEW IF EXISTS vw_competitor_display_ru;")

    with op.batch_alter_table("competitor_item") as batch_op:
        if "parsed_brand" in comp_item_cols:
            batch_op.alter_column(
                "parsed_brand",
                new_column_name="parsed_device_brand",
                existing_type=sa.String(length=128),
                existing_nullable=True,
            )
        if "parsed_model" in comp_item_cols:
            batch_op.alter_column(
                "parsed_model",
                new_column_name="parsed_device_model",
                existing_type=sa.String(length=255),
                existing_nullable=True,
            )
        if "parsed_variant" in comp_item_cols:
            batch_op.alter_column(
                "parsed_variant",
                new_column_name="parsed_device_variant",
                existing_type=sa.String(length=50),
                existing_nullable=True,
            )
        if "attrs_brand" in comp_item_cols:
            batch_op.alter_column(
                "attrs_brand",
                new_column_name="item_brand",
                existing_type=sa.String(length=128),
                existing_nullable=True,
            )
        elif "item_brand" not in comp_item_cols:
            batch_op.add_column(sa.Column("item_brand", sa.String(length=128), nullable=True))
        if "Производитель" in comp_item_cols:
            batch_op.alter_column(
                "Производитель",
                new_column_name="item_manufacturer",
                existing_type=sa.String(length=64),
                existing_nullable=True,
            )
        elif "item_manufacturer" not in comp_item_cols:
            batch_op.add_column(sa.Column("item_manufacturer", sa.String(length=64), nullable=True))
    op.drop_index("ix_competitor_item_parsed_brand", table_name="competitor_item")
    op.create_index(
        "ix_competitor_item_parsed_device_brand",
        "competitor_item",
        ["parsed_device_brand"],
        unique=False,
    )
    comp_item_cols = {col["name"] for col in sa.inspect(bind).get_columns("competitor_item")}
    _create_display_view(sqlite=is_sqlite, cols=comp_item_cols)

    with op.batch_alter_table("competitor_ftp_record") as batch_op:
        batch_op.alter_column(
            "parsed_brand",
            new_column_name="parsed_device_brand",
            existing_type=sa.String(length=128),
            existing_nullable=True,
        )
        batch_op.alter_column(
            "parsed_model",
            new_column_name="parsed_device_model",
            existing_type=sa.String(length=255),
            existing_nullable=True,
        )
        batch_op.alter_column(
            "parsed_variant",
            new_column_name="parsed_device_variant",
            existing_type=sa.String(length=50),
            existing_nullable=True,
        )
    op.drop_index("ix_competitor_ftp_record_parsed_brand", table_name="competitor_ftp_record")
    op.create_index(
        "ix_competitor_ftp_record_parsed_device_brand",
        "competitor_ftp_record",
        ["parsed_device_brand"],
        unique=False,
    )

    with op.batch_alter_table("competitor_item_compatibility") as batch_op:
        batch_op.drop_constraint(
            "uq_comp_item_compat_item_brand_compat_variant",
            type_="unique",
        )
        batch_op.alter_column(
            "brand",
            new_column_name="device_brand",
            existing_type=sa.String(length=128),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "compatibility",
            new_column_name="device_model",
            existing_type=sa.String(length=255),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "variant",
            new_column_name="device_variant",
            existing_type=sa.String(length=50),
            existing_nullable=True,
        )
        batch_op.create_unique_constraint(
            "uq_comp_item_compat_item_device_brand_model_variant",
            ["competitor_item_id", "device_brand", "device_model", "device_variant"],
        )
    op.drop_index(
        "ix_competitor_item_compatibility_brand",
        table_name="competitor_item_compatibility",
    )
    op.create_index(
        "ix_competitor_item_compatibility_device_brand",
        "competitor_item_compatibility",
        ["device_brand"],
        unique=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"
    op.execute("DROP VIEW IF EXISTS vw_competitor_display_ru;")

    op.drop_index(
        "ix_competitor_item_compatibility_device_brand",
        table_name="competitor_item_compatibility",
    )
    with op.batch_alter_table("competitor_item_compatibility") as batch_op:
        batch_op.drop_constraint(
            "uq_comp_item_compat_item_device_brand_model_variant",
            type_="unique",
        )
        batch_op.alter_column(
            "device_brand",
            new_column_name="brand",
            existing_type=sa.String(length=128),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "device_model",
            new_column_name="compatibility",
            existing_type=sa.String(length=255),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "device_variant",
            new_column_name="variant",
            existing_type=sa.String(length=50),
            existing_nullable=True,
        )
        batch_op.create_unique_constraint(
            "uq_comp_item_compat_item_brand_compat_variant",
            ["competitor_item_id", "brand", "compatibility", "variant"],
        )
    op.create_index(
        "ix_competitor_item_compatibility_brand",
        "competitor_item_compatibility",
        ["brand"],
        unique=False,
    )

    op.drop_index(
        "ix_competitor_ftp_record_parsed_device_brand", table_name="competitor_ftp_record"
    )
    with op.batch_alter_table("competitor_ftp_record") as batch_op:
        batch_op.alter_column(
            "parsed_device_brand",
            new_column_name="parsed_brand",
            existing_type=sa.String(length=128),
            existing_nullable=True,
        )
        batch_op.alter_column(
            "parsed_device_model",
            new_column_name="parsed_model",
            existing_type=sa.String(length=255),
            existing_nullable=True,
        )
        batch_op.alter_column(
            "parsed_device_variant",
            new_column_name="parsed_variant",
            existing_type=sa.String(length=50),
            existing_nullable=True,
        )
    op.create_index(
        "ix_competitor_ftp_record_parsed_brand",
        "competitor_ftp_record",
        ["parsed_brand"],
        unique=False,
    )

    op.drop_index("ix_competitor_item_parsed_device_brand", table_name="competitor_item")
    with op.batch_alter_table("competitor_item") as batch_op:
        batch_op.alter_column(
            "item_brand",
            new_column_name="attrs_brand",
            existing_type=sa.String(length=128),
            existing_nullable=True,
        )
        batch_op.alter_column(
            "item_manufacturer",
            new_column_name="Производитель",
            existing_type=sa.String(length=64),
            existing_nullable=True,
        )
        batch_op.alter_column(
            "parsed_device_brand",
            new_column_name="parsed_brand",
            existing_type=sa.String(length=128),
            existing_nullable=True,
        )
        batch_op.alter_column(
            "parsed_device_model",
            new_column_name="parsed_model",
            existing_type=sa.String(length=255),
            existing_nullable=True,
        )
        batch_op.alter_column(
            "parsed_device_variant",
            new_column_name="parsed_variant",
            existing_type=sa.String(length=50),
            existing_nullable=True,
        )
    op.create_index(
        "ix_competitor_item_parsed_brand",
        "competitor_item",
        ["parsed_brand"],
        unique=False,
    )
    comp_item_cols = {col["name"] for col in sa.inspect(bind).get_columns("competitor_item")}
    _create_display_view(sqlite=is_sqlite, cols=comp_item_cols)
