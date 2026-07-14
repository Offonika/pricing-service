from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree as ET


def _json_order() -> dict[str, object]:
    return {
        "idempotency_key": "proc-order:DISPLAY-AUTO-203-RB1:r1",
        "order_date": "2026-07-05",
        "procurement_contour": "ordinary",
        "supplier_code": "SUP-001",
        "supplier_name": "Поставщик тест",
        "contract_ref": "0xcontract",
        "warehouse_code": "MAIN",
        "currency": "RUB",
        "bitrix_item_url": "https://master.bitrix24.ru/crm/type/1132/details/777/",
        "confirmation_id": "bitrix-approval-777",
        "calculation_id": "display-auto-order-run-203",
        "lines": [
            {
                "nomenclature_code": "РБ000074721",
                "nomenclature_name": "Дисплей тест",
                "quantity": "5",
                "price": "1250.50",
                "comment": "Автозаказ витрины после подтверждения",
            }
        ],
    }


def test_export_ut103_procurement_supplier_orders_task_writes_ready_xml_from_json(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "supplier-order.json"
    input_path.write_text(json.dumps(_json_order(), ensure_ascii=False), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tasks.export_ut103_procurement_supplier_orders",
            "--exchange-root",
            str(tmp_path / "exchange"),
            "--message-id",
            "procurement-supplier-orders-task-test-001",
            "--input-json",
            str(input_path),
            "--json",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    summary = json.loads(result.stdout)
    output_path = Path(summary["path"])
    assert summary["message_id"] == "procurement-supplier-orders-task-test-001"
    assert summary["mode"] == "dry_run"
    assert summary["orders"] == 1
    assert summary["lines"] == 1
    assert output_path.exists()

    root = ET.fromstring(output_path.read_bytes())
    assert root.findtext("Header/Schema") == "procurement_onec_file_exchange.v1"
    assert root.findtext("Header/Mode") == "dry_run"
    assert root.findtext("SupplierOrders/SupplierOrder/DraftOnly") == "true"
    assert root.findtext("SupplierOrders/SupplierOrder/Supplier/Code") == "SUP-001"
    assert (
        root.findtext("SupplierOrders/SupplierOrder/Lines/Line/Nomenclature/Code") == "РБ000074721"
    )


def test_export_ut103_procurement_supplier_orders_task_uses_exchange_root_env(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "supplier-order.json"
    input_path.write_text(json.dumps(_json_order(), ensure_ascii=False), encoding="utf-8")
    exchange_root = tmp_path / "env-exchange"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tasks.export_ut103_procurement_supplier_orders",
            "--message-id",
            "procurement-smoke-001",
            "--input-json",
            str(input_path),
            "--json",
        ],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "UT103_EXCHANGE_ROOT": str(exchange_root)},
    )

    summary = json.loads(result.stdout)
    assert Path(summary["path"]) == (
        exchange_root
        / "to_1c"
        / "new"
        / "procurement_supplier_orders_procurement-smoke-001.ready.xml"
    )
    assert Path(summary["path"]).exists()


def test_export_ut103_procurement_supplier_orders_task_dry_run_does_not_write(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "supplier-order.json"
    input_path.write_text(json.dumps(_json_order(), ensure_ascii=False), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tasks.export_ut103_procurement_supplier_orders",
            "--exchange-root",
            str(tmp_path / "exchange"),
            "--message-id",
            "procurement-supplier-orders-task-test-001",
            "--mode",
            "apply",
            "--approved-by",
            "Омар",
            "--input-json",
            str(input_path),
            "--dry-run",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "<Schema>procurement_onec_file_exchange.v1</Schema>" in result.stdout
    assert "<ApprovedBy>Омар</ApprovedBy>" in result.stdout
    assert "<DraftOnly>true</DraftOnly>" in result.stdout
    assert not (tmp_path / "exchange" / "to_1c").exists()


def test_export_ut103_procurement_supplier_orders_task_lists_results(tmp_path: Path) -> None:
    result_dir = tmp_path / "exchange" / "from_1c" / "new"
    result_dir.mkdir(parents=True)
    (result_dir / "procurement_supplier_orders_task_test_001.result.xml").write_text(
        """<?xml version="1.0" encoding="windows-1251"?>
<ExchangeResult>
  <MessageId>procurement-supplier-orders-task-test-001</MessageId>
  <Status>success</Status>
  <ProcessedAt>05.07.2026 18:55:02</ProcessedAt>
  <Loaded>1</Loaded>
  <Failed>0</Failed>
  <Errors></Errors>
  <OrderResults>
    <OrderResult>
      <IdempotencyKey>proc-order:DISPLAY-AUTO-203-RB1:r1</IdempotencyKey>
      <Result>validated</Result>
      <Message>dry_run: черновик можно создать</Message>
      <OnecDocumentRef>0xorder</OnecDocumentRef>
      <OnecDocumentNumber>РБГУ000001</OnecDocumentNumber>
      <OnecDocumentDate>2026-07-05</OnecDocumentDate>
    </OrderResult>
  </OrderResults>
</ExchangeResult>""",
        encoding="windows-1251",
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tasks.export_ut103_procurement_supplier_orders",
            "--exchange-root",
            str(tmp_path / "exchange"),
            "--list-results",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    output = json.loads(result.stdout)
    assert output[0]["status"] == "success"
    assert output[0]["item_results"][0]["result"] == "validated"
    assert output[0]["item_results"][0]["onec_document_number"] == "РБГУ000001"
