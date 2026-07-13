from __future__ import annotations

from datetime import date

import pytest

from infra.cron.management_digest_from_a import build_management_digest, render_management_digest


def _task_efficiency_response(month: str = "2026-03") -> dict:
    month_end = {
        "2026-03": "2026-03-31",
        "2026-04": "2026-04-30",
    }.get(month, f"{month}-28")
    return {
        "as_of": month,
        "month": month,
        "freshness_status": "fresh",
        "source_status": "ready",
        "summary": {
            "employee_count": 2,
            "applicable_count": 2,
            "total_personal_tasks_with_deadline": 8,
            "closed_on_time_personal_tasks": 6,
            "late_closed_personal_tasks": 1,
            "open_overdue_personal_tasks": 1,
            "canceled_personal_tasks": 0,
            "average_on_time_share": 75.0,
            "bitrix_average_effectiveness_pct": 75.0,
            "bitrix_total_in_work_count": 8,
            "bitrix_completed_tasks_count": 6,
            "bitrix_task_remarks_count": 2,
            "low_efficiency_threshold": 80.0,
            "low_efficiency_count": 1,
        },
        "payload": [
            {
                "month_start": f"{month}-01",
                "month_end": month_end,
                "employee_bitrix_id": "1",
                "employee_key": "emp-ivan",
                "employee_name": "Иван",
                "total_personal_tasks_with_deadline": 4,
                "closed_on_time_personal_tasks": 4,
                "late_closed_personal_tasks": 0,
                "open_overdue_personal_tasks": 0,
                "canceled_personal_tasks": 0,
                "personal_tasks_on_time_share": 100.0,
                "bitrix_total_in_work_count": 4,
                "bitrix_completed_tasks_count": 4,
                "bitrix_task_remarks_count": 0,
                "bitrix_effectiveness_pct": 100.0,
                "is_metric_applicable": True,
            },
            {
                "month_start": f"{month}-01",
                "month_end": month_end,
                "employee_bitrix_id": "2",
                "employee_key": "emp-petr",
                "employee_name": "Петр",
                "total_personal_tasks_with_deadline": 4,
                "closed_on_time_personal_tasks": 2,
                "late_closed_personal_tasks": 1,
                "open_overdue_personal_tasks": 1,
                "canceled_personal_tasks": 0,
                "personal_tasks_on_time_share": 50.0,
                "bitrix_total_in_work_count": 4,
                "bitrix_completed_tasks_count": 2,
                "bitrix_task_remarks_count": 2,
                "bitrix_effectiveness_pct": 50.0,
                "is_metric_applicable": True,
            },
        ],
    }


def test_build_management_digest_success() -> None:
    responses = {
        ("/api/management/health", "date=2026-03-20"): {
            "status": "ok",
            "freshness_status": "fresh",
            "source_status": "ready",
            "components": [
                {
                    "component": "receivables",
                    "freshness_status": "fresh",
                    "source_status": "ready",
                },
                {
                    "component": "staffing",
                    "freshness_status": "fresh",
                    "source_status": "ready",
                },
                {
                    "component": "task_payloads",
                    "freshness_status": "fresh",
                    "source_status": "ready",
                    "latest_snapshot_date": "2026-03-20",
                },
            ],
        },
        ("/api/receivables/new-daily", "date=2026-03-20"): {
            "freshness_status": "fresh",
            "source_status": "ready",
            "payload": [
                {
                    "counterparty_ref": "cp-1",
                    "counterparty_name": "Контрагент 1",
                    "current_balance": "12000",
                },
                {
                    "counterparty_ref": "cp-2",
                    "counterparty_name": "Контрагент 2",
                    "current_balance": "5000",
                },
            ],
        },
        ("/api/receivables/cases", "date=2026-03-20&segment=overdue"): {
            "freshness_status": "fresh",
            "source_status": "ready",
            "payload": [
                {"current_balance": "6000"},
                {"current_balance": "3000"},
            ],
        },
        ("/api/receivables/cases", "date=2026-03-20&segment=inactive"): {
            "freshness_status": "fresh",
            "source_status": "ready",
            "payload": [{"current_balance": "30000"}],
        },
        ("/api/receivables/cases", "date=2026-03-20&segment=fired_manager"): {
            "freshness_status": "fresh",
            "source_status": "ready",
            "payload": [{"current_balance": "7000"}],
        },
        ("/api/receivables/cases", "date=2026-03-20&segment=adjustment_candidates"): {
            "freshness_status": "fresh",
            "source_status": "ready",
            "payload": [{"current_balance": "15000"}],
        },
        ("/api/receivables/employee-cases", "date=2026-03-20"): {
            "freshness_status": "fresh",
            "source_status": "ready",
            "payload": [{"current_balance": "4000"}],
        },
        ("/api/receivables/manager-summary", "date=2026-03-20"): {
            "freshness_status": "fresh",
            "source_status": "ready",
            "payload": [
                {
                    "manager_ref": "mgr-1",
                    "manager_name": "Иван",
                    "counterparty_count": 2,
                    "total_balance": "33000",
                },
                {
                    "manager_ref": None,
                    "manager_name": None,
                    "counterparty_count": 3,
                    "total_balance": "50000",
                },
            ],
        },
        (
            "/api/bi/receivables-contract-balances",
            "buyers_rub_only=true&date=2026-03-20",
        ): [
            {"current_balance": "3609847.63"},
            {"current_balance": "2000000.00"},
        ],
        ("/api/bi/receivables-current", "date=2026-03-20"): {
            "payload": [
                {
                    "current_balance": "5609847.63",
                    "aged_bucket": "8-30",
                }
            ]
        },
        (
            "/api/bi/receivables-contract-balances",
            "buyers_rub_only=true&date=2026-03-19",
        ): [
            {"current_balance": "3600000.00"},
            {"current_balance": "2000000.00"},
        ],
        (
            "/api/bi/receivables-contract-balances",
            "buyers_rub_only=true&date=2026-03-13",
        ): [
            {"current_balance": "3550000.00"},
            {"current_balance": "2000000.00"},
        ],
        (
            "/api/bi/receivables-contract-balances",
            "buyers_rub_only=true&date=2026-02-20",
        ): [
            {"current_balance": "3500000.00"},
            {"current_balance": "2000000.00"},
        ],
        ("/api/bi/sales-daily-kpi", "date_from=2026-03-20&date_to=2026-03-20"): {
            "payload": [
                {
                    "revenue": "2581273.70",
                    "sales_count": "2753.000",
                    "cost_of_sales": "2000000.00",
                },
            ],
        },
        ("/api/bi/sales-daily-kpi", "date_from=2026-03-19&date_to=2026-03-19"): {
            "payload": [
                {
                    "revenue": "2500000.00",
                    "sales_count": "2700.000",
                    "cost_of_sales": "2000000.00",
                },
            ],
        },
        ("/api/bi/sales-weekly-kpi", "date_from=2026-03-16&date_to=2026-03-20"): {
            "payload": [
                {
                    "revenue": "12000000.00",
                    "sales_count": "12500.000",
                    "cost_of_sales": "9000000.00",
                },
            ],
        },
        ("/api/bi/sales-weekly-kpi", "date_from=2026-03-09&date_to=2026-03-13"): {
            "payload": [
                {
                    "revenue": "11800000.00",
                    "sales_count": "12400.000",
                    "cost_of_sales": "9000000.00",
                },
            ],
        },
        ("/api/bi/sales-daily-kpi", "date_from=2026-03-01&date_to=2026-03-20"): {
            "payload": [
                {
                    "revenue": "50200000.00",
                    "sales_count": "53000.000",
                    "cost_of_sales": "38000000.00",
                },
            ],
        },
        ("/api/bi/sales-daily-kpi", "date_from=2026-02-01&date_to=2026-02-20"): {
            "payload": [
                {
                    "revenue": "47000000.00",
                    "sales_count": "50000.000",
                    "cost_of_sales": "36000000.00",
                },
            ],
        },
        ("/api/staffing/daily", "date=2026-03-20"): {
            "freshness_status": "fresh",
            "source_status": "ready",
            "payload": [
                {
                    "store_ref": "s1",
                    "store_name": "Точка 1",
                    "shift_code": "AM",
                    "deficit_count": 2,
                    "criticality": "critical",
                }
            ],
        },
        ("/api/management/task-payloads", "date=2026-03-20"): {
            "freshness_status": "fresh",
            "source_status": "ready",
            "payload": [
                {"rule_code": "receivable_employee"},
                {"rule_code": "staffing_shift_deficit"},
            ],
        },
        ("/api/management/task-efficiency", "month=2026-03"): _task_efficiency_response("2026-03"),
        (
            "/api/management/exchange-counterparty-settlements",
            "counterparty_code=РБ002085",
        ): {
            "status": "ready",
            "control_status": "warning",
            "counterparty_code": "РБ002085",
            "counterparty_name": "Обменник",
            "generated_at_msk": "2026-03-20T09:00:00+03:00",
            "period_start": "2026-03-01",
            "summary_by_currency": [
                {
                    "contract_currency_code": "643",
                    "contract_currency_name": "руб",
                    "current_balance": "1000000.00",
                    "current_balance_rub": "1000000.00",
                },
                {
                    "contract_currency_code": "840",
                    "contract_currency_name": "USD",
                    "current_balance": "-1000.00",
                    "current_balance_rub": "-90000.00",
                },
            ],
            "rub_control": {
                "rub_inflow": "90000.00",
                "foreign_outflow_rub": "90000.00",
                "movement_diff_rub": "0.00",
                "closing_balance_rub": "0.00",
                "status": "ok",
            },
            "rate_mismatch_control": {
                "status": "warning",
                "check_from": "2026-01-01",
                "check_to_msk": "2026-03-20T09:00:00+03:00",
                "mismatch_count": 1,
                "total_diff_rub": "3000.00",
                "total_abs_diff_rub": "3000.00",
                "tolerance_rub": "1.00",
                "returned_count": 1,
                "items": [
                    {
                        "document_type": "Приходный кассовый ордер",
                        "document_ref": "0x8c36002590803daf11f0f8f20d13df97",
                        "document_number": "РБГУ0020374",
                        "document_at": "2026-01-24T10:56:31",
                        "line_number": 1,
                        "contract_name": "Основной договор (доллары США)",
                        "currency_name": "USD",
                        "document_amount": "5000.00",
                        "document_rate": "77.600000",
                        "document_multiplicity": "1.000000",
                        "expected_rub": "388000.00",
                        "movement_amount": "5000.00",
                        "movement_rub": "385000.00",
                        "diff_rub": "3000.00",
                    }
                ],
            },
        },
        ("/api/management/cash-position", "top=15"): {
            "status": "ready",
            "generated_at_msk": "2026-03-20T09:00:00+03:00",
            "summary_by_category_currency": [
                {
                    "category": "bank_accounts",
                    "category_name": "счета",
                    "currency_code": "643",
                    "currency_name": "руб",
                    "current_balance": "1500000.00",
                },
                {
                    "category": "cashboxes",
                    "category_name": "кассы",
                    "currency_code": "840",
                    "currency_name": "USD",
                    "current_balance": "1200.00",
                },
                {
                    "category": "cards",
                    "category_name": "карты/эквайринг",
                    "currency_code": "643",
                    "currency_name": "руб",
                    "current_balance": "300000.00",
                },
            ],
        },
        ("/api/management/retail-director-monthly-kpi", "month=2026-02"): {
            "freshness_status": "fresh",
            "source_status": "ready",
            "payload": {"month": "2026-02"},
        },
    }

    def fetch_json(path: str, params: dict[str, str]) -> dict:
        key = (path, "&".join(f"{name}={value}" for name, value in sorted(params.items())))
        return responses[key]

    digest = build_management_digest(
        fetch_json=fetch_json,
        anchor_date=date(2026, 3, 20),
        role_code="cfo",
    )
    rendered = render_management_digest(digest)

    assert digest["status"] == "ready"
    assert digest["sections"]["receivables"]["buyers_total"]["total_balance"] == pytest.approx(
        5609847.63
    )
    assert digest["sections"]["receivables"]["buyers_total"]["day_delta"] == pytest.approx(9847.63)
    assert digest["sections"]["receivables"]["buyers_total"]["week_delta"] == pytest.approx(
        59847.63
    )
    assert digest["sections"]["receivables"]["buyers_total"]["month_delta"] == pytest.approx(
        109847.63
    )
    assert digest["sections"]["sales"]["day"]["gross_profit"] == pytest.approx(581273.70)
    assert digest["sections"]["sales"]["day"]["profitability_pct"] == pytest.approx(0.22518871)
    assert digest["sections"]["receivables"]["new_daily_count"] == 2
    assert digest["sections"]["receivables"]["overdue_count"] == 2
    assert digest["sections"]["receivables"]["overdue_total_balance"] == pytest.approx(9000.0)
    assert digest["sections"]["receivables"]["unassigned_counterparty_count"] == 3
    assert digest["sections"]["receivables"]["unassigned_total_balance"] == pytest.approx(50000.0)
    assert digest["sections"]["staffing"]["critical_shift_count"] == 1
    assert digest["sections"]["task_payloads"]["total_count"] == 2
    assert digest["sections"]["task_payloads"]["as_of"] == "2026-03-20"
    assert digest["sections"]["task_efficiency"]["summary"]["employee_count"] == 2
    assert digest["sections"]["exchange_counterparty"]["control_status"] == "warning"
    assert digest["sections"]["cash_position"]["status"] == "ready"
    assert "Точка 1/AM" in rendered
    assert "receivable_employee=1" in rendered
    assert "Payload'ы задач (2026-03-20)" in rendered
    assert "Эффективность задач Bitrix (2026-03): средняя 75,0%" in rendered
    assert "По всем сотрудникам: Иван: 100,0%" in rendered
    assert "Петр: 50,0%" in rendered
    assert rendered.index("Эффективность задач Bitrix (2026-03):") < rendered.index("Staffing:")
    assert rendered.index("Продажи месяц:") < rendered.index(
        "Эффективность задач Bitrix (2026-03):"
    )
    assert (
        "Дебиторка покупателей: 5 609 848 ₽ (+9 848 ₽ д/д; +59 848 ₽ н/н; +109 848 ₽ м/м)."
    ) in rendered
    assert (
        "Детали дебиторки: новые долги 2 на 17 000 ₽; просрочка 2 на 9 000 ₽; "
        "inactive 1 на 30 000 ₽; без владельца 3 на 50 000 ₽; "
        "уволенный менеджер 1 на 7 000 ₽; сотрудники 1 на 4 000 ₽; "
        "кандидаты на корректировку 1 на 15 000 ₽."
    ) in rendered
    assert (
        "Продажи день: выручка 2 581 274 ₽ (+81 274 ₽ д/д); валовая прибыль 581 274 ₽ "
        "(+81 274 ₽ д/д); рентабельность продаж 22,5% (+2,5 п.п. д/д); "
        "продано 2 753 шт. (+53 д/д); ср. чек 938 ₽ (+12 ₽ д/д)."
    ) in rendered
    assert (
        "Продажи неделя: выручка 12 000 000 ₽ (+200 000 ₽ н/н); валовая прибыль 3 000 000 ₽ "
        "(+200 000 ₽ н/н); рентабельность продаж 25,0% (+1,3 п.п. н/н); "
        "продано 12 500 шт. (+100 н/н); ср. чек 960 ₽ (+8 ₽ н/н)."
    ) in rendered
    assert (
        "Продажи месяц: выручка 50 200 000 ₽ (+3 200 000 ₽ м/м); валовая прибыль 12 200 000 ₽ "
        "(+1 200 000 ₽ м/м); рентабельность продаж 24,3% (+0,9 п.п. м/м); "
        "продано 53 000 шт. (+3 000 м/м); ср. чек 947 ₽ (+7 ₽ м/м)."
    ) in rendered
    assert (
        "Обменник РБ002085: ВНИМАНИЕ; приход рублей 90 000,00 ₽; "
        "расход валюты в руб. эквиваленте 90 000,00 ₽; "
        "разница 0,00 ₽; рублевый хвост 0,00 ₽."
    ) in rendered
    assert (
        "Ошибки курса Обменник: 1 док. на 3 000,00 ₽; "
        "РБГУ0020374 24.01 USD: 5 000 USD x 77,6 -> 388 000,00 ₽, "
        "регистр 385 000,00 ₽, разница 3 000,00 ₽."
    ) in rendered
    assert (
        "Обменник остатки по валютам договора: -1 000 USD (экв. -90 000,00 ₽); " "1 000 000 руб."
    ) in rendered
    assert (
        "Остатки денег по 1С без смешивания валют: счета: 1 500 000 руб; "
        "кассы: 1 200 USD; карты/эквайринг: 300 000 руб."
    ) in rendered


def test_build_management_digest_marks_bare_bi_lists_ready() -> None:
    def fetch_json(path: str, params: dict[str, str]):
        if path == "/api/management/health":
            return {
                "status": "ok",
                "freshness_status": "fresh",
                "source_status": "ready",
                "components": [
                    {
                        "component": "receivables",
                        "freshness_status": "fresh",
                        "source_status": "ready",
                        "latest_snapshot_date": "2026-05-13",
                        "metrics": {
                            "latest_balance_snapshot_date": "2026-05-13",
                            "buyer_case_total_balance": "100.00",
                        },
                    },
                    {
                        "component": "task_payloads",
                        "freshness_status": "fresh",
                        "source_status": "ready",
                        "latest_snapshot_date": "2026-05-13",
                    },
                ],
            }
        if path == "/api/bi/receivables-contract-balances":
            return [{"current_balance": "100.00"}]
        if path == "/api/bi/receivables-current":
            return [{"current_balance": "100.00", "aged_bucket": "0-7"}]
        if path == "/api/bi/sales-daily-kpi":
            date_from = params.get("date_from")
            date_to = params.get("date_to")
            if date_from == "2026-05-13" and date_to == "2026-05-13":
                return [{"revenue": "1000.00", "sales_count": "2.000", "cost_of_sales": "700.00"}]
            if date_from == "2026-05-12" and date_to == "2026-05-12":
                return [{"revenue": "800.00", "sales_count": "1.000", "cost_of_sales": "500.00"}]
            if date_from == "2026-05-01" and date_to == "2026-05-13":
                return [{"revenue": "5000.00", "sales_count": "10.000", "cost_of_sales": "3000.00"}]
            return [{"revenue": "4000.00", "sales_count": "8.000", "cost_of_sales": "2600.00"}]
        if path == "/api/bi/sales-weekly-kpi":
            if params.get("date_from") == "2026-05-11":
                return [{"revenue": "2000.00", "sales_count": "4.000", "cost_of_sales": "1200.00"}]
            return [{"revenue": "1500.00", "sales_count": "3.000", "cost_of_sales": "900.00"}]
        return {"freshness_status": "fresh", "source_status": "ready", "payload": []}

    digest = build_management_digest(
        fetch_json=fetch_json,
        anchor_date=date(2026, 5, 13),
    )
    rendered = render_management_digest(digest)

    assert digest["freshness"]["sales_daily_current"] == {
        "freshness_status": "fresh",
        "source_status": "ready",
    }
    assert digest["freshness"]["buyers_balance_current"] == {
        "freshness_status": "fresh",
        "source_status": "ready",
    }
    assert digest["sections"]["sales"]["status"] == "ready"
    assert digest["sections"]["sales"]["row_counts"]["day"] == 1
    assert "Продажи день: выручка 1 000 ₽" in rendered


def test_build_management_digest_uses_latest_task_payload_date_when_anchor_is_empty() -> None:
    responses = {
        ("/api/management/health", "date=2026-03-30"): {
            "status": "degraded",
            "freshness_status": "stale",
            "source_status": "partial",
            "components": [
                {
                    "component": "receivables",
                    "freshness_status": "stale",
                    "source_status": "ready",
                    "latest_snapshot_date": "2026-03-29",
                },
                {
                    "component": "staffing",
                    "freshness_status": "fresh",
                    "source_status": "ready",
                    "latest_snapshot_date": "2026-03-30",
                },
                {
                    "component": "task_payloads",
                    "freshness_status": "fresh",
                    "source_status": "ready",
                    "latest_snapshot_date": "2026-03-29",
                },
            ],
        },
        ("/api/receivables/new-daily", "date=2026-03-30"): {
            "freshness_status": "fresh",
            "source_status": "ready",
            "payload": [],
        },
        ("/api/receivables/cases", "date=2026-03-30&segment=overdue"): {
            "freshness_status": "fresh",
            "source_status": "ready",
            "payload": [],
        },
        ("/api/receivables/cases", "date=2026-03-30&segment=inactive"): {
            "freshness_status": "fresh",
            "source_status": "ready",
            "payload": [],
        },
        ("/api/receivables/cases", "date=2026-03-30&segment=fired_manager"): {
            "freshness_status": "fresh",
            "source_status": "ready",
            "payload": [],
        },
        ("/api/receivables/cases", "date=2026-03-30&segment=adjustment_candidates"): {
            "freshness_status": "fresh",
            "source_status": "ready",
            "payload": [],
        },
        ("/api/receivables/employee-cases", "date=2026-03-30"): {
            "freshness_status": "fresh",
            "source_status": "ready",
            "payload": [],
        },
        ("/api/receivables/manager-summary", "date=2026-03-30"): {
            "freshness_status": "fresh",
            "source_status": "ready",
            "payload": [],
        },
        (
            "/api/bi/receivables-contract-balances",
            "buyers_rub_only=true&date=2026-03-30",
        ): [],
        ("/api/bi/receivables-current", "date=2026-03-30"): {"payload": []},
        (
            "/api/bi/receivables-contract-balances",
            "buyers_rub_only=true&date=2026-03-29",
        ): [],
        (
            "/api/bi/receivables-contract-balances",
            "buyers_rub_only=true&date=2026-03-23",
        ): [],
        (
            "/api/bi/receivables-contract-balances",
            "buyers_rub_only=true&date=2026-02-28",
        ): [],
        ("/api/bi/sales-daily-kpi", "date_from=2026-03-30&date_to=2026-03-30"): {
            "payload": [],
        },
        ("/api/bi/sales-daily-kpi", "date_from=2026-03-29&date_to=2026-03-29"): {
            "payload": [],
        },
        ("/api/bi/sales-weekly-kpi", "date_from=2026-03-30&date_to=2026-03-30"): {
            "payload": [],
        },
        ("/api/bi/sales-weekly-kpi", "date_from=2026-03-23&date_to=2026-03-23"): {
            "payload": [],
        },
        ("/api/bi/sales-daily-kpi", "date_from=2026-03-01&date_to=2026-03-30"): {
            "payload": [],
        },
        ("/api/bi/sales-daily-kpi", "date_from=2026-02-01&date_to=2026-03-02"): {
            "payload": [],
        },
        ("/api/staffing/daily", "date=2026-03-30"): {
            "freshness_status": "fresh",
            "source_status": "ready",
            "payload": [],
        },
        ("/api/management/task-payloads", "date=2026-03-30"): {
            "freshness_status": "missing",
            "source_status": "empty",
            "payload": [],
        },
        ("/api/management/task-payloads", "date=2026-03-29"): {
            "freshness_status": "fresh",
            "source_status": "ready",
            "payload": [
                {"rule_code": "receivable_employee"},
                {"rule_code": "staffing_shift_deficit"},
            ],
        },
        ("/api/management/task-efficiency", "month=2026-03"): _task_efficiency_response("2026-03"),
    }

    def fetch_json(path: str, params: dict[str, str]) -> dict:
        key = (path, "&".join(f"{name}={value}" for name, value in sorted(params.items())))
        return responses[key]

    digest = build_management_digest(fetch_json=fetch_json, anchor_date=date(2026, 3, 30))
    rendered = render_management_digest(digest)

    assert digest["sections"]["receivables"]["buyers_total"]["status"] == "snapshot_pending"
    assert digest["sections"]["receivables"]["buyers_total"]["latest_snapshot_date"] == "2026-03-29"
    assert digest["sections"]["receivables"]["buyers_total"]["total_balance"] is None
    assert digest["sections"]["receivables"]["buyers_total"]["day_delta"] is None
    assert digest["sections"]["receivables"]["buyers_total"]["week_delta"] is None
    assert digest["sections"]["receivables"]["buyers_total"]["month_delta"] is None
    assert digest["sections"]["task_payloads"]["total_count"] == 2
    assert digest["sections"]["task_payloads"]["as_of"] == "2026-03-29"
    assert (
        "Дебиторка покупателей: актуальный срез за 2026-03-30 ещё не готов; "
        "последний snapshot 2026-03-29."
    ) in rendered
    assert "Payload'ы задач (2026-03-29): всего 2" in rendered


def test_build_management_digest_marks_profit_metrics_unavailable_when_source_fields_missing() -> (
    None
):
    def fetch_json(path: str, params: dict[str, str]):
        if path == "/api/management/health":
            return {"status": "ok", "components": []}
        if path == "/api/bi/receivables-contract-balances":
            return []
        if path == "/api/bi/sales-daily-kpi":
            return {
                "payload": [
                    {
                        "revenue": "1000.00",
                        "sales_count": "2.000",
                    }
                ]
            }
        if path == "/api/bi/sales-weekly-kpi":
            return {
                "payload": [
                    {
                        "revenue": "5000.00",
                        "sales_count": "10.000",
                    }
                ]
            }
        return {"freshness_status": "fresh", "source_status": "ready", "payload": []}

    digest = build_management_digest(fetch_json=fetch_json, anchor_date=date(2026, 3, 20))
    rendered = render_management_digest(digest)

    assert digest["sections"]["sales"]["day"]["gross_profit"] is None
    assert digest["sections"]["sales"]["day"]["profitability_pct"] is None
    assert (
        "Продажи день: выручка 1 000 ₽ (+0 ₽ д/д); валовая прибыль н/д; рентабельность продаж н/д;"
        in rendered
    )
    assert (
        "Продажи неделя: выручка 5 000 ₽ (+0 ₽ н/н); валовая прибыль н/д; рентабельность продаж н/д;"
    ) in rendered


def test_build_management_digest_treats_empty_new_daily_and_staffing_as_non_blocking() -> None:
    def fetch_json(path: str, params: dict[str, str]):
        if path == "/api/management/health":
            return {
                "status": "degraded",
                "freshness_status": "missing",
                "source_status": "empty",
                "components": [
                    {
                        "component": "receivables",
                        "freshness_status": "fresh",
                        "source_status": "ready",
                        "latest_snapshot_date": "2026-04-23",
                        "metrics": {"latest_balance_snapshot_date": "2026-04-23"},
                    },
                    {
                        "component": "staffing",
                        "freshness_status": "missing",
                        "source_status": "empty",
                    },
                    {
                        "component": "task_payloads",
                        "freshness_status": "fresh",
                        "source_status": "partial",
                        "latest_snapshot_date": "2026-04-23",
                        "metrics": {"task_payload_count": 1},
                    },
                ],
            }
        if path == "/api/receivables/new-daily":
            return {"freshness_status": "missing", "source_status": "empty", "payload": []}
        if path == "/api/bi/receivables-contract-balances":
            if params.get("date") == "2026-04-23":
                return [{"current_balance": "6129175.28"}]
            return [{"current_balance": "6000000.00"}]
        if path == "/api/bi/receivables-current":
            return {
                "payload": [
                    {
                        "current_balance": "6129175.28",
                        "aged_bucket": "0-7",
                    }
                ]
            }
        if path == "/api/management/task-payloads":
            return {
                "freshness_status": "fresh",
                "source_status": "ready",
                "payload": [{"rule_code": "receivable_employee"}],
            }
        if path == "/api/management/task-efficiency":
            return _task_efficiency_response("2026-04")
        if path == "/api/staffing/daily":
            return {"freshness_status": "missing", "source_status": "empty", "payload": []}
        return {"freshness_status": "fresh", "source_status": "ready", "payload": []}

    digest = build_management_digest(fetch_json=fetch_json, anchor_date=date(2026, 4, 23))

    assert digest["status"] == "ready"
    assert digest["sections"]["receivables"]["buyers_total"]["status"] == "ready"
    assert digest["sections"]["receivables"]["new_daily_count"] == 0


def test_build_management_digest_degrades_when_bi_and_case_buyers_totals_diverge() -> None:
    def fetch_json(path: str, params: dict[str, str]):
        if path == "/api/management/health":
            return {
                "status": "ok",
                "freshness_status": "fresh",
                "source_status": "ready",
                "components": [
                    {
                        "component": "receivables",
                        "freshness_status": "fresh",
                        "source_status": "ready",
                        "latest_snapshot_date": "2026-04-23",
                        "metrics": {
                            "latest_balance_snapshot_date": "2026-04-23",
                            "buyer_case_total_balance": "100.00",
                        },
                    },
                    {
                        "component": "staffing",
                        "freshness_status": "fresh",
                        "source_status": "ready",
                    },
                    {
                        "component": "task_payloads",
                        "freshness_status": "fresh",
                        "source_status": "ready",
                        "latest_snapshot_date": "2026-04-23",
                    },
                ],
            }
        if path == "/api/bi/receivables-contract-balances":
            if params.get("date") == "2026-04-23":
                return [{"current_balance": "200.00"}]
            return [{"current_balance": "100.00"}]
        if path == "/api/bi/receivables-current":
            return {"payload": [{"current_balance": "200.00", "aged_bucket": "0-7"}]}
        if path == "/api/management/task-efficiency":
            return _task_efficiency_response("2026-04")
        return {"freshness_status": "fresh", "source_status": "ready", "payload": []}

    digest = build_management_digest(fetch_json=fetch_json, anchor_date=date(2026, 4, 23))
    rendered = render_management_digest(digest)

    buyers_total = digest["sections"]["receivables"]["buyers_total"]
    assert digest["status"] == "degraded"
    assert buyers_total["status"] == "degraded"
    assert buyers_total["total_balance"] is None
    assert "buyers-срезы расходятся" in buyers_total["note"]
    assert "Дебиторка покупателей: buyers-срезы расходятся" in rendered


def test_build_management_digest_degrades_contradictory_buyers_snapshot() -> None:
    responses = {
        ("/api/management/health", "date=2026-04-19"): {
            "status": "ok",
            "freshness_status": "fresh",
            "source_status": "ready",
            "components": [
                {
                    "component": "receivables",
                    "freshness_status": "fresh",
                    "source_status": "ready",
                    "latest_snapshot_date": "2026-04-19",
                    "metrics": {
                        "latest_balance_snapshot_date": "2026-04-19",
                    },
                }
            ],
        },
        ("/api/receivables/new-daily", "date=2026-04-19"): {
            "freshness_status": "fresh",
            "source_status": "ready",
            "payload": [],
        },
        ("/api/receivables/cases", "date=2026-04-19&segment=overdue"): {
            "freshness_status": "fresh",
            "source_status": "ready",
            "payload": [],
        },
        ("/api/receivables/cases", "date=2026-04-19&segment=inactive"): {
            "freshness_status": "fresh",
            "source_status": "ready",
            "payload": [],
        },
        ("/api/receivables/cases", "date=2026-04-19&segment=fired_manager"): {
            "freshness_status": "fresh",
            "source_status": "ready",
            "payload": [],
        },
        ("/api/receivables/cases", "date=2026-04-19&segment=adjustment_candidates"): {
            "freshness_status": "fresh",
            "source_status": "ready",
            "payload": [],
        },
        ("/api/receivables/employee-cases", "date=2026-04-19"): {
            "freshness_status": "fresh",
            "source_status": "ready",
            "payload": [],
        },
        ("/api/receivables/manager-summary", "date=2026-04-19"): {
            "freshness_status": "fresh",
            "source_status": "ready",
            "payload": [],
        },
        (
            "/api/bi/receivables-contract-balances",
            "buyers_rub_only=true&date=2026-04-19",
        ): [
            {"current_balance": "85290609.38"},
        ],
        ("/api/bi/receivables-current", "date=2026-04-19"): {
            "payload": [
                {
                    "current_balance": "80144343.41",
                    "aged_bucket": "unknown",
                },
                {
                    "current_balance": "5146265.97",
                    "aged_bucket": "8-30",
                },
                {
                    "current_balance": "-141898037.79",
                    "aged_bucket": "unknown",
                },
            ]
        },
        (
            "/api/bi/receivables-contract-balances",
            "buyers_rub_only=true&date=2026-04-18",
        ): [
            {"current_balance": "85290609.38"},
        ],
        (
            "/api/bi/receivables-contract-balances",
            "buyers_rub_only=true&date=2026-04-12",
        ): [
            {"current_balance": "83339033.04"},
        ],
        (
            "/api/bi/receivables-contract-balances",
            "buyers_rub_only=true&date=2026-03-19",
        ): [
            {"current_balance": "89867632.84"},
        ],
        ("/api/bi/sales-daily-kpi", "date_from=2026-04-19&date_to=2026-04-19"): {"payload": []},
        ("/api/bi/sales-daily-kpi", "date_from=2026-04-18&date_to=2026-04-18"): {"payload": []},
        ("/api/bi/sales-weekly-kpi", "date_from=2026-04-13&date_to=2026-04-19"): {"payload": []},
        ("/api/bi/sales-weekly-kpi", "date_from=2026-04-06&date_to=2026-04-12"): {"payload": []},
        ("/api/bi/sales-daily-kpi", "date_from=2026-04-01&date_to=2026-04-19"): {"payload": []},
        ("/api/bi/sales-daily-kpi", "date_from=2026-03-01&date_to=2026-03-19"): {"payload": []},
        ("/api/staffing/daily", "date=2026-04-19"): {
            "freshness_status": "fresh",
            "source_status": "ready",
            "payload": [],
        },
        ("/api/management/task-payloads", "date=2026-04-19"): {
            "freshness_status": "fresh",
            "source_status": "ready",
            "payload": [],
        },
        ("/api/management/task-efficiency", "month=2026-04"): _task_efficiency_response("2026-04"),
    }

    def fetch_json(path: str, params: dict[str, str]) -> dict:
        key = (path, "&".join(f"{name}={value}" for name, value in sorted(params.items())))
        return responses[key]

    digest = build_management_digest(fetch_json=fetch_json, anchor_date=date(2026, 4, 19))
    rendered = render_management_digest(digest)

    buyers_total = digest["sections"]["receivables"]["buyers_total"]
    assert digest["status"] == "degraded"
    assert buyers_total["status"] == "degraded"
    assert buyers_total["total_balance"] is None
    assert buyers_total["day_delta"] is None
    assert buyers_total["week_delta"] is None
    assert buyers_total["month_delta"] is None
    assert buyers_total["snapshot_signed_total"] == pytest.approx(-56607428.41)
    assert buyers_total["snapshot_unknown_positive_share"] == pytest.approx(0.9396619861)
    assert "signed-остаток отрицательный" in buyers_total["note"]
    assert (
        "Дебиторка покупателей: текущий buyers-срез противоречив: signed-остаток "
        "отрицательный, 94% положительной суммы сидит в unknown-bucket; до exact-сверки "
        "1С на этот блок опираться нельзя."
    ) in rendered


def test_build_management_digest_degrades_without_failing() -> None:
    def fetch_json(path: str, params: dict[str, str]) -> dict:
        if path == "/api/management/health":
            raise RuntimeError("HTTP 404: Not Found")
        return {"freshness_status": "missing", "source_status": "empty", "payload": []}

    digest = build_management_digest(fetch_json=fetch_json, anchor_date=date(2026, 3, 20))
    rendered = render_management_digest(digest)

    assert digest["status"] == "degraded"
    assert digest["errors"]
    assert "Деградация:" in rendered
    assert "HTTP 404: Not Found" in rendered
    assert "Payload'ы задач: пусто или источник недоступен." in rendered


def test_build_management_digest_includes_retail_director_monthly_kpi_for_exec_roles() -> None:
    responses = {
        ("/api/management/health", "date=2026-04-17"): {
            "status": "ok",
            "freshness_status": "fresh",
            "source_status": "ready",
            "components": [],
        },
        ("/api/receivables/new-daily", "date=2026-04-17"): {
            "freshness_status": "fresh",
            "source_status": "ready",
            "payload": [],
        },
        ("/api/receivables/cases", "date=2026-04-17&segment=overdue"): {
            "freshness_status": "fresh",
            "source_status": "ready",
            "payload": [],
        },
        ("/api/receivables/cases", "date=2026-04-17&segment=inactive"): {
            "freshness_status": "fresh",
            "source_status": "ready",
            "payload": [],
        },
        ("/api/receivables/cases", "date=2026-04-17&segment=fired_manager"): {
            "freshness_status": "fresh",
            "source_status": "ready",
            "payload": [],
        },
        ("/api/receivables/cases", "date=2026-04-17&segment=adjustment_candidates"): {
            "freshness_status": "fresh",
            "source_status": "ready",
            "payload": [],
        },
        ("/api/receivables/employee-cases", "date=2026-04-17"): {
            "freshness_status": "fresh",
            "source_status": "ready",
            "payload": [],
        },
        ("/api/receivables/manager-summary", "date=2026-04-17"): {
            "freshness_status": "fresh",
            "source_status": "ready",
            "payload": [],
        },
        ("/api/bi/receivables-current", "date=2026-04-17"): {"payload": []},
        ("/api/bi/receivables-contract-balances", "buyers_rub_only=true&date=2026-04-17"): [],
        ("/api/bi/receivables-contract-balances", "buyers_rub_only=true&date=2026-04-16"): [],
        ("/api/bi/receivables-contract-balances", "buyers_rub_only=true&date=2026-04-10"): [],
        ("/api/bi/receivables-contract-balances", "buyers_rub_only=true&date=2026-03-17"): [],
        ("/api/bi/sales-daily-kpi", "date_from=2026-04-17&date_to=2026-04-17"): {"payload": []},
        ("/api/bi/sales-daily-kpi", "date_from=2026-04-16&date_to=2026-04-16"): {"payload": []},
        ("/api/bi/sales-weekly-kpi", "date_from=2026-04-14&date_to=2026-04-17"): {"payload": []},
        ("/api/bi/sales-weekly-kpi", "date_from=2026-04-07&date_to=2026-04-10"): {"payload": []},
        ("/api/bi/sales-daily-kpi", "date_from=2026-04-01&date_to=2026-04-17"): {"payload": []},
        ("/api/bi/sales-daily-kpi", "date_from=2026-03-01&date_to=2026-03-17"): {"payload": []},
        ("/api/staffing/daily", "date=2026-04-17"): {
            "freshness_status": "fresh",
            "source_status": "ready",
            "payload": [],
        },
        ("/api/management/task-payloads", "date=2026-04-17"): {
            "freshness_status": "fresh",
            "source_status": "ready",
            "payload": [],
        },
        ("/api/management/task-efficiency", "month=2026-04"): _task_efficiency_response("2026-04"),
        ("/api/management/retail-director-monthly-kpi", "month=2026-03"): {
            "freshness_status": "fresh",
            "source_status": "ready",
            "payload": {
                "month": "2026-03",
                "writeoff_amount": 1229121.82,
                "receipt_amount": 526672.97,
                "shrinkage_amount": 702448.85,
                "shrinkage_pct": 0.8499,
                "kpi_index_sum": 0.7214,
                "kpi_bonus_amount": 54105.0,
                "to_pay": 234105.0,
            },
        },
    }

    def fetch_json(path: str, params: dict[str, str]) -> dict:
        key = (path, "&".join(f"{name}={value}" for name, value in sorted(params.items())))
        return responses[key]

    digest = build_management_digest(
        fetch_json=fetch_json,
        anchor_date=date(2026, 4, 17),
        role_code="ceo",
    )
    rendered = render_management_digest(digest)

    monthly_kpi = digest["sections"]["retail_director_monthly_kpi"]
    assert monthly_kpi["status"] == "ready"
    assert monthly_kpi["month"] == "2026-03"
    assert monthly_kpi["payload"]["shrinkage_amount"] == 702448.85
    assert (
        "Розница, закрытый месяц 2026-03: списания 1 229 122 ₽; оприходования 526 673 ₽; "
        "чистые потери 702 449 ₽; уровень потерь 0,8499%."
    ) in rendered
    assert (
        "Премия retail_director: индекс KPI 0.7214; бонус 54 105 ₽; к выплате 234 105 ₽."
    ) in rendered


def test_build_management_digest_includes_retail_director_monthly_kpi_for_retail_network_head() -> (
    None
):
    responses = {
        ("/api/management/health", "date=2026-04-17"): {
            "status": "ok",
            "freshness_status": "fresh",
            "source_status": "ready",
            "components": [],
        },
        ("/api/receivables/new-daily", "date=2026-04-17"): {
            "freshness_status": "fresh",
            "source_status": "ready",
            "payload": [],
        },
        ("/api/receivables/cases", "date=2026-04-17&segment=overdue"): {
            "freshness_status": "fresh",
            "source_status": "ready",
            "payload": [],
        },
        ("/api/receivables/cases", "date=2026-04-17&segment=inactive"): {
            "freshness_status": "fresh",
            "source_status": "ready",
            "payload": [],
        },
        ("/api/receivables/cases", "date=2026-04-17&segment=fired_manager"): {
            "freshness_status": "fresh",
            "source_status": "ready",
            "payload": [],
        },
        ("/api/receivables/cases", "date=2026-04-17&segment=adjustment_candidates"): {
            "freshness_status": "fresh",
            "source_status": "ready",
            "payload": [],
        },
        ("/api/receivables/employee-cases", "date=2026-04-17"): {
            "freshness_status": "fresh",
            "source_status": "ready",
            "payload": [],
        },
        ("/api/receivables/manager-summary", "date=2026-04-17"): {
            "freshness_status": "fresh",
            "source_status": "ready",
            "payload": [],
        },
        ("/api/bi/receivables-current", "date=2026-04-17"): {"payload": []},
        ("/api/bi/receivables-contract-balances", "buyers_rub_only=true&date=2026-04-17"): [],
        ("/api/bi/receivables-contract-balances", "buyers_rub_only=true&date=2026-04-16"): [],
        ("/api/bi/receivables-contract-balances", "buyers_rub_only=true&date=2026-04-10"): [],
        ("/api/bi/receivables-contract-balances", "buyers_rub_only=true&date=2026-03-17"): [],
        ("/api/bi/sales-daily-kpi", "date_from=2026-04-17&date_to=2026-04-17"): {"payload": []},
        ("/api/bi/sales-daily-kpi", "date_from=2026-04-16&date_to=2026-04-16"): {"payload": []},
        ("/api/bi/sales-weekly-kpi", "date_from=2026-04-14&date_to=2026-04-17"): {"payload": []},
        ("/api/bi/sales-weekly-kpi", "date_from=2026-04-07&date_to=2026-04-10"): {"payload": []},
        ("/api/bi/sales-daily-kpi", "date_from=2026-04-01&date_to=2026-04-17"): {"payload": []},
        ("/api/bi/sales-daily-kpi", "date_from=2026-03-01&date_to=2026-03-17"): {"payload": []},
        ("/api/staffing/daily", "date=2026-04-17"): {
            "freshness_status": "fresh",
            "source_status": "ready",
            "payload": [],
        },
        ("/api/management/task-payloads", "date=2026-04-17"): {
            "freshness_status": "fresh",
            "source_status": "ready",
            "payload": [],
        },
        ("/api/management/task-efficiency", "month=2026-04"): _task_efficiency_response("2026-04"),
        ("/api/management/retail-director-monthly-kpi", "month=2026-03"): {
            "freshness_status": "fresh",
            "source_status": "ready",
            "payload": {
                "month": "2026-03",
                "writeoff_amount": 1229121.82,
                "receipt_amount": 526672.97,
                "shrinkage_amount": 702448.85,
                "shrinkage_pct": 0.8499,
                "kpi_index_sum": 0.7214,
                "kpi_bonus_amount": 54105.0,
                "to_pay": 234105.0,
            },
        },
    }

    def fetch_json(path: str, params: dict[str, str]) -> dict:
        key = (path, "&".join(f"{name}={value}" for name, value in sorted(params.items())))
        return responses[key]

    digest = build_management_digest(
        fetch_json=fetch_json,
        anchor_date=date(2026, 4, 17),
        role_code="retail_network_head",
    )

    monthly_kpi = digest["sections"]["retail_director_monthly_kpi"]
    assert monthly_kpi["status"] == "ready"
    assert monthly_kpi["payload"]["shrinkage_amount"] == 702448.85


def test_build_management_digest_includes_open_month_block_for_retail_network_head() -> None:
    def fetch_json(path: str, params: dict[str, str]) -> dict:
        if path == "/api/management/health":
            return {
                "status": "ok",
                "freshness_status": "fresh",
                "source_status": "ready",
                "components": [],
            }
        if path == "/api/bi/receivables-contract-balances":
            return []
        if path == "/api/bi/sales-daily-kpi":
            date_from = params.get("date_from")
            date_to = params.get("date_to")
            if date_from == "2026-04-01" and date_to == "2026-04-17":
                return {
                    "payload": [
                        {
                            "revenue": "10000000.00",
                            "sales_count": "1000.000",
                            "cost_of_sales": "7000000.00",
                        }
                    ]
                }
            if date_from == "2026-03-01" and date_to == "2026-03-17":
                return {
                    "payload": [
                        {
                            "revenue": "9500000.00",
                            "sales_count": "950.000",
                            "cost_of_sales": "6800000.00",
                        }
                    ]
                }
            return {"payload": []}
        if path == "/api/bi/sales-weekly-kpi":
            return {"payload": []}
        if path == "/api/management/retail-director-monthly-kpi":
            return {
                "freshness_status": "fresh",
                "source_status": "ready",
                "payload": {
                    "month": "2026-03",
                    "writeoff_amount": 1229121.82,
                    "receipt_amount": 526672.97,
                    "shrinkage_amount": 702448.85,
                    "shrinkage_pct": 0.8499,
                    "kpi_index_sum": 0.7214,
                    "kpi_bonus_amount": 54105.0,
                    "to_pay": 234105.0,
                },
            }
        return {"freshness_status": "fresh", "source_status": "ready", "payload": []}

    digest = build_management_digest(
        fetch_json=fetch_json,
        anchor_date=date(2026, 4, 17),
        role_code="retail_network_head",
    )
    rendered = render_management_digest(digest)

    open_month = digest["sections"]["retail_director_open_month"]
    assert open_month["month"] == "2026-04"
    assert open_month["revenue"] == pytest.approx(10000000.0)
    assert open_month["gross_profit"] == pytest.approx(3000000.0)
    assert open_month["revenue_delta"] == pytest.approx(500000.0)
    assert (
        "Розница, открытый месяц 2026-04 (2026-04-01..2026-04-17 vs 2026-03-01..2026-03-17): "
        "выручка 10 000 000 ₽ (+500 000 ₽); валовая прибыль 3 000 000 ₽ (+300 000 ₽ к тому же периоду); "
        "рентабельность продаж 30,0% (+1,6 п.п. к тому же периоду); продано 1 000 шт. (+50); "
        "ср. чек 10 000 ₽ (+0 ₽)."
    ) in rendered
