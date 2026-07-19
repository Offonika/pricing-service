from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest

from scripts import build_executive_procurement_snapshot as snapshot


@pytest.fixture(autouse=True)
def no_history_query(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(snapshot, "fetch_supplier_prepare_history", lambda *_args, **_kwargs: [])


def _order(onec_ref: str, contour: str = "cargo") -> dict[str, object]:
    return {
        "onec_ref": onec_ref,
        "procurement_contour_key": contour,
        "open_amount_rub": 100,
    }


def test_build_snapshot_reads_all_open_orders_without_document_date_filter(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    captured: dict[str, object] = {}

    def fake_fetch(_url, **kwargs):
        captured.update(kwargs)
        return [_order("0x02", "ved_import"), _order("0x01")]

    monkeypatch.setattr(snapshot, "fetch_open_supplier_orders", fake_fetch)
    output = tmp_path / "procurement.json"

    payload = snapshot.build_snapshot(
        "mssql://read-only",
        output=output,
        as_of=date(2026, 7, 11),
        generated_at=datetime(2026, 7, 11, 8, 0, tzinfo=UTC),
    )

    assert captured["date_from"] == ""
    assert captured["date_to"] == ""
    assert captured["contours"] == {"cargo", "ved_import"}
    assert captured["blank_contour_cargo_dropoff_only"] is False
    assert captured["filter_contours_in_sql"] is True
    assert captured["fail_on_query_limit"] is True
    assert payload["order_count"] == 2
    assert payload["schema_version"] == 2
    assert [item["onec_ref"] for item in payload["orders"]] == ["0x01", "0x02"]
    assert json.loads(output.read_text(encoding="utf-8")) == payload


@pytest.mark.parametrize(
    ("orders", "message"),
    [
        ([_order("")], "empty onec_ref"),
        ([_order("0x01"), _order("0X01")], "duplicate procurement onec_ref"),
        ([_order("0x01", "ordinary")], "unsupported procurement contour"),
    ],
)
def test_validate_orders_rejects_invalid_identity_or_contour(orders, message) -> None:
    with pytest.raises(ValueError, match=message):
        snapshot.validate_orders(orders, limit=5000)


def test_validate_orders_rejects_a_result_at_the_query_limit() -> None:
    with pytest.raises(RuntimeError, match="may be truncated"):
        snapshot.validate_orders([_order("0x01")], limit=1)


def test_validation_failure_preserves_previous_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    output = tmp_path / "procurement.json"
    output.write_text('{"previous": true}\n', encoding="utf-8")
    monkeypatch.setattr(
        snapshot,
        "fetch_open_supplier_orders",
        lambda *_args, **_kwargs: [_order("0x01"), _order("0X01")],
    )

    with pytest.raises(ValueError, match="duplicate procurement onec_ref"):
        snapshot.build_snapshot("mssql://read-only", output=output)

    assert json.loads(output.read_text(encoding="utf-8")) == {"previous": True}


def test_empty_full_snapshot_is_valid() -> None:
    payload = snapshot.build_payload(
        [],
        limit=5000,
        as_of=date(2026, 7, 11),
        generated_at=datetime(2026, 7, 11, 8, 0, tzinfo=UTC),
    )

    assert payload["source_status"] == "ready"
    assert payload["order_count"] == 0
    assert payload["orders"] == []


def test_supplier_prepare_profiles_use_nearest_rank_p75() -> None:
    observations = [
        {"supplier_ref": "supplier-a", "procurement_contour_key": "cargo", "lead_days": value}
        for value in [3, 5, 7, 9, 20]
    ]

    profiles = snapshot.build_supplier_prepare_profiles(observations)

    supplier_profile = next(item for item in profiles if item["level"] == "supplier_contour")
    contour_profile = next(item for item in profiles if item["level"] == "contour")
    assert supplier_profile["sample_size"] == 5
    assert supplier_profile["p75_days"] == 9
    assert contour_profile["p75_days"] == 9
