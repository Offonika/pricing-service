from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Iterable
from uuid import uuid4
from xml.etree import ElementTree

from pydantic import ValidationError
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.order_closure import OrderClosureBatch, OrderClosureEvent, OrderClosureItem
from app.schemas.order_closure import (
    OrderClosureBatchCreateRequest,
    OrderClosureCommandAckRequest,
    OrderClosureConfirmRequest,
)

MAX_BATCH_ITEMS = 200
LEASE_SECONDS = 1800
ALLOWED_REASONS = {
    "execution": "Исполнение заказа",
    "cancellation": "Отмена заказа",
}
PERIOD_RE = re.compile(r"^(?:\d{4}|\d{2}\.\d{2}\.\d{4})$")


class OrderClosureError(RuntimeError):
    code = "order_closure_error"


class OrderClosureConflict(OrderClosureError):
    code = "order_closure_conflict"


class OrderClosureNotFound(OrderClosureError):
    code = "order_closure_not_found"


class OrderClosureForbidden(OrderClosureError):
    code = "order_closure_forbidden"


@dataclass(frozen=True)
class Actor:
    actor_id: str
    actor_name: str | None
    can_confirm: bool


def _now(value: datetime | None = None) -> datetime:
    return value or datetime.now(UTC)


def _canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        ).encode("utf-8")
    ).hexdigest()


def _valid_period(value: str) -> bool:
    if not PERIOD_RE.fullmatch(value):
        return False
    try:
        if len(value) == 4:
            return 1 <= int(value) <= 9999
        datetime.strptime(value, "%d.%m.%Y")
    except ValueError:
        return False
    return True


def parse_pasted_lines(value: str) -> list[tuple[str, str | None]]:
    result: list[tuple[str, str | None]] = []
    seen: set[tuple[str, str | None]] = set()
    for raw_line in value.replace("\r", "\n").split("\n"):
        cells = [part.strip() for part in re.split(r"[\t;]+", raw_line.strip()) if part.strip()]
        if not cells:
            continue
        if len(cells) > 2:
            raise OrderClosureConflict("each row must contain number and optional date/year")
        period = next((cell for cell in cells if PERIOD_RE.fullmatch(cell)), None)
        numbers = [cell for cell in cells if cell != period]
        if len(numbers) != 1:
            raise OrderClosureConflict("each row must contain exactly one order number")
        key = (numbers[0], period)
        if key not in seen:
            result.append(key)
            seen.add(key)
        if len(result) > MAX_BATCH_ITEMS:
            raise OrderClosureConflict("batch item limit exceeded")
    if not result:
        raise OrderClosureConflict("order list is empty")
    return result


def _event(
    session: Session,
    batch: OrderClosureBatch,
    event_type: str,
    actor_id: str,
    payload: dict | None = None,
) -> None:
    session.add(
        OrderClosureEvent(
            batch=batch,
            event_type=event_type,
            actor_id=actor_id,
            payload=payload or {},
        )
    )


def create_batch(
    session: Session,
    *,
    payload: OrderClosureBatchCreateRequest,
    actor: Actor,
    now: datetime | None = None,
) -> OrderClosureBatch:
    created_at = _now(now)
    if payload.source_type == "excel":
        parsed = (
            parse_pasted_lines(payload.pasted_text or "")
            if payload.pasted_text
            else [
                (line.number.strip(), line.period.strip() if line.period else None)
                for line in payload.lines
            ]
        )
        if not parsed or len(parsed) > MAX_BATCH_ITEMS:
            raise OrderClosureConflict("batch must contain between 1 and 200 unique rows")
        if any(
            not number or period is None or not _valid_period(period) for number, period in parsed
        ):
            raise OrderClosureConflict("every row must contain a number and date/year")
        if len(set(parsed)) != len(parsed):
            raise OrderClosureConflict("duplicate input rows are not allowed")
        source_payload = {
            "rows": [{"number": number, "period": period} for number, period in parsed]
        }
    else:
        parsed = []
        source_payload = {"filters": payload.filters.model_dump() if payload.filters else {}}

    batch = OrderClosureBatch(
        public_id=str(uuid4()),
        status="draft",
        source_type=payload.source_type,
        source_payload=source_payload,
        actor_id=actor.actor_id,
        actor_name=actor.actor_name,
        command_kind="diagnose",
        command_requested_at=created_at,
        created_at=created_at,
        updated_at=created_at,
    )
    session.add(batch)
    session.flush()
    for position, (number, period) in enumerate(parsed, start=1):
        session.add(
            OrderClosureItem(
                batch=batch,
                position=position,
                input_number=number,
                input_period=period,
                status="pending",
                eligible=False,
                facts={},
                created_at=created_at,
                updated_at=created_at,
            )
        )
    _event(session, batch, "batch_created", actor.actor_id, {"source_type": payload.source_type})
    session.flush()
    return batch


def get_batch(session: Session, public_id: str) -> OrderClosureBatch:
    batch = session.scalar(
        select(OrderClosureBatch).where(OrderClosureBatch.public_id == public_id)
    )
    if batch is None:
        raise OrderClosureNotFound("batch not found")
    return batch


def request_diagnosis(
    session: Session, *, batch: OrderClosureBatch, actor: Actor, now: datetime | None = None
) -> OrderClosureBatch:
    if batch.status not in {"draft", "diagnosed", "stale", "failed"}:
        raise OrderClosureConflict("batch cannot be diagnosed in its current state")
    if batch.actor_id != actor.actor_id:
        raise OrderClosureForbidden("only the batch author can repeat diagnosis")
    requested_at = _now(now)
    batch.status = "draft"
    batch.command_kind = "diagnose"
    batch.command_requested_at = requested_at
    batch.lease_token = None
    batch.lease_until = None
    batch.diagnosis_hash = None
    batch.last_error_code = None
    batch.updated_at = requested_at
    for item in batch.items:
        item.status = "pending"
        item.eligible = False
        item.blocker_code = None
        item.blocker_text = None
        item.state_hash = None
        item.reason_code = None
        item.reason_ref = None
        item.reason_name = None
    _event(session, batch, "diagnosis_requested", actor.actor_id)
    session.flush()
    return batch


def confirm_batch(
    session: Session,
    *,
    batch: OrderClosureBatch,
    payload: OrderClosureConfirmRequest,
    actor: Actor,
    now: datetime | None = None,
) -> OrderClosureBatch:
    if not actor.can_confirm:
        raise OrderClosureForbidden("order_closure_operator role is required")
    if batch.actor_id != actor.actor_id:
        raise OrderClosureForbidden("the employee who created the batch must confirm it")
    if batch.status != "diagnosed" or not batch.diagnosis_hash:
        raise OrderClosureConflict("a completed diagnosis is required")
    if payload.diagnosis_hash != batch.diagnosis_hash:
        raise OrderClosureConflict("diagnosis has changed")
    assignments = {item.item_id: item for item in payload.assignments}
    if len(assignments) != len(payload.assignments):
        raise OrderClosureConflict("duplicate reason assignments are not allowed")
    eligible_by_id = {item.id: item for item in batch.items if item.eligible}
    if not assignments or not set(assignments).issubset(eligible_by_id):
        raise OrderClosureConflict("assignments must reference eligible rows")
    for item_id, assignment in assignments.items():
        item = eligible_by_id[item_id]
        expected_name = ALLOWED_REASONS[assignment.reason_code]
        if assignment.reason_name != expected_name:
            raise OrderClosureConflict("reason code and name do not match")
        allowed_reason = (item.facts or {}).get("allowed_reasons", {}).get(assignment.reason_code)
        if not isinstance(allowed_reason, dict) or not secrets.compare_digest(
            str(allowed_reason.get("ref") or ""), assignment.reason_ref
        ):
            raise OrderClosureConflict("reason reference does not match diagnosis")
        item.reason_code = assignment.reason_code
        item.reason_ref = assignment.reason_ref
        item.reason_name = assignment.reason_name
        item.status = "queued"
    for item in batch.items:
        if item.eligible and item.id not in assignments:
            item.status = "skipped"
    confirmed_at = _now(now)
    batch.status = "approved"
    batch.confirmed_by = actor.actor_id
    batch.confirmed_at = confirmed_at
    batch.command_kind = "apply"
    batch.command_requested_at = confirmed_at
    batch.lease_token = None
    batch.lease_until = None
    batch.updated_at = confirmed_at
    _event(
        session,
        batch,
        "batch_confirmed",
        actor.actor_id,
        {
            "diagnosis_hash": batch.diagnosis_hash,
            "assignments": [
                {
                    "item_id": assignment.item_id,
                    "reason_code": assignment.reason_code,
                    "reason_ref": assignment.reason_ref,
                }
                for assignment in payload.assignments
            ],
        },
    )
    session.flush()
    return batch


def lease_commands(
    session: Session,
    *,
    limit: int = 1,
    allow_apply: bool = True,
    now: datetime | None = None,
) -> list[OrderClosureBatch]:
    lease_at = _now(now)
    bounded_limit = max(1, min(int(limit), 10))
    conditions = [
        OrderClosureBatch.command_kind.is_not(None),
        or_(
            OrderClosureBatch.status.in_(("draft", "approved")),
            (OrderClosureBatch.status == "leased") & (OrderClosureBatch.lease_until < lease_at),
        ),
    ]
    if not allow_apply:
        conditions.append(OrderClosureBatch.command_kind == "diagnose")
    candidates = session.scalars(
        select(OrderClosureBatch)
        .where(*conditions)
        .order_by(OrderClosureBatch.command_requested_at, OrderClosureBatch.id)
        .limit(bounded_limit)
        .with_for_update(skip_locked=True)
    ).all()
    result: list[OrderClosureBatch] = []
    for batch in candidates:
        if batch.command_kind == "diagnose" and batch.status not in {"draft", "leased"}:
            continue
        if batch.command_kind == "apply" and batch.status not in {"approved", "leased"}:
            continue
        batch.status = "leased"
        batch.lease_token = secrets.token_hex(24)
        batch.lease_until = lease_at + timedelta(seconds=LEASE_SECONDS)
        batch.last_polled_at = lease_at
        batch.attempt_count += 1
        _event(session, batch, "command_leased", "onec:ut103", {"kind": batch.command_kind})
        result.append(batch)
    session.flush()
    return result


def _command_item_payload(item: OrderClosureItem) -> dict:
    return {
        "position": item.position,
        "input_number": item.input_number,
        "input_period": item.input_period or "",
        "onec_order_ref": item.onec_order_ref or "",
        "onec_order_number": item.onec_order_number or "",
        "onec_order_date": item.onec_order_date.isoformat() if item.onec_order_date else "",
        "state_hash": item.state_hash or "",
        "reason_code": item.reason_code or "",
        "reason_ref": item.reason_ref or "",
        "reason_name": item.reason_name or "",
    }


def render_commands_xml(
    batches: Iterable[OrderClosureBatch], *, generated_at: datetime | None = None
) -> bytes:
    root = ElementTree.Element(
        "order_closure_commands",
        {"generated_at": _now(generated_at).isoformat(), "stale": "false"},
    )
    for batch in batches:
        command = ElementTree.SubElement(
            root,
            "command",
            {
                "id": batch.public_id,
                "kind": batch.command_kind or "",
                "lease_token": batch.lease_token or "",
                "lease_until": batch.lease_until.isoformat() if batch.lease_until else "",
                "diagnosis_hash": batch.diagnosis_hash or "",
                "source_type": batch.source_type,
            },
        )
        source = ElementTree.SubElement(command, "source")
        if batch.source_type == "filter":
            filters = batch.source_payload.get("filters", {})
            ElementTree.SubElement(
                source,
                "filters",
                {name: str(value or "") for name, value in filters.items()},
            )
        for item in batch.items:
            if batch.command_kind == "apply" and item.status != "queued":
                continue
            node = ElementTree.SubElement(command, "order")
            for name, value in _command_item_payload(item).items():
                ElementTree.SubElement(node, name).text = str(value)
    return ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)


def ack_payload_from_xml(body: bytes) -> OrderClosureCommandAckRequest:
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as exc:
        raise OrderClosureConflict("invalid acknowledgement XML") from exc
    if root.tag != "order_closure_ack":
        raise OrderClosureConflict("unexpected acknowledgement root")
    try:
        items = []
        for node in root.findall("order"):
            facts = {
                key: value
                for key, value in node.attrib.items()
                if key
                in {
                    "closure_count",
                    "rtu_count",
                    "payment_count",
                    "has_debt",
                    "remaining",
                    "reserve",
                    "placement",
                }
            }
            allowed_reasons: dict[str, dict[str, str]] = {}
            for code in ALLOWED_REASONS:
                reason_ref = node.attrib.get(f"{code}_reason_ref", "")
                if reason_ref:
                    allowed_reasons[code] = {
                        "ref": reason_ref,
                        "name": ALLOWED_REASONS[code],
                    }
            if allowed_reasons:
                facts["allowed_reasons"] = allowed_reasons
            items.append(
                {
                    "position": int(node.attrib.get("position", "0")),
                    "input_number": node.attrib.get("input_number", ""),
                    "input_period": node.attrib.get("input_period") or None,
                    "onec_order_ref": node.attrib.get("onec_order_ref") or None,
                    "onec_order_number": node.attrib.get("onec_order_number") or None,
                    "onec_order_date": node.attrib.get("onec_order_date") or None,
                    "site_order_number": node.attrib.get("site_order_number") or None,
                    "department_ref": node.attrib.get("department_ref") or None,
                    "department_name": node.attrib.get("department_name") or None,
                    "eligible": node.attrib.get("eligible", "false").lower() == "true",
                    "blocker_code": node.attrib.get("blocker_code") or None,
                    "blocker_text": node.attrib.get("blocker_text") or None,
                    "facts": facts,
                    "state_hash": node.attrib.get("state_hash") or None,
                    "result_document_ref": node.attrib.get("result_document_ref") or None,
                    "result_document_number": node.attrib.get("result_document_number") or None,
                }
            )
        return OrderClosureCommandAckRequest(
            lease_token=root.attrib.get("lease_token", ""),
            outcome=root.attrib.get("outcome", "failed"),
            diagnosis_hash=root.attrib.get("diagnosis_hash") or None,
            receipt_hash=root.attrib.get("receipt_hash") or None,
            error_code=root.attrib.get("error_code") or None,
            items=items,
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise OrderClosureConflict("invalid acknowledgement XML payload") from exc


def acknowledge_command(
    session: Session,
    *,
    batch: OrderClosureBatch,
    payload: OrderClosureCommandAckRequest,
    now: datetime | None = None,
) -> tuple[OrderClosureBatch, bool]:
    if batch.status in {"applied", "stale", "failed", "diagnosed"} and batch.lease_token is None:
        return batch, True
    if batch.status != "leased" or not secrets.compare_digest(
        batch.lease_token or "", payload.lease_token
    ):
        raise OrderClosureConflict("lease token does not match")
    command_kind = batch.command_kind
    if command_kind == "diagnose" and payload.outcome != "diagnosed":
        if payload.outcome not in {"failed", "stale"}:
            raise OrderClosureConflict("unexpected diagnosis outcome")
    if command_kind == "apply" and payload.outcome == "diagnosed":
        raise OrderClosureConflict("unexpected apply outcome")
    if command_kind == "apply" and payload.diagnosis_hash != batch.diagnosis_hash:
        raise OrderClosureConflict("apply acknowledgement hash does not match the command")
    if payload.outcome == "diagnosed" and not payload.diagnosis_hash:
        raise OrderClosureConflict("diagnosed acknowledgement requires the 1C state hash")

    ack_at = _now(now)
    if payload.outcome == "diagnosed":
        existing = {item.position: item for item in batch.items}
        if batch.source_type == "filter":
            batch.items.clear()
            session.flush()
            existing = {}
        if len(payload.items) > MAX_BATCH_ITEMS or (
            batch.source_type == "excel" and not payload.items
        ):
            raise OrderClosureConflict("diagnosis must return the requested rows, up to 200")
        positions = [item.position for item in payload.items]
        if len(set(positions)) != len(positions):
            raise OrderClosureConflict("diagnosis positions must be unique")
        expected_positions = (
            set(existing) if batch.source_type == "excel" else set(range(1, len(payload.items) + 1))
        )
        if set(positions) != expected_positions:
            raise OrderClosureConflict("diagnosis must preserve contiguous input positions")
        resolved_refs = [
            item.onec_order_ref for item in payload.items if item.eligible and item.onec_order_ref
        ]
        if len(set(resolved_refs)) != len(resolved_refs):
            raise OrderClosureConflict("diagnosis must resolve unique orders")
        for ack_item in payload.items:
            if ack_item.eligible and (
                not ack_item.onec_order_ref
                or not ack_item.onec_order_number
                or not ack_item.onec_order_date
                or not ack_item.state_hash
            ):
                raise OrderClosureConflict(
                    "eligible diagnosis rows require exact order identity and state hash"
                )
            item = existing.get(ack_item.position)
            if item is None:
                item = OrderClosureItem(
                    batch=batch,
                    position=ack_item.position,
                    input_number=ack_item.input_number,
                    input_period=ack_item.input_period,
                    facts={},
                    status="pending",
                    eligible=False,
                    created_at=ack_at,
                    updated_at=ack_at,
                )
                session.add(item)
            item.onec_order_ref = ack_item.onec_order_ref
            item.onec_order_number = ack_item.onec_order_number
            item.onec_order_date = ack_item.onec_order_date
            item.site_order_number = ack_item.site_order_number
            item.department_ref = ack_item.department_ref
            item.department_name = ack_item.department_name
            item.eligible = ack_item.eligible
            item.status = "eligible" if ack_item.eligible else "blocked"
            item.blocker_code = ack_item.blocker_code
            item.blocker_text = ack_item.blocker_text
            item.facts = ack_item.facts
            item.state_hash = ack_item.state_hash or _canonical_hash(ack_item.model_dump())
            item.updated_at = ack_at
        session.flush()
        computed = _canonical_hash(
            [
                {
                    "position": item.position,
                    "order_ref": item.onec_order_ref,
                    "eligible": item.eligible,
                    "blocker": item.blocker_code,
                    "facts": item.facts,
                    "state_hash": item.state_hash,
                }
                for item in sorted(batch.items, key=lambda row: row.position)
            ]
        )
        # The accounting snapshot hash is authored by UT 10.3. The backend also
        # computes a transport hash when an older client omits it, but never
        # substitutes its own accounting interpretation for the 1C snapshot.
        batch.diagnosis_hash = payload.diagnosis_hash or computed
        batch.status = "diagnosed"
    else:
        results = {item.position: item for item in payload.items}
        queued_positions = {item.position for item in batch.items if item.status == "queued"}
        if payload.outcome == "applied" and set(results) != queued_positions:
            raise OrderClosureConflict("applied acknowledgement is incomplete")
        if payload.outcome == "applied" and any(
            not item.result_document_ref or not item.result_document_number
            for item in payload.items
        ):
            raise OrderClosureConflict("applied acknowledgement requires document links")
        for item in batch.items:
            result = results.get(item.position)
            if payload.outcome == "applied" and result is not None:
                item.result_document_ref = result.result_document_ref
                item.result_document_number = result.result_document_number
                item.status = "applied"
                item.updated_at = ack_at
            elif item.status == "queued":
                item.result_document_ref = None
                item.result_document_number = None
                item.status = payload.outcome
                item.updated_at = ack_at
        batch.status = payload.outcome
        batch.last_error_code = payload.error_code
        if payload.outcome == "applied":
            batch.applied_at = ack_at
    batch.command_kind = None
    batch.lease_token = None
    batch.lease_until = None
    batch.updated_at = ack_at
    _event(
        session,
        batch,
        f"command_{payload.outcome}",
        "onec:ut103",
        {
            "error_code": payload.error_code,
            "receipt_hash": payload.receipt_hash,
            "diagnosis_hash": payload.diagnosis_hash,
            "results": [
                {
                    "position": item.position,
                    "document_ref": item.result_document_ref,
                    "document_number": item.result_document_number,
                }
                for item in payload.items
                if item.result_document_ref or item.result_document_number
            ],
        },
    )
    session.flush()
    return batch, False
