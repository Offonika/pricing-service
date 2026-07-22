"""Ежемесячная read-only инвентаризация типов цен по всем уровням лестницы.

Источник истины нормативов: config/price_types/ruleset.yaml (уровни, пороги
удержания, исключения). Реализует регламент
reports/retail_price_types/customer-price-type-automation/2026-07-10/
monthly-price-type-inventory-rule-total10k-2026-07-10.md.

Источники данных (только чтение):
- 1С: один bulk-срез всех живых договоров покупателей группы ПОКУПАТЕЛИ;
- локальная витрина `receivable_ledger_event`: чистые продажи по месяцам.

Выход: CSV-списки по корзинам каждого уровня и summary в
reports/retail_price_types/customer-price-type-automation/auto/<месяц>/.
Типы цен нигде не изменяются: скрипт только считает и формирует списки.
Application DB также не изменяется без отдельного явного ``--persist``.

Имя файла историческое (первая версия покрывала только бронзу) и сохранено
ради установленного cron; решения принимает единый domain rules engine.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import yaml  # noqa: E402

from app.domains.customer_price_types import (  # noqa: E402
    CustomerPriceTypeFacts,
    CustomerPriceTypeRulesEngine,
    load_price_type_ruleset,
)
from app.infrastructure.customer_price_type_sources import (  # noqa: E402
    CustomerPriceTypeBulkSource,
    CustomerPriceTypeSourceEnrichments,
)
from app.infrastructure.db import (  # noqa: E402
    build_onec_engine_from_settings,
    get_application_session_factory,
)
from app.services.customer_price_types import CustomerPriceTypeRunService  # noqa: E402
from app.services.receivable_decision_onec_metrics import (  # noqa: E402
    fetch_counterparty_payment_form_metrics_from_onec,
    fetch_counterparty_profitability_metrics_from_onec,
)

REPORTS_DIR = REPO_ROOT / "reports/retail_price_types/customer-price-type-automation"
RULESET_PATH = REPO_ROOT / "config/price_types/ruleset.yaml"
RULESET = yaml.safe_load(RULESET_PATH.read_text(encoding="utf-8"))
LEVELS: dict[str, dict] = RULESET["levels"]
BUYERS_ROOT_GROUP_REF: str = RULESET["population"]["buyers_root_group_ref"]
BUYERS_CONTRACT_KIND_REF: str = RULESET["population"]["contract_kind_ref"]
DOMAIN_RULESET = load_price_type_ruleset(RULESET_PATH)
DOMAIN_ENGINE = CustomerPriceTypeRulesEngine(DOMAIN_RULESET)


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


def _json_ready(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value.quantize(Decimal("0.01")), "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    return value


def _build_enrichment_loader(onec_engine):
    def load(
        snapshot_month: date,
        counterparty_refs: list[str] | tuple[str, ...],
    ) -> CustomerPriceTypeSourceEnrichments:
        snapshot_date = _add_months(snapshot_month, 1) - date.resolution
        profitability = fetch_counterparty_profitability_metrics_from_onec(
            onec_engine,
            snapshot_date=snapshot_date,
            counterparty_refs=counterparty_refs,
        )
        payment_forms = fetch_counterparty_payment_form_metrics_from_onec(
            onec_engine,
            snapshot_date=snapshot_date,
            counterparty_refs=counterparty_refs,
        )
        economics: dict[str, dict[str, Any]] = {}
        returns: dict[str, dict[str, Any]] = {}
        for raw_ref, metrics in profitability.items():
            ref = str(raw_ref).strip().lower()
            payload = _json_ready(asdict(metrics))
            payload["status"] = metrics.source_status
            economics[ref] = payload
            defect_returns = metrics.defect_return_amount_90 or Decimal("0")
            revenue = metrics.revenue_90 or Decimal("0")
            returns[ref] = {
                "source_status": metrics.source_status,
                "defect_return_amount_90": _json_ready(defect_returns),
                "return_rate_pct": (
                    _json_ready(defect_returns / revenue * Decimal("100")) if revenue > 0 else None
                ),
                "review_type": ("data_check" if defect_returns > 0 and revenue <= 0 else None),
                "source_note": metrics.source_note,
            }
        payments = {
            str(raw_ref).strip().lower(): _json_ready(asdict(metrics))
            for raw_ref, metrics in payment_forms.items()
        }
        return CustomerPriceTypeSourceEnrichments(
            economics=economics,
            payments=payments,
            return_signals=returns,
        )

    return load


def _level_for_fact(fact: CustomerPriceTypeFacts) -> str | None:
    raw_types = [str(contract.price_type_name or "").casefold() for contract in fact.contracts]
    matches = {
        level.key
        for level in DOMAIN_RULESET.levels
        if any(raw.startswith(level.price_type_prefix.casefold()) for raw in raw_types)
    }
    if any(
        raw.startswith(prefix.casefold())
        for raw in raw_types
        for prefix in DOMAIN_RULESET.retail_prefixes
    ):
        matches.add("retail")
    return next(iter(matches)) if len(matches) == 1 else None


def _bucket(recommendation: str, *, excluded: bool) -> str:
    if excluded:
        return "служебная_карточка"
    return {
        "keep_current": "норма",
        "informational_upgrade_candidate": "информационный_кандидат",
        "manager_retention": "удержание_дожим",
        "isolate": "изолятор_1м",
        "recovery": "спящие_реанимация",
        "downgrade_to_retail": "спящие_реанимация",
        "data_check": "сверка_данных",
        "special_review": "ручная_проверка",
    }.get(recommendation.split(":", 1)[0], "ручная_проверка")


def _collect_facts(rule_month: date) -> list[CustomerPriceTypeFacts]:
    onec_engine = build_onec_engine_from_settings()
    factory = get_application_session_factory()
    try:
        with factory() as session:
            return CustomerPriceTypeBulkSource(
                onec_engine=onec_engine,
                application_session=session,
                buyers_root_group_ref=BUYERS_ROOT_GROUP_REF,
                contract_kind_ref=BUYERS_CONTRACT_KIND_REF,
                key_account_price_type_prefixes=DOMAIN_RULESET.key_account_prefixes,
                enrichment_loader=_build_enrichment_loader(onec_engine),
            ).collect(snapshot_month=rule_month)
    finally:
        onec_engine.dispose()


def build_inventory(
    rule_month: date,
    out_dir: Path,
    level_keys: list[str],
    *,
    persist: bool = False,
    run_key: str | None = None,
) -> dict[str, dict[str, int]]:
    facts = _collect_facts(rule_month)
    rows_by_level: dict[str, dict[str, list[list[str]]]] = defaultdict(lambda: defaultdict(list))
    for fact in facts:
        decision = DOMAIN_ENGINE.evaluate(fact)
        level_key = decision.current_level or _level_for_fact(fact) or "unknown"
        if level_key not in level_keys:
            continue
        bucket = _bucket(decision.recommendation, excluded=decision.excluded)
        rows_by_level[level_key][bucket].append(
            [
                fact.counterparty_code or "",
                fact.counterparty_name or "",
                f"{decision.total_3m:.2f}",
                f"{decision.last_month:.2f}",
                decision.recommendation_reason,
            ]
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    header = ["код_1с", "контрагент", "итог_3м", "последний_месяц", "решение"]
    rule_key = _month_key(rule_month)
    results: dict[str, dict[str, int]] = {}
    for level_key in level_keys:
        results[level_key] = {}
        for bucket, rows in sorted(rows_by_level[level_key].items()):
            path = out_dir / f"{level_key}-{bucket}-{rule_key}.csv"
            with path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(header)
                writer.writerows(sorted(rows, key=lambda row: (row[0], row[1])))
            results[level_key][bucket] = len(rows)

    if persist:
        run = CustomerPriceTypeRunService(get_application_session_factory()).execute(
            facts,
            source_statuses={
                "contracts": "ready",
                "sales_history": "ready",
                "ledger_reconciliation": "ready",
                "master_data": "ready",
            },
            run_key=run_key,
        )
        print(f"persisted run_id={run.run_id} status={run.status} created={run.created}")

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
            "\nРасчет выполнен единым rules engine по 12 полным месяцам прямого "
            "read-only среза 1С со сверкой локальной ledger-витрины.\n"
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
    parser.add_argument(
        "--persist",
        action="store_true",
        help="явно сохранить run/snapshots/cases в application DB",
    )
    parser.add_argument(
        "--run-key",
        default=None,
        help="alternate technical run key; действует только вместе с --persist",
    )
    args = parser.parse_args(argv)
    if args.run_key and not args.persist:
        parser.error("--run-key requires --persist")
    rule_month = args.month or _last_closed_month(date.today())
    out_dir = args.output_dir or REPORTS_DIR / "auto" / _month_key(rule_month)
    level_keys = list(LEVELS.keys()) if args.level == "all" else [args.level]
    results = build_inventory(
        rule_month,
        out_dir,
        level_keys,
        persist=args.persist,
        run_key=args.run_key,
    )
    print(f"месяц {_month_key(rule_month)} (ruleset {RULESET['ruleset_version']}) " f"-> {out_dir}")
    for level_key, counts in results.items():
        for bucket, count in counts.items():
            print(f"  {level_key}/{bucket}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
