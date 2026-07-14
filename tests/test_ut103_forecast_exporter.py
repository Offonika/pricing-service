from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from app.services.exporters.ut103_forecast import (
    ExchangeResult,
    ForecastSalesMessage,
    ForecastSalesRow,
    build_forecast_sales_xml,
    list_exchange_results,
    parse_exchange_result,
    write_forecast_sales_message,
)


def test_build_forecast_sales_xml_uses_ut103_contract() -> None:
    message = ForecastSalesMessage(
        message_id="forecast-sales-task-test-001",
        rows=(
            ForecastSalesRow(
                nomenclature_code="РБ000074721",
                warehouse_code="РБ0000050",
                period="2026-08",
                forecast_qty=Decimal("88.000"),
                forecast_amount=Decimal("188000.00"),
            ),
        ),
    )

    payload = build_forecast_sales_xml(message)

    assert payload.startswith(b"<?xml")
    root = ET.fromstring(payload)
    assert root.findtext("Header/Schema") == "forecast_sales.v1"
    assert root.findtext("Header/Source") == "pricing-service"
    assert root.findtext("Items/Item/NomenclatureCode") == "РБ000074721"
    assert root.findtext("Items/Item/WarehouseCode") == "РБ0000050"
    assert root.findtext("Items/Item/Period") == "2026-08"
    assert root.findtext("Items/Item/ForecastQty") == "88.000"
    assert root.findtext("Items/Item/ForecastAmount") == "188000.00"


def test_write_forecast_sales_message_places_ready_xml_atomically(tmp_path: Path) -> None:
    message = ForecastSalesMessage(
        message_id="forecast-sales-task-test-001",
        rows=(
            ForecastSalesRow(
                nomenclature_code="РБ000074721",
                warehouse_code="РБ0000050",
                period="2026-08",
                forecast_qty=88,
                forecast_amount=188000,
            ),
        ),
    )

    output_path = write_forecast_sales_message(tmp_path, message)

    assert output_path == (
        tmp_path / "to_1c" / "new" / "forecast_sales_forecast-sales-task-test-001.ready.xml"
    )
    assert output_path.exists()
    assert not list((tmp_path / "to_1c" / "new").glob("*.tmp"))
    with pytest.raises(FileExistsError):
        write_forecast_sales_message(tmp_path, message)


def test_parse_and_list_exchange_results(tmp_path: Path) -> None:
    result_dir = tmp_path / "from_1c" / "new"
    result_dir.mkdir(parents=True)
    result_path = result_dir / "forecast_sales_task_test_001.result.xml"
    result_path.write_text(
        """<?xml version="1.0" encoding="windows-1251"?>
<ExchangeResult>
  <MessageId>forecast-sales-task-test-001</MessageId>
  <Status>success</Status>
  <ProcessedAt>09.05.2026 18:55:02</ProcessedAt>
  <Loaded>1</Loaded>
  <Failed>0</Failed>
  <Errors></Errors>
</ExchangeResult>""",
        encoding="windows-1251",
    )

    result = parse_exchange_result(result_path)

    assert result == ExchangeResult(
        message_id="forecast-sales-task-test-001",
        status="success",
        processed_at="09.05.2026 18:55:02",
        loaded=1,
        failed=0,
        errors="",
        path=result_path,
    )
    assert result.ok
    assert list_exchange_results(tmp_path) == [result]
