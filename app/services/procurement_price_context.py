"""Price snapshots keep commercial decisions separate from FX and accounting cost."""

from __future__ import annotations

import copy
import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from typing import Any, Mapping

from app.schemas.procurement_price_context import ProcurementPriceContext
from app.services.procurement_order_metrics import _normalize_currency
from app.services.procurement_price_sources import EMPTY_REF, read_price_sources, valid_ref
from app.services.procurement_supply_scenarios import price_confirmed


def _amount(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _at(value: Any) -> str | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value))
    if parsed.year > 3000:
        parsed = parsed.replace(year=parsed.year - 2000)
    return parsed.isoformat() if parsed.year > 1900 else None


def _true(value: Any) -> bool:
    return value in (True, 1, b"\x01")


def _missing(reason: str, *, status: str = "missing", currency: str = "RUB") -> dict:
    return {"value": None, "currency": currency, "status": status, "reason": reason}


def _document(row: Mapping[str, Any], kind: str, *, ref_key: str = "document_ref") -> dict:
    return {
        "kind": kind,
        "ref": row[ref_key],
        "number": row.get("document_number"),
        "at": _at(row.get("document_at") or row.get("at")),
    }


def _fact(row: Mapping[str, Any], *, source: str, status: str, kind: str) -> dict:
    return {
        "value": str(_amount(row["value"])),
        "currency": _normalize_currency(row.get("currency")) or None,
        "status": status,
        "source": source,
        "at": _at(row.get("at")),
        "unit_ref": row.get("unit_ref"),
        "unit_name": row.get("unit_name"),
        "characteristic_ref": row.get("characteristic_ref"),
        "documents": [_document(row, kind)],
    }


def _latest(rows: list[dict]) -> dict | None:
    if not rows:
        return None
    latest_at = max(_at(row.get("at")) or "" for row in rows)
    latest = [row for row in rows if (_at(row.get("at")) or "") == latest_at]
    distinct = {json.dumps(row, default=str, sort_keys=True): row for row in latest}
    return next(iter(distinct.values())) if len(distinct) == 1 else None


def _rub_fact(row: dict, *, kind: str) -> dict | None:
    if (_amount(row.get("value")) or 0) <= 0:
        return None
    fact = _fact(row, source=f"onec:{kind}", status="confirmed", kind=kind)
    if fact["currency"] == "RUB":
        return fact
    rate = _amount(row.get("exchange_rate"))
    multiple = _amount(row.get("exchange_multiplicity"))
    if (
        not fact["currency"]
        or _normalize_currency(row.get("settlement_currency")) != fact["currency"]
        or not rate
        or rate <= 0
        or not multiple
        or multiple <= 0
    ):
        return None
    return {
        **fact,
        "currency": "RUB",
        "value": str(_amount(fact["value"]) * rate / multiple),
        "exchange_rate": str(rate),
        "exchange_multiplicity": str(multiple),
        "exchange_rate_at": fact["at"],
        "source": f"onec:{kind}:document_exchange_rate",
    }


def price_context_item(line: Any) -> dict[str, Any]:
    payload = line.payload or {}
    return {
        "key": str(line.id),
        "nomenclature_ref": line.nomenclature_ref,
        "nomenclature_code": line.nomenclature_code,
        "supplier_ref": str(line.order.supplier_ref or "").lower(),
        "order_ref": str(line.order.onec_document_ref or "").lower(),
        "line_number": line.line_number,
        "unit_ref": payload.get("unit_ref"),
        "characteristic_ref": payload.get("characteristic_ref"),
    }


def build_price_snapshot(item: dict, sources: dict[str, list], *, as_of: date) -> dict:
    """Only exact SKU/unit/characteristic evidence can describe a line's cost."""
    identity = {
        key: item.get(key)
        for key in (
            "nomenclature_code",
            "nomenclature_ref",
            "supplier_ref",
            "order_ref",
            "line_number",
        )
    }
    snapshot = {
        **identity,
        "source_status": "ready",
        "checked_on": as_of.isoformat(),
        "last_success_on": as_of.isoformat(),
        "stale": False,
        "purchase_rub": _missing("applicable_document_rate_missing"),
        "receipt_purchases_rub": [],
        "reference_cost_rub": _missing("confirmed_cost_missing"),
        "actual_cost_status": "not_formed",
        "actual_costs_rub": [],
        "supplier_quotes": [],
    }
    products = {
        row["item_ref"]: row
        for row in sources["products"]
        if (
            (valid_ref(item.get("nomenclature_ref")) == row["item_ref"])
            if valid_ref(item.get("nomenclature_ref"))
            else row["code"] == item.get("nomenclature_code")
        )
    }
    if len(products) != 1:
        snapshot["source_status"] = "ambiguous"
        snapshot["reference_cost_rub"] = _missing("exact_sku_missing", status="ambiguous")
        return snapshot
    product = next(iter(products.values()))
    sku = product["item_ref"]
    # Known code and GUID must agree; never fill a nearby or renamed SKU silently.
    if item.get("nomenclature_code") and product["code"] != item["nomenclature_code"]:
        snapshot["source_status"] = "ambiguous"
        snapshot["reference_cost_rub"] = _missing("sku_code_ref_conflict", status="ambiguous")
        return snapshot
    order_rows = [
        row
        for row in sources["orders"]
        if row["order_ref"] == item.get("order_ref")
        and row["item_ref"] == sku
        and row["line_number"] == item.get("line_number")
        and row["supplier_ref"] == item.get("supplier_ref")
    ]
    source_order = order_rows[0] if len(order_rows) == 1 else None
    unit = (source_order or {}).get("unit_ref") or item.get("unit_ref") or product.get("unit_ref")
    characteristic = (source_order or {}).get("characteristic_ref") or item.get(
        "characteristic_ref"
    )
    if characteristic is None and not _true(product.get("has_characteristics")):
        characteristic = EMPTY_REF
    snapshot.update(
        unit_ref=unit,
        unit_name=(source_order or {}).get("unit_name") or product.get("unit_name"),
        characteristic_ref=characteristic,
    )

    def same_dimensions(row: dict) -> bool:
        return bool(
            unit
            and unit != EMPTY_REF
            and characteristic
            and row["item_ref"] == sku
            and row.get("unit_ref") == unit
            and row.get("characteristic_ref") == characteristic
        )

    costs = [
        row
        for row in sources["costs"]
        if same_dimensions(row)
        and _normalize_currency(row.get("currency")) == "RUB"
        and (_amount(row.get("value")) or 0) > 0
    ]
    latest_cost = _latest(costs)
    if latest_cost:
        snapshot["reference_cost_rub"] = _fact(
            latest_cost,
            source="onec:ЦеныНоменклатуры:Себестоимость",
            status="reference",
            kind="УстановкаЦенНоменклатуры",
        )
    elif costs or not characteristic or not unit or unit == EMPTY_REF:
        snapshot["reference_cost_rub"] = _missing(
            "unit_characteristic_or_latest_cost_ambiguous", status="ambiguous"
        )
    quotes = [
        row
        for row in sources["quotes"]
        if same_dimensions(row)
        and row["supplier_ref"] == item.get("supplier_ref")
        and (_amount(row.get("value")) or 0) > 0
    ]
    snapshot["supplier_quotes"] = [
        _fact(
            row,
            source="onec:ЦеныНоменклатурыКонтрагентов",
            status="reference",
            kind="Запись цены поставщика",
        )
        for row in sorted(
            quotes,
            key=lambda row: (
                str(row.get("currency")),
                _at(row.get("at")) or "",
                row["document_ref"],
            ),
        )
    ]
    if source_order and (_amount(source_order.get("value")) or 0) > 0:
        purchase = _fact(
            source_order, source="onec:ЗаказПоставщику", status="confirmed", kind="ЗаказПоставщику"
        )
        snapshot["source_purchase"] = purchase
        snapshot["purchase_rub"] = (
            _rub_fact(source_order, kind="ЗаказПоставщику") or snapshot["purchase_rub"]
        )
    receipt_rows = [
        row
        for row in sources["receipts"]
        if same_dimensions(row)
        and row["order_ref"] == item.get("order_ref")
        and row.get("supplier_ref") == item.get("supplier_ref")
    ]
    # Keep different receipt prices/rates separate; never average them without a quantity basis.
    receipts = {}
    for row in receipt_rows:
        receipts.setdefault(row["receipt_ref"], row)
        rub = _rub_fact(row, kind="ПоступлениеТоваровУслуг")
        if rub and rub not in snapshot["receipt_purchases_rub"]:
            snapshot["receipt_purchases_rub"].append(rub)
    if receipt_rows:
        only_receipt = receipt_rows[0] if len(receipt_rows) == 1 else None
        same_price = bool(
            source_order
            and only_receipt
            and _amount(only_receipt.get("value")) == _amount(source_order.get("value"))
            and _normalize_currency(only_receipt.get("currency"))
            == _normalize_currency(source_order.get("currency"))
        )
        snapshot["purchase_rub"] = (
            _rub_fact(only_receipt, kind="ПоступлениеТоваровУслуг")
            or _missing("applicable_document_rate_missing")
            if same_price
            else _missing("see_individual_receipt_prices", status="ambiguous")
        )
    for receipt_ref, receipt in sorted(receipts.items()):
        matching_costs = [row for row in costs if row.get("receipt_ref") == receipt_ref]
        cost = _latest(matching_costs)
        allocations = [
            row
            for row in sources["allocations"]
            if same_dimensions(row) and row["receipt_ref"] == receipt_ref
        ]
        latest_allocation = _latest(allocations)
        # A price setting before a later expense is not the result of the final allocation.
        if (
            not cost
            or not latest_allocation
            or not _true(latest_allocation.get("final_allocation"))
        ):
            continue
        if (_at(cost.get("at")) or "") < (_at(latest_allocation.get("at")) or ""):
            continue
        fact = _fact(
            cost,
            source="onec:Себестоимость:receipt_final_allocation",
            status="confirmed",
            kind="УстановкаЦенНоменклатуры",
        )
        fact["documents"].append(
            _document(receipt, "ПоступлениеТоваровУслуг", ref_key="receipt_ref")
        )
        fact["documents"].extend(
            _document(row, "РаспределениеДопРасходов")
            for row in sorted(
                allocations, key=lambda row: (str(row.get("at")), row["document_ref"])
            )
        )
        snapshot["actual_costs_rub"].append(fact)
    if snapshot["actual_costs_rub"]:
        snapshot["actual_cost_status"] = (
            "confirmed" if len(snapshot["actual_costs_rub"]) == len(receipts) else "partial"
        )
    return snapshot


def collect_price_snapshots(engine, items: list[dict], *, as_of: date) -> dict[str, dict]:
    if not items:
        return {}
    try:
        sources = read_price_sources(engine, items, as_of=as_of)
        return {item["key"]: build_price_snapshot(item, sources, as_of=as_of) for item in items}
    except Exception as exc:
        # Do not expose connection strings or erase the last confirmed accounting state.
        return {
            item["key"]: {
                "source_status": "unavailable",
                "checked_on": as_of.isoformat(),
                "stale": True,
                "error_type": type(exc).__name__,
            }
            for item in items
        }


def merge_price_snapshot(previous: dict | None, incoming: dict) -> dict:
    if incoming.get("source_status") == "unavailable":
        return {**copy.deepcopy(previous or {}), **incoming}
    return copy.deepcopy(incoming)


def load_registry_price_contexts(database_url: str, snapshots: list[dict]) -> None:
    from app.infrastructure.db.engines import build_onec_engine

    items = []
    for order in snapshots:
        for index, line in enumerate(order.get("lines") or [], start=1):
            items.append(
                {
                    "key": f"{len(items)}",
                    "nomenclature_ref": str(line.get("item_ref_hex") or "").lower(),
                    "nomenclature_code": str(line.get("onec_item_code") or "").strip(),
                    "supplier_ref": str(
                        (order.get("supplier") or {}).get("onec_ref") or ""
                    ).lower(),
                    "order_ref": str(order.get("onec_ref") or "").lower(),
                    "line_number": int(line.get("line_no") or index),
                }
            )
    if not items:
        return
    engine = None
    try:
        engine = build_onec_engine(database_url, query_timeout_seconds=60, login_timeout_seconds=15)
        contexts = collect_price_snapshots(engine, items, as_of=date.today())
    except Exception as exc:
        contexts = {
            item["key"]: {
                "source_status": "unavailable",
                "stale": True,
                "checked_on": date.today().isoformat(),
                "error_type": type(exc).__name__,
            }
            for item in items
        }
    finally:
        if engine is not None:
            engine.dispose()
    index = 0
    for order in snapshots:
        for line in order.get("lines") or []:
            line["price_context"] = contexts[str(index)]
            index += 1


def serialize_price_context(line: Any) -> dict:
    payload = line.payload or {}
    snapshot = copy.deepcopy(payload.get("price_context") or {})
    identity = price_context_item(line)
    if snapshot and any(
        snapshot.get(key) != identity.get(key) for key in ("nomenclature_ref", "nomenclature_code")
    ):
        snapshot = {}
    if any(
        payload.get(key) and payload[key] != snapshot.get(key)
        for key in ("unit_ref", "characteristic_ref")
    ):
        snapshot = {}
    if snapshot.get("supplier_ref") != identity["supplier_ref"]:
        snapshot["supplier_quotes"] = []
        snapshot["source_status"] = "not_loaded"
    if snapshot.get("order_ref") != identity["order_ref"]:
        snapshot.update(
            actual_costs_rub=[], actual_cost_status="not_formed", receipt_purchases_rub=[]
        )
    dimensions = {
        key: snapshot.get(key) or payload.get(key)
        for key in ("unit_ref", "unit_name", "characteristic_ref")
    }
    confirmed = price_confirmed(line)
    agreed = {
        **dimensions,
        "value": str(line.purchase_price) if confirmed else None,
        "currency": line.currency or None,
        "status": "confirmed" if confirmed else "unconfirmed",
        "reason": None if confirmed else "price_not_agreed",
        "source": "onec:ЗаказПоставщику" if line.source_kind == "onec_import" else "buyer_decision",
    }
    decision = payload.get("price_decision") or {}
    if (
        confirmed
        and _amount(decision.get("value")) == _amount(line.purchase_price)
        and decision.get("currency") == line.currency
    ):
        agreed.update(
            at=decision.get("decided_at"),
            confirmed_by=decision.get("actor_name") or decision.get("actor"),
        )
    source_purchase = snapshot.get("source_purchase") or {}
    source_matches = bool(
        confirmed
        and source_purchase
        and _amount(source_purchase.get("value")) == _amount(line.purchase_price)
        and source_purchase.get("currency") == line.currency
        and all(
            snapshot.get(key) == identity.get(key)
            for key in ("order_ref", "supplier_ref", "line_number")
        )
    )
    rub = _missing("applicable_document_rate_missing")
    if not confirmed:
        rub = _missing("price_not_agreed", status="unconfirmed")
    elif line.currency == "RUB":
        rub = copy.deepcopy(agreed)
    if source_matches:
        agreed.update(at=source_purchase.get("at"), documents=source_purchase.get("documents", []))
        rub = snapshot.get("purchase_rub") or rub
    # Current manual price/currency always wins over a previously collected conversion.
    context = {
        **snapshot,
        "agreed_purchase": agreed,
        "purchase_rub": rub,
        "reference_cost_rub": snapshot.get("reference_cost_rub")
        or _missing("confirmed_cost_missing"),
    }
    return ProcurementPriceContext.model_validate(context).model_dump(mode="json")


def price_context_export(context: dict) -> dict[str, Any]:
    """Internal calculation export only; never included in supplier packages."""
    result = {"price_context_json": json.dumps(context, ensure_ascii=False, sort_keys=True)}
    for key in ("agreed_purchase", "purchase_rub", "reference_cost_rub"):
        fact = context.get(key) or {}
        for field in (
            "value",
            "currency",
            "status",
            "at",
            "unit_ref",
            "characteristic_ref",
            "exchange_rate",
            "exchange_multiplicity",
            "exchange_rate_at",
        ):
            result[f"{key}_{field}"] = fact.get(field)
    result["actual_cost_status"] = context.get("actual_cost_status", "not_formed")
    result["price_source_stale"] = context.get("stale", False)
    return result


def serialize_draft_price_context(line: dict, order: Mapping[str, Any]) -> dict:
    """The calculation JSON uses the same interpretation as persisted line API."""
    return serialize_price_context(
        SimpleNamespace(
            id=None,
            line_number=line["line_number"],
            nomenclature_ref=line["nomenclature_ref"],
            nomenclature_code=line.get("nomenclature_code"),
            purchase_price=line["purchase_price"],
            currency=line.get("currency") or order.get("currency") or "",
            source_kind=line.get("source_kind", "automatic"),
            payload=line.get("payload") or {},
            order=SimpleNamespace(
                supplier_ref=(order.get("supplier") or {}).get("ref"), onec_document_ref=None
            ),
        )
    )
