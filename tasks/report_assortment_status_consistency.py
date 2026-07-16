"""Сверка статуса между каталогом номенклатуры (1136) и картой решения (1056).

Read-only инструмент. Ничего не пишет ни в Bitrix, ни в 1С, ни в свою БД —
только сравнивает и печатает отчёт.

Контекст: статус ассортимента хранится в ДВУХ разных местах смарт-процессов
Bitrix — в каталоге (свойство "Статус ассортимента", PROPERTY_789, читает
``resolve_catalog_product_by_xml_id``) и в карточке решения оператора
("Закупка/Заказ", поле UF_CRM_8_ASSORTMENTSTATUSDECISION, читает
``collect_decisions``). Между ними нет проверки согласованности: если они
разойдутся (например, решение "Не закупать" записано, а каталог всё ещё
показывает "ПРОДАЖА"), никто об этом не узнает.

Сравнивать можно только решения, отличные от "no_change" — та вертикаль
управленческих статусов (matrix/on_demand/replace_candidate/nonliquid/
do_not_order) и "working", у остальных лестничных статусов (Плод/Новинка/…)
решения не бывает.

Источники данных — офлайн JSON-фикстуры (``--catalog-json``/``--decisions-json``),
либо (если передан ``--webhook-url``) живой Bitrix. Живой режим требует
настоящего доступа к Bitrix, которого нет в этой среде разработки — здесь
проверялась только офлайн-логика сравнения.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from app.services.procurement_order_formation import normalize_status


def compare_catalog_vs_decisions(
    catalog_items: list[dict[str, Any]],
    decision_items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Сравнить каталожный статус и решение по каждому известному коду.

    ``catalog_items``: [{"nomenclature_code": ..., "assortment_status": ...}, ...]
    ``decision_items``: [{"nomenclature_code": ..., "manual_status": ...}, ...]
    (``manual_status`` — то же, что ``status_decision`` в решении Bitrix,
    после ``override_from_bitrix_item``/``collect_decisions``.)
    """

    catalog_by_code = {
        str(item.get("nomenclature_code") or "").strip(): item
        for item in catalog_items
        if str(item.get("nomenclature_code") or "").strip()
    }
    decision_by_code = {
        str(item.get("nomenclature_code") or "").strip(): item
        for item in decision_items
        if str(item.get("nomenclature_code") or "").strip()
        and str(item.get("manual_status") or "").strip()
        and str(item.get("manual_status") or "").strip() != "no_change"
    }

    consistent: list[dict[str, Any]] = []
    divergent: list[dict[str, Any]] = []
    decision_without_catalog: list[dict[str, Any]] = []

    for code, decision in decision_by_code.items():
        catalog = catalog_by_code.get(code)
        decision_status = normalize_status(decision.get("manual_status"))
        if catalog is None:
            decision_without_catalog.append(
                {"nomenclature_code": code, "decision_status": decision_status}
            )
            continue
        catalog_status = normalize_status(catalog.get("assortment_status"))
        row = {
            "nomenclature_code": code,
            "decision_status": decision_status,
            "catalog_status": catalog_status,
        }
        if decision_status == catalog_status:
            consistent.append(row)
        else:
            divergent.append(row)

    return {
        "decisions_compared": len(decision_by_code),
        "consistent_count": len(consistent),
        "divergent_count": len(divergent),
        "decision_without_catalog_count": len(decision_without_catalog),
        "divergent": divergent,
        "decision_without_catalog": decision_without_catalog,
    }


def _load_json_list(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    items = payload.get("items") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise SystemExit(f"{path}: ожидается список или {{items: [...]}}")
    return items


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--catalog-json",
        type=Path,
        required=True,
        help="Список {nomenclature_code, assortment_status} из каталога (1136)",
    )
    parser.add_argument(
        "--decisions-json",
        type=Path,
        required=True,
        help="Список {nomenclature_code, manual_status} из карты решения (1056)",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    catalog_items = _load_json_list(args.catalog_json)
    decision_items = _load_json_list(args.decisions_json)
    report = compare_catalog_vs_decisions(catalog_items, decision_items)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
