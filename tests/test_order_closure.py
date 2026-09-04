from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api import order_closure as order_closure_api
from app.core.config import Settings
from app.main import app
from app.models.base import Base
from app.models.order_closure import OrderClosureBatch, OrderClosureEvent, OrderClosureItem
from app.schemas.order_closure import (
    OrderClosureBatchCreateRequest,
    OrderClosureCommandAckRequest,
    OrderClosureConfirmRequest,
)
from app.services import order_closure as service
from app.services.bitrix_order_closure_auth import (
    create_order_closure_session_token,
    verify_order_closure_session_token,
)

ORDER_REF = "11111111-1111-1111-1111-111111111111"
REASON_REF = "22222222-2222-2222-2222-222222222222"
DOCUMENT_REF = "33333333-3333-3333-3333-333333333333"
DIAGNOSIS_HASH = "d" * 64
RECEIPT_HASH = "e" * 64
STATE_HASH = "f" * 64


def _engine():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[
            OrderClosureBatch.__table__,
            OrderClosureItem.__table__,
            OrderClosureEvent.__table__,
        ],
    )
    return engine


def _operator() -> service.Actor:
    return service.Actor("bitrix:member:42", "Иван", True)


def _diagnosis_item(position: int = 1):
    return {
        "position": position,
        "input_number": "12345",
        "input_period": "2025",
        "onec_order_ref": ORDER_REF,
        "onec_order_number": "12345",
        "onec_order_date": date(2025, 5, 10),
        "eligible": True,
        "state_hash": STATE_HASH,
        "facts": {
            "has_closure": False,
            "rtu_count": 0,
            "payment_count": 0,
            "has_debt": False,
            "remaining": 0,
            "reserve": 0,
            "placement": 0,
            "allowed_reasons": {
                "cancellation": {
                    "ref": REASON_REF,
                    "name": "Отмена заказа",
                }
            },
        },
    }


def test_excel_parser_accepts_both_column_orders_and_deduplicates() -> None:
    assert service.parse_pasted_lines("123\t2025\n10.05.2025\t124\n123\t2025") == [
        ("123", "2025"),
        ("124", "10.05.2025"),
    ]


def test_excel_batch_requires_date_or_year_for_every_row() -> None:
    engine = _engine()
    with Session(engine) as db:
        with pytest.raises(service.OrderClosureConflict, match="date/year"):
            service.create_batch(
                db,
                payload=OrderClosureBatchCreateRequest(source_type="excel", pasted_text="12345"),
                actor=_operator(),
            )
        with pytest.raises(service.OrderClosureConflict, match="date/year"):
            service.create_batch(
                db,
                payload=OrderClosureBatchCreateRequest(
                    source_type="excel", pasted_text="12345\t31.02.2025"
                ),
                actor=_operator(),
            )


def test_excel_batch_rejects_more_than_200_unique_rows() -> None:
    engine = _engine()
    pasted = "\n".join(f"{number}\t2025" for number in range(201))
    with Session(engine) as db, pytest.raises(service.OrderClosureConflict, match="limit"):
        service.create_batch(
            db,
            payload=OrderClosureBatchCreateRequest(source_type="excel", pasted_text=pasted),
            actor=_operator(),
        )


def test_unknown_reason_is_rejected_by_the_v1_contract() -> None:
    with pytest.raises(ValidationError):
        OrderClosureConfirmRequest.model_validate(
            {
                "diagnosis_hash": DIAGNOSIS_HASH,
                "assignments": [
                    {
                        "item_id": 1,
                        "reason_code": "other",
                        "reason_ref": REASON_REF,
                        "reason_name": "Прочая причина",
                    }
                ],
            }
        )


def test_batch_diagnose_confirm_lease_and_apply_roundtrip() -> None:
    engine = _engine()
    now = datetime(2026, 9, 4, 9, 0, tzinfo=UTC)
    with Session(engine) as db:
        batch = service.create_batch(
            db,
            payload=OrderClosureBatchCreateRequest(source_type="excel", pasted_text="12345\t2025"),
            actor=_operator(),
            now=now,
        )
        db.commit()
        leased = service.lease_commands(db, now=now)[0]
        assert leased.command_kind == "diagnose"
        diagnosis_lease = leased.lease_token
        db.commit()

        batch, duplicate = service.acknowledge_command(
            db,
            batch=batch,
            payload=OrderClosureCommandAckRequest(
                lease_token=diagnosis_lease,
                outcome="diagnosed",
                diagnosis_hash=DIAGNOSIS_HASH,
                receipt_hash=RECEIPT_HASH,
                items=[_diagnosis_item()],
            ),
            now=now,
        )
        assert duplicate is False
        assert batch.status == "diagnosed"
        assert batch.diagnosis_hash
        item = batch.items[0]

        service.confirm_batch(
            db,
            batch=batch,
            actor=_operator(),
            payload=OrderClosureConfirmRequest(
                diagnosis_hash=batch.diagnosis_hash,
                assignments=[
                    {
                        "item_id": item.id,
                        "reason_code": "cancellation",
                        "reason_ref": REASON_REF,
                        "reason_name": "Отмена заказа",
                    }
                ],
            ),
            now=now,
        )
        assert batch.status == "approved"
        apply_command = service.lease_commands(db, now=now)[0]
        apply_lease = apply_command.lease_token
        xml = service.render_commands_xml([apply_command], generated_at=now).decode("utf-8")
        assert 'kind="apply"' in xml
        assert "Отмена заказа" in xml

        batch, _ = service.acknowledge_command(
            db,
            batch=batch,
            payload=OrderClosureCommandAckRequest(
                lease_token=apply_lease,
                outcome="applied",
                diagnosis_hash=batch.diagnosis_hash,
                receipt_hash="a" * 64,
                items=[
                    {
                        **_diagnosis_item(),
                        "result_document_ref": DOCUMENT_REF,
                        "result_document_number": "РБ000001",
                    }
                ],
            ),
            now=now,
        )
        assert batch.status == "applied"
        assert batch.items[0].result_document_number == "РБ000001"


def test_confirm_requires_author_operator_and_current_hash() -> None:
    engine = _engine()
    with Session(engine) as db:
        batch = service.create_batch(
            db,
            payload=OrderClosureBatchCreateRequest(source_type="excel", pasted_text="123\t2025"),
            actor=_operator(),
        )
        leased = service.lease_commands(db)[0]
        batch, _ = service.acknowledge_command(
            db,
            batch=batch,
            payload=OrderClosureCommandAckRequest(
                lease_token=leased.lease_token,
                outcome="diagnosed",
                diagnosis_hash=DIAGNOSIS_HASH,
                receipt_hash=RECEIPT_HASH,
                items=[_diagnosis_item()],
            ),
        )
        item_id = batch.items[0].id
        payload = OrderClosureConfirmRequest(
            diagnosis_hash="0" * 64,
            assignments=[
                {
                    "item_id": item_id,
                    "reason_code": "cancellation",
                    "reason_ref": REASON_REF,
                    "reason_name": "Отмена заказа",
                }
            ],
        )
        with pytest.raises(service.OrderClosureConflict):
            service.confirm_batch(db, batch=batch, payload=payload, actor=_operator())
        payload.diagnosis_hash = batch.diagnosis_hash
        payload.assignments[0].reason_ref = "99999999-9999-9999-9999-999999999999"
        with pytest.raises(service.OrderClosureConflict, match="reference"):
            service.confirm_batch(db, batch=batch, payload=payload, actor=_operator())
        payload.assignments[0].reason_ref = REASON_REF
        with pytest.raises(service.OrderClosureForbidden):
            service.confirm_batch(
                db,
                batch=batch,
                payload=payload,
                actor=service.Actor(_operator().actor_id, "Иван", False),
            )


def test_business_mismatch_marks_whole_batch_stale_without_results() -> None:
    engine = _engine()
    with Session(engine) as db:
        batch = service.create_batch(
            db,
            payload=OrderClosureBatchCreateRequest(source_type="excel", pasted_text="123\t2025"),
            actor=_operator(),
        )
        leased = service.lease_commands(db)[0]
        batch, _ = service.acknowledge_command(
            db,
            batch=batch,
            payload=OrderClosureCommandAckRequest(
                lease_token=leased.lease_token,
                outcome="stale",
                receipt_hash=RECEIPT_HASH,
                error_code="order_state_changed",
                items=[],
            ),
        )
        assert batch.status == "stale"
        assert batch.last_error_code == "order_state_changed"
        assert all(item.result_document_ref is None for item in batch.items)


def test_ack_after_terminal_state_is_idempotent() -> None:
    engine = _engine()
    with Session(engine) as db:
        batch = service.create_batch(
            db,
            payload=OrderClosureBatchCreateRequest(source_type="excel", pasted_text="123\t2025"),
            actor=_operator(),
        )
        leased = service.lease_commands(db)[0]
        ack = OrderClosureCommandAckRequest(
            lease_token=leased.lease_token,
            outcome="failed",
            receipt_hash=RECEIPT_HASH,
            error_code="temporary_error",
        )
        batch, duplicate = service.acknowledge_command(db, batch=batch, payload=ack)
        assert duplicate is False
        batch, duplicate = service.acknowledge_command(db, batch=batch, payload=ack)
        assert duplicate is True


def test_expired_lease_is_redelivered_with_a_new_token() -> None:
    engine = _engine()
    started = datetime(2026, 9, 4, 9, 0, tzinfo=UTC)
    with Session(engine) as db:
        batch = service.create_batch(
            db,
            payload=OrderClosureBatchCreateRequest(source_type="excel", pasted_text="123\t2025"),
            actor=_operator(),
            now=started,
        )
        first = service.lease_commands(db, now=started)[0]
        first_token = first.lease_token
        assert service.lease_commands(db, now=started + timedelta(minutes=29)) == []
        repeated = service.lease_commands(db, now=started + timedelta(minutes=31))[0]

        assert repeated.public_id == batch.public_id
        assert repeated.lease_token != first_token
        assert repeated.attempt_count == 2


def test_filter_diagnosis_replaces_placeholder_rows_and_preserves_onec_hash() -> None:
    engine = _engine()
    with Session(engine) as db:
        batch = service.create_batch(
            db,
            payload=OrderClosureBatchCreateRequest(
                source_type="filter",
                filters={"year": 2025, "category": "all", "state": "all"},
            ),
            actor=_operator(),
        )
        leased = service.lease_commands(db)[0]
        onec_hash = "a" * 64
        batch, _ = service.acknowledge_command(
            db,
            batch=batch,
            payload=OrderClosureCommandAckRequest(
                lease_token=leased.lease_token,
                outcome="diagnosed",
                diagnosis_hash=onec_hash,
                receipt_hash=RECEIPT_HASH,
                items=[
                    _diagnosis_item(1),
                    {
                        **_diagnosis_item(2),
                        "input_number": "12346",
                        "onec_order_ref": "44444444-4444-4444-4444-444444444444",
                    },
                ],
            ),
        )

        assert batch.diagnosis_hash == onec_hash
        assert [item.position for item in batch.items] == [1, 2]
        service.confirm_batch(
            db,
            batch=batch,
            actor=_operator(),
            payload=OrderClosureConfirmRequest(
                diagnosis_hash=onec_hash,
                assignments=[
                    {
                        "item_id": item.id,
                        "reason_code": "cancellation",
                        "reason_ref": REASON_REF,
                        "reason_name": "Отмена заказа",
                    }
                    for item in batch.items
                ],
            ),
        )
        apply = service.lease_commands(db)[0]
        xml = service.render_commands_xml([apply]).decode("utf-8")
        assert f'diagnosis_hash="{onec_hash}"' in xml


def test_confirmation_can_select_an_eligible_subset_without_sending_skipped_rows() -> None:
    engine = _engine()
    with Session(engine) as db:
        batch = service.create_batch(
            db,
            payload=OrderClosureBatchCreateRequest(
                source_type="excel", pasted_text="12345\t2025\n12346\t2025"
            ),
            actor=_operator(),
        )
        leased = service.lease_commands(db)[0]
        second = {
            **_diagnosis_item(2),
            "input_number": "12346",
            "onec_order_number": "12346",
            "onec_order_ref": "44444444-4444-4444-4444-444444444444",
        }
        batch, _ = service.acknowledge_command(
            db,
            batch=batch,
            payload=OrderClosureCommandAckRequest(
                lease_token=leased.lease_token,
                outcome="diagnosed",
                diagnosis_hash="1" * 64,
                receipt_hash=RECEIPT_HASH,
                items=[_diagnosis_item(1), second],
            ),
        )
        service.confirm_batch(
            db,
            batch=batch,
            actor=_operator(),
            payload=OrderClosureConfirmRequest(
                diagnosis_hash=batch.diagnosis_hash,
                assignments=[
                    {
                        "item_id": batch.items[0].id,
                        "reason_code": "cancellation",
                        "reason_ref": REASON_REF,
                        "reason_name": "Отмена заказа",
                    }
                ],
            ),
        )

        assert [item.status for item in batch.items] == ["queued", "skipped"]
        assert service.lease_commands(db, allow_apply=False) == []
        apply = service.lease_commands(db)[0]
        xml = service.render_commands_xml([apply]).decode("utf-8")
        assert "12345" in xml
        assert "12346" not in xml


def test_confirmation_rejects_assignments_to_blocked_rows() -> None:
    engine = _engine()
    with Session(engine) as db:
        batch = service.create_batch(
            db,
            payload=OrderClosureBatchCreateRequest(
                source_type="excel", pasted_text="12345\t2025\n12346\t2025"
            ),
            actor=_operator(),
        )
        leased = service.lease_commands(db)[0]
        blocked = {
            **_diagnosis_item(2),
            "input_number": "12346",
            "onec_order_ref": None,
            "onec_order_number": None,
            "onec_order_date": None,
            "eligible": False,
            "blocker_code": "ambiguous_order",
            "blocker_text": "Номер заказа неоднозначен",
        }
        batch, _ = service.acknowledge_command(
            db,
            batch=batch,
            payload=OrderClosureCommandAckRequest(
                lease_token=leased.lease_token,
                outcome="diagnosed",
                diagnosis_hash="b" * 64,
                receipt_hash=RECEIPT_HASH,
                items=[_diagnosis_item(1), blocked],
            ),
        )

        with pytest.raises(service.OrderClosureConflict, match="eligible rows"):
            service.confirm_batch(
                db,
                batch=batch,
                actor=_operator(),
                payload=OrderClosureConfirmRequest(
                    diagnosis_hash=batch.diagnosis_hash,
                    assignments=[
                        {
                            "item_id": batch.items[1].id,
                            "reason_code": "cancellation",
                            "reason_ref": REASON_REF,
                            "reason_name": "Отмена заказа",
                        }
                    ],
                ),
            )


def test_diagnosis_rejects_duplicate_or_missing_positions() -> None:
    engine = _engine()
    with Session(engine) as db:
        batch = service.create_batch(
            db,
            payload=OrderClosureBatchCreateRequest(
                source_type="excel", pasted_text="12345\t2025\n12346\t2025"
            ),
            actor=_operator(),
        )
        leased = service.lease_commands(db)[0]
        with pytest.raises(service.OrderClosureConflict, match="positions"):
            service.acknowledge_command(
                db,
                batch=batch,
                payload=OrderClosureCommandAckRequest(
                    lease_token=leased.lease_token,
                    outcome="diagnosed",
                    diagnosis_hash=DIAGNOSIS_HASH,
                    receipt_hash=RECEIPT_HASH,
                    items=[_diagnosis_item(1)],
                ),
            )


def test_stale_ack_discards_partial_document_results() -> None:
    engine = _engine()
    with Session(engine) as db:
        batch = service.create_batch(
            db,
            payload=OrderClosureBatchCreateRequest(source_type="excel", pasted_text="123\t2025"),
            actor=_operator(),
        )
        diagnose = service.lease_commands(db)[0]
        batch, _ = service.acknowledge_command(
            db,
            batch=batch,
            payload=OrderClosureCommandAckRequest(
                lease_token=diagnose.lease_token,
                outcome="diagnosed",
                diagnosis_hash="c" * 64,
                receipt_hash=RECEIPT_HASH,
                items=[_diagnosis_item()],
            ),
        )
        service.confirm_batch(
            db,
            batch=batch,
            actor=_operator(),
            payload=OrderClosureConfirmRequest(
                diagnosis_hash=batch.diagnosis_hash,
                assignments=[
                    {
                        "item_id": batch.items[0].id,
                        "reason_code": "cancellation",
                        "reason_ref": REASON_REF,
                        "reason_name": "Отмена заказа",
                    }
                ],
            ),
        )
        apply = service.lease_commands(db)[0]
        batch, _ = service.acknowledge_command(
            db,
            batch=batch,
            payload=OrderClosureCommandAckRequest(
                lease_token=apply.lease_token,
                outcome="stale",
                diagnosis_hash=batch.diagnosis_hash,
                receipt_hash=RECEIPT_HASH,
                error_code="order_state_changed",
                items=[{**_diagnosis_item(), "result_document_ref": DOCUMENT_REF}],
            ),
        )

        assert batch.status == "stale"
        assert batch.items[0].result_document_ref is None


def _auth_settings() -> Settings:
    return Settings(
        order_closure_bitrix_enabled=True,
        order_closure_bitrix_allowed_domains=["crm.example.test"],
        order_closure_bitrix_allowed_member_ids=["member-1"],
        order_closure_bitrix_allowed_user_ids=["viewer-1"],
        order_closure_operator_user_ids=["operator-1"],
        order_closure_bitrix_session_secret="order-closure-test-secret",
        order_closure_apply_enabled=True,
        order_closure_internal_api_token="internal-test-token",
    )


@pytest.mark.parametrize(("user_id", "can_confirm"), [("viewer-1", False), ("operator-1", True)])
def test_bitrix_session_separates_viewer_and_operator_roles(
    user_id: str, can_confirm: bool
) -> None:
    settings = _auth_settings()
    token, _ = create_order_closure_session_token(
        domain="crm.example.test",
        member_id="member-1",
        user_id=user_id,
        user_name="Тест",
        can_confirm=can_confirm,
        settings=settings,
        now=1000,
    )
    session = verify_order_closure_session_token(token, settings=settings, now=1001)
    assert session.can_confirm is can_confirm


def test_apply_gate_keeps_operator_read_only_until_rollout() -> None:
    settings = _auth_settings().model_copy(update={"order_closure_apply_enabled": False})
    token, _ = create_order_closure_session_token(
        domain="crm.example.test",
        member_id="member-1",
        user_id="operator-1",
        user_name="Оператор",
        can_confirm=False,
        settings=settings,
        now=1000,
    )
    session = verify_order_closure_session_token(token, settings=settings, now=1001)
    assert session.can_confirm is False


def test_internal_api_requires_its_dedicated_bearer(monkeypatch) -> None:
    settings = _auth_settings()
    monkeypatch.setattr(order_closure_api, "get_settings", lambda: settings)
    valid = HTTPAuthorizationCredentials(scheme="Bearer", credentials="internal-test-token")
    invalid = HTTPAuthorizationCredentials(scheme="Bearer", credentials="wrong-token")

    assert order_closure_api.require_internal_token(valid) == "internal-test-token"
    with pytest.raises(HTTPException) as exc_info:
        order_closure_api.require_internal_token(invalid)
    assert exc_info.value.status_code == 401


def test_order_closure_api_contract_is_exposed() -> None:
    paths = app.openapi()["paths"]
    assert {
        "/api/order-closures/candidates",
        "/api/order-closures/reasons",
        "/api/order-closures/batches",
        "/api/order-closures/batches/{batch_id}/diagnose",
        "/api/order-closures/batches/{batch_id}/confirm",
        "/api/order-closures/batches/{batch_id}",
        "/api/order-closures/internal/commands",
        "/api/order-closures/internal/commands/{batch_id}/ack",
    }.issubset(paths)


def test_xml_ack_parses_exact_refs_hash_and_escaped_text() -> None:
    payload = service.ack_payload_from_xml(f"""<?xml version="1.0" encoding="utf-8"?>
<order_closure_ack lease_token="lease" outcome="diagnosed"
 diagnosis_hash="{'d' * 64}" receipt_hash="{'e' * 64}">
  <order position="1" input_number="123&amp;45" input_period="2025"
   onec_order_ref="{ORDER_REF}" onec_order_number="12345"
   onec_order_date="2025-05-10" eligible="true" state_hash="{'f' * 64}"
   cancellation_reason_ref="{REASON_REF}" />
</order_closure_ack>""".encode())

    assert payload.diagnosis_hash == "d" * 64
    assert payload.receipt_hash == "e" * 64
    assert payload.items[0].input_number == "123&45"
    assert payload.items[0].onec_order_ref == ORDER_REF
    assert payload.items[0].facts["allowed_reasons"]["cancellation"]["ref"] == REASON_REF


def test_xml_ack_rejects_invalid_positions_and_refs() -> None:
    with pytest.raises(service.OrderClosureConflict, match="invalid acknowledgement"):
        service.ack_payload_from_xml(
            b'<order_closure_ack lease_token="lease" outcome="diagnosed">'
            b'<order position="0" input_number="123" onec_order_ref="not-a-uuid" />'
            b"</order_closure_ack>"
        )
