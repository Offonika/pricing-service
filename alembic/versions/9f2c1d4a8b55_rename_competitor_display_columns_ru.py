"""rename competitor display columns to RU and add touch flag

Revision ID: 9f2c1d4a8b55
Revises: 6b9d1a2c4e11
Create Date: 2025-02-06 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "9f2c1d4a8b55"
down_revision = "6b9d1a2c4e11"
branch_labels = None
depends_on = None


def _create_view(bind: sa.engine.Connection, *, sqlite: bool) -> None:
    op.execute("DROP VIEW IF EXISTS vw_competitor_display_ru;")
    if sqlite:
        op.execute("""
            CREATE VIEW vw_competitor_display_ru AS
            SELECT
                ci.id,
                ci.competitor,
                ci.external_id,
                ci.name,
                CASE ci."Тип дисплея"
                    WHEN 'LCD_TFT' THEN 'LCD (TFT)'
                    WHEN 'LCD_IPS' THEN 'LCD (IPS)'
                    WHEN 'LTPS_LCD' THEN 'LTPS LCD'
                    WHEN 'OLED' THEN 'OLED'
                    WHEN 'AMOLED' THEN 'AMOLED'
                    WHEN 'LTPO_AMOLED' THEN 'LTPO AMOLED'
                    ELSE 'Не определено'
                END AS "Тип дисплея",
                CASE
                    WHEN ci."Наличие_Рамки (в рамке)" = 1 THEN 'Да'
                    WHEN ci."Наличие_Рамки (в рамке)" = 0 THEN 'Нет'
                    ELSE 'Не определено'
                END AS "Наличие_Рамки (в рамке)",
                CASE
                    WHEN ci."Наличие_Тачскрина (с тачскрином)" = 1 THEN 'Да'
                    WHEN ci."Наличие_Тачскрина (с тачскрином)" = 0 THEN 'Нет'
                    ELSE 'Не определено'
                END AS "Наличие_Тачскрина (с тачскрином)",
                CASE ci."Качество"
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
                END AS "Качество"
            FROM competitor_item ci;
            """)
    else:
        op.execute("""
            CREATE VIEW vw_competitor_display_ru AS
            SELECT
                ci.id,
                ci.competitor,
                ci.external_id,
                ci.name,
                CASE ci."Тип дисплея"
                    WHEN 'LCD_TFT' THEN 'LCD (TFT)'
                    WHEN 'LCD_IPS' THEN 'LCD (IPS)'
                    WHEN 'LTPS_LCD' THEN 'LTPS LCD'
                    WHEN 'OLED' THEN 'OLED'
                    WHEN 'AMOLED' THEN 'AMOLED'
                    WHEN 'LTPO_AMOLED' THEN 'LTPO AMOLED'
                    ELSE 'Не определено'
                END AS "Тип дисплея",
                CASE
                    WHEN ci."Наличие_Рамки (в рамке)" IS TRUE THEN 'Да'
                    WHEN ci."Наличие_Рамки (в рамке)" IS FALSE THEN 'Нет'
                    ELSE 'Не определено'
                END AS "Наличие_Рамки (в рамке)",
                CASE
                    WHEN ci."Наличие_Тачскрина (с тачскрином)" IS TRUE THEN 'Да'
                    WHEN ci."Наличие_Тачскрина (с тачскрином)" IS FALSE THEN 'Нет'
                    ELSE 'Не определено'
                END AS "Наличие_Тачскрина (с тачскрином)",
                CASE ci."Качество"
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
                END AS "Качество"
            FROM competitor_item ci;
            """)


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {col["name"] for col in insp.get_columns("competitor_item")}

    with op.batch_alter_table("competitor_item") as batch:
        if "screen_matrix_type" in cols:
            batch.alter_column("screen_matrix_type", new_column_name="Тип дисплея")
        if "screen_quality_grade" in cols:
            batch.alter_column("screen_quality_grade", new_column_name="Качество")
        if "has_frame" in cols:
            batch.alter_column("has_frame", new_column_name="Наличие_Рамки (в рамке)")
        if "Наличие_Тачскрина (с тачскрином)" not in cols:
            batch.add_column(
                sa.Column("Наличие_Тачскрина (с тачскрином)", sa.Boolean(), nullable=True)
            )

    op.execute("""
        UPDATE competitor_item
        SET "Наличие_Тачскрина (с тачскрином)" = CASE
            WHEN screen_kit IN ('DISPLAY_WITH_TOUCH', 'DISPLAY_TOUCH_FRAME') THEN TRUE
            WHEN screen_kit IN ('DISPLAY_ONLY', 'DISPLAY_WITH_FRAME') THEN FALSE
            ELSE NULL
        END
        """)

    _create_view(bind, sqlite=bind.dialect.name == "sqlite")


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {col["name"] for col in insp.get_columns("competitor_item")}

    op.execute("DROP VIEW IF EXISTS vw_competitor_display_ru;")

    with op.batch_alter_table("competitor_item") as batch:
        if "Тип дисплея" in cols:
            batch.alter_column("Тип дисплея", new_column_name="screen_matrix_type")
        if "Качество" in cols:
            batch.alter_column("Качество", new_column_name="screen_quality_grade")
        if "Наличие_Рамки (в рамке)" in cols:
            batch.alter_column("Наличие_Рамки (в рамке)", new_column_name="has_frame")
        if "Наличие_Тачскрина (с тачскрином)" in cols:
            batch.drop_column("Наличие_Тачскрина (с тачскрином)")

    bind = op.get_bind()
    sqlite = bind.dialect.name == "sqlite"
    op.execute("DROP VIEW IF EXISTS vw_competitor_display_ru;")
    if sqlite:
        op.execute("""
            CREATE VIEW vw_competitor_display_ru AS
            SELECT
                id,
                competitor,
                external_id,
                name,
                CASE screen_matrix_type
                    WHEN 'LCD_TFT' THEN 'LCD (TFT)'
                    WHEN 'LCD_IPS' THEN 'LCD (IPS)'
                    WHEN 'LTPS_LCD' THEN 'LTPS LCD'
                    WHEN 'OLED' THEN 'OLED'
                    WHEN 'AMOLED' THEN 'AMOLED'
                    WHEN 'LTPO_AMOLED' THEN 'LTPO AMOLED'
                    ELSE 'Не определено'
                END AS "Тип дисплея",
                CASE
                    WHEN has_frame = 1 THEN 'Да'
                    WHEN has_frame = 0 THEN 'Нет'
                    ELSE 'Не определено'
                END AS "Наличие_Рамки (в рамке)",
                CASE
                    WHEN screen_kit IN ('DISPLAY_WITH_TOUCH', 'DISPLAY_TOUCH_FRAME') THEN 'Да'
                    WHEN screen_kit IN ('DISPLAY_ONLY', 'DISPLAY_WITH_FRAME') THEN 'Нет'
                    ELSE 'Не определено'
                END AS "Наличие_Тачскрина (с тачскрином)",
                CASE screen_quality_grade
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
                END AS "Качество"
            FROM competitor_item;
            """)
    else:
        op.execute("""
            CREATE VIEW vw_competitor_display_ru AS
            SELECT
                id,
                competitor,
                external_id,
                name,
                CASE screen_matrix_type
                    WHEN 'LCD_TFT' THEN 'LCD (TFT)'
                    WHEN 'LCD_IPS' THEN 'LCD (IPS)'
                    WHEN 'LTPS_LCD' THEN 'LTPS LCD'
                    WHEN 'OLED' THEN 'OLED'
                    WHEN 'AMOLED' THEN 'AMOLED'
                    WHEN 'LTPO_AMOLED' THEN 'LTPO AMOLED'
                    ELSE 'Не определено'
                END AS "Тип дисплея",
                CASE
                    WHEN has_frame IS TRUE THEN 'Да'
                    WHEN has_frame IS FALSE THEN 'Нет'
                    ELSE 'Не определено'
                END AS "Наличие_Рамки (в рамке)",
                CASE
                    WHEN screen_kit IN ('DISPLAY_WITH_TOUCH', 'DISPLAY_TOUCH_FRAME') THEN 'Да'
                    WHEN screen_kit IN ('DISPLAY_ONLY', 'DISPLAY_WITH_FRAME') THEN 'Нет'
                    ELSE 'Не определено'
                END AS "Наличие_Тачскрина (с тачскрином)",
                CASE screen_quality_grade
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
                END AS "Качество"
            FROM competitor_item;
            """)
