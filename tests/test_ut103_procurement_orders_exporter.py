from __future__ import annotations

import stat
from datetime import date
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from app.services.exporters.ut103_procurement_orders import (
    OneCReference,
    ProcurementSupplierOrder,
    ProcurementSupplierOrderExchangeResult,
    ProcurementSupplierOrderItemResult,
    ProcurementSupplierOrderLine,
    ProcurementSupplierOrderMessage,
    build_procurement_supplier_orders_xml,
    list_procurement_supplier_order_exchange_results,
    parse_procurement_supplier_order_exchange_result,
    write_procurement_supplier_orders_message,
)


def _supplier_order(*, draft_only: bool = True) -> ProcurementSupplierOrder:
    return ProcurementSupplierOrder(
        idempotency_key="proc-order:DISPLAY-AUTO-203-RB1:r1",
        order_date=date(2026, 7, 5),
        procurement_contour="ordinary",
        supplier=OneCReference(code="SUP-001", name="Поставщик тест"),
        contract=OneCReference(ref="0xcontract", name="Основной договор"),
        warehouse=OneCReference(code="MAIN", name="Основной склад"),
        currency="RUB",
        bitrix_item_url="https://master.bitrix24.ru/crm/type/1132/details/777/",
        confirmation_id="bitrix-approval-777",
        calculation_id="display-auto-order-run-203",
        draft_only=draft_only,
        lines=(
            ProcurementSupplierOrderLine(
                nomenclature=OneCReference(code="РБ000074721", name="Дисплей тест"),
                quantity="5",
                price="1250.50",
                comment="Автозаказ витрины после подтверждения",
                calculation_line_id="display-auto-order-run-203:РБ000074721",
            ),
        ),
    )


def test_build_procurement_supplier_orders_xml_uses_contract() -> None:
    message = ProcurementSupplierOrderMessage(
        message_id="procurement-supplier-orders-test-001",
        mode="apply",
        approved_by="Омар",
        orders=(_supplier_order(),),
    )

    payload = build_procurement_supplier_orders_xml(message)

    assert payload.startswith(b"<?xml")
    assert payload.decode("windows-1251")
    root = ET.fromstring(payload)
    assert root.findtext("Header/MessageId") == "procurement-supplier-orders-test-001"
    assert root.findtext("Header/Schema") == "procurement_onec_file_exchange.v1"
    assert root.findtext("Header/Source") == "pricing-service"
    assert root.findtext("Header/Target") == "1c_ut_10_3"
    assert root.findtext("Header/Mode") == "apply"
    assert root.findtext("Header/ApprovedBy") == "Омар"
    assert (
        root.findtext("SupplierOrders/SupplierOrder/IdempotencyKey")
        == "proc-order:DISPLAY-AUTO-203-RB1:r1"
    )
    assert root.findtext("SupplierOrders/SupplierOrder/DraftOnly") == "true"
    assert root.findtext("SupplierOrders/SupplierOrder/OrderDate") == "2026-07-05"
    assert root.findtext("SupplierOrders/SupplierOrder/ProcurementContour") == "Обычный"
    assert root.findtext("SupplierOrders/SupplierOrder/Supplier/Code") == "SUP-001"
    assert root.findtext("SupplierOrders/SupplierOrder/Contract/Ref") == "0xcontract"
    assert root.findtext("SupplierOrders/SupplierOrder/Warehouse/Code") == "MAIN"
    line = root.find("SupplierOrders/SupplierOrder/Lines/Line")
    assert line is not None
    assert line.findtext("Nomenclature/Code") == "РБ000074721"
    assert line.findtext("Quantity") == "5"
    assert line.findtext("Price") == "1250.50"
    assert line.findtext("Currency") == "RUB"


def test_build_procurement_supplier_orders_xml_requires_approval_for_apply() -> None:
    message = ProcurementSupplierOrderMessage(
        message_id="procurement-supplier-orders-test-apply-001",
        mode="apply",
        orders=(_supplier_order(),),
    )

    with pytest.raises(ValueError, match="ApprovedBy"):
        build_procurement_supplier_orders_xml(message)


def test_build_procurement_supplier_orders_xml_rejects_non_draft_order() -> None:
    message = ProcurementSupplierOrderMessage(
        message_id="procurement-supplier-orders-not-draft-001",
        orders=(_supplier_order(draft_only=False),),
    )
    with pytest.raises(ValueError, match="draft_only"):
        build_procurement_supplier_orders_xml(message)


def test_write_procurement_supplier_orders_message_places_ready_xml_atomically(
    tmp_path: Path,
) -> None:
    message = ProcurementSupplierOrderMessage(
        message_id="procurement-supplier-orders-test-001",
        orders=(_supplier_order(),),
    )

    output_path = write_procurement_supplier_orders_message(tmp_path, message)

    assert output_path == (
        tmp_path
        / "to_1c"
        / "new"
        / "procurement_supplier_orders_procurement-supplier-orders-test-001.ready.xml"
    )
    assert output_path.exists()
    assert stat.S_IMODE(output_path.stat().st_mode) == 0o660
    assert not list((tmp_path / "to_1c" / "new").glob("*.tmp"))
    with pytest.raises(FileExistsError):
        write_procurement_supplier_orders_message(tmp_path, message)


def test_parse_and_list_procurement_supplier_order_exchange_results(tmp_path: Path) -> None:
    result_dir = tmp_path / "from_1c" / "new"
    result_dir.mkdir(parents=True)
    result_path = result_dir / "procurement_supplier_orders_task_test_001.result.xml"
    result_path.write_text(
        """<?xml version="1.0" encoding="windows-1251"?>
<ExchangeResult>
  <MessageId>procurement-supplier-orders-test-001</MessageId>
  <Status>success</Status>
  <ProcessedAt>05.07.2026 18:55:02</ProcessedAt>
  <Loaded>1</Loaded>
  <Failed>0</Failed>
  <Errors></Errors>
  <OrderResults>
    <OrderResult>
      <IdempotencyKey>proc-order:DISPLAY-AUTO-203-RB1:r1</IdempotencyKey>
      <Result>validated</Result>
      <OnecDocumentRef>0xorder</OnecDocumentRef>
      <OnecDocumentNumber>РБГУ000001</OnecDocumentNumber>
      <OnecDocumentDate>2026-07-05</OnecDocumentDate>
      <Message>dry_run: черновик можно создать</Message>
    </OrderResult>
  </OrderResults>
</ExchangeResult>""",
        encoding="windows-1251",
    )

    result = parse_procurement_supplier_order_exchange_result(result_path)

    assert result == ProcurementSupplierOrderExchangeResult(
        message_id="procurement-supplier-orders-test-001",
        status="success",
        processed_at="05.07.2026 18:55:02",
        loaded=1,
        failed=0,
        errors="",
        item_results=(
            ProcurementSupplierOrderItemResult(
                idempotency_key="proc-order:DISPLAY-AUTO-203-RB1:r1",
                result="validated",
                message="dry_run: черновик можно создать",
                onec_document_ref="0xorder",
                onec_document_number="РБГУ000001",
                onec_document_date="2026-07-05",
            ),
        ),
        path=result_path,
    )
    assert list_procurement_supplier_order_exchange_results(tmp_path) == [result]
