"""Ежемесячная инвентаризация типов цен по всем уровням лестницы.

Источник истины нормативов: config/price_types/ruleset.yaml (уровни, пороги
удержания, исключения). Реализует регламент
reports/retail_price_types/customer-price-type-automation/2026-07-10/
monthly-price-type-inventory-rule-total10k-2026-07-10.md.

Источники данных (только чтение):
- 1С: живые договоры покупателей группы ПОКУПАТЕЛИ по каждому уровню;
- локальная витрина `receivable_ledger_event`: чистые продажи по месяцам.

Выход: CSV-списки по корзинам каждого уровня и summary в
reports/retail_price_types/customer-price-type-automation/auto/<месяц>/.
Типы цен нигде не изменяются: скрипт только считает и формирует списки.

Имя файла историческое (первая версия покрывала только бронзу) и сохранено
ради установленного cron; с ruleset 2026-07-18.1 скрипт считает все уровни.
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

import yaml  # noqa: E402

from app.infrastructure.db import (  # noqa: E402
    build_onec_engine_from_settings,
    get_application_engine,
)
from app.models import ReceivableLedgerEvent  # noqa: E402

REPORTS_DIR = REPO_ROOT / "reports/retail_price_types/customer-price-type-automation"
RULESET_PATH = REPO_ROOT / "config/price_types/ruleset.yaml"
RULESET = yaml.safe_load(RULESET_PATH.read_text(encoding="utf-8"))
LEVELS: dict[str, dict] = RULESET["levels"]
BUYERS_ROOT_GROUP_REF: str = RULESET["population"]["buyers_root_group_ref"]
BUYERS_CONTRACT_KIND_REF: str = RULESET["population"]["contract_kind_ref"]
EXCLUDED_REGISTRY_CLASSES = set(RULESET["population"]["excluded_registry_classes"])
SOURCE_LAYER = "regular_receivables"

CONTRACTS_SQL = text(f"""
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
      AND pt._Description LIKE :prefix
      AND master.dbo.fn_varbintohexstr(c._Fld515RRef) = :kind_ref
    """)


@dataclass
class LevelClient:
    code: str
    name: str
    price_types: set[str] = field(default_factory=set)
    monthly_net: dict[str, Decimal] = field(default_factory=dict)


def classify_client(
    total_3m: Decimal,
    last_month: Decimal,
    *,
    retention_norm: Decimal,
    hold_threshold: Decimal,
) -> str:
    """Чистая функция правила уровня (тестируется на границах).

    Возвращает корзину: норма / удержание_дожим / изолятор_1м.
    Предполагает, что клиент активен в окне (иначе - ветка спящих).
    """
    if total_3m >= retention_norm:
        return "норма"
    if last_month >= hold_threshold:
        return "удержание_дожим"
    return "изолятор_1м"


def _month_arg(value: str) -> date:
    return datetime.strptime(value, "%Y-%m").date().replace(day=1)


def _add_months(value: date, months: int) -> date:
    total = value.year * 12 + (value.month - 1) + months
    return date(total // 12, total % 12 + 1, 1)


def _month_key(value: date) -> str:
    return value.strftime("%Y-%m")


def _last_closed_month(today: date) -> date:
    return _add_months(today.replace(day=1), -1)


def load_level_clients(prefix: str) -> dict[str, LevelClient]:
    engine = build_onec_engine_from_settings()
    clients: dict[str, LevelClient] = {}
    with engine.connect() as conn:
        rows = conn.execute(
            CONTRACTS_SQL,
            {"prefix": f"{prefix}%", "kind_ref": BUYERS_CONTRACT_KIND_REF},
        )
        for row in rows:
            ref = (row.counterparty_ref or "").strip().lower()
            if not ref:
                continue
            client = clients.setdefault(
                ref,
                LevelClient(
                    code=(row.counterparty_code or "").strip(),
                    name=" ".join((row.counterparty_name or "").split()),
                ),
            )
            client.price_types.add((row.price_type_name or "").strip())
    return clients


def load_monthly_net(
    refs: set[str], *, period_end_exclusive: date
) -> dict[str, dict[str, Decimal]]:
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
        if ref not in refs:
            continue
        amount = Decimal(str(row.amount or 0))
        if row.event_type == "return":
            amount = -abs(amount)
        raw[(ref, row.month)] += amount
    result: dict[str, dict[str, Decimal]] = defaultdict(dict)
    for (ref, month), amount in raw.items():
        result[ref][month] = max(Decimal("0"), amount)
    return result


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


def build_level_inventory(
    level_key: str,
    rule_month: date,
    out_dir: Path,
    *,
    service_codes: set[str],
) -> dict[str, int]:
    level = LEVELS[level_key]
    retention_norm = Decimal(str(level["retention_norm_3m"]))
    hold_threshold = Decimal(str(level["hold_last_month"]))
    prefix = level["price_type_prefix"]

    clients = load_level_clients(prefix)
    monthly = load_monthly_net(set(clients), period_end_exclusive=_add_months(rule_month, 1))
    window = [_month_key(_add_months(rule_month, delta)) for delta in (-2, -1, 0)]
    rule_key = _month_key(rule_month)

    buckets: dict[str, list[list[str]]] = defaultdict(list)
    for ref, client in sorted(clients.items(), key=lambda item: item[1].code):
        months = monthly.get(ref, {})
        if client.code in service_codes:
            buckets["служебная_карточка"].append(
                [client.code, client.name, "", "", "реестр служебных: вне расчета"]
            )
            continue
        total_3m = sum((months.get(m, Decimal("0")) for m in window), Decimal("0"))
        last_month = months.get(rule_key, Decimal("0"))
        active_months = {m for m, net in months.items() if net > 0}
        row = [client.code, client.name, f"{total_3m:.2f}", f"{last_month:.2f}"]
        if last_month > 0 or total_3m > 0:
            bucket = classify_client(
                total_3m,
                last_month,
                retention_norm=retention_norm,
                hold_threshold=hold_threshold,
            )
            notes = {
                "норма": f"оставить {prefix}",
                "удержание_дожим": (f"дожать до {retention_norm:.0f}, тип цен не менять"),
                "изолятор_1м": "лечебный месяц CRM, тип цен не менять",
            }
            buckets[bucket].append([*row, notes[bucket]])
        elif active_months:
            last_active = max(active_months)
            months_ago = (
                rule_month.year * 12
                + rule_month.month
                - int(last_active[:4]) * 12
                - int(last_active[5:7])
            )
            wave = min(4, max(1, months_ago - 2))
            buckets["спящие_реанимация"].append(
                [
                    client.code,
                    client.name,
                    last_active,
                    f"{sum(months.values(), Decimal('0')):.2f}",
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
        path = out_dir / f"{level_key}-{bucket}-{rule_key}.csv"
        with open(path, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(sleeping_header if bucket == "спящие_реанимация" else header)
            writer.writerows(rows)
    return {bucket: len(rows) for bucket, rows in sorted(buckets.items())}


def build_inventory(
    rule_month: date, out_dir: Path, level_keys: list[str]
) -> dict[str, dict[str, int]]:
    service_codes = load_service_codes()
    results: dict[str, dict[str, int]] = {}
    for level_key in level_keys:
        results[level_key] = build_level_inventory(
            level_key, rule_month, out_dir, service_codes=service_codes
        )

    rule_key = _month_key(rule_month)
    summary = out_dir / f"price-levels-monthly-summary-{rule_key}.md"
    with open(summary, "w", encoding="utf-8") as handle:
        handle.write(
            f"# Автоинвентаризация типов цен за {rule_key}\n\n"
            f"ruleset: {RULESET['ruleset_version']}. "
            f"Сформировано: {datetime.now():%Y-%m-%d %H:%M}. "
            "Типы цен не менялись.\n\n"
            "| Уровень | Корзина | Клиентов |\n| --- | --- | ---: |\n"
        )
        for level_key, counts in results.items():
            for bucket, count in counts.items():
                handle.write(f"| {level_key} | {bucket} | {count} |\n")
        handle.write(
            "\nОграничение: спящие определяются в пределах покрытия витрины "
            "продаж; полная 12-месячная история появится по мере накопления.\n"
        )
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--month",
        type=_month_arg,
        default=None,
        help="расчетный (последний закрытый) месяц в формате YYYY-MM",
    )
    parser.add_argument(
        "--level",
        choices=[*LEVELS.keys(), "all"],
        default="all",
        help="уровень лестницы (по умолчанию все)",
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
    level_keys = list(LEVELS.keys()) if args.level == "all" else [args.level]
    results = build_inventory(rule_month, out_dir, level_keys)
    print(f"месяц {_month_key(rule_month)} (ruleset {RULESET['ruleset_version']}) " f"-> {out_dir}")
    for level_key, counts in results.items():
        for bucket, count in counts.items():
            print(f"  {level_key}/{bucket}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
