"""Diff-инструмент классификации статусов ассортимента.

Строит per-SKU снимок решения ``decide_assortment_status`` по одному и тому же
входу и сравнивает два снимка по критичным для авто-заказа полям:
``status``, ``auto_order_allowed``, ``blockers``, ``manual_review_required``
(и ``recommended_status`` для контекста).

Назначение — предохранитель ("этап 0") перед высокорисковыми миграциями контура
статусов: снятием замороженных пер-SKU решений из JSON (``_fruit_status_transfer_77``),
изменениями формулы и т.п. Меняем что-то — прогоняем diff до/после и видим,
не "поехали" ли статусы/флаг авто-заказа молча.

Режимы:

* ``snapshot`` — построить снимок из входных записей (тот же ``--input-json``,
  что у ``build_assortment_lifecycle_updates``). Флаг ``--no-fact-overlay``
  отключает наложение замороженных JSON-решений (``_fact_status_decision_from_record``),
  показывая чистый результат формулы.
* ``diff`` — сравнить два готовых снимка (``--before`` / ``--after``).
* ``overlay-audit`` — из одного входа построить снимок формулы и снимок
  "формула + JSON-оверлей" и сразу сравнить. Нулевой diff => JSON-оверлей ничего
  не добавляет и его можно снимать безопасно.

С ``--fail-on-change`` процесс завершается кодом 1 при любом расхождении — удобно
для гейта в CI/скрипте выкатки.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

from app.services.assortment_lifecycle import (
    decide_assortment_status,
    decide_target_assortment_status,
)
from tasks.build_assortment_lifecycle_updates import (
    _fact_status_decision_from_record,
    _lifecycle_input_from_record,
    _load_records,
    _matches_folder,
)

# Поля снимка, по которым считаем расхождение (требование ревью контура).
DIFF_FIELDS = (
    "status",
    "demand_state",
    "auto_order_allowed",
    "blockers",
    "manual_review_required",
    "first_receipt_at",
    "last_receipt_at",
    "history_age_days",
    "cost_quartile",
    "minimum_representation_qty",
)


def build_snapshot(
    records: list[dict[str, Any]],
    *,
    folder_filter: str = "",
    fact_overlay: bool = True,
    target_model: bool = False,
) -> dict[str, dict[str, Any]]:
    """Построить снимок ``{nomenclature_code: {status, auto_order_allowed, ...}}``."""

    snapshot: dict[str, dict[str, Any]] = {}
    for record in records:
        if folder_filter and not _matches_folder(record, folder_filter):
            continue
        lifecycle_input = _lifecycle_input_from_record(record)
        decision = (
            decide_target_assortment_status(lifecycle_input)
            if target_model
            else decide_assortment_status(lifecycle_input)
        )
        if fact_overlay and not target_model:
            decision = _fact_status_decision_from_record(record, decision)
        snapshot[decision.nomenclature_code] = {
            "status": decision.status.value,
            "auto_order_allowed": bool(decision.auto_order_allowed),
            "blockers": sorted(decision.blockers),
            "manual_review_required": bool(decision.manual_review_required),
            "recommended_status": (
                decision.recommended_status.value if decision.recommended_status else None
            ),
            "demand_state": decision.demand_state.value if decision.demand_state else None,
            "demand_state_label": decision.demand_state_label,
            "demand_reason_codes": list(decision.demand_reason_codes),
            "reason_codes": list(decision.reason_codes),
            "first_receipt_at": record.get("first_receipt_at"),
            "last_receipt_at": record.get("last_receipt_at"),
            "history_age_days": record.get("history_age_days"),
            "first_sale_at": record.get("first_sale_at"),
            "last_sale_at": record.get("last_sale_at"),
            "sales_qty_short": record.get("sales_qty_short"),
            "sales_qty_medium": record.get("sales_qty_medium"),
            "sales_qty_long": record.get("sales_qty_long"),
            "days_in_sale_short": record.get("days_in_sale_short"),
            "days_in_sale_medium": record.get("days_in_sale_medium"),
            "days_in_sale_long": record.get("days_in_sale_long"),
            "inventory_cost_per_unit": record.get("inventory_cost_per_unit"),
            "cost_quartile": record.get("cost_quartile"),
            "minimum_representation_qty": record.get("minimum_representation_qty"),
        }
    return snapshot


def _snapshot_items(payload: Any) -> dict[str, dict[str, Any]]:
    items = payload.get("items") if isinstance(payload, dict) else payload
    if not isinstance(items, dict):
        raise SystemExit(
            "Снимок должен быть объектом {nomenclature_code: {...}} или {items: {...}}"
        )
    return items


def diff_snapshots(
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Сравнить два снимка по ключевым полям."""

    before_codes = set(before)
    after_codes = set(after)

    changed: list[dict[str, Any]] = []
    transition_counts: dict[str, int] = {}
    auto_order_flips = {"enabled": 0, "disabled": 0}
    review_flips = {"enabled": 0, "disabled": 0}
    blocker_changed = 0

    for code in sorted(before_codes & after_codes):
        b = before[code]
        a = after[code]
        field_changes = {
            field: {"before": b.get(field), "after": a.get(field)}
            for field in DIFF_FIELDS
            if b.get(field) != a.get(field)
        }
        if not field_changes:
            continue
        changed.append(
            {
                "nomenclature_code": code,
                "changes": field_changes,
                "before": b,
                "after": a,
            }
        )
        if "status" in field_changes:
            key = f"{b.get('status')} -> {a.get('status')}"
            transition_counts[key] = transition_counts.get(key, 0) + 1
        if "auto_order_allowed" in field_changes:
            bucket = "enabled" if a.get("auto_order_allowed") else "disabled"
            auto_order_flips[bucket] += 1
        if "manual_review_required" in field_changes:
            bucket = "enabled" if a.get("manual_review_required") else "disabled"
            review_flips[bucket] += 1
        if "blockers" in field_changes:
            blocker_changed += 1

    removed = sorted(before_codes - after_codes)
    added = sorted(after_codes - before_codes)

    return {
        "summary": {
            "before_count": len(before_codes),
            "after_count": len(after_codes),
            "common": len(before_codes & after_codes),
            "changed": len(changed),
            "added": len(added),
            "removed": len(removed),
            "status_transitions": transition_counts,
            "auto_order_flips": auto_order_flips,
            "manual_review_flips": review_flips,
            "blocker_changed": blocker_changed,
        },
        "changed": changed,
        "added": added,
        "removed": removed,
    }


def _has_changes(diff: dict[str, Any]) -> bool:
    summary = diff["summary"]
    return bool(summary["changed"] or summary["added"] or summary["removed"])


def _print_diff(diff: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(diff, ensure_ascii=False, indent=2))
        return
    summary = diff["summary"]
    print(
        f"Записей: было {summary['before_count']}, стало {summary['after_count']}, "
        f"общих {summary['common']}."
    )
    print(
        f"Изменено {summary['changed']}, добавлено {summary['added']}, "
        f"удалено {summary['removed']}."
    )
    if summary["status_transitions"]:
        print("Переходы статусов:")
        for transition, count in sorted(
            summary["status_transitions"].items(), key=lambda kv: -kv[1]
        ):
            print(f"  {transition}: {count}")
    print(
        f"Флаг авто-заказа: включён у {summary['auto_order_flips']['enabled']}, "
        f"выключен у {summary['auto_order_flips']['disabled']}."
    )
    print(
        f"Ручной пересмотр: включён у {summary['manual_review_flips']['enabled']}, "
        f"выключен у {summary['manual_review_flips']['disabled']}."
    )
    print(f"Изменились блокеры у {summary['blocker_changed']}.")


def _cmd_snapshot(args: argparse.Namespace) -> int:
    records = _load_records(args.input_json)
    snapshot = build_snapshot(
        records,
        folder_filter=args.folder,
        fact_overlay=not args.no_fact_overlay,
        target_model=args.target_model,
    )
    payload = {
        "_meta": {
            "input_json": str(args.input_json),
            "folder": args.folder,
            "fact_overlay": not args.no_fact_overlay,
            "target_model": args.target_model,
            "count": len(snapshot),
        },
        "items": snapshot,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output_json:
        args.output_json.write_text(text, encoding="utf-8")
        print(str(args.output_json))
    else:
        print(text)
    return 0


def _cmd_diff(args: argparse.Namespace) -> int:
    before = _snapshot_items(json.loads(args.before.read_text(encoding="utf-8-sig")))
    after = _snapshot_items(json.loads(args.after.read_text(encoding="utf-8-sig")))
    diff = diff_snapshots(before, after)
    _print_diff(diff, as_json=args.json)
    if args.fail_on_change and _has_changes(diff):
        return 1
    return 0


def _cmd_overlay_audit(args: argparse.Namespace) -> int:
    records = _load_records(args.input_json)
    formula = build_snapshot(records, folder_filter=args.folder, fact_overlay=False)
    overlaid = build_snapshot(records, folder_filter=args.folder, fact_overlay=True)
    # before = "формула + JSON-оверлей" (текущее прод-поведение),
    # after = "чистая формула" (что будет после снятия оверлея из JSON).
    diff = diff_snapshots(overlaid, formula)
    _print_diff(diff, as_json=args.json)
    if args.fail_on_change and _has_changes(diff):
        return 1
    return 0


def _cmd_target_audit(args: argparse.Namespace) -> int:
    records = _load_records(args.input_json)
    before = build_snapshot(records, folder_filter=args.folder, fact_overlay=True)
    after = build_snapshot(
        records,
        folder_filter=args.folder,
        fact_overlay=False,
        target_model=True,
    )
    diff = diff_snapshots(before, after)
    diff["audit_rows"] = _build_target_audit_rows(before, after)
    legacy_orders = _load_order_rows(args.legacy_order_json)
    target_orders = _load_order_rows(args.target_order_json)
    _attach_order_and_capital_diff(diff, legacy_orders, target_orders)
    diff["audit_sections"] = _target_audit_sections(diff)
    diff["_meta"] = {
        "scope": args.folder,
        "before_model": "v1_with_fact_overlay",
        "after_model": "v2_shadow",
        "production_action": "none_read_only",
    }
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(
            json.dumps(diff, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    if args.output_csv:
        _write_target_audit_csv(args.output_csv, diff)
    _print_diff(diff, as_json=args.json)
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    snap = sub.add_parser("snapshot", help="Построить снимок классификации из входных записей")
    snap.add_argument(
        "--input-json", type=Path, required=True, help="JSON список или {items:[...]}"
    )
    snap.add_argument("--target-model", action="store_true", help="Build accepted v2 shadow")
    snap.add_argument("--folder", default="", help="Фильтр по папке, напр. дисплеи")
    snap.add_argument(
        "--no-fact-overlay",
        action="store_true",
        help="Не накладывать замороженные JSON-решения (чистая формула)",
    )
    snap.add_argument("--output-json", type=Path, help="Куда записать снимок")
    snap.set_defaults(func=_cmd_snapshot)

    audit_v2 = sub.add_parser(
        "target-audit", help="Compare current v1 and accepted v2 shadow on one input"
    )
    audit_v2.add_argument("--input-json", type=Path, required=True)
    audit_v2.add_argument("--folder", default="дисплеи")
    audit_v2.add_argument("--output-json", type=Path)
    audit_v2.add_argument("--output-csv", type=Path)
    audit_v2.add_argument("--legacy-order-json", type=Path)
    audit_v2.add_argument("--target-order-json", type=Path)
    audit_v2.add_argument("--json", action="store_true")
    audit_v2.set_defaults(func=_cmd_target_audit)

    dcmd = sub.add_parser("diff", help="Сравнить два снимка")
    dcmd.add_argument("--before", type=Path, required=True)
    dcmd.add_argument("--after", type=Path, required=True)
    dcmd.add_argument("--json", action="store_true", help="Машиночитаемый вывод")
    dcmd.add_argument(
        "--fail-on-change", action="store_true", help="Код возврата 1 при расхождении"
    )
    dcmd.set_defaults(func=_cmd_diff)

    audit = sub.add_parser(
        "overlay-audit",
        help="Сравнить формулу и 'формула + JSON-оверлей' на одном входе",
    )
    audit.add_argument("--input-json", type=Path, required=True)
    audit.add_argument("--folder", default="", help="Фильтр по папке, напр. дисплеи")
    audit.add_argument("--json", action="store_true", help="Машиночитаемый вывод")
    audit.add_argument(
        "--fail-on-change",
        action="store_true",
        help="Код возврата 1 при расхождении (оверлей нельзя снимать безопасно)",
    )
    audit.set_defaults(func=_cmd_overlay_audit)

    return parser.parse_args(argv)


def _load_order_rows(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    items = payload.get("items") or payload.get("rows") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise SystemExit("Order audit JSON must be a list or contain items/rows")
    return {
        str(item.get("nomenclature_code") or "").strip(): dict(item)
        for item in items
        if isinstance(item, dict) and str(item.get("nomenclature_code") or "").strip()
    }


def _attach_order_and_capital_diff(
    diff: dict[str, Any],
    legacy_orders: dict[str, dict[str, Any]],
    target_orders: dict[str, dict[str, Any]],
) -> None:
    order_delta = 0.0
    capital_delta = 0.0
    for item in diff.get("audit_rows", diff["changed"]):
        code = item["nomenclature_code"]
        before_order = legacy_orders.get(code, {})
        after_order = target_orders.get(code, {})
        before_qty = _number(before_order.get("recommended_order_qty"))
        after_qty = _number(after_order.get("recommended_order_qty"))
        cost = _number(
            item["after"].get("inventory_cost_per_unit")
            or after_order.get("inventory_cost_per_unit")
            or before_order.get("inventory_cost_per_unit")
        )
        item["order_impact"] = {
            "before_qty": before_qty,
            "after_qty": after_qty,
            "delta_qty": after_qty - before_qty,
            "inventory_cost_per_unit": cost,
            "capital_delta": (after_qty - before_qty) * cost,
        }
        order_delta += after_qty - before_qty
        capital_delta += (after_qty - before_qty) * cost
    diff["summary"]["recommended_order_delta_qty"] = order_delta
    diff["summary"]["capital_delta"] = capital_delta
    diff["summary"]["order_comparison_available"] = bool(legacy_orders and target_orders)


def _target_audit_sections(diff: dict[str, Any]) -> dict[str, list[str]]:
    rows = diff.get("audit_rows", diff["changed"])
    return {
        "exits_from_growing": [
            item["nomenclature_code"]
            for item in rows
            if item["before"].get("status") == "sale" and item["after"].get("status") != "sale"
        ],
        "spikes": [
            item["nomenclature_code"]
            for item in rows
            if item["after"].get("demand_state") == "spike"
        ],
        "unknown_facts": [
            item["nomenclature_code"]
            for item in rows
            if item["after"].get("demand_state") == "no_data"
            or "demand_data_missing" in item["after"].get("blockers", [])
        ],
        "manual_statuses": [
            item["nomenclature_code"]
            for item in rows
            if item["after"].get("status")
            in {"matrix", "on_demand", "replace_candidate", "nonliquid", "do_not_order", "pension"}
        ],
    }


def _build_target_audit_rows(
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for code in sorted(set(before) | set(after)):
        legacy = before.get(code, {})
        target = after.get(code, {})
        rows.append(
            {
                "nomenclature_code": code,
                "changed": any(legacy.get(field) != target.get(field) for field in DIFF_FIELDS),
                "before": legacy,
                "after": target,
            }
        )
    return rows


def _write_target_audit_csv(path: Path, diff: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = (
        "nomenclature_code",
        "legacy_status",
        "target_status",
        "demand_state",
        "first_receipt_at",
        "last_receipt_at",
        "history_age_days",
        "first_sale_at",
        "last_sale_at",
        "sales_qty_30",
        "sales_qty_90",
        "sales_qty_180",
        "days_in_sale_30",
        "days_in_sale_90",
        "days_in_sale_180",
        "inventory_cost_per_unit",
        "cost_quartile",
        "minimum_representation_qty",
        "legacy_order_qty",
        "target_order_qty",
        "order_delta_qty",
        "capital_delta",
        "reason_codes",
        "demand_reason_codes",
    )
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for item in diff.get("audit_rows", diff["changed"]):
            before = item["before"]
            after = item["after"]
            order = item.get("order_impact", {})
            writer.writerow(
                {
                    "nomenclature_code": item["nomenclature_code"],
                    "legacy_status": before.get("status"),
                    "target_status": after.get("status"),
                    "demand_state": after.get("demand_state"),
                    "first_receipt_at": after.get("first_receipt_at"),
                    "last_receipt_at": after.get("last_receipt_at"),
                    "history_age_days": after.get("history_age_days"),
                    "first_sale_at": after.get("first_sale_at"),
                    "last_sale_at": after.get("last_sale_at"),
                    "sales_qty_30": after.get("sales_qty_short"),
                    "sales_qty_90": after.get("sales_qty_medium"),
                    "sales_qty_180": after.get("sales_qty_long"),
                    "days_in_sale_30": after.get("days_in_sale_short"),
                    "days_in_sale_90": after.get("days_in_sale_medium"),
                    "days_in_sale_180": after.get("days_in_sale_long"),
                    "inventory_cost_per_unit": after.get("inventory_cost_per_unit"),
                    "cost_quartile": after.get("cost_quartile"),
                    "minimum_representation_qty": after.get("minimum_representation_qty"),
                    "legacy_order_qty": order.get("before_qty"),
                    "target_order_qty": order.get("after_qty"),
                    "order_delta_qty": order.get("delta_qty"),
                    "capital_delta": order.get("capital_delta"),
                    "reason_codes": ",".join(after.get("reason_codes") or []),
                    "demand_reason_codes": ",".join(after.get("demand_reason_codes") or []),
                }
            )


def _number(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
