from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import select

import app.services.receivable_credit_decisions as credit_decision_service
from app.core.config import Settings
from app.models.receivable_credit_decision import ReceivableCreditDecisionOperation
from app.services.receivable_credit_decisions import (
    parse_approved_decision,
    run_credit_decision_worker_once,
)

MAPPING = {
    "process": {"entity_type_id": 1200, "category_id": 44},
    "stage_map": {
        "approved": "DT1200_44:APPROVED",
        "onec_check": "DT1200_44:ONEC_CHECK",
        "applying": "DT1200_44:APPLYING",
        "applied": "DT1200_44:SUCCESS",
        "onec_error": "DT1200_44:ONEC_ERROR",
    },
    "fields": {
        "counterparty_ref": "counterpartyRef",
        "counterparty_guid": "counterpartyGuid",
        "counterparty_code": "counterpartyCode",
        "counterparty_name": "counterpartyName",
        "current_limit": "currentLimit",
        "current_depth": "currentDepth",
        "proposed_limit": "proposedLimit",
        "proposed_depth": "proposedDepth",
        "reason": "reason",
        "decision_revision": "decisionRevision",
        "decision_hash": "decisionHash",
        "approved_by": "approvedBy",
        "approved_at": "approvedAt",
        "connector_state": "connectorState",
        "connector_error": "connectorError",
        "readback_limit": "readbackLimit",
        "readback_depth": "readbackDepth",
    },
}
NOW = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)


def test_credit_decision_settings_parse_deployment_lists() -> None:
    settings = Settings(
        _env_file=None,
        receivable_credit_decision_approver_user_ids="115204,130757",
        receivable_credit_decision_pilot_counterparty_codes="РБ030337,РБ000001",
    )
    assert settings.receivable_credit_decision_approver_user_ids == ["115204", "130757"]
    assert settings.receivable_credit_decision_pilot_counterparty_codes == [
        "РБ030337",
        "РБ000001",
    ]
    assert not Settings(_env_file=None).receivable_credit_decision_auto_apply_enabled
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            receivable_credit_decision_max_readback_attempts=4,
        )


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "receivable_credit_decision_enabled": True,
        "receivable_credit_decision_entity_type_id": 1200,
        "receivable_credit_decision_category_id": 44,
        "receivable_credit_decision_approver_user_ids": ["115204"],
        "receivable_credit_decision_auto_apply_enabled": True,
        "receivable_credit_decision_pilot_counterparty_codes": ["РБ030337"],
        "receivable_credit_decision_result_timeout_seconds": 60,
        "receivable_credit_decision_max_dry_run_attempts": 2,
    }
    values.update(overrides)
    return Settings(**values)


def _item() -> dict[str, object]:
    return {
        "id": "2494",
        "categoryId": 44,
        "stageId": MAPPING["stage_map"]["approved"],
        "movedBy": "115204",
        "movedTime": NOW.isoformat(),
        "updatedTime": NOW.isoformat(),
        "counterpartyRef": "0X8FDA0025901E48EE11ED222EA7D9B21E",
        "counterpartyGuid": "a7d9b21e-222e-11ed-8fda-0025901e48ee",
        "counterpartyCode": "РБ030337",
        "counterpartyName": "Тестовый контрагент",
        "currentLimit": "100000.00",
        "currentDepth": "7",
        "proposedLimit": "150000.00",
        "proposedDepth": "14",
        "reason": "Утверждено финансовым директором",
        "decisionRevision": "7",
        "decisionHash": "",
    }


class FakeBitrix:
    def __init__(self) -> None:
        self.item = _item()
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.fail_applied_update = False
        self.fail_error_update = False

    def __call__(self, method: str, params: dict) -> dict:
        self.calls.append((method, params))
        if method == "crm.item.list":
            stage = params["filter"]["stageId"]
            items = [dict(self.item)] if self.item["stageId"] == stage else []
            return {"result": {"items": items}}
        if method == "crm.item.get":
            return {"result": {"item": dict(self.item)}}
        if method == "crm.item.update":
            fields = params["fields"]
            if (
                self.fail_applied_update
                and fields.get("stageId") == MAPPING["stage_map"]["applied"]
            ):
                raise RuntimeError("Bitrix unavailable")
            if (
                self.fail_error_update
                and fields.get("stageId") == MAPPING["stage_map"]["onec_error"]
            ):
                raise RuntimeError("Bitrix unavailable")
            self.item.update(fields)
            return {"result": {"item": dict(self.item)}}
        raise AssertionError(method)


def _write_result(
    root: Path,
    operation: ReceivableCreditDecisionOperation,
    *,
    mode: str,
    status: str,
    readback_limit: str = "150000.00",
    readback_depth: int = 14,
) -> Path:
    message_id = {
        "dry_run": operation.dry_run_message_id,
        "apply": operation.apply_message_id,
        "readback": operation.readback_message_id,
    }[mode]
    assert message_id
    result_dir = root / "from_1c" / "new"
    result_dir.mkdir(parents=True, exist_ok=True)
    path = result_dir / f"onec_commands_{message_id}.result.xml"
    readback = (
        ""
        if mode == "dry_run"
        else f"""
      <ReadbackLimit>{readback_limit}</ReadbackLimit>
      <ReadbackDepth>{readback_depth}</ReadbackDepth>"""
    )
    path.write_text(
        f"""<?xml version="1.0" encoding="windows-1251"?>
<ExchangeResult>
  <MessageId>{message_id}</MessageId>
  <Schema>onec_commands.v1</Schema>
  <Status>success</Status>
  <ProcessedAt>2026-07-28T15:00:00+03:00</ProcessedAt>
  <Loaded>1</Loaded><Failed>0</Failed><Errors></Errors>
  <CommandResults><CommandResult>
    <IdempotencyKey>receivable-decision:1200:2494:7</IdempotencyKey>
    <DecisionId>2494</DecisionId>
    <DecisionHash>{operation.decision_hash}</DecisionHash>
    <CounterpartyRef>{operation.counterparty_ref}</CounterpartyRef>
    <CounterpartyGuid>{operation.counterparty_guid}</CounterpartyGuid>
    <CounterpartyCode>РБ030337</CounterpartyCode>
    <Status>{status}</Status>
    <Message>ok</Message>{readback}
  </CommandResult></CommandResults>
</ExchangeResult>""",
        encoding="windows-1251",
    )
    return path


def test_worker_runs_dry_run_then_apply_and_recovers_bitrix_update(
    db_session, tmp_path: Path
) -> None:
    bitrix = FakeBitrix()
    settings = _settings()

    first = run_credit_decision_worker_once(
        db_session,
        exchange_root=tmp_path,
        settings=settings,
        mapping=MAPPING,
        bitrix_caller=bitrix,
        now=NOW,
    )
    operation = db_session.scalar(select(ReceivableCreditDecisionOperation))
    assert operation is not None
    assert first["created"] == 1
    assert operation.state == "dry_run_sent"
    assert operation.dry_run_attempts == 1

    _write_result(tmp_path, operation, mode="dry_run", status="validated")
    second = run_credit_decision_worker_once(
        db_session,
        exchange_root=tmp_path,
        settings=settings,
        mapping=MAPPING,
        bitrix_caller=bitrix,
        now=NOW + timedelta(seconds=10),
    )
    db_session.refresh(operation)
    assert second["errors"] == 0
    assert operation.state == "apply_sent"
    assert operation.apply_attempts == 1
    assert not (
        tmp_path / "from_1c" / "new" / f"onec_commands_{operation.dry_run_message_id}.result.xml"
    ).exists()
    assert (
        tmp_path
        / "from_1c"
        / "archive"
        / f"onec_commands_{operation.dry_run_message_id}.result.xml"
    ).exists()

    _write_result(tmp_path, operation, mode="apply", status="applied")
    bitrix.fail_applied_update = True
    third = run_credit_decision_worker_once(
        db_session,
        exchange_root=tmp_path,
        settings=settings,
        mapping=MAPPING,
        bitrix_caller=bitrix,
        now=NOW + timedelta(seconds=20),
    )
    db_session.refresh(operation)
    assert third["applied"] == 1
    assert operation.state == "applied"
    assert operation.active_counterparty_key is None
    assert operation.bitrix_sync_pending
    assert "1С применено" in (operation.last_error or "")
    assert (
        tmp_path / "from_1c" / "archive" / f"onec_commands_{operation.apply_message_id}.result.xml"
    ).exists()

    bitrix.fail_applied_update = False
    fourth = run_credit_decision_worker_once(
        db_session,
        exchange_root=tmp_path,
        settings=settings,
        mapping=MAPPING,
        bitrix_caller=bitrix,
        now=NOW + timedelta(seconds=30),
    )
    db_session.refresh(operation)
    assert fourth["errors"] == 0
    assert not operation.bitrix_sync_pending
    assert bitrix.item["stageId"] == MAPPING["stage_map"]["applied"]
    assert bitrix.item["readbackLimit"] == "150000.00"
    assert bitrix.item["readbackDepth"] == 14


def test_worker_stops_after_dry_run_when_auto_apply_is_disabled(db_session, tmp_path: Path) -> None:
    bitrix = FakeBitrix()
    settings = _settings(receivable_credit_decision_auto_apply_enabled=False)
    run_credit_decision_worker_once(
        db_session,
        exchange_root=tmp_path,
        settings=settings,
        mapping=MAPPING,
        bitrix_caller=bitrix,
        now=NOW,
    )
    operation = db_session.scalar(select(ReceivableCreditDecisionOperation))
    assert operation is not None
    _write_result(tmp_path, operation, mode="dry_run", status="validated")

    run_credit_decision_worker_once(
        db_session,
        exchange_root=tmp_path,
        settings=settings,
        mapping=MAPPING,
        bitrix_caller=bitrix,
        now=NOW + timedelta(seconds=10),
    )
    db_session.refresh(operation)
    assert operation.state == "dry_run_ok"
    assert operation.apply_message_id is None


def test_worker_cancels_when_card_changes_between_dry_run_and_apply(
    db_session, tmp_path: Path
) -> None:
    bitrix = FakeBitrix()
    settings = _settings()
    run_credit_decision_worker_once(
        db_session,
        exchange_root=tmp_path,
        settings=settings,
        mapping=MAPPING,
        bitrix_caller=bitrix,
        now=NOW,
    )
    operation = db_session.scalar(select(ReceivableCreditDecisionOperation))
    assert operation is not None
    _write_result(tmp_path, operation, mode="dry_run", status="validated")
    bitrix.item["proposedLimit"] = "160000.00"

    run_credit_decision_worker_once(
        db_session,
        exchange_root=tmp_path,
        settings=settings,
        mapping=MAPPING,
        bitrix_caller=bitrix,
        now=NOW + timedelta(seconds=10),
    )
    db_session.refresh(operation)
    assert operation.state == "cancelled"
    assert operation.apply_message_id is None
    assert operation.active_counterparty_key is None
    assert bitrix.item["stageId"] == MAPPING["stage_map"]["onec_error"]


def test_worker_rejects_approver_outside_allowlist(db_session, tmp_path: Path) -> None:
    bitrix = FakeBitrix()
    bitrix.item["movedBy"] = "999"
    result = run_credit_decision_worker_once(
        db_session,
        exchange_root=tmp_path,
        settings=_settings(),
        mapping=MAPPING,
        bitrix_caller=bitrix,
        now=NOW,
    )
    assert result["created"] == 0
    assert db_session.scalar(select(ReceivableCreditDecisionOperation)) is None
    assert bitrix.item["stageId"] == MAPPING["stage_map"]["onec_error"]
    assert "allowlist" in str(bitrix.item["connectorError"])


def test_worker_cancels_while_dry_run_result_is_pending(db_session, tmp_path: Path) -> None:
    bitrix = FakeBitrix()
    settings = _settings()
    run_credit_decision_worker_once(
        db_session,
        exchange_root=tmp_path,
        settings=settings,
        mapping=MAPPING,
        bitrix_caller=bitrix,
        now=NOW,
    )
    operation = db_session.scalar(select(ReceivableCreditDecisionOperation))
    assert operation is not None
    bitrix.item["stageId"] = "DT1200_44:FAIL"

    run_credit_decision_worker_once(
        db_session,
        exchange_root=tmp_path,
        settings=settings,
        mapping=MAPPING,
        bitrix_caller=bitrix,
        now=NOW + timedelta(seconds=10),
    )
    db_session.refresh(operation)
    assert operation.state == "cancelled"
    assert operation.apply_message_id is None
    assert operation.active_counterparty_key is None


def test_worker_never_resends_ambiguous_apply(db_session, tmp_path: Path) -> None:
    bitrix = FakeBitrix()
    settings = _settings()
    result = run_credit_decision_worker_once(
        db_session,
        exchange_root=tmp_path,
        settings=settings,
        mapping=MAPPING,
        bitrix_caller=bitrix,
        now=NOW,
    )
    operation = db_session.scalar(select(ReceivableCreditDecisionOperation))
    assert operation is not None
    _write_result(tmp_path, operation, mode="dry_run", status="validated")
    run_credit_decision_worker_once(
        db_session,
        exchange_root=tmp_path,
        settings=settings,
        mapping=MAPPING,
        bitrix_caller=bitrix,
        now=NOW + timedelta(seconds=10),
    )
    db_session.refresh(operation)
    ready = tmp_path / "to_1c" / "new" / f"onec_commands_{operation.apply_message_id}.ready.xml"
    assert ready.exists()
    ready.unlink()

    result = run_credit_decision_worker_once(
        db_session,
        exchange_root=tmp_path,
        settings=settings,
        mapping=MAPPING,
        bitrix_caller=bitrix,
        now=NOW + timedelta(seconds=80),
    )
    db_session.refresh(operation)
    assert operation.state == "applying"
    assert operation.apply_attempts == 1
    assert not ready.exists()
    assert "повторная отправка запрещена" in (operation.last_error or "")
    assert result["lost_result"] == 1
    assert result["manual_review_queue"] == 1
    assert result["recovery_readbacks"] == 1
    assert operation.readback_attempts == 1
    recovery_ready = (
        tmp_path / "to_1c" / "new" / f"onec_commands_{operation.readback_message_id}.ready.xml"
    )
    assert recovery_ready.exists()
    recovery_xml = recovery_ready.read_text(encoding="windows-1251")
    assert "<Mode>dry_run</Mode>" in recovery_xml
    assert "<ExpectedCurrentLimit>150000.00</ExpectedCurrentLimit>" in recovery_xml

    _write_result(tmp_path, operation, mode="readback", status="already_actual")
    recovered = run_credit_decision_worker_once(
        db_session,
        exchange_root=tmp_path,
        settings=settings,
        mapping=MAPPING,
        bitrix_caller=bitrix,
        now=NOW + timedelta(seconds=90),
    )
    db_session.refresh(operation)
    assert recovered["applied"] == 1
    assert operation.state == "applied"
    assert operation.apply_attempts == 1
    assert operation.readback_limit == 150000
    assert operation.readback_depth == 14


def test_worker_starts_recovery_timer_before_apply_publish(
    db_session, tmp_path: Path, monkeypatch
) -> None:
    bitrix = FakeBitrix()
    settings = _settings()
    run_credit_decision_worker_once(
        db_session,
        exchange_root=tmp_path,
        settings=settings,
        mapping=MAPPING,
        bitrix_caller=bitrix,
        now=NOW,
    )
    operation = db_session.scalar(select(ReceivableCreditDecisionOperation))
    assert operation is not None
    _write_result(tmp_path, operation, mode="dry_run", status="validated")

    original_write = credit_decision_service.write_credit_terms_message

    def fail_apply_publish(exchange_root, message, **kwargs):
        if message.mode == "apply":
            raise OSError("simulated uncertain apply publish")
        return original_write(exchange_root, message, **kwargs)

    monkeypatch.setattr(
        credit_decision_service,
        "write_credit_terms_message",
        fail_apply_publish,
    )
    failed_publish = run_credit_decision_worker_once(
        db_session,
        exchange_root=tmp_path,
        settings=settings,
        mapping=MAPPING,
        bitrix_caller=bitrix,
        now=NOW + timedelta(seconds=10),
    )
    db_session.refresh(operation)
    assert failed_publish["errors"] == 1
    assert operation.state == "applying"
    assert operation.apply_attempts == 1
    assert operation.apply_sent_at == (NOW + timedelta(seconds=10)).replace(tzinfo=None)

    recovered = run_credit_decision_worker_once(
        db_session,
        exchange_root=tmp_path,
        settings=settings,
        mapping=MAPPING,
        bitrix_caller=bitrix,
        now=NOW + timedelta(seconds=80),
    )
    db_session.refresh(operation)
    assert recovered["recovery_readbacks"] == 1
    assert operation.readback_message_id
    assert (
        tmp_path / "to_1c" / "new" / f"onec_commands_{operation.readback_message_id}.ready.xml"
    ).exists()


def test_worker_recovers_dry_run_publish_after_durable_state_commit(
    db_session, tmp_path: Path, monkeypatch
) -> None:
    bitrix = FakeBitrix()
    settings = _settings()
    original_write = credit_decision_service.write_credit_terms_message

    def fail_first_dry_run_publish(exchange_root, message, **kwargs):
        if message.mode == "dry_run":
            raise OSError("simulated uncertain dry_run publish")
        return original_write(exchange_root, message, **kwargs)

    monkeypatch.setattr(
        credit_decision_service,
        "write_credit_terms_message",
        fail_first_dry_run_publish,
    )
    failed_publish = run_credit_decision_worker_once(
        db_session,
        exchange_root=tmp_path,
        settings=settings,
        mapping=MAPPING,
        bitrix_caller=bitrix,
        now=NOW,
    )
    operation = db_session.scalar(select(ReceivableCreditDecisionOperation))
    assert operation is not None
    assert failed_publish["errors"] == 1
    assert operation.state == "dry_run_sent"
    assert operation.dry_run_attempts == 1
    assert operation.dry_run_message_id

    monkeypatch.setattr(
        credit_decision_service,
        "write_credit_terms_message",
        original_write,
    )
    recovered = run_credit_decision_worker_once(
        db_session,
        exchange_root=tmp_path,
        settings=settings,
        mapping=MAPPING,
        bitrix_caller=bitrix,
        now=NOW + timedelta(seconds=80),
    )
    db_session.refresh(operation)
    assert recovered["errors"] == 0
    assert operation.state == "dry_run_sent"
    assert operation.dry_run_attempts == 2
    assert (
        tmp_path / "to_1c" / "new" / f"onec_commands_{operation.dry_run_message_id}.ready.xml"
    ).exists()


def test_worker_rejects_result_without_decision_identity(db_session, tmp_path: Path) -> None:
    bitrix = FakeBitrix()
    settings = _settings()
    run_credit_decision_worker_once(
        db_session,
        exchange_root=tmp_path,
        settings=settings,
        mapping=MAPPING,
        bitrix_caller=bitrix,
        now=NOW,
    )
    operation = db_session.scalar(select(ReceivableCreditDecisionOperation))
    assert operation is not None
    result_path = _write_result(
        tmp_path,
        operation,
        mode="dry_run",
        status="validated",
    )
    payload = result_path.read_text(encoding="windows-1251")
    result_path.write_text(
        payload.replace(
            f"<DecisionHash>{operation.decision_hash}</DecisionHash>",
            "<DecisionHash></DecisionHash>",
        ),
        encoding="windows-1251",
    )

    result = run_credit_decision_worker_once(
        db_session,
        exchange_root=tmp_path,
        settings=settings,
        mapping=MAPPING,
        bitrix_caller=bitrix,
        now=NOW + timedelta(seconds=10),
    )
    db_session.refresh(operation)
    assert result["errors"] == 1
    assert operation.state == "dry_run_sent"


def test_message_id_is_bounded_hashed_and_deterministic() -> None:
    operation = ReceivableCreditDecisionOperation(
        bitrix_entity_type_id=2147483647,
        bitrix_item_id="9" * 64,
        bitrix_revision="ревизия-" + ("я" * 88),
        decision_hash="a" * 64,
    )
    expected_revision_hash = hashlib.sha256(operation.bitrix_revision.encode("utf-8")).hexdigest()[
        :12
    ]

    message_id = credit_decision_service._message_id(operation, "readback")

    assert message_id == (
        f"rcd-2147483647-{'9' * 64}-{expected_revision_hash}-" f"{'a' * 12}-readback"
    )
    assert len(message_id) <= 120
    assert message_id.isascii()


def test_new_revision_does_not_cancel_in_flight_apply(db_session, tmp_path: Path) -> None:
    bitrix = FakeBitrix()
    settings = _settings()
    run_credit_decision_worker_once(
        db_session,
        exchange_root=tmp_path,
        settings=settings,
        mapping=MAPPING,
        bitrix_caller=bitrix,
        now=NOW,
    )
    operation = db_session.scalar(select(ReceivableCreditDecisionOperation))
    assert operation is not None
    _write_result(tmp_path, operation, mode="dry_run", status="validated")
    run_credit_decision_worker_once(
        db_session,
        exchange_root=tmp_path,
        settings=settings,
        mapping=MAPPING,
        bitrix_caller=bitrix,
        now=NOW + timedelta(seconds=10),
    )
    db_session.refresh(operation)
    assert operation.state == "apply_sent"

    bitrix.item["stageId"] = MAPPING["stage_map"]["approved"]
    bitrix.item["decisionRevision"] = "8"
    bitrix.item["decisionHash"] = ""
    bitrix.item["movedTime"] = (NOW + timedelta(seconds=20)).isoformat()
    run_credit_decision_worker_once(
        db_session,
        exchange_root=tmp_path,
        settings=settings,
        mapping=MAPPING,
        bitrix_caller=bitrix,
        now=NOW + timedelta(seconds=20),
    )
    db_session.refresh(operation)

    assert operation.state == "apply_sent"
    assert operation.apply_attempts == 1
    assert operation.active_counterparty_key == operation.counterparty_key
    assert "новая ревизия заблокирована" in (operation.last_error or "")
    assert db_session.scalars(select(ReceivableCreditDecisionOperation)).all() == [operation]


def test_card_changed_during_apply_is_not_marked_applied_in_bitrix(
    db_session, tmp_path: Path
) -> None:
    bitrix = FakeBitrix()
    settings = _settings()
    run_credit_decision_worker_once(
        db_session,
        exchange_root=tmp_path,
        settings=settings,
        mapping=MAPPING,
        bitrix_caller=bitrix,
        now=NOW,
    )
    operation = db_session.scalar(select(ReceivableCreditDecisionOperation))
    assert operation is not None
    _write_result(tmp_path, operation, mode="dry_run", status="validated")
    run_credit_decision_worker_once(
        db_session,
        exchange_root=tmp_path,
        settings=settings,
        mapping=MAPPING,
        bitrix_caller=bitrix,
        now=NOW + timedelta(seconds=10),
    )
    db_session.refresh(operation)
    assert operation.state == "apply_sent"

    bitrix.item["proposedLimit"] = "160000.00"
    bitrix.item["decisionRevision"] = "8"
    bitrix.item["decisionHash"] = ""
    _write_result(tmp_path, operation, mode="apply", status="applied")
    metrics = run_credit_decision_worker_once(
        db_session,
        exchange_root=tmp_path,
        settings=settings,
        mapping=MAPPING,
        bitrix_caller=bitrix,
        now=NOW + timedelta(seconds=20),
    )
    db_session.refresh(operation)

    assert operation.state == "applied"
    assert operation.readback_limit == 150000
    assert not operation.bitrix_sync_pending
    assert "карточка Bitrix изменилась" in (operation.last_error or "")
    assert bitrix.item["stageId"] == MAPPING["stage_map"]["onec_error"]
    assert bitrix.item["connectorState"] == "applied_card_changed"
    assert metrics["manual_review_queue"] == 1


def test_apply_readback_mismatch_keeps_counterparty_lock_for_recovery(
    db_session, tmp_path: Path
) -> None:
    bitrix = FakeBitrix()
    settings = _settings()
    run_credit_decision_worker_once(
        db_session,
        exchange_root=tmp_path,
        settings=settings,
        mapping=MAPPING,
        bitrix_caller=bitrix,
        now=NOW,
    )
    operation = db_session.scalar(select(ReceivableCreditDecisionOperation))
    assert operation is not None
    _write_result(tmp_path, operation, mode="dry_run", status="validated")
    run_credit_decision_worker_once(
        db_session,
        exchange_root=tmp_path,
        settings=settings,
        mapping=MAPPING,
        bitrix_caller=bitrix,
        now=NOW + timedelta(seconds=10),
    )
    db_session.refresh(operation)
    assert operation.state == "apply_sent"

    _write_result(
        tmp_path,
        operation,
        mode="apply",
        status="applied",
        readback_limit="160000.00",
    )
    mismatch = run_credit_decision_worker_once(
        db_session,
        exchange_root=tmp_path,
        settings=settings,
        mapping=MAPPING,
        bitrix_caller=bitrix,
        now=NOW + timedelta(seconds=20),
    )
    db_session.refresh(operation)

    assert operation.state == "applying"
    assert operation.active_counterparty_key == operation.counterparty_key
    assert operation.apply_attempts == 1
    assert "блокировка контрагента сохранена" in (operation.last_error or "")
    assert mismatch["readback_mismatch"] == 1
    assert bitrix.item["connectorState"] == "applying"
    assert (
        tmp_path / "from_1c" / "archive" / f"onec_commands_{operation.apply_message_id}.result.xml"
    ).exists()

    run_credit_decision_worker_once(
        db_session,
        exchange_root=tmp_path,
        settings=settings,
        mapping=MAPPING,
        bitrix_caller=bitrix,
        now=NOW + timedelta(seconds=80),
    )
    db_session.refresh(operation)
    assert operation.readback_attempts == 1
    assert operation.apply_attempts == 1
    assert (
        tmp_path / "to_1c" / "new" / f"onec_commands_{operation.readback_message_id}.ready.xml"
    ).exists()


def test_failed_bitrix_update_is_retried(db_session, tmp_path: Path) -> None:
    bitrix = FakeBitrix()
    settings = _settings()
    run_credit_decision_worker_once(
        db_session,
        exchange_root=tmp_path,
        settings=settings,
        mapping=MAPPING,
        bitrix_caller=bitrix,
        now=NOW,
    )
    operation = db_session.scalar(select(ReceivableCreditDecisionOperation))
    assert operation is not None
    _write_result(tmp_path, operation, mode="dry_run", status="needs_review")
    bitrix.fail_error_update = True
    run_credit_decision_worker_once(
        db_session,
        exchange_root=tmp_path,
        settings=settings,
        mapping=MAPPING,
        bitrix_caller=bitrix,
        now=NOW + timedelta(seconds=10),
    )
    db_session.refresh(operation)
    assert operation.state == "failed"
    assert operation.bitrix_sync_pending

    bitrix.fail_error_update = False
    run_credit_decision_worker_once(
        db_session,
        exchange_root=tmp_path,
        settings=settings,
        mapping=MAPPING,
        bitrix_caller=bitrix,
        now=NOW + timedelta(seconds=20),
    )
    db_session.refresh(operation)
    assert not operation.bitrix_sync_pending
    assert bitrix.item["stageId"] == MAPPING["stage_map"]["onec_error"]
    assert bitrix.item["connectorState"] == "failed"


def test_lost_readback_retries_at_most_three_times_without_resending_apply(
    db_session, tmp_path: Path
) -> None:
    bitrix = FakeBitrix()
    settings = _settings(receivable_credit_decision_max_readback_attempts=3)
    run_credit_decision_worker_once(
        db_session,
        exchange_root=tmp_path,
        settings=settings,
        mapping=MAPPING,
        bitrix_caller=bitrix,
        now=NOW,
    )
    operation = db_session.scalar(select(ReceivableCreditDecisionOperation))
    assert operation is not None
    _write_result(tmp_path, operation, mode="dry_run", status="validated")
    run_credit_decision_worker_once(
        db_session,
        exchange_root=tmp_path,
        settings=settings,
        mapping=MAPPING,
        bitrix_caller=bitrix,
        now=NOW + timedelta(seconds=10),
    )
    db_session.refresh(operation)
    apply_ready = (
        tmp_path / "to_1c" / "new" / f"onec_commands_{operation.apply_message_id}.ready.xml"
    )
    apply_ready.unlink()

    last_metrics = {}
    for seconds in (80, 150, 220, 290):
        last_metrics = run_credit_decision_worker_once(
            db_session,
            exchange_root=tmp_path,
            settings=settings,
            mapping=MAPPING,
            bitrix_caller=bitrix,
            now=NOW + timedelta(seconds=seconds),
        )
    db_session.refresh(operation)

    assert operation.state == "applying"
    assert operation.apply_attempts == 1
    assert operation.readback_attempts == 3
    assert "допустимого числа повторов" in (operation.last_error or "")
    assert last_metrics["total_retries"] == 2
    assert not apply_ready.exists()


def test_pilot_allowlist_blocks_ingestion_for_other_counterparty(
    db_session, tmp_path: Path
) -> None:
    bitrix = FakeBitrix()
    bitrix.item["counterpartyCode"] = "РБ999999"
    result = run_credit_decision_worker_once(
        db_session,
        exchange_root=tmp_path,
        settings=_settings(),
        mapping=MAPPING,
        bitrix_caller=bitrix,
        now=NOW,
    )
    assert result["created"] == 0
    assert db_session.scalar(select(ReceivableCreditDecisionOperation)) is None
    assert bitrix.item["stageId"] == MAPPING["stage_map"]["approved"]


def test_approval_time_requires_explicit_timezone() -> None:
    item = _item()
    item["movedTime"] = "2026-07-28T12:00:00"

    with pytest.raises(ValueError, match="timezone"):
        parse_approved_decision(item, mapping=MAPPING)
