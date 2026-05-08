"""add family matching to bi onec competitor model map view

Revision ID: 3d4e5f6a7b8
Revises: 2cc3d4e5f6a7
Create Date: 2026-04-18 20:15:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3d4e5f6a7b8"
down_revision: str | None = "2cc3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _drop_view() -> None:
    op.execute("DROP VIEW IF EXISTS vw_bi_onec_competitor_model_map;")


UPGRADE_SQL = """
CREATE VIEW vw_bi_onec_competitor_model_map AS
WITH product_stage AS (
    SELECT
        ppm.id AS product_phone_model_link_id,
        ppm.product_id AS product_id,
        p.article AS product_article,
        p.fact_sku AS product_fact_sku,
        p.planned_sku AS product_planned_sku,
        p.code_1c AS product_code_1c,
        p.info_system_code AS product_info_system_code,
        p.name AS product_name,
        p.brand AS product_brand,
        p.category AS product_category,
        p.subject AS product_subject,
        p.is_active AS product_is_active,
        ppm.source AS product_link_source,
        ppm.raw_value AS product_raw_value,
        ppm.confidence AS product_confidence,
        ppm.is_manual AS product_is_manual,
        pm.id AS phone_model_id,
        pm.brand AS canonical_brand,
        pm.model_name AS canonical_model_name,
        pm.variant AS canonical_variant,
        trim(
            concat_ws(
                ' ',
                pm.brand,
                pm.model_name,
                nullif(pm.variant, '')
            )
        ) AS canonical_full_model,
        trim(
            concat_ws(
                ' ',
                pm.brand,
                pm.model_name,
                nullif(pm.variant, '')
            )
        ) AS product_canonical_full_model,
        pm.is_active AS canonical_is_active,
        lower(
            trim(
                concat_ws(
                    ' ',
                    p.name,
                    ppm.raw_value,
                    pm.brand,
                    pm.model_name,
                    nullif(pm.variant, '')
                )
            )
        ) AS product_match_text
    FROM product_phone_model ppm
    JOIN product p ON p.id = ppm.product_id
    JOIN phone_models pm ON pm.id = ppm.phone_model_id
    WHERE ppm.source = 'onec'
),
product_base AS (
    SELECT
        ps.*,
        CASE
            WHEN lower(coalesce(ps.canonical_brand, '')) <> 'apple' THEN NULL
            WHEN lower(coalesce(ps.product_subject, '')) <> 'дисплей' THEN NULL
            WHEN lower(coalesce(ps.canonical_model_name, '')) = 'iphone 12'
                AND coalesce(lower(ps.canonical_variant), '') IN ('', 'pro')
                THEN 'APL-IPH1212P'
            WHEN ps.product_match_text ~ 'ipad\\s*mini\\s*2.*ipad\\s*mini\\s*3'
                OR ps.product_match_text ~ 'ipad\\s*mini\\s*3.*ipad\\s*mini\\s*2'
                OR ps.product_match_text ~ 'a1489|a1490|a1491|a1599|a1600'
                THEN 'APL-IPDMN23'
            WHEN ps.product_match_text ~ 'ipad\\s*mini\\s*5'
                OR ps.product_match_text ~ 'a2124|a2125|a2126|a2133'
                THEN 'APL-IPDMN5'
            WHEN ps.product_match_text ~ 'ipad\\s*mini\\s*6'
                OR ps.product_match_text ~ 'a2567|a2568|a2569'
                THEN 'APL-IPDMN6'
            WHEN ps.product_match_text ~ 'ipad\\s*mini\\s*7'
                OR ps.product_match_text ~ 'a2993|a2995'
                THEN 'APL-IPDMN7'
            WHEN ps.product_match_text ~ 'ipad\\s*air\\s*(1|5)\\s*9\\.?7'
                OR ps.product_match_text ~ 'a1474|a1475|a1476|a1822|a1823'
                THEN 'APL-IPDA15'
            WHEN ps.product_match_text ~ 'ipad\\s*air\\s*2'
                OR ps.product_match_text ~ 'a1566|a1567'
                THEN 'APL-IPDA2'
            WHEN ps.product_match_text ~ 'ipad\\s*air\\s*3\\D*10\\.?5'
                OR ps.product_match_text ~ 'a2123|a2152|a2153'
                THEN 'APL-IPDA3'
            WHEN ps.product_match_text ~ 'ipad\\s*air\\s*4\\D+10\\.?9'
                OR ps.product_match_text ~ 'a2072|a2316|a2324|a2325'
                THEN 'APL-IPDA4'
            WHEN ps.product_match_text ~ 'ipad\\s*3.*ipad\\s*4'
                OR ps.product_match_text ~ 'ipad\\s*4.*ipad\\s*3'
                OR ps.product_match_text ~ 'a1416|a1430|a1403|a1458|a1459|a1460'
                THEN 'APL-IPD34'
            WHEN ps.product_match_text ~ 'ipad\\s*pro\\s*9\\.?7\\D*2016'
                OR ps.product_match_text ~ 'a1673|a1674|a1675'
                THEN 'APL-IPDP972016'
            WHEN ps.product_match_text ~ 'ipad\\s*pro.*11(?:\\.?0)?.*(2021|2022)'
                OR ps.product_match_text ~ 'a2301|a2377|a2459|a2435|a2761|a2759'
                THEN 'APL-IPDP112122'
            WHEN ps.product_match_text ~ 'ipad\\s*[789].*10\\.?2'
                OR ps.product_match_text ~ 'a2197|a2198|a2200|a2428|a2429|a2270|a2602|a2603|a2604'
                THEN 'APL-IPD789102'
            WHEN ps.product_match_text ~ 'ipad\\s*pro.*11(?:\\.?0)?.*(2018|2020)'
                OR ps.product_match_text ~ 'a1934|a1980|a2013|a2068|a2228|a2230'
                THEN 'APL-IPDP1118'
            WHEN ps.product_match_text ~ 'ipad\\s*pro.*12\\.?9.*(2018|2020)'
                OR ps.product_match_text ~ 'a1876|a1895|a2014|a2069|a2229|a2232|a2233'
                THEN 'APL-IPDP12918'
            WHEN ps.product_match_text ~ 'ipad\\s*pro.*12\\.?9.*(2021|2022)'
                OR ps.product_match_text ~ 'a2378|a2379|a2461|a2462|a2436|a2437|a2764|a2766'
                THEN 'APL-IPDP12921'
            WHEN ps.product_match_text ~ 'ipad\\s*air\\s*(6|7)\\D+11'
                OR ps.product_match_text ~ 'ipad\\s*air\\s*6/7\\D+11'
                OR ps.product_match_text ~ 'a3267'
                THEN 'APL-IPDA6711'
            WHEN ps.product_match_text ~ 'ipad\\s*air\\s*(6|7)\\D+13'
                OR ps.product_match_text ~ 'ipad\\s*air\\s*6/7\\D+13'
                OR ps.product_match_text ~ 'a3269|a3271'
                THEN 'APL-IPDA6713'
            WHEN ps.product_match_text ~ 'ipad\\s*10\\D+10\\.?9'
                OR ps.product_match_text ~ 'ipad\\s*10\\.?9\\D*2022'
                OR ps.product_match_text ~ 'a2696|a2757|a2777'
                THEN 'APL-IPD10'
            WHEN ps.product_match_text ~ 'ipad\\s*11\\D+11\\.?0'
                OR ps.product_match_text ~ 'a3354|a3355|a3356'
                THEN 'APL-IPD11'
            ELSE NULL
        END AS product_family_code
    FROM product_stage ps
),
competitor_stage AS (
    SELECT
        cic.id AS competitor_compatibility_id,
        cic.competitor_item_id AS competitor_item_id,
        cic.phone_model_id AS competitor_phone_model_id,
        ci.competitor AS competitor,
        ci.external_id AS competitor_sku,
        ci.name AS competitor_name,
        ci.category AS competitor_category,
        ci.item_type AS competitor_item_type,
        ci.is_active AS competitor_is_active,
        ci.parsed_device_brand AS competitor_parsed_brand,
        ci.parsed_device_model AS competitor_parsed_model,
        ci.parsed_device_variant AS competitor_parsed_variant,
        trim(
            concat_ws(
                ' ',
                ci.parsed_device_brand,
                ci.parsed_device_model,
                nullif(ci.parsed_device_variant, '')
            )
        ) AS competitor_parsed_full_model,
        ci.parse_status AS competitor_parse_status,
        ci.parse_error AS competitor_parse_error,
        ci.parse_version AS competitor_parse_version,
        cic.source AS competitor_link_source,
        cic.device_brand AS competitor_raw_brand,
        cic.device_model AS competitor_raw_model,
        cic.device_variant AS competitor_raw_variant,
        trim(
            concat_ws(
                ' ',
                cic.device_brand,
                cic.device_model,
                nullif(cic.device_variant, '')
            )
        ) AS competitor_raw_full_model,
        cic.notes AS competitor_notes,
        lower(
            trim(
                concat_ws(
                    ' ',
                    ci.name,
                    ci.category,
                    cic.device_brand,
                    cic.device_model,
                    nullif(cic.device_variant, ''),
                    ci.parsed_device_brand,
                    ci.parsed_device_model,
                    nullif(ci.parsed_device_variant, '')
                )
            )
        ) AS competitor_match_text
    FROM competitor_item_compatibility cic
    LEFT JOIN competitor_item ci ON ci.id = cic.competitor_item_id
),
competitor_base AS (
    SELECT
        cs.*,
        CASE
            WHEN lower(coalesce(cs.competitor_raw_brand, '')) <> 'apple' THEN NULL
            WHEN lower(coalesce(cs.competitor_category, '')) <> 'дисплей' THEN NULL
            WHEN cs.competitor_match_text ~ 'iphone\\s*12\\s*/\\s*12\\s*pro'
                OR cs.competitor_match_text ~ 'iphone\\s*12\\s*pro\\s*/\\s*12'
                OR cs.competitor_match_text ~ 'a2403|a2407'
                THEN 'APL-IPH1212P'
            WHEN cs.competitor_match_text ~ 'ipad\\s*mini\\s*2.*ipad\\s*mini\\s*3'
                OR cs.competitor_match_text ~ 'ipad\\s*mini\\s*3.*ipad\\s*mini\\s*2'
                OR cs.competitor_match_text ~ 'a1489|a1490|a1491|a1599|a1600'
                THEN 'APL-IPDMN23'
            WHEN cs.competitor_match_text ~ 'ipad\\s*mini\\s*5'
                OR cs.competitor_match_text ~ 'a2124|a2125|a2126|a2133'
                THEN 'APL-IPDMN5'
            WHEN cs.competitor_match_text ~ 'ipad\\s*mini\\s*6'
                OR cs.competitor_match_text ~ 'a2567|a2568|a2569'
                THEN 'APL-IPDMN6'
            WHEN cs.competitor_match_text ~ 'ipad\\s*mini\\s*7'
                OR cs.competitor_match_text ~ 'a2993|a2995'
                THEN 'APL-IPDMN7'
            WHEN cs.competitor_match_text ~ 'ipad\\s*air\\s*(1|5)\\s*9\\.?7'
                OR cs.competitor_match_text ~ 'a1474|a1475|a1476|a1822|a1823'
                THEN 'APL-IPDA15'
            WHEN cs.competitor_match_text ~ 'ipad\\s*air\\s*2'
                OR cs.competitor_match_text ~ 'a1566|a1567'
                THEN 'APL-IPDA2'
            WHEN cs.competitor_match_text ~ 'ipad\\s*air\\s*3\\D*10\\.?5'
                OR cs.competitor_match_text ~ 'a2123|a2152|a2153'
                THEN 'APL-IPDA3'
            WHEN cs.competitor_match_text ~ 'ipad\\s*air\\s*4\\D+10\\.?9'
                OR cs.competitor_match_text ~ 'a2072|a2316|a2324|a2325'
                THEN 'APL-IPDA4'
            WHEN cs.competitor_match_text ~ 'ipad\\s*3.*ipad\\s*4'
                OR cs.competitor_match_text ~ 'ipad\\s*4.*ipad\\s*3'
                OR cs.competitor_match_text ~ 'a1416|a1430|a1403|a1458|a1459|a1460'
                THEN 'APL-IPD34'
            WHEN cs.competitor_match_text ~ 'ipad\\s*pro\\s*9\\.?7\\D*2016'
                OR cs.competitor_match_text ~ 'a1673|a1674|a1675'
                THEN 'APL-IPDP972016'
            WHEN cs.competitor_match_text ~ 'ipad\\s*pro.*11(?:\\.?0)?.*(2021|2022)'
                OR cs.competitor_match_text ~ 'a2301|a2377|a2459|a2435|a2761|a2759'
                THEN 'APL-IPDP112122'
            WHEN cs.competitor_match_text ~ 'ipad\\s*[789].*10\\.?2'
                OR cs.competitor_match_text ~ 'a2197|a2198|a2200|a2428|a2429|a2270|a2602|a2603|a2604'
                THEN 'APL-IPD789102'
            WHEN cs.competitor_match_text ~ 'ipad\\s*pro.*11(?:\\.?0)?.*(2018|2020)'
                OR cs.competitor_match_text ~ 'a1934|a1980|a2013|a2068|a2228|a2230'
                THEN 'APL-IPDP1118'
            WHEN cs.competitor_match_text ~ 'ipad\\s*pro.*12\\.?9.*(2018|2020)'
                OR cs.competitor_match_text ~ 'a1876|a1895|a2014|a2069|a2229|a2232|a2233'
                THEN 'APL-IPDP12918'
            WHEN cs.competitor_match_text ~ 'ipad\\s*pro.*12\\.?9.*(2021|2022)'
                OR cs.competitor_match_text ~ 'a2378|a2379|a2461|a2462|a2436|a2437|a2764|a2766'
                THEN 'APL-IPDP12921'
            WHEN cs.competitor_match_text ~ 'ipad\\s*air\\s*(6|7)\\D+11'
                OR cs.competitor_match_text ~ 'ipad\\s*air\\s*6/7\\D+11'
                OR cs.competitor_match_text ~ 'a3267'
                THEN 'APL-IPDA6711'
            WHEN cs.competitor_match_text ~ 'ipad\\s*air\\s*(6|7)\\D+13'
                OR cs.competitor_match_text ~ 'ipad\\s*air\\s*6/7\\D+13'
                OR cs.competitor_match_text ~ 'a3269|a3271'
                THEN 'APL-IPDA6713'
            WHEN cs.competitor_match_text ~ 'ipad\\s*10\\D+10\\.?9'
                OR cs.competitor_match_text ~ 'ipad\\s*10\\.?9\\D*2022'
                OR cs.competitor_match_text ~ 'a2696|a2757|a2777'
                THEN 'APL-IPD10'
            WHEN cs.competitor_match_text ~ 'ipad\\s*11\\D+11\\.?0'
                OR cs.competitor_match_text ~ 'a3354|a3355|a3356'
                THEN 'APL-IPD11'
            ELSE NULL
        END AS competitor_family_code
    FROM competitor_stage cs
)
SELECT
    pb.product_phone_model_link_id,
    pb.product_id,
    pb.product_article,
    pb.product_fact_sku,
    pb.product_planned_sku,
    pb.product_code_1c,
    pb.product_info_system_code,
    pb.product_name,
    pb.product_brand,
    pb.product_category,
    pb.product_subject,
    pb.product_is_active,
    pb.product_link_source,
    pb.product_raw_value,
    pb.product_confidence,
    pb.product_is_manual,
    pb.phone_model_id,
    pb.canonical_brand,
    pb.canonical_model_name,
    pb.canonical_variant,
    pb.canonical_full_model,
    pb.product_canonical_full_model,
    pb.canonical_is_active,
    cb.competitor_compatibility_id,
    cb.competitor_item_id,
    cb.competitor,
    cb.competitor_sku,
    cb.competitor_name,
    cb.competitor_category,
    cb.competitor_item_type,
    cb.competitor_is_active,
    cb.competitor_parsed_brand,
    cb.competitor_parsed_model,
    cb.competitor_parsed_variant,
    cb.competitor_parsed_full_model,
    cb.competitor_parse_status,
    cb.competitor_parse_error,
    cb.competitor_parse_version,
    cb.competitor_link_source,
    cb.competitor_raw_brand,
    cb.competitor_raw_model,
    cb.competitor_raw_variant,
    cb.competitor_raw_full_model,
    CASE
        WHEN cb.competitor_compatibility_id IS NULL THEN NULL
        ELSE trim(
            concat_ws(
                ' ',
                pb.canonical_brand,
                pb.canonical_model_name,
                nullif(pb.canonical_variant, '')
            )
        )
    END AS competitor_canonical_full_model,
    cb.competitor_notes,
    pb.product_family_code,
    cb.competitor_family_code,
    CASE
        WHEN cb.competitor_compatibility_id IS NULL THEN NULL
        WHEN cb.competitor_phone_model_id = pb.phone_model_id THEN 'exact_phone_model'
        WHEN pb.product_family_code IS NOT NULL
            AND pb.product_family_code = cb.competitor_family_code
            THEN 'apple_display_family'
        ELSE 'exact_phone_model'
    END AS match_strategy,
    CASE
        WHEN cb.competitor_compatibility_id IS NULL THEN 'only_1c'
        ELSE 'matched_1c_to_competitor'
    END AS mapping_status
FROM product_base pb
LEFT JOIN competitor_base cb
    ON cb.competitor_phone_model_id = pb.phone_model_id
    OR (
        pb.product_family_code IS NOT NULL
        AND pb.product_family_code = cb.competitor_family_code
        AND coalesce(cb.competitor_phone_model_id, -1) <> pb.phone_model_id
    );
"""


DOWNGRADE_SQL = """
CREATE VIEW vw_bi_onec_competitor_model_map AS
SELECT
    ppm.id AS product_phone_model_link_id,
    ppm.product_id AS product_id,
    p.article AS product_article,
    p.fact_sku AS product_fact_sku,
    p.planned_sku AS product_planned_sku,
    p.code_1c AS product_code_1c,
    p.info_system_code AS product_info_system_code,
    p.name AS product_name,
    p.brand AS product_brand,
    p.category AS product_category,
    p.subject AS product_subject,
    p.is_active AS product_is_active,
    ppm.source AS product_link_source,
    ppm.raw_value AS product_raw_value,
    ppm.confidence AS product_confidence,
    ppm.is_manual AS product_is_manual,
    pm.id AS phone_model_id,
    pm.brand AS canonical_brand,
    pm.model_name AS canonical_model_name,
    pm.variant AS canonical_variant,
    trim(
        concat_ws(
            ' ',
            pm.brand,
            pm.model_name,
            nullif(pm.variant, '')
        )
    ) AS canonical_full_model,
    trim(
        concat_ws(
            ' ',
            pm.brand,
            pm.model_name,
            nullif(pm.variant, '')
        )
    ) AS product_canonical_full_model,
    pm.is_active AS canonical_is_active,
    cic.id AS competitor_compatibility_id,
    cic.competitor_item_id AS competitor_item_id,
    ci.competitor AS competitor,
    ci.external_id AS competitor_sku,
    ci.name AS competitor_name,
    ci.category AS competitor_category,
    ci.item_type AS competitor_item_type,
    ci.is_active AS competitor_is_active,
    ci.parsed_device_brand AS competitor_parsed_brand,
    ci.parsed_device_model AS competitor_parsed_model,
    ci.parsed_device_variant AS competitor_parsed_variant,
    trim(
        concat_ws(
            ' ',
            ci.parsed_device_brand,
            ci.parsed_device_model,
            nullif(ci.parsed_device_variant, '')
        )
    ) AS competitor_parsed_full_model,
    ci.parse_status AS competitor_parse_status,
    ci.parse_error AS competitor_parse_error,
    ci.parse_version AS competitor_parse_version,
    cic.source AS competitor_link_source,
    cic.device_brand AS competitor_raw_brand,
    cic.device_model AS competitor_raw_model,
    cic.device_variant AS competitor_raw_variant,
    trim(
        concat_ws(
            ' ',
            cic.device_brand,
            cic.device_model,
            nullif(cic.device_variant, '')
        )
    ) AS competitor_raw_full_model,
    CASE
        WHEN cic.id IS NULL THEN NULL
        ELSE trim(
            concat_ws(
                ' ',
                pm.brand,
                pm.model_name,
                nullif(pm.variant, '')
            )
        )
    END AS competitor_canonical_full_model,
    cic.notes AS competitor_notes,
    CASE
        WHEN cic.id IS NULL THEN 'only_1c'
        ELSE 'matched_1c_to_competitor'
    END AS mapping_status
FROM product_phone_model ppm
JOIN product p ON p.id = ppm.product_id
JOIN phone_models pm ON pm.id = ppm.phone_model_id
LEFT JOIN competitor_item_compatibility cic ON cic.phone_model_id = pm.id
LEFT JOIN competitor_item ci ON ci.id = cic.competitor_item_id
WHERE ppm.source = 'onec';
"""


def upgrade() -> None:
    _drop_view()
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    _drop_view()
    op.execute(DOWNGRADE_SQL)
