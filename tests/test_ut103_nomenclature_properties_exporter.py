from __future__ import annotations

import stat
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from app.services.exporters.ut103_nomenclature_properties import (
    NomenclaturePropertyUpdateMessage,
    NomenclaturePropertyUpdateRow,
    PropertyUpdateExchangeResult,
    PropertyUpdateItemResult,
    build_nomenclature_property_updates_xml,
    list_property_update_exchange_results,
    parse_property_update_exchange_result,
    write_nomenclature_property_updates_message,
)


def _status_row() -> NomenclaturePropertyUpdateRow:
    return NomenclaturePropertyUpdateRow(
        idempotency_key="nom-prop:РБ000074721:Статус ассортимента:2026-06-23:r1",
        nomenclature_code="РБ000074721",
        property_name="Статус ассортимента",
        value_type="property_value",
        new_value_name="Новинка",
        new_value_tag="new_item",
        reason="Первый заказ поставщику сдан в cargo",
    )


def test_build_nomenclature_property_updates_xml_uses_contract() -> None:
    message = NomenclaturePropertyUpdateMessage(
        message_id="nomenclature-properties-test-001",
        mode="dry_run",
        rows=(_status_row(),),
    )

    payload = build_nomenclature_property_updates_xml(message)

    assert payload.startswith(b"<?xml")
    assert payload.decode("windows-1251")
    root = ET.fromstring(payload)
    assert root.findtext("Header/MessageId") == "nomenclature-properties-test-001"
    assert root.findtext("Header/Schema") == "nomenclature_property_updates.v1"
    assert root.findtext("Header/Source") == "pricing-service"
    assert root.findtext("Header/Target") == "1c_ut_10_3"
    assert root.findtext("Header/Mode") == "dry_run"
    assert (
        root.findtext("Items/Item/IdempotencyKey")
        == "nom-prop:РБ000074721:Статус ассортимента:2026-06-23:r1"
    )
    assert root.findtext("Items/Item/NomenclatureCode") == "РБ000074721"
    assert root.findtext("Items/Item/PropertyName") == "Статус ассортимента"
    assert root.findtext("Items/Item/ValueType") == "property_value"
    assert root.findtext("Items/Item/NewValueName") == "Новинка"
    assert root.findtext("Items/Item/NewValueTag") == "new_item"
    assert root.findtext("Items/Item/Reason") == "Первый заказ поставщику сдан в cargo"


def test_build_nomenclature_property_updates_xml_allows_commercial_marks() -> None:
    message = NomenclaturePropertyUpdateMessage(
        message_id="nomenclature-properties-commercial-mark-001",
        mode="dry_run",
        rows=(
            NomenclaturePropertyUpdateRow(
                idempotency_key="nom-prop:РБ000074721:Коммерческие признаки:2026-06-27:r1",
                nomenclature_code="РБ000074721",
                property_name="Коммерческие признаки",
                value_type="string",
                new_value="exclusive",
                reason="Единственный товар на рынке",
            ),
        ),
    )

    root = ET.fromstring(build_nomenclature_property_updates_xml(message))

    assert root.findtext("Items/Item/PropertyName") == "Коммерческие признаки"
    assert root.findtext("Items/Item/NewValue") == "exclusive"


def test_build_nomenclature_property_updates_xml_allows_subject_property() -> None:
    message = NomenclaturePropertyUpdateMessage(
        message_id="nomenclature-properties-subject-001",
        mode="dry_run",
        rows=(
            NomenclaturePropertyUpdateRow(
                idempotency_key="nom-prop:РБ000074721:Предмет:2026-06-27:r1",
                nomenclature_code="РБ000074721",
                property_name="Предмет",
                value_type="property_value",
                new_value_name="дисплей",
                reason="Автоклассификация пустого свойства Предмет",
            ),
        ),
    )

    root = ET.fromstring(build_nomenclature_property_updates_xml(message))

    assert root.findtext("Items/Item/PropertyName") == "Предмет"
    assert root.findtext("Items/Item/ValueType") == "property_value"
    assert root.findtext("Items/Item/NewValueName") == "дисплей"


def test_build_nomenclature_property_updates_xml_requires_approval_for_apply() -> None:
    message = NomenclaturePropertyUpdateMessage(
        message_id="nomenclature-properties-test-apply-001",
        mode="apply",
        rows=(_status_row(),),
    )

    with pytest.raises(ValueError, match="ApprovedBy"):
        build_nomenclature_property_updates_xml(message)

    approved_message = NomenclaturePropertyUpdateMessage(
        message_id="nomenclature-properties-test-apply-001",
        mode="apply",
        approved_by="Арсений",
        rows=(_status_row(),),
    )
    root = ET.fromstring(build_nomenclature_property_updates_xml(approved_message))
    assert root.findtext("Header/ApprovedBy") == "Арсений"


def test_build_nomenclature_property_updates_xml_allows_any_non_empty_property_name() -> None:
    message = NomenclaturePropertyUpdateMessage(
        message_id="nomenclature-properties-test-any-property-001",
        rows=(
            NomenclaturePropertyUpdateRow(
                idempotency_key="nom-prop:РБ000074721:Произвольное свойство:2026-06-23:r1",
                nomenclature_code="РБ000074721",
                property_name="Произвольное свойство",
                value_type="string",
                new_value="test",
            ),
        ),
    )

    root = ET.fromstring(build_nomenclature_property_updates_xml(message))

    assert root.findtext("Items/Item/PropertyName") == "Произвольное свойство"
    assert root.findtext("Items/Item/NewValue") == "test"


def test_build_nomenclature_property_updates_xml_allows_requisite_target() -> None:
    message = NomenclaturePropertyUpdateMessage(
        message_id="nomenclature-requisite-sku-001",
        rows=(
            NomenclaturePropertyUpdateRow(
                idempotency_key="nom-prop:РБ000074721:SKU:2026-07-01:r1",
                nomenclature_code="РБ000074721",
                target_kind="requisite",
                property_name="SKU",
                value_type="string",
                new_value="F5-DSP-IPH12-OLD-BLK-CPH",
            ),
        ),
    )

    root = ET.fromstring(build_nomenclature_property_updates_xml(message))

    assert root.findtext("Items/Item/TargetKind") == "requisite"
    assert root.findtext("Items/Item/PropertyName") == "SKU"
    assert root.findtext("Items/Item/NewValue") == "F5-DSP-IPH12-OLD-BLK-CPH"


def test_write_nomenclature_property_updates_message_places_ready_xml_atomically(
    tmp_path: Path,
) -> None:
    message = NomenclaturePropertyUpdateMessage(
        message_id="nomenclature-properties-test-001",
        rows=(_status_row(),),
    )

    output_path = write_nomenclature_property_updates_message(tmp_path, message)

    assert output_path == (
        tmp_path
        / "to_1c"
        / "new"
        / "nomenclature_properties_nomenclature-properties-test-001.ready.xml"
    )
    assert output_path.exists()
    assert stat.S_IMODE(output_path.stat().st_mode) == 0o660
    assert not list((tmp_path / "to_1c" / "new").glob("*.tmp"))
    with pytest.raises(FileExistsError):
        write_nomenclature_property_updates_message(tmp_path, message)


def test_parse_and_list_property_update_exchange_results(tmp_path: Path) -> None:
    result_dir = tmp_path / "from_1c" / "new"
    result_dir.mkdir(parents=True)
    result_path = result_dir / "nomenclature_properties_task_test_001.result.xml"
    result_path.write_text(
        """<?xml version="1.0" encoding="windows-1251"?>
<ExchangeResult>
  <MessageId>nomenclature-properties-test-001</MessageId>
  <Status>success</Status>
  <ProcessedAt>23.06.2026 18:55:02</ProcessedAt>
  <Loaded>7 711</Loaded>
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

    result = parse_property_update_exchange_result(result_path)

    assert result == PropertyUpdateExchangeResult(
        message_id="nomenclature-properties-test-001",
        status="success",
        processed_at="23.06.2026 18:55:02",
        loaded=7711,
        failed=0,
        errors="",
        item_results=(
            PropertyUpdateItemResult(
                idempotency_key="nom-prop:РБ000074721:Статус ассортимента:2026-06-23:r1",
                nomenclature_code="РБ000074721",
                property_name="Статус ассортимента",
                result="validated",
                message="",
                current_value="",
                new_value="Новинка",
            ),
        ),
        path=result_path,
    )
    assert result.ok
    assert list_property_update_exchange_results(tmp_path) == [result]
