from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable

from sqlalchemy import Select, select, text
from sqlalchemy.orm import Session

from app.models import (
    LogisticsManualReview,
    LogisticsTransfer,
    LogisticsTransferState,
    LogisticsWarehouse,
)
from app.services import logistics

REVIEW_RTU_WITHOUT_SITE_ORDER = "rtu_without_site_order"
REVIEW_RTU_TARGET_WAREHOUSE_UNRESOLVED = "rtu_target_warehouse_unresolved"
REVIEW_RTU_LOOKUP_NOT_UNIQUE = "rtu_lookup_not_unique"
REVIEW_RTU_READINESS_GATE_FAILED = "rtu_readiness_gate_failed"
REVIEW_RTU_SOURCE_WAREHOUSE_UNRESOLVED = "rtu_source_warehouse_unresolved"
REVIEW_RTU_EXTERNAL_CARRIER_UNMAPPED = "rtu_external_carrier_unmapped"
REVIEW_RTU_EXTERNAL_CARRIER_STATE_CONFLICT = "rtu_external_carrier_state_conflict"

_TOKEN_RE = re.compile(r"[0-9a-zа-яё]+", re.IGNORECASE)
_ADDRESS_STOP_TOKENS = {
    "0",
    "00",
    "09",
    "г",
    "ул",
    "улица",
    "д",
    "дом",
    "стр",
    "строение",
    "к",
    "корп",
    "корпус",
    "этаж",
    "пав",
    "павильон",
    "помещение",
    "пр",
    "проспект",
    "кт",
    "пн",
    "вс",
    "москва",
    "санкт",
    "петербург",
    "тк",
    "тц",
    "магазин",
    "master",
    "mobile",
    "floor",
    "pavilion",
    "mon",
    "sun",
    "moscow",
    "saint",
    "st",
    "petersburg",
}


@dataclass(slots=True)
class RtuSourceRow:
    rtu_external_id: str
    rtu_number: str
    rtu_date: datetime
    site_order_external_id: str | None
    site_order_number: str | None
    onec_order_number: str | None
    source_warehouse_external_id: str | None
    source_warehouse_name: str | None
    source_warehouse_code: str | None
    pickup_department_external_id: str | None
    pickup_department_code: str | None
    pickup_department_name: str | None
    delivery_code: str | None
    site_delivery_method: str | None
    address_candidates: tuple[str, ...]
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RtuSkippedRow:
    review_type: str
    reason: str
    source_external_id: str | None
    site_order_number: str | None
    payload: dict[str, Any]


@dataclass(slots=True)
class RtuNormalizationResult:
    ready: list[RtuSourceRow]
    skipped: list[RtuSkippedRow]


@dataclass(slots=True)
class WarehouseResolution:
    warehouse: LogisticsWarehouse | None
    reason: str | None
    matches: list[dict[str, Any]]


@dataclass(slots=True)
class RtuSyncReport:
    dry_run: bool
    fetched: int = 0
    ready: int = 0
    synced_planned: int = 0
    synced_created: int = 0
    synced_updated: int = 0
    manual_review_resolved: int = 0
    skipped: int = 0
    manual_review_created: int = 0
    manual_review_planned: int = 0
    warehouses_created: int = 0
    warehouses_planned: int = 0
    external_carrier_planned: int = 0
    external_carrier_handoff_created: int = 0
    external_carrier_handoff_existing: int = 0
    external_carrier_state_conflicts: int = 0
    local_pickup_skipped: int = 0
    by_reason: Counter[str] = field(default_factory=Counter)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "fetched": self.fetched,
            "ready": self.ready,
            "synced_planned": self.synced_planned,
            "synced_created": self.synced_created,
            "synced_updated": self.synced_updated,
            "manual_review_resolved": self.manual_review_resolved,
            "skipped": self.skipped,
            "manual_review_created": self.manual_review_created,
            "manual_review_planned": self.manual_review_planned,
            "warehouses_created": self.warehouses_created,
            "warehouses_planned": self.warehouses_planned,
            "external_carrier_planned": self.external_carrier_planned,
            "external_carrier_handoff_created": self.external_carrier_handoff_created,
            "external_carrier_handoff_existing": self.external_carrier_handoff_existing,
            "external_carrier_state_conflicts": self.external_carrier_state_conflicts,
            "local_pickup_skipped": self.local_pickup_skipped,
            "by_reason": dict(self.by_reason),
        }


@dataclass(slots=True)
class WarehouseAliasRow:
    warehouse_external_id: str
    warehouse_name: str
    warehouse_code: str | None
    department_external_id: str | None
    department_name: str | None
    department_code: str | None
    address_alias: str | None
    phone: str | None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class WarehouseAliasSyncReport:
    dry_run: bool
    fetched: int = 0
    warehouses_seen: int = 0
    warehouses_created: int = 0
    warehouses_updated: int = 0
    warehouses_planned_created: int = 0
    warehouses_planned_updated: int = 0
    aliases_added: int = 0
    skipped: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "fetched": self.fetched,
            "warehouses_seen": self.warehouses_seen,
            "warehouses_created": self.warehouses_created,
            "warehouses_updated": self.warehouses_updated,
            "warehouses_planned_created": self.warehouses_planned_created,
            "warehouses_planned_updated": self.warehouses_planned_updated,
            "aliases_added": self.aliases_added,
            "skipped": self.skipped,
        }


@dataclass(slots=True)
class WarehouseAliasOverrideReport:
    dry_run: bool
    requested: int = 0
    warehouses_updated: int = 0
    aliases_added: int = 0
    aliases_existing: int = 0
    skipped: int = 0
    missing_warehouses: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "requested": self.requested,
            "warehouses_updated": self.warehouses_updated,
            "aliases_added": self.aliases_added,
            "aliases_existing": self.aliases_existing,
            "skipped": self.skipped,
            "missing_warehouses": self.missing_warehouses,
        }


def fetch_warehouse_address_aliases(
    onec_engine,
    *,
    limit: int | None = None,
) -> list[WarehouseAliasRow]:
    rows = _fetch_warehouse_alias_source_rows(onec_engine, limit=limit)
    return _normalize_warehouse_alias_rows(rows)


def sync_warehouse_address_aliases(
    session: Session,
    onec_engine,
    *,
    limit: int | None = None,
    dry_run: bool = True,
    source_rows: Iterable[dict[str, Any] | Any] | None = None,
) -> dict[str, Any]:
    raw_rows = (
        list(source_rows)
        if source_rows is not None
        else _fetch_warehouse_alias_source_rows(onec_engine, limit=limit)
    )
    alias_rows = _normalize_warehouse_alias_rows(raw_rows)
    grouped: dict[str, list[WarehouseAliasRow]] = {}
    for row in alias_rows:
        grouped.setdefault(row.warehouse_external_id, []).append(row)

    report = WarehouseAliasSyncReport(
        dry_run=dry_run,
        fetched=len(raw_rows),
        warehouses_seen=len(grouped),
        skipped=len(raw_rows) - len(alias_rows),
    )

    for warehouse_external_id, rows in grouped.items():
        existing = session.scalar(
            select(LogisticsWarehouse).where(
                LogisticsWarehouse.external_id == warehouse_external_id
            )
        )
        desired_payload = _warehouse_alias_payload(existing.payload if existing else None, rows)
        added_alias_count = _count_new_aliases(
            existing.payload if existing else None,
            desired_payload,
        )
        if existing is None:
            report.aliases_added += len(desired_payload.get("address_aliases") or [])
            if dry_run:
                report.warehouses_planned_created += 1
                continue
            first = rows[0]
            session.add(
                LogisticsWarehouse(
                    external_id=warehouse_external_id,
                    name=first.warehouse_name or warehouse_external_id,
                    kind=_warehouse_kind(first.warehouse_name),
                    payload=desired_payload,
                )
            )
            report.warehouses_created += 1
            continue

        if added_alias_count <= 0:
            continue
        report.aliases_added += added_alias_count
        if dry_run:
            report.warehouses_planned_updated += 1
            continue
        existing.payload = desired_payload
        report.warehouses_updated += 1

    if not dry_run:
        session.commit()
    return report.as_dict()


def apply_warehouse_alias_overrides(
    session: Session,
    overrides: Iterable[dict[str, Any]],
    *,
    dry_run: bool = True,
) -> dict[str, Any]:
    prepared = [_normalize_alias_override(item) for item in overrides]
    report = WarehouseAliasOverrideReport(dry_run=dry_run, requested=len(prepared))
    applied_at = logistics.utcnow()
    updated_warehouse_ids: set[int] = set()

    for item in prepared:
        warehouse_external_id = item["warehouse_external_id"]
        aliases = item["aliases"]
        warehouse = session.scalar(
            select(LogisticsWarehouse).where(
                LogisticsWarehouse.external_id == warehouse_external_id
            )
        )
        if warehouse is None:
            report.skipped += 1
            report.missing_warehouses.append(warehouse_external_id)
            continue

        payload = dict(warehouse.payload or {})
        current_aliases = _unique_strings(payload.get("address_aliases") or [])
        new_aliases = [alias for alias in aliases if alias not in current_aliases]
        report.aliases_existing += len(aliases) - len(new_aliases)
        if not new_aliases:
            continue

        report.aliases_added += len(new_aliases)
        updated_warehouse_ids.add(warehouse.id)
        if dry_run:
            continue

        payload["address_aliases"] = current_aliases + new_aliases
        history = list(payload.get("alias_override_history") or [])
        for alias in new_aliases:
            history.append(
                {
                    "alias": alias,
                    "reason": item.get("reason"),
                    "confirmed_by": item.get("confirmed_by"),
                    "applied_at": applied_at.isoformat(),
                }
            )
        payload["alias_override_history"] = history
        warehouse.payload = payload

    report.warehouses_updated = len(updated_warehouse_ids)
    if not dry_run:
        session.commit()
    return report.as_dict()


def fetch_ready_rtu_units(
    onec_engine,
    *,
    date_from: date | datetime | None = None,
    limit: int | None = None,
) -> list[RtuSourceRow]:
    rows = _fetch_rtu_source_rows(onec_engine, date_from=date_from, limit=limit)
    return normalize_rtu_source_rows(rows).ready


def sync_ready_rtu_units(
    session: Session,
    onec_engine,
    *,
    date_from: date | datetime | None = None,
    limit: int | None = None,
    dry_run: bool = True,
    external_carrier_flow: bool = False,
    source_rows: Iterable[dict[str, Any] | Any] | None = None,
) -> dict[str, Any]:
    raw_rows = (
        list(source_rows)
        if source_rows is not None
        else _fetch_rtu_source_rows(onec_engine, date_from=date_from, limit=limit)
    )
    normalized = normalize_rtu_source_rows(raw_rows)
    report = RtuSyncReport(dry_run=dry_run, fetched=len(raw_rows), ready=len(normalized.ready))

    for skipped in normalized.skipped:
        _record_skip(session, report, skipped, dry_run=dry_run)

    lookup_counts = Counter(
        make_rtu_lookup_code(row.rtu_external_id, row.site_order_number)
        for row in normalized.ready
        if row.site_order_number
    )
    unit_payloads: list[dict[str, Any]] = []
    external_carrier_rows: dict[str, RtuSourceRow] = {}
    local_pickup_external_ids: set[str] = set()
    for row in normalized.ready:
        skipped = _validate_ready_row(session, row, lookup_counts)
        if skipped is not None:
            _record_skip(session, report, skipped, dry_run=dry_run)
            continue

        source_warehouse = _ensure_source_warehouse(session, report, row, dry_run=dry_run)
        if source_warehouse is None:
            _record_skip(
                session,
                report,
                RtuSkippedRow(
                    review_type=REVIEW_RTU_SOURCE_WAREHOUSE_UNRESOLVED,
                    reason="RTU source warehouse is missing",
                    source_external_id=row.rtu_external_id,
                    site_order_number=row.site_order_number,
                    payload=row.raw,
                ),
                dry_run=dry_run,
            )
            continue

        if _requires_delivery_code_manual_review(row.delivery_code):
            _record_skip(
                session,
                report,
                RtuSkippedRow(
                    review_type=REVIEW_RTU_EXTERNAL_CARRIER_UNMAPPED,
                    reason="RTU delivery code requires manual review",
                    source_external_id=row.rtu_external_id,
                    site_order_number=row.site_order_number,
                    payload=row.raw,
                ),
                dry_run=dry_run,
            )
            continue

        if _is_external_delivery_method(row.delivery_code, row.site_delivery_method):
            if external_carrier_flow and _allows_legacy_external_carrier_handoff(
                row.delivery_code,
                row.site_delivery_method,
            ):
                lookup_code = make_rtu_lookup_code(row.rtu_external_id, row.site_order_number or "")
                unit_payloads.append(
                    _rtu_unit_payload(
                        row,
                        source_warehouse=source_warehouse,
                        target_warehouse=source_warehouse,
                        lookup_code=lookup_code,
                        target_resolution=[],
                        external_carrier_flow=True,
                    )
                )
                external_carrier_rows[row.rtu_external_id] = row
                continue

            existing_unit = session.scalar(
                select(LogisticsTransfer).where(
                    LogisticsTransfer.source_document_type == logistics.SOURCE_RTU,
                    LogisticsTransfer.external_id == row.rtu_external_id,
                )
            )
            if existing_unit is not None and _unit_is_with_external_carrier(
                session,
                existing_unit,
            ):
                continue
            _record_skip(
                session,
                report,
                RtuSkippedRow(
                    review_type=REVIEW_RTU_EXTERNAL_CARRIER_UNMAPPED,
                    reason="RTU delivery method requires external carrier/manual logistics flow",
                    source_external_id=row.rtu_external_id,
                    site_order_number=row.site_order_number,
                    payload=row.raw,
                ),
                dry_run=dry_run,
            )
            continue

        if row.pickup_department_external_id or row.pickup_department_code:
            resolution = resolve_target_warehouse_by_department(
                session,
                department_external_id=row.pickup_department_external_id,
                department_code=row.pickup_department_code,
            )
        elif _use_source_warehouse_for_empty_pickup_address(row):
            report.local_pickup_skipped += 1
            report.by_reason["local_pickup"] += 1
            local_pickup_external_ids.add(row.rtu_external_id)
            continue
        else:
            resolution = resolve_target_warehouse(session, row.address_candidates)
        if resolution.warehouse is None:
            _record_skip(
                session,
                report,
                RtuSkippedRow(
                    review_type=REVIEW_RTU_TARGET_WAREHOUSE_UNRESOLVED,
                    reason=resolution.reason or "RTU target warehouse was not resolved",
                    source_external_id=row.rtu_external_id,
                    site_order_number=row.site_order_number,
                    payload={**row.raw, "target_matches": resolution.matches},
                ),
                dry_run=dry_run,
            )
            continue

        if resolution.warehouse.external_id == source_warehouse.external_id:
            report.local_pickup_skipped += 1
            report.by_reason["local_pickup"] += 1
            local_pickup_external_ids.add(row.rtu_external_id)
            continue

        lookup_code = make_rtu_lookup_code(row.rtu_external_id, row.site_order_number or "")
        unit_payloads.append(
            _rtu_unit_payload(
                row,
                source_warehouse=source_warehouse,
                target_warehouse=resolution.warehouse,
                lookup_code=lookup_code,
                target_resolution=resolution.matches,
            )
        )

    if dry_run:
        report.synced_planned = len(unit_payloads)
        report.external_carrier_planned = len(external_carrier_rows)
        return report.as_dict()

    if unit_payloads:
        result = logistics.sync_units(session, unit_payloads)
        report.synced_created = int(result.get("created") or 0)
        report.synced_updated = int(result.get("updated") or 0)
        external_carrier_success_ids = _apply_external_carrier_handoffs(
            session,
            report,
            external_carrier_rows,
            dry_run=dry_run,
        )
        resolvable_payloads = [
            payload
            for payload in unit_payloads
            if payload["external_id"] not in external_carrier_rows
            or payload["external_id"] in external_carrier_success_ids
        ]
        report.manual_review_resolved = _resolve_manual_reviews_for_unit_payloads(
            session,
            resolvable_payloads,
        )
    report.manual_review_resolved += _resolve_manual_reviews_for_source_external_ids(
        session,
        local_pickup_external_ids,
    )
    session.commit()
    return report.as_dict()


def normalize_rtu_source_rows(rows: Iterable[dict[str, Any] | Any]) -> RtuNormalizationResult:
    ready: list[RtuSourceRow] = []
    skipped: list[RtuSkippedRow] = []
    for raw_row in rows:
        row = _row_to_dict(raw_row)
        if not _bool_value(row.get("is_posted")) or _bool_value(row.get("is_marked")):
            skipped.append(_readiness_skip(row, "RTU is not posted or is marked for deletion"))
            continue
        if not _bool_value(row.get("has_printed")) or not _bool_value(row.get("has_assembled")):
            skipped.append(_readiness_skip(row, "RTU does not pass printed/assembled gate"))
            continue

        rtu_external_id = _clean_ref(row.get("rtu_external_id"))
        rtu_number = _clean_string(row.get("rtu_number"))
        rtu_date = row.get("rtu_date")
        if not rtu_external_id or not rtu_number or not isinstance(rtu_date, datetime):
            skipped.append(_readiness_skip(row, "RTU row is missing required identity fields"))
            continue

        ready.append(
            RtuSourceRow(
                rtu_external_id=rtu_external_id,
                rtu_number=rtu_number,
                rtu_date=rtu_date,
                site_order_external_id=_clean_ref(row.get("site_order_external_id")) or None,
                site_order_number=_clean_string(row.get("site_order_number")) or None,
                onec_order_number=_clean_string(row.get("onec_order_number")) or None,
                source_warehouse_external_id=_clean_ref(row.get("source_warehouse_external_id"))
                or None,
                source_warehouse_name=_clean_string(row.get("source_warehouse_name")) or None,
                source_warehouse_code=_clean_string(row.get("source_warehouse_code")) or None,
                pickup_department_external_id=(
                    _clean_ref(row.get("pickup_department_external_id")) or None
                ),
                pickup_department_code=(_clean_string(row.get("pickup_department_code")) or None),
                pickup_department_name=(_clean_string(row.get("pickup_department_name")) or None),
                delivery_code=_clean_string(row.get("delivery_code")) or None,
                site_delivery_method=_clean_string(row.get("site_delivery_method")) or None,
                address_candidates=_address_candidates(row),
                raw=row,
            )
        )
    return RtuNormalizationResult(ready=ready, skipped=skipped)


def resolve_target_warehouse(
    session: Session,
    address_candidates: Iterable[str],
) -> WarehouseResolution:
    raw_candidates = [_clean_string(value) for value in address_candidates if _clean_string(value)]
    candidates = [_normalize_text(value) for value in raw_candidates]
    candidates = [value for value in candidates if value]
    if not candidates:
        return WarehouseResolution(None, "RTU has no address candidates", [])

    matches: dict[int, dict[str, Any]] = {}
    warehouses = session.scalars(
        select(LogisticsWarehouse)
        .where(LogisticsWarehouse.is_active.is_(True))
        .order_by(LogisticsWarehouse.id.asc())
    ).all()
    for warehouse in warehouses:
        for alias in _warehouse_aliases(warehouse):
            normalized_alias = _normalize_text(alias)
            if len(normalized_alias) < 5:
                continue
            if any(
                normalized_alias in candidate or candidate in normalized_alias
                for candidate in candidates
            ):
                matches[warehouse.id] = {
                    "warehouse_id": warehouse.id,
                    "warehouse_external_id": warehouse.external_id,
                    "warehouse_name": warehouse.name,
                    "alias": alias,
                    "match_type": "substring",
                }
                break
            score, overlap = _address_match_score(alias, raw_candidates)
            if score > 0:
                matches[warehouse.id] = {
                    "warehouse_id": warehouse.id,
                    "warehouse_external_id": warehouse.external_id,
                    "warehouse_name": warehouse.name,
                    "alias": alias,
                    "match_type": "token_overlap",
                    "score": score,
                    "overlap": overlap,
                }
                break

    if len(matches) == 1:
        warehouse_id = next(iter(matches))
        return WarehouseResolution(
            session.get(LogisticsWarehouse, warehouse_id), None, list(matches.values())
        )
    if len(matches) > 1:
        return WarehouseResolution(
            None, "RTU address matched multiple warehouses", list(matches.values())
        )
    return WarehouseResolution(None, "RTU address did not match any warehouse", [])


def resolve_target_warehouse_by_department(
    session: Session,
    *,
    department_external_id: str | None,
    department_code: str | None,
) -> WarehouseResolution:
    external_id = _clean_ref(department_external_id).casefold()
    code = _clean_string(department_code).casefold()
    matches: dict[int, dict[str, Any]] = {}
    warehouses = session.scalars(
        select(LogisticsWarehouse)
        .where(LogisticsWarehouse.is_active.is_(True))
        .order_by(LogisticsWarehouse.id.asc())
    ).all()
    for warehouse in warehouses:
        payload = warehouse.payload if isinstance(warehouse.payload, dict) else {}
        departments = payload.get("onec_departments")
        if not isinstance(departments, list):
            continue
        for department in departments:
            if not isinstance(department, dict):
                continue
            candidate_external_id = _clean_ref(department.get("external_id")).casefold()
            candidate_code = _clean_string(department.get("code")).casefold()
            if (external_id and candidate_external_id == external_id) or (
                code and candidate_code == code
            ):
                matches[warehouse.id] = {
                    "warehouse_id": warehouse.id,
                    "warehouse_external_id": warehouse.external_id,
                    "warehouse_name": warehouse.name,
                    "department_external_id": department.get("external_id"),
                    "department_code": department.get("code"),
                    "match_type": "pickup_department_exact",
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
            "Pickup department matched multiple warehouses",
            list(matches.values()),
        )
    return WarehouseResolution(
        None,
        "Pickup department did not match a logistics warehouse",
        [],
    )


def make_rtu_lookup_code(rtu_external_id: str, site_order_number: str) -> str:
    return f"MMLOG1|rtu|{rtu_external_id}|{site_order_number}"


def _rtu_unit_payload(
    row: RtuSourceRow,
    *,
    source_warehouse: LogisticsWarehouse,
    target_warehouse: LogisticsWarehouse,
    lookup_code: str,
    target_resolution: list[dict[str, Any]],
    external_carrier_flow: bool = False,
) -> dict[str, Any]:
    payload = {
        "source": "1c_rtu_sync",
        "onec_order_number": row.onec_order_number,
        "source_warehouse_code": row.source_warehouse_code,
        "source_warehouse_name": row.source_warehouse_name,
        "pickup_department_external_id": row.pickup_department_external_id,
        "pickup_department_code": row.pickup_department_code,
        "pickup_department_name": row.pickup_department_name,
        "delivery_code": row.delivery_code,
        "site_delivery_method": row.site_delivery_method,
        "address_candidates": list(row.address_candidates),
        "target_resolution": target_resolution,
    }
    if external_carrier_flow:
        payload.update(
            {
                "external_carrier_flow": True,
                "external_carrier_name": _external_carrier_name(
                    row.delivery_code,
                    row.site_delivery_method,
                ),
                "external_carrier_terminal": _first_address_candidate(row),
            }
        )
    return {
        "source_document_type": logistics.SOURCE_RTU,
        "external_id": row.rtu_external_id,
        "document_number": row.rtu_number,
        "document_date": row.rtu_date,
        "source_warehouse_external_id": source_warehouse.external_id,
        "target_warehouse_external_id": target_warehouse.external_id,
        "document_target_warehouse_external_id": target_warehouse.external_id,
        "final_recipient_name": (
            f"Заказ сайта {row.site_order_number}" if row.site_order_number else None
        ),
        "barcode": lookup_code,
        "lookup_code": lookup_code,
        "origin_order_external_id": row.site_order_external_id,
        "site_order_number": row.site_order_number,
        "status": "posted",
        "onec_deleted": False,
        "payload": payload,
    }


def _apply_external_carrier_handoffs(
    session: Session,
    report: RtuSyncReport,
    rows: dict[str, RtuSourceRow],
    *,
    dry_run: bool,
) -> set[str]:
    success_ids: set[str] = set()
    if dry_run or not rows:
        return success_ids

    for external_id, row in rows.items():
        transfer = session.scalar(
            select(LogisticsTransfer).where(
                LogisticsTransfer.source_document_type == logistics.SOURCE_RTU,
                LogisticsTransfer.external_id == external_id,
            )
        )
        if transfer is None:
            _record_skip(
                session,
                report,
                RtuSkippedRow(
                    review_type=REVIEW_RTU_EXTERNAL_CARRIER_STATE_CONFLICT,
                    reason="RTU external carrier unit was not created",
                    source_external_id=external_id,
                    site_order_number=row.site_order_number,
                    payload=row.raw,
                ),
                dry_run=False,
            )
            report.external_carrier_state_conflicts += 1
            continue

        result = logistics.handoff_to_external_carrier_from_sync(
            session,
            transfer_id=transfer.id,
            carrier_name=_external_carrier_name(row.delivery_code, row.site_delivery_method),
            carrier_terminal=_first_address_candidate(row),
            comment="1C RTU external carrier sync",
            idempotency_key=f"1c_rtu_external_carrier:{external_id}",
            meta={
                "delivery_code": row.delivery_code,
                "site_delivery_method": row.site_delivery_method,
                "site_order_number": row.site_order_number,
                "source_external_id": external_id,
            },
        )
        status = result.get("status")
        if status == "created":
            report.external_carrier_handoff_created += 1
            success_ids.add(external_id)
        elif status == "existing":
            report.external_carrier_handoff_existing += 1
            success_ids.add(external_id)
        else:
            _record_skip(
                session,
                report,
                RtuSkippedRow(
                    review_type=REVIEW_RTU_EXTERNAL_CARRIER_STATE_CONFLICT,
                    reason=str(result.get("detail") or "RTU external carrier state conflict"),
                    source_external_id=external_id,
                    site_order_number=row.site_order_number,
                    payload={
                        **row.raw,
                        "handoff_result": result,
                        "transfer_id": transfer.id,
                    },
                ),
                dry_run=False,
            )
            report.external_carrier_state_conflicts += 1

    if report.external_carrier_state_conflicts:
        session.commit()
    return success_ids


def _fetch_rtu_source_rows(
    onec_engine,
    *,
    date_from: date | datetime | None,
    limit: int | None,
) -> list[Any]:
    limit_clause = f"TOP ({max(1, int(limit))})" if limit else ""
    date_filter = "AND rtu._Date_Time >= :date_from" if date_from else ""
    statement = text(f"""
        SELECT {limit_clause}
            CONVERT(varchar(34), rtu._IDRRef, 1) AS rtu_external_id,
            LTRIM(RTRIM(rtu._Number)) AS rtu_number,
            rtu._Date_Time AS rtu_date,
            CONVERT(varchar(34), ord._IDRRef, 1) AS site_order_external_id,
            LTRIM(RTRIM(ord._Number)) AS onec_order_number,
            NULLIF(LTRIM(RTRIM(ord._Fld2425)), N'') AS site_order_number,
            NULLIF(LTRIM(RTRIM(ord._Fld9266)), N'') AS site_delivery_method,
            CONVERT(varchar(34), ord._Fld10203RRef, 1) AS pickup_department_external_id,
            NULLIF(LTRIM(RTRIM(pickup_dep._Code)), N'') AS pickup_department_code,
            NULLIF(LTRIM(RTRIM(pickup_dep._Description)), N'') AS pickup_department_name,
            CAST(ord._Fld2422 AS nvarchar(max)) AS site_delivery_addition,
            CAST(ord._Fld2395 AS nvarchar(max)) AS site_delivery_address,
            CAST(rtu._Fld4965 AS nvarchar(max)) AS rtu_delivery_addition,
            CAST(rtu._Fld4952 AS nvarchar(max)) AS rtu_delivery_address,
            CONVERT(varchar(34), rtu._Fld4940RRef, 1) AS source_warehouse_external_id,
            NULLIF(LTRIM(RTRIM(wh._Code)), N'') AS source_warehouse_code,
            wh._Description AS source_warehouse_name,
            CASE WHEN rtu._Marked = 0x01 THEN 1 ELSE 0 END AS is_marked,
            CASE WHEN rtu._Posted = 0x01 THEN 1 ELSE 0 END AS is_posted,
            CASE WHEN EXISTS (
                SELECT 1
                FROM dbo._InfoRg9448 AS print_event WITH (NOLOCK)
                WHERE print_event._Fld9449_RRRef = rtu._IDRRef
                  AND print_event._Fld9454 = N'Распечатан'
            ) THEN 1 ELSE 0 END AS has_printed,
            CASE WHEN EXISTS (
                SELECT 1
                FROM dbo._InfoRg9448 AS assembled_event WITH (NOLOCK)
                WHERE assembled_event._Fld9449_RRRef = rtu._IDRRef
                  AND assembled_event._Fld9454 = N'Собран'
            ) THEN 1 ELSE 0 END AS has_assembled
        FROM dbo._Document203 AS rtu WITH (NOLOCK)
        JOIN dbo._Document132 AS ord WITH (NOLOCK)
            ON ord._IDRRef = rtu._Fld4939_RRRef
        LEFT JOIN dbo._Reference68 AS pickup_dep WITH (NOLOCK)
            ON pickup_dep._IDRRef = ord._Fld10203RRef
        LEFT JOIN dbo._Reference80 AS wh WITH (NOLOCK)
            ON wh._IDRRef = rtu._Fld4940RRef
        WHERE rtu._Fld4939_RRRef IS NOT NULL
          {date_filter}
        ORDER BY rtu._Date_Time DESC
        """)
    params = {"date_from": date_from} if date_from else {}
    with onec_engine.connect() as connection:
        return list(connection.execute(statement, params))


def _fetch_warehouse_alias_source_rows(onec_engine, *, limit: int | None) -> list[Any]:
    limit_clause = f"TOP ({max(1, int(limit))})" if limit else ""
    statement = text(f"""
        SELECT {limit_clause}
            CONVERT(varchar(34), wh._IDRRef, 1) AS warehouse_external_id,
            NULLIF(LTRIM(RTRIM(wh._Code)), N'') AS warehouse_code,
            NULLIF(LTRIM(RTRIM(wh._Description)), N'') AS warehouse_name,
            CONVERT(varchar(34), dep._IDRRef, 1) AS department_external_id,
            NULLIF(LTRIM(RTRIM(dep._Code)), N'') AS department_code,
            NULLIF(LTRIM(RTRIM(dep._Description)), N'') AS department_name,
            NULLIF(LTRIM(RTRIM(CAST(dep._Fld9249 AS nvarchar(max)))), N'') AS address_alias,
            NULLIF(LTRIM(RTRIM(CAST(dep._Fld9518 AS nvarchar(max)))), N'') AS phone
        FROM dbo._Reference68 AS dep WITH (NOLOCK)
        JOIN dbo._Reference80 AS wh WITH (NOLOCK)
            ON wh._IDRRef = dep._Fld8919RRef
        WHERE dep._Fld8919RRef IS NOT NULL
          AND dep._Marked = 0x00
          AND wh._Marked = 0x00
          AND NULLIF(LTRIM(RTRIM(CAST(dep._Fld9249 AS nvarchar(max)))), N'') IS NOT NULL
        ORDER BY wh._Description, dep._Description
        """)
    with onec_engine.connect() as connection:
        return list(connection.execute(statement))


def _validate_ready_row(
    session: Session,
    row: RtuSourceRow,
    lookup_counts: Counter[str],
) -> RtuSkippedRow | None:
    if not row.site_order_number:
        return RtuSkippedRow(
            review_type=REVIEW_RTU_WITHOUT_SITE_ORDER,
            reason="RTU does not contain site_order_number",
            source_external_id=row.rtu_external_id,
            site_order_number=None,
            payload=row.raw,
        )
    lookup_code = make_rtu_lookup_code(row.rtu_external_id, row.site_order_number)
    if lookup_counts[lookup_code] > 1:
        return RtuSkippedRow(
            review_type=REVIEW_RTU_LOOKUP_NOT_UNIQUE,
            reason="Generated RTU lookup_code is duplicated in current batch",
            source_external_id=row.rtu_external_id,
            site_order_number=row.site_order_number,
            payload={**row.raw, "lookup_code": lookup_code},
        )
    existing = session.scalar(
        select(LogisticsTransfer).where(
            LogisticsTransfer.lookup_code == lookup_code,
            (
                (LogisticsTransfer.source_document_type != logistics.SOURCE_RTU)
                | (LogisticsTransfer.external_id != row.rtu_external_id)
            ),
        )
    )
    if existing is not None:
        return RtuSkippedRow(
            review_type=REVIEW_RTU_LOOKUP_NOT_UNIQUE,
            reason="Generated RTU lookup_code already belongs to another logistics unit",
            source_external_id=row.rtu_external_id,
            site_order_number=row.site_order_number,
            payload={
                **row.raw,
                "lookup_code": lookup_code,
                "conflict_transfer_id": existing.id,
            },
        )
    return None


def _ensure_source_warehouse(
    session: Session,
    report: RtuSyncReport,
    row: RtuSourceRow,
    *,
    dry_run: bool,
) -> LogisticsWarehouse | None:
    if not row.source_warehouse_external_id:
        return None
    warehouse = session.scalar(
        select(LogisticsWarehouse).where(
            LogisticsWarehouse.external_id == row.source_warehouse_external_id
        )
    )
    if warehouse is not None:
        return warehouse
    if dry_run:
        report.warehouses_planned += 1
        return LogisticsWarehouse(
            external_id=row.source_warehouse_external_id,
            name=row.source_warehouse_name
            or row.source_warehouse_code
            or row.source_warehouse_external_id,
            kind="warehouse",
            payload={
                "source": "1c_rtu_sync",
                "code": row.source_warehouse_code,
            },
        )
    warehouse = LogisticsWarehouse(
        external_id=row.source_warehouse_external_id,
        name=row.source_warehouse_name
        or row.source_warehouse_code
        or row.source_warehouse_external_id,
        kind="warehouse",
        payload={
            "source": "1c_rtu_sync",
            "code": row.source_warehouse_code,
        },
    )
    session.add(warehouse)
    session.flush()
    report.warehouses_created += 1
    return warehouse


def _record_skip(
    session: Session,
    report: RtuSyncReport,
    skipped: RtuSkippedRow,
    *,
    dry_run: bool,
) -> None:
    report.skipped += 1
    report.by_reason[skipped.review_type] += 1
    if dry_run:
        report.manual_review_planned += 1
        return
    if _create_manual_review_once(session, skipped):
        report.manual_review_created += 1


def _create_manual_review_once(session: Session, skipped: RtuSkippedRow) -> bool:
    existing = session.scalar(
        _manual_review_selector(
            skipped.source_external_id,
        )
    )
    if existing is not None:
        return False
    logistics.create_manual_review(
        session,
        review_type=skipped.review_type,
        reason=skipped.reason,
        source_document_type=logistics.SOURCE_RTU,
        source_external_id=skipped.source_external_id,
        payload=_jsonable_payload(
            {**skipped.payload, "site_order_number": skipped.site_order_number}
        ),
    )
    return True


def _resolve_manual_reviews_for_unit_payloads(
    session: Session,
    unit_payloads: list[dict[str, Any]],
) -> int:
    source_external_ids = sorted(
        {
            payload["external_id"]
            for payload in unit_payloads
            if payload.get("source_document_type") == logistics.SOURCE_RTU
            and payload.get("external_id")
        }
    )
    return _resolve_manual_reviews_for_source_external_ids(session, source_external_ids)


def _resolve_manual_reviews_for_source_external_ids(
    session: Session,
    source_external_ids: Iterable[str],
) -> int:
    source_external_ids = sorted({value for value in source_external_ids if value})
    if not source_external_ids:
        return 0
    reviews = session.scalars(
        select(LogisticsManualReview).where(
            LogisticsManualReview.source_document_type == logistics.SOURCE_RTU,
            LogisticsManualReview.source_external_id.in_(source_external_ids),
            LogisticsManualReview.status == "open",
        )
    ).all()
    if not reviews:
        return 0
    resolved_at = logistics.utcnow()
    for review in reviews:
        payload = dict(review.payload or {})
        payload["auto_resolved_by"] = "rtu_sync"
        payload["auto_resolved_at"] = resolved_at.isoformat()
        review.payload = payload
        review.status = "resolved"
        review.resolved_at = resolved_at
    session.commit()
    return len(reviews)


def _manual_review_selector(
    source_external_id: str | None,
) -> Select[tuple[LogisticsManualReview]]:
    return select(LogisticsManualReview).where(
        LogisticsManualReview.source_document_type == logistics.SOURCE_RTU,
        LogisticsManualReview.source_external_id == source_external_id,
        LogisticsManualReview.status == "open",
    )


def _readiness_skip(row: dict[str, Any], reason: str) -> RtuSkippedRow:
    return RtuSkippedRow(
        review_type=REVIEW_RTU_READINESS_GATE_FAILED,
        reason=reason,
        source_external_id=_clean_ref(row.get("rtu_external_id")) or None,
        site_order_number=_clean_string(row.get("site_order_number")) or None,
        payload=row,
    )


def _row_to_dict(row: dict[str, Any] | Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    mapping = getattr(row, "_mapping", None)
    if mapping is not None:
        return dict(mapping)
    return dict(row)


def _normalize_warehouse_alias_rows(
    rows: Iterable[dict[str, Any] | Any],
) -> list[WarehouseAliasRow]:
    normalized: list[WarehouseAliasRow] = []
    for raw_row in rows:
        row = _row_to_dict(raw_row)
        warehouse_external_id = _clean_ref(row.get("warehouse_external_id"))
        warehouse_name = _clean_string(row.get("warehouse_name"))
        address_alias = _clean_string(row.get("address_alias"))
        if not warehouse_external_id or not warehouse_name or not address_alias:
            continue
        normalized.append(
            WarehouseAliasRow(
                warehouse_external_id=warehouse_external_id,
                warehouse_name=warehouse_name,
                warehouse_code=_clean_string(row.get("warehouse_code")) or None,
                department_external_id=_clean_ref(row.get("department_external_id")) or None,
                department_name=_clean_string(row.get("department_name")) or None,
                department_code=_clean_string(row.get("department_code")) or None,
                address_alias=address_alias,
                phone=_clean_string(row.get("phone")) or None,
                raw=row,
            )
        )
    return normalized


def _normalize_alias_override(item: dict[str, Any]) -> dict[str, Any]:
    warehouse_external_id = _clean_ref(item.get("warehouse_external_id"))
    aliases = item.get("aliases")
    if aliases is None:
        aliases = [item.get("alias")]
    elif isinstance(aliases, str):
        aliases = [aliases]
    aliases = _unique_strings(aliases)
    if not warehouse_external_id:
        raise ValueError("warehouse_external_id is required")
    if not aliases:
        raise ValueError("alias or aliases is required")
    return {
        "warehouse_external_id": warehouse_external_id,
        "aliases": aliases,
        "reason": _clean_string(item.get("reason")) or None,
        "confirmed_by": _clean_string(item.get("confirmed_by")) or None,
    }


def _address_candidates(row: dict[str, Any]) -> tuple[str, ...]:
    values = (
        row.get("site_delivery_addition"),
        row.get("site_delivery_address"),
        row.get("rtu_delivery_addition"),
        row.get("rtu_delivery_address"),
    )
    result: list[str] = []
    for value in values:
        clean = _clean_string(value)
        if clean and clean not in result:
            result.append(clean)
    return tuple(result)


def _warehouse_alias_payload(
    existing_payload: dict | None,
    rows: list[WarehouseAliasRow],
) -> dict[str, Any]:
    payload = dict(existing_payload or {})
    payload["source"] = payload.get("source") or "1c_department_alias_sync"

    aliases = _unique_strings(payload.get("address_aliases") or [])
    department_aliases = _unique_strings(payload.get("department_aliases") or [])
    departments = list(payload.get("onec_departments") or [])
    department_ids = {
        item.get("external_id")
        for item in departments
        if isinstance(item, dict) and item.get("external_id")
    }

    for row in rows:
        aliases = _append_unique(aliases, row.address_alias)
        department_aliases = _append_unique(department_aliases, row.department_name)
        if row.department_external_id and row.department_external_id not in department_ids:
            departments.append(
                {
                    "external_id": row.department_external_id,
                    "code": row.department_code,
                    "name": row.department_name,
                    "phone": row.phone,
                }
            )
            department_ids.add(row.department_external_id)

    payload["address_aliases"] = aliases
    if department_aliases:
        payload["department_aliases"] = department_aliases
    if departments:
        payload["onec_departments"] = departments
    return payload


def _count_new_aliases(existing_payload: dict | None, desired_payload: dict[str, Any]) -> int:
    current = set(_unique_strings((existing_payload or {}).get("address_aliases") or []))
    desired = set(_unique_strings(desired_payload.get("address_aliases") or []))
    return len(desired - current)


def _warehouse_aliases(warehouse: LogisticsWarehouse) -> list[str]:
    aliases = [warehouse.name, warehouse.external_id]
    payload = warehouse.payload if isinstance(warehouse.payload, dict) else {}
    for key in ("address_aliases", "aliases", "addresses", "department_aliases"):
        value = payload.get(key)
        if isinstance(value, str):
            aliases.append(value)
        elif isinstance(value, list):
            aliases.extend(str(item) for item in value if item)
    for key in ("address", "pickup_address"):
        value = payload.get(key)
        if value:
            aliases.append(str(value))
    return [item for item in aliases if _clean_string(item)]


def _unique_strings(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        result = _append_unique(result, value)
    return result


def _append_unique(values: list[str], value: Any) -> list[str]:
    clean = _clean_string(value)
    if clean and clean not in values:
        values.append(clean)
    return values


def _warehouse_kind(name: str | None) -> str:
    normalized = _normalize_text(name or "")
    if "транзит" in normalized:
        return "transit"
    if "централь" in normalized:
        return "central"
    return "store"


_DELIVERY_CODE_CARRIER_NAMES = {
    "CDEK_PVZ": "СДЭК",
    "CDEK_COURIER": "СДЭК",
    "RUSSIAN_POST": "Почта России",
    "MM_COURIER": "Курьер Master Mobile",
    "DOSTAVISTA": "Dostavista",
    "YANDEX_TAXI": "Яндекс/такси",
    "MARSHRUTKA_PTG": "Маршрутка Пятигорска",
}
_KNOWN_DELIVERY_CODES = {"PICKUP", "OTHER", *_DELIVERY_CODE_CARRIER_NAMES}
_LEGACY_EXTERNAL_DELIVERY_MARKERS = (
    "сдэк",
    "почта",
    "курьер",
    "доставка",
    "достависта",
    "dostavista",
    "маршрутка",
    "яндекс",
    "такси",
)


def _normalize_delivery_code(value: str | None) -> str:
    return _clean_string(value).upper()


def _requires_delivery_code_manual_review(delivery_code: str | None) -> bool:
    normalized_code = _normalize_delivery_code(delivery_code)
    return bool(normalized_code) and (
        normalized_code == "OTHER" or normalized_code not in _KNOWN_DELIVERY_CODES
    )


def _is_external_delivery_method(
    delivery_code: str | None,
    value: str | None,
) -> bool:
    normalized_code = _normalize_delivery_code(delivery_code)
    if normalized_code:
        return normalized_code in _DELIVERY_CODE_CARRIER_NAMES
    normalized = _normalize_text(value or "")
    return any(marker in normalized for marker in _LEGACY_EXTERNAL_DELIVERY_MARKERS)


def _allows_legacy_external_carrier_handoff(
    delivery_code: str | None,
    value: str | None,
) -> bool:
    normalized_code = _normalize_delivery_code(delivery_code)
    if normalized_code:
        return normalized_code in {"CDEK_PVZ", "CDEK_COURIER", "RUSSIAN_POST"}
    normalized = _normalize_text(value or "")
    return any(marker in normalized for marker in ("сдэк", "почта"))


def _use_source_warehouse_for_empty_pickup_address(row: RtuSourceRow) -> bool:
    if row.address_candidates:
        return False
    normalized = _normalize_text(row.site_delivery_method or "")
    return "самовывоз" in normalized and not _is_external_delivery_method(
        row.delivery_code,
        row.site_delivery_method,
    )


def _external_carrier_name(delivery_code: str | None, value: str | None) -> str:
    normalized_code = _normalize_delivery_code(delivery_code)
    if normalized_code:
        return _DELIVERY_CODE_CARRIER_NAMES.get(normalized_code, normalized_code)
    normalized = _normalize_text(value or "")
    if "сдэк" in normalized:
        return "СДЭК"
    if "почта" in normalized:
        return "Почта России"
    if "курьер" in normalized:
        return "Курьерская доставка"
    if "достависта" in normalized:
        return "Dostavista"
    if "маршрутка" in normalized:
        return "Маршрутка Пятигорска"
    if "яндекс" in normalized or "такси" in normalized:
        return "Яндекс/такси"
    if "доставка" in normalized:
        return "Внешняя доставка"
    return _clean_string(value) or "Внешний перевозчик"


def _first_address_candidate(row: RtuSourceRow) -> str | None:
    for candidate in row.address_candidates:
        clean = _clean_string(candidate)
        if clean:
            return clean
    return None


def _unit_is_with_external_carrier(
    session: Session,
    transfer: LogisticsTransfer,
) -> bool:
    state = transfer.state or session.get(LogisticsTransferState, transfer.id)
    return state is not None and state.status == logistics.STATUS_WITH_EXTERNAL_CARRIER


def _normalize_text(value: str) -> str:
    tokens = _TOKEN_RE.findall(str(value).casefold().replace("ё", "е"))
    return " ".join(tokens)


def _address_match_score(alias: str, candidates: Iterable[str]) -> tuple[int, list[str]]:
    alias_tokens = _address_match_tokens(alias)
    alias_pavilions = _pavilion_tokens(alias)
    best_score = 0
    best_overlap: list[str] = []
    for candidate in candidates:
        candidate_tokens = _address_match_tokens(candidate)
        overlap = alias_tokens & candidate_tokens
        if not _is_confident_address_overlap(alias_pavilions, _pavilion_tokens(candidate), overlap):
            continue
        score = len(overlap)
        if alias_pavilions & _pavilion_tokens(candidate):
            score += 2
        if score > best_score:
            best_score = score
            best_overlap = sorted(overlap)
    return best_score, best_overlap


def _is_confident_address_overlap(
    alias_pavilions: set[str],
    candidate_pavilions: set[str],
    overlap: set[str],
) -> bool:
    if not _has_street_or_place_token(overlap):
        return False
    if alias_pavilions and candidate_pavilions and not alias_pavilions & candidate_pavilions:
        return False
    if alias_pavilions & candidate_pavilions and len(overlap) >= 2:
        return True
    if len(overlap) >= 3:
        return True
    return len(overlap) >= 2 and any(_has_digit(token) for token in overlap)


def _address_match_tokens(value: str) -> set[str]:
    result: set[str] = set()
    for token in _TOKEN_RE.findall(str(value).casefold().replace("ё", "е")):
        parts = re.findall(r"\d+|[a-zа-яё]+", token, flags=re.IGNORECASE)
        for item in {token, *parts}:
            if item in _ADDRESS_STOP_TOKENS:
                continue
            if len(item) <= 1 and not item.isdigit():
                continue
            result.add(item)
    return result


def _pavilion_tokens(value: str) -> set[str]:
    text = str(value).casefold().replace("ё", "е")
    result: set[str] = set()
    for match in re.finditer(
        r"(?:пав\.?|павильон|pavilion\.?)\s*([a-zа-яё]?[- ]?\d{2,4}(?:[/|-]\d{2,4})*)",
        text,
    ):
        result.update(re.findall(r"\d{2,4}", match.group(1)))
    return result


def _has_street_or_place_token(tokens: set[str]) -> bool:
    return any(len(token) >= 3 and not _has_digit(token) for token in tokens)


def _has_digit(value: str) -> bool:
    return any(symbol.isdigit() for symbol in value)


def _clean_string(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _clean_ref(value: Any) -> str:
    clean = _clean_string(value)
    if clean.lower().startswith("0x") and all(
        symbol in "0123456789abcdefABCDEF" for symbol in clean[2:]
    ):
        normalized = clean.lower()
        if normalized == "0x" + "0" * 32:
            return ""
        return normalized
    return clean


def _bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, bytes):
        return value not in {b"", b"\x00"}
    if value is None:
        return False
    text_value = str(value).strip().lower()
    return text_value in {"1", "true", "yes", "y", "0x01", "01"}


def _jsonable_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable_payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable_payload(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    return value
