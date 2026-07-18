"""Витрина возвратов по контрагентам: брак/качество отдельно, `как новый` отдельно.

Реализует отбор из методики
reports/retail_price_types/customer-price-type-automation/2026-07-13/
counterparty-return-qualification-proposal-2026-07-13.md
(решение пользователя 2026-07-18: строить сейчас, для портрета карточки).

Read-only запрос к 1С: продажи и возвраты из регистра `_AccumRg7550`,
разбор причин возврата из документов `_Document109` (брак/качество против
`как новый`: не подошло, отказался и прочее). Типы цен и данные не изменяются.

Выход: CSV-портрет в reports/retail_price_types/customer-price-type-automation/
<дата>/customer-returns-portrait-<дата>.csv.
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import text

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.infrastructure.db import build_onec_engine_from_settings  # noqa: E402

REPORTS_DIR = REPO_ROOT / "reports/retail_price_types/customer-price-type-automation"


def load_registry_classes() -> dict[str, str]:
    """Код карточки -> класс из реестра служебных (инструменты, сотрудники...)."""
    import glob

    classes: dict[str, str] = {}
    pattern = str(REPORTS_DIR / "*" / "service-cards-registry-*.csv")
    for path in glob.glob(pattern):
        with open(path, encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                code = (row.get("код_1с") or "").strip()
                cls = (row.get("класс") or "").strip()
                if code and cls and cls != "точка без владельца":
                    classes[code] = cls
    return classes


# Регистр продаж: 0xCB - реализация, 0x6D - возврат от покупателя.
SALES_RETURNS_SQL = text("""
    SELECT
        master.dbo.fn_varbintohexstr(cp._IDRRef) AS counterparty_ref,
        cp._Code AS counterparty_code,
        cp._Description AS counterparty_name,
        r._RecorderTRef AS recorder_tref,
        r._RecorderRRef AS recorder_rref,
        SUM(ABS(CAST(r._Fld7561 AS decimal(18, 2)))) AS amount,
        SUM(ABS(CAST(r._Fld7560 AS decimal(18, 3)))) AS qty
    FROM _AccumRg7550 AS r WITH (NOLOCK)
    JOIN _Reference54 AS cp WITH (NOLOCK)
        ON cp._IDRRef = r._Fld7559RRef
    WHERE r._Active = 0x01
      AND r._RecorderTRef IN (0x000000CB, 0x0000006D)
      AND r._Period >= :start
      AND r._Period < :period_end
    GROUP BY cp._IDRRef, cp._Code, cp._Description, r._RecorderTRef, r._RecorderRRef
    """)

# Договорные типы цен контрагента (для связки характера возвратов с уровнем).
CONTRACT_TYPES_SQL = text("""
    SELECT
        master.dbo.fn_varbintohexstr(cp._IDRRef) AS counterparty_ref,
        pt._Description AS price_type_name
    FROM _Reference37 AS c WITH (NOLOCK)
    JOIN _Reference87 AS pt WITH (NOLOCK)
        ON pt._IDRRef = c._Fld513_RRRef
    JOIN _Reference54 AS cp WITH (NOLOCK)
        ON cp._IDRRef = c._OwnerIDRRef
    WHERE c._Marked = 0x00
      AND cp._Marked = 0x00
      AND master.dbo.fn_varbintohexstr(c._Fld515RRef)
          = '0x9363c6f0a10557bf4822a55db4862286'
    """)

# Возвраты по товарам (для отличия «подборщика» от распределенного шпиля).
RETURNS_BY_PRODUCT_SQL = text("""
    SELECT
        master.dbo.fn_varbintohexstr(cp._IDRRef) AS counterparty_ref,
        p._Description AS product_name,
        SUM(ABS(CAST(r._Fld7561 AS decimal(18, 2)))) AS amount
    FROM _AccumRg7550 AS r WITH (NOLOCK)
    JOIN _Reference54 AS cp WITH (NOLOCK)
        ON cp._IDRRef = r._Fld7559RRef
    LEFT JOIN _Reference62 AS p WITH (NOLOCK)
        ON p._IDRRef = r._Fld7551RRef
    WHERE r._Active = 0x01
      AND r._RecorderTRef = 0x0000006D
      AND r._Period >= :start
      AND r._Period < :period_end
    GROUP BY cp._IDRRef, p._Description
    """)

# Причины возврата по документам: брак/качество против остального (`как новый`).
RETURN_REASONS_SQL = text("""
    SELECT
        ret._IDRRef AS return_doc_id,
        SUM(ABS(CAST(ret_line._Fld1707 AS decimal(18, 2)))) AS total_return_amount,
        SUM(ABS(CAST(ret_line._Fld1704 AS decimal(18, 3)))) AS total_return_qty,
        SUM(
            CASE
                WHEN LOWER(COALESCE(return_reason._Description, ret_line._Fld8914_S, N'')) LIKE N'%брак%'
                  OR LOWER(COALESCE(return_reason._Description, ret_line._Fld8914_S, N'')) LIKE N'%качеств%'
                  OR LOWER(COALESCE(return_reason._Description, ret_line._Fld8914_S, N'')) LIKE N'%дефект%'
                  OR LOWER(COALESCE(return_reason._Description, ret_line._Fld8914_S, N'')) LIKE N'%неисправ%'
                  OR LOWER(COALESCE(return_reason._Description, ret_line._Fld8914_S, N'')) LIKE N'%некоррект%'
                  OR LOWER(COALESCE(return_reason._Description, ret_line._Fld8914_S, N'')) LIKE N'%полос%'
                  OR LOWER(COALESCE(return_reason._Description, ret_line._Fld8914_S, N'')) LIKE N'%царап%'
                  OR LOWER(COALESCE(return_reason._Description, ret_line._Fld8914_S, N'')) LIKE N'%разъем%'
                  OR LOWER(COALESCE(return_reason._Description, ret_line._Fld8914_S, N'')) LIKE N'%не работает%'
                    THEN ABS(CAST(ret_line._Fld1704 AS decimal(18, 3)))
                ELSE 0
            END
        ) AS defect_return_qty,
        SUM(
            CASE
                WHEN LOWER(COALESCE(return_reason._Description, ret_line._Fld8914_S, N'')) LIKE N'%брак%'
                  OR LOWER(COALESCE(return_reason._Description, ret_line._Fld8914_S, N'')) LIKE N'%качеств%'
                  OR LOWER(COALESCE(return_reason._Description, ret_line._Fld8914_S, N'')) LIKE N'%дефект%'
                  OR LOWER(COALESCE(return_reason._Description, ret_line._Fld8914_S, N'')) LIKE N'%неисправ%'
                  OR LOWER(COALESCE(return_reason._Description, ret_line._Fld8914_S, N'')) LIKE N'%некоррект%'
                  OR LOWER(COALESCE(return_reason._Description, ret_line._Fld8914_S, N'')) LIKE N'%полос%'
                  OR LOWER(COALESCE(return_reason._Description, ret_line._Fld8914_S, N'')) LIKE N'%царап%'
                  OR LOWER(COALESCE(return_reason._Description, ret_line._Fld8914_S, N'')) LIKE N'%разъем%'
                  OR LOWER(COALESCE(return_reason._Description, ret_line._Fld8914_S, N'')) LIKE N'%не работает%'
                    THEN ABS(CAST(ret_line._Fld1707 AS decimal(18, 2)))
                ELSE 0
            END
        ) AS defect_return_amount
    FROM _Document109 AS ret WITH (NOLOCK)
    JOIN _Document109_VT1698 AS ret_line WITH (NOLOCK)
        ON ret_line._Document109_IDRRef = ret._IDRRef
    LEFT JOIN _Reference8913 AS return_reason WITH (NOLOCK)
        ON return_reason._IDRRef = ret_line._Fld8914_RRRef
    WHERE ret._Marked = 0x00
      AND ret._Posted = 0x01
      AND ret._Date_Time >= :start
      AND ret._Date_Time < :period_end
    GROUP BY ret._IDRRef
    """)


def _rate(part: Decimal, base: Decimal) -> Decimal:
    if base <= 0:
        return Decimal("0")
    return (part / base * 100).quantize(Decimal("0.01"))


def _history_band(sales: Decimal) -> str:
    if sales < 5_000:
        return "low_history"
    if sales < 15_000:
        return "medium_history"
    return "stable_history"


def _model_key(product_name: str) -> str:
    """Модельная группа: артикулы одной модели (цвет/ORIG) считаем одним товаром."""
    return product_name.split("(", 1)[0].strip().lower()


def _behavior_group(return_rate: Decimal, defect_rate: Decimal, has_returns: bool) -> str:
    if not has_returns:
        return "no_returns"
    if return_rate > 12 or defect_rate > 6:
        return "critical_returns"
    if return_rate > 7 or defect_rate > 3:
        return "elevated_returns"
    if return_rate > 3 or defect_rate > 1:
        return "watch_returns"
    return "healthy_returns"


def build_portrait(days: int, out_path: Path) -> dict[str, int]:
    registry_classes = load_registry_classes()
    period_end = datetime.combine(date.today() + timedelta(days=1), datetime.min.time())
    start = period_end - timedelta(days=days)
    engine = build_onec_engine_from_settings()

    clients: dict[str, dict] = {}
    return_docs: dict[bytes, str] = {}
    price_types: dict[str, set[str]] = {}
    with engine.connect() as conn:
        for row in conn.execute(CONTRACT_TYPES_SQL):
            ref = (row.counterparty_ref or "").strip().lower()
            price_types.setdefault(ref, set()).add((row.price_type_name or "").strip())
        for row in conn.execute(SALES_RETURNS_SQL, {"start": start, "period_end": period_end}):
            ref = (row.counterparty_ref or "").strip().lower()
            item = clients.setdefault(
                ref,
                {
                    "code": (row.counterparty_code or "").strip(),
                    "name": " ".join((row.counterparty_name or "").split()),
                    "sales": Decimal("0"),
                    "returns": Decimal("0"),
                    "defect": Decimal("0"),
                    "sales_qty": Decimal("0"),
                    "return_qty": Decimal("0"),
                    "defect_qty": Decimal("0"),
                    "sales_docs": 0,
                    "return_docs": 0,
                },
            )
            amount = Decimal(str(row.amount or 0))
            qty = Decimal(str(row.qty or 0))
            if row.recorder_tref == b"\x00\x00\x00\xcb":
                item["sales"] += amount
                item["sales_qty"] += qty
                item["sales_docs"] += 1
            else:
                item["returns"] += amount
                item["return_qty"] += qty
                item["return_docs"] += 1
                return_docs[bytes(row.recorder_rref)] = ref
        for row in conn.execute(RETURN_REASONS_SQL, {"start": start, "period_end": period_end}):
            ref = return_docs.get(bytes(row.return_doc_id))
            if ref is None:
                continue
            clients[ref]["defect"] += Decimal(str(row.defect_return_amount or 0))
            clients[ref]["defect_qty"] += Decimal(str(row.defect_return_qty or 0))
        model_returns: dict[str, dict[str, Decimal]] = {}
        model_names: dict[str, dict[str, str]] = {}
        for row in conn.execute(RETURNS_BY_PRODUCT_SQL, {"start": start, "period_end": period_end}):
            ref = (row.counterparty_ref or "").strip().lower()
            name = " ".join((row.product_name or "?").split())
            key = _model_key(name)
            bucket = model_returns.setdefault(ref, {})
            bucket[key] = bucket.get(key, Decimal("0")) + Decimal(str(row.amount or 0))
            model_names.setdefault(ref, {}).setdefault(key, name)
    for ref, item in clients.items():
        item["price_types"] = "; ".join(sorted(price_types.get(ref, set()))) or "-"
        models = model_returns.get(ref, {})
        total = sum(models.values())
        if total > 0:
            top_key = max(models, key=models.get)
            item["top_model_share"] = (models[top_key] / total * 100).quantize(Decimal("0.1"))
            item["top_model_name"] = model_names[ref][top_key]
        else:
            item["top_model_share"] = Decimal("0")
            item["top_model_name"] = ""

    counts: dict[str, int] = {}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "код_1с",
                "контрагент",
                "типы_цен",
                f"продажи_{days}д",
                f"возвраты_{days}д",
                "возврат_брак_качество",
                "возврат_как_новый",
                "документов_продаж",
                "документов_возврата",
                "штук_продано",
                "штук_возвращено",
                "штук_брак",
                "штук_новый",
                "процент_возврата",
                "процент_возврата_штуки",
                "процент_брака",
                "процент_как_новый",
                "база_наблюдения",
                "группа_поведения",
                "характер",
                "топ_модель_доля_возвратов",
                "топ_модель",
            ]
        )
        rows = sorted(clients.values(), key=lambda c: (-c["returns"], -c["sales"]))
        for c in rows:
            if c["sales"] <= 0 and c["returns"] <= 0:
                continue
            new_amount = max(Decimal("0"), c["returns"] - c["defect"])
            new_qty = max(Decimal("0"), c["return_qty"] - c["defect_qty"])
            return_rate = _rate(c["returns"], c["sales"])
            return_qty_rate = _rate(c["return_qty"], c["sales_qty"])
            defect_rate = _rate(c["defect"], c["sales"])
            new_rate = _rate(new_amount, c["sales"])
            band = _history_band(c["sales"])
            group = _behavior_group(return_rate, defect_rate, c["returns"] > 0)
            # Характер против случайности (уточнение 2026-07-18): без количества
            # сделок новичок "купил раз - вернул раз" неотличим от шпиля.
            # Калибровка по рынку: медиана возвратов "как новый" ~15%, p75 ~26%,
            # p90 ~40% - возврат неподошедших запчастей это норма.
            is_fitting = c["top_model_share"] >= 60 and c["return_docs"] >= 3
            if c["code"] in registry_classes:
                brain_drill = f"вне клиентского контура ({registry_classes[c['code']]})"
            elif c["sales_docs"] <= 2 and return_rate >= 90 and c["returns"] > 0:
                brain_drill = "разовая сделка (купил-вернул, не характер)"
            elif band == "low_history" or c["return_docs"] < 5 or c["sales_docs"] < 5:
                brain_drill = ""
            elif new_rate >= 40:
                # Подборщик: возвраты сконцентрированы на одной модели - это
                # подбор запчасти, а не системный шпиль (уточнение 2026-07-18).
                brain_drill = (
                    "повышенные возвраты новым (подбор запчасти)"
                    if is_fitting
                    else "сверхнормативные возвраты (мозга-шпиль)"
                )
            elif new_rate >= 25:
                brain_drill = (
                    "повышенные возвраты новым (подбор запчасти)"
                    if is_fitting
                    else "повышенные возвраты новым"
                )
            else:
                brain_drill = ""
            counts[group] = counts.get(group, 0) + 1
            if brain_drill:
                counts[brain_drill] = counts.get(brain_drill, 0) + 1
            writer.writerow(
                [
                    c["code"],
                    c["name"],
                    c["price_types"],
                    f"{c['sales']:.2f}",
                    f"{c['returns']:.2f}",
                    f"{c['defect']:.2f}",
                    f"{new_amount:.2f}",
                    c["sales_docs"],
                    c["return_docs"],
                    f"{c['sales_qty']:.0f}",
                    f"{c['return_qty']:.0f}",
                    f"{c['defect_qty']:.0f}",
                    f"{new_qty:.0f}",
                    f"{return_rate}",
                    f"{return_qty_rate}",
                    f"{defect_rate}",
                    f"{new_rate}",
                    band,
                    group,
                    brain_drill,
                    f"{c['top_model_share']}",
                    c["top_model_name"][:80],
                ]
            )
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    stamp = date.today().isoformat()
    out_path = args.output or (REPORTS_DIR / stamp / f"customer-returns-portrait-{stamp}.csv")
    counts = build_portrait(args.days, out_path)
    print(f"окно {args.days} дней -> {out_path}")
    for group, count in sorted(counts.items()):
        print(f"  {group}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
