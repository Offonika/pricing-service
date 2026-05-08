"""add binding without solder column to competitor items

Revision ID: a8b9c0d1e2f3
Revises: f7a8b9c0d1e2
Create Date: 2025-02-06 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "a8b9c0d1e2f3"
down_revision = "f7a8b9c0d1e2"
branch_labels = None
depends_on = None


def _create_view(*, sqlite: bool) -> None:
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
                CASE
                    WHEN ci."Площадка под IC" = 1 THEN 'Да'
                    WHEN ci."Площадка под IC" = 0 THEN 'Нет'
                    ELSE 'Не определено'
                END AS "Площадка под IC",
                CASE
                    WHEN ci."Привязка без пайки" = 1 THEN 'Да'
                    WHEN ci."Привязка без пайки" = 0 THEN 'Нет'
                    ELSE 'Не определено'
                END AS "Привязка без пайки",
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
                END AS "Качество",
                ci."Производитель" AS "Производитель",
                ci."Цвет" AS "Цвет",
                ci."Теги_Матрицы" AS "Теги_Матрицы"
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
                CASE
                    WHEN ci."Площадка под IC" IS TRUE THEN 'Да'
                    WHEN ci."Площадка под IC" IS FALSE THEN 'Нет'
                    ELSE 'Не определено'
                END AS "Площадка под IC",
                CASE
                    WHEN ci."Привязка без пайки" IS TRUE THEN 'Да'
                    WHEN ci."Привязка без пайки" IS FALSE THEN 'Нет'
                    ELSE 'Не определено'
                END AS "Привязка без пайки",
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
                END AS "Качество",
                ci."Производитель" AS "Производитель",
                ci."Цвет" AS "Цвет",
                ci."Теги_Матрицы" AS "Теги_Матрицы"
            FROM competitor_item ci;
            """)


def upgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"
    insp = sa.inspect(bind)
    cols = {col["name"] for col in insp.get_columns("competitor_item")}

    with op.batch_alter_table("competitor_item") as batch:
        if "Привязка без пайки" not in cols:
            batch.add_column(sa.Column("Привязка без пайки", sa.Boolean(), nullable=True))

    _create_view(sqlite=is_sqlite)


def downgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"
    insp = sa.inspect(bind)
    cols = {col["name"] for col in insp.get_columns("competitor_item")}

    op.execute("DROP VIEW IF EXISTS vw_competitor_display_ru;")
    with op.batch_alter_table("competitor_item") as batch:
        if "Привязка без пайки" in cols:
            batch.drop_column("Привязка без пайки")

    if is_sqlite:
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
                CASE
                    WHEN ci."Площадка под IC" = 1 THEN 'Да'
                    WHEN ci."Площадка под IC" = 0 THEN 'Нет'
                    ELSE 'Не определено'
                END AS "Площадка под IC",
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
                END AS "Качество",
                ci."Производитель" AS "Производитель",
                ci."Цвет" AS "Цвет",
                ci."Теги_Матрицы" AS "Теги_Матрицы"
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
                CASE
                    WHEN ci."Площадка под IC" IS TRUE THEN 'Да'
                    WHEN ci."Площадка под IC" IS FALSE THEN 'Нет'
                    ELSE 'Не определено'
                END AS "Площадка под IC",
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
                END AS "Качество",
                ci."Производитель" AS "Производитель",
                ci."Цвет" AS "Цвет",
                ci."Теги_Матрицы" AS "Теги_Матрицы"
            FROM competitor_item ci;
            """)
