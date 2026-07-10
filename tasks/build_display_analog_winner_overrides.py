from __future__ import annotations

import argparse
import csv
import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence

DEFAULT_BASE_OVERRIDES_JSON = Path("config/assortment/display-manual-overrides.json")
DEFAULT_REVIEW_CSV = (
    Path("reports/assortment_lifecycle")
    / date.today().isoformat()
    / "display-auto-order-analog-transition-review.csv"
)
DEFAULT_OUTPUT_JSON = (
    Path("build/assortment")
    / date.today().isoformat()
    / "display-manual-overrides-analog-winners.json"
)
ANALOG_WINNER_RULE = "analog_winner_transition"
STOP_MANUAL_STATUSES = frozenset({"on_demand", "nonliquid", "do_not_order"})


def main() -> int:
    args = _parse_args()
    review_rows = load_analog_winner_rows(args.review_csv)
    payload = build_override_payload(
        review_rows,
        base_overrides=_load_optional_json(args.base_overrides_json),
        review_csv=args.review_csv,
        approved_by=args.approved_by,
        changed_at=args.changed_at,
    )
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    result = {
        "status": "ready",
        "review_csv": str(args.review_csv),
        "analog_winner_candidates": len(review_rows),
        "base_override_rows": len(_items(_load_optional_json(args.base_overrides_json))),
        "output_items": len(payload["items"]),
        "output_json": str(args.output_json) if args.output_json else None,
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def load_analog_winner_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise SystemExit(f"analog review csv not found: {path}")
    rows: list[dict[str, str]] = []
    with path.open(encoding="utf-8-sig", newline="") as csv_file:
        for row in csv.DictReader(csv_file):
            if _clean(row.get("analog_role")) != "primary_analog":
                continue
            warnings = {
                warning.strip()
                for warning in _clean(row.get("warnings")).split(";")
                if warning.strip()
            }
            if "analog_winner_not_auto_order_allowed" not in warnings:
                continue
            if _decimal(row.get("analog_group_recommended_order_qty")) <= 0:
                continue
            if _clean(row.get("dry_run_decision")) != "manual_review":
                continue
            code = _clean(row.get("nomenclature_code"))
            if code:
                rows.append(dict(row))
    return rows


def build_override_payload(
    review_rows: Sequence[Mapping[str, Any]],
    *,
    base_overrides: Mapping[str, Any] | Sequence[Any] | None,
    review_csv: Path,
    approved_by: str,
    changed_at: date,
) -> dict[str, Any]:
    base_payload = base_overrides if isinstance(base_overrides, Mapping) else {}
    base_items = _items(base_overrides)
    items_by_code: dict[str, dict[str, Any]] = {}
    skipped_manual_stop_codes: list[str] = []
    for item in base_items:
        code = _code(item)
        if code:
            items_by_code[code] = dict(item)

    added_codes: list[str] = []
    for row in review_rows:
        code = _clean(row.get("nomenclature_code"))
        if not code:
            continue
        existing = items_by_code.get(code, {})
        manual_status = _clean(existing.get("manual_status")).casefold()
        if manual_status in STOP_MANUAL_STATUSES:
            skipped_manual_stop_codes.append(code)
            continue
        if existing.get("analog_winner_confirmed_by_folder_responsible") is True:
            continue
        items_by_code[code] = {
            **existing,
            "nomenclature_code": code,
            "analog_winner_confirmed_by_folder_responsible": True,
            "working_confirmed_by_folder_responsible": True,
            "manual_reason": _manual_reason(row),
            "manual_approved_by": approved_by,
            "manual_changed_at": changed_at.isoformat(),
            "approval_source": "display_analog_winner_batch_v1",
            "approval_rule": ANALOG_WINNER_RULE,
            "approval_rule_ru": "лучший аналог группы с расчетной потребностью",
            "source_review_csv": str(review_csv),
            "analog_group_id": _clean(row.get("analog_group_id")),
            "analog_group_size": _clean(row.get("analog_group_size")),
            "analog_group_recommended_order_qty": _clean(
                row.get("analog_group_recommended_order_qty")
            ),
            "analog_group_net_sales_qty": _clean(row.get("analog_group_net_sales_qty")),
            "analog_winner_score": _clean(row.get("analog_winner_score")),
        }
        added_codes.append(code)

    payload = dict(base_payload)
    payload.update(
        {
            "_analog_winner_confirmation_applied_at": datetime.now().isoformat(
                timespec="seconds"
            ),
            "_analog_winner_confirmation_source": str(review_csv),
            "_analog_winner_confirmation_rule": ANALOG_WINNER_RULE,
            "_analog_winner_confirmation_rule_ru": (
                "лучший аналог группы с расчетной потребностью"
            ),
            "_analog_winner_confirmation_added": len(added_codes),
            "_analog_winner_confirmation_skipped_manual_stops": sorted(
                skipped_manual_stop_codes
            ),
            "_approved_by": approved_by,
            "_changed_at": changed_at.isoformat(),
            "items": sorted(items_by_code.values(), key=lambda item: _code(item)),
        }
    )
    return payload


def _manual_reason(row: Mapping[str, Any]) -> str:
    name = _clean(row.get("name"))
    group_size = _clean(row.get("analog_group_size")) or "нескольких"
    order_qty = _clean(row.get("analog_group_recommended_order_qty")) or "0"
    net_sales = _clean(row.get("analog_group_net_sales_qty")) or "0"
    return (
        "Подтверждено как рабочий товар-победитель группы аналогов: "
        f"{name}; группа {group_size} SKU, продажи группы за окно {net_sales} шт., "
        f"расчетная потребность {order_qty} шт. Старые аналоги не заказывать, остаток допродавать."
    )


def _items(payload: Mapping[str, Any] | Sequence[Any] | None) -> list[dict[str, Any]]:
    if not payload:
        return []
    raw_items: Any
    if isinstance(payload, Mapping):
        raw_items = payload.get("items")
        if raw_items is None:
            raw_items = [
                {"nomenclature_code": code, **value}
                for code, value in payload.items()
                if isinstance(value, Mapping)
            ]
    else:
        raw_items = payload
    if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes)):
        raise SystemExit("manual overrides must be a list or an object with items")
    if not all(isinstance(item, Mapping) for item in raw_items):
        raise SystemExit("manual override items must be objects")
    return [dict(item) for item in raw_items]


def _load_optional_json(path: Path | None) -> Any:
    if path is None or not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _code(item: Mapping[str, Any]) -> str:
    return _clean(item.get("nomenclature_code") or item.get("NomenclatureCode"))


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _decimal(value: Any) -> Decimal:
    raw = _clean(value).replace(" ", "").replace(",", ".")
    if not raw:
        return Decimal("0")
    try:
        return Decimal(raw)
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build display manual overrides for best analog winners."
    )
    parser.add_argument("--review-csv", type=Path, default=DEFAULT_REVIEW_CSV)
    parser.add_argument("--base-overrides-json", type=Path, default=DEFAULT_BASE_OVERRIDES_JSON)
    parser.add_argument("--output-json", type=Path, default=DEFAULT_OUTPUT_JSON)
    parser.add_argument("--approved-by", default=f"chat_{date.today().isoformat()}")
    parser.add_argument("--changed-at", type=_parse_date, default=date.today())
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"date must be YYYY-MM-DD, got: {value}") from exc


if __name__ == "__main__":
    raise SystemExit(main())
