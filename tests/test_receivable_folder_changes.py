from __future__ import annotations

from pathlib import Path

from app.services.receivable_folder_changes import (
    approve_folder_change_operation,
    create_folder_change_operation,
    publish_folder_change_dry_run,
    sync_folder_change_results,
)


def _signal() -> dict[str, object]:
    return {
        "signal_key": "a" * 64,
        "queue": "actionable",
        "action_required": True,
        "counterparty_ref": "0X8FDA0025901E48EE11ED222EA7D9B21E",
        "counterparty_code": "РБ000001",
        "counterparty_name": "Клиент",
        "current_folder_ref": "0X8FDA0025901E48EE11ED222EA7D9B231",
        "current_folder_name": "Старая",
        "recommended_folder_ref": "0X8FDA0025901E48EE11ED222EA7D9B232",
        "recommended_folder_name": "Новая",
        "review_reason": "origin_document_structure_confirmed_manual_review",
    }


def _write_result(
    root: Path,
    operation,
    *,
    mode: str,
    status: str,
    readback_folder_ref: str = "0X8FDA0025901E48EE11ED222EA7D9B232",
) -> None:
    message_id = operation.dry_run_message_id if mode == "dry_run" else operation.apply_message_id
    result_dir = root / "from_1c" / "new"
    result_dir.mkdir(parents=True, exist_ok=True)
    (result_dir / f"onec_commands_{message_id}.result.xml").write_text(
        f"""<?xml version="1.0" encoding="windows-1251"?>
<ExchangeResult>
  <MessageId>{message_id}</MessageId><Schema>onec_commands.v1</Schema>
  <CommandResults><CommandResult>
    <Status>{status}</Status><Message>ok</Message>
    <DecisionHash>{operation.decision_hash or ''}</DecisionHash>
    <CounterpartyRef>{operation.counterparty_ref}</CounterpartyRef>
    <ExpectedOldFolderRef>{operation.expected_old_folder_ref}</ExpectedOldFolderRef>
    <ProposedNewFolderRef>{operation.proposed_new_folder_ref}</ProposedNewFolderRef>
    <ReadbackFolderRef>{readback_folder_ref}</ReadbackFolderRef>
    <ReadbackFolderName>Новая</ReadbackFolderName>
  </CommandResult></CommandResults>
</ExchangeResult>""",
        encoding="windows-1251",
    )


def test_folder_change_requires_dry_run_then_explicit_approval(db_session, tmp_path: Path) -> None:
    operation = create_folder_change_operation(
        db_session, signal=_signal(), data_version="report-1"
    )
    assert operation.state == "draft"
    assert operation.decision_hash is None

    publish_folder_change_dry_run(db_session, operation, exchange_root=tmp_path)
    assert operation.state == "dry_run_sent"
    dry_run_xml = next((tmp_path / "to_1c" / "new").glob("*.ready.xml"))
    assert "<Mode>dry_run</Mode>" in dry_run_xml.read_text(encoding="windows-1251")
    assert "<ApprovedBy />" in dry_run_xml.read_text(encoding="windows-1251")

    _write_result(tmp_path, operation, mode="dry_run", status="validated")
    summary = sync_folder_change_results(db_session, exchange_root=tmp_path)
    assert summary["updated"] == 1, (summary, operation.last_error)
    assert operation.state == "dry_run_ok"

    approve_folder_change_operation(
        db_session,
        operation,
        approved_by_bitrix_user_id="115204",
        exchange_root=tmp_path,
    )
    assert operation.state == "apply_sent"
    assert len(operation.decision_hash or "") == 64
    apply_xml = next((tmp_path / "to_1c" / "new").glob("*apply.ready.xml"))
    assert "<Mode>apply</Mode>" in apply_xml.read_text(encoding="windows-1251")
    assert "<ApprovedBy>115204</ApprovedBy>" in apply_xml.read_text(encoding="windows-1251")

    _write_result(tmp_path, operation, mode="apply", status="applied")
    sync_folder_change_results(db_session, exchange_root=tmp_path)
    assert operation.state == "applied"
    assert operation.active_counterparty_key is None
    assert operation.readback_folder_ref == "0X8FDA0025901E48EE11ED222EA7D9B232"


def test_folder_change_apply_readback_drift_needs_review(db_session, tmp_path: Path) -> None:
    operation = create_folder_change_operation(
        db_session, signal=_signal(), data_version="report-1"
    )
    publish_folder_change_dry_run(db_session, operation, exchange_root=tmp_path)
    _write_result(tmp_path, operation, mode="dry_run", status="validated")
    sync_folder_change_results(db_session, exchange_root=tmp_path)
    approve_folder_change_operation(
        db_session,
        operation,
        approved_by_bitrix_user_id="115204",
        exchange_root=tmp_path,
    )
    _write_result(
        tmp_path,
        operation,
        mode="apply",
        status="applied",
        readback_folder_ref="0X8FDA0025901E48EE11ED222EA7D9B233",
    )

    summary = sync_folder_change_results(db_session, exchange_root=tmp_path)

    assert summary["failed"] == 1
    assert operation.state == "needs_review"
    assert operation.active_counterparty_key == "0x8fda0025901e48ee11ed222ea7d9b21e"
