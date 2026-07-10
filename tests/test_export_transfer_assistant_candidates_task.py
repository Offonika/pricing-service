from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from decimal import Decimal

from tasks import export_transfer_assistant_candidates as task


def _candidate() -> dict:
    return {
        "product": {"ref": "0xPRODUCT1", "code": "P-1", "name": "Дисплей iPhone"},
        "warehouse": {"ref": "0xWH1", "code": "WH-1", "name": "Основной склад"},
        "order": {"ref": "0xORDER1", "number": "РБ000001", "site_order_number": "S-1"},
        "source_document": {
            "type": "reserve_register",
            "ref": "0xORDER1",
            "number": "РБ000001",
        },
        "quantity": Decimal("2.5"),
        "status": "reserved_for_order",
        "reason": "stock is reserved or linked to a customer order",
        "onec_document_keys": {"order_ref": "0xORDER1"},
        "fact_date": datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc),
        "data_source": "test",
        "measures": {
            "stock_quantity": Decimal("0"),
            "reserved_quantity": Decimal("2.5"),
            "placement_quantity": Decimal("0"),
            "order_quantity": Decimal("0"),
            "issued_quantity": Decimal("0"),
            "return_quantity": Decimal("0"),
        },
        "pickup_deadline": None,
        "pickup_deadline_source": None,
    }


def test_transfer_assistant_export_writes_csv(tmp_path) -> None:
    output_path = tmp_path / "candidates.csv"

    task.write_candidates([_candidate()], output_format="csv", output_path=output_path)

    rows = list(csv.DictReader(output_path.read_text(encoding="utf-8-sig").splitlines()))
    assert rows[0]["status"] == "reserved_for_order"
    assert rows[0]["quantity"] == "2.5"
    assert rows[0]["product_name"] == "Дисплей iPhone"
    assert json.loads(rows[0]["onec_document_keys"]) == {"order_ref": "0xORDER1"}


def test_transfer_assistant_export_main_passes_filters(monkeypatch, tmp_path) -> None:
    captured = {}

    def fake_candidates(**kwargs):
        captured.update(kwargs)
        return [_candidate()]

    monkeypatch.setattr(
        task.transfer_assistant, "list_transfer_assistant_candidates", fake_candidates
    )
    output_path = tmp_path / "candidates.json"

    result = task.main(
        [
            "--date-from",
            "2026-07-01",
            "--date-to",
            "2026-07-02",
            "--warehouse-id",
            "WH-1",
            "--status",
            "reserved_for_order",
            "--limit",
            "25",
            "--output",
            str(output_path),
        ]
    )

    assert result == 0
    assert captured["date_from"].isoformat() == "2026-07-01"
    assert captured["date_to"].isoformat() == "2026-07-02"
    assert captured["warehouse_id"] == "WH-1"
    assert captured["status"] == "reserved_for_order"
    assert captured["limit"] == 25
    assert json.loads(output_path.read_text(encoding="utf-8"))[0]["quantity"] == "2.5"


def test_transfer_assistant_export_can_filter_source_kind(monkeypatch) -> None:
    captured = {}

    class Settings:
        logistics_transfer_assistant_pickup_hold_days = 7

    def fake_fetch(**kwargs):
        captured.update(kwargs)
        return [
            {
                "product_ref": "0xPRODUCT1",
                "product_code": "P-1",
                "product_name": "Дисплей iPhone",
                "warehouse_ref": "0xWH1",
                "warehouse_code": "WH-1",
                "warehouse_name": "Основной склад",
                "quantity": Decimal("1"),
                "fact_date": datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc),
                "data_source": "1c:customer_order_placements",
                "source_document_type": "supplier_order",
                "source_document_ref": "0xSUPPLIER1",
                "placement_quantity": Decimal("1"),
                "order_ref": "0xORDER1",
                "order_number": "РБ000001",
            }
        ]

    monkeypatch.setattr(task, "get_settings", lambda: Settings())
    monkeypatch.setattr(task.transfer_assistant, "fetch_transfer_assistant_source_rows", fake_fetch)

    args = task._parse_args(
        [
            "--status",
            "reserved_for_order",
            "--source-kind",
            "placement",
            "--limit",
            "5",
        ]
    )
    items = task.load_candidates(args)

    assert captured["source_kinds"] == {"placement"}
    assert captured["limit"] == 1000
    assert items[0]["status"] == "reserved_for_order"
    assert items[0]["data_source"] == "1c:customer_order_placements"
