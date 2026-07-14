from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree as ET


def _json_row() -> dict[str, str]:
    return {
        "idempotency_key": "nom-prop:РБ000074721:Статус ассортимента:2026-06-23:r1",
        "nomenclature_code": "РБ000074721",
        "property_name": "Статус ассортимента",
        "value_type": "property_value",
        "new_value_name": "Новинка",
        "new_value_tag": "new_item",
        "reason": "Первый заказ поставщику сдан в cargo",
    }


def test_export_ut103_nomenclature_properties_task_writes_ready_xml_from_json(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "property-updates.json"
    input_path.write_text(json.dumps([_json_row()], ensure_ascii=False), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tasks.export_ut103_nomenclature_properties",
            "--exchange-root",
            str(tmp_path / "exchange"),
            "--message-id",
            "nomenclature-properties-task-test-001",
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
    assert summary["message_id"] == "nomenclature-properties-task-test-001"
    assert summary["mode"] == "dry_run"
    assert summary["rows"] == 1
    assert output_path.exists()

    root = ET.fromstring(output_path.read_bytes())
    assert root.findtext("Header/Schema") == "nomenclature_property_updates.v1"
    assert root.findtext("Header/Mode") == "dry_run"
    assert root.findtext("Items/Item/NomenclatureCode") == "РБ000074721"
    assert root.findtext("Items/Item/PropertyName") == "Статус ассортимента"
    assert root.findtext("Items/Item/NewValueTag") == "new_item"


def test_export_ut103_nomenclature_properties_task_uses_exchange_root_env(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "property-updates.json"
    input_path.write_text(json.dumps([_json_row()], ensure_ascii=False), encoding="utf-8")
    exchange_root = tmp_path / "env-exchange"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tasks.export_ut103_nomenclature_properties",
            "--message-id",
            "smoke-001",
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
        exchange_root / "to_1c" / "new" / "nomenclature_properties_smoke-001.ready.xml"
    )
    assert Path(summary["path"]).exists()


def test_export_ut103_nomenclature_properties_task_dry_run_from_csv_does_not_write(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "property-updates.csv"
    input_path.write_text(
        "\n".join(
            [
                "IdempotencyKey,NomenclatureCode,PropertyName,ValueType,NewValueName,NewValueTag,Reason",
                (
                    "nom-prop:РБ000074721:Статус ассортимента:2026-06-23:r1,"
                    "РБ000074721,Статус ассортимента,property_value,"
                    "Новинка,new_item,Первый заказ сдан в cargo"
                ),
            ]
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tasks.export_ut103_nomenclature_properties",
            "--exchange-root",
            str(tmp_path / "exchange"),
            "--message-id",
            "nomenclature-properties-task-test-001",
            "--input-csv",
            str(input_path),
            "--dry-run",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "<Schema>nomenclature_property_updates.v1</Schema>" in result.stdout
    assert "<NewValueTag>new_item</NewValueTag>" in result.stdout
    assert not (tmp_path / "exchange" / "to_1c").exists()


def test_export_ut103_nomenclature_properties_task_lists_results(tmp_path: Path) -> None:
    result_dir = tmp_path / "exchange" / "from_1c" / "new"
    result_dir.mkdir(parents=True)
    (result_dir / "nomenclature_properties_task_test_001.result.xml").write_text(
        """<?xml version="1.0" encoding="windows-1251"?>
<ExchangeResult>
  <MessageId>nomenclature-properties-task-test-001</MessageId>
  <Status>success</Status>
  <ProcessedAt>23.06.2026 18:55:02</ProcessedAt>
  <Loaded>1</Loaded>
  <Failed>0</Failed>
  <Errors></Errors>
  <ItemResults>
    <ItemResult>
      <IdempotencyKey>nom-prop:РБ000074721:Статус ассортимента:2026-06-23:r1</IdempotencyKey>
      <NomenclatureCode>РБ000074721</NomenclatureCode>
      <PropertyName>Статус ассортимента</PropertyName>
      <Result>validated</Result>
      <Message></Message>
      <CurrentValue></CurrentValue>
      <NewValue>Новинка</NewValue>
    </ItemResult>
  </ItemResults>
</ExchangeResult>""",
        encoding="windows-1251",
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tasks.export_ut103_nomenclature_properties",
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
