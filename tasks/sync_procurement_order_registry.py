from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.infrastructure.db.session import get_application_session_factory
from app.models.procurement_order_formation import (
    ProcurementOrderFormation,
    ProcurementOrderFormationEvent,
    ProcurementOrderFormationLine,
)
from app.services.bitrix_order_formation import resolve_catalog_products_by_xml_ids
from app.services.procurement_order_formation import (
    PROCUREMENT_PROCESS_ENTITY_TYPE_ID,
    normalize_guid,
    serialize_linked_process,
)
from app.services.procurement_order_process_link import (
    ProcurementProcessCardSnapshot,
    reconcile_procurement_order_process_links,
    record_procurement_process_sync_failure,
)
from app.services.procurement_order_product_rows import (
    summarize_product_row_sync,
    sync_procurement_order_product_rows,
)
from app.services.procurement_order_registry import (
    decimal_value,
    lifecycle_status_for_snapshot,
    normalize_onec_ref,
    upsert_onec_order_snapshot,
)
from scripts.ensure_procurement_bitrix_process import (
    DEFAULT_ENV_FILE,
    DEFAULT_MAPPING_PATH,
    load_env,
)
from scripts.import_onec_supplier_order_to_procurement import load_mapping
from scripts.sync_open_cargo_supplier_orders_to_bitrix import (
    bitrix_webhook,
    fetch_open_supplier_orders,
    fetch_supplier_orders_by_refs,
    parse_contour_keys,
    run_bitrix_import,
    write_json,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RESULT_PATH = REPO_ROOT / "build/bitrix/procurement_order_registry_sync.json"


class ConfirmedProcurementProcessLinkError(RuntimeError):
    """A deterministic identity conflict that needs manual reconciliation."""


def _is_confirmed_process_error_text(value: Any) -> bool:
    message = str(value or "").casefold()
    return any(
        marker in message
        for marker in (
            "несколько bitrix-карточек",
            "несколько карточек смарт-процесса",
            "duplicate onec guid",
            "уже связана с заказом",
            "номер документа не совпадает",
            "дата документа не совпадает",
        )
    )


def is_confirmed_process_sync_failure(error: BaseException) -> bool:
    return isinstance(error, ConfirmedProcurementProcessLinkError) or (
        _is_confirmed_process_error_text(error)
    )


def _prepare_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    snapshot = dict(snapshot)
    snapshot["lifecycle_status"] = lifecycle_status_for_snapshot(snapshot)
    snapshot["received_qty"] = max(
        decimal_value(snapshot.get("ordered_qty")) - decimal_value(snapshot.get("open_qty")),
        0,
    )
    contour_key = str(snapshot.get("procurement_contour_key") or "ordinary")
    lifecycle = str(snapshot["lifecycle_status"])
    if lifecycle == "received":
        snapshot["procurement_stage_key"] = "closed"
    elif lifecycle == "partially_received":
        snapshot["procurement_stage_key"] = "receiving"
    elif lifecycle == "cancelled":
        snapshot["procurement_stage_key"] = {
            "ordinary": "cancelled",
            "cargo": "exception",
            "ved_import": "blocked",
        }.get(contour_key, "supplier_order")
    elif lifecycle == "in_transit":
        snapshot["procurement_stage_key"] = {
            "ordinary": "waiting_delivery",
            "cargo": "in_transit",
            "ved_import": "logistics_customs",
        }.get(contour_key, "supplier_order")
    return snapshot


def _process_cards_from_rows(
    result_rows: list[dict[str, Any]],
) -> list[ProcurementProcessCardSnapshot]:
    cards: list[ProcurementProcessCardSnapshot] = []
    for row in result_rows:
        if str(row.get("action") or "").strip() in {"blocked", "dry_run_update_or_create"}:
            continue
        item_id = str(row.get("item_id") or "").strip()
        onec_ref = normalize_onec_ref(row.get("onec_ref"))
        if not item_id or not onec_ref:
            continue
        raw_date = str(row.get("onec_date") or "").strip()[:10]
        try:
            onec_date = datetime.fromisoformat(raw_date).date() if raw_date else None
        except ValueError:
            onec_date = None
        cards.append(
            ProcurementProcessCardSnapshot(
                item_id=item_id,
                onec_ref=onec_ref,
                onec_number=str(row.get("source_number") or "").strip(),
                onec_date=onec_date,
                category_id=int(row["category_id"]) if row.get("category_id") else None,
                stage_id=str(row.get("stage_id") or "").strip(),
                stage_name=str(row.get("stage_name") or "").strip(),
                entity_type_id=int(row.get("entity_type_id") or 0),
            )
        )
    return cards


def _resolved_mapping_path(settings: Settings, mapping_path: Path | None) -> Path:
    path = mapping_path or Path(settings.procurement_labels_mapping_path)
    return path if path.is_absolute() else REPO_ROOT / path


def sync_onec_order_process_by_ref(
    db: Session,
    *,
    order_id: int,
    onec_ref: str,
    settings: Settings | None = None,
    webhook_base: str = "",
    mapping_path: Path | None = None,
    assigned_by_id: str = "130750",
    supplier_assigned_by_id: str = "",
    finance_user_id: str = "",
) -> dict[str, Any]:
    """Immediately refresh one 1C order and link its canonical process 1056."""

    settings = settings or get_settings()
    normalized_ref = normalize_onec_ref(onec_ref)
    if not re.fullmatch(r"0x[0-9a-f]{32}", normalized_ref):
        raise ConfirmedProcurementProcessLinkError("Некорректный GUID документа 1С")
    if not settings.onec_database_url:
        raise RuntimeError("ONEC_DATABASE_URL is not configured")
    snapshots = fetch_supplier_orders_by_refs(
        settings.onec_database_url,
        refs=[normalized_ref],
        contours=parse_contour_keys("ordinary,cargo,ved_import"),
    )
    if len(snapshots) != 1:
        raise RuntimeError(
            "Канонический snapshot заказа 1С ещё не доступен по GUID; повторит плановая синхронизация"
        )
    snapshot = _prepare_snapshot(snapshots[0])
    catalog_product_ids = _catalog_product_ids_for_snapshots(db, [snapshot])
    registry_result = upsert_onec_order_snapshot(
        db,
        snapshot,
        synced_at=datetime.now(UTC).replace(tzinfo=None),
        catalog_product_ids=catalog_product_ids,
    )
    if registry_result.conflict:
        raise ConfirmedProcurementProcessLinkError(registry_result.conflict)
    if registry_result.order_id != order_id:
        raise ConfirmedProcurementProcessLinkError(
            f"GUID документа 1С связан с другой записью заказа #{registry_result.order_id}"
        )
    db.commit()

    resolved_webhook = str(
        webhook_base
        or settings.procurement_bitrix_webhook_url
        or settings.bitrix_box_webhook_base
        or ""
    ).strip()
    if not resolved_webhook:
        raise RuntimeError("PROCUREMENT_BITRIX_WEBHOOK_URL is not configured")
    mapping = load_mapping(_resolved_mapping_path(settings, mapping_path))
    mapping_entity_type_id = int((mapping.get("process") or {}).get("entity_type_id") or 0)
    if mapping_entity_type_id != PROCUREMENT_PROCESS_ENTITY_TYPE_ID:
        raise ConfirmedProcurementProcessLinkError(
            f"Настроен неподдерживаемый Smart Process {mapping_entity_type_id}; требуется 1056"
        )
    rows = run_bitrix_import(
        [snapshot],
        webhook_base=resolved_webhook,
        mapping=mapping,
        apply=True,
        supplier_assigned_by_id=supplier_assigned_by_id or assigned_by_id,
        finance_user_id=finance_user_id,
    )
    blocked = next(
        (row for row in rows if str(row.get("action") or "").strip() == "blocked"),
        None,
    )
    if blocked:
        error = str(blocked.get("error") or "Синхронизация Smart Process заблокирована")
        if _is_confirmed_process_error_text(error):
            raise ConfirmedProcurementProcessLinkError(error)
        raise RuntimeError(error)
    cards = _process_cards_from_rows(rows)
    if len(cards) != 1:
        raise RuntimeError("Bitrix24 не вернул созданную или найденную карточку процесса")
    reconciliation = reconcile_procurement_order_process_links(
        db,
        cards,
        actor="system:procurement-process-immediate-sync",
        mark_missing=False,
    )
    db.commit()
    order = db.get(ProcurementOrderFormation, order_id)
    if order is None:
        raise LookupError("order formation card was not found")
    product_rows = sync_procurement_order_product_rows(
        db,
        order,
        apply=True,
        settings=settings,
        webhook_base=resolved_webhook,
        actor="system:procurement-process-immediate-sync",
    )
    db.commit()
    state = serialize_linked_process(order)["state"]
    return {
        "order_id": order_id,
        "onec_ref": normalized_ref,
        "state": state,
        "item_id": order.bitrix_item_id if state == "linked" else None,
        "reconciliation": reconciliation,
        "product_rows_sync": product_rows,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Synchronize the unified order registry from 1C")
    parser.add_argument("--contours", default="ordinary,cargo,ved_import")
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--source-only",
        action="store_true",
        help="Validate and report the 1C read-only source without opening the application DB.",
    )
    parser.add_argument("--sync-bitrix", action="store_true")
    parser.add_argument(
        "--assigned-by-id",
        default="",
        help="Deprecated compatibility alias for --supplier-assigned-by-id.",
    )
    parser.add_argument("--supplier-assigned-by-id", default="")
    parser.add_argument("--finance-user-id", default="")
    parser.add_argument("--webhook-url", default="")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--mapping-path", type=Path, default=DEFAULT_MAPPING_PATH)
    parser.add_argument("--result-path", type=Path, default=DEFAULT_RESULT_PATH)
    return parser.parse_args(argv)


def _known_refs() -> list[str]:
    session = get_application_session_factory()()
    try:
        values = [
            str(value)
            for value in session.scalars(
                select(ProcurementOrderFormation.onec_document_ref).where(
                    ProcurementOrderFormation.onec_document_ref.is_not(None),
                    ProcurementOrderFormation.lifecycle_status.not_in(("received", "cancelled")),
                )
            ).all()
            if str(value or "").strip()
        ]
        return [value for value in values if re.fullmatch(r"0x[0-9a-fA-F]{32}", value.strip())]
    finally:
        session.close()


def _catalog_product_ids_for_snapshots(session, snapshots: list[dict[str, Any]]) -> dict[str, str]:
    refs = {
        normalize_onec_ref(line.get("item_ref_hex") or line.get("nomenclature_ref"))
        for snapshot in snapshots
        for line in snapshot.get("lines") or []
        if str(line.get("item_ref_hex") or line.get("nomenclature_ref") or "").strip()
    }
    known = {
        normalize_onec_ref(nomenclature_ref): str(product_id)
        for nomenclature_ref, product_id in session.execute(
            select(
                ProcurementOrderFormationLine.nomenclature_ref,
                ProcurementOrderFormationLine.bitrix_product_id,
            ).where(
                ProcurementOrderFormationLine.nomenclature_ref.in_(refs),
                ProcurementOrderFormationLine.bitrix_product_id.is_not(None),
            )
        ).all()
        if str(product_id or "").strip()
    }
    unresolved_refs = refs - known.keys()
    if not unresolved_refs:
        return known

    resolved = resolve_catalog_products_by_xml_ids(sorted(unresolved_refs))
    known.update(
        {
            raw_ref: product.product_id
            for raw_ref in unresolved_refs
            if (product := resolved.get(normalize_guid(raw_ref))) is not None and product.product_id
        }
    )
    return known


def _persist_snapshots(snapshots: list[dict[str, Any]], *, apply: bool) -> list[dict[str, Any]]:
    session = get_application_session_factory()()
    try:
        catalog_product_ids = _catalog_product_ids_for_snapshots(session, snapshots)
        synced_at = datetime.now(UTC).replace(tzinfo=None)
        rows = []
        for snapshot in snapshots:
            result = upsert_onec_order_snapshot(
                session,
                snapshot,
                synced_at=synced_at,
                catalog_product_ids=catalog_product_ids,
            )
            rows.append(
                {
                    "action": result.action,
                    "order_id": result.order_id,
                    "onec_ref": result.onec_ref,
                    "lifecycle_status": result.lifecycle_status,
                    "conflict": result.conflict,
                    "source_number": str(snapshot.get("number") or ""),
                }
            )
        if apply:
            session.commit()
        else:
            session.rollback()
        return rows
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def _store_bitrix_links(
    result_rows: list[dict[str, Any]],
    *,
    settings: Settings,
    webhook_base: str,
) -> tuple[dict[str, int], list[dict[str, Any]]]:
    session = get_application_session_factory()()
    try:
        summary = reconcile_procurement_order_process_links(
            session,
            _process_cards_from_rows(result_rows),
            actor="system:onec-procurement-registry",
            mark_missing=False,
        )
        for row in result_rows:
            if str(row.get("action") or "").strip() != "blocked":
                continue
            ref = normalize_onec_ref(row.get("onec_ref"))
            order = session.scalar(
                select(ProcurementOrderFormation).where(
                    func.lower(ProcurementOrderFormation.onec_document_ref) == ref
                )
            )
            if order is None:
                continue
            error = str(row.get("error") or "Синхронизация Smart Process заблокирована")
            record_procurement_process_sync_failure(
                session,
                order.id,
                error,
                confirmed_broken=_is_confirmed_process_error_text(error),
                actor="system:onec-procurement-registry",
            )
        synced_refs = {
            normalize_onec_ref(row.get("onec_ref"))
            for row in result_rows
            if str(row.get("action") or "").strip() != "blocked"
        }
        orders = list(
            session.scalars(
                select(ProcurementOrderFormation).where(
                    func.lower(ProcurementOrderFormation.onec_document_ref).in_(synced_refs)
                )
            ).all()
        )
        product_rows = [
            sync_procurement_order_product_rows(
                session,
                order,
                apply=True,
                settings=settings,
                webhook_base=webhook_base,
                actor="system:onec-procurement-registry",
            )
            for order in orders
        ]
        session.commit()
        return summary, product_rows
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def _store_missing_conflicts(refs: list[str]) -> None:
    if not refs:
        return
    session = get_application_session_factory()()
    try:
        orders = list(
            session.scalars(
                select(ProcurementOrderFormation).where(
                    func.lower(ProcurementOrderFormation.onec_document_ref).in_(refs)
                )
            ).all()
        )
        synced_at = datetime.now(UTC).replace(tzinfo=None)
        for order in orders:
            message = "Документ не найден по сохранённому GUID 1С; статус не изменён"
            order.last_onec_sync_at = synced_at
            if order.sync_conflict == message:
                continue
            order.sync_conflict = message
            session.add(
                ProcurementOrderFormationEvent(
                    order_id=order.id,
                    entity_type="order",
                    entity_id=str(order.id),
                    event_type="onec_order_reconciliation_required",
                    actor="system:onec-procurement-registry",
                    before={"lifecycle_status": order.lifecycle_status},
                    after={"lifecycle_status": order.lifecycle_status, "sync_conflict": message},
                    payload={"onec_ref": order.onec_document_ref},
                )
            )
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def run(args: argparse.Namespace) -> dict[str, Any]:
    settings = get_settings()
    if not settings.onec_database_url:
        raise RuntimeError("ONEC_DATABASE_URL is not configured")
    contours = parse_contour_keys(args.contours)
    known_refs = [] if args.source_only else _known_refs()
    open_orders = fetch_open_supplier_orders(
        settings.onec_database_url,
        limit=args.limit,
        date_from="",
        date_to="",
        contours=contours,
        filter_contours_in_sql=True,
        fail_on_query_limit=True,
    )
    refreshed = fetch_supplier_orders_by_refs(
        settings.onec_database_url,
        refs=known_refs,
        contours=contours,
    )
    snapshots_by_ref = {
        str(item.get("onec_ref") or "").strip().lower(): item
        for item in [*open_orders, *refreshed]
        if str(item.get("onec_ref") or "").strip()
    }
    snapshots = list(snapshots_by_ref.values())
    missing_refs = sorted(set(value.lower() for value in known_refs) - set(snapshots_by_ref))
    snapshots = [_prepare_snapshot(snapshot) for snapshot in snapshots]
    registry_rows = (
        [] if args.source_only else _persist_snapshots(snapshots, apply=bool(args.apply))
    )
    lifecycle_by_ref = {
        str(row.get("onec_ref") or "").lower(): str(row.get("lifecycle_status") or "")
        for row in registry_rows
        if row.get("lifecycle_status")
    }
    for snapshot in snapshots:
        resolved_lifecycle = lifecycle_by_ref.get(str(snapshot.get("onec_ref") or "").lower())
        if resolved_lifecycle:
            snapshot["lifecycle_status"] = resolved_lifecycle
            contour_key = str(snapshot.get("procurement_contour_key") or "ordinary")
            if resolved_lifecycle == "received":
                snapshot["procurement_stage_key"] = "closed"
            elif resolved_lifecycle == "partially_received":
                snapshot["procurement_stage_key"] = "receiving"
            elif resolved_lifecycle == "cancelled":
                snapshot["procurement_stage_key"] = {
                    "ordinary": "cancelled",
                    "cargo": "exception",
                    "ved_import": "blocked",
                }.get(contour_key, "supplier_order")
            elif resolved_lifecycle == "in_transit":
                snapshot["procurement_stage_key"] = {
                    "ordinary": "waiting_delivery",
                    "cargo": "in_transit",
                    "ved_import": "logistics_customs",
                }.get(contour_key, "supplier_order")
    if args.apply and not args.source_only:
        _store_missing_conflicts(missing_refs)

    bitrix_rows: list[dict[str, Any]] = []
    product_row_results: list[dict[str, Any]] = []
    link_reconciliation = {"checked": 0, "linked": 0, "unchanged": 0, "broken": 0}
    if args.sync_bitrix and not args.source_only:
        webhook = bitrix_webhook(args, load_env(args.env_file))
        if not webhook:
            raise RuntimeError("PROCUREMENT_BITRIX_WEBHOOK_URL is not configured")
        mapping = load_mapping(args.mapping_path)
        mapping_entity_type_id = int((mapping.get("process") or {}).get("entity_type_id") or 0)
        if mapping_entity_type_id != PROCUREMENT_PROCESS_ENTITY_TYPE_ID:
            raise RuntimeError(
                f"Unsupported procurement Smart Process {mapping_entity_type_id}; expected 1056"
            )
        bitrix_rows = run_bitrix_import(
            snapshots,
            webhook_base=webhook,
            mapping=mapping,
            apply=bool(args.apply),
            supplier_assigned_by_id=str(args.supplier_assigned_by_id or args.assigned_by_id),
            finance_user_id=str(args.finance_user_id),
        )
        if args.apply:
            link_reconciliation, product_row_results = _store_bitrix_links(
                bitrix_rows,
                settings=settings,
                webhook_base=webhook,
            )

    return {
        "mode": "apply" if args.apply else "dry-run",
        "summary": {
            "snapshots": len(snapshots),
            "open_orders": len(open_orders),
            "known_ref_refreshes": len(refreshed),
            "missing_known_refs": len(missing_refs),
            "registry_actions": dict(Counter(row["action"] for row in registry_rows)),
            "bitrix_actions": dict(
                Counter(
                    str(row.get("would_action") or row.get("action") or "") for row in bitrix_rows
                )
            ),
            "bitrix_blocked": sum(
                1 for row in bitrix_rows if str(row.get("action") or "") == "blocked"
            ),
            "product_rows": summarize_product_row_sync(product_row_results),
        },
        "missing_refs": missing_refs,
        "registry_rows": registry_rows,
        "bitrix_rows": bitrix_rows,
        "product_row_results": product_row_results,
        "link_reconciliation": link_reconciliation,
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = run(args)
    write_json(args.result_path, result)
    print(
        json.dumps(
            {"mode": result["mode"], "summary": result["summary"]}, ensure_ascii=False, indent=2
        )
    )
    conflicts = int(result["summary"]["registry_actions"].get("conflict", 0))
    blocked = int(result["summary"].get("bitrix_blocked", 0))
    missing = int(result["summary"].get("missing_known_refs", 0))
    return 2 if conflicts or blocked or missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
