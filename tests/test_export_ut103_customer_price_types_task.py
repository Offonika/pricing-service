from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree as ET


def _write_input(path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.DictWriter(
            csv_file,
            fieldnames=(
                "counterparty_ref",
                "counterparty_name",
                "current_price_type",
                "target_price_type",
                "decision",
                "reason",
            ),
        )
        writer.writeheader()
        writer.writerow(
            {
                "counterparty_ref": "0X8FDA0025901E48EE11ED222EA7D9B21E",
                "counterparty_name": "МикроСервис",
                "current_price_type": "2.Бронзовый",
                "target_price_type": "Розница",
                "decision": "approved_for_manual_1c_update",
                "reason": "Утвержденный список",
            }
        )


def test_export_customer_price_type_task_writes_ready_xml(tmp_path: Path) -> None:
    input_path = tmp_path / "approved.csv"
    _write_input(input_path)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tasks.export_ut103_customer_price_types",
            "--exchange-root",
            str(tmp_path / "exchange"),
            "--message-id",
            "bronze-to-retail-task-test-001",
            "--input-csv",
            str(input_path),
            "--json",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    summary = json.loads(result.stdout)
    output_path = Path(summary["path"])
    assert summary["mode"] == "dry_run"
    assert summary["rows"] == 1
    assert output_path.exists()
    root = ET.fromstring(output_path.read_bytes())
    assert root.findtext("Header/Schema") == "customer_price_type_updates.v1"
    assert root.findtext("Items/Item/CounterpartyGuid") == "2500da8f-1e90-ee48-11ed-222ea7d9b21e"


def test_export_customer_price_type_task_validate_only_does_not_write(tmp_path: Path) -> None:
    input_path = tmp_path / "approved.csv"
    _write_input(input_path)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tasks.export_ut103_customer_price_types",
            "--message-id",
            "bronze-to-retail-task-test-002",
            "--input-csv",
            str(input_path),
            "--validate-only",
            "--json",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    summary = json.loads(result.stdout)
    assert summary["validated_only"] is True
    assert summary["rows"] == 1
    assert not (tmp_path / "to_1c").exists()
