from __future__ import annotations

import stat
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from app.services.exporters.ut103_credit_terms import (
    CreditTermsCommand,
    CreditTermsMessage,
    build_credit_terms_xml,
    list_credit_terms_results,
    parse_credit_terms_result,
    write_credit_terms_message,
)

COUNTERPARTY_REF = "0X8FDA0025901E48EE11ED222EA7D9B21E"
COUNTERPARTY_GUID = "a7d9b21e-222e-11ed-8fda-0025901e48ee"
DECISION_HASH = "a" * 64


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
        "expected_current_limit": Decimal("100000.00"),
        "expected_current_depth": 7,
        "new_limit": Decimal("150000.00"),
        "new_depth": 14,
        "currency": "RUB",
        "reason": "Утверждено финансовым директором",
        "approved_by": "115204",
        "approved_at": datetime(2026, 7, 28, 10, 0, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return CreditTermsCommand(**values)  # type: ignore[arg-type]


def test_build_credit_terms_xml_keeps_atomic_pair_and_windows_encoding() -> None:
    payload = build_credit_terms_xml(
        CreditTermsMessage(message_id="decision-2494-7-dry-run", commands=(_command(),))
    )
    assert b"encoding='windows-1251'" in payload
    root = ET.fromstring(payload)
    assert root.findtext("Header/Schema") == "onec_commands.v1"
    assert root.findtext("Header/Mode") == "dry_run"
    assert root.findtext("Commands/Command/CommandType") == "set_credit_terms"
    assert root.findtext("Commands/Command/ExpectedCurrentLimit") == "100000.00"
    assert root.findtext("Commands/Command/ExpectedCurrentDepth") == "7"
    assert root.findtext("Commands/Command/NewLimit") == "150000.00"
    assert root.findtext("Commands/Command/NewDepth") == "14"
    assert root.findtext("Commands/Command/DecisionHash") == DECISION_HASH


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"new_limit": Decimal("-1")}, "negative"),
        ({"new_depth": -1}, "negative"),
        ({"new_depth": 1.5}, "integer"),
        ({"currency": "USD"}, "RUB"),
        ({"decision_hash": "not-a-hash"}, "SHA-256"),
        ({"counterparty_guid": "00000000-0000-0000-0000-000000000000"}, "does not match"),
    ],
)
def test_credit_terms_validation_rejects_unsafe_values(
    overrides: dict[str, object], error: str
) -> None:
    with pytest.raises(ValueError, match=error):
        build_credit_terms_xml(
            CreditTermsMessage(message_id="unsafe", commands=(_command(**overrides),))
        )


def test_zero_values_are_allowed() -> None:
    build_credit_terms_xml(
        CreditTermsMessage(
            message_id="zero-values",
            commands=(
                _command(
                    expected_current_limit=Decimal("0"),
                    expected_current_depth=0,
                    new_limit=Decimal("0"),
                    new_depth=0,
                ),
            ),
        )
    )


def test_duplicate_counterparty_is_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate counterparty"):
        build_credit_terms_xml(
            CreditTermsMessage(
                message_id="duplicate",
                commands=(
                    _command(),
                    _command(
                        idempotency_key="receivable-decision:2494:8",
                        decision_id="2495",
                    ),
                ),
            )
        )


def test_write_and_parse_credit_terms_result(tmp_path: Path) -> None:
    message = CreditTermsMessage(message_id="decision-2494-7", commands=(_command(),))
    output = write_credit_terms_message(tmp_path, message)
    assert output == (tmp_path / "to_1c" / "new" / "onec_commands_decision-2494-7.ready.xml")
    assert stat.S_IMODE(output.stat().st_mode) == 0o660

    result_dir = tmp_path / "from_1c" / "new"
    result_dir.mkdir(parents=True)
    result_path = result_dir / "onec_commands_decision-2494-7.result.xml"
    result_path.write_text(
        f"""<?xml version="1.0" encoding="windows-1251"?>
<ExchangeResult>
  <MessageId>decision-2494-7</MessageId>
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
      <Status>applied</Status>
      <Message>Условия записаны и прочитаны обратно</Message>
      <OldLimit>100000.00</OldLimit>
      <OldDepth>7</OldDepth>
      <RequestedLimit>150000.00</RequestedLimit>
      <RequestedDepth>14</RequestedDepth>
      <ReadbackLimit>150000.00</ReadbackLimit>
      <ReadbackDepth>14</ReadbackDepth>
    </CommandResult>
  </CommandResults>
</ExchangeResult>""",
        encoding="windows-1251",
    )

    result = parse_credit_terms_result(result_path)
    assert result.ok
    assert result.command_results[0].readback_limit == Decimal("150000.00")
    assert result.command_results[0].readback_depth == 14
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
