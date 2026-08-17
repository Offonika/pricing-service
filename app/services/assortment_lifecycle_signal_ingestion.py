"""Deterministic dry-run ingestion for the first procurement signal wave.

The module prepares signal-store rows, but deliberately has no persistence
entrypoint.  Its output is an audit artifact: scope exclusions, registry
resolution, quarantine, source-identity conflicts and point-in-time projection.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.display_family_registry import (
    DisplayFamily,
    DisplayFamilyMember,
    DisplayFamilyRegistryVersion,
)
from app.models.product import Product
from app.services.assortment_lifecycle_signals import (
    ASSORTMENT_SIGNAL_SCHEMA_VERSION,
    AssortmentLifecycleSignalError,
    AssortmentLifecycleSignalInput,
    PreparedAssortmentLifecycleSignal,
    prepare_assortment_lifecycle_signal,
)
from app.services.display_scope_policy import (
    display_scope_exclusion_reason,
    display_scope_record_code,
    display_scope_record_name,
    filter_display_scope_records,
)

SIGNAL_INGESTION_SOURCE_SCHEMA = "assortment_signal_source_bundle.v1"
SIGNAL_INGESTION_ARTIFACT_SCHEMA = "assortment_signal_ingestion_dry_run.v1"
DISPLAY_FAMILY_REGISTRY_SNAPSHOT_SCHEMA = "display_family_registry_snapshot.v1"
FIRST_WAVE_SIGNAL_TYPES = frozenset(
    {
        "customer_sale",
        "stock_availability",
        "supplier_order",
        "supplier_receipt",
        "cargo",
    }
)

_ROW_ALIAS_FIELDS = (
    "nomenclature_code",
    "code",
    "_Code",
    "code_1c",
    "article",
    "fact_sku",
    "info_system_code",
    "sku",
)
_MEMBER_ALIAS_FIELDS = (
    "nomenclature_code",
    "code_1c",
    "article",
    "fact_sku",
    "info_system_code",
)


class AssortmentLifecycleSignalIngestionError(ValueError):
    """The dry-run cannot produce a trustworthy reconciliation artifact."""


@dataclass(frozen=True)
class DisplayFamilyRegistryMemberSnapshot:
    product_id: int
    family_key: str | None
    nomenclature_code: str
    aliases: tuple[str, ...]
    name: str


@dataclass(frozen=True)
class DisplayFamilyRegistrySnapshot:
    version_number: int
    status: str
    members: tuple[DisplayFamilyRegistryMemberSnapshot, ...]
    source: str


@dataclass(frozen=True)
class _PreparedRow:
    source_row_number: int
    quantity: Decimal
    prepared: PreparedAssortmentLifecycleSignal


def _clean(value: object | None) -> str:
    return " ".join(str(value or "").strip().split())


def _normalized_alias(value: object | None) -> str:
    return _clean(value).casefold()


def _decimal(value: object | None) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        normalized = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not normalized.is_finite():
        return None
    return normalized


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _datetime(value: object, field_name: str) -> datetime:
    parsed: datetime
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        source = value.strip()
        if source.endswith("Z"):
            source = source[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(source)
        except ValueError as exc:
            raise AssortmentLifecycleSignalIngestionError(
                f"{field_name}_must_be_iso_datetime"
            ) from exc
    else:
        raise AssortmentLifecycleSignalIngestionError(f"{field_name}_must_be_datetime")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AssortmentLifecycleSignalIngestionError(f"{field_name}_must_be_timezone_aware")
    return parsed.astimezone(UTC)


def _member_aliases(raw: Mapping[str, Any]) -> tuple[str, ...]:
    aliases: list[str] = []
    for field_name in _MEMBER_ALIAS_FIELDS:
        value = _clean(raw.get(field_name))
        if value:
            aliases.append(value)
    extra = raw.get("aliases") or ()
    if not isinstance(extra, Sequence) or isinstance(extra, (str, bytes, bytearray)):
        raise AssortmentLifecycleSignalIngestionError("registry_member_aliases_must_be_list")
    aliases.extend(_clean(value) for value in extra if _clean(value))
    unique: dict[str, str] = {}
    for alias in aliases:
        unique.setdefault(alias.casefold(), alias)
    return tuple(unique[key] for key in sorted(unique))


def display_family_registry_snapshot_from_mapping(
    payload: Mapping[str, Any],
    *,
    source: str = "json_fixture",
) -> DisplayFamilyRegistrySnapshot:
    """Validate a portable read-only registry snapshot used by the dry-run CLI."""

    schema = _clean(payload.get("schema"))
    if schema != DISPLAY_FAMILY_REGISTRY_SNAPSHOT_SCHEMA:
        raise AssortmentLifecycleSignalIngestionError(
            f"unsupported_family_registry_snapshot_schema:{schema or 'missing'}"
        )
    version_number = payload.get("version_number")
    if isinstance(version_number, bool) or not isinstance(version_number, int):
        raise AssortmentLifecycleSignalIngestionError(
            "family_registry_version_must_be_positive_integer"
        )
    status = _clean(payload.get("status")).casefold()
    raw_members = payload.get("members")
    if not isinstance(raw_members, Sequence) or isinstance(raw_members, (str, bytes, bytearray)):
        raise AssortmentLifecycleSignalIngestionError("family_registry_members_must_be_list")

    members: list[DisplayFamilyRegistryMemberSnapshot] = []
    for index, raw_member in enumerate(raw_members, start=1):
        if not isinstance(raw_member, Mapping):
            raise AssortmentLifecycleSignalIngestionError(
                f"family_registry_member_must_be_object:{index}"
            )
        product_id = raw_member.get("product_id")
        if isinstance(product_id, bool) or not isinstance(product_id, int) or product_id <= 0:
            raise AssortmentLifecycleSignalIngestionError(
                f"family_registry_product_id_must_be_positive_integer:{index}"
            )
        aliases = _member_aliases(raw_member)
        if not aliases:
            raise AssortmentLifecycleSignalIngestionError(
                f"family_registry_member_alias_required:{product_id}"
            )
        canonical_code = _clean(raw_member.get("nomenclature_code")) or aliases[0]
        family_key = _clean(raw_member.get("family_key")) or None
        members.append(
            DisplayFamilyRegistryMemberSnapshot(
                product_id=product_id,
                family_key=family_key,
                nomenclature_code=canonical_code,
                aliases=aliases,
                name=_clean(raw_member.get("name")),
            )
        )

    snapshot = DisplayFamilyRegistrySnapshot(
        version_number=version_number,
        status=status,
        members=tuple(sorted(members, key=lambda member: member.product_id)),
        source=_clean(source) or "unknown",
    )
    _validate_registry_snapshot(snapshot)
    return snapshot


def load_active_display_family_registry_snapshot(
    session: Session,
) -> DisplayFamilyRegistrySnapshot:
    """Load the active application registry without changing database state."""

    versions = list(
        session.scalars(
            select(DisplayFamilyRegistryVersion).where(
                DisplayFamilyRegistryVersion.status == "active"
            )
        ).all()
    )
    if not versions:
        raise AssortmentLifecycleSignalIngestionError("active_family_registry_missing")
    if len(versions) != 1:
        raise AssortmentLifecycleSignalIngestionError("multiple_active_family_registries")
    version = versions[0]
    rows = session.execute(
        select(
            DisplayFamilyMember.product_id,
            DisplayFamily.family_key,
            Product.code_1c,
            Product.article,
            Product.fact_sku,
            Product.info_system_code,
            Product.name,
        )
        .join(DisplayFamily, DisplayFamily.id == DisplayFamilyMember.family_id)
        .join(Product, Product.id == DisplayFamilyMember.product_id)
        .where(
            DisplayFamilyMember.registry_version_id == version.id,
            DisplayFamily.registry_version_id == version.id,
        )
        .order_by(DisplayFamilyMember.product_id)
    ).all()
    members = tuple(
        DisplayFamilyRegistryMemberSnapshot(
            product_id=int(row.product_id),
            family_key=_clean(row.family_key) or None,
            nomenclature_code=(
                _clean(row.code_1c)
                or _clean(row.article)
                or _clean(row.fact_sku)
                or _clean(row.info_system_code)
            ),
            aliases=tuple(
                dict.fromkeys(
                    value
                    for value in (
                        _clean(row.code_1c),
                        _clean(row.article),
                        _clean(row.fact_sku),
                        _clean(row.info_system_code),
                    )
                    if value
                )
            ),
            name=_clean(row.name),
        )
        for row in rows
    )
    snapshot = DisplayFamilyRegistrySnapshot(
        version_number=int(version.version_number),
        status=_clean(version.status).casefold(),
        members=members,
        source="application_active_registry",
    )
    _validate_registry_snapshot(snapshot)
    return snapshot


def display_family_registry_snapshot_as_mapping(
    snapshot: DisplayFamilyRegistrySnapshot,
) -> dict[str, Any]:
    """Serialize the portable registry contract without ORM state."""

    _validate_registry_snapshot(snapshot)
    return {
        "schema": DISPLAY_FAMILY_REGISTRY_SNAPSHOT_SCHEMA,
        "version_number": snapshot.version_number,
        "status": snapshot.status,
        "source": snapshot.source,
        "members": [
            {
                "product_id": member.product_id,
                "family_key": member.family_key,
                "nomenclature_code": member.nomenclature_code,
                "aliases": list(member.aliases),
                "name": member.name,
            }
            for member in snapshot.members
        ],
    }


def _validate_registry_snapshot(snapshot: DisplayFamilyRegistrySnapshot | None) -> None:
    if snapshot is None:
        raise AssortmentLifecycleSignalIngestionError("active_family_registry_missing")
    if snapshot.status != "active":
        raise AssortmentLifecycleSignalIngestionError(
            f"family_registry_not_active:{snapshot.status or 'missing'}"
        )
    if isinstance(snapshot.version_number, bool) or snapshot.version_number <= 0:
        raise AssortmentLifecycleSignalIngestionError(
            "family_registry_version_must_be_positive_integer"
        )
    if not snapshot.members:
        raise AssortmentLifecycleSignalIngestionError("active_family_registry_has_no_members")
    product_ids = [member.product_id for member in snapshot.members]
    if len(product_ids) != len(set(product_ids)):
        raise AssortmentLifecycleSignalIngestionError(
            "active_family_registry_has_duplicate_product_members"
        )
    for member in snapshot.members:
        if member.product_id <= 0:
            raise AssortmentLifecycleSignalIngestionError(
                "family_registry_product_id_must_be_positive_integer"
            )
        if not member.nomenclature_code or not member.aliases:
            raise AssortmentLifecycleSignalIngestionError(
                f"family_registry_member_alias_required:{member.product_id}"
            )


def _registry_alias_index(
    snapshot: DisplayFamilyRegistrySnapshot,
) -> dict[str, tuple[DisplayFamilyRegistryMemberSnapshot, ...]]:
    mutable: dict[str, dict[int, DisplayFamilyRegistryMemberSnapshot]] = defaultdict(dict)
    for member in snapshot.members:
        for alias in member.aliases:
            normalized = _normalized_alias(alias)
            if normalized:
                mutable[normalized].setdefault(member.product_id, member)
    return {
        alias: tuple(products[product_id] for product_id in sorted(products))
        for alias, products in mutable.items()
    }


def _row_aliases(row: Mapping[str, Any]) -> tuple[str, ...]:
    values: dict[str, str] = {}
    for field_name in _ROW_ALIAS_FIELDS:
        value = _clean(row.get(field_name))
        if value:
            values.setdefault(value.casefold(), value)
    return tuple(values[key] for key in sorted(values))


def _source_identity(row: Mapping[str, Any]) -> tuple[str, str, str] | None:
    signal_type = _clean(row.get("signal_type")).casefold()
    source = _clean(row.get("source")).casefold()
    source_event_id = _clean(row.get("source_event_id"))
    if not signal_type or not source or not source_event_id:
        return None
    return signal_type, source, source_event_id


def _source_identity_key(identity: tuple[str, str, str]) -> str:
    signal_type, source, source_event_id = identity
    payload = {
        "schema_version": ASSORTMENT_SIGNAL_SCHEMA_VERSION,
        "signal_type": signal_type,
        "source": source,
        "source_event_id": source_event_id,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _raw_row_hash(row: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        row,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _resolve_registry_member(
    row: Mapping[str, Any],
    alias_index: Mapping[str, tuple[DisplayFamilyRegistryMemberSnapshot, ...]],
) -> tuple[DisplayFamilyRegistryMemberSnapshot | None, tuple[str, ...]]:
    aliases = _row_aliases(row)
    if not aliases:
        return None, ("nomenclature_code_required",)
    matched: dict[int, DisplayFamilyRegistryMemberSnapshot] = {}
    for alias in aliases:
        for member in alias_index.get(alias.casefold(), ()):
            matched.setdefault(member.product_id, member)
    if not matched:
        return None, ("sku_not_in_active_family_registry",)
    if len(matched) != 1:
        return None, ("ambiguous_sku_in_active_family_registry",)
    member = next(iter(matched.values()))
    if not member.family_key:
        return None, ("family_linkage_missing",)
    return member, ()


def _quarantine_row(
    row: Mapping[str, Any],
    *,
    source_row_number: int,
    reason_codes: Sequence[str],
) -> dict[str, Any]:
    return {
        "source_row_number": source_row_number,
        "signal_type": _clean(row.get("signal_type")).casefold(),
        "source": _clean(row.get("source")).casefold(),
        "source_event_id": _clean(row.get("source_event_id")),
        "nomenclature_code": display_scope_record_code(row),
        "name": display_scope_record_name(row),
        "reason_codes": sorted({_clean(reason) for reason in reason_codes if _clean(reason)}),
    }


def _signal_mapping(prepared: PreparedAssortmentLifecycleSignal) -> dict[str, Any]:
    values = prepared.values
    quantity = values.get("quantity")
    reliability = values["reliability"]
    return {
        "schema_version": ASSORTMENT_SIGNAL_SCHEMA_VERSION,
        "signal_key": prepared.signal_key,
        "payload_hash": prepared.payload_hash,
        "signal_type": values["signal_type"],
        "source": values["source"],
        "source_event_id": values["source_event_id"],
        "occurred_at": values["occurred_at"].isoformat(),
        "available_at": values["available_at"].isoformat(),
        "nomenclature_code": values["nomenclature_code"],
        "display_family_key": values["display_family_key"],
        "display_family_registry_version": values["display_family_registry_version"],
        "reliability": _decimal_text(reliability),
        "reliability_reason": values["reliability_reason"],
        "quantity": _decimal_text(quantity) if quantity is not None else None,
        "direction": values["direction"],
        "payload": values["payload"],
    }


def _sum_outcome_quantities(
    records: Sequence[Mapping[str, Any]],
    outcomes: Mapping[int, str],
) -> tuple[dict[str, Decimal], tuple[int, ...]]:
    totals: dict[str, Decimal] = defaultdict(Decimal)
    parse_errors: list[int] = []
    for source_row_number, row in enumerate(records, start=1):
        quantity = _decimal(row.get("quantity"))
        if quantity is None:
            parse_errors.append(source_row_number)
            continue
        totals["input_numeric"] += quantity
        outcome = outcomes.get(source_row_number)
        if outcome is None:
            raise AssortmentLifecycleSignalIngestionError(
                f"internal_row_outcome_missing:{source_row_number}"
            )
        totals[outcome] += quantity
    return totals, tuple(parse_errors)


def build_assortment_signal_ingestion_dry_run(
    source_bundle: Mapping[str, Any],
    registry_snapshot: DisplayFamilyRegistrySnapshot | None,
    *,
    as_of: datetime | str | None = None,
) -> dict[str, Any]:
    """Prepare and reconcile first-wave signals without writing any state."""

    schema = _clean(source_bundle.get("schema"))
    if schema != SIGNAL_INGESTION_SOURCE_SCHEMA:
        raise AssortmentLifecycleSignalIngestionError(
            f"unsupported_signal_source_schema:{schema or 'missing'}"
        )
    raw_items = source_bundle.get("items")
    if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes, bytearray)):
        raise AssortmentLifecycleSignalIngestionError("signal_source_items_must_be_list")
    records: list[dict[str, Any]] = []
    for index, raw_item in enumerate(raw_items, start=1):
        if not isinstance(raw_item, Mapping):
            raise AssortmentLifecycleSignalIngestionError(
                f"signal_source_item_must_be_object:{index}"
            )
        records.append(dict(raw_item))

    _validate_registry_snapshot(registry_snapshot)
    assert registry_snapshot is not None
    cutoff_source = as_of if as_of is not None else source_bundle.get("as_of")
    if cutoff_source is None:
        raise AssortmentLifecycleSignalIngestionError("as_of_required")
    cutoff = _datetime(cutoff_source, "as_of")
    alias_index = _registry_alias_index(registry_snapshot)

    indexed_records = [
        {**row, "__source_row_number": source_row_number}
        for source_row_number, row in enumerate(records, start=1)
    ]
    scope_result = filter_display_scope_records(indexed_records)
    outcomes: dict[int, str] = {}
    for row in indexed_records:
        if display_scope_exclusion_reason(display_scope_record_name(row)) is not None:
            outcomes[int(row["__source_row_number"])] = "scope_excluded"

    quarantine: list[dict[str, Any]] = []
    prepared_rows: list[_PreparedRow] = []
    for indexed_row in scope_result.included:
        row = dict(indexed_row)
        source_row_number = int(row.pop("__source_row_number"))
        reason_codes: list[str] = []
        signal_type = _clean(row.get("signal_type")).casefold()
        if signal_type not in FIRST_WAVE_SIGNAL_TYPES:
            reason_codes.append("unsupported_first_wave_signal_type")
        if not display_scope_record_name(row):
            reason_codes.append("display_name_required_for_scope")
        quantity = _decimal(row.get("quantity"))
        if quantity is None:
            reason_codes.append("quantity_required_and_must_be_decimal")
        elif quantity < 0:
            reason_codes.append("quantity_must_be_non_negative")

        member, registry_reasons = _resolve_registry_member(row, alias_index)
        reason_codes.extend(registry_reasons)
        if reason_codes:
            outcomes[source_row_number] = "quarantined"
            quarantine.append(
                _quarantine_row(
                    row,
                    source_row_number=source_row_number,
                    reason_codes=reason_codes,
                )
            )
            continue
        assert quantity is not None
        assert member is not None

        raw_payload = row.get("payload", {})
        payload = {} if raw_payload is None else raw_payload
        if not isinstance(payload, Mapping):
            outcomes[source_row_number] = "quarantined"
            quarantine.append(
                _quarantine_row(
                    row,
                    source_row_number=source_row_number,
                    reason_codes=("payload_must_be_mapping",),
                )
            )
            continue
        normalized_payload = dict(payload)
        normalized_payload.setdefault("display_name", display_scope_record_name(row))
        source_aliases = _row_aliases(row)
        normalized_payload.setdefault(
            "source_nomenclature_code",
            display_scope_record_code(row) or (source_aliases[0] if source_aliases else ""),
        )
        normalized_payload.setdefault("registry_product_id", member.product_id)
        try:
            prepared = prepare_assortment_lifecycle_signal(
                AssortmentLifecycleSignalInput(
                    signal_type=signal_type,
                    source=_clean(row.get("source")),
                    source_event_id=_clean(row.get("source_event_id")),
                    occurred_at=_datetime(row.get("occurred_at"), "occurred_at"),
                    available_at=_datetime(row.get("available_at"), "available_at"),
                    reliability=row.get("reliability"),
                    reliability_reason=_clean(row.get("reliability_reason")),
                    nomenclature_code=member.nomenclature_code,
                    display_family_key=member.family_key,
                    display_family_registry_version=registry_snapshot.version_number,
                    quantity=quantity,
                    payload=normalized_payload,
                )
            )
        except (AssortmentLifecycleSignalError, AssortmentLifecycleSignalIngestionError) as exc:
            outcomes[source_row_number] = "quarantined"
            quarantine.append(
                _quarantine_row(
                    row,
                    source_row_number=source_row_number,
                    reason_codes=tuple(str(exc).split(";")),
                )
            )
            continue
        prepared_rows.append(
            _PreparedRow(
                source_row_number=source_row_number,
                quantity=quantity,
                prepared=prepared,
            )
        )

    by_identity: dict[str, list[_PreparedRow]] = defaultdict(list)
    for prepared_row in prepared_rows:
        by_identity[prepared_row.prepared.signal_key].append(prepared_row)
    quarantine_by_row_number = {int(row["source_row_number"]): row for row in quarantine}
    quarantined_rows_by_identity: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    for source_row_number in sorted(quarantine_by_row_number):
        identity = _source_identity(records[source_row_number - 1])
        if identity is not None:
            quarantined_rows_by_identity[identity].append(source_row_number)

    accepted_rows: list[_PreparedRow] = []
    exact_duplicates: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    identities_conflicted_with_prepared_rows: set[tuple[str, str, str]] = set()
    for signal_key, identity_rows in sorted(by_identity.items()):
        payload_hashes = {row.prepared.payload_hash for row in identity_rows}
        identity_values = identity_rows[0].prepared.values
        source_identity = (
            str(identity_values["signal_type"]),
            str(identity_values["source"]),
            str(identity_values["source_event_id"]),
        )
        invalid_identity_rows = quarantined_rows_by_identity.get(source_identity, [])
        if len(payload_hashes) > 1 or invalid_identity_rows:
            if invalid_identity_rows:
                identities_conflicted_with_prepared_rows.add(source_identity)
            for row in identity_rows:
                outcomes[row.source_row_number] = "conflicted"
            for source_row_number in invalid_identity_rows:
                outcomes[source_row_number] = "conflicted"
            conflicts.append(
                {
                    "signal_key": signal_key,
                    "signal_type": identity_values["signal_type"],
                    "source": identity_values["source"],
                    "source_event_id": identity_values["source_event_id"],
                    "source_row_numbers": sorted(
                        [
                            *(row.source_row_number for row in identity_rows),
                            *invalid_identity_rows,
                        ]
                    ),
                    "payload_hashes": sorted(payload_hashes),
                    "reason_code": "signal_identity_exists_with_different_payload",
                    "invalid_or_unresolved_rows": [
                        quarantine_by_row_number[source_row_number]
                        for source_row_number in invalid_identity_rows
                    ],
                }
            )
            continue
        ordered_rows = sorted(identity_rows, key=lambda row: row.source_row_number)
        accepted_rows.append(ordered_rows[0])
        outcomes[ordered_rows[0].source_row_number] = "prepared"
        for duplicate in ordered_rows[1:]:
            outcomes[duplicate.source_row_number] = "exact_duplicate"
            exact_duplicates.append(
                {
                    "signal_key": signal_key,
                    "payload_hash": duplicate.prepared.payload_hash,
                    "source_row_number": duplicate.source_row_number,
                    "duplicate_of_source_row_number": ordered_rows[0].source_row_number,
                }
            )

    for source_identity, source_row_numbers in sorted(quarantined_rows_by_identity.items()):
        if source_identity in identities_conflicted_with_prepared_rows:
            continue
        raw_hashes = {
            _raw_row_hash(records[source_row_number - 1])
            for source_row_number in source_row_numbers
        }
        if len(raw_hashes) <= 1:
            continue
        for source_row_number in source_row_numbers:
            outcomes[source_row_number] = "conflicted"
        signal_type, source, source_event_id = source_identity
        conflicts.append(
            {
                "signal_key": _source_identity_key(source_identity),
                "signal_type": signal_type,
                "source": source,
                "source_event_id": source_event_id,
                "source_row_numbers": sorted(source_row_numbers),
                "payload_hashes": [],
                "raw_content_hashes": sorted(raw_hashes),
                "reason_code": "signal_identity_exists_with_different_payload",
                "invalid_or_unresolved_rows": [
                    quarantine_by_row_number[source_row_number]
                    for source_row_number in source_row_numbers
                ],
            }
        )

    conflicts.sort(key=lambda row: (str(row["signal_key"]), row["source_row_numbers"]))
    quarantine = [
        row for row in quarantine if outcomes[int(row["source_row_number"])] == "quarantined"
    ]

    accepted_rows.sort(key=lambda row: row.source_row_number)
    prepared_signals = [
        {"source_row_number": row.source_row_number, **_signal_mapping(row.prepared)}
        for row in accepted_rows
    ]
    as_of_signals: list[dict[str, Any]] = []
    hidden_not_occurred = 0
    hidden_not_available = 0
    for row, signal in zip(accepted_rows, prepared_signals, strict=True):
        occurred_at = row.prepared.values["occurred_at"]
        available_at = row.prepared.values["available_at"]
        if occurred_at > cutoff:
            hidden_not_occurred += 1
            continue
        if available_at > cutoff:
            hidden_not_available += 1
            continue
        as_of_signals.append(signal)

    quantity_totals, quantity_parse_error_rows = _sum_outcome_quantities(records, outcomes)
    classified_quantity_total = sum(
        (
            quantity_totals[outcome]
            for outcome in (
                "scope_excluded",
                "prepared",
                "exact_duplicate",
                "conflicted",
                "quarantined",
            )
        ),
        Decimal("0"),
    )
    source_row_count = len(records)
    scope_included_row_count = len(scope_result.included)
    classified_included_row_count = (
        len(accepted_rows)
        + len(exact_duplicates)
        + sum(len(conflict["source_row_numbers"]) for conflict in conflicts)
        + len(quarantine)
    )
    row_equations = {
        "source_equals_scope_included_plus_excluded": source_row_count
        == scope_included_row_count + scope_result.excluded_row_count,
        "scope_included_equals_all_ingestion_outcomes": scope_included_row_count
        == classified_included_row_count,
    }
    quantity_equations = {
        "input_numeric_equals_all_outcome_numeric": quantity_totals["input_numeric"]
        == classified_quantity_total
    }
    if not all(row_equations.values()) or not all(quantity_equations.values()):
        raise AssortmentLifecycleSignalIngestionError("internal_reconciliation_failed")

    status = "ready"
    if conflicts:
        status = "blocked_conflicts"
    elif quarantine:
        status = "ready_with_quarantine"
    return {
        "schema": SIGNAL_INGESTION_ARTIFACT_SCHEMA,
        "status": status,
        "dry_run": True,
        "production_authorized": False,
        "persistence_performed": False,
        "external_writes": False,
        "signal_release_allowed": False,
        "as_of": cutoff.isoformat(),
        "source_bundle": {
            "schema": schema,
            "bundle_id": _clean(source_bundle.get("bundle_id")) or None,
        },
        "family_registry": {
            "schema": DISPLAY_FAMILY_REGISTRY_SNAPSHOT_SCHEMA,
            "version_number": registry_snapshot.version_number,
            "status": registry_snapshot.status,
            "source": registry_snapshot.source,
            "member_count": len(registry_snapshot.members),
            "alias_count": len(alias_index),
        },
        "scope": scope_result.audit,
        "prepared_signals": prepared_signals,
        "as_of_projection": {
            "signal_count": len(as_of_signals),
            "hidden_not_occurred_count": hidden_not_occurred,
            "hidden_not_available_count": hidden_not_available,
            "signals": as_of_signals,
        },
        "exact_duplicates": exact_duplicates,
        "conflicts": conflicts,
        "quarantine": quarantine,
        "reconciliation": {
            "rows": {
                "source": source_row_count,
                "scope_included": scope_included_row_count,
                "scope_excluded": scope_result.excluded_row_count,
                "prepared": len(accepted_rows),
                "exact_duplicate": len(exact_duplicates),
                "conflicted": sum(len(conflict["source_row_numbers"]) for conflict in conflicts),
                "quarantined": len(quarantine),
                "equations": row_equations,
            },
            "quantity": {
                "input_numeric": _decimal_text(quantity_totals["input_numeric"]),
                "scope_excluded": _decimal_text(quantity_totals["scope_excluded"]),
                "prepared": _decimal_text(quantity_totals["prepared"]),
                "exact_duplicate": _decimal_text(quantity_totals["exact_duplicate"]),
                "conflicted": _decimal_text(quantity_totals["conflicted"]),
                "quarantined": _decimal_text(quantity_totals["quarantined"]),
                "unparseable_source_row_numbers": list(quantity_parse_error_rows),
                "equations": quantity_equations,
            },
        },
    }
