from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree as ET


def test_export_ut103_forecast_task_writes_ready_xml(tmp_path: Path) -> None:
    input_path = tmp_path / "forecast.json"
    input_path.write_text(
        json.dumps(
            [
                {
                    "nomenclature_code": "РБ000074721",
                    "warehouse_code": "РБ0000050",
                    "period": "2026-08",
                    "forecast_qty": "88",
                    "forecast_amount": "188000.00",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tasks.export_ut103_forecast",
            "--exchange-root",
            str(tmp_path / "exchange"),
            "--message-id",
            "forecast-sales-task-test-001",
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
    assert summary["message_id"] == "forecast-sales-task-test-001"
    assert summary["rows"] == 1
    assert output_path.exists()

    root = ET.fromstring(output_path.read_bytes())
    assert root.findtext("Header/MessageId") == "forecast-sales-task-test-001"
    assert root.findtext("Items/Item/NomenclatureCode") == "РБ000074721"


def test_export_ut103_forecast_task_lists_results(tmp_path: Path) -> None:
    result_dir = tmp_path / "exchange" / "from_1c" / "new"
    result_dir.mkdir(parents=True)
    (result_dir / "forecast.result.xml").write_text(
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

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tasks.export_ut103_forecast",
            "--exchange-root",
            str(tmp_path / "exchange"),
            "--list-results",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(result.stdout)[0]["status"] == "success"
