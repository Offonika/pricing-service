from __future__ import annotations

import argparse
import csv
import json
import os
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

from sqlalchemy import func, select

from app.core.config import get_settings
from app.infrastructure.db.engines import build_engine
from app.services.assortment_lifecycle_classification_store import (
    ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE,
)

DEFAULT_OUTPUT_CSV = (
    Path("reports/assortment_lifecycle")
    / date.today().isoformat()
    / "display-sale-auto-order-treatment-plan.csv"
)

RULE_LABELS_RU = {
    "working_confirmation_required": "нужно подтвердить, что товар стал рабочим",
    "demand_method_manual_review": "метод спроса еще ручной, истории мало для безопасного расчета",
    "future_ka_mapping_needs_mapping": "витрина признаков не готова, нужен маппинг",
    "quality_required": "не заполнено качество",
}

COHORT_LABELS_RU = {
    "01_справочник_качество": "лечить справочник и качество",
    "02_нет_метода_спроса": "лечить метод спроса",
    "03_дорогой_риск": "дорогой товар, нужен лимит",
    "04_ручной_блокер": "ручное подтверждение правила",
    "99_прочее": "проверить правило классификации",
}

DEMAND_METHOD_LABELS_RU = {
    "available_days_average": "средняя по дням, когда товар был доступен",
    "manual_review": "ручной разбор",
}

KA_MAPPING_LABELS_RU = {
    "ready": "готово",
    "needs_mapping": "нужен маппинг",
}

EXPENSIVE_PROFILE_LABELS_RU = {
    "": "",
    "fast_expensive": "дорогой быстрый",
    "slow_expensive": "дорогой медленный",
}

CSV_COLUMNS = [
    "code",
    "cohort",
    "cohort_label_ru",
    "treatment",
    "demand_method_code",
    "demand_method_label_ru",
    "future_ka_mapping_status",
    "future_ka_mapping_label_ru",
    "manual_review_required",
    "expensive_profile",
    "expensive_profile_label_ru",
    "quality_raw",
    "rule_codes",
    "rule_labels_ru",
    "rules_with_translation",
    "reason_text",
    "name",
]


def main() -> int:
    args = _parse_args()
    settings = get_settings()
    database_url = args.database_url or os.environ.get("DATABASE_URL") or settings.database_url
    engine = build_engine(database_url, pool_pre_ping=True)
    try:
        rows = load_sale_rows(engine, folder=args.folder)
    finally:
        engine.dispose()

    treatment_rows = build_treatment_rows(rows)
    if args.output_csv:
        write_csv(args.output_csv, treatment_rows)
    payload = {
        "status": "ready",
        "items": len(treatment_rows),
        "output_csv": str(args.output_csv) if args.output_csv else None,
        "summary": build_summary(treatment_rows),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def load_sale_rows(engine, *, folder: str) -> list[dict[str, Any]]:
    table = ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE
    with engine.connect() as conn:
        last_run_id = conn.execute(
            select(func.max(table.c.last_run_id)).where(table.c.folder.ilike(f"%{folder}%"))
        ).scalar()
        query = (
            select(table)
            .where(
                table.c.folder.ilike(f"%{folder}%"),
                table.c.last_run_id == last_run_id,
                table.c.status == "sale",
            )
            .order_by(table.c.nomenclature_code.asc())
        )
        return [dict(row) for row in conn.execute(query).mappings()]


def build_treatment_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in rows:
        if _is_green_core(row):
            continue
        rule_codes = _rule_codes(row)
        cohort, treatment = _cohort(row, rule_codes)
        result.append(
            {
                "code": _clean(row.get("nomenclature_code")),
                "cohort": cohort,
                "cohort_label_ru": COHORT_LABELS_RU.get(cohort, cohort),
                "treatment": treatment,
                "demand_method_code": _clean(row.get("demand_method_code")),
                "demand_method_label_ru": DEMAND_METHOD_LABELS_RU.get(
                    _clean(row.get("demand_method_code")),
                    _clean(row.get("demand_method_code")),
                ),
                "future_ka_mapping_status": _clean(row.get("future_ka_mapping_status")),
                "future_ka_mapping_label_ru": KA_MAPPING_LABELS_RU.get(
                    _clean(row.get("future_ka_mapping_status")),
                    _clean(row.get("future_ka_mapping_status")),
                ),
                "manual_review_required": bool(row.get("manual_review_required")),
                "expensive_profile": _clean(row.get("expensive_profile")),
                "expensive_profile_label_ru": EXPENSIVE_PROFILE_LABELS_RU.get(
                    _clean(row.get("expensive_profile")),
                    _clean(row.get("expensive_profile")),
                ),
                "quality_raw": _clean(row.get("quality_raw")),
                "rule_codes": "; ".join(rule_codes),
                "rule_labels_ru": "; ".join(_rule_label_ru(code) for code in rule_codes),
                "rules_with_translation": "; ".join(
                    f"{code} — {_rule_label_ru(code)}" for code in rule_codes
                ),
                "reason_text": _clean(row.get("reason_text")),
                "name": _clean(row.get("name")),
            }
        )
    return result


def build_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    rule_counts: Counter[str] = Counter()
    rule_label_counts: Counter[str] = Counter()
    cohort_counts: Counter[str] = Counter()
    expensive_profile_counts: Counter[str] = Counter()
    for row in rows:
        cohort_counts[_clean(row.get("cohort"))] += 1
        expensive_profile_counts[_clean(row.get("expensive_profile")) or "ordinary"] += 1
        for code in _split_codes(row.get("rule_codes")):
            rule_counts[code] += 1
            rule_label_counts[f"{code} — {_rule_label_ru(code)}"] += 1
    return {
        "cohort_counts": dict(sorted(cohort_counts.items())),
        "expensive_profile_counts": dict(sorted(expensive_profile_counts.items())),
        "rule_counts": dict(sorted(rule_counts.items())),
        "rule_label_counts": dict(sorted(rule_label_counts.items())),
    }


def write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: _csv_value(row.get(column)) for column in CSV_COLUMNS})
    return path


def _cohort(row: Mapping[str, Any], rule_codes: Sequence[str]) -> tuple[str, str]:
    expensive_profile = _clean(row.get("expensive_profile"))
    if "quality_required" in rule_codes or "future_ka_mapping_needs_mapping" in rule_codes:
        return (
            "01_справочник_качество",
            "Заполнить качество/маппинг карточки; если товар слабый - оставить ручной стоп.",
        )
    if "demand_method_manual_review" in rule_codes:
        return (
            "02_нет_метода_спроса",
            "Разобрать продажи, доступные дни и поступления; широкий спрос перевести на среднюю, разовый оставить ручным.",
        )
    if expensive_profile in {"slow_expensive", "fast_expensive"}:
        return (
            "03_дорогой_риск",
            "Добавить лимиты денег/штук и отдельный зеленый порог для дорогих товаров.",
        )
    if rule_codes:
        return (
            "04_ручной_блокер",
            "Разобрать правило и снять только после подтверждения фактов продаж, маржи и остатков.",
        )
    return ("99_прочее", "Проверить правило классификации.")


def _is_green_core(row: Mapping[str, Any]) -> bool:
    return (
        _clean(row.get("status")) == "sale"
        and _clean(row.get("future_ka_mapping_status")) == "ready"
        and _clean(row.get("demand_method_code")) == "available_days_average"
        and not bool(row.get("manual_review_required"))
        and not _json_list(row.get("blockers"))
        and not _json_list(row.get("export_blockers"))
        and bool(_clean(row.get("quality_raw")))
    )


def _rule_codes(row: Mapping[str, Any]) -> list[str]:
    codes: list[str] = []
    for code in (*_json_list(row.get("blockers")), *_json_list(row.get("export_blockers"))):
        clean_code = _clean(code)
        if clean_code and clean_code not in codes:
            codes.append(clean_code)
    if _clean(row.get("demand_method_code")) == "manual_review":
        codes.append("demand_method_manual_review")
    if _clean(row.get("future_ka_mapping_status")) != "ready":
        codes.append("future_ka_mapping_needs_mapping")
    if not _clean(row.get("quality_raw")):
        codes.append("quality_required")
    return codes


def _rule_label_ru(code: str) -> str:
    return RULE_LABELS_RU.get(code, code)


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return [value]
        return parsed if isinstance(parsed, list) else [parsed]
    return [value]


def _split_codes(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    return [_clean(part) for part in str(value).split(";") if _clean(part)]


def _csv_value(value: Any) -> str:
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if value is None:
        return ""
    return str(value)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build treatment plan for display sale rows excluded from auto-order dry-run."
    )
    parser.add_argument("--database-url", default="")
    parser.add_argument("--folder", default="дисплеи")
    parser.add_argument("--output-csv", type=Path, default=DEFAULT_OUTPUT_CSV)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())
