"""Append-only point-in-time signals for procurement and cold-start replay."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.assortment_lifecycle_signal import AssortmentLifecycleSignal

ASSORTMENT_SIGNAL_SCHEMA_VERSION = "assortment_signal.v1"
SUPPORTED_SIGNAL_TYPES = frozenset(
    {
        "customer_sale",
        "stock_availability",
        "supplier_order",
        "supplier_receipt",
        "cargo",
        "kmp4",
        "site_order",
        "site_cart",
        "wordstat_direction",
    }
)
ALLOWED_DIRECTIONS = frozenset({"up", "down", "flat", "unknown"})


class AssortmentLifecycleSignalError(ValueError):
    """Invalid or conflicting point-in-time signal."""


class AssortmentLifecycleSignalConflict(AssortmentLifecycleSignalError):
    """The source identity was already stored with different content."""


@dataclass(frozen=True)
class AssortmentLifecycleSignalInput:
    signal_type: str
    source: str
    source_event_id: str
    occurred_at: datetime
    available_at: datetime
    reliability: Decimal | float | int | str
    reliability_reason: str
    nomenclature_code: str | None = None
    display_family_key: str | None = None
    display_family_registry_version: int | None = None
    quantity: Decimal | float | int | str | None = None
    direction: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PreparedAssortmentLifecycleSignal:
    signal_key: str
    payload_hash: str
    values: Mapping[str, Any]


@dataclass(frozen=True)
class AssortmentLifecycleSignalAppendResult:
    signal: AssortmentLifecycleSignal
    created: bool


def _clean(value: object | None) -> str:
    return " ".join(str(value or "").strip().split())


def _utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise AssortmentLifecycleSignalError(f"{field_name}_must_be_datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise AssortmentLifecycleSignalError(f"{field_name}_must_be_timezone_aware")
    return value.astimezone(UTC)


def _decimal(
    value: Decimal | float | int | str,
    field_name: str,
) -> Decimal:
    if isinstance(value, bool):
        raise AssortmentLifecycleSignalError(f"{field_name}_must_be_decimal")
    try:
        normalized = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise AssortmentLifecycleSignalError(f"{field_name}_must_be_decimal") from exc
    if not normalized.is_finite():
        raise AssortmentLifecycleSignalError(f"{field_name}_must_be_finite")
    return normalized


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise AssortmentLifecycleSignalError("payload_contains_non_finite_number")
        return format(Decimal(str(value)).normalize(), "f")
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise AssortmentLifecycleSignalError("payload_contains_non_finite_number")
        return format(value.normalize(), "f")
    if isinstance(value, datetime):
        return _utc(value, "payload_datetime").isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {
            str(key): _json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_value(item) for item in value]
    raise AssortmentLifecycleSignalError(
        f"payload_contains_unsupported_type:{type(value).__name__}"
    )


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def prepare_assortment_lifecycle_signal(
    signal: AssortmentLifecycleSignalInput,
) -> PreparedAssortmentLifecycleSignal:
    signal_type = _clean(signal.signal_type).casefold()
    source = _clean(signal.source).casefold()
    source_event_id = _clean(signal.source_event_id)
    nomenclature_code = _clean(signal.nomenclature_code) or None
    display_family_key = _clean(signal.display_family_key) or None
    reliability_reason = _clean(signal.reliability_reason)
    direction = _clean(signal.direction).casefold() or None
    occurred_at = _utc(signal.occurred_at, "occurred_at")
    available_at = _utc(signal.available_at, "available_at")
    reliability = _decimal(signal.reliability, "reliability")
    quantity = _decimal(signal.quantity, "quantity") if signal.quantity is not None else None
    registry_version = signal.display_family_registry_version

    errors: list[str] = []
    if signal_type not in SUPPORTED_SIGNAL_TYPES:
        errors.append("unsupported_signal_type")
    if len(signal_type) > 64:
        errors.append("signal_type_too_long")
    if not source:
        errors.append("source_required")
    if not source_event_id:
        errors.append("source_event_id_required")
    if len(source) > 64:
        errors.append("source_too_long")
    if len(source_event_id) > 255:
        errors.append("source_event_id_too_long")
    if not nomenclature_code and not display_family_key:
        errors.append("sku_or_family_link_required")
    if nomenclature_code and len(nomenclature_code) > 64:
        errors.append("nomenclature_code_too_long")
    if display_family_key and len(display_family_key) > 80:
        errors.append("display_family_key_too_long")
    if display_family_key and registry_version is None:
        errors.append("display_family_registry_version_required")
    if not display_family_key and registry_version is not None:
        errors.append("display_family_key_required_for_registry_version")
    if registry_version is not None:
        if isinstance(registry_version, bool) or not isinstance(registry_version, int):
            errors.append("display_family_registry_version_must_be_integer")
        elif registry_version <= 0:
            errors.append("display_family_registry_version_must_be_positive")
    if available_at < occurred_at:
        errors.append("available_at_before_occurred_at")
    if reliability < 0 or reliability > 1:
        errors.append("reliability_out_of_range")
    if not reliability_reason:
        errors.append("reliability_reason_required")
    if len(reliability_reason) > 255:
        errors.append("reliability_reason_too_long")
    if quantity is not None and quantity < 0:
        errors.append("quantity_must_be_non_negative")
    if direction is not None and direction not in ALLOWED_DIRECTIONS:
        errors.append("unsupported_direction")
    if signal_type == "wordstat_direction":
        if quantity is not None:
            errors.append("wordstat_quantity_forbidden")
        if direction is None:
            errors.append("wordstat_direction_required")
    if not isinstance(signal.payload, Mapping):
        errors.append("payload_must_be_mapping")
    if errors:
        raise AssortmentLifecycleSignalError(";".join(sorted(set(errors))))

    payload = _json_value(signal.payload)
    identity = {
        "schema_version": ASSORTMENT_SIGNAL_SCHEMA_VERSION,
        "signal_type": signal_type,
        "source": source,
        "source_event_id": source_event_id,
    }
    signal_content = {
        **identity,
        "occurred_at": occurred_at.isoformat(),
        "nomenclature_code": nomenclature_code,
        "display_family_key": display_family_key,
        "display_family_registry_version": registry_version,
    }
    values = {
        **signal_content,
        "available_at": available_at,
        "occurred_at": occurred_at,
        "reliability": reliability,
        "reliability_reason": reliability_reason,
        "quantity": quantity,
        "direction": direction,
        "payload": payload,
    }
    content = {
        **signal_content,
        "available_at": available_at.isoformat(),
        "reliability": format(reliability.normalize(), "f"),
        "reliability_reason": reliability_reason,
        "quantity": format(quantity.normalize(), "f") if quantity is not None else None,
        "direction": direction,
        "payload": payload,
    }
    signal_key = _sha256_text(_canonical_json(identity))
    payload_hash = _sha256_text(_canonical_json(content))
    return PreparedAssortmentLifecycleSignal(
        signal_key=signal_key,
        payload_hash=payload_hash,
        values={
            **values,
            "signal_key": signal_key,
            "payload_hash": payload_hash,
        },
    )


def append_assortment_lifecycle_signal(
    session: Session,
    signal: AssortmentLifecycleSignalInput,
) -> AssortmentLifecycleSignalAppendResult:
    """Append one signal or return the byte-equivalent existing event."""

    prepared = prepare_assortment_lifecycle_signal(signal)
    existing = session.scalar(
        select(AssortmentLifecycleSignal).where(
            AssortmentLifecycleSignal.signal_key == prepared.signal_key
        )
    )
    if existing is not None:
        if existing.payload_hash != prepared.payload_hash:
            raise AssortmentLifecycleSignalConflict("signal_identity_exists_with_different_payload")
        return AssortmentLifecycleSignalAppendResult(signal=existing, created=False)

    stored = AssortmentLifecycleSignal(**dict(prepared.values))
    try:
        with session.begin_nested():
            session.add(stored)
            session.flush()
    except IntegrityError as exc:
        concurrent = session.scalar(
            select(AssortmentLifecycleSignal).where(
                AssortmentLifecycleSignal.signal_key == prepared.signal_key
            )
        )
        if concurrent is None:
            raise AssortmentLifecycleSignalError("signal_persistence_failed") from exc
        if concurrent.payload_hash != prepared.payload_hash:
            raise AssortmentLifecycleSignalConflict(
                "signal_identity_exists_with_different_payload"
            ) from exc
        return AssortmentLifecycleSignalAppendResult(signal=concurrent, created=False)
    return AssortmentLifecycleSignalAppendResult(signal=stored, created=True)


def list_assortment_lifecycle_signals_as_of(
    session: Session,
    as_of: datetime,
    *,
    nomenclature_code: str | None = None,
    display_family_key: str | None = None,
    signal_types: Sequence[str] | None = None,
) -> list[AssortmentLifecycleSignal]:
    """Return only facts that were available to the system at the cutoff."""

    cutoff = _utc(as_of, "as_of")
    query = select(AssortmentLifecycleSignal).where(
        AssortmentLifecycleSignal.occurred_at <= cutoff,
        AssortmentLifecycleSignal.available_at <= cutoff,
    )
    code = _clean(nomenclature_code)
    family_key = _clean(display_family_key)
    if code:
        query = query.where(AssortmentLifecycleSignal.nomenclature_code == code)
    if family_key:
        query = query.where(AssortmentLifecycleSignal.display_family_key == family_key)
    if signal_types is not None:
        normalized_types = sorted({_clean(value).casefold() for value in signal_types})
        unsupported = sorted(set(normalized_types) - SUPPORTED_SIGNAL_TYPES)
        if unsupported:
            raise AssortmentLifecycleSignalError("unsupported_signal_type:" + ",".join(unsupported))
        if not normalized_types:
            return []
        query = query.where(AssortmentLifecycleSignal.signal_type.in_(normalized_types))
    return list(
        session.scalars(
            query.order_by(
                AssortmentLifecycleSignal.available_at,
                AssortmentLifecycleSignal.occurred_at,
                AssortmentLifecycleSignal.id,
            )
        ).all()
    )


def assortment_lifecycle_signal_snapshot(
    signal: AssortmentLifecycleSignal,
) -> dict[str, Any]:
    return {
        "schema_version": signal.schema_version,
        "signal_key": signal.signal_key,
        "signal_type": signal.signal_type,
        "source": signal.source,
        "source_event_id": signal.source_event_id,
        "occurred_at": signal.occurred_at.isoformat(),
        "available_at": signal.available_at.isoformat(),
        "nomenclature_code": signal.nomenclature_code,
        "display_family_key": signal.display_family_key,
        "display_family_registry_version": signal.display_family_registry_version,
        "reliability": format(Decimal(signal.reliability).normalize(), "f"),
        "reliability_reason": signal.reliability_reason,
        "quantity": (
            format(Decimal(signal.quantity).normalize(), "f")
            if signal.quantity is not None
            else None
        ),
        "direction": signal.direction,
        "payload": signal.payload,
        "payload_hash": signal.payload_hash,
        "ingested_at": signal.ingested_at.isoformat() if signal.ingested_at else None,
    }
