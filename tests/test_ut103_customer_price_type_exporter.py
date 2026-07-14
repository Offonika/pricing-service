from __future__ import annotations

import stat
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from app.services.exporters.ut103_customer_price_types import (
    CustomerPriceTypeUpdateMessage,
    CustomerPriceTypeUpdateRow,
    build_customer_price_type_updates_xml,
    list_customer_price_type_exchange_results,
    one_c_guid_from_counterparty_ref,
    parse_customer_price_type_exchange_result,
    write_customer_price_type_updates_message,
)

COUNTERPARTY_REF = "0X8FDA0025901E48EE11ED222EA7D9B21E"
COUNTERPARTY_GUID = "2500da8f-1e90-ee48-11ed-222ea7d9b21e"


def _row() -> CustomerPriceTypeUpdateRow:
    return CustomerPriceTypeUpdateRow(
        idempotency_key="customer-price-type:bronze-to-retail-202606:0X8FDA0025901E48EE11ED222EA7D9B21E",
        counterparty_ref=COUNTERPARTY_REF,
        counterparty_guid=COUNTERPARTY_GUID,
        counterparty_name="МикроСервис",
        expected_current_price_type="2.Бронзовый",
        target_price_type="Розница",
        reason="Утвержденный список за июнь",
    )


def test_one_c_guid_from_sql_counterparty_ref_uses_little_endian_layout() -> None:
    assert one_c_guid_from_counterparty_ref(COUNTERPARTY_REF) == COUNTERPARTY_GUID


def test_build_customer_price_type_updates_xml_uses_safe_contract() -> None:
    root = ET.fromstring(
        build_customer_price_type_updates_xml(
            CustomerPriceTypeUpdateMessage(
                message_id="bronze-to-retail-202606-dry-run",
                mode="dry_run",
                rows=(_row(),),
            )
        )
    )

    assert root.findtext("Header/Schema") == "customer_price_type_updates.v1"
    assert root.findtext("Header/Mode") == "dry_run"
    assert root.findtext("Items/Item/CounterpartyRef") == COUNTERPARTY_REF
    assert root.findtext("Items/Item/CounterpartyGuid") == COUNTERPARTY_GUID
    assert root.findtext("Items/Item/ExpectedCurrentPriceType") == "2.Бронзовый"
    assert root.findtext("Items/Item/TargetPriceType") == "Розница"
    assert root.findtext("Items/Item/Decision") == "approved_for_manual_1c_update"


def test_apply_package_requires_approval() -> None:
    message = CustomerPriceTypeUpdateMessage(
        message_id="bronze-to-retail-202606-apply",
        mode="apply",
        rows=(_row(),),
    )
    with pytest.raises(ValueError, match="ApprovedBy"):
        build_customer_price_type_updates_xml(message)

    root = ET.fromstring(
        build_customer_price_type_updates_xml(
            CustomerPriceTypeUpdateMessage(
                message_id="bronze-to-retail-202606-apply",
                mode="apply",
                approved_by="Арсений",
                rows=(_row(),),
            )
        )
    )
    assert root.findtext("Header/ApprovedBy") == "Арсений"


def test_contract_rejects_any_nonapproved_price_type_change() -> None:
    unsafe_row = CustomerPriceTypeUpdateRow(**{**_row().__dict__, "target_price_type": "4.Золотой"})
    with pytest.raises(ValueError, match="target_price_type"):
        build_customer_price_type_updates_xml(
            CustomerPriceTypeUpdateMessage(message_id="unsafe", rows=(unsafe_row,))
        )


def test_contract_rejects_duplicate_counterparty_before_writing() -> None:
    duplicate = CustomerPriceTypeUpdateRow(
        **{
            **_row().__dict__,
            "idempotency_key": "customer-price-type:another-batch:0X8FDA0025901E48EE11ED222EA7D9B21E",
        }
    )
    with pytest.raises(ValueError, match="duplicate counterparty_ref"):
        build_customer_price_type_updates_xml(
            CustomerPriceTypeUpdateMessage(message_id="duplicate", rows=(_row(), duplicate))
        )


def test_write_and_parse_customer_price_type_result(tmp_path: Path) -> None:
    message = CustomerPriceTypeUpdateMessage(message_id="bronze-to-retail-202606", rows=(_row(),))
    output_path = write_customer_price_type_updates_message(tmp_path, message)
    assert output_path == (
        tmp_path / "to_1c" / "new" / "customer_price_types_bronze-to-retail-202606.ready.xml"
    )
    assert stat.S_IMODE(output_path.stat().st_mode) == 0o660

    result_dir = tmp_path / "from_1c" / "new"
    result_dir.mkdir(parents=True)
    result_path = result_dir / "customer_price_types_bronze-to-retail-202606.result.xml"
    result_path.write_text(
        f"""<?xml version="1.0" encoding="windows-1251"?>
<ExchangeResult>
  <MessageId>bronze-to-retail-202606</MessageId>
  <Status>success</Status>
  <ProcessedAt>2026-07-12T10:00:00</ProcessedAt>
  <Loaded>1</Loaded>
  <Failed>0</Failed>
  <Errors></Errors>
  <ItemResults>
    <ItemResult>
      <IdempotencyKey>{_row().idempotency_key}</IdempotencyKey>
      <CounterpartyRef>{COUNTERPARTY_REF}</CounterpartyRef>
      <CounterpartyGuid>{COUNTERPARTY_GUID}</CounterpartyGuid>
      <CounterpartyName>МикроСервис</CounterpartyName>
      <Result>validated</Result>
      <Message>Строка проверена, запись не выполнялась</Message>
      <ContractGuid>c0000000-0000-0000-0000-000000000001</ContractGuid>
      <ContractName>Основной договор</ContractName>
      <CurrentPriceType>2.Бронзовый</CurrentPriceType>
      <TargetPriceType>Розница</TargetPriceType>
      <FoundContracts>Основной договор</FoundContracts>
    </ItemResult>
  </ItemResults>
</ExchangeResult>""",
        encoding="windows-1251",
    )

    result = parse_customer_price_type_exchange_result(result_path)
    assert result.ok
    assert result.item_results[0].result == "validated"
    assert result.item_results[0].current_price_type == "2.Бронзовый"
    assert list_customer_price_type_exchange_results(tmp_path) == [result]
