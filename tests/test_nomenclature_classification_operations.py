from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.nomenclature_classification_operation import (
    NomenclatureClassificationOperation,
    NomenclatureClassificationOperationEvent,
    NomenclatureClassificationOperationItem,
)
from app.services.exporters.ut103_nomenclature_classifications import (
    NomenclatureClassificationIntentRow,
    OneCClassificationReference,
    rows_from_nomenclature_classification_payload,
)
from app.services.nomenclature_classification_operations import (
    cancel_nomenclature_classification_operation,
    get_nomenclature_classification_status,
    register_nomenclature_classification_operation,
    request_nomenclature_classification_apply,
    run_nomenclature_classification_cycle,
)

KIND_OLD = "11111111-1111-1111-1111-111111111111"
KIND_NEW = "22222222-2222-2222-2222-222222222222"
GROUP_OLD = "33333333-3333-3333-3333-333333333333"
GROUP_NEW = "44444444-4444-4444-4444-444444444444"
CATEGORY_OLD = "55555555-5555-5555-5555-555555555555"
CATEGORY_NEW = "66666666-6666-6666-6666-666666666666"
NOMENCLATURE = "77777777-7777-7777-7777-777777777777"
UNRELATED_CATEGORY = "88888888-8888-8888-8888-888888888888"


def _settings(**overrides):
    values = {
        "nomenclature_classification_transport_enabled": True,
        "nomenclature_classification_worker_enabled": True,
        "nomenclature_classification_auto_apply_enabled": False,
        "nomenclature_classification_approved_by_allowlist": ["115204"],
        "nomenclature_classification_pilot_nomenclature_codes": ["РБ000001"],
        "nomenclature_classification_poll_limit": 20,
        "nomenclature_classification_result_timeout_seconds": 60,
        "nomenclature_classification_max_dry_run_attempts": 3,
        "nomenclature_classification_max_readback_attempts": 3,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _intent(
    *, key: str = "nom-class:РБ000001:decision-42:r1"
) -> NomenclatureClassificationIntentRow:
    return NomenclatureClassificationIntentRow(
        idempotency_key=key,
        nomenclature_code="РБ000001",
        nomenclature_guid=NOMENCLATURE,
        expected_kind=OneCClassificationReference(KIND_OLD, "OLD-KIND"),
        target_kind=OneCClassificationReference(KIND_NEW, "NEW-KIND"),
        expected_group=OneCClassificationReference(GROUP_OLD, "OLD-GROUP"),
        target_group=OneCClassificationReference(GROUP_NEW, "NEW-GROUP"),
        expected_category=OneCClassificationReference(CATEGORY_OLD, "OLD-CATEGORY"),
        target_category=OneCClassificationReference(CATEGORY_NEW, "NEW-CATEGORY"),
        group_mode="set",
        category_mode="replace_expected",
        reason="Утверждённое наведение порядка",
    )


def _write_result(
    exchange_root: Path,
    operation: NomenclatureClassificationOperation,
    *,
    mode: str,
    item_result: str,
    status: str = "success",
) -> bytes:
    row = rows_from_nomenclature_classification_payload(operation.canonical_payload)[0]
    message_id = {
        "dry_run": operation.dry_run_message_id,
        "apply": operation.apply_message_id,
        "readback": operation.readback_message_id,
    }[mode]
    initial_categories = [UNRELATED_CATEGORY]
    if row.expected_category.guid:
        initial_categories.insert(0, row.expected_category.guid)
    projected_categories = list(initial_categories)
    if row.category_mode in {"replace_expected", "remove_expected"}:
        projected_categories = [
            guid for guid in projected_categories if guid != row.expected_category.guid
        ]
    if (
        row.category_mode != "remove_expected"
        and row.target_category.guid not in projected_categories
    ):
        projected_categories.insert(0, row.target_category.guid)
    projected = ";".join(projected_categories)
    before_applied = mode == "readback"
    old_kind = row.target_kind.guid if before_applied else row.expected_kind.guid
    old_group = row.target_group.guid if before_applied else row.expected_group.guid
    old_categories = projected if before_applied else ";".join(initial_categories)
    readback_kind = row.expected_kind.guid if mode == "dry_run" else row.target_kind.guid
    readback_group = row.expected_group.guid if mode == "dry_run" else row.target_group.guid
    readback_categories = old_categories if mode == "dry_run" else projected
    failed = 0 if status == "success" else 1
    loaded = 1 - failed
    payload = f"""<?xml version="1.0" encoding="windows-1251"?>
<ExchangeResult>
  <OperationId>{operation.operation_id}</OperationId><MessageId>{message_id}</MessageId>
  <Schema>nomenclature_classification_updates.v3</Schema><Mode>{mode}</Mode>
  <CommandHash>{operation.command_hash}</CommandHash><Status>{status}</Status>
  <ProcessedAt>2026-08-04T12:00:00</ProcessedAt><Loaded>{loaded}</Loaded><Failed>{failed}</Failed><Errors></Errors>
  <ItemResults><ItemResult>
    <IdempotencyKey>{row.idempotency_key}</IdempotencyKey><DecisionHash>{row.decision_hash}</DecisionHash>
    <NomenclatureCode>{row.nomenclature_code}</NomenclatureCode><NomenclatureGuid>{row.nomenclature_guid}</NomenclatureGuid>
    <ExpectedKindGuid>{row.expected_kind.guid}</ExpectedKindGuid><ExpectedKindCode>{row.expected_kind.code}</ExpectedKindCode>
    <TargetKindGuid>{row.target_kind.guid}</TargetKindGuid><TargetKindCode>{row.target_kind.code}</TargetKindCode>
    <ExpectedGroupGuid>{row.expected_group.guid}</ExpectedGroupGuid><ExpectedGroupCode>{row.expected_group.code}</ExpectedGroupCode>
    <TargetGroupGuid>{row.target_group.guid}</TargetGroupGuid><TargetGroupCode>{row.target_group.code}</TargetGroupCode>
    <GroupMode>{row.group_mode}</GroupMode><CategoryMode>{row.category_mode}</CategoryMode>
    <ExpectedCategoryGuid>{row.expected_category.guid}</ExpectedCategoryGuid><ExpectedCategoryCode>{row.expected_category.code}</ExpectedCategoryCode>
    <TargetCategoryGuid>{row.target_category.guid}</TargetCategoryGuid><TargetCategoryCode>{row.target_category.code}</TargetCategoryCode>
    <Result>{item_result}</Result><Message>OK</Message>
    <OldKindGuid>{old_kind}</OldKindGuid><ReadbackKindGuid>{readback_kind}</ReadbackKindGuid>
    <OldGroupGuid>{old_group}</OldGroupGuid><ReadbackGroupGuid>{readback_group}</ReadbackGroupGuid>
    <OldCategoryGuids>{old_categories}</OldCategoryGuids>
    <ProjectedCategoryGuids>{projected}</ProjectedCategoryGuids>
    <ReadbackCategoryGuids>{readback_categories}</ReadbackCategoryGuids>
  </ItemResult></ItemResults>
</ExchangeResult>""".encode("windows-1251")
    result_dir = exchange_root / "from_1c" / "new"
    result_dir.mkdir(parents=True, exist_ok=True)
    (result_dir / f"nomenclature_classifications_{message_id}.result.xml").write_bytes(payload)
    return payload


def test_transport_gates_and_persisted_idempotency(db_session: Session) -> None:
    disabled = _settings(nomenclature_classification_transport_enabled=False)
    with pytest.raises(RuntimeError, match="disabled"):
        register_nomenclature_classification_operation(
            db_session,
            (_intent(),),
            approved_by="115204",
            requested_by="operator",
            settings=disabled,
        )
    with pytest.raises(ValueError, match="allowlist"):
        register_nomenclature_classification_operation(
            db_session,
            (_intent(),),
            approved_by="not-allowed",
            requested_by="operator",
            settings=_settings(),
        )

    operation = register_nomenclature_classification_operation(
        db_session,
        (_intent(),),
        approved_by="115204",
        requested_by="operator",
        settings=_settings(),
    )
    repeated = register_nomenclature_classification_operation(
        db_session,
        (_intent(),),
        approved_by="115204",
        requested_by="operator",
        settings=_settings(),
    )

    assert repeated.id == operation.id
    assert operation.state == "pending_dry_run"
    assert operation.items[0].decision_hash != operation.command_hash
    assert db_session.scalar(
        select(NomenclatureClassificationOperationEvent).where(
            NomenclatureClassificationOperationEvent.event_type == "registered"
        )
    )

    with pytest.raises(ValueError, match="active classification operation"):
        register_nomenclature_classification_operation(
            db_session,
            (_intent(key="another-key"),),
            approved_by="115204",
            requested_by="operator",
            settings=_settings(),
        )


def test_database_constraint_blocks_concurrent_product_registration(
    db_session: Session,
) -> None:
    existing = register_nomenclature_classification_operation(
        db_session,
        (_intent(),),
        approved_by="115204",
        requested_by="operator",
        settings=_settings(),
    )
    raced = NomenclatureClassificationOperation(
        operation_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        command_hash="a" * 64,
        state="pending_dry_run",
        approved_by="115204",
        requested_by="concurrent-operator",
        source="pricing-service",
        target="1c_ut_10_3",
        canonical_payload={"simulated": "race after preflight"},
    )
    raced.items.append(
        NomenclatureClassificationOperationItem(
            idempotency_key="nom-class:РБ000001:concurrent:r1",
            decision_hash="b" * 64,
            nomenclature_code="РБ000001",
            nomenclature_guid=NOMENCLATURE,
            active_nomenclature_key=existing.items[0].active_nomenclature_key,
            canonical_payload={"simulated": "race after preflight"},
        )
    )
    db_session.add(raced)

    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()

    persisted = list(db_session.scalars(select(NomenclatureClassificationOperation)))
    assert [operation.operation_id for operation in persisted] == [existing.operation_id]


def test_request_apply_requires_exact_dry_run_and_allowlists(db_session: Session) -> None:
    operation = register_nomenclature_classification_operation(
        db_session,
        (_intent(),),
        approved_by="115204",
        requested_by="operator",
        settings=_settings(),
    )
    with pytest.raises(ValueError, match="only after"):
        request_nomenclature_classification_apply(
            db_session,
            operation.operation_id,
            requested_by="operator",
            settings=_settings(),
        )

    operation.state = "dry_run_ok"
    db_session.commit()
    with pytest.raises(ValueError, match="pilot allowlist"):
        request_nomenclature_classification_apply(
            db_session,
            operation.operation_id,
            requested_by="operator",
            settings=_settings(nomenclature_classification_pilot_nomenclature_codes=[]),
        )

    operation.canonical_payload = {**operation.canonical_payload, "source": "tampered"}
    db_session.commit()
    with pytest.raises(ValueError, match="CommandHash"):
        request_nomenclature_classification_apply(
            db_session,
            operation.operation_id,
            requested_by="operator",
            settings=_settings(),
        )


def test_worker_gate_cancel_and_safe_dry_run_retries(
    db_session: Session,
    tmp_path: Path,
) -> None:
    started = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    with pytest.raises(RuntimeError, match="worker is disabled"):
        run_nomenclature_classification_cycle(
            db_session,
            exchange_root=tmp_path,
            settings=_settings(nomenclature_classification_worker_enabled=False),
            now=started,
        )

    operation = register_nomenclature_classification_operation(
        db_session,
        (_intent(),),
        approved_by="115204",
        requested_by="operator",
        settings=_settings(),
    )
    run_nomenclature_classification_cycle(
        db_session, exchange_root=tmp_path, settings=_settings(), now=started
    )
    original_message_id = operation.dry_run_message_id
    run_nomenclature_classification_cycle(
        db_session,
        exchange_root=tmp_path,
        settings=_settings(nomenclature_classification_max_dry_run_attempts=2),
        now=started + timedelta(seconds=61),
    )
    assert operation.dry_run_message_id == original_message_id
    assert operation.dry_run_attempts == 2
    request_events = list(
        db_session.scalars(
            select(NomenclatureClassificationOperationEvent).where(
                NomenclatureClassificationOperationEvent.event_type == "request_persisted"
            )
        )
    )
    assert [event.payload["attempt"] for event in request_events] == [1, 2]

    run_nomenclature_classification_cycle(
        db_session,
        exchange_root=tmp_path,
        settings=_settings(nomenclature_classification_max_dry_run_attempts=2),
        now=started + timedelta(seconds=122),
    )
    assert operation.state == "failed"
    assert operation.failure_kind == "dry_run_timeout"
    assert operation.items[0].active_nomenclature_key is None

    cancelled = cancel_nomenclature_classification_operation(
        db_session,
        operation.operation_id,
        requested_by="operator",
    )
    assert cancelled.state == "cancelled"


def test_exact_apply_state_machine_and_append_only_events(
    db_session: Session,
    tmp_path: Path,
) -> None:
    started = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    operation = register_nomenclature_classification_operation(
        db_session,
        (_intent(),),
        approved_by="115204",
        requested_by="operator",
        settings=_settings(),
    )
    run_nomenclature_classification_cycle(
        db_session, exchange_root=tmp_path, settings=_settings(), now=started
    )
    assert operation.state == "dry_run_sent"
    _write_result(tmp_path, operation, mode="dry_run", item_result="validated")
    run_nomenclature_classification_cycle(
        db_session, exchange_root=tmp_path, settings=_settings(), now=started
    )
    assert operation.state == "dry_run_ok"
    request_nomenclature_classification_apply(
        db_session,
        operation.operation_id,
        requested_by="operator",
        settings=_settings(),
    )
    run_nomenclature_classification_cycle(
        db_session, exchange_root=tmp_path, settings=_settings(), now=started
    )
    assert operation.state == "apply_sent"
    _write_result(tmp_path, operation, mode="apply", item_result="applied")
    run_nomenclature_classification_cycle(
        db_session,
        exchange_root=tmp_path,
        settings=_settings(),
        now=started + timedelta(seconds=1),
    )
    assert operation.state == "applied"
    assert operation.apply_attempts == 1
    assert operation.items[0].active_nomenclature_key is None
    events = list(
        db_session.scalars(
            select(NomenclatureClassificationOperationEvent)
            .where(NomenclatureClassificationOperationEvent.operation_pk == operation.id)
            .order_by(NomenclatureClassificationOperationEvent.id)
        )
    )
    assert any(
        event.event_type == "state_transition"
        and event.payload == {"state_from": "dry_run_sent", "state_to": "dry_run_ok"}
        for event in events
    )
    assert any(
        event.event_type == "state_transition"
        and event.payload == {"state_from": "apply_sent", "state_to": "applying"}
        for event in events
    )
    assert events[-1].event_type == "applied"


def test_conflicting_or_partial_dry_run_result_fails_with_product_lock(
    db_session: Session,
    tmp_path: Path,
) -> None:
    started = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    operation = register_nomenclature_classification_operation(
        db_session,
        (_intent(),),
        approved_by="115204",
        requested_by="operator",
        settings=_settings(),
    )
    run_nomenclature_classification_cycle(
        db_session, exchange_root=tmp_path, settings=_settings(), now=started
    )
    original = _write_result(tmp_path, operation, mode="dry_run", item_result="validated")
    run_nomenclature_classification_cycle(
        db_session, exchange_root=tmp_path, settings=_settings(), now=started
    )
    assert operation.state == "dry_run_ok"

    result_dir = tmp_path / "from_1c" / "new"
    retry = original.replace(
        b"<ProcessedAt>2026-08-04T12:00:00</ProcessedAt>",
        b"<ProcessedAt>2026-08-04T12:05:00</ProcessedAt>",
    )
    (
        result_dir / f"nomenclature_classifications_{operation.dry_run_message_id}.result.xml"
    ).write_bytes(retry)
    run_nomenclature_classification_cycle(
        db_session,
        exchange_root=tmp_path,
        settings=_settings(),
        now=started + timedelta(seconds=1),
    )
    assert operation.state == "dry_run_ok"
    assert operation.failure_kind is None

    conflicting = original.replace(b"<Message>OK</Message>", b"<Message>changed</Message>")
    (
        result_dir / f"nomenclature_classifications_{operation.dry_run_message_id}.result.xml"
    ).write_bytes(conflicting)
    run_nomenclature_classification_cycle(
        db_session,
        exchange_root=tmp_path,
        settings=_settings(),
        now=started + timedelta(seconds=2),
    )
    assert operation.state == "failed"
    assert operation.failure_kind == "result_identity_conflict"
    assert operation.items[0].active_nomenclature_key is not None

    cancel_nomenclature_classification_operation(
        db_session,
        operation.operation_id,
        requested_by="operator",
        confirm_read_only_reconciled=True,
    )
    second = register_nomenclature_classification_operation(
        db_session,
        (_intent(key="nom-class:РБ000001:decision-43:r1"),),
        approved_by="115204",
        requested_by="operator",
        settings=_settings(),
    )
    run_nomenclature_classification_cycle(
        db_session,
        exchange_root=tmp_path,
        settings=_settings(),
        now=started + timedelta(seconds=2),
    )
    _write_result(
        tmp_path,
        second,
        mode="dry_run",
        item_result="needs_review",
        status="partial",
    )
    run_nomenclature_classification_cycle(
        db_session,
        exchange_root=tmp_path,
        settings=_settings(),
        now=started + timedelta(seconds=3),
    )
    assert second.state == "failed"
    assert second.failure_kind == "partial_dry_run"
    assert second.items[0].active_nomenclature_key is not None


def test_end_to_end_lost_apply_uses_readback_without_replay(
    sqlite_engine,
    tmp_path: Path,
) -> None:
    from app.models import Base

    Base.metadata.create_all(sqlite_engine)
    started = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    try:
        with Session(sqlite_engine) as db:
            operation = register_nomenclature_classification_operation(
                db,
                (_intent(),),
                approved_by="115204",
                requested_by="operator",
                settings=_settings(),
            )
            operation_id = operation.operation_id
            run_nomenclature_classification_cycle(
                db, exchange_root=tmp_path, settings=_settings(), now=started
            )
            operation = db.scalar(
                select(NomenclatureClassificationOperation).where(
                    NomenclatureClassificationOperation.operation_id == operation_id
                )
            )
            assert operation is not None and operation.state == "dry_run_sent"
            dry_run_payload = _write_result(
                tmp_path, operation, mode="dry_run", item_result="validated"
            )

        # A new Session simulates a worker restart between publication and result consumption.
        with Session(sqlite_engine) as db:
            run_nomenclature_classification_cycle(
                db, exchange_root=tmp_path, settings=_settings(), now=started
            )
            operation = db.scalar(
                select(NomenclatureClassificationOperation).where(
                    NomenclatureClassificationOperation.operation_id == operation_id
                )
            )
            assert operation is not None and operation.state == "dry_run_ok"
            duplicate_dir = tmp_path / "from_1c" / "new"
            duplicate_dir.mkdir(parents=True, exist_ok=True)
            (
                duplicate_dir
                / f"nomenclature_classifications_{operation.dry_run_message_id}.result.xml"
            ).write_bytes(dry_run_payload)
            repeated = run_nomenclature_classification_cycle(
                db, exchange_root=tmp_path, settings=_settings(), now=started
            )
            assert repeated["errors"] == []
            assert operation.state == "dry_run_ok"
            request_nomenclature_classification_apply(
                db,
                operation_id,
                requested_by="operator",
                settings=_settings(),
            )
            run_nomenclature_classification_cycle(
                db, exchange_root=tmp_path, settings=_settings(), now=started
            )
            operation = db.scalar(
                select(NomenclatureClassificationOperation).where(
                    NomenclatureClassificationOperation.operation_id == operation_id
                )
            )
            assert operation is not None and operation.state == "apply_sent"
            apply_message_id = operation.apply_message_id

            run_nomenclature_classification_cycle(
                db,
                exchange_root=tmp_path,
                settings=_settings(),
                now=started + timedelta(seconds=61),
            )
            operation = db.scalar(
                select(NomenclatureClassificationOperation).where(
                    NomenclatureClassificationOperation.operation_id == operation_id
                )
            )
            assert operation is not None and operation.state == "applying"
            assert operation.readback_message_id and operation.readback_message_id.endswith(
                "-readback"
            )
            assert operation.apply_attempts == 1
            assert operation.apply_message_id == apply_message_id
            _write_result(tmp_path, operation, mode="readback", item_result="already_actual")

            run_nomenclature_classification_cycle(
                db,
                exchange_root=tmp_path,
                settings=_settings(),
                now=started + timedelta(seconds=62),
            )
            status = get_nomenclature_classification_status(db, operation_id)
            assert status["state"] == "applied"
            assert status["apply_attempts"] == 1
            assert operation.items[0].active_nomenclature_key is None

            # A late exact apply result after successful recovery is harmless and
            # cannot reopen or fail the already reconciled operation.
            _write_result(tmp_path, operation, mode="apply", item_result="applied")
            run_nomenclature_classification_cycle(
                db,
                exchange_root=tmp_path,
                settings=_settings(),
                now=started + timedelta(seconds=63),
            )
            assert operation.state == "applied"
            assert operation.apply_attempts == 1

            restore = NomenclatureClassificationIntentRow(
                idempotency_key="nom-class:РБ000001:restore:r2",
                nomenclature_code="РБ000001",
                nomenclature_guid=NOMENCLATURE,
                expected_kind=OneCClassificationReference(KIND_NEW, "NEW-KIND"),
                target_kind=OneCClassificationReference(KIND_NEW, "NEW-KIND"),
                expected_group=OneCClassificationReference(GROUP_NEW, "NEW-GROUP"),
                target_group=OneCClassificationReference(),
                group_mode="clear_expected",
                expected_category=OneCClassificationReference(CATEGORY_NEW, "NEW-CATEGORY"),
                target_category=OneCClassificationReference(),
                category_mode="remove_expected",
                reason="Восстановление исходного пустого состояния",
            )
            restored = register_nomenclature_classification_operation(
                db,
                (restore,),
                approved_by="115204",
                requested_by="operator",
                settings=_settings(),
            )
            run_nomenclature_classification_cycle(
                db,
                exchange_root=tmp_path,
                settings=_settings(),
                now=started + timedelta(seconds=64),
            )
            _write_result(tmp_path, restored, mode="dry_run", item_result="validated")
            run_nomenclature_classification_cycle(
                db,
                exchange_root=tmp_path,
                settings=_settings(),
                now=started + timedelta(seconds=65),
            )
            assert restored.state == "dry_run_ok"
            request_nomenclature_classification_apply(
                db,
                restored.operation_id,
                requested_by="operator",
                settings=_settings(),
            )
            run_nomenclature_classification_cycle(
                db,
                exchange_root=tmp_path,
                settings=_settings(),
                now=started + timedelta(seconds=66),
            )
            _write_result(tmp_path, restored, mode="apply", item_result="applied")
            run_nomenclature_classification_cycle(
                db,
                exchange_root=tmp_path,
                settings=_settings(),
                now=started + timedelta(seconds=67),
            )
            assert restored.state == "applied"
            assert restored.items[0].readback_category_guids == [UNRELATED_CATEGORY]
    finally:
        Base.metadata.drop_all(sqlite_engine)
