"""Линтер противоречий контура «Типы цен»: ruleset против документов и blueprint.

Проверяет, что:
1) актуальные нормативы из config/price_types/ruleset.yaml присутствуют в
   канонических документах;
2) устаревшие значения (superseded_values) не встречаются в них вне контекста
   истории решений (history_markers, строка или 5 строк выше);
3) blueprint JSON согласован с ruleset: rulebook-уровни, состояние заморозки
   повышений, обязательные поля, отсутствие «голых» порогов бронзы в переходах.

Запуск: ./.venv/bin/python scripts/validate_price_type_ruleset.py
Выход 0 = противоречий нет; 1 = найдены (список печатается).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
RULESET = yaml.safe_load(
    (REPO_ROOT / "config/price_types/ruleset.yaml").read_text(encoding="utf-8")
)
REPORTS = REPO_ROOT / "reports/retail_price_types/customer-price-type-automation"
BLUEPRINT_JSON = REPO_ROOT / "build/bitrix/customer_price_type_blueprint.json"
TRACKED_SPEC = REPO_ROOT / "docs/specs/customer-price-type-management-v1.md"

OPTIONAL_REPORT_DOCS = [
    REPORTS / "2026-07-17/price-type-retention-norms-draft-2026-07-17.md",
    REPORTS / "2026-07-10/monthly-price-type-inventory-rule-total10k-2026-07-10.md",
    REPORTS / "2026-07-18/revision-package/business-rules-catalog.md",
    REPORTS / "2026-07-18/revision-package/field-mapping.md",
    REPORTS / "2026-07-18/revision-package/README.md",
    REPORTS / "2026-07-16/customer-price-type-review-package-2026-07-16.md",
]

# Release-contained документ, где обязаны присутствовать действующие нормативы.
NORM_REQUIRED_DOCS = [TRACKED_SPEC]


def _fmt(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def check_current_values(errors: list[str]) -> None:
    for doc in NORM_REQUIRED_DOCS:
        text = doc.read_text(encoding="utf-8")
        for key, level in RULESET["levels"].items():
            for label, value in (
                ("норматив", level["retention_norm_3m"]),
                ("удержание", level["hold_last_month"]),
            ):
                if _fmt(value) not in text and str(value) not in text:
                    errors.append(
                        f"{doc.name}: не найден действующий {label} уровня "
                        f"{key} = {_fmt(value)}"
                    )


def check_superseded(errors: list[str], *, canonical_docs: list[Path]) -> None:
    markers = [m.lower() for m in RULESET.get("history_markers", [])]
    for doc in canonical_docs:
        lines = doc.read_text(encoding="utf-8").splitlines()
        for idx, line in enumerate(lines):
            low = line.lower()
            for stale in RULESET.get("superseded_values", []):
                if stale.lower() not in low:
                    continue
                context = " ".join(lines[max(0, idx - 5) : idx + 1]).lower()
                if any(marker in context for marker in markers):
                    continue
                errors.append(
                    f"{doc.name}:{idx + 1}: устаревшее значение «{stale}» вне "
                    f"контекста истории: {line.strip()[:80]}"
                )


def check_blueprint(errors: list[str], *, blueprint_json: Path) -> None:
    if not blueprint_json.exists():
        errors.append("blueprint JSON не собран (build/bitrix/...json)")
        return
    bp = json.loads(blueprint_json.read_text(encoding="utf-8"))
    rulebook = bp.get("rulebook", {})
    if rulebook.get("ruleset_version") != RULESET["ruleset_version"]:
        errors.append(
            "blueprint: ruleset_version не совпадает "
            f"({rulebook.get('ruleset_version')} != {RULESET['ruleset_version']})"
        )
    for key, level in RULESET["levels"].items():
        book = rulebook.get("levels", {}).get(key, {})
        if book.get("retention_norm_3m") != level["retention_norm_3m"]:
            errors.append(f"blueprint: норматив уровня {key} расходится с ruleset")
        if book.get("hold_last_month") != level["hold_last_month"]:
            errors.append(f"blueprint: порог удержания {key} расходится с ruleset")
    freeze_factors = [
        item for item in bp.get("stop_factors", []) if item.get("key") == "upgrade_freeze"
    ]
    if RULESET["upgrades"]["frozen"] and not freeze_factors:
        errors.append("blueprint: отсутствует действующий стоп-фактор upgrade_freeze")
    if not RULESET["upgrades"]["frozen"] and freeze_factors:
        errors.append("blueprint: отменённый стоп-фактор upgrade_freeze всё ещё активен")
    required = {f["logical_key"] for f in bp.get("fields", []) if f.get("required")}
    for key in ("counterparty_ref", "snapshot_date", "current_price_type"):
        if key not in required:
            errors.append(f"blueprint: поле {key} не помечено обязательным")
    for rule in bp.get("transition_rules", []):
        if "3300" in rule.get("when", "") and "ruleset" not in rule.get("when", ""):
            errors.append(
                "blueprint: переход с «голым» порогом бронзы без ссылки на "
                f"ruleset: {rule['from']} -> {rule['to']}"
            )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blueprint-json", type=Path, default=BLUEPRINT_JSON)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    errors: list[str] = []
    canonical_docs = [TRACKED_SPEC, *(doc for doc in OPTIONAL_REPORT_DOCS if doc.exists())]
    check_current_values(errors)
    check_superseded(errors, canonical_docs=canonical_docs)
    check_blueprint(errors, blueprint_json=args.blueprint_json)
    if errors:
        print(f"ЛИНТЕР: найдено противоречий: {len(errors)} (ruleset {RULESET['ruleset_version']})")
        for err in errors:
            print(f"  - {err}")
        return 1
    print(
        f"ЛИНТЕР: противоречий нет (ruleset {RULESET['ruleset_version']}, "
        f"документов проверено: {len(canonical_docs)})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
