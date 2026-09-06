from copy import deepcopy
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services import procurement_price_context as prices
from app.services.procurement_price_sources import EMPTY_REF

SKU = "0x" + "1" * 32
SUPPLIER = "0x" + "2" * 32
UNIT = "0x" + "3" * 32
ORDER = "0x" + "4" * 32
RECEIPT = "0x" + "5" * 32
AS_OF = date(2026, 9, 6)


@pytest.fixture
def evidence():
    dimensions = {
        "item_ref": SKU,
        "unit_ref": UNIT,
        "unit_name": "шт",
        "characteristic_ref": EMPTY_REF,
    }
    item = {
        "key": "7",
        "nomenclature_ref": SKU,
        "nomenclature_code": "РБ000064181",
        "supplier_ref": SUPPLIER,
        "order_ref": ORDER,
        "line_number": 1,
    }
    purchase = {
        **dimensions,
        "order_ref": ORDER,
        "line_number": 1,
        "supplier_ref": SUPPLIER,
        "value": "160",
        "currency": "156",
        "settlement_currency": "156",
        "exchange_rate": "12.1",
        "exchange_multiplicity": "1",
        "document_ref": ORDER,
        "document_number": "РБГУ0000412",
        "at": "2026-07-06T14:50:00",
    }
    receipt = {
        **purchase,
        "receipt_ref": RECEIPT,
        "document_ref": RECEIPT,
        "document_number": "РБГУ0000967",
        "at": "2026-08-21T11:03:13",
        "exchange_rate": "12.85",
    }
    cost = {
        **dimensions,
        "value": "2108.65",
        "currency": "643",
        "at": "2026-08-21T11:39:06",
        "document_ref": "0x" + "6" * 32,
        "document_number": "РБ000001527",
        "receipt_ref": RECEIPT,
    }
    sources = {
        "products": [
            {**dimensions, "code": item["nomenclature_code"], "has_characteristics": b"\x00"}
        ],
        "costs": [cost],
        "orders": [purchase],
        "receipts": [receipt],
        "quotes": [
            {
                **purchase,
                "document_ref": RECEIPT,
                "document_number": "РБГУ0000967",
                "at": receipt["at"],
            }
        ],
        "allocations": [
            {
                **dimensions,
                "receipt_ref": RECEIPT,
                "document_ref": "0x" + "7" * 32,
                "document_number": "РБГУ0000818",
                "at": "2026-08-21T11:04:59",
                "final_allocation": b"\x01",
            }
        ],
    }
    return item, sources


def line_for(item, snapshot, *, value="160", currency="CNY", source_kind="onec_import"):
    return SimpleNamespace(
        id=7,
        line_number=1,
        nomenclature_ref=item["nomenclature_ref"],
        nomenclature_code=item["nomenclature_code"],
        purchase_price=Decimal(value),
        currency=currency,
        source_kind=source_kind,
        payload={"price_context": snapshot, "price_confirmed": True},
        order=SimpleNamespace(
            onec_document_ref=item["order_ref"], supplier_ref=item["supplier_ref"]
        ),
    )


def test_three_prices_use_receipt_rate_and_exact_final_allocation(evidence):
    item, sources = evidence
    snapshot = prices.build_price_snapshot(item, sources, as_of=AS_OF)
    context = prices.serialize_price_context(line_for(item, snapshot))
    assert context["agreed_purchase"]["value"] == "160"
    assert context["agreed_purchase"]["currency"] == "CNY"
    assert Decimal(context["purchase_rub"]["value"]) == Decimal("2056")
    assert context["purchase_rub"]["exchange_rate"] == "12.85"
    assert context["purchase_rub"]["documents"][0]["number"] == "РБГУ0000967"
    assert context["reference_cost_rub"]["value"] == "2108.65"
    assert context["reference_cost_rub"]["status"] == "reference"
    assert context["actual_costs_rub"][0]["value"] == "2108.65"
    assert {doc["number"] for doc in context["actual_costs_rub"][0]["documents"]} == {
        "РБГУ0000967",
        "РБ000001527",
        "РБГУ0000818",
    }
    assert context["actual_cost_status"] == "confirmed"
    assert "price_change_pct" not in context
    assert "additional_expenses" not in context  # No subtraction of independent price records.


def test_draft_keeps_placeholder_with_reference_cost_and_quotes(evidence):
    item, sources = evidence
    item["order_ref"] = ""
    snapshot = prices.build_price_snapshot(item, sources, as_of=AS_OF)
    line = line_for(item, snapshot, value="1", source_kind="automatic")
    context = prices.serialize_price_context(line)
    assert context["agreed_purchase"]["value"] is None
    assert context["agreed_purchase"]["status"] == "unconfirmed"
    assert context["purchase_rub"]["value"] is None
    assert context["reference_cost_rub"]["value"] == "2108.65"
    assert context["actual_costs_rub"] == []
    assert context["supplier_quotes"][0]["currency"] == "CNY"
    assert line.purchase_price == 1


@pytest.mark.parametrize(
    "broken",
    [
        "receipt_link",
        "basis",
        "allocation",
        "final_allocation",
        "cost_before_expense",
        "unit",
        "characteristic",
    ],
)
def test_new_supply_cost_needs_all_evidence(evidence, broken):
    item, sources = evidence
    if broken == "receipt_link":
        sources["receipts"][0]["order_ref"] = "other-order"
    elif broken == "basis":
        sources["costs"][0]["receipt_ref"] = None
    elif broken == "allocation":
        sources["allocations"] = []
    elif broken == "final_allocation":
        sources["allocations"][0]["final_allocation"] = b"\x00"
    elif broken == "cost_before_expense":
        sources["costs"][0]["at"] = "2026-08-21T11:04:00"
    else:
        sources["allocations"][0][broken + "_ref"] = "different"
    snapshot = prices.build_price_snapshot(item, sources, as_of=AS_OF)
    assert snapshot["actual_costs_rub"] == []
    assert snapshot["actual_cost_status"] == "not_formed"
    assert snapshot["reference_cost_rub"]["value"] == "2108.65"


def test_partial_receipts_have_individual_rub_values_and_costs(evidence):
    item, sources = evidence
    sources["receipts"].append(
        {
            **sources["receipts"][0],
            "receipt_ref": "another-receipt",
            "document_ref": "another-receipt",
            "exchange_rate": "1300",
            "exchange_multiplicity": "100",
        }
    )
    snapshot = prices.build_price_snapshot(item, sources, as_of=AS_OF)
    assert snapshot["purchase_rub"]["value"] is None
    assert snapshot["purchase_rub"]["status"] == "ambiguous"
    assert {Decimal(f["value"]) for f in snapshot["receipt_purchases_rub"]} == {
        Decimal("2056"),
        Decimal("2080"),
    }
    assert snapshot["actual_cost_status"] == "partial"


@pytest.mark.parametrize(
    "field,value",
    [
        ("exchange_rate", None),
        ("exchange_rate", "0"),
        ("exchange_rate", "NaN"),
        ("exchange_multiplicity", "0"),
        ("settlement_currency", "840"),
    ],
)
def test_invalid_or_unrelated_rate_is_unknown_not_zero(evidence, field, value):
    item, sources = evidence
    sources["receipts"][0][field] = value
    snapshot = prices.build_price_snapshot(item, sources, as_of=AS_OF)
    assert snapshot["purchase_rub"]["value"] is None
    assert snapshot["reference_cost_rub"]["value"] == "2108.65"


@pytest.mark.parametrize("mutation", ["price", "currency", "supplier", "order", "sku"])
def test_edit_never_reuses_an_obsolete_conversion(evidence, mutation):
    item, sources = evidence
    line = line_for(item, prices.build_price_snapshot(item, sources, as_of=AS_OF))
    if mutation == "price":
        line.purchase_price = Decimal("180")
    elif mutation == "currency":
        line.currency = "USD"
    elif mutation == "supplier":
        line.order.supplier_ref = "new-supplier"
    elif mutation == "order":
        line.order.onec_document_ref = "new-order"
    else:
        line.nomenclature_ref = "new-sku"
    context = prices.serialize_price_context(line)
    assert context["purchase_rub"]["value"] is None
    if mutation == "sku":
        assert context["reference_cost_rub"]["value"] is None
    if mutation in {"sku", "order"}:
        assert context["actual_costs_rub"] == []
    if mutation == "supplier":
        assert context["supplier_quotes"] == []


def test_source_failure_preserves_last_confirmed_facts_and_repeat_is_idempotent(
    evidence, monkeypatch
):
    item, sources = evidence
    old = prices.build_price_snapshot(item, sources, as_of=AS_OF)
    before = deepcopy(old)

    def failure(*args, **kwargs):
        raise ConnectionError("secret should never be in the response")

    monkeypatch.setattr(prices, "read_price_sources", failure)
    incoming = prices.collect_price_snapshots(object(), [item], as_of=date(2026, 9, 7))[item["key"]]
    merged = prices.merge_price_snapshot(old, incoming)
    context = prices.serialize_price_context(line_for(item, merged))
    assert context["stale"]
    assert context["last_success_on"] == "2026-09-06"
    assert context["error_type"] == "ConnectionError"
    assert context["actual_costs_rub"][0]["value"] == "2108.65"
    assert "secret" not in str(context)
    assert old == before
    assert prices.merge_price_snapshot(merged, incoming) == merged
    assert prices.merge_price_snapshot(merged, before) == before


def test_latest_cost_conflict_and_unknown_characteristic_do_not_choose_arbitrarily(evidence):
    item, sources = evidence
    sources["costs"].append(
        {**sources["costs"][0], "document_ref": "conflicting-document", "value": "3100"}
    )
    snapshot = prices.build_price_snapshot(item, sources, as_of=AS_OF)
    assert snapshot["reference_cost_rub"]["status"] == "ambiguous"
    assert snapshot["actual_costs_rub"] == []
    item["order_ref"] = ""
    sources["products"][0]["has_characteristics"] = b"\x01"
    assert (
        prices.build_price_snapshot(item, sources, as_of=AS_OF)["reference_cost_rub"]["value"]
        is None
    )


def test_code_ref_conflict_and_other_supplier_cannot_supply_a_price(evidence):
    item, sources = evidence
    sources["quotes"][0]["supplier_ref"] = "other-supplier"
    assert prices.build_price_snapshot(item, sources, as_of=AS_OF)["supplier_quotes"] == []
    item["nomenclature_code"] = "similar-sku"
    assert (
        prices.build_price_snapshot(item, sources, as_of=AS_OF)["reference_cost_rub"]["value"]
        is None
    )


def test_manual_rub_price_has_its_own_confirmation_and_export(evidence, tmp_path):
    import csv
    import json

    from tasks.build_procurement_order_formation_dry_run import write_lines_csv

    item, sources = evidence
    item["order_ref"] = ""
    snapshot = prices.build_price_snapshot(item, sources, as_of=AS_OF)
    line = line_for(item, snapshot, value="1900", currency="RUB", source_kind="automatic")
    line.payload["price_decision"] = {
        "value": "1900",
        "currency": "RUB",
        "actor_name": "Закупщик",
        "decided_at": "2026-09-06T15:00:00+03:00",
    }
    context = prices.serialize_price_context(line)
    assert context["purchase_rub"]["value"] == "1900"
    assert context["agreed_purchase"]["confirmed_by"] == "Закупщик"
    assert context["purchase_rub"]["exchange_rate"] is None
    path = tmp_path / "calculation.csv"
    write_lines_csv(
        path,
        [
            {
                "stable_key": "draft",
                "supplier": {},
                "contract": {},
                "warehouse": {},
                "lines": [{"price_context": context}],
            }
        ],
    )
    with path.open(encoding="utf-8-sig") as handle:
        row = next(csv.DictReader(handle))
    assert row["agreed_purchase_value"] == "1900"
    assert row["reference_cost_rub_value"] == "2108.65"
    assert row["purchase_rub_exchange_rate"] == ""
    assert json.loads(row["price_context_json"]) == context


def test_placeholder_currency_mismatch_is_not_a_price_error():
    from datetime import datetime

    from app.services.procurement_exceptions import exception_facts

    line = SimpleNamespace(
        id=1,
        removed=False,
        purchase_price=Decimal(1),
        source_kind="automatic",
        payload={"price_change_status": "currency_mismatch"},
    )
    order = SimpleNamespace(
        origin="generated",
        status="draft",
        lines=[line],
        payload={},
        bitrix_product_rows_sync_state="not_started",
        currency="RUB",
    )
    reasons = {reason for reason, _, _ in exception_facts(order, now=datetime(2026, 9, 6))}
    assert "unconfirmed_price" in reasons
    assert "price_history" not in reasons
    line.purchase_price = Decimal("1900")
    line.payload["price_confirmed"] = True
    reasons = {reason for reason, _, _ in exception_facts(order, now=datetime(2026, 9, 6))}
    assert "price_history" in reasons
