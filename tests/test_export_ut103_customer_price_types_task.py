from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from xml.etree import ElementTree as ET

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models import Base
from app.models.customer_price_type import (
    CustomerPriceTypeCase,
    CustomerPriceTypeCaseEvent,
    CustomerPriceTypeProfile,
    CustomerPriceTypeRun,
    CustomerPriceTypeSnapshot,
)

COUNTERPARTY_REF = "0x8fda0025901e48ee11ed222ea7d9b21e"
SECOND_COUNTERPARTY_REF = "0x9fda0025901e48ee11ed222ea7d9b21e"
SNAPSHOT_HASH = "a" * 64


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


def _seed_approved_case(
    database_path: Path,
    *,
    counterparty_ref: str = COUNTERPARTY_REF,
    snapshot_hash: str = SNAPSHOT_HASH,
    approved_snapshot_hash: str | None = None,
    approver_name: str = "Кештов Арсений Юрьевич",
) -> int:
    engine = create_engine(f"sqlite:///{database_path}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        run = CustomerPriceTypeRun(
            run_key=f"approved-export-run-{counterparty_ref}",
            snapshot_month=date(2026, 6, 1),
            ruleset_version="2026-06-v1",
            as_of=date(2026, 6, 30),
            window_start=date(2026, 4, 1),
            window_end=date(2026, 7, 1),
            source_statuses={"contracts": "ready"},
            source_fingerprint="f" * 64,
            input_count=1,
            calculated_count=1,
            actionable_count=1,
            status="completed",
        )
        profile = CustomerPriceTypeProfile(
            counterparty_ref=counterparty_ref,
            counterparty_code=f"РБ{counterparty_ref[-8:]}",
            counterparty_name=f"Тестовый клиент {counterparty_ref[-8:]}",
            is_service_card=False,
            is_hygiene=False,
            master_data_flags=[],
        )
        session.add_all((run, profile))
        session.flush()
        snapshot = CustomerPriceTypeSnapshot(
            run_id=run.id,
            profile_id=profile.id,
            counterparty_ref=counterparty_ref,
            snapshot_month=date(2026, 6, 1),
            ruleset_version=run.ruleset_version,
            current_price_type="2.Бронзовый",
            current_level="bronze",
            contract_candidates=[],
            monthly_sales={},
            total_3m=Decimal("0"),
            last_month=Decimal("0"),
            economics={"status": "ready"},
            payments={},
            returns={},
            history={"coverage_months": 12},
            source_status="ready",
            source_statuses={"contracts": "ready"},
            conflicts=[],
            stop_factors=["human_approval_required"],
            system_recommendation="downgrade_to_retail",
            recommended_price_type="Розница",
            recommendation_reason="Утверждённое понижение",
            action_required=True,
            case_type="recovery",
            review_type="dead_soul",
            reasons=["dead_soul"],
            snapshot_hash=snapshot_hash,
        )
        session.add(snapshot)
        session.flush()
        case = CustomerPriceTypeCase(
            case_key=f"{counterparty_ref}:2026-06",
            profile_id=profile.id,
            current_snapshot_id=snapshot.id,
            snapshot_month=date(2026, 6, 1),
            ruleset_version=run.ruleset_version,
            case_type="recovery",
            review_type="dead_soul",
            reasons=["dead_soul"],
            stage="READY_FOR_1C",
            manager_action_completeness={"completed": True},
            system_recommendation="downgrade_to_retail",
            recommended_price_type="Розница",
            human_final_decision="downgrade_to_retail",
            approval_status="approved",
            approver_ref="user-1",
            approver_name=approver_name,
            approved_at=datetime(2026, 8, 2, 5, 0, 0),
            approved_snapshot_hash=approved_snapshot_hash or snapshot_hash,
            onec_export_status="ready",
            onec_readback_status="not_requested",
            version=2,
        )
        session.add(case)
        session.flush()
        profile.latest_snapshot_id = snapshot.id
        profile.open_case_id = case.id
        session.commit()
        case_id = case.id
    engine.dispose()
    return case_id


def _task_env(database_path: Path) -> dict[str, str]:
    return {**os.environ, "DATABASE_URL": f"sqlite:///{database_path}"}


def _write_result(
    path: Path,
    *,
    message_id: str,
    item_result: str,
    current_price_type: str,
    readback_price_type: str,
    message: str = "Проверено",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""<?xml version="1.0" encoding="windows-1251"?>
<ExchangeResult>
  <MessageId>{message_id}</MessageId>
  <Schema>customer_price_type_updates.v1</Schema>
  <Status>success</Status>
  <ProcessedAt>2026-08-02T09:00:00</ProcessedAt>
  <Loaded>1</Loaded>
  <Failed>0</Failed>
  <Errors></Errors>
  <ItemResults>
    <ItemResult>
      <IdempotencyKey>customer-price-type:{COUNTERPARTY_REF}:2026-06:{SNAPSHOT_HASH}</IdempotencyKey>
      <CounterpartyRef>{COUNTERPARTY_REF.upper()}</CounterpartyRef>
      <CounterpartyGuid>a7d9b21e-222e-11ed-8fda-0025901e48ee</CounterpartyGuid>
      <CounterpartyName>Арсений Кештов</CounterpartyName>
      <Result>{item_result}</Result>
      <Message>{message}</Message>
      <ContractGuid>070bc34d-43b8-11f1-8266-002590803daf</ContractGuid>
      <ContractName>Основной договор1</ContractName>
      <CurrentPriceType>{current_price_type}</CurrentPriceType>
      <TargetPriceType>Розница</TargetPriceType>
      <ReadbackPriceType>{readback_price_type}</ReadbackPriceType>
      <FoundContracts>Основной договор1</FoundContracts>
    </ItemResult>
  </ItemResults>
</ExchangeResult>""",
        encoding="windows-1251",
    )


def test_export_customer_price_type_task_writes_only_approved_db_case(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "pricing.db"
    case_id = _seed_approved_case(database_path)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tasks.export_ut103_customer_price_types",
            "--exchange-root",
            str(tmp_path / "exchange"),
            "--message-id",
            "bronze-to-retail-task-test-001",
            "--from-approved-cases",
            "--snapshot-month",
            "2026-06",
            "--case-id",
            str(case_id),
            "--json",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=_task_env(database_path),
    )

    summary = json.loads(result.stdout)
    output_path = Path(summary["path"])
    assert summary["mode"] == "dry_run"
    assert summary["rows"] == 1
    assert summary["input_source"] == "approved_cases"
    assert summary["case_ids"] == [case_id]
    assert summary["approved_by"] == "Кештов Арсений Юрьевич"
    assert output_path.exists()
    root = ET.fromstring(output_path.read_bytes())
    assert root.findtext("Header/Schema") == "customer_price_type_updates.v1"
    assert root.findtext("Items/Item/CounterpartyGuid") == "a7d9b21e-222e-11ed-8fda-0025901e48ee"
    assert root.findtext("Header/ApprovedBy") == "Кештов Арсений Юрьевич"
    assert root.findtext("Items/Item/CounterpartyRef") == COUNTERPARTY_REF.upper()
    assert root.findtext("Items/Item/IdempotencyKey") == (
        f"customer-price-type:{COUNTERPARTY_REF}:2026-06:{SNAPSHOT_HASH}"
    )
    engine = create_engine(f"sqlite:///{database_path}")
    with Session(engine) as session:
        events = session.scalars(
            select(CustomerPriceTypeCaseEvent).order_by(CustomerPriceTypeCaseEvent.id)
        ).all()
        assert [event.event_type for event in events] == [
            "onec_export_requested",
            "onec_export_queued",
        ]
        assert {event.metadata_json["message_id"] for event in events} == {
            "bronze-to-retail-task-test-001"
        }
    engine.dispose()


def test_export_customer_price_type_apply_requires_persisted_dry_run_and_records_readback(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "pricing.db"
    case_id = _seed_approved_case(database_path)
    exchange_root = tmp_path / "exchange"
    dry_run_message_id = "bronze-to-retail-durable-dry-run"
    apply_message_id = "bronze-to-retail-durable-apply"
    base_command = [
        sys.executable,
        "-m",
        "tasks.export_ut103_customer_price_types",
        "--exchange-root",
        str(exchange_root),
        "--from-approved-cases",
        "--snapshot-month",
        "2026-06",
        "--case-id",
        str(case_id),
        "--json",
    ]
    subprocess.run(
        [*base_command, "--message-id", dry_run_message_id],
        check=True,
        capture_output=True,
        text=True,
        env=_task_env(database_path),
    )

    blocked_apply = subprocess.run(
        [
            *base_command,
            "--message-id",
            apply_message_id,
            "--mode",
            "apply",
            "--validated-dry-run-message-id",
            dry_run_message_id,
        ],
        check=False,
        capture_output=True,
        text=True,
        env=_task_env(database_path),
    )
    assert blocked_apply.returncode != 0
    assert "persisted successful dry_run" in blocked_apply.stderr
    assert not list((exchange_root / "to_1c" / "new").glob("*durable-apply*"))

    dry_run_result_path = tmp_path / "dry-run.result.xml"
    _write_result(
        dry_run_result_path,
        message_id=dry_run_message_id,
        item_result="validated",
        current_price_type="2.Бронзовый",
        readback_price_type="2.Бронзовый",
    )
    recorded_dry_run = subprocess.run(
        [
            sys.executable,
            "-m",
            "tasks.export_ut103_customer_price_types",
            "--record-result",
            str(dry_run_result_path),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=_task_env(database_path),
    )
    assert json.loads(recorded_dry_run.stdout) == {
        "case_ids": [case_id],
        "idempotent_replay": False,
        "message_id": dry_run_message_id,
        "mode": "dry_run",
        "result_path": str(dry_run_result_path),
        "succeeded": True,
    }
    replayed_dry_run = subprocess.run(
        [
            sys.executable,
            "-m",
            "tasks.export_ut103_customer_price_types",
            "--record-result",
            str(dry_run_result_path),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=_task_env(database_path),
    )
    assert json.loads(replayed_dry_run.stdout)["idempotent_replay"] is True

    _write_result(
        dry_run_result_path,
        message_id=dry_run_message_id,
        item_result="validated",
        current_price_type="2.Бронзовый",
        readback_price_type="2.Бронзовый",
        message="Изменённое содержимое результата с тем же MessageId",
    )
    conflicting_replay = subprocess.run(
        [
            sys.executable,
            "-m",
            "tasks.export_ut103_customer_price_types",
            "--record-result",
            str(dry_run_result_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=_task_env(database_path),
    )
    assert conflicting_replay.returncode != 0
    assert "different result content" in conflicting_replay.stderr

    applied = subprocess.run(
        [
            *base_command,
            "--message-id",
            apply_message_id,
            "--mode",
            "apply",
            "--validated-dry-run-message-id",
            dry_run_message_id,
        ],
        check=True,
        capture_output=True,
        text=True,
        env=_task_env(database_path),
    )
    apply_summary = json.loads(applied.stdout)
    apply_path = Path(apply_summary["path"])
    apply_root = ET.fromstring(apply_path.read_bytes())
    assert apply_root.findtext("Header/Mode") == "apply"
    assert apply_root.findtext("Header/MessageId") == apply_message_id
    assert apply_root.findtext("Items/Item/IdempotencyKey") == (
        f"customer-price-type:{COUNTERPARTY_REF}:2026-06:{SNAPSHOT_HASH}"
    )

    apply_result_path = tmp_path / "apply.result.xml"
    _write_result(
        apply_result_path,
        message_id=apply_message_id,
        item_result="applied",
        current_price_type="2.Бронзовый",
        readback_price_type="Розница",
        message="Тип цен изменён; readback подтверждён",
    )
    recorded_apply = subprocess.run(
        [
            sys.executable,
            "-m",
            "tasks.export_ut103_customer_price_types",
            "--record-result",
            str(apply_result_path),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=_task_env(database_path),
    )
    assert json.loads(recorded_apply.stdout)["succeeded"] is True

    engine = create_engine(f"sqlite:///{database_path}")
    with Session(engine) as session:
        case = session.get(CustomerPriceTypeCase, case_id)
        events = session.scalars(
            select(CustomerPriceTypeCaseEvent)
            .where(CustomerPriceTypeCaseEvent.case_id == case_id)
            .order_by(CustomerPriceTypeCaseEvent.id)
        ).all()
        assert case.stage == "CLOSED_CHANGED"
        assert case.onec_export_status == "exported"
        assert case.onec_readback_status == "confirmed"
        assert [event.event_type for event in events] == [
            "onec_export_requested",
            "onec_export_queued",
            "onec_dry_run_succeeded",
            "onec_export_requested",
            "onec_export_queued",
            "onec_apply_succeeded",
        ]
    engine.dispose()


def test_export_customer_price_type_task_fails_closed_for_stale_approval(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "pricing.db"
    case_id = _seed_approved_case(database_path, approved_snapshot_hash="b" * 64)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tasks.export_ut103_customer_price_types",
            "--exchange-root",
            str(tmp_path / "exchange"),
            "--message-id",
            "bronze-to-retail-task-test-stale",
            "--from-approved-cases",
            "--snapshot-month",
            "2026-06",
            "--case-id",
            str(case_id),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=_task_env(database_path),
    )

    assert result.returncode != 0
    assert "approved_snapshot_hash" in result.stderr
    assert not (tmp_path / "exchange" / "to_1c").exists()


def test_export_customer_price_type_task_rejects_mixed_approvers(tmp_path: Path) -> None:
    database_path = tmp_path / "pricing.db"
    first_case_id = _seed_approved_case(database_path)
    second_case_id = _seed_approved_case(
        database_path,
        counterparty_ref=SECOND_COUNTERPARTY_REF,
        snapshot_hash="b" * 64,
        approver_name="Другой руководитель",
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tasks.export_ut103_customer_price_types",
            "--exchange-root",
            str(tmp_path / "exchange"),
            "--message-id",
            "bronze-to-retail-task-test-mixed-approvers",
            "--from-approved-cases",
            "--snapshot-month",
            "2026-06",
            "--case-id",
            str(first_case_id),
            "--case-id",
            str(second_case_id),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=_task_env(database_path),
    )

    assert result.returncode != 0
    assert "same approved_by" in result.stderr
    assert not (tmp_path / "exchange" / "to_1c").exists()


def test_export_customer_price_type_task_rejects_csv_queue_write(tmp_path: Path) -> None:
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
            "bronze-to-retail-task-test-csv",
            "--input-csv",
            str(input_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "allowed only with --validate-only" in result.stderr
    assert not (tmp_path / "exchange" / "to_1c").exists()


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
    assert summary["input_source"] == "csv_validate_only"
    assert not (tmp_path / "to_1c").exists()
