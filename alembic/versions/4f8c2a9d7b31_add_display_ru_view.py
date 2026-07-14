"""add RU display report view

Revision ID: 4f8c2a9d7b31
Revises: c1b2d3e4f5a6
Create Date: 2025-02-06 00:00:00.000000
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "4f8c2a9d7b31"
down_revision = "c1b2d3e4f5a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"

    op.execute("DROP VIEW IF EXISTS vw_competitor_display_ru;")

    if is_sqlite:
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
                    WHEN 'OR100' THEN 'OR100'
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
                    WHEN 'OR100' THEN 'OR100'
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


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS vw_competitor_display_ru;")
