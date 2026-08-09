from __future__ import annotations

import stat
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from app.services.exporters.ut103_credit_terms import (
    MAX_MESSAGE_ID_LENGTH,
    CreditTermsCommand,
    CreditTermsMessage,
    build_credit_terms_xml,
    build_receivable_credit_decision_message_id,
    list_credit_terms_results,
    parse_credit_terms_result,
    write_credit_terms_message,
)

COUNTERPARTY_REF = "0X8FDA0025901E48EE11ED222EA7D9B21E"
COUNTERPARTY_GUID = "a7d9b21e-222e-11ed-8fda-0025901e48ee"
CONTRACT_REF = "0X8266002590803DAF11F143B8070BC34D"
CONTRACT_GUID = "070bc34d-43b8-11f1-8266-002590803daf"
ORGANIZATION_REF = "0X44445555555555553333222211111111"
ORGANIZATION_GUID = "11111111-2222-3333-4444-555555555555"
DECISION_HASH = "a" * 64
MESSAGE_ID = build_receivable_credit_decision_message_id(
    entity_type_id=1200,
    item_id="2494",
    revision="7",
    decision_hash=DECISION_HASH,
    suffix="dry-run",
)


def _command(**overrides: object) -> CreditTermsCommand:
    values: dict[str, object] = {
        "idempotency_key": "receivable-decision:2494:7",
        "decision_id": "2494",
        "decision_hash": DECISION_HASH,
        "revision": "7",
        "counterparty_ref": COUNTERPARTY_REF,
        "counterparty_guid": COUNTERPARTY_GUID,
        "counterparty_code": "РБ030337",
        "counterparty_name": "Тестовый контрагент",
        "contract_ref": CONTRACT_REF,
        "contract_guid": CONTRACT_GUID,
        "contract_code": "РБ0058149",
        "contract_name": "Основной договор1",
        "contract_organization_ref": ORGANIZATION_REF,
        "contract_organization_guid": ORGANIZATION_GUID,
        "contract_organization_code": "000000001",
        "contract_organization_name": "MASTER MOBILE",
        "expected_current_limit": Decimal("100000.00"),
        "expected_current_depth": 7,
        "expected_current_debt_control_enabled": True,
        "new_limit": Decimal("150000.00"),
        "new_depth": 14,
        "new_debt_control_enabled": True,
        "currency": "RUB",
        "reason": "Утверждено финансовым директором",
        "approved_by": "115204",
        "approved_at": datetime(2026, 7, 28, 10, 0, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return CreditTermsCommand(**values)  # type: ignore[arg-type]


def test_build_credit_terms_xml_keeps_atomic_pair_and_windows_encoding() -> None:
    payload = build_credit_terms_xml(
        CreditTermsMessage(message_id=MESSAGE_ID, commands=(_command(),))
    )
    assert b"encoding='windows-1251'" in payload
    root = ET.fromstring(payload)
    assert root.findtext("Header/Schema") == "onec_commands.v1"
    assert root.findtext("Header/Mode") == "dry_run"
    assert root.findtext("Commands/Command/CommandType") == "set_credit_terms"
    assert root.findtext("Commands/Command/ContractRef") == CONTRACT_REF
    assert root.findtext("Commands/Command/ContractGuid") == CONTRACT_GUID
    assert root.findtext("Commands/Command/ContractOrganizationGuid") == ORGANIZATION_GUID
    assert root.findtext("Commands/Command/ExpectedCurrentLimit") == "100000.00"
    assert root.findtext("Commands/Command/ExpectedCurrentDepth") == "7"
    assert root.findtext("Commands/Command/ExpectedCurrentDebtControlEnabled") == "true"
    assert root.findtext("Commands/Command/NewLimit") == "150000.00"
    assert root.findtext("Commands/Command/NewDepth") == "14"
    assert root.findtext("Commands/Command/NewDebtControlEnabled") == "true"
    assert root.findtext("Commands/Command/DecisionHash") == DECISION_HASH


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"new_limit": Decimal("-1")}, "negative"),
        ({"new_depth": -1}, "negative"),
        ({"new_depth": 1.5}, "integer"),
        ({"currency": "USD"}, "RUB"),
        ({"decision_hash": "not-a-hash"}, "SHA-256"),
        ({"decision_id": "manual-id"}, "numeric"),
        ({"counterparty_guid": "00000000-0000-0000-0000-000000000000"}, "does not match"),
        ({"contract_guid": "00000000-0000-0000-0000-000000000000"}, "does not match"),
        (
            {"contract_organization_guid": "00000000-0000-0000-0000-000000000000"},
            "does not match",
        ),
        ({"new_debt_control_enabled": "true"}, "boolean"),
        ({"new_limit": Decimal("10000000000000000")}, r"Numeric\(18,2\)"),
        ({"new_depth": 100000}, r"Numeric\(5,0\)"),
        ({"idempotency_key": "x" * 201}, "length limits"),
    ],
)
def test_credit_terms_validation_rejects_unsafe_values(
    overrides: dict[str, object], error: str
) -> None:
    with pytest.raises(ValueError, match=error):
        build_credit_terms_xml(
            CreditTermsMessage(message_id=MESSAGE_ID, commands=(_command(**overrides),))
        )


def test_zero_values_are_allowed() -> None:
    build_credit_terms_xml(
        CreditTermsMessage(
            message_id=MESSAGE_ID,
            commands=(
                _command(
                    expected_current_limit=Decimal("0"),
                    expected_current_depth=0,
                    expected_current_debt_control_enabled=False,
                    new_limit=Decimal("0"),
                    new_depth=0,
                    new_debt_control_enabled=False,
                ),
            ),
        )
    )


@pytest.mark.parametrize(
    "message_id",
    [
        " leading-space",
        "trailing-space ",
        "contains/slash",
        "кириллица",
        "x" * (MAX_MESSAGE_ID_LENGTH + 1),
    ],
)
def test_manual_message_id_is_rejected_without_sanitizing(message_id: str) -> None:
    with pytest.raises(ValueError, match="message_id"):
        build_credit_terms_xml(CreditTermsMessage(message_id=message_id, commands=(_command(),)))


def test_credit_terms_message_requires_exactly_one_command() -> None:
    with pytest.raises(ValueError, match="exactly one command"):
        build_credit_terms_xml(
            CreditTermsMessage(
                message_id=MESSAGE_ID,
                commands=(
                    _command(),
                    _command(
                        idempotency_key="receivable-decision:2494:8",
                        decision_id="2495",
                    ),
                ),
            )
        )


def test_manual_message_id_must_match_command_identity() -> None:
    wrong_identity = MESSAGE_ID.replace("aaaaaaaaaaaa", "bbbbbbbbbbbb")
    with pytest.raises(ValueError, match="command identity"):
        build_credit_terms_xml(
            CreditTermsMessage(message_id=wrong_identity, commands=(_command(),))
        )


def test_write_and_parse_credit_terms_result(tmp_path: Path) -> None:
    message = CreditTermsMessage(message_id=MESSAGE_ID, commands=(_command(),))
    output = write_credit_terms_message(tmp_path, message)
    assert output == (tmp_path / "to_1c" / "new" / f"onec_commands_{MESSAGE_ID}.ready.xml")
    assert stat.S_IMODE(output.stat().st_mode) == 0o660

    result_dir = tmp_path / "from_1c" / "new"
    result_dir.mkdir(parents=True)
    result_path = result_dir / f"onec_commands_{MESSAGE_ID}.result.xml"
    result_path.write_text(
        f"""<?xml version="1.0" encoding="windows-1251"?>
<ExchangeResult>
  <MessageId>{MESSAGE_ID}</MessageId>
  <Schema>onec_commands.v1</Schema>
  <Status>success</Status>
  <ProcessedAt>2026-07-28T13:00:00+03:00</ProcessedAt>
  <Loaded>1</Loaded>
  <Failed>0</Failed>
  <Errors></Errors>
  <CommandResults>
    <CommandResult>
      <IdempotencyKey>receivable-decision:2494:7</IdempotencyKey>
      <DecisionId>2494</DecisionId>
      <DecisionHash>{DECISION_HASH}</DecisionHash>
      <CounterpartyRef>{COUNTERPARTY_REF}</CounterpartyRef>
      <CounterpartyGuid>{COUNTERPARTY_GUID}</CounterpartyGuid>
      <CounterpartyCode>РБ030337</CounterpartyCode>
      <ContractRef>{CONTRACT_REF}</ContractRef>
      <ContractGuid>{CONTRACT_GUID}</ContractGuid>
      <ContractCode>РБ0058149</ContractCode>
      <ContractOrganizationRef>{ORGANIZATION_REF}</ContractOrganizationRef>
      <ContractOrganizationGuid>{ORGANIZATION_GUID}</ContractOrganizationGuid>
      <ContractOrganizationCode>000000001</ContractOrganizationCode>
      <Status>applied</Status>
      <Message>Условия записаны и прочитаны обратно</Message>
      <OldLimit>100000.00</OldLimit>
      <OldContractLimit>100000.00</OldContractLimit>
      <OldDepth>7</OldDepth>
      <OldDebtControlEnabled>true</OldDebtControlEnabled>
      <RequestedLimit>150000.00</RequestedLimit>
      <RequestedContractLimit>150000.00</RequestedContractLimit>
      <RequestedDepth>14</RequestedDepth>
      <RequestedDebtControlEnabled>true</RequestedDebtControlEnabled>
      <ReadbackLimit>150 000,00</ReadbackLimit>
      <ReadbackContractLimit>150 000,00</ReadbackContractLimit>
      <ReadbackDepth>14</ReadbackDepth>
      <ReadbackDebtControlEnabled>true</ReadbackDebtControlEnabled>
    </CommandResult>
  </CommandResults>
</ExchangeResult>""",
        encoding="windows-1251",
    )

    result = parse_credit_terms_result(result_path)
    assert result.ok
    assert result.command_results[0].readback_limit == Decimal("150000.00")
    assert result.command_results[0].readback_contract_limit == Decimal("150000.00")
    assert result.command_results[0].readback_depth == 14
    assert result.command_results[0].readback_debt_control_enabled is True
    assert list_credit_terms_results(tmp_path) == [result]


def test_parser_rejects_unknown_status() -> None:
    root = """<ExchangeResult>
<MessageId>x</MessageId><Schema>onec_commands.v1</Schema><Status>success</Status>
<Loaded>1</Loaded><Failed>0</Failed><Errors></Errors>
<CommandResults><CommandResult><IdempotencyKey>x</IdempotencyKey>
<Status>unknown</Status></CommandResult></CommandResults></ExchangeResult>"""
    with pytest.raises(ValueError, match="unsupported"):
        from app.services.exporters.ut103_credit_terms import _parse_command_result

        _parse_command_result(ET.fromstring(root).find("CommandResults/CommandResult"))
