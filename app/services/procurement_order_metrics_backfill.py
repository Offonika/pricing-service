"""Safe metric enrichment for open procurement-order assistant lines."""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, selectinload

from app.models.procurement_order_formation import (
    ProcurementOrderFormation,
    ProcurementOrderFormationEvent,
    ProcurementOrderFormationLine,
    ProcurementSupplierProfile,
)
from app.services.procurement_order_metrics import (
    DEFAULT_METRICS_WINDOW_DAYS,
    fetch_procurement_line_metrics_from_onec,
    fetch_supplier_contract_terms,
    fetch_supplier_order_counts,
)
from app.services.procurement_supplier_profiles import (
    aggregate_supplier_facts,
    upsert_supplier_profile_facts,
)
from tasks.report_display_auto_order_adaptive_lead_time_comparison import (
    build_lead_time_indexes,
    choose_lead_time_candidate,
)
from tasks.report_display_supplier_lead_time_history import display_group_key

MANIFEST_SCHEMA_VERSION = 1
OPEN_ASSISTANT_STATUSES = frozenset({"draft", "review", "error"})
IMMUTABLE_ONEC_STATUSES = frozenset({"pending", "transmitted"})
MANAGED_METRIC_KEYS = frozenset(
    {
        "metrics_as_of",
        "metrics_window_days",
        "profitability_pct",
        "profitability_source",
        "profitability_sales_amount",
        "profitability_cost_amount",
        "profitability_status",
        "product_defect_pct",
        "product_defect_history_units",
        "product_defect_return_units",
        "product_defect_confidence",
        "product_defect_source",
        "product_defect_status",
        "supplier_defect_pct",
        "supplier_defect_history_units",
        "supplier_defect_confidence",
        "supplier_defect_attribution",
        "supplier_defect_source",
        "supplier_defect_source_status",
        "latest_historical_purchase_price",
        "previous_purchase_price",
        "price_change_pct",
        "price_change_status",
        "price_metrics_source",
        "price_history_count",
        "price_history_currency_ref",
        "price_history_expected_currency",
        "price_history_available_currencies",
        "price_history_latest_at",
        "price_history_previous_at",
        "supplier_history_order_count",
        "supplier_prepare_days",
        "logistics_days",
        "lead_time_days",
        "lead_time_source_level",
        "lead_time_confidence",
        "lead_time_latest_supplier_order_at",
        "lead_time_latest_cargo_handoff_at",
        "lead_time_latest_receipt_at",
    }
)
PROFILE_FACT_FIELDS = (
    "history_order_count",
    "supplier_prepare_days",
    "logistics_days",
    "lead_time_days",
    "lead_time_confidence",
    "price_history_count",
    "supplier_defect_pct",
    "supplier_defect_history_units",
    "supplier_defect_confidence",
    "payment_terms",
    "credit_days",
    "credit_limit",
    "terms_source",
    "terms_status",
    "facts_payload",
    "facts_updated_at",
)


def build_metrics_backfill_plan(
    db: Session,
    onec_engine: Engine,
    *,
    lead_time_rows: Sequence[Mapping[str, Any]] = (),
    as_of: date,
    window_days: int = DEFAULT_METRICS_WINDOW_DAYS,
    run_id: str | None = None,
) -> dict[str, Any]:
    run_id = run_id or f"procurement-metrics-{uuid.uuid4().hex}"
    orders = list(
        db.scalars(
            select(ProcurementOrderFormation)
            .where(ProcurementOrderFormation.status.in_(OPEN_ASSISTANT_STATUSES))
            .where(~ProcurementOrderFormation.onec_status.in_(IMMUTABLE_ONEC_STATUSES))
            .options(selectinload(ProcurementOrderFormation.lines))
            .order_by(ProcurementOrderFormation.id)
        )
        .unique()
        .all()
    )
    lines = [
        line
        for order in orders
        for line in sorted(order.lines, key=lambda item: item.line_number)
        if not line.removed
    ]
    metric_items = [
        {
            "nomenclature_code": line.nomenclature_code,
            "supplier_ref": line.order.supplier_ref,
            "currency": line.currency or line.order.currency,
        }
        for line in lines
    ]
    onec_metrics = fetch_procurement_line_metrics_from_onec(
        onec_engine,
        items=metric_items,
        as_of=as_of,
        window_days=window_days,
    )
    supplier_order_counts = fetch_supplier_order_counts(
        onec_engine,
        supplier_refs=[order.supplier_ref or "" for order in orders],
        period_end=datetime.combine(as_of, time.min),
    )
    contract_terms = fetch_supplier_contract_terms(
        onec_engine,
        items=[
            {
                "supplier_ref": order.supplier_ref,
                "contract_ref": order.contract_ref,
            }
            for order in orders
        ],
    )
    code_index, group_index = build_lead_time_indexes(lead_time_rows)
    items: list[dict[str, Any]] = []
    supplier_line_payloads: dict[str, list[dict[str, Any]]] = defaultdict(list)
    supplier_contract_terms: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for line in lines:
        code = _clean(line.nomenclature_code)
        supplier_ref = _normalize_ref(line.order.supplier_ref)
        currency = _normalize_currency(line.currency or line.order.currency)
        metrics = dict(onec_metrics.get((code, supplier_ref, currency), {}))
        metrics["supplier_history_order_count"] = supplier_order_counts.get(supplier_ref)
        lead_candidate, source_level = choose_lead_time_candidate(
            code,
            display_group_key({"name": line.nomenclature_name}),
            code_index=code_index,
            group_index=group_index,
        )
        metrics.update(_lead_time_payload(lead_candidate, source_level=source_level))
        before_payload = _json_copy(line.payload or {})
        after_payload = _merge_metrics_payload(before_payload, metrics)
        supplier_line_payloads[supplier_ref].append(after_payload)
        contract_ref = _normalize_ref(line.order.contract_ref)
        terms = contract_terms.get((supplier_ref, contract_ref))
        if terms is not None:
            supplier_contract_terms[supplier_ref][contract_ref] = dict(terms)
        items.append(
            {
                "order_id": int(line.order_id),
                "line_id": int(line.id),
                "line_number": line.line_number,
                "nomenclature_code": code,
                "supplier_ref": supplier_ref,
                "currency": currency,
                "changed": before_payload != after_payload,
                "before_payload": before_payload,
                "after_payload": after_payload,
                "before_line_version": line.version,
            }
        )

    profiles = {
        profile.supplier_ref: profile
        for profile in db.scalars(
            select(ProcurementSupplierProfile).where(
                ProcurementSupplierProfile.supplier_ref.in_(
                    [value for value in supplier_line_payloads if value]
                )
            )
        ).all()
    }
    supplier_items: list[dict[str, Any]] = []
    orders_by_supplier = {_normalize_ref(order.supplier_ref): order for order in reversed(orders)}
    for supplier_ref, payloads in sorted(supplier_line_payloads.items()):
        if not supplier_ref:
            continue
        order = orders_by_supplier[supplier_ref]
        facts = aggregate_supplier_facts(payloads)
        terms_facts = _supplier_contract_terms_payload(
            supplier_contract_terms.get(supplier_ref, {}).values()
        )
        facts["facts_payload"] = {
            **dict(facts.get("facts_payload") or {}),
            **dict(terms_facts.pop("facts_payload") or {}),
        }
        facts.update(terms_facts)
        profile = profiles.get(supplier_ref)
        before = _profile_snapshot(profile)
        supplier_items.append(
            {
                "supplier_ref": supplier_ref,
                "supplier_code": order.supplier_code,
                "supplier_name": order.supplier_name,
                "before_profile": before,
                "before_profile_version": profile.version if profile is not None else 0,
                "profile_existed": profile is not None,
                "facts": _json_copy(facts),
                "changed": _profile_facts_changed(profile, facts),
            }
        )

    changed_items = [item for item in items if item["changed"]]
    changed_order_ids = sorted({int(item["order_id"]) for item in changed_items})
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "run_id": run_id,
        "mode": "dry_run",
        "created_at": datetime.now(UTC).isoformat(),
        "database_commit": False,
        "as_of": as_of.isoformat(),
        "window_days": int(window_days),
        "summary": {
            "orders_scanned": len(orders),
            "lines_scanned": len(lines),
            "lines_changed": len(changed_items),
            "lines_unchanged": len(lines) - len(changed_items),
            "supplier_profiles_scanned": len(supplier_items),
            "supplier_profiles_changed": sum(item["changed"] for item in supplier_items),
            "profitability_ready": sum(
                item["after_payload"].get("profitability_pct") is not None for item in items
            ),
            "price_change_ready": sum(
                item["after_payload"].get("price_change_pct") is not None for item in items
            ),
            "product_defect_ready": sum(
                item["after_payload"].get("product_defect_pct") is not None for item in items
            ),
            "supplier_defect_ready": sum(
                item["after_payload"].get("supplier_defect_attribution") == "supplier_exact"
                and item["after_payload"].get("supplier_defect_pct") is not None
                for item in items
            ),
            "lead_time_ready": sum(
                item["after_payload"].get("lead_time_days") is not None for item in items
            ),
        },
        "safety": {
            "onec_write": False,
            "bitrix_write": False,
            "site_write": False,
            "commercial_fields_write": False,
            "open_assistant_orders_only": True,
        },
        "items": items,
        "suppliers": supplier_items,
        "orders": [
            {"order_id": int(order.id), "before_order_version": order.version}
            for order in orders
            if int(order.id) in changed_order_ids
        ],
    }


def apply_metrics_backfill(db: Session, plan: Mapping[str, Any]) -> dict[str, Any]:
    _validate_manifest(plan, expected_mode="dry_run")
    changed_items = [dict(item) for item in plan.get("items", []) if item.get("changed")]
    by_order: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for item in changed_items:
        by_order[int(item["order_id"])].append(item)
    applied_items: list[dict[str, Any]] = []
    applied_orders: list[dict[str, Any]] = []
    for order_id, changes in by_order.items():
        order = _get_order_for_update(db, order_id)
        if not _order_is_open(order):
            raise RuntimeError(f"order {order_id} left the open assistant queue")
        before_order_version = order.version
        before_lines: list[dict[str, Any]] = []
        after_lines: list[dict[str, Any]] = []
        for change in changes:
            line = _line_by_id(order, int(change["line_id"]))
            if line.version != int(change["before_line_version"]):
                raise RuntimeError(f"line {line.id} version changed")
            if _json_copy(line.payload or {}) != change["before_payload"]:
                raise RuntimeError(f"line {line.id} payload changed")
            before_lines.append({"line_id": line.id, "version": line.version})
            line.payload = _json_copy(change["after_payload"])
            line.version += 1
            change["applied_line_version"] = line.version
            after_lines.append({"line_id": line.id, "version": line.version})
            applied_items.append(change)
        order.version += 1
        applied_orders.append(
            {
                "order_id": order.id,
                "before_order_version": before_order_version,
                "applied_order_version": order.version,
            }
        )
        db.add(
            ProcurementOrderFormationEvent(
                order_id=order.id,
                entity_type="order",
                entity_id=str(order.id),
                event_type="procurement_order_metrics_backfilled",
                actor="system:procurement-order-metrics",
                idempotency_key=f"{plan['run_id']}:order:{order.id}",
                before={"version": before_order_version, "lines": before_lines},
                after={"version": order.version, "lines": after_lines},
                payload={"onec_write": False, "bitrix_write": False},
            )
        )

    applied_suppliers: list[dict[str, Any]] = []
    for entry in plan.get("suppliers", []):
        supplier = dict(entry)
        if not supplier.get("changed"):
            continue
        current = db.scalar(
            select(ProcurementSupplierProfile)
            .where(ProcurementSupplierProfile.supplier_ref == supplier["supplier_ref"])
            .with_for_update()
        )
        if (current.version if current is not None else 0) != int(
            supplier["before_profile_version"]
        ):
            raise RuntimeError(f"supplier profile {supplier['supplier_ref']} version changed")
        profile, changed = upsert_supplier_profile_facts(
            db,
            supplier_ref=supplier["supplier_ref"],
            supplier_code=supplier.get("supplier_code"),
            supplier_name=supplier["supplier_name"],
            facts=supplier["facts"],
            run_id=str(plan["run_id"]),
        )
        if changed:
            supplier["applied_profile_version"] = profile.version
            applied_suppliers.append(supplier)
    db.flush()
    result = _json_copy(plan)
    result.update(
        {
            "mode": "apply",
            "applied_at": datetime.now(UTC).isoformat(),
            "items": applied_items,
            "orders": applied_orders,
            "suppliers": applied_suppliers,
        }
    )
    result["summary"]["applied_lines"] = len(applied_items)
    result["summary"]["applied_orders"] = len(applied_orders)
    result["summary"]["applied_supplier_profiles"] = len(applied_suppliers)
    return result


def rollback_metrics_backfill(
    db: Session,
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    _validate_manifest(manifest, expected_mode="apply")
    run_id = str(manifest["run_id"])
    order_entries = {int(item["order_id"]): dict(item) for item in manifest.get("orders", [])}
    by_order: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for item in manifest.get("items", []):
        by_order[int(item["order_id"])].append(dict(item))
    rolled_back_lines = 0
    rolled_back_orders = 0
    for order_id, entries in by_order.items():
        rollback_key = f"{run_id}:rollback:order:{order_id}"
        if db.scalar(
            select(ProcurementOrderFormationEvent).where(
                ProcurementOrderFormationEvent.idempotency_key == rollback_key
            )
        ):
            continue
        order = _get_order_for_update(db, order_id)
        order_entry = order_entries[order_id]
        if order.version != int(order_entry["applied_order_version"]):
            raise RuntimeError(f"order {order_id} version changed after metrics backfill")
        for entry in entries:
            line = _line_by_id(order, int(entry["line_id"]))
            if line.version != int(entry["applied_line_version"]):
                raise RuntimeError(f"line {line.id} version changed after metrics backfill")
            if _json_copy(line.payload or {}) != entry["after_payload"]:
                raise RuntimeError(f"line {line.id} payload changed after metrics backfill")
            line.payload = _json_copy(entry["before_payload"])
            line.version += 1
            rolled_back_lines += 1
        before_order_version = order.version
        order.version += 1
        rolled_back_orders += 1
        db.add(
            ProcurementOrderFormationEvent(
                order_id=order.id,
                entity_type="order",
                entity_id=str(order.id),
                event_type="procurement_order_metrics_backfill_rolled_back",
                actor="system:procurement-order-metrics",
                idempotency_key=rollback_key,
                before={"version": before_order_version},
                after={"version": order.version},
                payload={"source_run_id": run_id},
            )
        )

    rolled_back_profiles = 0
    for entry in manifest.get("suppliers", []):
        supplier = dict(entry)
        rollback_key = f"{run_id}:rollback:supplier:{supplier['supplier_ref']}"
        if db.scalar(
            select(ProcurementOrderFormationEvent).where(
                ProcurementOrderFormationEvent.idempotency_key == rollback_key
            )
        ):
            continue
        profile = db.scalar(
            select(ProcurementSupplierProfile)
            .where(ProcurementSupplierProfile.supplier_ref == supplier["supplier_ref"])
            .with_for_update()
        )
        if profile is None or profile.version != int(supplier["applied_profile_version"]):
            raise RuntimeError(
                f"supplier profile {supplier['supplier_ref']} changed after metrics backfill"
            )
        before = dict(supplier.get("before_profile") or {})
        before_version = profile.version
        if not supplier.get("profile_existed", True):
            db.delete(profile)
            rolled_back_profiles += 1
            db.add(
                ProcurementOrderFormationEvent(
                    order_id=None,
                    entity_type="supplier_profile",
                    entity_id=supplier["supplier_ref"],
                    event_type="supplier_profile_metrics_rolled_back",
                    actor="system:procurement-order-metrics",
                    idempotency_key=rollback_key,
                    before={"version": before_version},
                    after={"deleted": True},
                    payload={"source_run_id": run_id, "profile_created_by_run": True},
                )
            )
            continue
        for field_name in PROFILE_FACT_FIELDS:
            value = before.get(field_name)
            if field_name in {"supplier_defect_pct", "credit_limit"}:
                value = _decimal_or_none(value)
            elif field_name == "facts_updated_at":
                value = datetime.fromisoformat(value) if value else None
            elif field_name == "facts_payload":
                value = dict(value or {})
            setattr(profile, field_name, value)
        profile.version += 1
        rolled_back_profiles += 1
        db.add(
            ProcurementOrderFormationEvent(
                order_id=None,
                entity_type="supplier_profile",
                entity_id=profile.supplier_ref,
                event_type="supplier_profile_metrics_rolled_back",
                actor="system:procurement-order-metrics",
                idempotency_key=rollback_key,
                before={"version": supplier["applied_profile_version"]},
                after={"version": profile.version},
                payload={"source_run_id": run_id},
            )
        )
    db.flush()
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "run_id": run_id,
        "mode": "rollback",
        "rolled_back_at": datetime.now(UTC).isoformat(),
        "summary": {
            "rolled_back_lines": rolled_back_lines,
            "rolled_back_orders": rolled_back_orders,
            "rolled_back_supplier_profiles": rolled_back_profiles,
        },
    }


def _lead_time_payload(
    candidate: Mapping[str, Any] | None,
    *,
    source_level: str,
) -> dict[str, Any]:
    if not candidate:
        return {}
    prepare = _int_or_none(candidate.get("recommended_supplier_prepare_days"))
    logistics = _int_or_none(candidate.get("recommended_logistics_days"))
    if prepare is None or logistics is None:
        return {}
    return {
        "supplier_prepare_days": prepare,
        "logistics_days": logistics,
        "lead_time_days": prepare + logistics,
        "lead_time_source_level": source_level,
        "lead_time_confidence": _clean(candidate.get("lead_time_confidence")) or None,
        "lead_time_latest_supplier_order_at": _clean(candidate.get("latest_supplier_order_at"))
        or None,
        "lead_time_latest_cargo_handoff_at": _clean(candidate.get("latest_cargo_handoff_at"))
        or None,
        "lead_time_latest_receipt_at": _clean(candidate.get("latest_receipt_at")) or None,
    }


def _supplier_contract_terms_payload(
    terms: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    rows = [dict(row) for row in terms]
    facts_payload = {
        "contract_terms": [
            {
                "contract_ref": row.get("contract_ref"),
                "contract_code": row.get("contract_code"),
                "contract_name": row.get("contract_name"),
                "source_status": row.get("contract_source_status"),
            }
            for row in rows
        ]
    }
    if len(rows) != 1:
        return {
            "payment_terms": None,
            "credit_days": None,
            "credit_limit": None,
            "terms_source": "onec_contract",
            "terms_status": "missing",
            "facts_payload": facts_payload,
        }
    row = rows[0]
    return {
        "payment_terms": row.get("payment_terms"),
        "credit_days": row.get("credit_days"),
        "credit_limit": row.get("credit_limit"),
        "terms_source": "onec_contract",
        "terms_status": row.get("terms_status") or "missing",
        "facts_payload": facts_payload,
    }


def _merge_metrics_payload(
    payload: Mapping[str, Any],
    metrics: Mapping[str, Any],
) -> dict[str, Any]:
    result = {
        key: _json_copy(value) for key, value in payload.items() if key not in MANAGED_METRIC_KEYS
    }
    result.update({key: _json_copy(value) for key, value in metrics.items() if value is not None})
    return result


def _profile_snapshot(profile: ProcurementSupplierProfile | None) -> dict[str, Any]:
    if profile is None:
        return {
            **{field_name: None for field_name in PROFILE_FACT_FIELDS},
            "terms_source": "onec_contract",
            "terms_status": "missing",
            "facts_payload": {},
        }
    return {
        field_name: _json_copy(getattr(profile, field_name)) for field_name in PROFILE_FACT_FIELDS
    }


def _profile_facts_changed(
    profile: ProcurementSupplierProfile | None,
    facts: Mapping[str, Any],
) -> bool:
    if profile is None:
        return True
    for field_name in PROFILE_FACT_FIELDS:
        if field_name == "facts_updated_at":
            continue
        actual = getattr(profile, field_name)
        expected = facts.get(field_name)
        if field_name in {"supplier_defect_pct", "credit_limit"}:
            actual = _decimal_or_none(actual)
            expected = _decimal_or_none(expected)
        elif field_name in {
            "history_order_count",
            "supplier_prepare_days",
            "logistics_days",
            "lead_time_days",
            "price_history_count",
            "supplier_defect_history_units",
            "credit_days",
        }:
            actual = _int_or_none(actual)
            expected = _int_or_none(expected)
        elif field_name == "facts_payload":
            actual = dict(actual or {})
            expected = dict(expected or {})
        if actual != expected:
            return True
    return False


def _get_order_for_update(db: Session, order_id: int) -> ProcurementOrderFormation:
    order = db.scalar(
        select(ProcurementOrderFormation)
        .where(ProcurementOrderFormation.id == order_id)
        .options(selectinload(ProcurementOrderFormation.lines))
        .with_for_update()
    )
    if order is None:
        raise LookupError(f"order {order_id} was not found")
    return order


def _line_by_id(
    order: ProcurementOrderFormation,
    line_id: int,
) -> ProcurementOrderFormationLine:
    line = next((item for item in order.lines if item.id == line_id), None)
    if line is None:
        raise LookupError(f"line {line_id} was not found in order {order.id}")
    return line


def _order_is_open(order: ProcurementOrderFormation) -> bool:
    return (
        order.status in OPEN_ASSISTANT_STATUSES and order.onec_status not in IMMUTABLE_ONEC_STATUSES
    )


def _validate_manifest(manifest: Mapping[str, Any], *, expected_mode: str) -> None:
    if int(manifest.get("schema_version") or 0) != MANIFEST_SCHEMA_VERSION:
        raise ValueError("unsupported procurement metrics manifest schema")
    if manifest.get("mode") != expected_mode:
        raise ValueError(f"procurement metrics manifest mode must be {expected_mode}")
    if not str(manifest.get("run_id") or "").strip():
        raise ValueError("procurement metrics manifest run_id is required")


def _normalize_ref(value: Any) -> str:
    return _clean(value).lower()


def _normalize_currency(value: Any) -> str:
    normalized = "".join(character for character in _clean(value).upper() if character.isalnum())
    aliases = {"643": "RUB", "RUR": "RUB", "РУБ": "RUB"}
    return aliases.get(normalized, normalized)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _decimal_or_none(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _json_copy(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Mapping):
        return {str(key): _json_copy(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_copy(item) for item in value]
    return value
