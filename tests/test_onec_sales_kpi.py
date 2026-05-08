from app.services.onec_sales_kpi import ONEC_DAILY_SALES_KPI_SQL
from app.services.receivables_extractors import (
    EMPLOYEE_MOVEMENTS_SQL,
    EMPLOYEE_OPENING_SQL,
    PAYMENTS_SQL,
    REGULAR_OPENING_SQL,
    SALES_RETURNS_SQL,
    SETTLEMENTS_SQL,
)


def test_onec_sales_kpi_uses_document_responsible_fields() -> None:
    assert "sale._Fld4950RRef" in ONEC_DAILY_SALES_KPI_SQL
    assert "manager_ref._IDRRef = sale._Fld4950RRef" in ONEC_DAILY_SALES_KPI_SQL
    assert "ret._Fld1689RRef" in ONEC_DAILY_SALES_KPI_SQL
    assert "manager_ref._IDRRef = ret._Fld1689RRef" in ONEC_DAILY_SALES_KPI_SQL
    assert "LEFT JOIN dbo._Reference69 AS manager_ref" in ONEC_DAILY_SALES_KPI_SQL

    assert "sale._Fld4942RRef, 1) AS manager_ref" not in ONEC_DAILY_SALES_KPI_SQL
    assert "ret._Fld1682RRef, 1) AS manager_ref" not in ONEC_DAILY_SALES_KPI_SQL


def test_onec_sales_kpi_uses_historical_cost_register() -> None:
    assert "FROM dbo._AccumRg7580 AS reg" in ONEC_DAILY_SALES_KPI_SQL
    assert "SUM(CAST(reg._Fld7588 AS decimal(18, 2))) AS cost_of_sales" in ONEC_DAILY_SALES_KPI_SQL


def test_receivables_extractors_use_document_responsible_fields() -> None:
    assert "LEFT JOIN _Reference69 AS sale_actor" in SALES_RETURNS_SQL
    assert "sale_actor._IDRRef = sale._Fld4950RRef" in SALES_RETURNS_SQL
    assert "LEFT JOIN _Reference69 AS ret_actor" in SALES_RETURNS_SQL
    assert "ret_actor._IDRRef = ret._Fld1689RRef" in SALES_RETURNS_SQL
    assert "sale_actor._IDRRef = sale._Fld4942RRef" not in SALES_RETURNS_SQL
    assert "ret_actor._IDRRef = ret._Fld1682RRef" not in SALES_RETURNS_SQL

    assert "LEFT JOIN _Reference69 AS sale_actor" in PAYMENTS_SQL
    assert "sale_actor._IDRRef = base_sale._Fld4950RRef" in PAYMENTS_SQL
    assert "sale_actor._IDRRef = base_sale._Fld4942RRef" not in PAYMENTS_SQL


def test_receivables_extractors_include_card_payment_documents_in_summary_register() -> None:
    assert "r._RecorderTRef IN (0x000000BA, 0x000000A9)" in PAYMENTS_SQL
    assert "LEFT JOIN _Document169 AS doc169" in PAYMENTS_SQL
    assert "COALESCE(doc186._Number, doc169._Number) AS external_document_number" in PAYMENTS_SQL


def test_employee_movements_use_register_signed_amount_without_sale_recalculation() -> None:
    assert "linked_sale_amounts" not in EMPLOYEE_MOVEMENTS_SQL
    assert "_Document203_VT4966" not in EMPLOYEE_MOVEMENTS_SQL
    assert "CASE\n                WHEN r._RecordKind = 0 THEN r._Fld7620" in (
        EMPLOYEE_MOVEMENTS_SQL
    )


def test_regular_opening_uses_opening_date_snapshot_instead_of_previous_period() -> None:
    assert "t._Period <= CAST(:opening_balance_date AS datetime)" in REGULAR_OPENING_SQL
    assert "t._Period < CAST(:opening_balance_date AS datetime)" not in REGULAR_OPENING_SQL


def test_receivables_extractors_are_scoped_to_master_mobile_organization() -> None:
    assert "WHERE _Description = N'MASTER MOBILE'" in REGULAR_OPENING_SQL
    assert "WHERE _Description = N'MASTER MOBILE'" in SALES_RETURNS_SQL
    assert "WHERE _Description = N'MASTER MOBILE'" in PAYMENTS_SQL
    assert "WHERE _Description = N'MASTER MOBILE'" in SETTLEMENTS_SQL
    assert "WHERE _Description = N'MASTER MOBILE'" in EMPLOYEE_MOVEMENTS_SQL

    assert "t._Fld7558RRef IN (SELECT _IDRRef FROM target_organization)" in REGULAR_OPENING_SQL
    assert "t._Fld7618RRef IN (SELECT _IDRRef FROM target_organization)" in EMPLOYEE_OPENING_SQL
    assert "r._Fld7558RRef IN (SELECT _IDRRef FROM target_organization)" in SALES_RETURNS_SQL
    assert "pko._Fld4680RRef IN (SELECT _IDRRef FROM target_organization)" in PAYMENTS_SQL
    assert "doc._Fld4843RRef IN (SELECT _IDRRef FROM target_organization)" in SETTLEMENTS_SQL
    assert "r._Fld7618RRef IN (SELECT _IDRRef FROM target_organization)" in EMPLOYEE_MOVEMENTS_SQL
