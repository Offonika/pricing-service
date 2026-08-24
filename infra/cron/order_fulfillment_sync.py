#!/usr/bin/env python3
"""Cron entrypoint for site order fulfillment dry-run synchronization.

The job is intentionally conservative: it writes review/outbox/apply-result
artifacts and only mutates Bitrix when explicitly started with --apply.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import subprocess
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.config import get_settings  # noqa: E402
from app.infrastructure.db.engines import build_engine  # noqa: E402
from app.services import site_order_fulfillment as fulfillment  # noqa: E402

DEFAULT_ENV_FILES = (REPO_ROOT / ".env", Path("/etc/mm-management-orchestrator.env"))
QUICK_STAGE_IDS = (
    "NEW",
    "PREPARATION",
    "PREPAYMENT_INVOICE",
    "EXECUTING",
    "PICKUP_WAITING",
    "DELIVERY_REVIEW",
)
PREPAYMENT_WAITING_MAX_AGE_DAYS = 7
PROCESS_STAGES = (
    "NEW",
    "PREPARATION",
    "PREPAYMENT_INVOICE",
    "EXECUTING",
    "FINAL_INVOICE",
    "IN_DELIVERY",
    "PICKUP_WAITING",
    "PICKUP_STORAGE",
    "DELIVERY_REVIEW",
    "DISMANTLING",
    "WON",
    "LOSE",
    "APOLOGY",
)
NEW_REVIEW_FIELDS = (
    "site_order_number",
    "bitrix_deal_id",
    "crm_stage",
    "crm_delivery",
    "crm_payment_status",
    "order_canceled",
    "assembled",
    "onec_sale_amount",
    "onec_payment_amount",
    "onec_debt_amount",
    "onec_payment_confirmed",
    "recommended_stage",
    "action",
    "review_reason",
)
CRM_ASSEMBLED_FIELD = "UF_CRM_MM_1C_ASSEMBLED"
READY_APPLY_RESULTS = {"dry_run_ready", "ready"}
TECHNICAL_APPLY_RESULTS = {
    "technical_review",
    "update_error",
    "live_lookup_error",
}
TECHNICAL_INPUT_STATES = {"blocked_missing_target_stage"}
NOTIFICATION_STATE_KEY_LIMIT = 10000
MONITORED_QUEUE_STAGES = ("NEW", "PREPARATION", "DELIVERY_REVIEW")
STAGE_RU_LABELS = {
    "NEW": "Новые",
    "PREPARATION": "Проверка заказа",
    "PREPAYMENT_INVOICE": "Ожидает оплаты",
    "EXECUTING": "Сборка / обеспечение",
    "FINAL_INVOICE": "Готов к отгрузке",
    "PICKUP_WAITING": "Ожидает самовывоза",
    "PICKUP_STORAGE": "Хранение в ПВЗ / отделении",
    "IN_DELIVERY": "Передан в доставку",
    "DELIVERY_REVIEW": "Проблема доставки",
    "DISMANTLING": "Расформирование / отмена",
    "WON": "Сделка успешна",
    "LOSE": "Сделка неуспешна",
    "APOLOGY": "Извинение",
}
OPERATIONAL_ALERT_LABELS = {
    "stage_count": "зависшие заказы в очередях",
    "overdue_prepayment": "ожидание оплаты больше 7 дней",
    "outbox_error": "CRM не приняла автоматическое обновление",
    "rtu_without_assembled": "есть реализация в 1С, но нет события «Собран»",
    "pickup_waiting_close_candidate": "самовывоз можно проверить на закрытие",
}
OPERATIONAL_ALERT_ACTIONS = {
    "stage_count": "Проверить очередь и перевести заказ дальше или закрыть вручную.",
    "overdue_prepayment": "Связаться с клиентом или закрыть как неуспешную сделку.",
    "outbox_error": "Проверить товары, остатки, отгрузки и ошибку Bitrix.",
    "rtu_without_assembled": "Проверить оформление в 1С и добить событие «Собран», если заказ действительно готов.",
    "pickup_waiting_close_candidate": "Проверить оплату/долг в 1С и закрыть в успех, если заказ действительно выдан.",
}
MANUAL_REVIEW_LABELS = {
    "bitrix_deal_not_found": "для заказа не найдена сделка в Bitrix24",
    "terminal_crm_stage": "сделка уже находится в финальной стадии",
    "pickup_received_without_payment_confirmation": "выдача указана, но оплата не подтверждена",
    "manual_review": "недостаточно данных для автоматического решения",
}
MANUAL_REVIEW_ACTIONS = {
    "bitrix_deal_not_found": "Найти или создать сделку и связать её с заказом.",
    "terminal_crm_stage": "Проверить, нужно ли исправлять финальную стадию; автоматически она не меняется.",
    "pickup_received_without_payment_confirmation": "Сверить оплату и долг в 1С перед закрытием сделки.",
    "manual_review": "Открыть заказ и определить следующий шаг вручную.",
}
MONITORING_CSV_FIELDS = (
    "key",
    "severity",
    "alert_type",
    "site_order_number",
    "bitrix_deal_id",
    "stage_id",
    "count",
    "reason",
    "artifact",
)
RTU_SIGNAL_CSV_FIELDS = (
    "site_order_number",
    "bitrix_deal_id",
    "crm_stage",
    "rtu_count",
    "latest_rtu_number",
    "latest_rtu_date",
    "assembled_rtu_count",
)
DEFAULT_ONEC_ASSEMBLY_CRM_STATE_PATH = REPO_ROOT / ".local/onec_assembly_crm_reconciler.sqlite3"
BITRIX_READ_RETRY_DELAYS = (0.5, 1.5)


@dataclass(slots=True)
class NotifyConfig:
    enabled: bool
    business_user_ids: list[int]
    tech_user_ids: list[int]
    method: str
    site_dialog_id: str | None
    site_dialog_method: str
    state_path: Path


@dataclass(slots=True)
class NewDealDecision:
    site_order_number: str
    bitrix_deal_id: int
    crm_stage: str | None
    crm_delivery: str | None
    crm_payment_status: str | None
    order_canceled: bool | None
    assembled: bool | None
    onec_sale_amount: Decimal | None
    onec_payment_amount: Decimal | None
    onec_debt_amount: Decimal | None
    onec_payment_confirmed: bool | None
    recommended_stage: str | None
    action: str
    review_reason: str


@dataclass(slots=True)
class SaleOrderStatus:
    order_number: str
    canceled: bool
    status_id: str | None
    payed: bool | None
    created_at: datetime | None = None


@dataclass(slots=True)
class OneCOrderSettlement:
    order_number: str
    posted_sale_count: int
    posted_sale_amount: Decimal | None
    payment_amount: Decimal | None
    debt_amount: Decimal | None
    payment_confirmed: bool
    evidence: str


def load_env_files(paths: tuple[Path, ...] = DEFAULT_ENV_FILES) -> dict[str, str]:
    values: dict[str, str] = {}
    for path in paths:
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def apply_env_defaults(values: dict[str, str]) -> None:
    for key, value in values.items():
        os.environ.setdefault(key, value)


def resolve_bitrix_webhook_url() -> str | None:
    settings = get_settings()
    return (
        settings.order_fulfillment_bitrix_webhook_url
        or os.environ.get("ORDER_FULFILLMENT_BITRIX_WEBHOOK_URL")
        or os.environ.get("BITRIX_BOX_WEBHOOK_BASE")
    )


def decide_new_deal_stage(
    deal: fulfillment.BitrixDealSnapshot,
    *,
    order_status: SaleOrderStatus | None = None,
    onec_settlement: OneCOrderSettlement | None = None,
) -> NewDealDecision:
    order_number = fulfillment._clean_string(  # noqa: SLF001 - cron reuses service normalizer.
        (deal.raw or {}).get(fulfillment.CRM_ORDER_NUMBER_FIELD)
    )
    stage = fulfillment._clean_string(deal.stage_id)  # noqa: SLF001
    delivery = fulfillment._clean_string(deal.delivery)  # noqa: SLF001
    payment_status = fulfillment._clean_string(deal.payment_status)  # noqa: SLF001
    assembled = _truthy_bitrix_value((deal.raw or {}).get(CRM_ASSEMBLED_FIELD))

    if not order_number:
        return _new_decision(deal, None, "missing_order_number")
    if order_status is not None and order_status.canceled:
        if assembled:
            return _new_decision(
                deal,
                "DISMANTLING",
                "canceled_assembled_to_dismantling",
                order_status=order_status,
            )
        return _new_decision(
            deal,
            "LOSE",
            "canceled_unassembled_to_lost",
            order_status=order_status,
        )
    if stage == "PREPARATION":
        if payment_status == "1":
            return _new_decision(deal, "EXECUTING", "preparation_paid_to_assembly")
        if _is_pickup_delivery(delivery):
            return _new_decision(deal, "EXECUTING", "preparation_pickup_to_assembly")
        if _is_courier_delivery(delivery):
            return _new_decision(deal, "EXECUTING", "preparation_courier_cod_to_assembly")
        return _new_decision(deal, None, f"preparation_waiting:{delivery or '-'}")
    if stage == "PREPAYMENT_INVOICE":
        if (
            order_status is not None
            and order_status.status_id == "F"
            and onec_settlement is not None
            and onec_settlement.posted_sale_count > 0
        ):
            return _new_decision(
                deal,
                None,
                "historical_completed_with_rtu_needs_delivery_check",
                order_status=order_status,
                onec_settlement=onec_settlement,
            )
        if order_status is not None and order_status.payed:
            return _new_decision(
                deal,
                "EXECUTING",
                "prepayment_paid_to_assembly",
                order_status=order_status,
            )
        if _is_prepayment_waiting_expired(order_status):
            if onec_settlement is None or onec_settlement.posted_sale_count <= 0:
                return _new_decision(
                    deal,
                    None,
                    "prepayment_unpaid_unconfirmed_in_onec",
                    order_status=order_status,
                    onec_settlement=onec_settlement,
                )
            return _new_decision(
                deal,
                "LOSE",
                "prepayment_unpaid_expired_to_lost",
                order_status=order_status,
            )
        return _new_decision(deal, None, "prepayment_waiting_payment")
    if stage == "PICKUP_WAITING":
        return _decide_pickup_waiting_stage(
            deal,
            order_status=order_status,
            onec_settlement=onec_settlement,
            delivery=delivery,
            payment_status=payment_status,
        )
    if stage == "DELIVERY_REVIEW":
        return _decide_delivery_review_stage(
            deal,
            order_status=order_status,
            delivery=delivery,
            assembled=assembled,
        )
    if stage != "NEW":
        return _new_decision(deal, None, f"not_new_stage:{stage or '-'}")
    if payment_status == "1":
        return _new_decision(deal, "EXECUTING", "paid_order_to_assembly")
    if _is_pickup_delivery(delivery):
        return _new_decision(deal, "EXECUTING", "pickup_to_assembly")
    if _is_courier_delivery(delivery):
        return _new_decision(deal, "EXECUTING", "courier_cod_to_assembly")
    if _is_prepayment_delivery(delivery):
        return _new_decision(deal, "PREPAYMENT_INVOICE", "carrier_unpaid_to_payment_waiting")
    if not delivery:
        return _new_decision(deal, None, "missing_crm_delivery")
    return _new_decision(deal, None, f"unknown_crm_delivery:{delivery}")


def _decide_delivery_review_stage(
    deal: fulfillment.BitrixDealSnapshot,
    *,
    order_status: SaleOrderStatus | None,
    delivery: str,
    assembled: bool,
) -> NewDealDecision:
    if order_status is not None and order_status.status_id == "F":
        return _new_decision(
            deal,
            None,
            "delivery_review_completed_needs_handoff_confirmation",
            order_status=order_status,
        )
    if order_status is not None and order_status.status_id == "P" and order_status.payed:
        if not assembled:
            return _new_decision(
                deal,
                "EXECUTING",
                "delivery_review_paid_to_assembly",
                order_status=order_status,
            )
        if _is_pickup_delivery(delivery):
            return _new_decision(
                deal,
                "FINAL_INVOICE",
                "delivery_review_paid_pickup_ready_for_dispatch",
                order_status=order_status,
            )
        return _new_decision(
            deal,
            "FINAL_INVOICE",
            "delivery_review_paid_carrier_assembled",
            order_status=order_status,
        )
    if order_status is not None and order_status.status_id == "N" and order_status.payed is False:
        if _is_pickup_delivery(delivery) and assembled:
            return _new_decision(
                deal,
                "FINAL_INVOICE",
                "delivery_review_pickup_ready_for_dispatch",
                order_status=order_status,
            )
        if _is_prepayment_waiting_expired(order_status):
            return _new_decision(
                deal,
                "LOSE",
                "delivery_review_unpaid_expired_to_lost",
                order_status=order_status,
            )
        if _is_courier_delivery(delivery):
            return _new_decision(
                deal,
                "EXECUTING",
                "delivery_review_courier_cod_to_assembly",
                order_status=order_status,
            )
        if _is_prepayment_delivery(delivery):
            return _new_decision(
                deal,
                "PREPAYMENT_INVOICE",
                "delivery_review_carrier_unpaid_to_payment_waiting",
                order_status=order_status,
            )
    return _new_decision(
        deal,
        None,
        f"delivery_review_waiting:{delivery or '-'}",
        order_status=order_status,
    )


def _decide_pickup_waiting_stage(
    deal: fulfillment.BitrixDealSnapshot,
    *,
    order_status: SaleOrderStatus | None,
    onec_settlement: OneCOrderSettlement | None,
    delivery: str,
    payment_status: str,
) -> NewDealDecision:
    if not _is_pickup_delivery(delivery):
        return _new_decision(
            deal,
            None,
            f"pickup_waiting_delivery_check:{delivery or '-'}",
            order_status=order_status,
        )
    if order_status is None:
        return _new_decision(deal, None, "pickup_waiting_no_site_status")
    if order_status.status_id != "F":
        return _new_decision(
            deal,
            None,
            f"pickup_waiting_client_waiting:{order_status.status_id or '-'}",
            order_status=order_status,
        )
    return _new_decision(
        deal,
        None,
        "pickup_waiting_completed_needs_handoff_confirmation",
        order_status=order_status,
        onec_settlement=onec_settlement,
    )


def _is_pickup_delivery(delivery: str) -> bool:
    return fulfillment.classify_delivery_method(delivery) == fulfillment.DELIVERY_CLASS_PICKUP


def _is_courier_delivery(delivery: str) -> bool:
    return fulfillment.classify_delivery_method(delivery) == fulfillment.DELIVERY_CLASS_COURIER


def _is_prepayment_delivery(delivery: str) -> bool:
    return fulfillment.classify_delivery_method(delivery) == fulfillment.DELIVERY_CLASS_CARRIER


def _is_prepayment_waiting_expired(order_status: SaleOrderStatus | None) -> bool:
    if order_status is None or order_status.created_at is None:
        return False
    if order_status.payed is not False:
        return False
    return datetime.now() - order_status.created_at >= timedelta(
        days=PREPAYMENT_WAITING_MAX_AGE_DAYS
    )


def _new_decision(
    deal: fulfillment.BitrixDealSnapshot,
    recommended_stage: str | None,
    reason: str,
    *,
    order_status: SaleOrderStatus | None = None,
    onec_settlement: OneCOrderSettlement | None = None,
) -> NewDealDecision:
    order_number = fulfillment._clean_string(  # noqa: SLF001
        (deal.raw or {}).get(fulfillment.CRM_ORDER_NUMBER_FIELD)
    )
    action = "update_stage" if recommended_stage else "manual_review"
    return NewDealDecision(
        site_order_number=order_number,
        bitrix_deal_id=deal.deal_id,
        crm_stage=fulfillment._clean_string(deal.stage_id) or None,  # noqa: SLF001
        crm_delivery=fulfillment._clean_string(deal.delivery) or None,  # noqa: SLF001
        crm_payment_status=fulfillment._clean_string(deal.payment_status) or None,  # noqa: SLF001
        order_canceled=order_status.canceled if order_status is not None else None,
        assembled=_truthy_bitrix_value((deal.raw or {}).get(CRM_ASSEMBLED_FIELD)),
        onec_sale_amount=(
            onec_settlement.posted_sale_amount if onec_settlement is not None else None
        ),
        onec_payment_amount=(
            onec_settlement.payment_amount if onec_settlement is not None else None
        ),
        onec_debt_amount=onec_settlement.debt_amount if onec_settlement is not None else None,
        onec_payment_confirmed=(
            onec_settlement.payment_confirmed if onec_settlement is not None else None
        ),
        recommended_stage=recommended_stage,
        action=action,
        review_reason=reason,
    )


def fetch_new_deals(
    client: fulfillment.BitrixChatClient, *, limit: int
) -> list[fulfillment.BitrixDealSnapshot]:
    deals: list[fulfillment.BitrixDealSnapshot] = []
    for stage_id in QUICK_STAGE_IDS:
        stage_count = 0
        start: int | None = 0
        while start is not None and stage_count < limit:
            response = client.call(
                "crm.deal.list",
                {
                    "filter": {"STAGE_ID": stage_id},
                    "select": [*fulfillment.CRM_REVIEW_SELECT_FIELDS, CRM_ASSEMBLED_FIELD],
                    "order": {"ID": "ASC"},
                    "start": start,
                },
            )
            for item in response.get("result") or []:
                deal = fulfillment.bitrix_deal_from_payload(item)
                if deal is not None:
                    deals.append(deal)
                    stage_count += 1
                    if stage_count >= limit:
                        break
            next_value = response.get("next")
            start = int(next_value) if next_value is not None else None
    return deals


def fetch_sale_order_statuses(order_numbers: list[str]) -> dict[str, SaleOrderStatus]:
    clean_numbers = sorted(
        {
            order_number.strip()
            for order_number in order_numbers
            if order_number and order_number.strip().isdigit()
        }
    )
    if not clean_numbers:
        return {}

    php_code = _build_sale_order_status_php(clean_numbers)
    try:
        completed = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "bitrix-box", "sudo -u mm php"],
            input=php_code,
            text=True,
            capture_output=True,
            check=False,
            timeout=20,
        )
    except Exception:
        return {}
    if completed.returncode != 0:
        return {}

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}

    result: dict[str, SaleOrderStatus] = {}
    for order_number, raw_status in payload.items():
        if not isinstance(raw_status, dict):
            continue
        result[str(order_number)] = SaleOrderStatus(
            order_number=str(order_number),
            canceled=fulfillment._clean_string(raw_status.get("CANCELED")) == "Y",  # noqa: SLF001
            status_id=fulfillment._clean_string(raw_status.get("STATUS_ID"))
            or None,  # noqa: SLF001
            payed=(
                fulfillment._clean_string(raw_status.get("PAYED")) == "Y"  # noqa: SLF001
                if raw_status.get("PAYED") is not None
                else None
            ),
            created_at=_parse_bitrix_datetime(raw_status.get("DATE_INSERT")),
        )
    return result


def fetch_onec_order_settlements(order_numbers: list[str]) -> dict[str, OneCOrderSettlement]:
    settings = get_settings()
    if not settings.onec_database_url:
        return {}
    unique_orders = [
        order_number
        for order_number in dict.fromkeys(
            fulfillment._clean_string(order) for order in order_numbers  # noqa: SLF001
        )
        if order_number and order_number.isdigit()
    ]
    if not unique_orders:
        return {}

    params = {f"order_{index}": order for index, order in enumerate(unique_orders)}
    placeholders = ", ".join(f":order_{index}" for index in range(len(unique_orders)))
    statement = text(f"""
        WITH orders AS (
            SELECT
                _IDRRef AS order_ref,
                NULLIF(LTRIM(RTRIM(_Fld2425)), N'') AS site_order_number
            FROM dbo._Document132 WITH (NOLOCK)
            WHERE LTRIM(RTRIM(_Fld2425)) IN ({placeholders})
        ),
        sales AS (
            SELECT
                o.site_order_number,
                sale._IDRRef AS sale_ref,
                CAST(sale._Fld4948 AS decimal(18, 2)) AS sale_amount
            FROM orders AS o
            JOIN dbo._Document203 AS sale WITH (NOLOCK)
                ON sale._Fld4939_TYPE = 0x08
               AND sale._Fld4939_RTRef = 0x00000084
               AND sale._Fld4939_RRRef = o.order_ref
            WHERE sale._Posted = 0x01
              AND sale._Marked <> 0x01
        ),
        sale_summary AS (
            SELECT
                site_order_number,
                COUNT(*) AS posted_sale_count,
                CAST(SUM(sale_amount) AS decimal(18, 2)) AS posted_sale_amount
            FROM sales
            GROUP BY site_order_number
        ),
        pko_payments AS (
            SELECT
                o.site_order_number,
                CAST(pko._Fld4688 AS decimal(18, 2)) AS amount
            FROM orders AS o
            JOIN dbo._Document196 AS pko WITH (NOLOCK)
                ON pko._Fld4697_TYPE = 0x08
               AND pko._Fld4697_RTRef = 0x00000084
               AND pko._Fld4697_RRRef = o.order_ref
            WHERE pko._Posted = 0x01
              AND pko._Marked <> 0x01
            UNION ALL
            SELECT
                s.site_order_number,
                CAST(pko._Fld4688 AS decimal(18, 2)) AS amount
            FROM sales AS s
            JOIN dbo._Document196 AS pko WITH (NOLOCK)
                ON pko._Fld4697_TYPE = 0x08
               AND pko._Fld4697_RTRef = 0x000000CB
               AND pko._Fld4697_RRRef = s.sale_ref
            WHERE pko._Posted = 0x01
              AND pko._Marked <> 0x01
            UNION ALL
            SELECT
                o.site_order_number,
                CAST(acquiring._Fld3414 AS decimal(18, 2)) AS amount
            FROM orders AS o
            JOIN dbo._Document169 AS acquiring WITH (NOLOCK)
                ON acquiring._Fld3417_TYPE = 0x08
               AND acquiring._Fld3417_RTRef = 0x00000084
               AND acquiring._Fld3417_RRRef = o.order_ref
            WHERE acquiring._Posted = 0x01
              AND acquiring._Marked <> 0x01
        ),
        payment_summary AS (
            SELECT
                site_order_number,
                CAST(SUM(amount) AS decimal(18, 2)) AS payment_amount
            FROM pko_payments
            GROUP BY site_order_number
        ),
        order_debt_summary AS (
            SELECT
                o.site_order_number,
                CAST(
                    SUM(
                        CASE WHEN debt._RecordKind = 0 THEN debt._Fld7620 ELSE -debt._Fld7620 END
                    ) AS decimal(18, 2)
                ) AS order_debt_amount
            FROM orders AS o
            JOIN dbo._AccumRg7614 AS debt WITH (NOLOCK)
                ON debt._RecorderTRef = 0x00000084
               AND debt._RecorderRRef = o.order_ref
            WHERE debt._Active = 0x01
            GROUP BY o.site_order_number
        )
        SELECT
            o.site_order_number,
            COALESCE(sale_summary.posted_sale_count, 0) AS posted_sale_count,
            sale_summary.posted_sale_amount,
            payment_summary.payment_amount,
            order_debt_summary.order_debt_amount
        FROM orders AS o
        LEFT JOIN sale_summary
            ON sale_summary.site_order_number = o.site_order_number
        LEFT JOIN payment_summary
            ON payment_summary.site_order_number = o.site_order_number
        LEFT JOIN order_debt_summary
            ON order_debt_summary.site_order_number = o.site_order_number
        WHERE o.site_order_number IS NOT NULL
        """)

    engine = None
    try:
        engine = build_engine(settings.onec_database_url, pool_pre_ping=True)
        with engine.connect() as connection:
            rows = connection.execute(statement, params).fetchall()
    except Exception:
        return {}
    finally:
        if engine is not None:
            engine.dispose()

    result: dict[str, OneCOrderSettlement] = {}
    for row in rows:
        mapping = getattr(row, "_mapping", row)
        order_number = fulfillment._clean_string(mapping["site_order_number"])  # noqa: SLF001
        if not order_number:
            continue
        posted_sale_count = int(mapping["posted_sale_count"] or 0)
        posted_sale_amount = _decimal_or_none(mapping["posted_sale_amount"])
        payment_amount = _decimal_or_none(mapping["payment_amount"])
        order_debt_amount = _decimal_or_none(mapping["order_debt_amount"])
        debt_amount = _calculate_order_debt_amount(
            order_debt_amount=order_debt_amount,
            payment_amount=payment_amount,
        )
        payment_confirmed, evidence = _onec_payment_confirmation(
            posted_sale_count=posted_sale_count,
            posted_sale_amount=posted_sale_amount,
            payment_amount=payment_amount,
            debt_amount=debt_amount,
        )
        result[order_number] = OneCOrderSettlement(
            order_number=order_number,
            posted_sale_count=posted_sale_count,
            posted_sale_amount=posted_sale_amount,
            payment_amount=payment_amount,
            debt_amount=debt_amount,
            payment_confirmed=payment_confirmed,
            evidence=evidence,
        )
    return result


def _onec_payment_confirmation(
    *,
    posted_sale_count: int,
    posted_sale_amount: Decimal | None,
    payment_amount: Decimal | None,
    debt_amount: Decimal | None,
) -> tuple[bool, str]:
    tolerance = Decimal("0.05")
    has_posted_sale = posted_sale_count > 0 and (posted_sale_amount or Decimal("0")) > 0
    if has_posted_sale and debt_amount is not None and abs(debt_amount) <= tolerance:
        return True, "onec_no_debt"
    if (
        posted_sale_amount is not None
        and posted_sale_amount > 0
        and payment_amount is not None
        and payment_amount >= posted_sale_amount - tolerance
    ):
        return True, "onec_full_payment"
    return False, "onec_payment_not_confirmed"


def _calculate_order_debt_amount(
    *,
    order_debt_amount: Decimal | None,
    payment_amount: Decimal | None,
) -> Decimal | None:
    if order_debt_amount is None:
        return None
    return order_debt_amount - (payment_amount or Decimal("0"))


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _parse_bitrix_datetime(value: Any) -> datetime | None:
    text_value = fulfillment._clean_string(value)  # noqa: SLF001
    if not text_value:
        return None
    for format_value in ("%Y-%m-%d %H:%M:%S", "%d.%m.%Y %H:%M:%S"):
        try:
            return datetime.strptime(text_value, format_value)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text_value)
    except ValueError:
        return None


def _build_sale_order_status_php(order_numbers: list[str]) -> str:
    encoded_numbers = json.dumps(order_numbers, ensure_ascii=False)
    return f"""<?php
$_SERVER["DOCUMENT_ROOT"] = "/var/www/mm/data/www/crm.master-mobile.ru";
define("NO_KEEP_STATISTIC", true);
define("NOT_CHECK_PERMISSIONS", true);
define("BX_CRONTAB", true);
chdir($_SERVER["DOCUMENT_ROOT"]);
require($_SERVER["DOCUMENT_ROOT"] . "/bitrix/modules/main/include/prolog_before.php");
$connection = Bitrix\\Main\\Application::getConnection();
$numbers = json_decode({json.dumps(encoded_numbers)}, true);
$ids = [];
foreach ($numbers as $number) {{
    if (preg_match('/^\\d+$/', (string)$number)) {{
        $ids[] = (int)$number;
    }}
}}
$result = [];
if (!empty($ids)) {{
    $sql = "SELECT ID, ACCOUNT_NUMBER, CANCELED, STATUS_ID, PAYED, DATE_INSERT FROM b_sale_order WHERE ID IN (" . implode(",", $ids) . ") OR ACCOUNT_NUMBER IN (" . implode(",", $ids) . ")";
    $rows = $connection->query($sql)->fetchAll();
    foreach ($rows as $row) {{
        $key = (string)($row["ACCOUNT_NUMBER"] ?: $row["ID"]);
        $dateInsert = $row["DATE_INSERT"];
        if ($dateInsert instanceof \\Bitrix\\Main\\Type\\DateTime) {{
            $dateInsert = $dateInsert->format("Y-m-d H:i:s");
        }} else {{
            $dateInsert = (string)$dateInsert;
        }}
        $result[$key] = [
            "ID" => (string)$row["ID"],
            "ACCOUNT_NUMBER" => (string)$row["ACCOUNT_NUMBER"],
            "CANCELED" => (string)$row["CANCELED"],
            "STATUS_ID" => (string)$row["STATUS_ID"],
            "PAYED" => (string)$row["PAYED"],
            "DATE_INSERT" => $dateInsert,
        ];
    }}
}}
echo json_encode($result, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
"""


def _truthy_bitrix_value(value: Any) -> bool:
    return fulfillment._clean_string(value).lower() in {
        "1",
        "y",
        "yes",
        "true",
        "да",
    }  # noqa: SLF001


def build_new_deal_outbox_rows(
    decisions: list[NewDealDecision],
    *,
    available_stage_ids: set[str],
) -> list[fulfillment.OrderFulfillmentStageOutboxRow]:
    review_rows = [
        fulfillment.OrderFulfillmentReviewRow(
            site_order_number=decision.site_order_number,
            bitrix_deal_id=decision.bitrix_deal_id,
            crm_stage=decision.crm_stage,
            crm_delivery=decision.crm_delivery,
            crm_payment_status=decision.crm_payment_status,
            onec_raw_delivery=None,
            onec_order_date=None,
            onec_courier=None,
            onec_delivery_cost=None,
            chat_event=f"new_deal:{decision.review_reason}",
            event_confidence="medium",
            evidence_redacted=f"NEW deal routing: {decision.review_reason}",
            recommended_stage=decision.recommended_stage,
            action=decision.action,
            manual_review_reason=(
                decision.review_reason if decision.action == "manual_review" else None
            ),
        )
        for decision in decisions
    ]
    return fulfillment.build_stage_outbox_rows(
        review_rows,
        available_stage_ids=available_stage_ids,
        allowed_target_stages={
            "PREPARATION",
            "PREPAYMENT_INVOICE",
            "EXECUTING",
            "FINAL_INVOICE",
            "PICKUP_WAITING",
            "DISMANTLING",
            "WON",
            "LOSE",
        },
    )


def run_quick_sync(
    *,
    client: fulfillment.BitrixChatClient,
    output_dir: Path,
    stamp: str,
    apply: bool,
    limit: int,
) -> dict[str, Any]:
    deals = fetch_new_deals(client, limit=limit)
    order_numbers = [
        fulfillment._clean_string(
            (deal.raw or {}).get(fulfillment.CRM_ORDER_NUMBER_FIELD)
        )  # noqa: SLF001
        for deal in deals
    ]
    order_statuses = fetch_sale_order_statuses(order_numbers)
    onec_settlements = fetch_onec_order_settlements(
        quick_onec_settlement_candidate_orders(deals, order_statuses)
    )
    decisions = [
        decide_new_deal_stage(
            deal,
            order_status=order_statuses.get(
                fulfillment._clean_string(
                    (deal.raw or {}).get(fulfillment.CRM_ORDER_NUMBER_FIELD)
                )  # noqa: SLF001
            ),
            onec_settlement=onec_settlements.get(
                fulfillment._clean_string(
                    (deal.raw or {}).get(fulfillment.CRM_ORDER_NUMBER_FIELD)
                )  # noqa: SLF001
            ),
        )
        for deal in deals
    ]
    available_stage_ids = client.list_deal_stage_ids()
    review_path = output_dir / f"new-deals-review-{stamp}.csv"
    write_new_review_csv(review_path, decisions)
    outbox_rows = build_new_deal_outbox_rows(decisions, available_stage_ids=available_stage_ids)
    outbox_path = output_dir / f"new-deals-stage-outbox-{stamp}.csv"
    fulfillment.write_stage_outbox_csv(outbox_path, outbox_rows)
    apply_results = apply_outbox_by_target(outbox_rows, client=client, apply=apply)
    apply_path = output_dir / f"new-deals-stage-apply-result-{stamp}.csv"
    fulfillment.write_stage_apply_result_csv(apply_path, apply_results)
    stage_summary = fetch_stage_summary(client)
    stage_path = output_dir / f"quick-stage-summary-{stamp}.csv"
    write_dict_csv(stage_path, stage_summary)
    rtu_signal_rows = query_rtu_without_assembled_for_deals(deals)
    rtu_signal_path = output_dir / f"executing-rtu-without-assembled-{stamp}.csv"
    write_dict_csv(rtu_signal_path, rtu_signal_rows, fieldnames=list(RTU_SIGNAL_CSV_FIELDS))
    monitoring_rows = build_operational_monitoring_rows(
        decisions=decisions,
        apply_results=apply_results,
        stage_summary=stage_summary,
        rtu_signal_rows=rtu_signal_rows,
        artifacts={
            "review": str(review_path),
            "apply_result": str(apply_path),
            "stage_summary": str(stage_path),
            "rtu_signal": str(rtu_signal_path),
        },
    )
    monitoring_path = output_dir / f"quick-operational-monitoring-{stamp}.csv"
    write_dict_csv(monitoring_path, monitoring_rows, fieldnames=list(MONITORING_CSV_FIELDS))
    summary = {
        "mode": "quick",
        "deals": len(deals),
        "review": str(review_path),
        "outbox": str(outbox_path),
        "apply_result": str(apply_path),
        "stage_summary": str(stage_path),
        "monitoring": str(monitoring_path),
        "rtu_without_assembled": str(rtu_signal_path),
        "dry_run": not apply,
        "by_reason": dict(Counter(decision.review_reason for decision in decisions)),
        "outbox_rows": len(outbox_rows),
        "apply_results": dict(Counter(row.result for row in apply_results)),
        "stage_summary_counts": stage_summary_counts(stage_summary),
        "stage_summary_error_count": sum(
            1 for row in stage_summary if clean_csv_value(row.get("error"))
        ),
        "rtu_without_assembled_rows": len(rtu_signal_rows),
    }
    enrich_summary_item_from_artifacts(summary)
    enrich_summary_item_from_monitoring(summary)
    return summary


def quick_onec_settlement_candidate_orders(
    deals: list[fulfillment.BitrixDealSnapshot],
    order_statuses: dict[str, SaleOrderStatus],
) -> list[str]:
    order_numbers: list[str] = []
    for deal in deals:
        order_number = fulfillment._clean_string(  # noqa: SLF001
            (deal.raw or {}).get(fulfillment.CRM_ORDER_NUMBER_FIELD)
        )
        if not order_number:
            continue
        order_status = order_statuses.get(order_number)
        if order_status is None:
            continue
        stage_id = fulfillment._clean_string(deal.stage_id)  # noqa: SLF001
        if stage_id == "PREPAYMENT_INVOICE" and order_status.status_id == "F":
            order_numbers.append(order_number)
            continue
        if order_status.payed is True:
            continue
        if stage_id == "PREPAYMENT_INVOICE" and _is_prepayment_waiting_expired(order_status):
            order_numbers.append(order_number)
            continue
        if stage_id == "PICKUP_WAITING" and order_status.status_id == "F":
            if fulfillment._clean_string(deal.payment_status) == "1":  # noqa: SLF001
                continue
            if not _is_pickup_delivery(fulfillment._clean_string(deal.delivery)):  # noqa: SLF001
                continue
            order_numbers.append(order_number)
    return order_numbers


def run_chat_sync(
    *,
    client: fulfillment.BitrixChatClient,
    output_dir: Path,
    stamp: str,
    apply: bool,
    site_limit: int,
    courier_limit: int,
    review_limit: int,
) -> dict[str, Any]:
    settings = get_settings()
    engine = build_engine(settings.database_url, pool_pre_ping=True)
    onec_engine = None
    stats: dict[str, Any] = {}
    try:
        if settings.onec_database_url:
            onec_engine = build_engine(settings.onec_database_url, pool_pre_ping=True)
        with Session(engine) as session:
            stats["site_master_mobile"] = fulfillment.ingest_bitrix_chat(
                session,
                client=client,
                chat_code=fulfillment.CHAT_SITE_MASTER_MOBILE,
                dialog_id=settings.order_fulfillment_site_chat_dialog_id,
                limit=site_limit,
                run_ocr=False,
                settings=settings,
            )
            stats["courier_spb"] = fulfillment.ingest_bitrix_chat(
                session,
                client=client,
                chat_code=fulfillment.CHAT_COURIER_SPB,
                dialog_id=settings.order_fulfillment_spb_courier_chat_dialog_id,
                limit=courier_limit,
                run_ocr=bool(settings.order_fulfillment_ocr_enabled),
                settings=settings,
            )
            session.commit()
            review_rows = fulfillment.build_review_rows(
                session,
                limit=review_limit,
                bitrix_client=client,
                onec_engine=onec_engine,
                settings=settings,
            )
    finally:
        engine.dispose()
        if onec_engine is not None:
            onec_engine.dispose()
    review_path = output_dir / f"chat-review-{stamp}.csv"
    fulfillment.write_review_csv(review_path, review_rows)
    outbox_rows = fulfillment.build_stage_outbox_rows(
        review_rows,
        available_stage_ids=client.list_deal_stage_ids(),
    )
    outbox_path = output_dir / f"chat-stage-outbox-{stamp}.csv"
    fulfillment.write_stage_outbox_csv(outbox_path, outbox_rows)
    chat_apply = apply and settings.order_fulfillment_chat_auto_apply_enabled
    blocked_event_prefixes = ("pickup_",) if settings.order_fulfillment_bot_enabled else ()
    apply_results = apply_outbox_by_target(
        outbox_rows,
        client=client,
        apply=chat_apply,
        allowed_target_stages=fulfillment.CHAT_AUTO_APPLY_TARGET_STAGES,
        blocked_event_prefixes=blocked_event_prefixes,
    )
    apply_path = output_dir / f"chat-stage-apply-result-{stamp}.csv"
    fulfillment.write_stage_apply_result_csv(apply_path, apply_results)
    summary = {
        "mode": "chat",
        "ingest": stats,
        "review": str(review_path),
        "outbox": str(outbox_path),
        "apply_result": str(apply_path),
        "dry_run": not chat_apply,
        "apply_requested": apply,
        "auto_apply_enabled": chat_apply,
        "auto_apply_configured": settings.order_fulfillment_chat_auto_apply_enabled,
        "pickup_auto_apply_suppressed_by_bot": bool(
            settings.order_fulfillment_bot_enabled
            and settings.order_fulfillment_chat_auto_apply_enabled
        ),
        "apply_target_stages": (
            sorted(fulfillment.CHAT_AUTO_APPLY_TARGET_STAGES) if chat_apply else []
        ),
        "review_rows": len(review_rows),
        "outbox_rows": len(outbox_rows),
        "apply_results": dict(Counter(row.result for row in apply_results)),
    }
    enrich_summary_item_from_artifacts(summary)
    return summary


def run_daily_sync(
    *,
    client: fulfillment.BitrixChatClient,
    output_dir: Path,
    stamp: str,
    unknown_date_from: date | None,
) -> dict[str, Any]:
    settings = get_settings()
    unknown_rows = fulfillment.query_unknown_delivery_methods(
        settings,
        date_from=unknown_date_from,
    )
    unknown_path = output_dir / f"unknown-delivery-methods-{stamp}.csv"
    write_unknown_delivery_csv(unknown_path, unknown_rows)
    stage_summary = fetch_stage_summary(client)
    stage_path = output_dir / f"stage-summary-{stamp}.csv"
    write_dict_csv(stage_path, stage_summary)
    return {
        "mode": "daily",
        "unknown_delivery": str(unknown_path),
        "unknown_delivery_rows": len(unknown_rows),
        "stage_summary": str(stage_path),
        "stage_summary_rows": len(stage_summary),
        "stage_summary_error_count": sum(
            1 for row in stage_summary if clean_csv_value(row.get("error"))
        ),
    }


def apply_outbox_by_target(
    rows: list[fulfillment.OrderFulfillmentStageOutboxRow],
    *,
    client: fulfillment.BitrixChatClient,
    apply: bool,
    allowed_target_stages: set[str] | None = None,
    blocked_event_prefixes: tuple[str, ...] = (),
) -> list[fulfillment.OrderFulfillmentStageApplyResult]:
    grouped: dict[
        tuple[str, bool],
        list[fulfillment.OrderFulfillmentStageOutboxRow],
    ] = defaultdict(list)
    for row in rows:
        row_apply = (
            apply
            and (allowed_target_stages is None or row.target_stage in allowed_target_stages)
            and not any(row.chat_event.startswith(prefix) for prefix in blocked_event_prefixes)
        )
        grouped[(row.target_stage, row_apply)].append(row)
    results: list[fulfillment.OrderFulfillmentStageApplyResult] = []
    for (target_stage, target_apply), target_rows in grouped.items():
        results.extend(
            fulfillment.apply_stage_outbox_rows(
                target_rows,
                client=client,
                apply=target_apply,
                target_stage=target_stage,
            )
        )
    return results


def fetch_stage_summary(client: fulfillment.BitrixChatClient) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for stage_id in PROCESS_STAGES:
        try:
            total = _bitrix_list_total(client, {"STAGE_ID": stage_id})
            internet_orders = _bitrix_list_total(
                client,
                {
                    "STAGE_ID": stage_id,
                    f"!{fulfillment.CRM_ORDER_NUMBER_FIELD}": "",
                },
            )
            error = ""
        except fulfillment.BitrixChatError as exc:
            # Stage counters are useful diagnostics, but they must not prevent
            # the daily operational digest from being delivered.
            total = None
            internet_orders = None
            error = fulfillment._safe_error_reason(str(exc))  # noqa: SLF001
        rows.append(
            {
                "stage_id": stage_id,
                "deal_count": total,
                "internet_order_count": internet_orders,
                "error": error,
            }
        )
    return rows


def _bitrix_list_total(client: fulfillment.BitrixChatClient, filter_payload: dict[str, Any]) -> int:
    payload = {
        "filter": filter_payload,
        "select": ["ID"],
        "start": 0,
    }
    response: dict[str, Any] | None = None
    for attempt in range(len(BITRIX_READ_RETRY_DELAYS) + 1):
        try:
            response = client.call("crm.deal.list", payload)
            break
        except fulfillment.BitrixChatError:
            if attempt >= len(BITRIX_READ_RETRY_DELAYS):
                raise
            time.sleep(BITRIX_READ_RETRY_DELAYS[attempt])
    if response is None:
        raise fulfillment.BitrixChatError("crm.deal.list returned no response")
    total = response.get("total")
    if total is not None:
        return int(total)
    return len(response.get("result") or [])


def write_new_review_csv(path: Path, rows: list[NewDealDecision]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=list(NEW_REVIEW_FIELDS))
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "site_order_number": row.site_order_number,
                    "bitrix_deal_id": row.bitrix_deal_id,
                    "crm_stage": row.crm_stage,
                    "crm_delivery": row.crm_delivery,
                    "crm_payment_status": row.crm_payment_status,
                    "order_canceled": row.order_canceled,
                    "assembled": row.assembled,
                    "onec_sale_amount": row.onec_sale_amount,
                    "onec_payment_amount": row.onec_payment_amount,
                    "onec_debt_amount": row.onec_debt_amount,
                    "onec_payment_confirmed": row.onec_payment_confirmed,
                    "recommended_stage": row.recommended_stage,
                    "action": row.action,
                    "review_reason": row.review_reason,
                }
            )
    return path


def write_unknown_delivery_csv(
    path: Path,
    rows: list[fulfillment.DeliveryMethodReportRow],
) -> Path:
    payload = [
        {
            "raw_delivery_method": row.raw_delivery_method,
            "count": row.count,
            "status": row.status,
            "note": row.note,
        }
        for row in rows
    ]
    return write_dict_csv(path, payload)


def write_dict_csv(
    path: Path,
    rows: list[dict[str, Any]],
    *,
    fieldnames: list[str] | None = None,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = fieldnames or (
        sorted({key for row in rows for key in row}) if rows else ["status"]
    )
    with path.open("w", encoding="utf-8-sig", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def stage_summary_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    return {
        clean_csv_value(row.get("stage_id")): int(row.get("internet_order_count") or 0)
        for row in rows
        if clean_csv_value(row.get("stage_id")) and not clean_csv_value(row.get("error"))
    }


def query_rtu_without_assembled_for_deals(
    deals: list[fulfillment.BitrixDealSnapshot],
) -> list[dict[str, Any]]:
    executing_deals = [
        deal
        for deal in deals
        if clean_csv_value(deal.stage_id) == "EXECUTING"
        and fulfillment._clean_string(  # noqa: SLF001
            (deal.raw or {}).get(fulfillment.CRM_ORDER_NUMBER_FIELD)
        )
    ]
    if not executing_deals:
        return []

    order_numbers = [
        fulfillment._clean_string(
            (deal.raw or {}).get(fulfillment.CRM_ORDER_NUMBER_FIELD)
        )  # noqa: SLF001
        for deal in executing_deals
    ]
    try:
        rtu_by_order = query_rtu_signal_by_orders(order_numbers)
    except Exception:
        return []
    return build_rtu_without_assembled_rows(executing_deals, rtu_by_order)


def query_rtu_signal_by_orders(order_numbers: list[str]) -> dict[str, dict[str, Any]]:
    settings = get_settings()
    unique_orders = [
        order_number
        for order_number in dict.fromkeys(clean_csv_value(item) for item in order_numbers)
        if order_number
    ]
    if not unique_orders or not settings.onec_database_url:
        return {}

    params = {f"order_{index}": order for index, order in enumerate(unique_orders)}
    placeholders = ", ".join(f":order_{index}" for index in range(len(unique_orders)))
    statement = text(f"""
        WITH rtu_source AS (
            SELECT
                NULLIF(LTRIM(RTRIM(ord._Fld2425)), N'') AS site_order_number,
                LTRIM(RTRIM(rtu._Number)) AS rtu_number,
                rtu._Date_Time AS rtu_date,
                CASE WHEN EXISTS (
                    SELECT 1
                    FROM dbo._InfoRg9448 AS assembled_event WITH (NOLOCK)
                    WHERE assembled_event._Fld9449_RRRef = rtu._IDRRef
                      AND assembled_event._Fld9449_TYPE = 0x08
                      AND assembled_event._Fld9449_RTRef = 0x000000CB
                      AND assembled_event._Fld9454 = N'Собран'
                ) THEN 1 ELSE 0 END AS has_assembled,
                CASE WHEN EXISTS (
                    SELECT 1
                    FROM dbo._InfoRg9448 AS print_event WITH (NOLOCK)
                    WHERE print_event._Fld9449_RRRef = rtu._IDRRef
                      AND print_event._Fld9449_TYPE = 0x08
                      AND print_event._Fld9449_RTRef = 0x000000CB
                      AND print_event._Fld9454 = N'Распечатан'
                ) THEN 1 ELSE 0 END AS has_print,
                CASE WHEN EXISTS (
                    SELECT 1
                    FROM dbo._InfoRg9448 AS scan_event WITH (NOLOCK)
                    WHERE scan_event._Fld9449_RRRef = rtu._IDRRef
                      AND scan_event._Fld9449_TYPE = 0x08
                      AND scan_event._Fld9449_RTRef = 0x000000CB
                      AND scan_event._Fld9454 = N'Отсканирован'
                ) THEN 1 ELSE 0 END AS has_scan,
                CASE WHEN EXISTS (
                    SELECT 1
                    FROM dbo._Document109 AS return_doc WITH (NOLOCK)
                    WHERE return_doc._Posted = 0x01
                      AND return_doc._Marked = 0x00
                      AND (
                          (
                              return_doc._Fld1684_TYPE = 0x08
                              AND return_doc._Fld1684_RTRef = 0x000000CB
                              AND return_doc._Fld1684_RRRef = rtu._IDRRef
                          )
                          OR EXISTS (
                              SELECT 1
                              FROM dbo._Document109_VT1698 AS return_line WITH (NOLOCK)
                              WHERE return_line._Document109_IDRRef = return_doc._IDRRef
                                AND return_line._Fld1712_TYPE = 0x08
                                AND return_line._Fld1712_RTRef = 0x000000CB
                                AND return_line._Fld1712_RRRef = rtu._IDRRef
                          )
                      )
                ) THEN 1 ELSE 0 END AS has_return
            FROM dbo._Document203 AS rtu WITH (NOLOCK)
            JOIN dbo._Document132 AS ord WITH (NOLOCK)
                ON ord._IDRRef = rtu._Fld4939_RRRef
            WHERE rtu._Fld4939_RRRef IS NOT NULL
              AND rtu._Posted = 0x01
              AND rtu._Marked <> 0x01
              AND LTRIM(RTRIM(ord._Fld2425)) IN ({placeholders})
        ),
        ranked AS (
            SELECT
                site_order_number,
                rtu_number,
                rtu_date,
                has_assembled,
                has_print,
                has_scan,
                has_return,
                ROW_NUMBER() OVER (
                    PARTITION BY site_order_number
                    ORDER BY rtu_date DESC, rtu_number DESC
                ) AS rn
            FROM rtu_source
            WHERE site_order_number IS NOT NULL
        )
        SELECT
            site_order_number,
            COUNT(*) AS rtu_count,
            SUM(has_assembled) AS assembled_rtu_count,
            SUM(has_print) AS printed_rtu_count,
            SUM(has_scan) AS scanned_rtu_count,
            SUM(CASE WHEN has_print = 1 AND has_scan = 1 THEN 1 ELSE 0 END)
                AS issued_rtu_count,
            SUM(has_return) AS returned_rtu_count,
            MAX(CASE WHEN rn = 1 THEN rtu_number END) AS latest_rtu_number,
            MAX(CASE WHEN rn = 1 THEN rtu_date END) AS latest_rtu_date
        FROM ranked
        GROUP BY site_order_number
        """)
    engine = build_engine(settings.onec_database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            rows = connection.execute(statement, params).fetchall()
    finally:
        engine.dispose()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        mapping = getattr(row, "_mapping", row)
        order_number = clean_csv_value(mapping["site_order_number"])
        if not order_number:
            continue
        result[order_number] = {
            "rtu_count": int(mapping["rtu_count"] or 0),
            "assembled_rtu_count": int(mapping["assembled_rtu_count"] or 0),
            "printed_rtu_count": int(mapping["printed_rtu_count"] or 0),
            "scanned_rtu_count": int(mapping["scanned_rtu_count"] or 0),
            "issued_rtu_count": int(mapping["issued_rtu_count"] or 0),
            "returned_rtu_count": int(mapping["returned_rtu_count"] or 0),
            "latest_rtu_number": clean_csv_value(mapping["latest_rtu_number"]),
            "latest_rtu_date": mapping["latest_rtu_date"],
        }
    return result


def build_rtu_without_assembled_rows(
    deals: list[fulfillment.BitrixDealSnapshot],
    rtu_by_order: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for deal in deals:
        order_number = fulfillment._clean_string(  # noqa: SLF001
            (deal.raw or {}).get(fulfillment.CRM_ORDER_NUMBER_FIELD)
        )
        signal = rtu_by_order.get(order_number)
        if not signal:
            continue
        if int(signal.get("rtu_count") or 0) <= 0:
            continue
        if int(signal.get("assembled_rtu_count") or 0) > 0:
            continue
        latest_rtu_date = signal.get("latest_rtu_date")
        rows.append(
            {
                "site_order_number": order_number,
                "bitrix_deal_id": deal.deal_id,
                "crm_stage": deal.stage_id,
                "rtu_count": int(signal.get("rtu_count") or 0),
                "latest_rtu_number": signal.get("latest_rtu_number") or "",
                "latest_rtu_date": (
                    latest_rtu_date.isoformat()
                    if isinstance(latest_rtu_date, datetime)
                    else clean_csv_value(latest_rtu_date)
                ),
                "assembled_rtu_count": int(signal.get("assembled_rtu_count") or 0),
            }
        )
    return rows


def build_operational_monitoring_rows(
    *,
    decisions: list[NewDealDecision],
    apply_results: list[fulfillment.OrderFulfillmentStageApplyResult],
    stage_summary: list[dict[str, Any]],
    rtu_signal_rows: list[dict[str, Any]],
    artifacts: dict[str, str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    counts = stage_summary_counts(stage_summary)
    for stage_id in MONITORED_QUEUE_STAGES:
        count = counts.get(stage_id, 0)
        if count <= 0:
            continue
        rows.append(
            monitoring_row(
                key=f"stage_count|{stage_id}|{count}",
                severity="critical" if stage_id == "DELIVERY_REVIEW" else "warning",
                alert_type="stage_count",
                stage_id=stage_id,
                count=count,
                reason=f"{stage_id}_has_internet_orders",
                artifact=artifacts.get("stage_summary"),
            )
        )

    for decision in decisions:
        if decision.review_reason != "prepayment_unpaid_expired_to_lost":
            continue
        rows.append(
            monitoring_row(
                key=(
                    "overdue_prepayment|"
                    f"{decision.site_order_number or '-'}|{decision.bitrix_deal_id}"
                ),
                severity="warning",
                alert_type="overdue_prepayment",
                site_order_number=decision.site_order_number,
                bitrix_deal_id=decision.bitrix_deal_id,
                stage_id=decision.crm_stage,
                count=1,
                reason=decision.review_reason,
                artifact=artifacts.get("review"),
            )
        )

    for decision in decisions:
        if decision.review_reason not in {
            "pickup_waiting_completed_needs_payment_check",
            "pickup_waiting_completed_needs_handoff_confirmation",
        }:
            continue
        rows.append(
            monitoring_row(
                key=(
                    "pickup_waiting_close_candidate|"
                    f"{decision.site_order_number or '-'}|{decision.bitrix_deal_id}"
                ),
                severity="warning",
                alert_type="pickup_waiting_close_candidate",
                site_order_number=decision.site_order_number,
                bitrix_deal_id=decision.bitrix_deal_id,
                stage_id=decision.crm_stage,
                count=1,
                reason=decision.review_reason,
                artifact=artifacts.get("review"),
            )
        )

    for result in apply_results:
        if not is_technical_apply_result(result):
            continue
        rows.append(
            monitoring_row(
                key=(
                    "outbox_error|"
                    f"{result.site_order_number or '-'}|{result.bitrix_deal_id}|"
                    f"{result.target_stage}|{result.result}|{result.reason or '-'}"
                ),
                severity="critical",
                alert_type="outbox_error",
                site_order_number=result.site_order_number,
                bitrix_deal_id=result.bitrix_deal_id,
                stage_id=result.target_stage,
                count=1,
                reason=result.reason or result.result,
                artifact=artifacts.get("apply_result"),
            )
        )

    for row in rtu_signal_rows:
        rows.append(
            monitoring_row(
                key=(
                    "rtu_without_assembled|"
                    f"{clean_csv_value(row.get('site_order_number')) or '-'}|"
                    f"{clean_csv_value(row.get('bitrix_deal_id')) or '-'}"
                ),
                severity="warning",
                alert_type="rtu_without_assembled",
                site_order_number=clean_csv_value(row.get("site_order_number")),
                bitrix_deal_id=clean_csv_value(row.get("bitrix_deal_id")),
                stage_id=clean_csv_value(row.get("crm_stage")),
                count=int(row.get("rtu_count") or 0),
                reason="executing_has_rtu_without_assembled_event",
                artifact=artifacts.get("rtu_signal"),
            )
        )
    return rows


def monitoring_row(
    *,
    key: str,
    severity: str,
    alert_type: str,
    site_order_number: Any = None,
    bitrix_deal_id: Any = None,
    stage_id: Any = None,
    count: int = 1,
    reason: str = "",
    artifact: str | None = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "severity": severity,
        "alert_type": alert_type,
        "site_order_number": clean_csv_value(site_order_number),
        "bitrix_deal_id": clean_csv_value(bitrix_deal_id),
        "stage_id": clean_csv_value(stage_id),
        "count": int(count),
        "reason": clean_csv_value(reason),
        "artifact": artifact or "",
    }


def is_technical_apply_result(row: fulfillment.OrderFulfillmentStageApplyResult) -> bool:
    return row.result in TECHNICAL_APPLY_RESULTS or row.input_state in TECHNICAL_INPUT_STATES


def enrich_summary_item_from_artifacts(item: dict[str, Any]) -> dict[str, Any]:
    review_rows = read_csv_dicts(item.get("review"))
    outbox_rows = read_csv_dicts(item.get("outbox"))
    apply_rows = read_csv_dicts(item.get("apply_result"))

    if review_rows:
        action_counts = Counter(clean_csv_value(row.get("action")) for row in review_rows)
        manual_rows = [
            row for row in review_rows if clean_csv_value(row.get("action")) == "manual_review"
        ]
        item["deal_keys"] = unique_values(
            key for row in review_rows if (key := review_deal_key(row))
        )
        item["review_action_counts"] = dict(action_counts)
        item["manual_review_keys"] = unique_values(manual_review_key(row) for row in manual_rows)
        item["manual_review_reason_counts"] = dict(
            Counter(manual_review_reason(row) for row in manual_rows)
        )
        item["manual_review_examples"] = [manual_review_example(row) for row in manual_rows[:5]]
    else:
        item.setdefault("deal_keys", [])
        item.setdefault("review_action_counts", {})
        item.setdefault("manual_review_keys", [])
        item.setdefault("manual_review_reason_counts", {})
        item.setdefault("manual_review_examples", [])

    if outbox_rows:
        item["outbox_state_counts"] = dict(
            Counter(clean_csv_value(row.get("state")) for row in outbox_rows)
        )
        item["outbox_target_counts"] = dict(
            Counter(clean_csv_value(row.get("target_stage")) for row in outbox_rows)
        )
    else:
        item.setdefault("outbox_state_counts", {})
        item.setdefault("outbox_target_counts", {})

    if apply_rows:
        item["apply_results"] = dict(
            Counter(clean_csv_value(row.get("result")) for row in apply_rows)
        )
        item["ready_keys"] = unique_values(
            clean_csv_value(row.get("idempotency_key"))
            for row in apply_rows
            if clean_csv_value(row.get("result")) in READY_APPLY_RESULTS
        )
        technical_rows = [row for row in apply_rows if is_technical_apply_row(row)]
        item["technical_review_keys"] = unique_values(
            technical_review_key(row) for row in technical_rows
        )
        item["technical_review_result_counts"] = dict(
            Counter(technical_result_label(row) for row in technical_rows)
        )
        item["technical_review_examples"] = [
            technical_review_example(row) for row in technical_rows[:5]
        ]
    else:
        item.setdefault("ready_keys", [])
        item.setdefault("technical_review_keys", [])
        item.setdefault("technical_review_result_counts", {})
        item.setdefault("technical_review_examples", [])
    return item


def enrich_summary_item_from_monitoring(item: dict[str, Any]) -> dict[str, Any]:
    rows = read_csv_dicts(item.get("monitoring"))
    if not rows:
        item.setdefault("operational_alert_keys", [])
        item.setdefault("operational_alert_counts", {})
        item.setdefault("operational_alert_examples", [])
        return item

    item["operational_alert_keys"] = unique_values(row.get("key") for row in rows)
    item["operational_alert_counts"] = dict(
        Counter(clean_csv_value(row.get("alert_type")) or "operational_alert" for row in rows)
    )
    item["operational_alert_examples"] = [operational_alert_example(row) for row in rows[:5]]
    return item


def operational_alert_example(row: dict[str, Any]) -> dict[str, str]:
    return {
        "key": clean_csv_value(row.get("key")),
        "site_order_number": clean_csv_value(row.get("site_order_number")) or "-",
        "bitrix_deal_id": clean_csv_value(row.get("bitrix_deal_id")) or "-",
        "stage_id": clean_csv_value(row.get("stage_id")) or "-",
        "reason": clean_csv_value(row.get("reason")) or "-",
        "alert_type": clean_csv_value(row.get("alert_type")) or "-",
        "severity": clean_csv_value(row.get("severity")) or "-",
    }


def read_csv_dicts(path_value: Any) -> list[dict[str, str]]:
    path_text = clean_csv_value(path_value)
    if not path_text:
        return []
    path = Path(path_text)
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as file_obj:
        return [dict(row) for row in csv.DictReader(file_obj)]


def clean_csv_value(value: Any) -> str:
    return fulfillment._clean_string(value)  # noqa: SLF001 - cron shares CSV normalizer.


def unique_values(values: Any) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text_value = clean_csv_value(value)
        if not text_value or text_value in seen:
            continue
        seen.add(text_value)
        result.append(text_value)
    return result


def review_deal_key(row: dict[str, Any]) -> str:
    deal_id = clean_csv_value(row.get("bitrix_deal_id"))
    order_number = clean_csv_value(row.get("site_order_number"))
    if deal_id:
        return f"deal:{deal_id}"
    if order_number:
        return f"order:{order_number}"
    return ""


def manual_review_reason(row: dict[str, Any]) -> str:
    return (
        clean_csv_value(row.get("manual_review_reason"))
        or clean_csv_value(row.get("review_reason"))
        or "manual_review"
    )


def manual_review_event(row: dict[str, Any]) -> str:
    event = clean_csv_value(row.get("chat_event"))
    if event:
        return event
    return f"new_deal:{manual_review_reason(row)}"


def manual_review_key(row: dict[str, Any]) -> str:
    return "|".join(
        [
            clean_csv_value(row.get("site_order_number")) or "-",
            clean_csv_value(row.get("bitrix_deal_id")) or "-",
            manual_review_event(row) or "-",
            manual_review_reason(row) or "-",
            clean_csv_value(row.get("recommended_stage")) or "-",
        ]
    )


def manual_review_example(row: dict[str, Any]) -> dict[str, str]:
    return {
        "key": manual_review_key(row),
        "site_order_number": clean_csv_value(row.get("site_order_number")) or "-",
        "bitrix_deal_id": clean_csv_value(row.get("bitrix_deal_id")) or "-",
        "reason": manual_review_reason(row),
        "recommended_stage": clean_csv_value(row.get("recommended_stage")) or "-",
    }


def is_technical_apply_row(row: dict[str, Any]) -> bool:
    result = clean_csv_value(row.get("result"))
    input_state = clean_csv_value(row.get("input_state"))
    return result in TECHNICAL_APPLY_RESULTS or input_state in TECHNICAL_INPUT_STATES


def technical_result_label(row: dict[str, Any]) -> str:
    input_state = clean_csv_value(row.get("input_state"))
    if input_state in TECHNICAL_INPUT_STATES:
        return input_state
    return clean_csv_value(row.get("result")) or "technical_review"


def technical_review_key(row: dict[str, Any]) -> str:
    return "|".join(
        [
            clean_csv_value(row.get("site_order_number")) or "-",
            clean_csv_value(row.get("bitrix_deal_id")) or "-",
            clean_csv_value(row.get("target_stage")) or "-",
            technical_result_label(row),
            clean_csv_value(row.get("reason")) or "-",
        ]
    )


def technical_review_example(row: dict[str, Any]) -> dict[str, str]:
    return {
        "key": technical_review_key(row),
        "site_order_number": clean_csv_value(row.get("site_order_number")) or "-",
        "bitrix_deal_id": clean_csv_value(row.get("bitrix_deal_id")) or "-",
        "target_stage": clean_csv_value(row.get("target_stage")) or "-",
        "result": technical_result_label(row),
        "reason": clean_csv_value(row.get("reason")) or "-",
    }


def notify_config_from_settings(settings: Any) -> NotifyConfig:
    state_path = Path(settings.order_fulfillment_notify_state_path)
    if not state_path.is_absolute():
        state_path = REPO_ROOT / state_path
    raw_site_dialog_id = getattr(settings, "order_fulfillment_notify_site_dialog_id", None)
    site_dialog_id = normalize_notify_dialog_id(raw_site_dialog_id)
    if raw_site_dialog_id is None:
        site_dialog_id = normalize_notify_dialog_id(
            getattr(settings, "order_fulfillment_site_chat_dialog_id", None)
        )
    return NotifyConfig(
        enabled=bool(settings.order_fulfillment_notify_enabled),
        business_user_ids=list(settings.order_fulfillment_notify_business_user_ids or []),
        tech_user_ids=list(settings.order_fulfillment_notify_tech_user_ids or []),
        method=settings.order_fulfillment_notify_method or "im.notify.system.add",
        site_dialog_id=site_dialog_id,
        site_dialog_method=(
            settings.order_fulfillment_notify_site_dialog_method or "im.message.add"
        ),
        state_path=state_path,
    )


def normalize_notify_dialog_id(value: Any) -> str | None:
    dialog_id = clean_csv_value(value)
    if not dialog_id:
        return None
    if dialog_id.lower() in {"-", "0", "false", "none", "null", "disabled"}:
        return None
    return dialog_id


def load_notification_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def write_notification_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    temp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp_path, path)


def deliver_order_fulfillment_notifications(
    *,
    client: Any,
    summary: dict[str, Any],
    summary_path: Path,
    output_dir: Path,
    settings: Any,
) -> dict[str, Any]:
    config = notify_config_from_settings(settings)
    result: dict[str, Any] = {"enabled": config.enabled, "sent": [], "skipped": [], "errors": []}
    if not config.enabled:
        result["skipped"].append("notifications_disabled")
        return result

    state = load_notification_state(config.state_path)
    state_changed = False

    manual_events = collect_events(summary, mode="chat", key_field="manual_review_keys")
    manual_new = new_events_for_state(state, "manual_review_keys", manual_events)
    if manual_new and config.business_user_ids:
        delivery = send_order_fulfillment_message(
            client=client,
            config=config,
            user_ids=config.business_user_ids,
            dialog_id=None,
            tag=f"order-fulfillment|manual|{summary.get('stamp')}",
            message=build_manual_review_message(summary, summary_path, manual_new),
        )
        result["errors"].extend(delivery["errors"])
        if delivery_reached_required_target(delivery, required_dialog_id=None):
            result["sent"].append({"kind": "manual_review", "count": len(manual_new)})
            remember_state_keys(state, "manual_review_keys", [event["key"] for event in manual_new])
            state_changed = True
        else:
            result["skipped"].append("manual_review_delivery_failed")
    elif manual_new:
        result["skipped"].append("manual_review_no_business_recipients")

    technical_events = collect_events(summary, key_field="technical_review_keys")
    technical_new = new_events_for_state(state, "technical_review_keys", technical_events)
    if technical_new and config.tech_user_ids:
        delivery = send_order_fulfillment_message(
            client=client,
            config=config,
            user_ids=config.tech_user_ids,
            dialog_id=None,
            tag=f"order-fulfillment|technical|{summary.get('stamp')}",
            message=build_technical_review_message(summary, summary_path, technical_new),
        )
        result["errors"].extend(delivery["errors"])
        if delivery_reached_required_target(delivery, required_dialog_id=None):
            result["sent"].append({"kind": "technical_review", "count": len(technical_new)})
            remember_state_keys(
                state,
                "technical_review_keys",
                [event["key"] for event in technical_new],
            )
            state_changed = True
        else:
            result["skipped"].append("technical_review_delivery_failed")
    elif technical_new:
        result["skipped"].append("technical_review_no_tech_recipients")

    operational_events = collect_events(summary, key_field="operational_alert_keys")
    operational_new = new_events_for_state(
        state,
        "operational_alert_keys",
        operational_events,
    )
    operational_user_ids = unique_ints([*config.business_user_ids, *config.tech_user_ids])
    if operational_new and has_notification_recipient(
        user_ids=operational_user_ids,
        dialog_id=config.site_dialog_id,
    ):
        delivery = send_order_fulfillment_message(
            client=client,
            config=config,
            user_ids=operational_user_ids,
            dialog_id=config.site_dialog_id,
            tag=f"order-fulfillment|ops|{summary.get('stamp')}",
            message=build_operational_alert_message(summary, summary_path, operational_new),
        )
        result["errors"].extend(delivery["errors"])
        if delivery_reached_required_target(delivery, required_dialog_id=config.site_dialog_id):
            result["sent"].append({"kind": "operational_alert", "count": len(operational_new)})
            remember_state_keys(
                state,
                "operational_alert_keys",
                [event["key"] for event in operational_new],
            )
            state_changed = True
        else:
            result["skipped"].append("operational_alert_delivery_failed")
    elif operational_new:
        result["skipped"].append("operational_alert_no_recipients")

    if has_daily_item(summary):
        daily_delivery = deliver_daily_digest(
            client=client,
            config=config,
            state=state,
            summary=summary,
            summary_path=summary_path,
            output_dir=output_dir,
        )
        result["sent"].extend(daily_delivery["sent"])
        result["skipped"].extend(daily_delivery["skipped"])
        result["errors"].extend(daily_delivery["errors"])
        state_changed = state_changed or bool(daily_delivery["state_changed"])

    if state_changed:
        write_notification_state(config.state_path, state)
    return result


def collect_events(
    summary: dict[str, Any],
    *,
    key_field: str,
    mode: str | None = None,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for item in summary.get("items") or []:
        if mode is not None and item.get("mode") != mode:
            continue
        examples = {
            clean_csv_value(example.get("key")): example
            for example in item.get(example_field_for_key(key_field)) or []
            if isinstance(example, dict)
        }
        for key in item.get(key_field) or []:
            clean_key = clean_csv_value(key)
            if not clean_key:
                continue
            example = examples.get(clean_key, {})
            events.append({"key": clean_key, "item": item, "example": example})
    return unique_events(events)


def example_field_for_key(key_field: str) -> str:
    if key_field == "manual_review_keys":
        return "manual_review_examples"
    if key_field == "technical_review_keys":
        return "technical_review_examples"
    if key_field == "operational_alert_keys":
        return "operational_alert_examples"
    return ""


def unique_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for event in events:
        key = clean_csv_value(event.get("key"))
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(event)
    return result


def new_events_for_state(
    state: dict[str, Any],
    state_field: str,
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    seen = {clean_csv_value(key) for key in state.get(state_field) or []}
    return [event for event in events if clean_csv_value(event.get("key")) not in seen]


def remember_state_keys(state: dict[str, Any], state_field: str, keys: list[str]) -> None:
    current = unique_values(state.get(state_field) or [])
    combined = unique_values([*current, *keys])
    state[state_field] = combined[-NOTIFICATION_STATE_KEY_LIMIT:]


def send_bitrix_notification(
    *,
    client: Any,
    method: str,
    user_ids: list[int],
    tag: str,
    message: str,
) -> dict[str, Any]:
    errors: list[str] = []
    sent_user_ids: list[int] = []
    for user_id in unique_ints(user_ids):
        try:
            client.call(
                method,
                {
                    "USER_ID": str(user_id),
                    "MESSAGE": message,
                    "TAG": tag,
                    "SUB_TAG": tag,
                },
            )
        except Exception as exc:  # noqa: BLE001 - notification must not stop the cron.
            errors.append(f"user_id={user_id}: {type(exc).__name__}")
        else:
            sent_user_ids.append(user_id)
    return {"sent_user_ids": sent_user_ids, "errors": errors}


def send_order_fulfillment_message(
    *,
    client: Any,
    config: NotifyConfig,
    user_ids: list[int],
    dialog_id: str | None,
    tag: str,
    message: str,
) -> dict[str, Any]:
    notification = send_bitrix_notification(
        client=client,
        method=config.method,
        user_ids=user_ids,
        tag=tag,
        message=message,
    )
    dialog = send_bitrix_dialog_message(
        client=client,
        method=config.site_dialog_method,
        dialog_id=dialog_id,
        message=message,
    )
    return {
        "sent_user_ids": notification["sent_user_ids"],
        "sent_dialog_ids": dialog["sent_dialog_ids"],
        "errors": [*notification["errors"], *dialog["errors"]],
    }


def send_bitrix_dialog_message(
    *,
    client: Any,
    method: str,
    dialog_id: str | None,
    message: str,
) -> dict[str, Any]:
    if not dialog_id:
        return {"sent_dialog_ids": [], "errors": []}
    try:
        client.call(method or "im.message.add", {"DIALOG_ID": dialog_id, "MESSAGE": message})
    except Exception as exc:  # noqa: BLE001 - notification must not stop the cron.
        return {
            "sent_dialog_ids": [],
            "errors": [f"dialog_id={dialog_id}: {type(exc).__name__}"],
        }
    return {"sent_dialog_ids": [dialog_id], "errors": []}


def has_notification_recipient(*, user_ids: list[int], dialog_id: str | None) -> bool:
    return bool(user_ids or dialog_id)


def delivery_reached_required_target(
    delivery: dict[str, Any],
    *,
    required_dialog_id: str | None,
) -> bool:
    if required_dialog_id:
        return bool(delivery.get("sent_dialog_ids"))
    return bool(delivery.get("sent_user_ids"))


def unique_ints(values: list[int]) -> list[int]:
    seen: set[int] = set()
    result: list[int] = []
    for value in values:
        int_value = int(value)
        if int_value in seen:
            continue
        seen.add(int_value)
        result.append(int_value)
    return result


def build_manual_review_message(
    summary: dict[str, Any],
    summary_path: Path,
    events: list[dict[str, Any]],
) -> str:
    counts = Counter(
        clean_csv_value(event.get("example", {}).get("reason")) or "manual_review"
        for event in events
    )
    del summary, summary_path
    lines = [
        "Интернет-заказы: требуется ручная проверка",
        f"Новых случаев: {len(events)}",
        "Что произошло и что сделать:",
        *format_manual_review_summary(counts),
    ]
    examples = format_manual_review_examples([event.get("example", {}) for event in events])
    if examples:
        lines.extend(["Заказы:", *examples])
    lines.append("Ответственный: менеджер сделки или руководитель интернет-заказов.")
    return "\n".join(lines)


def build_technical_review_message(
    summary: dict[str, Any],
    summary_path: Path,
    events: list[dict[str, Any]],
) -> str:
    counts = Counter(
        clean_csv_value(event.get("example", {}).get("result")) or "technical_review"
        for event in events
    )
    lines = [
        "MASTER-MOBILE.RU: CRM интернет-заказы, нужен технический разбор",
        f"Новых технических случаев: {len(events)}",
        f"Результаты: {format_top_counts(counts)}",
        "Примеры:",
        *format_examples([event.get("example", {}) for event in events]),
        f"Подробный отчет: {format_paths(paths_for_events(events, 'apply_result'))}",
        f"Служебная сводка: {summary_path}",
        f"Автоизменение CRM: {auto_apply_label_ru(summary)}",
    ]
    return "\n".join(lines)


def build_operational_alert_message(
    summary: dict[str, Any],
    summary_path: Path,
    events: list[dict[str, Any]],
) -> str:
    counts = Counter(operational_alert_type(event) for event in events)
    del summary, summary_path
    lines = [
        "Интернет-заказы: требуется действие",
        f"Новых сигналов: {format_ru_count(len(events), 'сигнал', 'сигнала', 'сигналов')}",
        "Что видно:",
        *format_operational_alert_summary(counts),
        "Примеры:",
        *format_operational_examples(events),
        "Ответственный: менеджер сделки; если он не определён — руководитель интернет-заказов.",
    ]
    return "\n".join(lines)


def manual_review_label(reason: str) -> str:
    return MANUAL_REVIEW_LABELS.get(reason, "нужна ручная проверка")


def manual_review_action(reason: str) -> str:
    return MANUAL_REVIEW_ACTIONS.get(reason, "Открыть заказ и определить следующий шаг вручную.")


def format_manual_review_summary(counts: Counter, *, limit: int = 5) -> list[str]:
    if not counts:
        return ["- новых случаев нет"]
    return [
        f"- {manual_review_label(reason)}: {count}. {manual_review_action(reason)}"
        for reason, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]
    ]


def format_manual_review_examples(examples: list[dict[str, Any]], *, limit: int = 5) -> list[str]:
    lines: list[str] = []
    for example in examples:
        if not isinstance(example, dict):
            continue
        order_number = clean_csv_value(example.get("site_order_number"))
        deal_id = clean_csv_value(example.get("bitrix_deal_id"))
        if order_number in {"", "-"} and deal_id in {"", "-"}:
            continue
        reason = clean_csv_value(example.get("reason")) or "manual_review"
        lines.append(
            f"- заказ {order_number or '-'} / сделка {deal_id or '-'}: "
            f"{manual_review_label(reason)}"
        )
        if len(lines) >= limit:
            break
    return lines


def operational_alert_type(event: dict[str, Any]) -> str:
    example = event.get("example") if isinstance(event.get("example"), dict) else {}
    alert_type = clean_csv_value(example.get("alert_type"))
    if alert_type:
        return alert_type
    key = clean_csv_value(event.get("key"))
    if "|" in key:
        return key.split("|", 1)[0]
    return key or "operational_alert"


def operational_alert_label(alert_type: str) -> str:
    return OPERATIONAL_ALERT_LABELS.get(alert_type, "прочие сигналы")


def operational_alert_action(alert_type: str) -> str:
    return OPERATIONAL_ALERT_ACTIONS.get(alert_type, "Открыть подробный отчет и разобрать вручную.")


def format_operational_alert_summary(counts: Counter, *, limit: int = 5) -> list[str]:
    if not counts:
        return ["- новых проблем нет"]
    lines: list[str] = []
    for alert_type, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]:
        lines.append(
            f"- {operational_alert_label(alert_type)}: {count}. "
            f"{operational_alert_action(alert_type)}"
        )
    return lines


def format_operational_alert_counts(counts: Counter, *, limit: int = 5) -> str:
    if not counts:
        return "-"
    parts = [
        f"{operational_alert_label(alert_type)}: {count}"
        for alert_type, count in sorted(
            counts.items(),
            key=lambda item: (-item[1], operational_alert_label(item[0])),
        )[:limit]
    ]
    return "; ".join(parts)


def format_operational_examples(events: list[dict[str, Any]], *, limit: int = 5) -> list[str]:
    lines: list[str] = []
    for event in events[:limit]:
        lines.append(format_operational_example(event))
    return lines or ["- нет примеров"]


def format_operational_example(event: dict[str, Any]) -> str:
    alert_type = operational_alert_type(event)
    example = event.get("example") if isinstance(event.get("example"), dict) else {}
    key_parts = clean_csv_value(event.get("key")).split("|")
    order_number = clean_csv_value(example.get("site_order_number"))
    deal_id = clean_csv_value(example.get("bitrix_deal_id"))
    stage_id = clean_csv_value(example.get("stage_id"))
    count = clean_csv_value(example.get("count"))

    if alert_type == "stage_count":
        stage_id = stage_id or (key_parts[1] if len(key_parts) > 1 else "")
        count = count or (key_parts[2] if len(key_parts) > 2 else "")
        stage_label = STAGE_RU_LABELS.get(stage_id, stage_id or "стадии")
        order_count = int(count or 1)
        return (
            f"- стадия «{stage_label}»: "
            f"{format_ru_count(order_count, 'заказ', 'заказа', 'заказов')} требует проверки"
        )

    if alert_type == "rtu_without_assembled":
        order_number = order_number or (key_parts[1] if len(key_parts) > 1 else "")
        deal_id = deal_id or (key_parts[2] if len(key_parts) > 2 else "")
        return (
            f"- заказ {order_number or '-'} / сделка {deal_id or '-'}: "
            "в 1С есть реализация, но нет события «Собран»"
        )

    if alert_type == "overdue_prepayment":
        order_number = order_number or (key_parts[1] if len(key_parts) > 1 else "")
        deal_id = deal_id or (key_parts[2] if len(key_parts) > 2 else "")
        return (
            f"- заказ {order_number or '-'} / сделка {deal_id or '-'}: " "оплату ждут больше 7 дней"
        )

    if alert_type == "pickup_waiting_close_candidate":
        order_number = order_number or (key_parts[1] if len(key_parts) > 1 else "")
        deal_id = deal_id or (key_parts[2] if len(key_parts) > 2 else "")
        return (
            f"- заказ {order_number or '-'} / сделка {deal_id or '-'}: "
            "сайт показывает «Выполнен», нужно проверить оплату/долг в 1С"
        )

    if alert_type == "outbox_error":
        order_number = order_number or (key_parts[1] if len(key_parts) > 1 else "")
        deal_id = deal_id or (key_parts[2] if len(key_parts) > 2 else "")
        return (
            f"- заказ {order_number or '-'} / сделка {deal_id or '-'}: "
            "Bitrix не принял автоматическое обновление"
        )

    reason = clean_csv_value(example.get("reason")) or "нужен ручной разбор"
    return f"- {reason}"


def format_ru_count(count: int, one: str, few: str, many: str) -> str:
    count_abs = abs(int(count))
    tail = count_abs % 100
    if 11 <= tail <= 14:
        word = many
    else:
        last = count_abs % 10
        if last == 1:
            word = one
        elif 2 <= last <= 4:
            word = few
        else:
            word = many
    return f"{count} {word}"


def paths_for_events(events: list[dict[str, Any]], field: str) -> list[str]:
    return unique_values(
        event.get("item", {}).get(field) for event in events if isinstance(event.get("item"), dict)
    )


def format_paths(paths: list[str], *, limit: int = 2) -> str:
    if not paths:
        return "-"
    shown = paths[:limit]
    suffix = f"; +{len(paths) - limit}" if len(paths) > limit else ""
    return "; ".join(shown) + suffix


def format_top_counts(counts: Counter, *, limit: int = 5) -> str:
    if not counts:
        return "-"
    parts = [
        f"{label}: {count}"
        for label, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]
    ]
    return "; ".join(parts)


def format_examples(examples: list[dict[str, Any]], *, limit: int = 5) -> list[str]:
    lines: list[str] = []
    for example in examples[:limit]:
        if not isinstance(example, dict):
            continue
        order_number = clean_csv_value(example.get("site_order_number")) or "-"
        deal_id = clean_csv_value(example.get("bitrix_deal_id")) or "-"
        reason = (
            clean_csv_value(example.get("reason")) or clean_csv_value(example.get("result")) or "-"
        )
        lines.append(f"- заказ {order_number} / сделка {deal_id}: {reason}")
    return lines or ["- нет примеров"]


def auto_apply_label(summary: dict[str, Any]) -> str:
    return "выключен" if bool(summary.get("dry_run", True)) else "включен"


def auto_apply_label_ru(summary: dict[str, Any]) -> str:
    if bool(summary.get("dry_run", True)):
        return "выключено, CRM не менялась"
    return "включено"


def has_daily_item(summary: dict[str, Any]) -> bool:
    return any(item.get("mode") == "daily" for item in summary.get("items") or [])


def deliver_daily_digest(
    *,
    client: Any,
    config: NotifyConfig,
    state: dict[str, Any],
    summary: dict[str, Any],
    summary_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    result: dict[str, Any] = {"sent": [], "skipped": [], "errors": [], "state_changed": False}
    current_stamp = clean_csv_value(summary.get("stamp"))
    current_dt = parse_summary_stamp(current_stamp)
    daily_date = current_dt.date().isoformat() if current_dt else date.today().isoformat()
    if state.get("last_daily_digest_date") == daily_date:
        result["skipped"].append("daily_digest_already_sent")
        return result
    if not has_notification_recipient(
        user_ids=config.business_user_ids,
        dialog_id=config.site_dialog_id,
    ):
        result["skipped"].append("daily_digest_no_recipients")
        return result

    digest = build_daily_digest(output_dir, summary, summary_path, state)
    delivery = send_order_fulfillment_message(
        client=client,
        config=config,
        user_ids=config.business_user_ids,
        dialog_id=config.site_dialog_id,
        tag=f"order-fulfillment|daily|{daily_date}",
        message=digest["message"],
    )
    result["errors"].extend(delivery["errors"])
    if delivery_reached_required_target(
        delivery,
        required_dialog_id=config.site_dialog_id,
    ):
        result["sent"].append({"kind": "daily_digest", "count": 1})
        state["last_daily_digest_date"] = daily_date
        state["last_daily_digest_stamp"] = current_stamp
        result["state_changed"] = True
    else:
        result["skipped"].append("daily_digest_delivery_failed")
    return result


def build_daily_digest(
    output_dir: Path,
    current_summary: dict[str, Any],
    current_summary_path: Path,
    state: dict[str, Any],
) -> dict[str, Any]:
    summaries = load_daily_digest_summaries(
        output_dir=output_dir,
        current_summary=current_summary,
        current_summary_path=current_summary_path,
        last_stamp=clean_csv_value(state.get("last_daily_digest_stamp")),
    )
    current_stamp = clean_csv_value(current_summary.get("stamp"))
    current_dt = parse_summary_stamp(current_stamp) or datetime.now()
    last_dt = parse_summary_stamp(clean_csv_value(state.get("last_daily_digest_stamp")))
    period_start = last_dt or (current_dt - timedelta(hours=24))
    onec_activity = load_onec_assembly_crm_activity(
        period_start=period_start,
        period_end=current_dt,
    )
    applied_transitions = daily_applied_crm_transitions(summaries)
    crm_transitions = set(applied_transitions)
    crm_transitions.update(onec_activity["crm_transitions"])

    latest_quick = latest_applied_quick_item(summaries)
    operational_keys = set(latest_quick.get("operational_alert_keys") or [])
    overdue_orders = daily_overdue_payment_orders(operational_keys)
    technical_errors = daily_current_technical_errors(latest_quick)
    action_count = len(overdue_orders) + len(technical_errors)
    stage_summary_error_count = sum(
        int(item.get("stage_summary_error_count") or 0)
        for item in current_summary.get("items") or []
        if item.get("mode") == "daily"
    )

    lines = [
        "MASTER-MOBILE.RU: контроль интернет-заказов",
        "Автоматически обработано за период:",
        "- подтверждена сборка: "
        f"{format_ru_count(len(onec_activity['assembled_orders']), 'заказ', 'заказа', 'заказов')};",
        "- подтверждена выдача: "
        f"{format_ru_count(len(onec_activity['issued_orders']), 'заказ', 'заказа', 'заказов')};",
        "- выполнено переходов CRM: "
        f"{format_ru_count(len(crm_transitions), 'переход', 'перехода', 'переходов')}.",
    ]
    if not action_count:
        lines.append("Ручных действий сегодня не требуется.")
    else:
        lines.append(
            "Требуется вмешательство: "
            f"{format_ru_count(action_count, 'заказ', 'заказа', 'заказов')}."
        )
        action_lines: list[str] = []
        for order_number, deal_id in overdue_orders:
            action_lines.append(
                f"- заказ {order_number} / сделка {deal_id}: проверить просроченную оплату."
            )
        for error in technical_errors:
            action_lines.append(daily_technical_error_line(error))
        lines.extend(action_lines[:5])
        if len(action_lines) > 5:
            lines.append(f"- ещё {len(action_lines) - 5}")
    if stage_summary_error_count:
        lines.append(
            "Техническое примечание: Bitrix временно не отдал статистику по части стадий; "
            "операционная сводка сформирована по сохранённым результатам проверок."
        )
    return {"message": "\n".join(lines), "summary_count": len(summaries)}


def load_onec_assembly_crm_activity(
    *,
    period_start: datetime,
    period_end: datetime,
    state_path: Path | None = None,
) -> dict[str, set[Any]]:
    path = state_path or Path(
        os.environ.get("ONEC_ASSEMBLY_CRM_STATE_PATH") or DEFAULT_ONEC_ASSEMBLY_CRM_STATE_PATH
    )
    result: dict[str, set[Any]] = {
        "assembled_orders": set(),
        "issued_orders": set(),
        "crm_transitions": set(),
    }
    if not path.exists():
        return result
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        rows = connection.execute(
            """
            SELECT event_key, site_order_number, crm_response
            FROM processed_events
            WHERE processed_at > ? AND processed_at <= ?
            """,
            (
                period_start.strftime("%Y-%m-%d %H:%M:%S"),
                period_end.strftime("%Y-%m-%d %H:%M:%S"),
            ),
        ).fetchall()
        connection.close()
    except (OSError, sqlite3.Error):
        return result
    for event_key, order_number, raw_response in rows:
        order = clean_csv_value(order_number)
        if not order:
            continue
        event = clean_csv_value(event_key)
        if event.startswith("assembled:"):
            result["assembled_orders"].add(order)
        elif event.startswith("issued-scan:"):
            result["issued_orders"].add(order)
        try:
            response = json.loads(raw_response or "{}")
        except (TypeError, json.JSONDecodeError):
            response = {}
        stage_from = clean_csv_value(response.get("stage_from"))
        stage_to = clean_csv_value(response.get("stage_to"))
        deal_id = clean_csv_value(response.get("deal_id"))
        if (
            stage_to
            and stage_to != stage_from
            and clean_csv_value(response.get("action")).startswith("moved_to_")
        ):
            result["crm_transitions"].add((order, deal_id, stage_to))
    return result


def daily_applied_crm_transitions(
    summaries: list[tuple[Path, dict[str, Any]]],
) -> set[tuple[str, str, str]]:
    transitions: set[tuple[str, str, str]] = set()
    for _, summary in summaries:
        for item in summary.get("items") or []:
            for row in read_csv_dicts(item.get("apply_result")):
                if clean_csv_value(row.get("applied")) != "1":
                    continue
                order_number = clean_csv_value(row.get("site_order_number"))
                deal_id = clean_csv_value(row.get("bitrix_deal_id"))
                target_stage = clean_csv_value(row.get("target_stage"))
                if order_number and deal_id and target_stage:
                    transitions.add((order_number, deal_id, target_stage))
    return transitions


def latest_applied_quick_item(
    summaries: list[tuple[Path, dict[str, Any]]],
) -> dict[str, Any]:
    candidates: list[tuple[str, dict[str, Any]]] = []
    for _, summary in summaries:
        stamp = clean_csv_value(summary.get("stamp"))
        for item in summary.get("items") or []:
            if item.get("mode") == "quick" and not bool(item.get("dry_run", True)):
                candidates.append((stamp, item))
    return max(candidates, key=lambda entry: entry[0])[1] if candidates else {}


def daily_current_technical_errors(item: dict[str, Any]) -> list[dict[str, str]]:
    errors: dict[tuple[str, str, str], dict[str, str]] = {}
    for row in read_csv_dicts(item.get("apply_result")):
        if clean_csv_value(row.get("result")) not in TECHNICAL_APPLY_RESULTS:
            continue
        order_number = clean_csv_value(row.get("site_order_number"))
        deal_id = clean_csv_value(row.get("bitrix_deal_id"))
        target_stage = clean_csv_value(row.get("target_stage"))
        if not order_number or not deal_id:
            continue
        errors[(order_number, deal_id, target_stage)] = {
            "site_order_number": order_number,
            "bitrix_deal_id": deal_id,
            "target_stage": target_stage,
            "reason": clean_csv_value(row.get("reason")),
        }
    return sorted(
        errors.values(),
        key=lambda row: (
            len(row["site_order_number"]),
            row["site_order_number"],
            len(row["bitrix_deal_id"]),
            row["bitrix_deal_id"],
        ),
    )


def daily_technical_error_line(error: dict[str, str]) -> str:
    target_stage = clean_csv_value(error.get("target_stage"))
    stage_label = STAGE_RU_LABELS.get(target_stage, target_stage or "следующую стадию")
    reason = clean_csv_value(error.get("reason")).lower()
    explanation = (
        "конфликт количества товаров в заказе и отгрузках"
        if "распределен по отгрузкам" in reason or "распределён по отгрузкам" in reason
        else "CRM не приняла автоматическое обновление"
    )
    return (
        f"- заказ {error['site_order_number']} / сделка {error['bitrix_deal_id']}: "
        f"не выполнен переход в «{stage_label}» — {explanation}."
    )


def daily_overdue_payment_orders(operational_keys: set[str]) -> list[tuple[str, str]]:
    orders: set[tuple[str, str]] = set()
    for key in operational_keys:
        parts = key.split("|")
        if len(parts) < 3 or parts[0] != "overdue_prepayment":
            continue
        order_number = clean_csv_value(parts[1])
        deal_id = clean_csv_value(parts[2])
        if not order_number or order_number == "-" or not deal_id or deal_id == "-":
            continue
        orders.add((order_number, deal_id))
    return sorted(orders, key=lambda item: (len(item[0]), item[0], len(item[1]), item[1]))


def load_daily_digest_summaries(
    *,
    output_dir: Path,
    current_summary: dict[str, Any],
    current_summary_path: Path,
    last_stamp: str,
) -> list[tuple[Path, dict[str, Any]]]:
    current_stamp = clean_csv_value(current_summary.get("stamp"))
    current_dt = parse_summary_stamp(current_stamp)
    last_dt = parse_summary_stamp(last_stamp)
    cutoff_dt = None if last_dt else ((current_dt or datetime.now()) - timedelta(hours=24))
    summaries: list[tuple[Path, dict[str, Any]]] = []

    for path in sorted(output_dir.glob("order-fulfillment-sync-summary-*.json")):
        payload = load_summary_json(path)
        if not payload:
            continue
        stamp = clean_csv_value(payload.get("stamp"))
        stamp_dt = parse_summary_stamp(stamp)
        if last_dt and stamp_dt and stamp_dt <= last_dt:
            continue
        if current_dt and stamp_dt and stamp_dt > current_dt:
            continue
        if cutoff_dt and stamp_dt and stamp_dt < cutoff_dt:
            continue
        summaries.append((path, payload))

    if not any(path == current_summary_path for path, _ in summaries):
        summaries.append((current_summary_path, current_summary))
    return summaries


def load_summary_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def parse_summary_stamp(stamp: str) -> datetime | None:
    if not stamp:
        return None
    try:
        return datetime.strptime(stamp, "%Y%m%d-%H%M%S")
    except ValueError:
        return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("quick", "chat", "daily", "all"), default="all")
    parser.add_argument("--apply", action="store_true", help="Actually update Bitrix stages.")
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--new-limit", type=int, default=500)
    parser.add_argument("--site-chat-limit", type=int, default=50)
    parser.add_argument("--courier-chat-limit", type=int, default=10)
    parser.add_argument("--review-limit", type=int, default=100)
    parser.add_argument("--unknown-date-from", type=date.fromisoformat, default=None)
    return parser.parse_args()


def main() -> int:
    apply_env_defaults(load_env_files())
    get_settings.cache_clear()
    args = parse_args()
    settings = get_settings()
    output_dir = args.output_dir or Path(settings.order_fulfillment_artifact_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    webhook_url = resolve_bitrix_webhook_url()
    if not webhook_url:
        raise SystemExit(
            "ORDER_FULFILLMENT_BITRIX_WEBHOOK_URL or BITRIX_BOX_WEBHOOK_BASE is missing"
        )
    client = fulfillment.BitrixChatClient(webhook_url)
    summaries: list[dict[str, Any]] = []
    if args.mode in {"quick", "all"}:
        summaries.append(
            run_quick_sync(
                client=client,
                output_dir=output_dir,
                stamp=stamp,
                apply=args.apply,
                limit=args.new_limit,
            )
        )
    if args.mode in {"chat", "all"}:
        summaries.append(
            run_chat_sync(
                client=client,
                output_dir=output_dir,
                stamp=stamp,
                apply=args.apply,
                site_limit=args.site_chat_limit,
                courier_limit=args.courier_chat_limit,
                review_limit=args.review_limit,
            )
        )
    if args.mode in {"daily", "all"}:
        summaries.append(
            run_daily_sync(
                client=client,
                output_dir=output_dir,
                stamp=stamp,
                unknown_date_from=args.unknown_date_from,
            )
        )
    summary = {
        "stamp": stamp,
        "mode": args.mode,
        "dry_run": not args.apply,
        "items": summaries,
    }
    summary_path = output_dir / f"order-fulfillment-sync-summary-{stamp}.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        notifications = deliver_order_fulfillment_notifications(
            client=client,
            summary=summary,
            summary_path=summary_path,
            output_dir=output_dir,
            settings=settings,
        )
    except Exception as exc:  # noqa: BLE001 - notification failures must not stop sync.
        notifications = {
            "enabled": bool(settings.order_fulfillment_notify_enabled),
            "sent": [],
            "skipped": [],
            "errors": [f"{type(exc).__name__}: notification delivery failed"],
        }
    summary["notifications"] = notifications
    if notifications.get("errors"):
        summary["notification_errors"] = notifications["errors"]
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"summary": str(summary_path), **summary}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
