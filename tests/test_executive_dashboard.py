from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api import bitrix_executive_dashboard as executive_dashboard_page
from app.api.dependencies import get_db
from app.core.config import Settings
from app.main import app
from app.models import (
    ExecutiveActionItem,
    OneCSalesDailyKpi,
    ReceivableCase,
    ReceivableFolderRecommendationCache,
    ReceivableWorkItem,
)
from app.schemas.executive_dashboard import (
    ExecutiveProfitLossInventoryDataQuality,
    ExecutiveProfitLossInventoryLoss,
    ExecutiveProfitLossInventoryStore,
)
from app.services import bitrix_executive_dashboard_auth, executive_dashboard
from app.services.executive_dashboard import (
    _resolve_shared_path,
    build_executive_actions_response,
    build_executive_cashflow_period_response,
    build_executive_dashboard,
    build_executive_profit_loss_period_response,
    build_executive_sales_period_response,
)
from app.services.onec_inventory_cost import OneCInventoryCostSnapshot


def _settings(snapshot_path: Path, *, access_rules_json: str | None = None) -> Settings:
    settings = Settings(
        onec_database_url=None,
        management_internal_api_token="secret-token",
        executive_dashboard_finance_snapshot_path=str(snapshot_path),
        executive_dashboard_cashflow_period_cache_path=str(
            snapshot_path.parent / "cashflow_period_cache.json"
        ),
        executive_dashboard_warehouse_snapshot_path=str(
            snapshot_path.parent / "warehouse_snapshot.json"
        ),
        executive_dashboard_owner_cash_control_snapshot_path=str(
            snapshot_path.parent / "owner_cash_transit_snapshot.json"
        ),
        executive_dashboard_sales_plan_snapshot_path=str(
            snapshot_path.parent / "sales_plan_monthly_snapshot.json"
        ),
        executive_dashboard_bitrix_enabled=True,
        executive_dashboard_bitrix_allowed_domains=["crm.master-mobile.ru"],
        executive_dashboard_bitrix_allowed_member_ids=["member-1"],
        executive_dashboard_bitrix_full_access_user_ids=["42"],
        executive_dashboard_bitrix_domain_access_user_ids=["77"],
        executive_dashboard_access_rules_json=access_rules_json,
        executive_dashboard_bitrix_session_secret="test-executive-dashboard-session-secret",
    )
    settings.executive_dashboard_bp_tax_accrual_root = str(snapshot_path.parent / "bp-tax-accruals")
    return settings


def _override_settings(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    monkeypatch.setattr(executive_dashboard, "get_settings", lambda: settings)
    monkeypatch.setattr(bitrix_executive_dashboard_auth, "get_settings", lambda: settings)


def _receivable_case(
    as_of: date,
    *,
    balance: Decimal = Decimal("12500.00"),
    counterparty_ref: str = "cp-1",
    counterparty_name: str = "Клиент 1",
    segment: str = "buyers",
    phone_status: str | None = None,
) -> ReceivableCase:
    return ReceivableCase(
        snapshot_date=as_of,
        segment=segment,
        owner_type="finance",
        recommendation="Проверить оплату покупателя",
        counterparty_ref=counterparty_ref,
        counterparty_name=counterparty_name,
        current_balance=balance,
        origin_document_ref="sale-1",
        origin_document_number="РБГУ0001",
        origin_document_date=datetime(2026, 6, 1, 12, 0),
        current_manager_ref="mgr-1",
        current_manager_name="Менеджер 1",
        department_ref="dep-1",
        department_name="01. Горбушкин Двор",
        planned_payment_date=datetime(2026, 6, 28),
        due_date=datetime(2026, 6, 10),
        overdue_days=17,
        is_overdue=True,
        aged_bucket="1-30",
        activity_segment="active",
    )


def _work_item(
    *,
    counterparty_ref: str = "cp-1",
    counterparty_name: str = "Клиент 1",
    status: str = "new_debt",
    phone_status: str = "present",
    payment_postponed_count: int = 0,
) -> ReceivableWorkItem:
    return ReceivableWorkItem(
        stable_key=f"receivable-workplace:{counterparty_ref}",
        counterparty_ref=counterparty_ref,
        counterparty_name=counterparty_name,
        status=status,
        current_balance=Decimal("12500.00"),
        phone_status=phone_status,
        needs_call_today=True,
        promised_payment_date=datetime(2026, 6, 28),
        payload={"payment_postponed_count": payment_postponed_count},
    )


def _folder_cache(as_of: date) -> ReceivableFolderRecommendationCache:
    return ReceivableFolderRecommendationCache(
        snapshot_date=as_of,
        status_scope="all",
        report_revision="r1",
        source_status="cached",
        summary={
            "needs_review_count": 3,
            "move_recommended_count": 0,
            "total_count": 8,
        },
        payload=[],
    )


def _action(
    *,
    stable_key: str = "creditors:1",
    domain: str = "creditors_payables",
    amount: Decimal | None = Decimal("3000.00"),
    responsible_bitrix_user_id: str | None = "77",
) -> ExecutiveActionItem:
    return ExecutiveActionItem(
        stable_key=stable_key,
        business_date=date(2026, 6, 27),
        domain=domain,
        severity="critical",
        title="Подтвердить оплату поставщику",
        description="Платеж сегодня, нужен владелец решения.",
        amount=amount,
        currency="RUB",
        responsible_bitrix_user_id=responsible_bitrix_user_id,
        deadline_at=datetime(2026, 6, 27, 12, 0),
        status="open",
        source_system="1c_payables",
        source_ref="supplier-order-1",
        dedupe_key=f"{stable_key}:dedupe",
        drilldown_url="/bitrix/tasks/task/view/1/",
        payload={"source_anchor": "1C: Заказ поставщику -> Платежный календарь"},
    )


def _sales_kpi(
    sales_date: date,
    *,
    revenue: Decimal = Decimal("1000.00"),
    cost_of_sales: Decimal = Decimal("650.00"),
    sales_count: Decimal = Decimal("2.000"),
    manager_ref: str = "mgr-1",
    manager_name: str = "Менеджер 1",
    store_ref: str = "store-1",
    store_name: str = "Горбушкин Двор",
) -> OneCSalesDailyKpi:
    return OneCSalesDailyKpi(
        sales_date=sales_date,
        manager_ref=manager_ref,
        manager_name=manager_name,
        store_ref=store_ref,
        store_name=store_name,
        revenue=revenue,
        cost_of_sales=cost_of_sales,
        sales_count=sales_count,
    )


def _write_sales_plan_snapshot(
    path: Path,
    *,
    period_month: str = "2026-06",
    stores: list[dict[str, object]] | None = None,
) -> None:
    store_rows = stores or [
        {
            "scope_key": "store-1",
            "scope_name": "Горбушкин Двор",
            "approved_revenue": "3000.00",
            "approved_margin_pct": "35.00",
            "approved_gross_profit": "1050.00",
        },
        {
            "scope_key": "store-2",
            "scope_name": "Склад Сайт",
            "approved_revenue": "6000.00",
            "approved_margin_pct": "50.00",
            "approved_gross_profit": "3000.00",
        },
    ]
    network_revenue = sum(
        (Decimal(str(row["approved_revenue"])) for row in store_rows), Decimal("0")
    )
    network_gross_profit = sum(
        (Decimal(str(row["approved_gross_profit"])) for row in store_rows), Decimal("0")
    )
    network_margin = (
        Decimal("0")
        if network_revenue == 0
        else network_gross_profit / network_revenue * Decimal("100")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generated_at": "2026-06-05T12:00:00+00:00",
                "source_status": "ready",
                "note": None,
                "months": [
                    {
                        "period_month": period_month,
                        "revision_no": 3,
                        "snapshot_id": "snapshot-2026-06-v3",
                        "frozen_at": "2026-06-01T09:00:00+00:00",
                        "source_status": "ready",
                        "note": None,
                        "network": {
                            "scope_key": "network",
                            "scope_name": "Сеть",
                            "approved_revenue": str(network_revenue),
                            "approved_margin_pct": str(network_margin),
                            "approved_gross_profit": str(network_gross_profit),
                        },
                        "stores": store_rows,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _write_cashflow_period_cache(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "generated_at": "2026-06-28T12:00:00+00:00",
                "source_status": "ready",
                "freshness_status": "fresh",
                "period": {"date_from": "2026-06-01", "date_to": "2026-06-30", "days": 30},
                "cash_position": {
                    "rows": [
                        {
                            "snapshot_date": "2026-05-31",
                            "source_status": "ready",
                            "total_balance": "1000.00",
                        },
                        {
                            "snapshot_date": "2026-06-28",
                            "source_status": "ready",
                            "total_balance": "1500.00",
                        },
                    ]
                },
                "rows": [
                    {
                        "business_date": "2026-06-27",
                        "article_key": "customer_payments",
                        "article_name": "Оплата от покупателей",
                        "dds_group": "operating",
                        "group_label": "Операционная деятельность",
                        "direction": "inflow",
                        "is_internal_transfer": False,
                        "cash_account_ref_hex": "bank-1",
                        "cash_account_name": "Сбер",
                        "cash_currency_code": "RUB",
                        "currency_name": "руб",
                        "inflow_amount": "1000.00",
                        "outflow_amount": "0",
                        "net_amount": "1000.00",
                        "movement_count": 1,
                        "review_count": 0,
                        "profit_loss_class": "ignored",
                        "profit_loss_recognition_method": "cashflow_fallback",
                    },
                    {
                        "business_date": "2026-06-28",
                        "article_key": "suppliers",
                        "article_name": "Поставщики",
                        "dds_group": "operating",
                        "group_label": "Операционная деятельность",
                        "direction": "outflow",
                        "is_internal_transfer": False,
                        "cash_account_ref_hex": "bank-1",
                        "cash_account_name": "Сбер",
                        "cash_currency_code": "RUB",
                        "currency_name": "руб",
                        "inflow_amount": "0",
                        "outflow_amount": "400.00",
                        "net_amount": "-400.00",
                        "movement_count": 1,
                        "review_count": 1,
                        "profit_loss_class": "open_question",
                        "profit_loss_recognition_method": "cashflow_fallback",
                        "profit_loss_source_status": "partial",
                        "profit_loss_question_code": "suppliers_may_duplicate_cogs",
                        "profit_loss_question_key": (
                            "suppliers_may_duplicate_cogs:suppliers:operating:suppliers"
                        ),
                        "profit_loss_question_reason": (
                            "Поставщики похожи на закупку товара или смешанную статью; "
                            "исключено, чтобы не задвоить себестоимость."
                        ),
                        "profit_loss_question_action": (
                            "Разделить товарную закупку и сервисные расходы в статье ДДС."
                        ),
                    },
                    {
                        "business_date": "2026-06-28",
                        "article_key": "internal_transfer",
                        "article_name": "Внутренний перевод",
                        "dds_group": "internal",
                        "group_label": "Внутренние перемещения",
                        "direction": "internal",
                        "is_internal_transfer": True,
                        "cash_account_ref_hex": "cash-1",
                        "cash_account_name": "Касса",
                        "cash_currency_code": "RUB",
                        "currency_name": "руб",
                        "inflow_amount": "100.00",
                        "outflow_amount": "100.00",
                        "net_amount": "0.00",
                        "movement_count": 2,
                        "review_count": 0,
                        "profit_loss_class": "ignored",
                        "profit_loss_recognition_method": "cashflow_fallback",
                    },
                ],
                "profit_loss_expenses": {
                    "source_status": "partial",
                    "freshness_status": "partial",
                    "recognition_method": "cashflow_fallback",
                    "totals": {
                        "operating_expenses": "0.00",
                        "operating_expense_movement_count": 0,
                        "operating_expense_review_count": 0,
                        "open_question_count": 1,
                        "open_question_movement_count": 1,
                        "open_question_amount": "400.00",
                    },
                    "breakdown": [],
                    "open_questions": [],
                },
                "quality_daily": [
                    {
                        "business_date": "2026-06-28",
                        "issue_type": "missing_article",
                        "issue_label": "Документ без статьи ДДС",
                        "severity": "high",
                        "issue_count": 3,
                        "amount_abs": "1200.00",
                    }
                ],
                "quality_issues": [
                    {
                        "issue_key": "issue-1",
                        "issue_type": "missing_article",
                        "issue_label": "Документ без статьи ДДС",
                        "severity": "high",
                        "business_date": "2026-06-28",
                        "amount_abs": "400.00",
                        "description": "Нет статьи ДДС",
                        "proposed_action": "Заполнить статью",
                        "status": "open",
                    }
                ],
                "filters": {
                    "currencies": ["RUB"],
                    "groups": ["operating", "internal"],
                    "directions": ["inflow", "outflow", "internal"],
                    "cash_accounts": [{"ref": "bank-1", "name": "Сбер"}],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _write_profit_loss_cashflow_cache(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "generated_at": "2026-06-28T12:00:00+00:00",
                "source_status": "ready",
                "freshness_status": "fresh",
                "period": {"date_from": "2026-06-01", "date_to": "2026-06-30", "days": 30},
                "rows": [
                    {
                        "business_date": "2026-06-27",
                        "article_key": "rent",
                        "article_name": "Аренда",
                        "dds_group": "operating",
                        "group_label": "Операционная деятельность",
                        "dds_subgroup": "rent",
                        "subgroup_label": "Аренда",
                        "direction": "outflow",
                        "is_internal_transfer": False,
                        "cash_account_ref_hex": "bank-1",
                        "cash_account_name": "Сбер",
                        "cash_currency_code": "RUB",
                        "currency_name": "руб",
                        "inflow_amount": "0",
                        "outflow_amount": "120.00",
                        "net_amount": "-120.00",
                        "movement_count": 1,
                        "review_count": 0,
                        "profit_loss_class": "operating_expense",
                        "profit_loss_line_key": "rent",
                        "profit_loss_line_label": "Аренда",
                        "profit_loss_recognition_method": "cashflow_fallback",
                        "profit_loss_source_status": "ready",
                    },
                    {
                        "business_date": "2026-06-27",
                        "article_key": "bank_fees",
                        "article_name": "Комиссия банка",
                        "dds_group": "operating",
                        "group_label": "Операционная деятельность",
                        "dds_subgroup": "bank_fees",
                        "subgroup_label": "Комиссии банка",
                        "direction": "outflow",
                        "is_internal_transfer": False,
                        "cash_account_ref_hex": "bank-1",
                        "cash_account_name": "Сбер",
                        "cash_currency_code": "RUB",
                        "currency_name": "руб",
                        "inflow_amount": "0",
                        "outflow_amount": "30.00",
                        "net_amount": "-30.00",
                        "movement_count": 1,
                        "review_count": 0,
                        "profit_loss_class": "operating_expense",
                        "profit_loss_line_key": "bank_fees",
                        "profit_loss_line_label": "Комиссии банка",
                        "profit_loss_recognition_method": "cashflow_fallback",
                        "profit_loss_source_status": "ready",
                    },
                    {
                        "business_date": "2026-06-27",
                        "article_key": "suppliers",
                        "article_name": "Поставщики",
                        "dds_group": "operating",
                        "group_label": "Операционная деятельность",
                        "dds_subgroup": "suppliers",
                        "subgroup_label": "Поставщики",
                        "direction": "outflow",
                        "is_internal_transfer": False,
                        "cash_account_ref_hex": "bank-1",
                        "cash_account_name": "Сбер",
                        "cash_currency_code": "RUB",
                        "currency_name": "руб",
                        "inflow_amount": "0",
                        "outflow_amount": "400.00",
                        "net_amount": "-400.00",
                        "movement_count": 1,
                        "review_count": 0,
                        "profit_loss_class": "open_question",
                        "profit_loss_question_key": (
                            "suppliers_may_duplicate_cogs:suppliers:operating:suppliers"
                        ),
                        "profit_loss_question_reason": (
                            "Поставщики похожи на закупку товара или смешанную статью; "
                            "исключено, чтобы не задвоить себестоимость."
                        ),
                        "profit_loss_question_action": (
                            "Разделить товарную закупку и сервисные расходы в статье ДДС."
                        ),
                        "profit_loss_recognition_method": "cashflow_fallback",
                    },
                ],
                "filters": {
                    "currencies": ["RUB"],
                    "groups": ["operating"],
                    "directions": ["outflow"],
                    "cash_accounts": [{"ref": "bank-1", "name": "Сбер"}],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _write_warehouse_snapshot(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "as_of": "2026-06-27",
                "generated_at": "2026-06-27T12:00:00+00:00",
                "source_status": "ready",
                "freshness_status": "fresh",
                "period": {"date_from": "2026-06-01", "date_to": "2026-06-27"},
                "warehouse_operations": {
                    "source_status": "ready",
                    "freshness_status": "fresh",
                    "as_of": "2026-06-27",
                    "source_anchor": "1C: ПеремещениеТоваров -> piecework.fact_transfer_lines",
                    "transfer_document_count": 120,
                    "rows_count": 900,
                    "pieces_picked": "1250.00",
                    "picker_count": 8,
                    "avg_need_fact": "5.20",
                    "practical_max_need_fact": "9.30",
                    "rows_ge_1h": 4,
                    "rows_ge_4h": 1,
                    "quality_issue_count": 3,
                    "picker_error_count": 1,
                    "top_warehouses": [
                        {
                            "warehouse_name": "Склад Сайт",
                            "pick_hours": "42.00",
                            "pieces_picked": "700.00",
                            "picker_count": 4,
                        }
                    ],
                    "quality_breakdown": [
                        {"key": "rows_ge_1h", "label": "Строки сборки больше 1 часа", "count": 4}
                    ],
                },
                "source_freshness": {
                    "warehouse.piecework": {
                        "title": "Склад / сборка из piecework",
                        "source_status": "ready",
                        "freshness_status": "fresh",
                        "as_of": "2026-06-27",
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _role_rules() -> str:
    return json.dumps(
        {
            "roles": [
                {
                    "role": "procurement",
                    "bitrix_user_ids": ["201"],
                    "allowed_blocks": ["procurement_import"],
                    "allowed_action_domains": ["procurement_import"],
                    "money_blocks": ["procurement_import"],
                },
                {"role": "receivables", "bitrix_user_ids": ["202"]},
                {"role": "finance", "bitrix_user_ids": ["203"]},
                {"role": "warehouse", "bitrix_user_ids": ["206"]},
                {"role": "infrastructure", "bitrix_user_ids": ["207"]},
                {"role": "personal", "bitrix_user_ids": ["204"]},
                {"role": "procurement", "bitrix_user_ids": ["205"]},
                {"role": "receivables", "bitrix_user_ids": ["205"]},
            ]
        },
        ensure_ascii=False,
    )


def test_access_policy_matrix_resolves_roles_and_blocks(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "missing.json", access_rules_json=_role_rules())

    procurement = bitrix_executive_dashboard_auth.resolve_executive_dashboard_access(
        bitrix_user_id="201",
        settings=settings,
    )
    assert procurement.roles == ("procurement",)
    assert procurement.allowed_blocks == ("procurement_import",)
    assert procurement.allowed_action_domains == ("procurement_import",)
    assert procurement.money_blocks == ("procurement_import",)

    receivables = bitrix_executive_dashboard_auth.resolve_executive_dashboard_access(
        bitrix_user_id="202",
        settings=settings,
    )
    assert receivables.allowed_blocks == ("debtors", "receivables_control")
    assert receivables.money_blocks == ()

    finance = bitrix_executive_dashboard_auth.resolve_executive_dashboard_access(
        bitrix_user_id="203",
        settings=settings,
    )
    assert finance.allowed_blocks == (
        "money_today",
        "profit_loss",
        "debtors",
        "receivables_control",
        "creditors_payables",
        "reconciliation",
    )
    assert finance.money_blocks == (
        "money_today",
        "profit_loss",
        "debtors",
        "creditors_payables",
    )
    assert "procurement_import" not in finance.allowed_blocks
    assert "warehouse_operations" not in finance.allowed_blocks

    warehouse = bitrix_executive_dashboard_auth.resolve_executive_dashboard_access(
        bitrix_user_id="206",
        settings=settings,
    )
    assert warehouse.allowed_blocks == ("warehouse_operations",)
    assert warehouse.allowed_action_domains == ("warehouse_operations",)
    assert warehouse.money_blocks == ()

    infrastructure = bitrix_executive_dashboard_auth.resolve_executive_dashboard_access(
        bitrix_user_id="207",
        settings=settings,
    )
    assert infrastructure.allowed_blocks == ("infrastructure",)
    assert infrastructure.allowed_action_domains == ()
    assert infrastructure.money_blocks == ()

    personal = bitrix_executive_dashboard_auth.resolve_executive_dashboard_access(
        bitrix_user_id="204",
        settings=settings,
    )
    assert personal.allowed_blocks == ("tasks", "daily_focus")
    assert personal.personal_actions_only is True

    combined = bitrix_executive_dashboard_auth.resolve_executive_dashboard_access(
        bitrix_user_id="205",
        settings=settings,
    )
    assert combined.roles == ("procurement", "receivables")
    assert combined.allowed_blocks == (
        "debtors",
        "receivables_control",
        "procurement_import",
    )


def test_access_policy_is_stored_in_bitrix_session_token(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "missing.json", access_rules_json=_role_rules())
    access = bitrix_executive_dashboard_auth.resolve_executive_dashboard_access(
        bitrix_user_id="201",
        settings=settings,
    )

    token, _ = bitrix_executive_dashboard_auth.create_executive_dashboard_session_token(
        domain="crm.master-mobile.ru",
        member_id="member-1",
        user_id="201",
        user_name="Закупщик",
        access=access,
        settings=settings,
        now=1_785_000_000,
    )
    session = bitrix_executive_dashboard_auth.verify_executive_dashboard_session_token(
        token,
        settings=settings,
        now=1_785_000_010,
    )

    assert session.roles == ("procurement",)
    assert session.allowed_blocks == ("procurement_import",)
    assert session.allowed_action_domains == ("procurement_import",)
    assert session.money_blocks == ("procurement_import",)


def test_dashboard_marks_missing_finance_sources_without_zero_truth(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _override_settings(monkeypatch, _settings(tmp_path / "missing.json"))
    db_session.add(_receivable_case(date(2026, 6, 27)))
    db_session.commit()

    result = build_executive_dashboard(
        db_session,
        requested_date=date(2026, 6, 27),
        access_level="full",
    )

    blocks = {block.key: block for block in result.blocks}
    assert blocks["money_today"].source_status == "source_missing"
    assert blocks["creditors_payables"].source_status == "source_missing"
    assert (
        blocks["creditors_payables"].summary["source_anchor"]
        == "1С: деньги, взаиморасчёты и фактическая стоимость товарных партий"
    )
    assert blocks["debtors"].source_status == "ready"
    assert blocks["debtors"].title == "Дебиторка покупателей"
    assert blocks["debtors"].metrics[0].value == Decimal("12500.00")
    assert blocks["debtors"].drilldown_url == "/bitrix/receivables/?date=2026-06-27"
    assert blocks["receivables_control"].source_status == "partial"


def test_management_balance_places_assets_and_liabilities_on_their_sides(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_path = tmp_path / "finance_snapshot.json"
    snapshot_path.write_text(
        json.dumps(
            {
                "as_of": "2026-07-11",
                "money_today": {
                    "as_of": "2026-07-11",
                    "cash_position": {
                        "as_of": "2026-07-11",
                        "source_status": "ready",
                        "freshness_status": "fresh",
                        "total_balance_rub": "500.00",
                    },
                },
                "creditors_payables": {
                    "source_status": "ready",
                    "freshness_status": "fresh",
                    "as_of": "2026-07-11",
                    "total_payable": "70.00",
                    "gross_payable": "220.00",
                    "supplier_payable": "-80.00",
                    "employee_payable": "-50.00",
                    "owner_payable": "200.00",
                    "counterparty_count": 2,
                    "reverse_balance": "150.00",
                    "reverse_balance_label": "Авансы / переплаты (−)",
                    "groups": [
                        {
                            "key": "suppliers",
                            "asset_amount": "100.00",
                            "liability_amount": "20.00",
                            "gross_payable": "20.00",
                            "total_payable": "-80.00",
                            "reverse_balance": "100.00",
                        },
                        {
                            "key": "employees",
                            "asset_amount": "50.00",
                            "liability_amount": "0.00",
                            "gross_payable": "0.00",
                            "total_payable": "-50.00",
                            "reverse_balance": "50.00",
                        },
                        {
                            "key": "other_debtors",
                            "asset_amount": "30.00",
                            "liability_amount": "10.00",
                            "gross_payable": "10.00",
                            "total_payable": "-20.00",
                            "reverse_balance": "30.00",
                        },
                        {
                            "key": "owners",
                            "asset_amount": "0.00",
                            "liability_amount": "200.00",
                            "gross_payable": "200.00",
                            "total_payable": "200.00",
                            "reverse_balance": "0.00",
                        },
                    ],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _override_settings(monkeypatch, _settings(snapshot_path))
    monkeypatch.setattr(
        executive_dashboard,
        "_load_onec_inventory_cost",
        lambda as_of: (
            OneCInventoryCostSnapshot(
                amount=Decimal("1000.00"),
                quantity=Decimal("25.000"),
                as_of=as_of,
                source_row_count=5,
            ),
            "",
        ),
    )
    db_session.add(_receivable_case(date(2026, 7, 11), balance=Decimal("70.00")))
    db_session.commit()

    result = build_executive_dashboard(
        db_session,
        requested_date=date(2026, 7, 11),
        access_level="full",
    )

    block = next(item for item in result.blocks if item.key == "creditors_payables")
    metrics = {metric.key: metric.value for metric in block.metrics}
    assert block.title == "Управленческий баланс"
    assert metrics == {
        "balance_assets_total": Decimal("1650.00"),
        "balance_liabilities_total": Decimal("200.00"),
    }
    assert [row["amount"] for row in block.summary["balance_assets"]] == [
        "500.00",
        "1000.00",
        "80.00",
        "50.00",
        "20.00",
        "0",
    ]
    assert block.summary["balance_assets"][1]["source_status"] == "ready"
    assert (
        block.summary["balance_assets"][1]["note"]
        == "1С УТ 10.3: ПартииТоваровНаСкладах.СтоимостьОстаток"
    )
    assert [row["amount"] for row in block.summary["balance_liabilities"]] == [
        "0",
        "0",
        "0",
        "200.00",
    ]
    assert block.summary["balance_assets"][2]["label"] == "Дебиторка поставщиков"
    assert block.summary["balance_assets"][4]["label"] == "Прочие дебиторы"
    assert block.summary["balance_liabilities"][3]["label"] == "Задолженность собственникам"


def test_management_balance_does_not_activate_unreleased_owner_cash_formula(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_path = tmp_path / "finance_snapshot.json"
    snapshot_path.write_text(
        json.dumps(
            {
                "money_today": {
                    "as_of": "2026-07-12",
                    "cash_position": {
                        "as_of": "2026-07-12",
                        "source_status": "ready",
                        "total_balance_rub": "100.00",
                    },
                },
                "creditors_payables": {
                    "as_of": "2026-07-12",
                    "source_status": "ready",
                    "groups": [],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (tmp_path / "owner_cash_transit_snapshot.json").write_text(
        json.dumps(
            {
                "as_of": "2026-07-12",
                "source_status": "partial",
                "control_status": "completed",
                "summary": {
                    "money_in_transit_asset": "200000.00",
                    "unclassified_owner_funds_liability": "0.00",
                    "unresolved_related_party_asset": "87880.00",
                    "unresolved_related_party_liability": "0.00",
                    "dividends_ytd": "13415228.19",
                    "dividends_current_month": "100000.00",
                    "dividend_comment_warning_count": 1,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _override_settings(monkeypatch, _settings(snapshot_path))

    result = build_executive_dashboard(
        db_session,
        requested_date=date(2026, 7, 12),
        access_level="full",
    )

    block = next(item for item in result.blocks if item.key == "creditors_payables")
    assets = {row["key"]: row for row in block.summary["balance_assets"]}
    liabilities = {row["key"]: row for row in block.summary["balance_liabilities"]}
    equity = {row["key"]: row for row in block.summary["balance_equity"]}
    assert "owner_cash_in_transit" not in assets
    assert "owner_related_party_unresolved" not in assets
    assert "owner_funds_unclassified" not in liabilities
    assert "dividends_paid_ytd" not in equity


def test_dashboard_accepts_yesterday_within_configured_lag(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _override_settings(monkeypatch, _settings(tmp_path / "missing.json"))
    db_session.add_all(
        [
            _receivable_case(date(2026, 6, 27)),
            _folder_cache(date(2026, 6, 27)),
        ]
    )
    db_session.commit()

    result = build_executive_dashboard(
        db_session,
        requested_date=date(2026, 6, 28),
        access_level="full",
    )

    blocks = {block.key: block for block in result.blocks}
    assert blocks["debtors"].source_status == "ready"
    assert blocks["debtors"].freshness_status == "fresh"
    assert blocks["receivables_control"].source_status == "ready"
    assert blocks["receivables_control"].freshness_status == "fresh"


def test_executive_dashboard_page_serves_release_local_ui(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index_path = tmp_path / "index.html"
    index_path.write_text(
        '<!doctype html><html><head><script src="./assets/app.js"></script></head><body></body></html>',
        encoding="utf-8",
    )
    monkeypatch.setattr(executive_dashboard_page, "_INDEX_PATHS", (index_path,))

    response = client.get("/bitrix/executive-dashboard/")

    assert response.status_code == 200
    assert 'src="/assets/app.js"' in response.text


def test_executive_dashboard_page_returns_503_without_release_ui(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        executive_dashboard_page,
        "_INDEX_PATHS",
        (tmp_path / "missing-index.html",),
    )

    response = client.get("/bitrix/executive-dashboard/")

    assert response.status_code == 503
    assert response.json()["detail"] == "Executive dashboard UI is not built"


def test_shared_snapshot_path_is_resolved_from_workspace_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_path = tmp_path / "contracts" / "snapshot.json"
    snapshot_path.parent.mkdir(parents=True)
    snapshot_path.write_text("{}", encoding="utf-8")
    resolved = _resolve_shared_path(str(snapshot_path))

    assert resolved == snapshot_path


def test_profit_loss_open_question_uses_explicit_inflow_amount(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache_path = tmp_path / "cashflow_period_cache.json"
    cache_path.write_text(
        json.dumps(
            {
                "version": 1,
                "generated_at": "2026-02-28T12:00:00+00:00",
                "source_status": "ready",
                "freshness_status": "fresh",
                "period": {
                    "date_from": "2026-02-01",
                    "date_to": "2026-02-28",
                    "days": 28,
                },
                "rows": [
                    {
                        "business_date": "2026-02-26",
                        "article_key": "supplier_services",
                        "article_name": "Оплата поставщику (за услуги)",
                        "dds_group": "operating",
                        "dds_subgroup": "suppliers",
                        "direction": "inflow",
                        "inflow_amount": "108005.63",
                        "outflow_amount": "0",
                        "movement_count": 1,
                        "review_count": 0,
                        "profit_loss_class": "open_question",
                        "profit_loss_question_key": "inflow_on_supplier_service_expense_article",
                        "profit_loss_question_reason": "Поступление по расходной статье.",
                        "profit_loss_question_amount": "108005.63",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _override_settings(monkeypatch, _settings(tmp_path / "finance_snapshot.json"))

    result = executive_dashboard._profit_loss_expenses_from_cashflow_cache(
        session=db_session,
        date_from=date(2026, 2, 1),
        date_to=date(2026, 2, 28),
    )

    assert result["totals"]["expense_open_question_amount"] == Decimal("108005.63")
    assert result["open_questions"][0].amount == Decimal("108005.63")


def test_profit_loss_expenses_allow_one_day_cache_lag(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_profit_loss_cashflow_cache(tmp_path / "cashflow_period_cache.json")
    _override_settings(monkeypatch, _settings(tmp_path / "finance_snapshot.json"))

    result = executive_dashboard._profit_loss_expenses_from_cashflow_cache(
        session=db_session,
        date_from=date(2026, 6, 1),
        date_to=date(2026, 7, 1),
    )

    assert result["source_status"] == "partial"
    assert result["freshness_status"] == "fresh"
    assert "допустимого лага" in result["note"]


def test_profit_loss_expenses_reject_two_day_cache_lag(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_profit_loss_cashflow_cache(tmp_path / "cashflow_period_cache.json")
    _override_settings(monkeypatch, _settings(tmp_path / "finance_snapshot.json"))

    result = executive_dashboard._profit_loss_expenses_from_cashflow_cache(
        session=db_session,
        date_from=date(2026, 6, 1),
        date_to=date(2026, 7, 2),
    )

    assert result["source_status"] == "stale"
    assert result["freshness_status"] == "stale"
    assert "выходит за кэш" in result["note"]


def test_profit_loss_period_does_not_become_stale_for_one_day_cache_lag(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_profit_loss_cashflow_cache(tmp_path / "cashflow_period_cache.json")
    _override_settings(monkeypatch, _settings(tmp_path / "finance_snapshot.json"))
    db_session.add(_sales_kpi(date(2026, 6, 30)))
    db_session.commit()

    result = build_executive_profit_loss_period_response(
        db_session,
        date_from=date(2026, 6, 1),
        date_to=date(2026, 7, 1),
    )

    assert result.source_status == "partial"
    assert result.freshness_status == "partial"
    assert result.expense_source_status == "partial"
    assert "допустимого лага" in (result.note or "")


def test_profit_loss_block_reads_sales_kpi(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_path = tmp_path / "missing.json"
    _write_profit_loss_cashflow_cache(tmp_path / "cashflow_period_cache.json")
    _override_settings(monkeypatch, _settings(snapshot_path))
    db_session.add_all(
        [
            _sales_kpi(
                date(2026, 6, 27),
                revenue=Decimal("1000.00"),
                cost_of_sales=Decimal("600.00"),
                sales_count=Decimal("2.000"),
            ),
            _sales_kpi(
                date(2026, 6, 27),
                revenue=Decimal("500.00"),
                cost_of_sales=Decimal("300.00"),
                sales_count=Decimal("1.000"),
                manager_ref="mgr-2",
                manager_name="Менеджер 2",
            ),
        ]
    )
    db_session.commit()

    result = build_executive_dashboard(
        db_session,
        requested_date=date(2026, 6, 27),
        access_level="full",
    )

    block = {item.key: item for item in result.blocks}["profit_loss"]
    metrics = {metric.key: metric.value for metric in block.metrics}
    assert block.title == "Отчет о прибылях и убытках"
    assert block.source_status == "partial"
    assert metrics["revenue"] == Decimal("1500.00")
    assert metrics["cost_of_sales"] == Decimal("900.00")
    assert metrics["gross_profit"] == Decimal("600.00")
    assert metrics["gross_margin_pct"] == Decimal("0.4000")
    assert metrics["operating_expenses"] == Decimal("150.00")
    assert metrics["operating_profit"] == Decimal("450.00")
    assert block.summary["expense_source_status"] == "partial"
    assert block.summary["expense_open_question_count"] == 1
    assert block.summary["missing_expense_line_count"] == 4
    assert "profit_loss" in {source.source_key for source in result.source_freshness}


def test_profit_loss_period_response_aggregates_sales_kpi(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_profit_loss_cashflow_cache(tmp_path / "cashflow_period_cache.json")
    _override_settings(monkeypatch, _settings(tmp_path / "finance_snapshot.json"))
    monkeypatch.setattr(
        executive_dashboard,
        "load_retail_director_monthly_kpi",
        lambda month: {
            "schema_version": 2,
            "month": month,
            "writeoff_amount": "1229121.82",
            "receipt_amount": "526672.97",
            "shrinkage_amount": "702448.85",
            "shrinkage_pct": "0.8499",
            "norm_pct": "0.3000",
            "matched_store_count": 12,
            "stores": [
                {
                    "store_ref": "store-1",
                    "store_name": "Горбушкин Двор",
                    "sales_amount": "1000000.00",
                    "writeoff_amount": "8000.00",
                    "receipt_amount": "1000.00",
                    "shrinkage_amount": "7000.00",
                    "shrinkage_pct": "0.7000",
                    "norm_pct": "0.3000",
                    "variance_to_norm_pct": "0.4000",
                    "above_norm": True,
                    "source_status": "ready",
                    "has_operations": True,
                },
                {
                    "store_ref": "store-2",
                    "store_name": "Склад Сайт",
                    "sales_amount": "250000.00",
                    "writeoff_amount": "100.00",
                    "receipt_amount": "200.00",
                    "shrinkage_amount": "-100.00",
                    "shrinkage_pct": "-0.0400",
                    "norm_pct": "0.3000",
                    "variance_to_norm_pct": "-0.3400",
                    "above_norm": False,
                    "source_status": "ready",
                    "has_operations": True,
                },
            ],
            "top_documents": [
                {
                    "stable_key": "_Document210:doc-1:inventory_writeoff",
                    "operation_kind": "inventory_writeoff",
                    "operation_label": "Инвентаризационное списание",
                    "document_type": "_Document210",
                    "document_ref": "doc-1",
                    "document_number": "СП-1",
                    "document_date": "2026-06-20",
                    "store_ref": "store-1",
                    "store_name": "Горбушкин Двор",
                    "amount": "8000.00",
                    "effect_amount": "8000.00",
                },
                {
                    "stable_key": "_Document170:doc-2:inventory_receipt",
                    "operation_kind": "inventory_receipt",
                    "operation_label": "Оприходование по инвентаризации",
                    "document_type": "_Document170",
                    "document_ref": "doc-2",
                    "document_number": "ОП-1",
                    "document_date": "2026-06-21",
                    "store_ref": "store-1",
                    "store_name": "Горбушкин Двор",
                    "amount": "1000.00",
                    "effect_amount": "-1000.00",
                },
            ],
            "data_quality": {
                "source_status": "partial",
                "approved_store_count": 13,
                "source_store_count": 13,
                "matched_store_count": 12,
                "unmatched_store_count": 1,
                "source_document_count": 22,
                "matched_document_count": 21,
                "unmatched_document_count": 1,
                "unmatched_writeoff_amount": "500.00",
                "unmatched_receipt_amount": "0.00",
                "excluded_store_count": 2,
                "excluded_document_count": 3,
                "excluded_writeoff_amount": "100.00",
                "excluded_receipt_amount": "25.00",
                "store_scope_status": "approved",
                "store_scope_source": "approved_freeze",
                "store_scope_month": "2026-06",
                "norm_source_status": "approved",
                "norm_source": "bitrix_kpi_v2_export",
            },
            "owner": {
                "employee_key": "emp-1",
                "employee_bitrix_id": "42",
                "employee_name": "Руководитель сети",
                "role_code": "retail_director",
            },
            "warnings": [],
        },
    )
    monkeypatch.setattr(
        executive_dashboard,
        "load_retail_director_monthly_kpi_history",
        lambda _: {
            "previous_month": {
                "month": "2026-05",
                "writeoff_amount": "1000.00",
                "receipt_amount": "200.00",
                "shrinkage_amount": "800.00",
                "shrinkage_pct": "0.4000",
            },
            "history": [
                {"month": "2026-05", "shrinkage_amount": "800.00", "shrinkage_pct": "0.4000"},
                {"month": "2026-03", "shrinkage_amount": "600.00", "shrinkage_pct": "0.3000"},
                {"month": "2026-02", "shrinkage_amount": "400.00", "shrinkage_pct": "0.2000"},
            ],
            "source_status": "ready",
        },
    )
    db_session.add_all(
        [
            _sales_kpi(
                date(2026, 6, 26),
                revenue=Decimal("1000.00"),
                cost_of_sales=Decimal("700.00"),
                sales_count=Decimal("2.000"),
                store_ref="store-1",
                store_name="Горбушкин Двор",
            ),
            _sales_kpi(
                date(2026, 6, 27),
                revenue=Decimal("250.00"),
                cost_of_sales=Decimal("50.00"),
                sales_count=Decimal("1.000"),
                store_ref="store-2",
                store_name="Склад Сайт",
            ),
        ]
    )
    db_session.commit()

    result = build_executive_profit_loss_period_response(
        db_session,
        date_from=date(2026, 6, 26),
        date_to=date(2026, 6, 27),
    )

    line_by_key = {line.key: line for line in result.lines}
    assert result.source_status == "partial"
    assert result.totals["revenue"] == Decimal("1250.00")
    assert result.totals["cost_of_sales"] == Decimal("750.00")
    assert result.totals["gross_profit"] == Decimal("500.00")
    assert result.totals["operating_expenses"] == Decimal("150.00")
    assert result.totals["operating_profit"] == Decimal("350.00")
    assert result.totals["expense_open_question_count"] == 1
    assert result.totals["gross_margin_pct"] == Decimal("0.4000")
    assert line_by_key["cost_of_sales"].amount == Decimal("-750.00")
    assert line_by_key["operating_expenses"].amount == Decimal("-150.00")
    assert line_by_key["operating_profit"].amount == Decimal("350.00")
    assert line_by_key["operating_profit"].source_status == "partial"
    assert line_by_key["net_profit"].source_status == "partial"
    assert line_by_key["net_profit"].amount == Decimal("350.00")
    assert result.expense_source_status == "partial"
    assert {row.key for row in result.expense_breakdown} == {"rent", "bank_fees"}
    assert result.expense_open_questions[0].amount == Decimal("400.00")
    assert result.inventory_loss is not None
    assert result.inventory_loss.source_status == "ready"
    assert result.inventory_loss.writeoff_amount == Decimal("1229121.82")
    assert result.inventory_loss.receipt_amount == Decimal("526672.97")
    assert result.inventory_loss.loss_amount == Decimal("702448.85")
    assert result.inventory_loss.norm_pct == Decimal("0.3000")
    assert result.inventory_loss.variance_to_norm_pct == Decimal("0.5499")
    assert result.inventory_loss.previous_month is not None
    assert result.inventory_loss.previous_month.loss_amount == Decimal("800.00")
    assert result.inventory_loss.average_loss_amount_3m == Decimal("600.00")
    assert result.inventory_loss.average_loss_pct_3m == Decimal("0.3000")
    assert len(result.inventory_loss.history) == 4
    assert len(result.inventory_loss.stores) == 2
    assert len(result.inventory_loss.top_documents) == 2
    assert [action.action_type for action in result.inventory_loss.actions] == [
        "store_above_norm",
        "unmatched_documents",
    ]
    assert result.inventory_loss.actions[0].responsible_name == "Руководитель сети"
    assert result.inventory_loss.data_quality.excluded_document_count == 3
    assert result.inventory_loss.data_quality.store_scope_status == "approved"
    assert result.inventory_loss.data_quality.norm_source_status == "approved"
    assert result.daily[-1].business_date == date(2026, 6, 27)
    assert {row.label for row in result.by_store} == {"Горбушкин Двор", "Склад Сайт"}


def test_profit_loss_subtracts_inventory_loss_and_ready_bp_taxes(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path / "finance_snapshot.json")
    settings.executive_dashboard_bp_tax_accrual_root = str(tmp_path / "bp-tax-accruals")
    _override_settings(monkeypatch, settings)
    cashflow_path = tmp_path / "cashflow_period_cache.json"
    _write_profit_loss_cashflow_cache(cashflow_path)
    cashflow_payload = json.loads(cashflow_path.read_text(encoding="utf-8"))
    cashflow_payload["rows"].append(
        {
            "business_date": "2026-06-27",
            "article_key": "customer_refunds",
            "article_name": "Возврат денежных средств покупателю",
            "dds_group": "operating",
            "dds_subgroup": "customer_refunds",
            "direction": "outflow",
            "is_internal_transfer": False,
            "inflow_amount": "0",
            "outflow_amount": "100.00",
            "net_amount": "-100.00",
            "movement_count": 1,
            "review_count": 0,
            "profit_loss_class": "contra_revenue",
            "profit_loss_recognition_method": "cashflow_fallback",
            "profit_loss_source_status": "ready",
        }
    )
    cashflow_payload["rows"].extend(
        [
            {
                "business_date": "2026-06-27",
                "article_key": "supplier_services",
                "article_name": "Оплата поставщику (за услуги)",
                "dds_group": "operating",
                "dds_subgroup": "suppliers",
                "direction": "outflow",
                "is_internal_transfer": False,
                "inflow_amount": "0",
                "outflow_amount": "109.00",
                "movement_count": 1,
                "review_count": 0,
                "profit_loss_class": "operating_expense",
                "profit_loss_line_key": "supplier_services",
                "profit_loss_line_label": "Услуги поставщиков",
            },
            {
                "business_date": "2026-06-27",
                "article_key": "supplier_services",
                "article_name": "Оплата поставщику (за услуги)",
                "dds_group": "operating",
                "dds_subgroup": "suppliers",
                "direction": "inflow",
                "is_internal_transfer": False,
                "inflow_amount": "108.00",
                "outflow_amount": "0",
                "movement_count": 1,
                "review_count": 0,
                "profit_loss_class": "operating_expense_refund",
                "profit_loss_line_key": "supplier_services",
                "profit_loss_line_label": "Услуги поставщиков",
            },
        ]
    )
    cashflow_path.write_text(
        json.dumps(cashflow_payload, ensure_ascii=False),
        encoding="utf-8",
    )
    tax_path = tmp_path / "bp-tax-accruals" / "2026-06" / "bp-tax-accruals-2026-06.json"
    tax_path.parent.mkdir(parents=True, exist_ok=True)
    tax_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "month": "2026-06",
                "source_status": "ready",
                "lines": {
                    "tax_expense_accrued": {
                        "amount": "40.00",
                        "source_status": "ready",
                    }
                },
                "breakdown": [
                    {
                        "category": "insurance_contributions",
                        "debit_account": "44.01",
                        "debit_account_label": "Издержки",
                        "credit_account": "69.09",
                        "credit_account_label": "Взносы",
                        "posting_count": 1,
                        "amount": "15.00",
                    },
                    {
                        "category": "simplified_tax",
                        "debit_account": "99.01.1",
                        "debit_account_label": "Финрезультат",
                        "credit_account": "68.12",
                        "credit_account_label": "УСН",
                        "posting_count": 2,
                        "amount": "25.00",
                    },
                ],
                "control": {"tax_expense_posting_count": 3},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        executive_dashboard,
        "load_retail_director_monthly_kpi",
        lambda _: {
            "schema_version": 2,
            "month": "2026-06",
            "source_status": "ready",
            "writeoff_amount": "80.00",
            "receipt_amount": "30.00",
            "shrinkage_amount": "50.00",
            "data_quality": {"source_status": "ready"},
        },
    )
    monkeypatch.setattr(
        executive_dashboard,
        "load_retail_director_monthly_kpi_history",
        lambda _: {"history": [], "source_status": "ready"},
    )
    db_session.add(
        _sales_kpi(
            date(2026, 6, 30),
            revenue=Decimal("1000.00"),
            cost_of_sales=Decimal("600.00"),
        )
    )
    db_session.commit()

    result = build_executive_profit_loss_period_response(
        db_session,
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 30),
    )

    lines = {line.key: line for line in result.lines}
    assert result.totals["inventory_loss_expense"] == Decimal("50.00")
    assert result.totals["operating_tax_expense_accrued"] == Decimal("15.00")
    assert result.totals["operating_expenses"] == Decimal("151.00")
    assert result.totals["operating_expenses_total"] == Decimal("166.00")
    assert result.totals["gross_revenue"] == Decimal("1000.00")
    assert result.totals["customer_refunds"] == Decimal("100.00")
    assert result.totals["revenue"] == Decimal("900.00")
    assert result.totals["gross_profit"] == Decimal("300.00")
    assert result.totals["operating_profit"] == Decimal("84.00")
    assert result.totals["tax_expense_accrued"] == Decimal("25.00")
    assert result.totals["total_tax_expense_accrued"] == Decimal("40.00")
    assert result.totals["net_profit"] == Decimal("59.00")
    assert lines["gross_revenue"].amount == Decimal("1000.00")
    assert lines["customer_refunds"].amount == Decimal("-100.00")
    assert lines["revenue"].amount == Decimal("900.00")
    assert lines["inventory_loss"].amount == Decimal("-50.00")
    assert lines["operating_taxes"].amount == Decimal("-15.00")
    assert lines["taxes"].amount == Decimal("-25.00")
    assert lines["net_profit"].amount == Decimal("59.00")
    assert {ratio.key for ratio in result.ratios} >= {"net_profit_margin_pct"}
    assert len(result.monthly) == 1
    assert result.monthly[0].month == "2026-06"
    assert result.monthly[0].operating_profit == Decimal("84.00")
    assert result.monthly[0].net_profit == Decimal("59.00")
    assert result.monthly[0].net_profit_margin_pct == Decimal("0.0656")


def test_profit_loss_sums_inventory_losses_for_all_full_months(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    losses = {
        "2026-01": Decimal("10.00"),
        "2026-02": Decimal("20.00"),
        "2026-03": Decimal("30.00"),
        "2026-04": Decimal("40.00"),
        "2026-05": Decimal("50.00"),
        "2026-06": Decimal("60.00"),
    }

    monkeypatch.setattr(
        executive_dashboard,
        "_profit_loss_inventory_loss",
        lambda period_end: ExecutiveProfitLossInventoryLoss(
            month=period_end.strftime("%Y-%m"),
            source_status="ready",
            loss_amount=losses[period_end.strftime("%Y-%m")],
        ),
    )

    result = executive_dashboard._profit_loss_inventory_adjustment(
        ExecutiveProfitLossInventoryLoss(
            month="2026-06",
            source_status="ready",
            loss_amount=losses["2026-06"],
        ),
        date_from=date(2026, 1, 1),
        date_to=date(2026, 6, 30),
    )

    assert result["amount"] == Decimal("210.00")
    assert result["source_status"] == "ready"
    assert "6 полных месяцев" in result["note"]


def test_profit_loss_keeps_available_inventory_losses_when_month_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def load_loss(period_end: date) -> ExecutiveProfitLossInventoryLoss:
        month = period_end.strftime("%Y-%m")
        return ExecutiveProfitLossInventoryLoss(
            month=month,
            source_status="source_missing" if month == "2026-02" else "ready",
            loss_amount=None if month == "2026-02" else Decimal("10.00"),
        )

    monkeypatch.setattr(executive_dashboard, "_profit_loss_inventory_loss", load_loss)

    result = executive_dashboard._profit_loss_inventory_adjustment(
        ExecutiveProfitLossInventoryLoss(
            month="2026-03",
            source_status="ready",
            loss_amount=Decimal("10.00"),
        ),
        date_from=date(2026, 1, 1),
        date_to=date(2026, 3, 31),
    )

    assert result["amount"] == Decimal("20.00")
    assert result["source_status"] == "partial"
    assert "2026-02" in result["note"]


def test_profit_loss_period_marks_missing_inventory_loss_report(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_profit_loss_cashflow_cache(tmp_path / "cashflow_period_cache.json")
    _override_settings(monkeypatch, _settings(tmp_path / "finance_snapshot.json"))
    monkeypatch.setattr(executive_dashboard, "load_retail_director_monthly_kpi", lambda _: None)
    db_session.add(_sales_kpi(date(2026, 6, 27)))
    db_session.commit()

    result = build_executive_profit_loss_period_response(
        db_session,
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 27),
    )

    assert result.inventory_loss is not None
    assert result.inventory_loss.month == "2026-06"
    assert result.inventory_loss.source_status == "source_missing"
    assert result.inventory_loss.loss_amount is None


def test_profit_loss_period_keeps_v1_inventory_totals_without_false_detail(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_profit_loss_cashflow_cache(tmp_path / "cashflow_period_cache.json")
    _override_settings(monkeypatch, _settings(tmp_path / "finance_snapshot.json"))
    monkeypatch.setattr(
        executive_dashboard,
        "load_retail_director_monthly_kpi",
        lambda month: {
            "schema_version": 1,
            "month": month,
            "writeoff_amount": "1000.00",
            "receipt_amount": "250.00",
            "shrinkage_amount": "750.00",
            "shrinkage_pct": "0.5000",
            "warnings": [],
        },
    )
    monkeypatch.setattr(
        executive_dashboard,
        "load_retail_director_monthly_kpi_history",
        lambda _: {"previous_month": None, "history": [], "source_status": "source_missing"},
    )
    db_session.add(_sales_kpi(date(2026, 6, 27)))
    db_session.commit()

    result = build_executive_profit_loss_period_response(
        db_session,
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 27),
    )

    assert result.inventory_loss is not None
    assert result.inventory_loss.source_status == "ready"
    assert result.inventory_loss.detail_source_status == "source_missing"
    assert result.inventory_loss.loss_amount == Decimal("750.00")
    assert result.inventory_loss.stores == []
    assert result.inventory_loss.top_documents == []
    assert "Источник v1" in str(result.inventory_loss.note)


def test_inventory_loss_keeps_totals_when_detail_and_part_of_history_are_malformed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        executive_dashboard,
        "load_retail_director_monthly_kpi",
        lambda month: {
            "schema_version": 2,
            "month": month,
            "writeoff_amount": "1000.00",
            "receipt_amount": "250.00",
            "shrinkage_amount": "750.00",
            "shrinkage_pct": "0.5000",
            "norm_pct": "0.3000",
            "stores": [],
            "top_documents": [],
            "data_quality": {"approved_store_count": "bad"},
            "warnings": [],
        },
    )
    monkeypatch.setattr(
        executive_dashboard,
        "load_retail_director_monthly_kpi_history",
        lambda _: {
            "previous_month": {
                "month": "2026-05",
                "shrinkage_amount": "not-a-number",
            },
            "history": [
                {"month": "2026-04", "shrinkage_amount": "600.00"},
                {"month": "2026-03", "shrinkage_amount": "not-a-number"},
            ],
            "source_status": "ready",
        },
    )

    result = executive_dashboard._profit_loss_inventory_loss(date(2026, 6, 30))

    assert result.source_status == "ready"
    assert result.loss_amount == Decimal("750.00")
    assert result.detail_source_status == "source_error"
    assert result.data_quality.source_status == "source_error"
    assert result.previous_month is None
    assert result.average_loss_amount_3m == Decimal("600.00")
    assert result.history_source_status == "partial"
    assert [item.month for item in result.history] == ["2026-04", "2026-06"]
    assert "Историю товарных потерь не удалось прочитать." in result.warnings


def test_inventory_loss_returns_source_error_for_malformed_network_totals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        executive_dashboard,
        "load_retail_director_monthly_kpi",
        lambda month: {
            "schema_version": 2,
            "month": month,
            "writeoff_amount": "not-a-number",
            "receipt_amount": "250.00",
            "shrinkage_amount": "750.00",
        },
    )
    monkeypatch.setattr(
        executive_dashboard,
        "load_retail_director_monthly_kpi_history",
        lambda _: {"previous_month": None, "history": [], "source_status": "source_missing"},
    )

    result = executive_dashboard._profit_loss_inventory_loss(date(2026, 6, 30))

    assert result.source_status == "source_error"
    assert result.detail_source_status == "source_error"
    assert result.loss_amount is None


@pytest.mark.parametrize(
    "invalid_amount",
    [float("nan"), float("inf"), float("-inf"), "1e999999"],
)
def test_inventory_loss_returns_source_error_for_non_finite_network_totals(
    monkeypatch: pytest.MonkeyPatch,
    invalid_amount: object,
) -> None:
    monkeypatch.setattr(
        executive_dashboard,
        "load_retail_director_monthly_kpi",
        lambda month: {
            "schema_version": 2,
            "month": month,
            "writeoff_amount": invalid_amount,
            "receipt_amount": "0.00",
            "shrinkage_amount": invalid_amount,
        },
    )
    monkeypatch.setattr(
        executive_dashboard,
        "load_retail_director_monthly_kpi_history",
        lambda _: {"previous_month": None, "history": [], "source_status": "source_missing"},
    )

    result = executive_dashboard._profit_loss_inventory_loss(date(2026, 6, 30))

    assert result.source_status == "source_error"
    assert result.detail_source_status == "source_error"
    assert result.loss_amount is None


def test_inventory_loss_skips_unquantizable_history_without_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        executive_dashboard,
        "load_retail_director_monthly_kpi",
        lambda month: {
            "schema_version": 2,
            "month": month,
            "writeoff_amount": "100.00",
            "receipt_amount": "0.00",
            "shrinkage_amount": "100.00",
            "stores": [],
            "top_documents": [],
            "data_quality": {},
            "warnings": [],
        },
    )
    monkeypatch.setattr(
        executive_dashboard,
        "load_retail_director_monthly_kpi_history",
        lambda _: {
            "previous_month": {
                "month": "2026-05",
                "shrinkage_amount": "1e999999",
            },
            "history": [{"month": "2026-05", "shrinkage_amount": "1e999999"}],
            "source_status": "ready",
        },
    )

    result = executive_dashboard._profit_loss_inventory_loss(date(2026, 6, 30))

    assert result.source_status == "ready"
    assert result.previous_month is None
    assert result.average_loss_amount_3m is None
    assert result.history_source_status == "source_error"
    assert [item.month for item in result.history] == ["2026-06"]
    assert "Историю товарных потерь не удалось прочитать." in result.warnings


def test_inventory_loss_does_not_raise_above_norm_action_for_draft_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        executive_dashboard,
        "load_retail_director_monthly_kpi",
        lambda month: {
            "schema_version": 2,
            "month": month,
            "writeoff_amount": "1000.00",
            "receipt_amount": "0.00",
            "shrinkage_amount": "1000.00",
            "shrinkage_pct": "1.0000",
            "norm_pct": "0.3000",
            "stores": [
                {
                    "store_ref": "store-1",
                    "store_name": "Точка 1",
                    "sales_amount": "100000.00",
                    "writeoff_amount": "1000.00",
                    "receipt_amount": "0.00",
                    "shrinkage_amount": "1000.00",
                    "shrinkage_pct": "1.0000",
                    "norm_pct": "0.3000",
                    "has_operations": True,
                }
            ],
            "top_documents": [],
            "data_quality": {
                "source_status": "ready",
                "store_scope_status": "draft",
                "norm_source_status": "fallback",
            },
            "warnings": [],
        },
    )
    monkeypatch.setattr(
        executive_dashboard,
        "load_retail_director_monthly_kpi_history",
        lambda _: {"previous_month": None, "history": [], "source_status": "source_missing"},
    )

    result = executive_dashboard._profit_loss_inventory_loss(date(2026, 6, 30))

    assert result.stores[0].above_norm is True
    assert result.actions == []
    assert result.data_quality.store_scope_status == "draft"
    assert result.data_quality.norm_source_status == "fallback"


def test_inventory_actions_require_confirmed_provenance() -> None:
    store = ExecutiveProfitLossInventoryStore(
        store_ref="store-1",
        store_name="Точка 1",
        sales_amount=Decimal("100000.00"),
        writeoff_amount=Decimal("1000.00"),
        receipt_amount=Decimal("0.00"),
        loss_amount=Decimal("1000.00"),
        loss_pct=Decimal("1.0000"),
        norm_pct=Decimal("0.3000"),
        variance_to_norm_pct=Decimal("0.7000"),
        above_norm=True,
        has_operations=True,
    )

    actions = executive_dashboard._inventory_actions(
        [store],
        data_quality=ExecutiveProfitLossInventoryDataQuality(
            source_status="ready",
            store_scope_status="unknown",
            norm_source_status="unknown",
        ),
        owner=None,
    )

    assert actions == []


def test_inventory_actions_signal_zero_sales_without_operations() -> None:
    store = ExecutiveProfitLossInventoryStore(
        store_ref="store-1",
        store_name="Точка 1",
        sales_amount=Decimal("0.00"),
        writeoff_amount=Decimal("0.00"),
        receipt_amount=Decimal("0.00"),
        loss_amount=Decimal("0.00"),
        norm_pct=Decimal("0.3000"),
        has_operations=False,
    )

    actions = executive_dashboard._inventory_actions(
        [store],
        data_quality=ExecutiveProfitLossInventoryDataQuality(
            source_status="ready",
            store_scope_status="approved",
            norm_source_status="approved",
        ),
        owner=None,
    )

    assert [item.action_type for item in actions] == ["store_missing_sales"]
    assert "утверждённый контур" in actions[0].description


def test_inventory_actions_order_missing_sales_by_loss_and_label_draft_scope() -> None:
    stores = [
        ExecutiveProfitLossInventoryStore(
            store_ref="store-low",
            store_name="А",
            sales_amount=Decimal("0.00"),
            loss_amount=Decimal("10.00"),
            has_operations=False,
        ),
        ExecutiveProfitLossInventoryStore(
            store_ref="store-high",
            store_name="Б",
            sales_amount=Decimal("0.00"),
            loss_amount=Decimal("100.00"),
            has_operations=False,
        ),
    ]

    actions = executive_dashboard._inventory_actions(
        stores,
        data_quality=ExecutiveProfitLossInventoryDataQuality(
            source_status="ready",
            store_scope_status="draft",
            norm_source_status="fallback",
        ),
        owner=None,
    )

    assert [item.store_ref for item in actions] == ["store-high", "store-low"]
    assert all("утверждён" not in item.title.lower() for item in actions)
    assert all("черновой контур" in item.description for item in actions)


def test_inventory_actions_use_neutral_wording_for_unknown_scope() -> None:
    store = ExecutiveProfitLossInventoryStore(
        store_ref="store-1",
        store_name="Точка 1",
        sales_amount=None,
        loss_amount=Decimal("0.00"),
        has_operations=False,
    )

    actions = executive_dashboard._inventory_actions(
        [store],
        data_quality=ExecutiveProfitLossInventoryDataQuality(
            source_status="partial",
            store_scope_status="unknown",
            norm_source_status="unknown",
        ),
        owner=None,
    )

    assert len(actions) == 1
    assert "утверждён" not in actions[0].title.lower()
    assert "утверждение" in actions[0].description


def test_sales_period_response_calculates_forecast_comparison_and_filters(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path / "missing.json")
    _write_sales_plan_snapshot(Path(settings.executive_dashboard_sales_plan_snapshot_path))
    _override_settings(monkeypatch, settings)
    rows = []
    cursor = date(2026, 5, 1)
    while cursor <= date(2026, 6, 5):
        rows.append(
            _sales_kpi(
                cursor,
                revenue=Decimal("100.00"),
                cost_of_sales=Decimal("60.00"),
                store_ref="store-1",
                store_name="Горбушкин Двор",
            )
        )
        rows.append(
            _sales_kpi(
                cursor,
                revenue=Decimal("200.00"),
                cost_of_sales=Decimal("100.00"),
                manager_ref="mgr-2",
                manager_name="Менеджер 2",
                store_ref="store-2",
                store_name="Склад Сайт",
            )
        )
        cursor += timedelta(days=1)
    db_session.add_all(rows)
    db_session.commit()

    result = build_executive_sales_period_response(
        db_session,
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 30),
        today=date(2026, 6, 5),
    )

    assert result.source_status == "ready"
    assert result.forecast_status == "ready"
    assert result.totals["revenue"] == Decimal("1500.00")
    assert result.totals["forecast_revenue_period_end"] == Decimal("9000.00")
    assert result.comparison["revenue"] == Decimal("1500.00")
    assert result.daily[5].business_date == date(2026, 6, 6)
    assert result.daily[5].actual_revenue is None
    assert result.daily[5].forecast_revenue == Decimal("300.00")
    assert len(result.monthly) == 12
    assert result.monthly[-1].month == "2026-06"
    assert result.monthly[-1].revenue == Decimal("1500.00")
    assert result.monthly[-1].sales_count == Decimal("20.000")
    assert result.monthly[-1].forecast_revenue == Decimal("9000.00")
    assert result.monthly[-1].gross_margin_pct is not None
    assert result.monthly[-1].gross_margin_pct == result.totals["gross_margin_pct"]
    assert result.monthly[-1].comparison_sales_count == Decimal("0")
    assert {item.label for item in result.stores} == {"Горбушкин Двор", "Склад Сайт"}
    assert {item.label for item in result.managers} == {"Менеджер 1", "Менеджер 2"}
    assert result.plan_status == "ready"
    assert result.plan is not None
    assert result.plan.approved_revenue == Decimal("9000.00")
    assert result.plan.approved_margin_pct == Decimal("0.45")
    assert result.plan.comparison_basis == "forecast"
    assert result.plan.comparison_revenue == Decimal("9000.00")
    assert result.plan.plan_attainment_pct == Decimal("1")
    diagnostics = {item.key: item for item in result.diagnostic_kpis}
    assert len(diagnostics) == 6
    assert diagnostics["lost_gross_profit_margin_gap"].value == Decimal("0")
    assert diagnostics["gross_profit_per_unit"].value == Decimal("35.00")
    assert diagnostics["cost_per_unit"].value == Decimal("40.00")
    assert diagnostics["margin_gap_pp"].value == Decimal("1.6700")
    assert diagnostics["stores_below_plan_count"].value == 0
    assert diagnostics["stores_below_plan_count"].meta["problem"] == []
    assert diagnostics["managers_below_target_margin_count"].value == 0
    assert diagnostics["managers_below_target_margin_count"].meta["problem"] == []
    store_meta = {item.key: item.meta for item in result.by_store}
    assert store_meta["store-1"]["approved_revenue"] == Decimal("3000.00")
    assert store_meta["store-2"]["plan_attainment_pct"] == Decimal("1")

    filtered = build_executive_sales_period_response(
        db_session,
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 30),
        store_ref="store-2",
        today=date(2026, 6, 5),
    )
    assert filtered.totals["revenue"] == Decimal("1000.00")
    assert filtered.totals["forecast_revenue_period_end"] == Decimal("6000.00")
    assert filtered.monthly[-1].sales_count == Decimal("10.000")
    assert [row.label for row in filtered.by_store] == ["Склад Сайт"]
    assert filtered.plan is not None
    assert filtered.plan.scope_type == "store"
    assert filtered.plan.approved_revenue == Decimal("6000.00")
    assert filtered.plan.plan_attainment_pct == Decimal("1")


def test_sales_period_manager_uses_weighted_store_margin_without_revenue_plan(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path / "missing.json")
    _write_sales_plan_snapshot(Path(settings.executive_dashboard_sales_plan_snapshot_path))
    _override_settings(monkeypatch, settings)
    db_session.add_all(
        [
            _sales_kpi(
                date(2026, 6, 30),
                revenue=Decimal("100.00"),
                cost_of_sales=Decimal("60.00"),
                manager_ref="mgr-1",
                store_ref="store-1",
            ),
            _sales_kpi(
                date(2026, 6, 30),
                revenue=Decimal("300.00"),
                cost_of_sales=Decimal("180.00"),
                manager_ref="mgr-1",
                store_ref="store-2",
            ),
        ]
    )
    db_session.commit()

    result = build_executive_sales_period_response(
        db_session,
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 30),
        manager_ref="mgr-1",
        today=date(2026, 7, 1),
    )

    assert result.plan_status == "ready"
    assert result.plan is not None
    assert result.plan.scope_type == "manager"
    assert result.plan.approved_revenue is None
    assert result.plan.plan_attainment_pct is None
    assert result.plan.comparison_basis == "manager_margin_only"
    assert result.plan.approved_margin_pct == Decimal("0.4625")
    diagnostics = {item.key: item for item in result.diagnostic_kpis}
    assert diagnostics["stores_below_plan_count"].source_status == "not_applicable"
    assert diagnostics["managers_below_target_margin_count"].value == 1
    assert diagnostics["managers_below_target_margin_count"].meta["problem"] == [
        {"key": "mgr-1", "label": "Менеджер 1"}
    ]


def test_sales_period_lists_problem_stores_and_managers_in_diagnostic_meta(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path / "missing.json")
    _write_sales_plan_snapshot(Path(settings.executive_dashboard_sales_plan_snapshot_path))
    _override_settings(monkeypatch, settings)
    rows = []
    cursor = date(2026, 6, 1)
    while cursor <= date(2026, 6, 20):
        rows.append(
            _sales_kpi(
                cursor,
                revenue=Decimal("100.00"),
                cost_of_sales=Decimal("80.00"),
                store_ref="store-1",
                store_name="Горбушкин Двор",
            )
        )
        cursor += timedelta(days=1)
    db_session.add_all(rows)
    db_session.commit()

    result = build_executive_sales_period_response(
        db_session,
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 30),
        today=date(2026, 7, 5),
    )

    diagnostics = {item.key: item for item in result.diagnostic_kpis}
    stores_metric = diagnostics["stores_below_plan_count"]
    assert stores_metric.source_status == "ready"
    assert stores_metric.value == 2
    assert stores_metric.meta["problem"] == [
        {"key": "store-1", "label": "Горбушкин Двор"},
        {"key": "store-2", "label": "Склад Сайт"},
    ]
    managers_metric = diagnostics["managers_below_target_margin_count"]
    assert managers_metric.source_status == "ready"
    assert managers_metric.value == 1
    assert managers_metric.meta["problem"] == [{"key": "mgr-1", "label": "Менеджер 1"}]


def test_sales_period_requires_complete_store_plan_coverage(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path / "missing.json")
    _write_sales_plan_snapshot(
        Path(settings.executive_dashboard_sales_plan_snapshot_path),
        stores=[
            {
                "scope_key": "store-1",
                "scope_name": "Горбушкин Двор",
                "approved_revenue": "3000.00",
                "approved_margin_pct": "35.00",
                "approved_gross_profit": "1050.00",
            }
        ],
    )
    _override_settings(monkeypatch, settings)
    db_session.add_all(
        [
            _sales_kpi(date(2026, 6, 30), store_ref="store-1"),
            _sales_kpi(date(2026, 6, 30), store_ref="store-2", manager_ref="mgr-2"),
        ]
    )
    db_session.commit()

    result = build_executive_sales_period_response(
        db_session,
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 30),
        today=date(2026, 7, 1),
    )

    diagnostics = {item.key: item for item in result.diagnostic_kpis}
    assert result.source_status == "ready"
    assert diagnostics["lost_gross_profit_margin_gap"].source_status == "partial"
    assert diagnostics["lost_gross_profit_margin_gap"].value is None
    assert diagnostics["margin_gap_pp"].value is None
    assert diagnostics["stores_below_plan_count"].source_status == "partial"
    assert diagnostics["stores_below_plan_count"].value is None
    assert diagnostics["stores_below_plan_count"].meta["problem"] == []
    assert diagnostics["managers_below_target_margin_count"].source_status == "partial"
    assert diagnostics["managers_below_target_margin_count"].value is None
    assert diagnostics["managers_below_target_margin_count"].meta["problem"] == []


def test_sales_period_marks_duplicated_frozen_plan_revision_as_source_error(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path / "missing.json")
    snapshot_path = Path(settings.executive_dashboard_sales_plan_snapshot_path)
    _write_sales_plan_snapshot(snapshot_path)
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    payload["months"].append(dict(payload["months"][0], revision_no=4))
    snapshot_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    _override_settings(monkeypatch, settings)
    db_session.add(_sales_kpi(date(2026, 6, 30)))
    db_session.commit()

    result = build_executive_sales_period_response(
        db_session,
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 30),
        today=date(2026, 7, 1),
    )

    assert result.source_status == "ready"
    assert result.plan_status == "source_error"
    assert "несколько frozen-планов" in str(result.plan_note)
    assert result.plan is None
    diagnostics = {item.key: item for item in result.diagnostic_kpis}
    assert diagnostics["margin_gap_pp"].source_status == "source_error"
    assert diagnostics["stores_below_plan_count"].source_status == "source_error"
    assert diagnostics["stores_below_plan_count"].meta["problem"] == []


def test_sales_period_plan_is_not_applicable_for_partial_month(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path / "missing.json")
    _write_sales_plan_snapshot(Path(settings.executive_dashboard_sales_plan_snapshot_path))
    _override_settings(monkeypatch, settings)
    db_session.add(_sales_kpi(date(2026, 6, 20)))
    db_session.commit()

    result = build_executive_sales_period_response(
        db_session,
        date_from=date(2026, 6, 10),
        date_to=date(2026, 6, 20),
        today=date(2026, 6, 20),
    )

    assert result.source_status == "ready"
    assert result.plan_status == "not_applicable"
    assert result.plan is None
    assert result.plan_note == "Плановые показатели доступны только в режиме «Месяц»."
    diagnostics = {item.key: item for item in result.diagnostic_kpis}
    assert diagnostics["gross_profit_per_unit"].source_status == "ready"
    assert diagnostics["margin_gap_pp"].source_status == "not_applicable"


def test_sales_period_handles_zero_revenue_and_volume_without_division(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path / "missing.json")
    _write_sales_plan_snapshot(Path(settings.executive_dashboard_sales_plan_snapshot_path))
    _override_settings(monkeypatch, settings)
    db_session.add(
        _sales_kpi(
            date(2026, 6, 30),
            revenue=Decimal("0"),
            cost_of_sales=Decimal("0"),
            sales_count=Decimal("0"),
        )
    )
    db_session.commit()

    result = build_executive_sales_period_response(
        db_session,
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 30),
        today=date(2026, 7, 1),
    )

    diagnostics = {item.key: item for item in result.diagnostic_kpis}
    assert result.source_status == "ready"
    assert result.plan_status == "ready"
    assert diagnostics["gross_profit_per_unit"].value is None
    assert diagnostics["gross_profit_per_unit"].source_status == "source_missing"
    assert diagnostics["cost_per_unit"].value is None
    assert diagnostics["lost_gross_profit_margin_gap"].value is None


def test_sales_period_monthly_comparison_sales_count_is_year_over_year(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _override_settings(monkeypatch, _settings(tmp_path / "missing.json"))
    db_session.add_all(
        [
            _sales_kpi(date(2026, 6, 10), sales_count=Decimal("7.000")),
            _sales_kpi(date(2026, 4, 10), sales_count=Decimal("11.000")),
            _sales_kpi(date(2025, 6, 10), sales_count=Decimal("3.000")),
            _sales_kpi(date(2025, 6, 20), sales_count=Decimal("99.000")),
            _sales_kpi(date(2025, 4, 10), sales_count=Decimal("17.000")),
        ]
    )
    db_session.commit()

    result = build_executive_sales_period_response(
        db_session,
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 15),
        today=date(2026, 6, 15),
    )

    by_month = {row.month: row for row in result.monthly}
    assert by_month["2026-06"].comparison_sales_count == Decimal("3.000")
    assert by_month["2026-04"].comparison_sales_count == Decimal("17.000")


def test_sales_period_marks_forecast_as_unavailable_without_history(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _override_settings(monkeypatch, _settings(tmp_path / "missing.json"))
    db_session.add(_sales_kpi(date(2026, 6, 5)))
    db_session.commit()

    result = build_executive_sales_period_response(
        db_session,
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 30),
        today=date(2026, 6, 5),
    )

    assert result.source_status == "ready"
    assert result.plan_status == "source_missing"
    assert result.plan_note is not None
    assert result.forecast_status == "insufficient_history"
    assert result.totals["forecast_revenue_period_end"] is None
    assert "четыре недели" in str(result.forecast_note)


def test_sales_period_does_not_recalculate_forecast_for_closed_month(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _override_settings(monkeypatch, _settings(tmp_path / "missing.json"))
    db_session.add(_sales_kpi(date(2026, 6, 30)))
    db_session.commit()

    result = build_executive_sales_period_response(
        db_session,
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 30),
        today=date(2026, 7, 1),
    )

    assert result.forecast_status == "not_applicable"
    assert result.totals["forecast_revenue_period_end"] is None


def test_sales_period_completed_range_notes_period_not_month(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _override_settings(monkeypatch, _settings(tmp_path / "missing.json"))
    cursor = date(2026, 5, 10)
    while cursor <= date(2026, 6, 20):
        db_session.add(_sales_kpi(cursor))
        cursor += timedelta(days=1)
    db_session.commit()

    result = build_executive_sales_period_response(
        db_session,
        date_from=date(2026, 6, 14),
        date_to=date(2026, 6, 20),
        today=date(2026, 6, 20),
    )

    assert result.forecast_status == "complete"
    assert result.forecast_note == "Период полностью закрыт фактическими данными."


def test_debtors_block_uses_buyer_cases_not_other_receivables(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _override_settings(monkeypatch, _settings(tmp_path / "missing.json"))
    db_session.add_all(
        [
            _receivable_case(
                date(2026, 6, 27),
                balance=Decimal("1000.00"),
                counterparty_ref="cp-positive",
                counterparty_name="Клиент с долгом",
            ),
            _receivable_case(
                date(2026, 6, 27),
                balance=Decimal("9000.00"),
                counterparty_ref="cp-employee",
                counterparty_name="Сотрудник с долгом",
                segment="employee",
            ),
        ]
    )
    db_session.commit()

    result = build_executive_dashboard(
        db_session,
        requested_date=date(2026, 6, 27),
        access_level="full",
    )

    debtors = {block.key: block for block in result.blocks}["debtors"]
    metric_keys = [metric.key for metric in debtors.metrics]
    assert metric_keys == [
        "total_receivable",
        "total_overdue",
        "overdue_90",
        "customer_count",
    ]
    assert debtors.metrics[0].value == Decimal("1000.00")
    assert debtors.metrics[1].value == Decimal("1000.00")
    assert debtors.summary["overdue_30_amount"] == "0"
    assert debtors.summary["need_call_today_amount"] == "1000.00"
    assert debtors.summary["source_segment"] == "buyers"
    assert debtors.summary["row_count"] == 1
    assert debtors.summary["drilldown_label"] == "Открыть рабочее место дебиторки"


def test_warehouse_block_reads_piecework_snapshot(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_path = tmp_path / "finance_snapshot.json"
    warehouse_path = tmp_path / "warehouse_snapshot.json"
    _write_warehouse_snapshot(warehouse_path)
    _override_settings(monkeypatch, _settings(snapshot_path))

    result = build_executive_dashboard(
        db_session,
        requested_date=date(2026, 6, 27),
        access_level="full",
    )

    warehouse = {block.key: block for block in result.blocks}["warehouse_operations"]
    metric_by_key = {metric.key: metric for metric in warehouse.metrics}
    assert warehouse.title == "Склад / сборка"
    assert warehouse.source_status == "ready"
    assert metric_by_key["pieces_picked"].value == Decimal("1250.00")
    assert metric_by_key["avg_need_fact"].value == Decimal("5.20")
    assert metric_by_key["quality_issue_count"].value == 3
    assert warehouse.summary["top_warehouses"][0]["warehouse_name"] == "Склад Сайт"
    assert "warehouse.piecework" in {source.source_key for source in result.source_freshness}


def test_receivables_control_block_keeps_folder_control_separate(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _override_settings(monkeypatch, _settings(tmp_path / "missing.json"))
    db_session.add_all(
        [
            _receivable_case(date(2026, 6, 27)),
            _work_item(phone_status="missing", payment_postponed_count=2),
            _folder_cache(date(2026, 6, 27)),
        ]
    )
    db_session.commit()

    result = build_executive_dashboard(
        db_session,
        requested_date=date(2026, 6, 27),
        access_level="full",
    )

    control = {block.key: block for block in result.blocks}["receivables_control"]
    metrics = {metric.key: metric.value for metric in control.metrics}
    assert control.title == "Контроль дебиторки"
    assert control.drilldown_url == "/bitrix/receivables/?date=2026-06-27&tab=folders"
    assert metrics["need_call_today_count"] == 1
    assert metrics["no_phone_count"] == 1
    assert metrics["payment_postponed_count"] == 2
    assert metrics["folder_needs_review_count"] == 3
    assert "folder_move_recommended_count" not in metrics
    assert control.summary["folder_move_recommended_count"] == 0
    assert control.summary["folder_control_source_status"] == "ready"


def test_domain_access_masks_financial_amounts(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_path = tmp_path / "finance_snapshot.json"
    snapshot_path.write_text(
        json.dumps(
            {
                "as_of": "2026-06-27",
                "source_status": "ready",
                "money_today": {
                    "source_status": "ready",
                    "bank_balance": "100000",
                    "cash_balance": "5000",
                    "cash_position": {
                        "source_status": "ready",
                        "as_of": "2026-06-27",
                        "total_balance": "105000",
                        "bank_balance_total": "100000",
                        "cashbox_balance_total": "5000",
                    },
                    "cashflow_today": {
                        "source_status": "ready",
                        "as_of": "2026-06-27",
                        "inflow_amount": "12000",
                        "outflow_amount": "7000",
                        "net_amount": "5000",
                        "movement_count": 3,
                    },
                },
                "creditors_payables": {
                    "source_status": "ready",
                    "source_anchor": "1C: Заказ поставщику -> Платежный календарь",
                    "total_payable": "3000",
                    "due_today": "3000",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _override_settings(monkeypatch, _settings(snapshot_path))
    db_session.add_all([_receivable_case(date(2026, 6, 27)), _action()])
    db_session.commit()

    result = build_executive_dashboard(
        db_session,
        requested_date=date(2026, 6, 27),
        access_level="domain",
        bitrix_user_id="77",
    )

    blocks = {block.key: block for block in result.blocks}
    money_metrics = {metric.key: metric for metric in blocks["money_today"].metrics}
    assert money_metrics["cash_position_total_balance"].masked is True
    assert money_metrics["cash_position_total_balance"].value is None
    assert "bank_balance" not in money_metrics
    assert blocks["creditors_payables"].metrics[0].masked is True
    assert result.top_actions[0].amount is None


def test_money_today_uses_cash_position_and_cashflow_today(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_path = tmp_path / "finance_snapshot.json"
    snapshot_path.write_text(
        json.dumps(
            {
                "as_of": "2026-06-27",
                "source_status": "partial",
                "money_today": {
                    "source_status": "partial",
                    "bank_balance": "999999",
                    "cash_balance": "888888",
                    "cash_position": {
                        "source_status": "ready",
                        "freshness_status": "fresh",
                        "as_of": "2026-06-27",
                        "total_balance": "57930000",
                        "total_balance_rub": "57930000",
                        "bank_balance_total": "3399434",
                        "bank_balance_total_rub": "3399434",
                        "savings_balance_total": "15222069",
                        "cashbox_balance_total": "54530566",
                        "cashbox_balance_total_rub": "54530566",
                        "foreign_balance_total": "11670000",
                        "negative_balance_total": "-345416",
                        "currency_count": 5,
                        "breakdown_by_currency": [
                            {
                                "cash_category": "cashboxes",
                                "cash_category_label": "Кассы",
                                "cash_currency_code": "840",
                                "cash_currency_name": "USD",
                                "balance_native": "103123.85",
                                "balance_rub": "10111287.44",
                            }
                        ],
                    },
                    "cashflow_today": {
                        "source_status": "ready",
                        "freshness_status": "fresh",
                        "as_of": "2026-06-27",
                        "inflow_amount": "40000",
                        "outflow_amount": "10000",
                        "net_amount": "30000",
                        "movement_count": 5,
                        "review_count": 2,
                    },
                },
                "source_freshness": {
                    "finance.cash_position": {
                        "title": "Остатки денег 1C",
                        "source_status": "ready",
                        "freshness_status": "fresh",
                        "as_of": "2026-06-27",
                    },
                    "finance.cashflow": {
                        "title": "ДДС / cashflow",
                        "source_status": "ready",
                        "freshness_status": "fresh",
                        "as_of": "2026-06-27",
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _override_settings(monkeypatch, _settings(snapshot_path))

    result = build_executive_dashboard(
        db_session,
        requested_date=date(2026, 6, 27),
        access_level="full",
    )

    money = {block.key: block for block in result.blocks}["money_today"]
    metric_by_key = {metric.key: metric for metric in money.metrics}
    assert money.title == "Деньги / ДДС"
    assert metric_by_key["cash_position_total_balance"].value == Decimal("57930000")
    assert metric_by_key["cash_position_bank_balance_total"].value == Decimal("3399434")
    savings_metric = metric_by_key["cash_position_savings_balance_total"]
    assert savings_metric.value == Decimal("15222069")
    assert savings_metric.label == "Сберсчета / личные счета"
    assert metric_by_key["cash_position_foreign_balance_total"].value == Decimal("11670000")
    assert metric_by_key["cash_position_negative_balance_total"].value == Decimal("-345416")
    assert money.summary["cash_position_breakdown_by_currency"][0]["cash_currency_name"] == "USD"
    assert metric_by_key["cashflow_net_amount"].value == Decimal("30000")
    assert metric_by_key["cashflow_review_count"].value == 2
    assert "bank_balance" not in metric_by_key
    assert {"finance.cash_position", "finance.cashflow"}.issubset(
        {source.source_key for source in result.source_freshness}
    )


def test_reconciliation_block_surfaces_issue_details_and_report_delivery(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_path = tmp_path / "finance_snapshot.json"
    snapshot_path.write_text(
        json.dumps(
            {
                "as_of": "2026-06-27",
                "source_status": "ready",
                "reconciliation": {
                    "source_status": "ready",
                    "as_of": "2026-06-27",
                    "unmatched_count": 3,
                    "issue_amount_abs": "17093.00",
                    "issue_breakdown": [
                        {
                            "issue_type": "sber_onec_amount_mismatch",
                            "label": "Сумма Сбера и 1С не совпадает",
                            "count": 3,
                        }
                    ],
                    "issue_examples": [
                        {
                            "issue_key": "i1",
                            "issue_type_label": "Сумма Сбера и 1С не совпадает",
                            "department": "МСК-017",
                            "amount_delta": "12340.00",
                            "proposed_action": "Сверить сумму Sber API, банковского ордера и оплаты картой 1С.",
                        }
                    ],
                    "report_delivery": {
                        "status": "sent",
                        "task_count": 1,
                        "task_ids": ["1550"],
                    },
                    "dds_issue_count": 2,
                    "dds_issue_examples": [
                        {
                            "issue_key": "dds-1",
                            "description": "Документ без статьи ДДС",
                            "proposed_action": "Заполнить статью ДДС.",
                        }
                    ],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _override_settings(monkeypatch, _settings(snapshot_path))

    result = build_executive_dashboard(
        db_session,
        requested_date=date(2026, 6, 27),
        access_level="full",
    )

    block = {item.key: item for item in result.blocks}["reconciliation"]
    metric_by_key = {metric.key: metric for metric in block.metrics}
    assert metric_by_key["unmatched_count"].value == 3
    assert metric_by_key["issue_amount_abs"].value == Decimal("17093.00")
    assert metric_by_key["report_task_count"].value == 1
    assert block.summary["issue_breakdown"][0]["count"] == 3
    assert block.summary["issue_examples"][0]["department"] == "МСК-017"
    assert block.summary["report_delivery"]["task_ids"] == ["1550"]
    assert block.summary["dds_issue_examples"][0]["issue_key"] == "dds-1"


def test_procurement_role_receives_only_procurement_block_and_actions(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_path = tmp_path / "finance_snapshot.json"
    snapshot_path.write_text(
        json.dumps(
            {
                "as_of": "2026-06-27",
                "source_status": "ready",
                "money_today": {
                    "source_status": "ready",
                    "bank_balance": "100000",
                },
                "procurement_import": {
                    "source_status": "ready",
                    "open_supplier_orders": 4,
                    "open_order_amount_rub": "1250000",
                    "payment_ready_amount": "1250000",
                    "currency_exposure": "1250000",
                    "risk_summary": {
                        "at_risk_count": 2,
                        "at_risk_amount_rub": "500000",
                        "critical_count": 1,
                    },
                    "stage_breakdown": [
                        {"key": "in_transit", "label": "В пути", "count": 2, "amount_rub": "800000"}
                    ],
                    "currency_breakdown": [
                        {"currency": "RMB", "count": 3, "amount_rub": "1200000"}
                    ],
                    "data_quality": {"responsible_coverage_pct": "75.0"},
                },
                "creditors_payables": {
                    "source_status": "ready",
                    "total_payable": "50000",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    settings = _settings(snapshot_path, access_rules_json=_role_rules())
    _override_settings(monkeypatch, settings)
    access = bitrix_executive_dashboard_auth.resolve_executive_dashboard_access(
        bitrix_user_id="201",
        settings=settings,
    )
    access_context = bitrix_executive_dashboard_auth.ExecutiveDashboardAuthContext(
        actor="bitrix:member-1:201",
        source="bitrix",
        access_level=access.access_level,
        bitrix_user_id="201",
        roles=access.roles,
        allowed_blocks=access.allowed_blocks,
        allowed_action_domains=access.allowed_action_domains,
        money_blocks=access.money_blocks,
        personal_actions_only=access.personal_actions_only,
    )
    db_session.add_all(
        [
            _receivable_case(date(2026, 6, 27)),
            _action(
                stable_key="procurement-visible",
                domain="procurement_import",
                amount=Decimal("1250000.00"),
                responsible_bitrix_user_id="42",
            ),
            _action(stable_key="debtors-hidden", domain="debtors", responsible_bitrix_user_id=None),
        ]
    )
    db_session.commit()

    result = build_executive_dashboard(
        db_session,
        requested_date=date(2026, 6, 27),
        access_context=access_context,
    )

    assert [block.key for block in result.blocks] == ["procurement_import"]
    procurement = result.blocks[0]
    metric_by_key = {metric.key: metric for metric in procurement.metrics}
    assert metric_by_key["payment_ready_amount"].masked is False
    assert metric_by_key["payment_ready_amount"].value == Decimal("1250000")
    assert metric_by_key["procurement_at_risk_count"].value == 2
    assert procurement.summary["stage_breakdown"][0]["amount_rub"] == "800000"
    assert [item.stable_key for item in result.top_actions] == ["procurement-visible"]
    assert result.source_freshness[0].source_key == "procurement_import"


def test_actions_filter_for_domain_user_and_status(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _override_settings(monkeypatch, _settings(tmp_path / "missing.json"))
    db_session.add_all(
        [
            _action(stable_key="a-visible", responsible_bitrix_user_id="77"),
            _action(stable_key="a-hidden", domain="tasks", responsible_bitrix_user_id="42"),
            _action(stable_key="a-public", domain="tasks", responsible_bitrix_user_id=None),
        ]
    )
    db_session.commit()

    result = build_executive_actions_response(
        db_session,
        requested_date=date(2026, 6, 27),
        status="open",
        domain=None,
        access_level="domain",
        bitrix_user_id="77",
    )

    assert {item.stable_key for item in result.payload} == {"a-visible", "a-public"}


def test_procurement_nested_amounts_are_masked_without_money_permission(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_path = tmp_path / "finance_snapshot.json"
    snapshot_path.write_text(
        json.dumps(
            {
                "as_of": "2026-07-11",
                "source_status": "ready",
                "procurement_import": {
                    "as_of": "2026-07-11",
                    "source_status": "ready",
                    "open_supplier_orders": 1,
                    "open_order_amount_rub": "1000",
                    "risk_summary": {"at_risk_count": 1, "at_risk_amount_rub": "1000"},
                    "stage_breakdown": [{"key": "in_transit", "count": 1, "amount_rub": "1000"}],
                    "currency_breakdown": [{"currency": "RMB", "count": 1, "amount_rub": "1000"}],
                },
            }
        ),
        encoding="utf-8",
    )
    _override_settings(monkeypatch, _settings(snapshot_path))
    context = bitrix_executive_dashboard_auth.ExecutiveDashboardAuthContext(
        actor="test",
        source="internal",
        access_level="domain",
        allowed_blocks=("procurement_import",),
        allowed_action_domains=("procurement_import",),
        money_blocks=(),
    )

    result = build_executive_dashboard(
        db_session,
        requested_date=date(2026, 7, 11),
        access_context=context,
    )

    block = result.blocks[0]
    assert block.summary["risk_summary"]["at_risk_amount_rub"] is None
    assert block.summary["stage_breakdown"][0]["amount_rub"] is None
    assert block.summary["currency_breakdown"][0]["amount_rub"] is None
    assert next(metric for metric in block.metrics if metric.key == "open_order_amount_rub").masked


def test_procurement_snapshot_attention_becomes_clickable_action_and_disappears_after_fix(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_path = tmp_path / "finance_snapshot.json"
    snapshot = {
        "as_of": "2026-07-11",
        "source_status": "partial",
        "procurement_import": {
            "as_of": "2026-07-11",
            "source_status": "ready",
            "attention_items": [
                {
                    "onec_ref": "0x01",
                    "onec_source_number": "РБГУ0001",
                    "supplier_title": "Поставщик",
                    "amount_rub": "1000.00",
                    "currency": "RMB",
                    "reason_code": "missing_cargo_handoff_date",
                    "reason": "Не заполнена дата «Сдача в карго».",
                    "severity": "warning",
                    "deadline_date": "2026-07-10",
                    "recommendation": "Заполнить поле в документе 1С.",
                    "source_system": "1C",
                }
            ],
        },
    }
    snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    _override_settings(monkeypatch, _settings(snapshot_path))

    result = build_executive_actions_response(
        db_session,
        requested_date=date(2026, 7, 11),
        status="open",
        domain="procurement_import",
        access_level="full",
    )

    assert result.total_count == 1
    action = result.payload[0]
    assert action.title == "Заказ РБГУ0001: Не заполнена дата «Сдача в карго»."
    assert action.severity == "warning"
    assert action.deadline_at.date() == date(2026, 7, 10)
    assert action.amount == Decimal("1000.00")
    assert action.source_ref == "0x01"
    assert action.payload["correction_system"] == "1C"
    assert action.payload["correction_field"] == "Сдача в карго"

    dashboard = build_executive_dashboard(
        db_session,
        requested_date=date(2026, 7, 11),
        access_level="full",
    )
    assert next(block for block in dashboard.blocks if block.key == "tasks")

    snapshot["procurement_import"]["attention_items"] = []
    snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    resolved = build_executive_actions_response(
        db_session,
        requested_date=date(2026, 7, 11),
        status="open",
        domain="procurement_import",
        access_level="full",
    )
    assert resolved.total_count == 0
    assert resolved.source_status == "empty"


def test_cashflow_period_response_aggregates_period_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_path = tmp_path / "finance_snapshot.json"
    cache_path = tmp_path / "cashflow_period_cache.json"
    _write_cashflow_period_cache(cache_path)
    _override_settings(monkeypatch, _settings(snapshot_path))

    result = build_executive_cashflow_period_response(
        date_from=date(2026, 6, 27),
        date_to=date(2026, 6, 28),
    )

    assert result.source_status == "ready"
    assert result.totals["external_inflow_amount"] == Decimal("1000.00")
    assert result.totals["external_outflow_amount"] == Decimal("400.00")
    assert result.totals["external_net_amount"] == Decimal("600.00")
    assert result.totals["internal_inflow_amount"] == Decimal("100.00")
    assert result.totals["quality_issue_count"] == 3
    assert result.totals["quality_issue_amount_abs"] == Decimal("1200.00")
    assert result.cash_position["closing"]["total_balance"] == "1500.00"
    assert result.daily[-1].business_date == date(2026, 6, 28)
    assert result.quality_issues[0].issue_label == "Документ без статьи ДДС"


def test_cashflow_owner_issue_keeps_live_status_and_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_path = tmp_path / "finance_snapshot.json"
    cache_path = tmp_path / "cashflow_period_cache.json"
    _write_cashflow_period_cache(cache_path)
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    cache["quality_issues"] = [
        {
            "issue_key": "owner-transfer:960",
            "issue_type": "owner_transfer_unmatched_incoming",
            "issue_label": "Нет исходящего платежа на карту",
            "severity": "high",
            "business_date": "2026-06-28",
            "amount_abs": "200000.00",
            "status": "open",
            "document_number": "РБГУ0151620",
            "bitrix_task_id": "960",
            "task_status": "completed",
            "drilldown_url": (
                "https://crm.master-mobile.ru/company/personal/user/0/tasks/" "task/view/960/"
            ),
        }
    ]
    cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    _override_settings(monkeypatch, _settings(snapshot_path))

    result = build_executive_cashflow_period_response(
        date_from=date(2026, 6, 27),
        date_to=date(2026, 6, 28),
    )

    assert result.source_status == "ready"
    issue = result.quality_issues[0]
    assert issue.document_number is None
    assert issue.bitrix_task_id is None
    assert issue.task_status is None
    assert issue.drilldown_url is None


def test_profit_loss_period_api_forbids_user_without_money_access(
    client: TestClient,
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path / "finance_snapshot.json", access_rules_json=_role_rules())
    _override_settings(monkeypatch, settings)
    access = bitrix_executive_dashboard_auth.resolve_executive_dashboard_access(
        bitrix_user_id="202",
        settings=settings,
    )
    token, _ = bitrix_executive_dashboard_auth.create_executive_dashboard_session_token(
        domain="crm.master-mobile.ru",
        member_id="member-1",
        user_id="202",
        user_name="Дебиторка",
        access=access,
        settings=settings,
        now=1_785_000_000,
    )
    app.dependency_overrides[get_db] = lambda: db_session
    try:
        response = client.get(
            "/api/management/executive-dashboard/profit-loss-period?date_from=2026-06-27&date_to=2026-06-28",
            headers={"Authorization": f"Bearer {token}"},
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 403


def test_profit_loss_period_api_returns_sales_for_finance_role(
    client: TestClient,
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path / "finance_snapshot.json", access_rules_json=_role_rules())
    _write_profit_loss_cashflow_cache(tmp_path / "cashflow_period_cache.json")
    _override_settings(monkeypatch, settings)
    db_session.add(
        _sales_kpi(
            date(2026, 6, 28),
            revenue=Decimal("1200.00"),
            cost_of_sales=Decimal("700.00"),
            sales_count=Decimal("3.000"),
        )
    )
    db_session.commit()
    access = bitrix_executive_dashboard_auth.resolve_executive_dashboard_access(
        bitrix_user_id="203",
        settings=settings,
    )
    token, _ = bitrix_executive_dashboard_auth.create_executive_dashboard_session_token(
        domain="crm.master-mobile.ru",
        member_id="member-1",
        user_id="203",
        user_name="Финансы",
        access=access,
        settings=settings,
        now=1_785_000_000,
    )
    app.dependency_overrides[get_db] = lambda: db_session
    try:
        response = client.get(
            "/api/management/executive-dashboard/profit-loss-period?date_from=2026-06-27&date_to=2026-06-28",
            headers={"Authorization": f"Bearer {token}"},
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["totals"]["revenue"] == "1200.00"
    assert payload["totals"]["cost_of_sales"] == "700.00"
    assert payload["totals"]["gross_profit"] == "500.00"
    assert payload["totals"]["operating_expenses"] == "150.00"
    assert payload["totals"]["operating_profit"] == "350.00"
    assert payload["expense_source_status"] == "partial"
    assert payload["expense_breakdown"][0]["key"] == "rent"
    assert payload["expense_open_questions"][0]["amount"] == "400.00"
    assert payload["lines"][-1]["source_status"] == "partial"
    assert payload["totals"]["net_profit"] == "350.00"


def test_sales_period_api_is_available_to_full_access_and_forbidden_to_finance(
    client: TestClient,
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path / "finance_snapshot.json", access_rules_json=_role_rules())
    _override_settings(monkeypatch, settings)
    db_session.add(_sales_kpi(date(2026, 6, 28), revenue=Decimal("1200.00")))
    db_session.commit()

    app.dependency_overrides[get_db] = lambda: db_session
    try:
        full_response = client.get(
            "/api/management/executive-dashboard/sales-period"
            "?date_from=2026-06-01&date_to=2026-06-30",
            headers={"Authorization": "Bearer secret-token"},
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert full_response.status_code == 200
    assert full_response.json()["month"] == "2026-06"

    finance_access = bitrix_executive_dashboard_auth.resolve_executive_dashboard_access(
        bitrix_user_id="203",
        settings=settings,
    )
    token, _ = bitrix_executive_dashboard_auth.create_executive_dashboard_session_token(
        domain="crm.master-mobile.ru",
        member_id="member-1",
        user_id="203",
        user_name="Финансы",
        access=finance_access,
        settings=settings,
        now=1_785_000_000,
    )
    app.dependency_overrides[get_db] = lambda: db_session
    try:
        finance_response = client.get(
            "/api/management/executive-dashboard/sales-period"
            "?date_from=2026-06-01&date_to=2026-06-30",
            headers={"Authorization": f"Bearer {token}"},
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert finance_response.status_code == 403


def test_cashflow_period_api_forbids_user_without_money_access(
    client: TestClient,
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path / "finance_snapshot.json", access_rules_json=_role_rules())
    _write_cashflow_period_cache(tmp_path / "cashflow_period_cache.json")
    _override_settings(monkeypatch, settings)
    access = bitrix_executive_dashboard_auth.resolve_executive_dashboard_access(
        bitrix_user_id="202",
        settings=settings,
    )
    token, _ = bitrix_executive_dashboard_auth.create_executive_dashboard_session_token(
        domain="crm.master-mobile.ru",
        member_id="member-1",
        user_id="202",
        user_name="Дебиторка",
        access=access,
        settings=settings,
        now=1_785_000_000,
    )
    app.dependency_overrides[get_db] = lambda: db_session
    try:
        response = client.get(
            "/api/management/executive-dashboard/cashflow-period?date_from=2026-06-27&date_to=2026-06-28",
            headers={"Authorization": f"Bearer {token}"},
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 403


def test_cashflow_period_api_returns_money_for_finance_role(
    client: TestClient,
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path / "finance_snapshot.json", access_rules_json=_role_rules())
    _write_cashflow_period_cache(tmp_path / "cashflow_period_cache.json")
    _override_settings(monkeypatch, settings)
    access = bitrix_executive_dashboard_auth.resolve_executive_dashboard_access(
        bitrix_user_id="203",
        settings=settings,
    )
    token, _ = bitrix_executive_dashboard_auth.create_executive_dashboard_session_token(
        domain="crm.master-mobile.ru",
        member_id="member-1",
        user_id="203",
        user_name="Финансы",
        access=access,
        settings=settings,
        now=1_785_000_000,
    )
    app.dependency_overrides[get_db] = lambda: db_session
    try:
        response = client.get(
            "/api/management/executive-dashboard/cashflow-period?date_from=2026-06-27&date_to=2026-06-28",
            headers={"Authorization": f"Bearer {token}"},
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["totals"]["external_net_amount"] == "600.00"
    assert payload["totals"]["quality_issue_count"] == 3
    assert payload["quality_issues"][0]["issue_label"] == "Документ без статьи ДДС"


def test_cashflow_period_allows_one_day_cache_lag_as_partial_fresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path / "finance_snapshot.json")
    _write_cashflow_period_cache(tmp_path / "cashflow_period_cache.json")
    _override_settings(monkeypatch, settings)

    result = build_executive_cashflow_period_response(
        date_from=date(2026, 6, 27),
        date_to=date(2026, 7, 1),
    )

    assert result.daily
    assert result.source_status == "partial"
    assert result.freshness_status == "fresh"
    assert "допустимого лага" in (result.note or "")


def test_cashflow_period_marks_two_day_cache_lag_as_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path / "finance_snapshot.json")
    _write_cashflow_period_cache(tmp_path / "cashflow_period_cache.json")
    _override_settings(monkeypatch, settings)

    result = build_executive_cashflow_period_response(
        date_from=date(2026, 6, 27),
        date_to=date(2026, 7, 2),
    )

    assert result.daily
    assert result.source_status == "stale"
    assert result.freshness_status == "stale"
    assert "выходит за кэш" in (result.note or "")


def test_actions_api_forbids_foreign_domain_for_role_policy(
    client: TestClient,
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path / "missing.json", access_rules_json=_role_rules())
    _override_settings(monkeypatch, settings)
    access = bitrix_executive_dashboard_auth.resolve_executive_dashboard_access(
        bitrix_user_id="201",
        settings=settings,
    )
    token, _ = bitrix_executive_dashboard_auth.create_executive_dashboard_session_token(
        domain="crm.master-mobile.ru",
        member_id="member-1",
        user_id="201",
        user_name="Закупщик",
        access=access,
        settings=settings,
        now=1_785_000_000,
    )
    app.dependency_overrides[get_db] = lambda: db_session
    try:
        response = client.get(
            "/api/management/executive-dashboard/actions?date=2026-06-27&domain=debtors",
            headers={"Authorization": f"Bearer {token}"},
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 403


def test_executive_dashboard_api_accepts_internal_token(
    client: TestClient,
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _override_settings(monkeypatch, _settings(tmp_path / "missing.json"))
    db_session.add(_receivable_case(date(2026, 6, 27)))
    db_session.commit()
    app.dependency_overrides[get_db] = lambda: db_session
    try:
        response = client.get(
            "/api/management/executive-dashboard?date=2026-06-27",
            headers={"Authorization": "Bearer secret-token"},
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["access_level"] == "full"
    assert {block["key"] for block in payload["blocks"]} >= {
        "money_today",
        "debtors",
        "receivables_control",
        "creditors_payables",
        "daily_focus",
    }
