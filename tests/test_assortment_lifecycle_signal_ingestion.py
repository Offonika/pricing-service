from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services.assortment_lifecycle_signal_ingestion import (
    AssortmentLifecycleSignalIngestionError,
    build_assortment_signal_ingestion_dry_run,
    display_family_registry_snapshot_from_mapping,
)


def _registry(*, missing_family_for: str | None = None, status: str = "active"):
    members = []
    for index, code in enumerate(("SALE", "STOCK", "ORDER", "RECEIPT", "CARGO"), start=1):
        members.append(
            {
                "product_id": index,
                "family_key": None if code == missing_family_for else "iphone-17-pro-max",
                "nomenclature_code": code,
                "aliases": [f"ARTICLE-{code}", f"FACT-{code}"],
                "name": f"Дисплей {code}",
            }
        )
    return display_family_registry_snapshot_from_mapping(
        {
            "schema": "display_family_registry_snapshot.v1",
            "version_number": 2,
            "status": status,
            "members": members,
        }
    )


def _row(
    signal_type: str = "customer_sale",
    *,
    code: str = "SALE",
    event_id: str = "event-1",
    quantity: object = "1",
    name: str | None = None,
    occurred_at: str = "2026-08-17T09:00:00+00:00",
    available_at: str = "2026-08-17T09:01:00+00:00",
) -> dict[str, object]:
    return {
        "signal_type": signal_type,
        "source": "normalized-test-source",
        "source_event_id": event_id,
        "occurred_at": occurred_at,
        "available_at": available_at,
        "reliability": "0.95",
        "reliability_reason": "source_reconciled",
        "nomenclature_code": code,
        "name": name or f"Дисплей {code}",
        "quantity": quantity,
        "payload": {"document": event_id},
    }


def _bundle(items: list[dict[str, object]], *, as_of: str = "2026-08-17T12:00:00Z"):
    return {
        "schema": "assortment_signal_source_bundle.v1",
        "bundle_id": "test-bundle",
        "as_of": as_of,
        "items": items,
    }


def test_happy_path_prepares_all_five_first_wave_signal_types() -> None:
    rows = [
        _row("customer_sale", code="SALE", event_id="sale-1"),
        _row("stock_availability", code="STOCK", event_id="stock-1", quantity="8"),
        _row("supplier_order", code="ORDER", event_id="order-1", quantity="5"),
        _row("supplier_receipt", code="RECEIPT", event_id="receipt-1", quantity="4"),
        _row("cargo", code="CARGO", event_id="cargo-1", quantity="3"),
    ]

    result = build_assortment_signal_ingestion_dry_run(_bundle(rows), _registry())

    assert result["status"] == "ready"
    assert {row["signal_type"] for row in result["prepared_signals"]} == {
        "customer_sale",
        "stock_availability",
        "supplier_order",
        "supplier_receipt",
        "cargo",
    }
    assert {row["display_family_key"] for row in result["prepared_signals"]} == {
        "iphone-17-pro-max"
    }
    assert {row["display_family_registry_version"] for row in result["prepared_signals"]} == {2}
    assert result["as_of_projection"]["signal_count"] == 5
    assert result["persistence_performed"] is False
    assert result["production_authorized"] is False


def test_bitok_is_excluded_before_registry_resolution_and_listed_once() -> None:
    bitok = _row(code="UNKNOWN-BITOK", name="Дисплей iPhone 13 (биток)")
    changed_same_identity = {**bitok, "quantity": "2"}

    result = build_assortment_signal_ingestion_dry_run(
        _bundle([bitok, changed_same_identity]),
        _registry(),
    )

    assert result["status"] == "ready"
    assert result["prepared_signals"] == []
    assert result["quarantine"] == []
    assert result["conflicts"] == []
    assert result["scope"]["excluded_row_count"] == 2
    assert result["scope"]["excluded_item_count"] == 1
    assert result["scope"]["exclusions"] == [
        {
            "nomenclature_code": "UNKNOWN-BITOK",
            "name": "Дисплей iPhone 13 (биток)",
            "reason_code": "excluded_display_name_bitok",
            "scope_policy_version": "display_scope_policy.v1",
        }
    ]


def test_unknown_sku_goes_to_quarantine() -> None:
    result = build_assortment_signal_ingestion_dry_run(
        _bundle([_row(code="UNKNOWN")]),
        _registry(),
    )

    assert result["status"] == "ready_with_quarantine"
    assert result["prepared_signals"] == []
    assert result["quarantine"][0]["reason_codes"] == ["sku_not_in_active_family_registry"]


def test_product_alias_resolves_to_the_registry_canonical_code() -> None:
    row = _row()
    row.pop("nomenclature_code")
    row["article"] = "ARTICLE-SALE"

    result = build_assortment_signal_ingestion_dry_run(
        _bundle([row]),
        _registry(),
    )

    assert result["status"] == "ready"
    assert result["prepared_signals"][0]["nomenclature_code"] == "SALE"
    assert result["prepared_signals"][0]["payload"]["source_nomenclature_code"] == ("ARTICLE-SALE")


def test_member_without_family_linkage_goes_to_quarantine() -> None:
    result = build_assortment_signal_ingestion_dry_run(
        _bundle([_row(code="SALE")]),
        _registry(missing_family_for="SALE"),
    )

    assert result["status"] == "ready_with_quarantine"
    assert result["prepared_signals"] == []
    assert result["quarantine"][0]["reason_codes"] == ["family_linkage_missing"]


def test_non_mapping_payload_goes_to_quarantine_even_when_empty() -> None:
    row = _row()
    row["payload"] = []

    result = build_assortment_signal_ingestion_dry_run(
        _bundle([row]),
        _registry(),
    )

    assert result["status"] == "ready_with_quarantine"
    assert result["quarantine"][0]["reason_codes"] == ["payload_must_be_mapping"]


def test_exact_duplicate_is_counted_without_a_second_signal() -> None:
    row = _row()

    result = build_assortment_signal_ingestion_dry_run(
        _bundle([row, dict(row)]),
        _registry(),
    )

    assert result["status"] == "ready"
    assert len(result["prepared_signals"]) == 1
    assert len(result["exact_duplicates"]) == 1
    assert result["reconciliation"]["rows"]["prepared"] == 1
    assert result["reconciliation"]["rows"]["exact_duplicate"] == 1


def test_same_identity_with_different_content_blocks_the_identity() -> None:
    first = _row(quantity="1")
    changed = _row(quantity="2")

    result = build_assortment_signal_ingestion_dry_run(
        _bundle([first, changed]),
        _registry(),
    )

    assert result["status"] == "blocked_conflicts"
    assert result["prepared_signals"] == []
    assert result["conflicts"][0]["reason_code"] == (
        "signal_identity_exists_with_different_payload"
    )
    assert result["conflicts"][0]["source_row_numbers"] == [1, 2]
    assert result["reconciliation"]["rows"]["conflicted"] == 2


def test_quarantined_variant_blocks_an_otherwise_valid_source_identity() -> None:
    valid = _row(quantity="1")
    unresolved = _row(code="UNKNOWN", quantity="2")

    result = build_assortment_signal_ingestion_dry_run(
        _bundle([valid, unresolved]),
        _registry(),
    )

    assert result["status"] == "blocked_conflicts"
    assert result["prepared_signals"] == []
    assert result["quarantine"] == []
    assert result["conflicts"][0]["source_row_numbers"] == [1, 2]
    assert result["conflicts"][0]["invalid_or_unresolved_rows"][0]["reason_codes"] == [
        "sku_not_in_active_family_registry"
    ]
    assert result["reconciliation"]["rows"]["conflicted"] == 2
    assert result["reconciliation"]["rows"]["quarantined"] == 0


def test_different_quarantined_rows_with_same_identity_are_a_blocking_conflict() -> None:
    first = _row(code="UNKNOWN-1", quantity="1")
    second = _row(code="UNKNOWN-2", quantity="2")

    result = build_assortment_signal_ingestion_dry_run(
        _bundle([first, second]),
        _registry(),
    )

    assert result["status"] == "blocked_conflicts"
    assert result["quarantine"] == []
    assert result["conflicts"][0]["source_row_numbers"] == [1, 2]
    assert len(result["conflicts"][0]["raw_content_hashes"]) == 2


def test_late_available_at_is_hidden_from_earlier_as_of_projection() -> None:
    row = _row(
        occurred_at="2026-08-15T09:00:00+00:00",
        available_at="2026-08-17T09:00:00+00:00",
    )

    result = build_assortment_signal_ingestion_dry_run(
        _bundle([row], as_of="2026-08-16T12:00:00+00:00"),
        _registry(),
    )

    assert len(result["prepared_signals"]) == 1
    assert result["as_of_projection"]["signals"] == []
    assert result["as_of_projection"]["hidden_not_available_count"] == 1


def test_missing_or_inactive_registry_fails_closed() -> None:
    with pytest.raises(
        AssortmentLifecycleSignalIngestionError,
        match="active_family_registry_missing",
    ):
        build_assortment_signal_ingestion_dry_run(_bundle([_row()]), None)

    with pytest.raises(
        AssortmentLifecycleSignalIngestionError,
        match="family_registry_not_active:superseded",
    ):
        _registry(status="superseded")


def test_row_and_quantity_reconciliation_equations_balance() -> None:
    prepared = _row(event_id="prepared", quantity="3")
    duplicate = dict(prepared)
    conflict_one = _row(event_id="conflict", quantity="4")
    conflict_two = _row(event_id="conflict", quantity="5")
    unknown = _row(code="UNKNOWN", event_id="unknown", quantity="6")
    excluded = _row(
        code="BITOK",
        event_id="bitok",
        quantity="7",
        name="Дисплей (биток)",
    )

    result = build_assortment_signal_ingestion_dry_run(
        _bundle([prepared, duplicate, conflict_one, conflict_two, unknown, excluded]),
        _registry(),
    )

    rows = result["reconciliation"]["rows"]
    quantity = result["reconciliation"]["quantity"]
    assert rows == {
        "source": 6,
        "scope_included": 5,
        "scope_excluded": 1,
        "prepared": 1,
        "exact_duplicate": 1,
        "conflicted": 2,
        "quarantined": 1,
        "equations": {
            "source_equals_scope_included_plus_excluded": True,
            "scope_included_equals_all_ingestion_outcomes": True,
        },
    }
    assert quantity["input_numeric"] == "28"
    assert quantity["scope_excluded"] == "7"
    assert quantity["prepared"] == "3"
    assert quantity["exact_duplicate"] == "3"
    assert quantity["conflicted"] == "9"
    assert quantity["quarantined"] == "6"
    assert quantity["equations"] == {"input_numeric_equals_all_outcome_numeric": True}


def test_datetime_argument_may_be_passed_as_timezone_aware_datetime() -> None:
    result = build_assortment_signal_ingestion_dry_run(
        _bundle([_row()]),
        _registry(),
        as_of=datetime(2026, 8, 17, 12, tzinfo=UTC),
    )

    assert result["as_of"] == "2026-08-17T12:00:00+00:00"
