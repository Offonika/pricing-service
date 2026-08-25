from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import Session, aliased

from app.models.logistics import LogisticsWarehouse
from app.models.site_order_fulfillment import (
    BitrixChatMessage,
    PickupInventoryItem,
    PickupInventoryRun,
    PickupInventorySubmission,
    SiteOrderExecutionEvent,
)
from app.services import site_order_fulfillment as fulfillment
from app.services.logistics_onec import WarehouseResolution, resolve_target_warehouse

PARSER_VERSION = "pickup-inventory-v2"
MODE_FULL = "full"
MODE_CARRY = "carry"
MODE_ZERO = "zero"
MODE_CORRECTION = "correction"
STATUS_CONFIRMED = "confirmed"
STATUS_MANUAL_REVIEW = "manual_review"

CARRY_MARKERS = (
    "всё актуально",
    "все актуально",
    "все заказы актуальны",
    "заказы актуальны",
    "все актульно",
    "без изменений",
)
ZERO_MARKERS = (
    "нет невыданных",
    "невыданных нет",
    "остатков нет",
    "нулевой остаток",
    "заказов нет",
    "все заказы выданы",
    "всё выдано",
    "все выдано",
)
CORRECTION_MARKERS = ("исправление", "коррекция", "уточнение", "актуальный список")
NON_STATE_MARKERS = (
    "расформир",
    "клиент отказ",
    "клиент забрал",
    "заказ выдан",
    "заказы выданы",
    "продлить",
    "продлен",
    "продлён",
    "удерживать",
    "оставьте",
)
GLUED_ORDER_RE = re.compile(r"(?<!\d)(2\d{5})(2\d{5})(?!\d)")
BLOCKING_EVENT_TYPES = {
    fulfillment.EVENT_PICKUP_MOVING,
    fulfillment.EVENT_PICKUP_REDIRECTED,
    fulfillment.EVENT_PICKUP_STORED,
    fulfillment.EVENT_PICKUP_DISMANTLING,
    fulfillment.EVENT_PICKUP_DISMANTLED,
    fulfillment.EVENT_PICKUP_EXCEPTION,
}


@dataclass(frozen=True, slots=True)
class InventoryParseResult:
    mode: str
    order_numbers: tuple[str, ...]
    ambiguous_tokens: tuple[str, ...]
    explicit: bool


@dataclass(frozen=True, slots=True)
class InventoryDisappearance:
    site_order_number: str
    warehouse_id: int
    previous_submission_id: int
    current_submission_id: int
    previous_at: datetime
    current_at: datetime


def parse_inventory_text(
    text_value: str,
    *,
    order_exists: Callable[[str], bool] | None = None,
) -> InventoryParseResult:
    normalized = fulfillment._clean_text(text_value)  # noqa: SLF001
    if any(marker in normalized for marker in CARRY_MARKERS):
        return InventoryParseResult(MODE_CARRY, (), (), True)
    if any(marker in normalized for marker in ZERO_MARKERS):
        return InventoryParseResult(MODE_ZERO, (), (), True)
    if "заказы выданы" in normalized and not fulfillment.extract_order_numbers(text_value):
        return InventoryParseResult(MODE_ZERO, (), (), True)
    mode = (
        MODE_CORRECTION if any(marker in normalized for marker in CORRECTION_MARKERS) else MODE_FULL
    )
    order_numbers = list(fulfillment.extract_order_numbers(text_value))
    ambiguous: list[str] = []
    for match in GLUED_ORDER_RE.finditer(text_value):
        left, right = match.groups()
        if order_exists is not None and order_exists(left) and order_exists(right):
            order_numbers.extend((left, right))
        else:
            ambiguous.append(match.group(0))
    non_state_message = any(marker in normalized for marker in NON_STATE_MARKERS)
    return InventoryParseResult(
        mode,
        tuple(dict.fromkeys(order_numbers)),
        tuple(dict.fromkeys(ambiguous)),
        bool(order_numbers) and not ambiguous and not non_state_message,
    )


def persist_inventory_message(
    session: Session,
    *,
    message: BitrixChatMessage,
    order_exists: Callable[[str], bool] | None = None,
    pickup_aliases: dict[str, list[str]] | None = None,
) -> PickupInventorySubmission | None:
    existing = session.scalar(
        select(PickupInventorySubmission).where(
            PickupInventorySubmission.source_message_id == message.id
        )
    )
    if existing is not None:
        return existing
    text_value = fulfillment.bitrix_message_text_for_parsing(message)
    parsed = parse_inventory_text(text_value, order_exists=order_exists)
    resolution = resolve_pickup_inventory_warehouse(
        session,
        text_value,
        pickup_aliases=pickup_aliases,
    )
    submitted_at = message.message_at or datetime.now(UTC).replace(tzinfo=None)
    business_date = _moscow_date(submitted_at)
    run = session.scalar(
        select(PickupInventoryRun).where(
            PickupInventoryRun.dialog_id == message.dialog_id,
            PickupInventoryRun.business_date == business_date,
        )
    )
    if run is None:
        run = PickupInventoryRun(
            dialog_id=message.dialog_id,
            business_date=business_date,
            status="open",
            started_at=submitted_at,
            payload={},
        )
        session.add(run)
        session.flush()
    resolved_warehouse = resolution.warehouse
    if resolved_warehouse is not None and resolved_warehouse.kind not in {"store", "retail"}:
        resolved_warehouse = None
    warehouse_id = resolved_warehouse.id if resolved_warehouse is not None else None
    latest = (
        latest_confirmed_submission(
            session,
            warehouse_id=warehouse_id,
            before_at=submitted_at,
        )
        if warehouse_id is not None
        else None
    )
    revision = (
        int(
            session.scalar(
                select(func.max(PickupInventorySubmission.revision)).where(
                    PickupInventorySubmission.run_id == run.id,
                    PickupInventorySubmission.warehouse_id == warehouse_id,
                )
            )
            or 0
        )
        + 1
    )
    status = (
        STATUS_CONFIRMED if parsed.explicit and warehouse_id is not None else STATUS_MANUAL_REVIEW
    )
    order_numbers = list(parsed.order_numbers)
    if parsed.mode == MODE_CARRY:
        if latest is None:
            status = STATUS_MANUAL_REVIEW
        else:
            order_numbers = [item.site_order_number for item in latest.items]
    submission = PickupInventorySubmission(
        run_id=run.id,
        warehouse_id=warehouse_id,
        source_message_id=message.id,
        supersedes_submission_id=(latest.id if latest is not None else None),
        author_id=message.author_id,
        mode=parsed.mode,
        status=status,
        revision=revision,
        submitted_at=submitted_at,
        confirmed_at=(submitted_at if status == STATUS_CONFIRMED else None),
        parser_version=PARSER_VERSION,
        payload={
            "ambiguous_tokens": list(parsed.ambiguous_tokens),
            "pickup_resolution_reason": (
                resolution.reason
                if resolved_warehouse is not None or resolution.warehouse is None
                else "matched warehouse is not a pickup point"
            ),
            "pickup_matches": resolution.matches,
        },
    )
    session.add(submission)
    session.flush()
    for order_number in order_numbers:
        session.add(
            PickupInventoryItem(
                submission_id=submission.id,
                site_order_number=order_number,
                validation_status="valid",
                payload={},
            )
        )
    run.updated_at = datetime.now(UTC).replace(tzinfo=None)
    session.flush()
    return submission


def resolve_pickup_inventory_warehouse(
    session: Session,
    text_value: str,
    *,
    pickup_aliases: dict[str, list[str]] | None = None,
) -> WarehouseResolution:
    """Resolve chat-only aliases without changing shared RTU/address matching."""

    resolution = resolve_target_warehouse(session, [text_value])
    if resolution.warehouse is not None:
        if pickup_aliases and resolution.warehouse.external_id not in pickup_aliases:
            return WarehouseResolution(
                None,
                "matched warehouse is outside pickup contour",
                resolution.matches,
            )
        return resolution
    if not pickup_aliases:
        return resolution
    normalized_text = _normalize_alias_text(text_value)
    if not normalized_text:
        return resolution
    external_ids = list(pickup_aliases)
    warehouses = {
        warehouse.external_id: warehouse
        for warehouse in session.scalars(
            select(LogisticsWarehouse)
            .where(
                LogisticsWarehouse.external_id.in_(external_ids),
                LogisticsWarehouse.is_active.is_(True),
                LogisticsWarehouse.kind.in_(["store", "retail"]),
            )
            .order_by(LogisticsWarehouse.id.asc())
        ).all()
    }
    matches: dict[int, dict[str, object]] = {}
    for external_id, aliases in pickup_aliases.items():
        warehouse = warehouses.get(external_id)
        if warehouse is None:
            continue
        for alias in aliases:
            normalized_alias = _normalize_alias_text(alias)
            if len(normalized_alias) >= 4 and normalized_alias in normalized_text:
                matches[warehouse.id] = {
                    "warehouse_id": warehouse.id,
                    "warehouse_external_id": warehouse.external_id,
                    "warehouse_name": warehouse.name,
                    "alias": alias,
                    "match_type": "pickup_chat_alias",
                }
                break
    if len(matches) == 1:
        warehouse_id = next(iter(matches))
        return WarehouseResolution(
            session.get(LogisticsWarehouse, warehouse_id),
            None,
            list(matches.values()),
        )
    if len(matches) > 1:
        return WarehouseResolution(
            None,
            "pickup chat text matched multiple warehouses",
            list(matches.values()),
        )
    return resolution


def reprocess_manual_inventory_submissions(
    session: Session,
    *,
    pickup_aliases: dict[str, list[str]],
    limit: int = 1000,
    now: datetime | None = None,
) -> dict[str, object]:
    """Append confirmed revisions when a newer parser resolves a manual row."""

    now = now or datetime.now(UTC).replace(tzinfo=None)
    rows = session.scalars(
        select(PickupInventorySubmission)
        .where(PickupInventorySubmission.status == STATUS_MANUAL_REVIEW)
        .order_by(
            PickupInventorySubmission.submitted_at.asc(),
            PickupInventorySubmission.id.asc(),
        )
        .limit(max(1, min(limit, 5000)))
    ).all()
    by_warehouse: dict[str, int] = {}
    stats: dict[str, object] = {
        "checked": 0,
        "confirmed": 0,
        "unresolved": 0,
        "carry_without_previous": 0,
        "by_warehouse": by_warehouse,
    }
    for submission in rows:
        stats["checked"] = int(stats["checked"]) + 1
        message = submission.source_message
        if message is None:
            stats["unresolved"] = int(stats["unresolved"]) + 1
            continue
        text_value = fulfillment.bitrix_message_text_for_parsing(message)
        parsed = parse_inventory_text(text_value)
        resolution = resolve_pickup_inventory_warehouse(
            session,
            text_value,
            pickup_aliases=pickup_aliases,
        )
        warehouse = resolution.warehouse
        if not parsed.explicit or warehouse is None:
            stats["unresolved"] = int(stats["unresolved"]) + 1
            continue
        latest = latest_confirmed_submission(
            session,
            warehouse_id=warehouse.id,
            before_at=submission.submitted_at,
        )
        if parsed.mode == MODE_CARRY:
            if latest is None:
                stats["carry_without_previous"] = int(stats["carry_without_previous"]) + 1
                continue
            order_numbers = [item.site_order_number for item in latest.items]
        else:
            order_numbers = list(parsed.order_numbers)
        revision = (
            int(
                session.scalar(
                    select(func.max(PickupInventorySubmission.revision)).where(
                        PickupInventorySubmission.run_id == submission.run_id,
                        PickupInventorySubmission.warehouse_id == warehouse.id,
                    )
                )
                or 0
            )
            + 1
        )
        reparsed = PickupInventorySubmission(
            run_id=submission.run_id,
            warehouse_id=warehouse.id,
            source_message_id=submission.source_message_id,
            supersedes_submission_id=(latest.id if latest is not None else submission.id),
            author_id=submission.author_id,
            mode=parsed.mode,
            status=STATUS_CONFIRMED,
            revision=revision,
            submitted_at=submission.submitted_at,
            confirmed_at=submission.submitted_at,
            parser_version=PARSER_VERSION,
            payload={
                "automatically_reparsed_from_submission_id": submission.id,
                "automatically_reparsed_at": now.isoformat(),
                "pickup_resolution_reason": resolution.reason,
                "pickup_matches": resolution.matches,
            },
        )
        session.add(reparsed)
        session.flush()
        _add_inventory_items(session, submission=reparsed, order_numbers=order_numbers)
        submission.status = "superseded"
        submission.payload = {
            **(submission.payload or {}),
            "automatically_reparsed_submission_id": reparsed.id,
            "automatically_reparsed_at": now.isoformat(),
        }
        stats["confirmed"] = int(stats["confirmed"]) + 1
        by_warehouse[warehouse.external_id] = by_warehouse.get(warehouse.external_id, 0) + 1
    session.flush()
    return stats


def _normalize_alias_text(value: str) -> str:
    return " ".join(fulfillment._clean_text(value).replace("ё", "е").split())  # noqa: SLF001


def create_clarified_submission(
    session: Session,
    *,
    submission: PickupInventorySubmission,
    warehouse_id: int,
    mode: str,
    actor_id: str,
    now: datetime,
) -> PickupInventorySubmission:
    """Append a confirmed revision created from an explicit clarification click."""

    if mode not in {MODE_FULL, MODE_CARRY, MODE_ZERO, MODE_CORRECTION}:
        raise ValueError("unsupported_inventory_mode")
    source_message = submission.source_message
    if source_message is None:
        raise ValueError("inventory_source_message_missing")
    parsed = parse_inventory_text(fulfillment.bitrix_message_text_for_parsing(source_message))
    latest = latest_confirmed_submission(
        session,
        warehouse_id=warehouse_id,
        before_at=submission.submitted_at,
    )
    if mode == MODE_CARRY:
        if latest is None:
            raise ValueError("inventory_previous_state_missing")
        order_numbers = [item.site_order_number for item in latest.items]
    elif mode == MODE_ZERO:
        order_numbers = []
    else:
        if not parsed.order_numbers or parsed.ambiguous_tokens:
            raise ValueError("inventory_order_numbers_ambiguous")
        order_numbers = list(parsed.order_numbers)
        if mode == MODE_FULL and parsed.mode == MODE_CORRECTION:
            mode = MODE_CORRECTION

    revision = (
        int(
            session.scalar(
                select(func.max(PickupInventorySubmission.revision)).where(
                    PickupInventorySubmission.run_id == submission.run_id,
                    PickupInventorySubmission.warehouse_id == warehouse_id,
                )
            )
            or 0
        )
        + 1
    )
    clarified = PickupInventorySubmission(
        run_id=submission.run_id,
        warehouse_id=warehouse_id,
        source_message_id=submission.source_message_id,
        supersedes_submission_id=(latest.id if latest is not None else submission.id),
        author_id=submission.author_id,
        mode=mode,
        status=STATUS_CONFIRMED,
        revision=revision,
        submitted_at=submission.submitted_at,
        confirmed_at=now,
        parser_version=PARSER_VERSION,
        payload={
            "clarified_from_submission_id": submission.id,
            "clarified_by": actor_id,
            "clarified_at": now.isoformat(),
        },
    )
    session.add(clarified)
    session.flush()
    _add_inventory_items(session, submission=clarified, order_numbers=order_numbers)
    submission.status = "superseded"
    submission.payload = {
        **(submission.payload or {}),
        "clarified_submission_id": clarified.id,
        "clarified_by": actor_id,
        "clarified_at": now.isoformat(),
    }
    session.flush()
    return clarified


def create_point_selected_submission(
    session: Session,
    *,
    submission: PickupInventorySubmission,
    warehouse_id: int,
    actor_id: str,
    now: datetime,
) -> PickupInventorySubmission:
    """Append a manual revision after a user explicitly selects the point."""

    revision = (
        int(
            session.scalar(
                select(func.max(PickupInventorySubmission.revision)).where(
                    PickupInventorySubmission.run_id == submission.run_id,
                    PickupInventorySubmission.warehouse_id == warehouse_id,
                )
            )
            or 0
        )
        + 1
    )
    selected = PickupInventorySubmission(
        run_id=submission.run_id,
        warehouse_id=warehouse_id,
        source_message_id=submission.source_message_id,
        supersedes_submission_id=submission.id,
        author_id=submission.author_id,
        mode=submission.mode,
        status=STATUS_MANUAL_REVIEW,
        revision=revision,
        submitted_at=submission.submitted_at,
        confirmed_at=None,
        parser_version=PARSER_VERSION,
        payload={
            **(submission.payload or {}),
            "point_selected_from_submission_id": submission.id,
            "point_selected_by": actor_id,
            "point_selected_at": now.isoformat(),
        },
    )
    session.add(selected)
    session.flush()
    _add_inventory_items(
        session,
        submission=selected,
        order_numbers=(item.site_order_number for item in submission.items),
    )
    submission.status = "superseded"
    submission.payload = {
        **(submission.payload or {}),
        "point_selected_submission_id": selected.id,
    }
    session.flush()
    return selected


def _add_inventory_items(
    session: Session,
    *,
    submission: PickupInventorySubmission,
    order_numbers: Iterable[str],
) -> None:
    for order_number in dict.fromkeys(order_numbers):
        session.add(
            PickupInventoryItem(
                submission_id=submission.id,
                site_order_number=order_number,
                validation_status="valid",
                payload={},
            )
        )
    session.flush()


def disappearance_candidates(
    session: Session,
    *,
    current_submission: PickupInventorySubmission,
) -> list[InventoryDisappearance]:
    if current_submission.status != STATUS_CONFIRMED or current_submission.warehouse_id is None:
        return []
    previous = (
        session.get(PickupInventorySubmission, current_submission.supersedes_submission_id)
        if current_submission.supersedes_submission_id is not None
        else None
    )
    if previous is None or previous.status != STATUS_CONFIRMED:
        return []
    previous_orders = {item.site_order_number for item in previous.items}
    current_orders = {item.site_order_number for item in current_submission.items}
    return [
        InventoryDisappearance(
            site_order_number=order_number,
            warehouse_id=current_submission.warehouse_id,
            previous_submission_id=previous.id,
            current_submission_id=current_submission.id,
            previous_at=previous.submitted_at,
            current_at=current_submission.submitted_at,
        )
        for order_number in sorted(previous_orders - current_orders)
    ]


def disappearance_is_uncontested(
    session: Session,
    *,
    candidate: InventoryDisappearance,
) -> tuple[bool, str]:
    newer_submission = aliased(PickupInventorySubmission)
    latest_presence = session.scalar(
        select(PickupInventoryItem.id)
        .join(
            PickupInventorySubmission,
            PickupInventorySubmission.id == PickupInventoryItem.submission_id,
        )
        .where(
            PickupInventorySubmission.status == STATUS_CONFIRMED,
            PickupInventoryItem.site_order_number == candidate.site_order_number,
            ~exists(
                select(newer_submission.id).where(
                    newer_submission.warehouse_id == PickupInventorySubmission.warehouse_id,
                    newer_submission.status == STATUS_CONFIRMED,
                    or_(
                        newer_submission.submitted_at > PickupInventorySubmission.submitted_at,
                        and_(
                            newer_submission.submitted_at == PickupInventorySubmission.submitted_at,
                            newer_submission.id > PickupInventorySubmission.id,
                        ),
                    ),
                )
            ),
        )
        .limit(1)
    )
    if latest_presence is not None:
        return False, "present_in_newer_inventory"
    blocking_event = session.scalar(
        select(SiteOrderExecutionEvent.id)
        .where(
            SiteOrderExecutionEvent.event_at > candidate.previous_at,
            SiteOrderExecutionEvent.event_type.in_(BLOCKING_EVENT_TYPES),
            SiteOrderExecutionEvent.case_id
            == select(fulfillment.SiteOrderExecutionCase.id)
            .where(
                fulfillment.SiteOrderExecutionCase.site_order_number == candidate.site_order_number
            )
            .scalar_subquery(),
        )
        .limit(1)
    )
    if blocking_event is not None:
        return False, "newer_blocking_event"
    return True, "confirmed_disappearance"


def latest_confirmed_submission(
    session: Session,
    *,
    warehouse_id: int,
    before_at: datetime | None = None,
) -> PickupInventorySubmission | None:
    query = select(PickupInventorySubmission).where(
        PickupInventorySubmission.warehouse_id == warehouse_id,
        PickupInventorySubmission.status == STATUS_CONFIRMED,
    )
    if before_at is not None:
        query = query.where(PickupInventorySubmission.submitted_at <= before_at)
    return session.scalar(
        query.order_by(
            PickupInventorySubmission.submitted_at.desc(),
            PickupInventorySubmission.id.desc(),
        ).limit(1)
    )


def _moscow_date(value: datetime) -> date:
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return aware.astimezone(ZoneInfo("Europe/Moscow")).date()
