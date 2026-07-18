"""Ежемесячная инвентаризация типа цен 2.Бронзовый.

Реализует регламент
reports/retail_price_types/customer-price-type-automation/2026-07-10/
monthly-price-type-inventory-rule-total10k-2026-07-10.md
(ветки: корзины правила 10к/3.3к, спящие клиенты, реестр служебных карточек).

Источники (только чтение):
- 1С: договоры покупателей с типом цен `2.Бронзовый*` (справочники
  `_Reference37`/`_Reference54`/`_Reference87`);
- локальная витрина `receivable_ledger_event`: чистые продажи по месяцам.

Выход: CSV-списки и summary в
reports/retail_price_types/customer-price-type-automation/auto/<месяц>/.
Типы цен нигде не изменяются: скрипт только считает и формирует списки.
"""

from __future__ import annotations

import argparse
import csv
import glob
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, text
from sqlalchemy.orm import Session

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.infrastructure.db import (  # noqa: E402
    build_onec_engine_from_settings,
    get_application_engine,
)
from app.models import ReceivableLedgerEvent  # noqa: E402

REPORTS_DIR = REPO_ROOT / "reports/retail_price_types/customer-price-type-automation"
RULE_TOTAL_3M = Decimal("10000")
RULE_LAST_MONTH = Decimal("3300")
BUYERS_CONTRACT_KIND_REF = "0x9363c6f0a10557bf4822a55db4862286"
SOURCE_LAYER = "regular_receivables"

# Корневая группа контрагентов "ПОКУПАТЕЛИ": контур работает только по ней.
BUYERS_ROOT_GROUP_REF = "0x859f00215d1c454811df26d4ab62d095"

BRONZE_CONTRACTS_SQL = text(f"""
    WITH buyers_groups AS (
        SELECT _IDRRef FROM _Reference54 WITH (NOLOCK)
        WHERE _IDRRef = CONVERT(varbinary(16), '{BUYERS_ROOT_GROUP_REF}', 1)
        UNION ALL
        SELECT child._IDRRef FROM _Reference54 AS child WITH (NOLOCK)
        JOIN buyers_groups AS parent ON child._ParentIDRRef = parent._IDRRef
        WHERE child._Folder = 0x00
    )
    SELECT
        master.dbo.fn_varbintohexstr(cp._IDRRef) AS counterparty_ref,
        cp._Code AS counterparty_code,
        cp._Description AS counterparty_name,
        pt._Description AS price_type_name
    FROM _Reference37 AS c WITH (NOLOCK)
    JOIN _Reference87 AS pt WITH (NOLOCK)
        ON pt._IDRRef = c._Fld513_RRRef
    JOIN _Reference54 AS cp WITH (NOLOCK)
        ON cp._IDRRef = c._OwnerIDRRef
    WHERE c._Marked = 0x00
      AND cp._Marked = 0x00
      AND cp._ParentIDRRef IN (SELECT _IDRRef FROM buyers_groups)
      AND pt._Description LIKE N'2.Бронзовый%'
      AND master.dbo.fn_varbintohexstr(c._Fld515RRef) = :kind_ref
    """)


@dataclass
class BronzeClient:
    code: str
    name: str
    price_types: set[str] = field(default_factory=set)
    monthly_net: dict[str, Decimal] = field(default_factory=dict)


def _month_arg(value: str) -> date:
    return datetime.strptime(value, "%Y-%m").date().replace(day=1)


def _add_months(value: date, months: int) -> date:
    total = value.year * 12 + (value.month - 1) + months
    return date(total // 12, total % 12 + 1, 1)


def _month_key(value: date) -> str:
    return value.strftime("%Y-%m")


def _last_closed_month(today: date) -> date:
    return _add_months(today.replace(day=1), -1)


def load_bronze_clients() -> dict[str, BronzeClient]:
    engine = build_onec_engine_from_settings()
    clients: dict[str, BronzeClient] = {}
    with engine.connect() as conn:
        for row in conn.execute(BRONZE_CONTRACTS_SQL, {"kind_ref": BUYERS_CONTRACT_KIND_REF}):
            ref = (row.counterparty_ref or "").strip().lower()
            if not ref:
                continue
            client = clients.setdefault(
                ref,
                BronzeClient(
                    code=(row.counterparty_code or "").strip(),
                    name=" ".join((row.counterparty_name or "").split()),
                ),
            )
            client.price_types.add((row.price_type_name or "").strip())
    return clients


def load_monthly_net(clients: dict[str, BronzeClient], *, period_end_exclusive: date) -> None:
    """Чистые продажи клиента за месяц: max(0, реализации - возвраты)."""
    engine = get_application_engine()
    month_expr = func.to_char(ReceivableLedgerEvent.external_document_date, "YYYY-MM")
    raw: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    with Session(engine) as session:
        rows = (
            session.query(
                ReceivableLedgerEvent.counterparty_ref,
                month_expr.label("month"),
                ReceivableLedgerEvent.event_type,
                func.sum(ReceivableLedgerEvent.amount_delta).label("amount"),
            )
            .filter(
                ReceivableLedgerEvent.external_document_date
                < datetime.combine(period_end_exclusive, datetime.min.time()),
                ReceivableLedgerEvent.event_type.in_(("sale", "return")),
                ReceivableLedgerEvent.source_layer == SOURCE_LAYER,
            )
            .group_by(
                ReceivableLedgerEvent.counterparty_ref,
                month_expr,
                ReceivableLedgerEvent.event_type,
            )
            .all()
        )
    for row in rows:
        ref = (row.counterparty_ref or "").strip().lower()
        if ref not in clients:
            continue
        amount = Decimal(str(row.amount or 0))
        if row.event_type == "return":
            amount = -abs(amount)
        raw[(ref, row.month)] += amount
    for (ref, month), amount in raw.items():
        clients[ref].monthly_net[month] = max(Decimal("0"), amount)


# Из расчета исключаются только эти классы реестра; безымянные точки участвуют
# в расчете как обычные клиенты (решение 2026-07-17). Карточки сотрудников -
# внутренние операции, не клиенты (решение 2026-07-18).
EXCLUDED_REGISTRY_CLASSES = {
    "служебный инструмент",
    "фиктивная/техническая",
    "карточка сотрудника",
}


def load_service_codes() -> set[str]:
    codes: set[str] = set()
    for path in glob.glob(str(REPORTS_DIR / "*" / "service-cards-registry-*.csv")):
        with open(path, encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                code = (row.get("код_1с") or "").strip()
                cls = (row.get("класс") or "").strip()
                if code and cls in EXCLUDED_REGISTRY_CLASSES:
                    codes.add(code)
    return codes


def build_inventory(rule_month: date, out_dir: Path) -> dict[str, int]:
    clients = load_bronze_clients()
    load_monthly_net(clients, period_end_exclusive=_add_months(rule_month, 1))
    service_codes = load_service_codes()

    window = [_month_key(_add_months(rule_month, delta)) for delta in (-2, -1, 0)]
    rule_key = _month_key(rule_month)

    buckets: dict[str, list[list[str]]] = defaultdict(list)
    for _ref, client in sorted(clients.items(), key=lambda item: item[1].code):
        if client.code in service_codes:
            buckets["служебная_карточка"].append(
                [client.code, client.name, "", "", "реестр служебных: вне расчета"]
            )
            continue
        total_3m = sum(
            (client.monthly_net.get(month, Decimal("0")) for month in window),
            Decimal("0"),
        )
        last_month = client.monthly_net.get(rule_key, Decimal("0"))
        active_months = {month for month, net in client.monthly_net.items() if net > 0}
        row = [client.code, client.name, f"{total_3m:.2f}", f"{last_month:.2f}"]
        if last_month > 0 or total_3m > 0:
            if total_3m >= RULE_TOTAL_3M:
                buckets["норма"].append([*row, "оставить 2.Бронзовый"])
            elif any(
                client.monthly_net.get(month, Decimal("0")) >= RULE_TOTAL_3M for month in window
            ):
                buckets["контроль_противоречия"].append(
                    [*row, "итог 3м < 10к, но был месяц 10к+: проверить данные"]
                )
            elif last_month >= RULE_LAST_MONTH:
                buckets["удержание_дожим"].append([*row, "дожать до порога, тип цен не менять"])
            else:
                buckets["изолятор_1м"].append([*row, "лечебный месяц CRM, тип цен не менять"])
        elif active_months:
            last_active = max(active_months)
            months_ago = (
                rule_month.year * 12
                + rule_month.month
                - int(last_active[:4]) * 12
                - int(last_active[5:7])
            )
            wave = min(4, max(1, (months_ago - 2)))
            buckets["спящие_реанимация"].append(
                [
                    client.code,
                    client.name,
                    last_active,
                    f"{sum(client.monthly_net.values(), Decimal('0')):.2f}",
                    f"волна {wave}: CRM-реанимация, условия сохраняются",
                ]
            )
        else:
            buckets["без_продаж_в_витрине"].append(
                [client.code, client.name, "", "", "нет продаж в покрытии витрины"]
            )

    out_dir.mkdir(parents=True, exist_ok=True)
    header = ["код_1с", "контрагент", "итог_3м", "последний_месяц", "решение"]
    sleeping_header = [
        "код_1с",
        "контрагент",
        "последняя_покупка",
        "продажи_в_покрытии",
        "решение",
    ]
    for bucket, rows in buckets.items():
        path = out_dir / f"bronze-{bucket}-{rule_key}.csv"
        with open(path, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(sleeping_header if bucket == "спящие_реанимация" else header)
            writer.writerows(rows)

    counts = {bucket: len(rows) for bucket, rows in sorted(buckets.items())}
    summary = out_dir / f"bronze-monthly-summary-{rule_key}.md"
    with open(summary, "w", encoding="utf-8") as handle:
        handle.write(
            f"# Автоинвентаризация 2.Бронзовый за {rule_key}\n\n"
            f"Сформировано: {datetime.now():%Y-%m-%d %H:%M}. Типы цен не менялись.\n\n"
            "| Корзина | Клиентов |\n| --- | ---: |\n"
        )
        for bucket, count in counts.items():
            handle.write(f"| {bucket} | {count} |\n")
        handle.write(
            "\nОграничение: спящие определяются в пределах покрытия витрины "
            "продаж; полная 12-месячная история появится по мере накопления.\n"
        )
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--month",
        type=_month_arg,
        default=None,
        help="расчетный (последний закрытый) месяц в формате YYYY-MM",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="каталог результата (по умолчанию reports/.../auto/<месяц>)",
    )
    args = parser.parse_args(argv)
    rule_month = args.month or _last_closed_month(date.today())
    out_dir = args.output_dir or REPORTS_DIR / "auto" / _month_key(rule_month)
    counts = build_inventory(rule_month, out_dir)
    print(f"месяц {_month_key(rule_month)} -> {out_dir}")
    for bucket, count in counts.items():
        print(f"  {bucket}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
