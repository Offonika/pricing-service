from __future__ import annotations

import json
from datetime import date, datetime
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
from app.services import bitrix_executive_dashboard_auth, executive_dashboard
from app.services.executive_dashboard import (
    _resolve_shared_path,
    build_executive_actions_response,
    build_executive_cashflow_period_response,
    build_executive_dashboard,
    build_executive_profit_loss_period_response,
)


def _settings(snapshot_path: Path, *, access_rules_json: str | None = None) -> Settings:
    return Settings(
        management_internal_api_token="secret-token",
        executive_dashboard_finance_snapshot_path=str(snapshot_path),
        executive_dashboard_cashflow_period_cache_path=str(
            snapshot_path.parent / "cashflow_period_cache.json"
        ),
        executive_dashboard_warehouse_snapshot_path=str(
            snapshot_path.parent / "warehouse_snapshot.json"
        ),
        executive_dashboard_bitrix_enabled=True,
        executive_dashboard_bitrix_allowed_domains=["crm.master-mobile.ru"],
        executive_dashboard_bitrix_allowed_member_ids=["member-1"],
        executive_dashboard_bitrix_full_access_user_ids=["42"],
        executive_dashboard_bitrix_domain_access_user_ids=["77"],
        executive_dashboard_access_rules_json=access_rules_json,
        executive_dashboard_bitrix_session_secret="test-executive-dashboard-session-secret",
    )


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
        == "1C: Задолженность поставщикам товаров / Поставщики; "
        "Взаиморасчеты с контрагентами / СОТРУДНИКИ"
    )
    assert blocks["debtors"].source_status == "ready"
    assert blocks["debtors"].title == "Дебиторка покупателей"
    assert blocks["debtors"].metrics[0].value == Decimal("12500.00")
    assert blocks["debtors"].drilldown_url == "/bitrix/receivables/?date=2026-06-27"
    assert blocks["receivables_control"].source_status == "partial"


def test_payables_block_exposes_only_negative_net_debt(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_path = tmp_path / "finance_snapshot.json"
    snapshot_path.write_text(
        json.dumps(
            {
                "as_of": "2026-07-11",
                "creditors_payables": {
                    "source_status": "ready",
                    "freshness_status": "fresh",
                    "as_of": "2026-07-11",
                    "total_payable": "130.00",
                    "gross_payable": "150.00",
                    "supplier_payable": "80.00",
                    "employee_payable": "50.00",
                    "counterparty_count": 2,
                    "reverse_balance": "20.00",
                    "reverse_balance_label": "Авансы / переплаты (−)",
                    "counterparties": [
                        {"counterparty_name": "Поставщик", "payable_amount": "100.00"},
                        {"counterparty_name": "Сотрудник", "payable_amount": "50.00"},
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
        requested_date=date(2026, 7, 11),
        access_level="full",
    )

    block = next(item for item in result.blocks if item.key == "creditors_payables")
    metrics = {metric.key: metric.value for metric in block.metrics}
    assert block.title == "Кредиторская задолженность"
    assert metrics == {
        "total_payable": Decimal("-130.00"),
        "supplier_payable": Decimal("-80.00"),
        "employee_payable": Decimal("-50.00"),
    }
    assert block.summary["reverse_balance"] == "20.00"


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
    workspace_root = tmp_path / "workspace"
    snapshot_path = workspace_root / "mm-compensation" / "build" / "snapshot.json"
    snapshot_path.parent.mkdir(parents=True)
    snapshot_path.write_text("{}", encoding="utf-8")
    release_root = tmp_path / "releases" / "pricing-service" / "release-1"
    release_root.mkdir(parents=True)
    monkeypatch.chdir(release_root)
    monkeypatch.setenv("MM_WORKSPACE_ROOT", str(workspace_root))

    resolved = _resolve_shared_path("../mm-compensation/build/snapshot.json")

    assert resolved == snapshot_path


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
    assert block.summary["missing_expense_line_count"] == 2
    assert "profit_loss" in {source.source_key for source in result.source_freshness}


def test_profit_loss_period_response_aggregates_sales_kpi(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_profit_loss_cashflow_cache(tmp_path / "cashflow_period_cache.json")
    _override_settings(monkeypatch, _settings(tmp_path / "finance_snapshot.json"))
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
    assert line_by_key["net_profit"].source_status == "source_missing"
    assert result.expense_source_status == "partial"
    assert {row.key for row in result.expense_breakdown} == {"rent", "bank_fees"}
    assert result.expense_open_questions[0].amount == Decimal("400.00")
    assert result.daily[-1].business_date == date(2026, 6, 27)
    assert {row.label for row in result.by_store} == {"Горбушкин Двор", "Склад Сайт"}


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
                    "payment_ready_amount": "1250000",
                    "currency_exposure": "1250000",
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
    assert action.title == "Заказ РБГУ0001: заполнить «Сдача в карго»"
    assert action.amount == Decimal("1000.00")
    assert action.source_ref == "0x01"
    assert action.payload["correction_system"] == "1C"
    assert action.payload["correction_field"] == "Сдача в карго"

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
    assert payload["lines"][-1]["source_status"] == "source_missing"


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


def test_cashflow_period_marks_partial_cache_coverage_as_stale(
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
