from __future__ import annotations

from tasks.export_display_auto_order_site_events import normalize_export_rows


def test_export_anonymizes_fuser_and_keeps_no_personal_fields() -> None:
    source = {
        "event_date": "2026-07-01",
        "event_type": "site_unordered_cart",
        "product_xml_id": "2685293e-967c-11e1-bdb9-0025901e48ef",
        "quantity": "2",
        "fuser_id": "12345",
        "delay_flag": "N",
        "email": "customer@example.test",
        "phone": "+79990000000",
    }

    first = normalize_export_rows([source], salt=b"fixed-test-salt")[0]
    second = normalize_export_rows([source], salt=b"fixed-test-salt")[0]

    assert first == second
    assert first["session_key"] != "12345"
    assert len(first["session_key"]) == 64
    assert "fuser_id" not in first
    assert "email" not in first
    assert "phone" not in first


def test_order_event_key_is_stable_and_quantity_is_non_negative() -> None:
    row = {
        "event_date": "2026-02-01 12:00:00",
        "event_type": "site_order",
        "product_xml_id": "guid",
        "quantity": "-2",
        "order_id": "77",
        "order_number": "220077",
        "fuser_id": "12345",
        "cancelled_at": "2026-02-05 10:00:00",
    }

    result = normalize_export_rows([row], salt=b"fixed-test-salt")[0]

    assert result["event_date"] == "2026-02-01"
    assert result["cancelled_at"] == "2026-02-05"
    assert result["quantity"] == "0"
    assert result["event_key"]
    assert result["order_number"] == "220077"
