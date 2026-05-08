"""add matrix tags column for display parsing

Revision ID: b2c3d4e5f6a7
Revises: 9f2c1d4a8b55
Create Date: 2025-02-06 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision = "b2c3d4e5f6a7"
down_revision = "9f2c1d4a8b55"
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
                ci."Теги_Матрицы" AS "Теги_Матрицы"
            FROM competitor_item ci;
            """)


def upgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"
    insp = sa.inspect(bind)
    cols = {col["name"] for col in insp.get_columns("competitor_item")}

    with op.batch_alter_table("competitor_item") as batch:
        if "Теги_Матрицы" not in cols:
            batch.add_column(
                sa.Column(
                    "Теги_Матрицы",
                    sa.JSON().with_variant(postgresql.JSONB, "postgresql"),
                    nullable=True,
                )
            )

    _create_view(sqlite=is_sqlite)


def downgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"
    insp = sa.inspect(bind)
    cols = {col["name"] for col in insp.get_columns("competitor_item")}

    op.execute("DROP VIEW IF EXISTS vw_competitor_display_ru;")
    with op.batch_alter_table("competitor_item") as batch:
        if "Теги_Матрицы" in cols:
            batch.drop_column("Теги_Матрицы")

    # Restore view without tags column
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
