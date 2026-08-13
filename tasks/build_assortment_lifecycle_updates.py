from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.services.assortment_lifecycle import (
    ASSORTMENT_STATUS_LABELS,
    AssortmentLifecycleDecision,
    AssortmentLifecycleInput,
    AssortmentStatus,
    CommercialMarksDecision,
    CommercialMarksInput,
    ExpensiveProfileDecision,
    ExpensiveProfileInput,
    ManagerNeedSignal,
    WarehouseSalesPointInput,
    build_commercial_mark_property_update_rows,
    build_procurement_profile_property_update_row,
    build_status_property_update_rows,
    classify_expensive_profile,
    decide_commercial_marks,
    decide_legacy_assortment_status,
    decide_target_assortment_status,
    systemic_sales_point_codes,
    validate_manager_need_signal,
)
from app.services.assortment_lifecycle_v2_policy import (
    DEFAULT_ASSORTMENT_LIFECYCLE_V2_POLICY_PATH,
    AssortmentLifecycleV2Policy,
    load_assortment_lifecycle_v2_policy,
)
from app.services.exporters.ut103_exchange import load_ut103_env_file, resolve_ut103_exchange_root
from app.services.exporters.ut103_nomenclature_properties import (
    DEFAULT_SOURCE,
    NomenclaturePropertyUpdateMessage,
    NomenclaturePropertyUpdateRow,
    build_nomenclature_property_updates_xml,
    write_nomenclature_property_updates_message,
)

DEFAULT_TASK_SOURCE = os.environ.get(
    "UT103_NOMENCLATURE_PROPERTIES_SOURCE",
    DEFAULT_SOURCE,
)
DISPLAY_SCOPE_MARKERS = ("диспле", "матриц")


def main() -> int:
    load_ut103_env_file()
    args = _parse_args()
    if args.print_xml or args.write_ready:
        raise SystemExit(
            "Lifecycle property export to UT 10.3 is retired; "
            "use pricing-service classification output"
        )
    records = _load_records(args.input_json)
    v2_policy = load_assortment_lifecycle_v2_policy(args.v2_policy_json)
    rows, items = build_updates_from_records(
        records,
        folder_filter=args.folder,
        changed_at=args.changed_at,
        source=args.source,
        suspicious_quantity_threshold=args.suspicious_quantity_threshold,
        model_version=args.model_version,
        v2_policy=v2_policy,
    )
    message_id = args.message_id or (
        f"assortment-lifecycle-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    )

    output_path: Path | None = None
    if args.output_json:
        _write_rows_json(args.output_json, rows)

    if args.print_xml or args.write_ready:
        message = _build_message(args, message_id, rows)
        if args.print_xml:
            print(build_nomenclature_property_updates_xml(message).decode("windows-1251"))
        if args.write_ready:
            try:
                exchange_root = resolve_ut103_exchange_root(args.exchange_root)
            except ValueError as exc:
                raise SystemExit(str(exc)) from exc
            output_path = write_nomenclature_property_updates_message(
                exchange_root,
                message,
                overwrite=args.overwrite,
            )

    if args.json:
        print(
            json.dumps(
                {
                    "message_id": message_id,
                    "mode": args.mode,
                    "items": items,
                    "rows": len(rows),
                    "output_json": str(args.output_json) if args.output_json else None,
                    "path": str(output_path) if output_path else None,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    elif not args.print_xml:
        print(f"items={len(items)} rows={len(rows)}")
        if args.output_json:
            print(args.output_json)
        if output_path:
            print(output_path)
    return 0


def build_updates_from_records(
    records: list[dict[str, Any]],
    *,
    folder_filter: str = "",
    changed_at: date | None = None,
    source: str = DEFAULT_TASK_SOURCE,
    suspicious_quantity_threshold: Decimal | int | str | None = None,
    model_version: str = "v2-shadow",
    v2_policy: AssortmentLifecycleV2Policy | None = None,
) -> tuple[list[NomenclaturePropertyUpdateRow], list[dict[str, Any]]]:
    v2_policy = v2_policy or load_assortment_lifecycle_v2_policy()
    rows: list[NomenclaturePropertyUpdateRow] = []
    summaries: list[dict[str, Any]] = []
    for record in records:
        if folder_filter and not _matches_folder(record, folder_filter):
            continue
        lifecycle_input = _lifecycle_input_from_record(record)
        legacy_decision = decide_legacy_assortment_status(lifecycle_input)
        legacy_decision = _fact_status_decision_from_record(record, legacy_decision)
        target_decision = decide_target_assortment_status(
            lifecycle_input,
            demand_policy=v2_policy.demand,
        )
        if model_version not in {"v1", "v2-shadow", "v2-live"}:
            raise ValueError(f"unsupported assortment lifecycle model: {model_version}")
        status_decision = target_decision if model_version == "v2-live" else legacy_decision
        commercial_decision = decide_commercial_marks(_commercial_marks_input_from_record(record))
        profile_decision = _profile_decision_from_record(record)
        sales_point_codes = systemic_sales_point_codes(_warehouses_from_record(record))
        signal_summaries = _manager_signal_summaries(record, suspicious_quantity_threshold)

        status_export_blockers = _status_export_blockers(status_decision)
        commercial_export_blockers = _commercial_export_blockers(commercial_decision)
        export_blockers = (*status_export_blockers, *commercial_export_blockers)
        if not status_export_blockers:
            rows.extend(
                build_status_property_update_rows(
                    status_decision,
                    source=source,
                    changed_at=changed_at,
                )
            )

        if not commercial_export_blockers:
            rows.extend(
                build_commercial_mark_property_update_rows(
                    commercial_decision,
                    changed_at=changed_at,
                )
            )

        if profile_decision is not None:
            profile_row = build_procurement_profile_property_update_row(
                lifecycle_input.nomenclature_code,
                profile_decision,
                changed_at=changed_at,
                approved_by=str(_optional_field(record, "folder_responsible", default="")),
            )
            if profile_row is not None:
                rows.append(profile_row)

        summaries.append(
            _item_summary(
                record,
                status_decision,
                commercial_decision,
                profile_decision,
                sales_point_codes,
                signal_summaries,
                export_blockers,
                legacy_decision=legacy_decision,
                target_decision=target_decision,
                model_version=model_version,
            )
        )
    return rows, summaries


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build dry-run nomenclature_property_updates.v1 rows for assortment lifecycle "
            "statuses and procurement behavior profiles."
        )
    )
    parser.add_argument("--input-json", type=Path, required=True, help="JSON list or {items:[...]}")
    parser.add_argument("--folder", default="", help="Optional folder/path filter, e.g. дисплеи")
    parser.add_argument("--message-id", help="Stable idempotency key for this package")
    parser.add_argument("--mode", choices=("dry_run", "apply"), default="dry_run")
    parser.add_argument("--approved-by", default="", help="Required for apply mode")
    parser.add_argument("--source", default=DEFAULT_TASK_SOURCE)
    parser.add_argument(
        "--model-version",
        choices=("v1", "v2-shadow", "v2-live"),
        default="v2-shadow",
        help=(
            "v2-shadow calculates the accepted v2 target while keeping the persisted "
            "legacy stage; v2-live requires separately approved live_enabled=true"
        ),
    )
    parser.add_argument(
        "--v2-policy-json",
        type=Path,
        default=DEFAULT_ASSORTMENT_LIFECYCLE_V2_POLICY_PATH,
    )
    parser.add_argument("--changed-at", type=_parse_date, default=None)
    parser.add_argument("--suspicious-quantity-threshold", default=None)
    parser.add_argument("--output-json", type=Path, help="Write rows JSON for export task")
    parser.add_argument("--print-xml", action="store_true", help="Print dry-run XML")
    parser.add_argument(
        "--write-ready", action="store_true", help="Write ready XML to UT103 exchange"
    )
    parser.add_argument("--exchange-root", help="UT103 exchange root for --write-ready")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--json", action="store_true", help="Print machine-readable summary")
    args = parser.parse_args()
    if not (args.output_json or args.print_xml or args.write_ready or args.json):
        args.json = True
    return args


def _build_message(
    args: argparse.Namespace,
    message_id: str,
    rows: list[NomenclaturePropertyUpdateRow],
) -> NomenclaturePropertyUpdateMessage:
    if not rows:
        raise SystemExit("No property update rows built; nothing to export")
    return NomenclaturePropertyUpdateMessage(
        message_id=message_id,
        rows=tuple(rows),
        mode=args.mode,
        approved_by=args.approved_by,
        source=args.source,
    )


def _load_records(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    items = payload.get("items") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise SystemExit("JSON must be a list or an object with an items list")
    if not all(isinstance(item, dict) for item in items):
        raise SystemExit("Every item must be an object")
    return items


def _lifecycle_input_from_record(record: dict[str, Any]) -> AssortmentLifecycleInput:
    manual_status = _optional_field(record, "manual_status", "ManualStatus", default=None)
    if _is_legacy_exclusive_manual_status(manual_status):
        manual_status = None
    return AssortmentLifecycleInput(
        nomenclature_code=str(_field(record, "nomenclature_code", "NomenclatureCode")),
        created_at=_optional_date_field(record, "created_at", "CreatedAt"),
        first_supplier_order_at=_optional_date_field(
            record,
            "first_supplier_order_at",
            "FirstSupplierOrderAt",
        ),
        supplier_order_cargo_handoff_dates=_date_tuple(
            _optional_field(
                record,
                "supplier_order_cargo_handoff_dates",
                "cargo_handoff_dates",
                "SupplierOrderCargoHandoffDates",
                default=[],
            )
        ),
        receipt_dates=_date_tuple(
            _optional_field(record, "receipt_dates", "ReceiptDates", default=[])
        ),
        first_receipt_at=_optional_date_field(record, "first_receipt_at", "FirstReceiptAt"),
        last_receipt_at=_optional_date_field(record, "last_receipt_at", "LastReceiptAt"),
        first_stock_inflow_at=_optional_date_field(
            record, "first_stock_inflow_at", "FirstStockInflowAt"
        ),
        last_stock_inflow_at=_optional_date_field(
            record, "last_stock_inflow_at", "LastStockInflowAt"
        ),
        first_sale_at=_optional_date_field(record, "first_sale_at", "FirstSaleAt"),
        last_sale_at=_optional_date_field(record, "last_sale_at", "LastSaleAt"),
        # Дата расчёта приходит из факта (сборщик проставляет as_of при выгрузке).
        # Без неё правило «Родился мёртвым» просто не сработает — статус не поедет.
        as_of=_optional_date_field(record, "as_of", "AsOf"),
        # Спрос по окнам 30/90/180 и дни наличия за те же окна: на них держатся
        # переходы «Пошли продажи -> Растим -> Поддерживаем». Полей нет —
        # формула сама откатится на прежнюю, поставочную логику.
        sales_qty_short=_optional_field(record, "sales_qty_short", "SalesQtyShort", default=None),
        sales_qty_medium=_optional_field(
            record, "sales_qty_medium", "SalesQtyMedium", default=None
        ),
        sales_qty_long=_optional_field(record, "sales_qty_long", "SalesQtyLong", default=None),
        lifetime_sales_qty=_optional_field(
            record, "lifetime_sales_qty", "LifetimeSalesQty", default=None
        ),
        days_in_sale_short=_optional_field(
            record, "days_in_sale_short", "DaysInSaleShort", default=None
        ),
        days_in_sale_medium=_optional_field(
            record, "days_in_sale_medium", "DaysInSaleMedium", default=None
        ),
        days_in_sale_long=_optional_field(
            record, "days_in_sale_long", "DaysInSaleLong", default=None
        ),
        previous_status=_optional_field(record, "previous_status", "PreviousStatus", default=None),
        previous_demand_state=_optional_field(
            record, "previous_demand_state", "PreviousDemandState", default=None
        ),
        demand_state_since=_optional_date_field(record, "demand_state_since", "DemandStateSince"),
        previous_demand_state_at=_optional_date_field(
            record, "previous_demand_state_at", "PreviousDemandStateAt"
        ),
        sales_active_days_short=_optional_int_field(
            record, "sales_active_days_short", "SalesActiveDaysShort"
        ),
        sales_document_count_short=_optional_int_field(
            record, "sales_document_count_short", "SalesDocumentCountShort"
        ),
        sales_customer_count_short=_optional_int_field(
            record, "sales_customer_count_short", "SalesCustomerCountShort"
        ),
        sales_point_count_short=_optional_int_field(
            record, "sales_point_count_short", "SalesPointCountShort"
        ),
        sales_max_day_share_short=_optional_field(
            record, "sales_max_day_share_short", "SalesMaxDayShareShort", default=None
        ),
        has_need_signal=_bool_field(record, "has_need_signal", "HasNeedSignal", default=False),
        has_external_need_signal=_optional_bool_field(
            record, "has_external_need_signal", "HasExternalNeedSignal"
        ),
        working_confirmed_by_folder_responsible=_bool_field(
            record,
            "working_confirmed_by_folder_responsible",
            "WorkingConfirmedByFolderResponsible",
            default=False,
        ),
        analog_winner_confirmed_by_folder_responsible=_bool_field(
            record,
            "analog_winner_confirmed_by_folder_responsible",
            "AnalogWinnerConfirmedByFolderResponsible",
            default=False,
        ),
        manual_status=manual_status,
        manual_reason=str(_optional_field(record, "manual_reason", "ManualReason", default="")),
        manual_approved_by=str(
            _optional_field(record, "manual_approved_by", "ManualApprovedBy", default="")
        ),
        manual_changed_at=_optional_date_field(record, "manual_changed_at", "ManualChangedAt"),
        exclusive_min_stock_qty=_optional_field(
            record,
            "exclusive_min_stock_qty",
            "ExclusiveMinStockQty",
            default=None,
        ),
        exclusive_review_period_days=int(
            _optional_field(
                record,
                "exclusive_review_period_days",
                "ExclusiveReviewPeriodDays",
                default=30,
            )
        ),
    )


def _fact_status_decision_from_record(
    record: dict[str, Any],
    fallback: AssortmentLifecycleDecision,
) -> AssortmentLifecycleDecision:
    raw = _optional_field(record, "fact_status_decision", "FactStatusDecision", default=None)
    if raw in (None, ""):
        return fallback
    if not isinstance(raw, dict):
        raise SystemExit("fact_status_decision must be an object")

    raw_status = str(
        _optional_field(raw, "target_status", "TargetStatus", "status", "Status", default="")
    ).strip()
    if not raw_status:
        return fallback
    try:
        status = AssortmentStatus(raw_status)
    except ValueError as exc:
        raise SystemExit(f"unsupported fact_status_decision target_status: {raw_status}") from exc

    relation = str(
        _optional_field(
            raw,
            "fact_lifecycle_relation",
            "FactLifecycleRelation",
            default="fact_status_decision",
        )
    ).strip()
    reason = str(
        _optional_field(
            raw,
            "reason",
            "Reason",
            default=f"Статус задан реестром решений по фактам: {ASSORTMENT_STATUS_LABELS[status]}.",
        )
    )
    approved_by = str(_optional_field(raw, "approved_by", "ApprovedBy", default=""))
    changed_at = _optional_date_from_value(
        _optional_field(raw, "decided_at", "DecidedAt", "changed_at", "ChangedAt", default=None)
    )

    reason_codes = tuple(
        code
        for code in ("fact_status_decision", relation)
        if code and code != "fact_status_decision"
    )
    if not reason_codes:
        reason_codes = ("fact_status_decision",)
    else:
        reason_codes = ("fact_status_decision", *reason_codes)

    return AssortmentLifecycleDecision(
        nomenclature_code=fallback.nomenclature_code,
        status=status,
        status_label=ASSORTMENT_STATUS_LABELS[status],
        reason_codes=reason_codes,
        reason_text=reason,
        manual_review_required=status in {AssortmentStatus.NEWBORN},
        auto_order_allowed=False,
        blockers=("ut103_export_blocked", "fact_status_decision_requires_1c_approval"),
        changed_at=changed_at,
        approved_by=approved_by,
    )


def _commercial_marks_input_from_record(record: dict[str, Any]) -> CommercialMarksInput:
    marks = _commercial_marks_from_record(record)
    legacy_exclusive = _is_legacy_exclusive_manual_status(
        _optional_field(record, "manual_status", "ManualStatus", default=None)
    )
    if legacy_exclusive and "exclusive" not in marks:
        marks = (*marks, "exclusive")
    return CommercialMarksInput(
        nomenclature_code=str(_field(record, "nomenclature_code", "NomenclatureCode")),
        commercial_marks=marks,
        exclusive_kind=str(_optional_field(record, "exclusive_kind", "ExclusiveKind", default="")),
        exclusive_confidence=str(
            _optional_field(record, "exclusive_confidence", "ExclusiveConfidence", default="")
        ),
        exclusive_checked_at=(
            _optional_date_field(record, "exclusive_checked_at", "ExclusiveCheckedAt")
            or (
                _optional_date_field(record, "manual_changed_at", "ManualChangedAt")
                if legacy_exclusive
                else None
            )
        ),
        exclusive_review_at=_optional_date_field(
            record,
            "exclusive_review_at",
            "ExclusiveReviewAt",
        ),
        exclusive_review_period_days=int(
            _optional_field(
                record,
                "exclusive_review_period_days",
                "ExclusiveReviewPeriodDays",
                default=30,
            )
        ),
        exclusive_reason=str(
            _optional_field(
                record,
                "exclusive_reason",
                "ExclusiveReason",
                default=(
                    _optional_field(record, "manual_reason", "ManualReason", default="")
                    if legacy_exclusive
                    else ""
                ),
            )
        ),
        exclusive_approved_by=str(
            _optional_field(
                record,
                "exclusive_approved_by",
                "ExclusiveApprovedBy",
                default=(
                    _optional_field(record, "manual_approved_by", "ManualApprovedBy", default="")
                    if legacy_exclusive
                    else ""
                ),
            )
        ),
        exclusive_evidence_refs=_text_tuple(
            _optional_field(
                record,
                "exclusive_evidence_refs",
                "ExclusiveEvidenceRefs",
                default=[],
            )
        ),
        exclusive_min_stock_qty=_optional_field(
            record,
            "exclusive_min_stock_qty",
            "ExclusiveMinStockQty",
            default=None,
        ),
    )


def _profile_decision_from_record(record: dict[str, Any]) -> ExpensiveProfileDecision | None:
    manual_profile = _optional_field(
        record,
        "manual_expensive_profile",
        "manual_profile",
        "ManualExpensiveProfile",
        default=None,
    )
    item_value = _optional_field(
        record,
        "expensive_item_value",
        "item_value",
        "ExpensiveItemValue",
        default=None,
    )
    group_values = _optional_field(
        record,
        "expensive_group_values",
        "group_values",
        "ExpensiveGroupValues",
        default=[],
    )
    if manual_profile is None and item_value is None:
        return None
    if item_value is None:
        item_value = 0
    if manual_profile is None and not group_values:
        raise SystemExit("expensive_group_values is required when expensive_item_value is provided")
    route_days = _optional_field(
        record,
        "expensive_route_days",
        "route_days",
        "ExpensiveRouteDays",
        default=None,
    )
    return classify_expensive_profile(
        ExpensiveProfileInput(
            item_value=item_value,
            group_values=tuple(group_values or [item_value]),
            route_days=int(route_days) if route_days not in (None, "") else None,
            manual_profile=manual_profile,
        )
    )


def _warehouses_from_record(record: dict[str, Any]) -> tuple[WarehouseSalesPointInput, ...]:
    raw_warehouses = _optional_field(record, "warehouses", "Warehouses", default=[])
    if not isinstance(raw_warehouses, list):
        raise SystemExit("warehouses must be a list")
    warehouses: list[WarehouseSalesPointInput] = []
    for raw in raw_warehouses:
        if not isinstance(raw, dict):
            raise SystemExit("warehouse item must be an object")
        warehouses.append(
            WarehouseSalesPointInput(
                warehouse_code=str(_field(raw, "warehouse_code", "code", "WarehouseCode")),
                sells_systematically=_bool_field(
                    raw, "sells_systematically", "SellsSystematically", default=True
                ),
                is_central=_bool_field(raw, "is_central", "IsCentral", default=False),
                is_defect_warehouse=_bool_field(
                    raw,
                    "is_defect_warehouse",
                    "IsDefectWarehouse",
                    default=False,
                ),
                is_transit=_bool_field(raw, "is_transit", "IsTransit", default=False),
                is_non_systematic_sale=_bool_field(
                    raw,
                    "is_non_systematic_sale",
                    "IsNonSystematicSale",
                    default=False,
                ),
            )
        )
    return tuple(warehouses)


def _manager_signal_summaries(
    record: dict[str, Any],
    suspicious_quantity_threshold: Decimal | int | str | None,
) -> list[dict[str, Any]]:
    raw_signals = _optional_field(record, "manager_need_signals", "ManagerNeedSignals", default=[])
    if not isinstance(raw_signals, list):
        raise SystemExit("manager_need_signals must be a list")
    summaries: list[dict[str, Any]] = []
    for raw in raw_signals:
        if not isinstance(raw, dict):
            raise SystemExit("manager signal item must be an object")
        signal = ManagerNeedSignal(
            nomenclature_code=str(
                _optional_field(
                    raw,
                    "nomenclature_code",
                    "NomenclatureCode",
                    default=_field(record, "nomenclature_code", "NomenclatureCode"),
                )
            ),
            manager_id=str(_field(raw, "manager_id", "ManagerId")),
            quantity=_field(raw, "quantity", "Quantity"),
            source=str(_field(raw, "source", "Source")),
            signal_date=_parse_date(str(_field(raw, "signal_date", "SignalDate"))),
            comment=str(_optional_field(raw, "comment", "Comment", default="")),
        )
        decision = validate_manager_need_signal(
            signal,
            suspicious_quantity_threshold=suspicious_quantity_threshold,
        )
        summaries.append(
            {
                "manager_id": signal.manager_id,
                "quantity": _json_value(decision.normalized_quantity),
                "source": signal.source,
                "accepted": decision.accepted,
                "suspicious": decision.suspicious,
                "issues": list(decision.issues),
            }
        )
    return summaries


def _item_summary(
    record: dict[str, Any],
    status_decision: AssortmentLifecycleDecision,
    commercial_decision: CommercialMarksDecision,
    profile_decision: ExpensiveProfileDecision | None,
    sales_point_codes: tuple[str, ...],
    signal_summaries: list[dict[str, Any]],
    export_blockers: tuple[str, ...],
    *,
    legacy_decision: AssortmentLifecycleDecision,
    target_decision: AssortmentLifecycleDecision,
    model_version: str,
) -> dict[str, Any]:
    summary = {
        "nomenclature_code": status_decision.nomenclature_code,
        "name": str(_optional_field(record, "name", "Name", default="")),
        "folder": _folder_text(record),
        "status": status_decision.status.value,
        "status_label": status_decision.status_label,
        "recommended_status": (
            status_decision.recommended_status.value if status_decision.recommended_status else None
        ),
        "reason_codes": list(status_decision.reason_codes),
        "reason_text": status_decision.reason_text,
        "blockers": list(status_decision.blockers),
        "export_blockers": list(export_blockers),
        "auto_order_allowed": status_decision.auto_order_allowed,
        "manual_review_required": (
            status_decision.manual_review_required or commercial_decision.manual_review_required
        ),
        "sales_point_warehouse_codes": list(sales_point_codes),
        "manager_need_signals": signal_summaries,
        "commercial_marks": [mark.value for mark in commercial_decision.commercial_marks],
        "commercial_mark_labels": list(commercial_decision.commercial_mark_labels),
        "commercial_mark_blockers": list(commercial_decision.blockers),
        "classification_model": model_version,
        "legacy_status": legacy_decision.status.value,
        "legacy_status_label": legacy_decision.status_label,
        "target_status": target_decision.status.value,
        "target_status_label": target_decision.status_label,
        "stage_changed_in_target": legacy_decision.status != target_decision.status,
        "demand_state": (
            target_decision.demand_state.value if target_decision.demand_state else None
        ),
        "demand_state_label": target_decision.demand_state_label,
        "demand_reason_codes": list(target_decision.demand_reason_codes),
        "demand_reason_text": target_decision.demand_reason_text,
        "demand_state_since": _json_value(target_decision.demand_state_since),
        "target_reason_codes": list(target_decision.reason_codes),
        "target_reason_text": target_decision.reason_text,
    }
    if commercial_decision.exclusive_kind or "exclusive" in summary["commercial_marks"]:
        summary["exclusive_kind"] = commercial_decision.exclusive_kind
        summary["exclusive_confidence"] = commercial_decision.exclusive_confidence
        summary["exclusive_checked_at"] = _json_value(commercial_decision.exclusive_checked_at)
        summary["exclusive_review_at"] = _json_value(commercial_decision.exclusive_review_at)
        summary["exclusive_reason"] = commercial_decision.exclusive_reason
        summary["exclusive_approved_by"] = commercial_decision.exclusive_approved_by
        summary["exclusive_evidence_refs"] = list(commercial_decision.exclusive_evidence_refs)
        summary["exclusive_min_stock_qty"] = _json_value(
            commercial_decision.exclusive_min_stock_qty
        )
    if profile_decision is not None:
        summary["expensive_profile"] = (
            profile_decision.profile.value if profile_decision.profile else None
        )
        summary["expensive_profile_label"] = profile_decision.profile_label
        summary["expensive_threshold_value"] = _json_value(profile_decision.threshold_value)
        summary["expensive_item_value"] = _json_value(profile_decision.item_value)
        summary["expensive_reason_codes"] = list(profile_decision.reason_codes)
    return summary


def _status_export_blockers(decision: AssortmentLifecycleDecision) -> tuple[str, ...]:
    if "ut103_export_blocked" in decision.blockers:
        return decision.blockers
    if decision.status.value in {"matrix", "on_demand", "nonliquid", "do_not_order"}:
        return decision.blockers
    return ()


def _commercial_export_blockers(decision: CommercialMarksDecision) -> tuple[str, ...]:
    return decision.blockers


def _commercial_marks_from_record(record: dict[str, Any]) -> tuple[str, ...]:
    value = _optional_field(record, "commercial_marks", "CommercialMarks", default=[])
    return _text_tuple(value)


def _text_tuple(value: Any) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if isinstance(value, str):
        return tuple(part.strip() for part in value.split(",") if part.strip())
    if isinstance(value, list):
        return tuple(str(item).strip() for item in value if str(item).strip())
    raise SystemExit("text list field must be a list or comma-separated string")


def _is_legacy_exclusive_manual_status(value: Any) -> bool:
    return str(value or "").strip().casefold() == "exclusive"


def _write_rows_json(path: Path, rows: list[NomenclaturePropertyUpdateRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"items": [_row_to_mapping(row) for row in rows]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _row_to_mapping(row: NomenclaturePropertyUpdateRow) -> dict[str, Any]:
    return {
        "idempotency_key": row.idempotency_key,
        "nomenclature_code": row.nomenclature_code,
        "property_name": row.property_name,
        "value_type": row.value_type,
        "new_value": _json_value(row.new_value),
        "new_value_name": row.new_value_name,
        "new_value_tag": row.new_value_tag,
        "expected_current_value_name": row.expected_current_value_name,
        "expected_current_value_tag": row.expected_current_value_tag,
        "reason": row.reason,
        "approved_by": row.approved_by,
    }


def _matches_folder(record: dict[str, Any], folder_filter: str) -> bool:
    needle = folder_filter.casefold().strip()
    folder_text = _folder_text(record).casefold()
    if needle in folder_text:
        return True
    if not _is_display_scope_text(needle):
        return False
    if _is_display_scope_text(folder_text):
        return True
    subject = (
        str(_optional_field(record, "subject_1c", "subject", "Предмет", default=""))
        .casefold()
        .strip()
    )
    return subject in {"дисплей", "матрица"}


def _folder_text(record: dict[str, Any]) -> str:
    values = [
        _optional_field(record, "folder", "Folder", default=""),
        _optional_field(record, "folder_name", "FolderName", default=""),
        _optional_field(record, "folder_path", "FolderPath", default=""),
    ]
    return " / ".join(str(value) for value in values if value)


def _is_display_scope_text(value: str) -> bool:
    normalized = value.casefold()
    return any(marker in normalized for marker in DISPLAY_SCOPE_MARKERS)


def _field(item: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in item and item[name] not in (None, ""):
            return item[name]
    raise SystemExit(f"Missing required field; expected one of: {', '.join(names)}")


def _optional_field(item: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in item and item[name] not in (None, ""):
            return item[name]
    return default


def _optional_date_field(item: dict[str, Any], *names: str) -> date | None:
    value = _optional_field(item, *names, default=None)
    return _optional_date_from_value(value)


def _optional_int_field(item: dict[str, Any], *names: str) -> int | None:
    value = _optional_field(item, *names, default=None)
    if value in (None, ""):
        return None
    return int(value)


def _optional_date_from_value(value: Any) -> date | None:
    if value in (None, ""):
        return None
    return _parse_date(str(value))


def _date_tuple(value: Any) -> tuple[date, ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, list):
        raise SystemExit("date list field must be a list")
    return tuple(_parse_date(str(item)) for item in value if item not in (None, ""))


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"date must be YYYY-MM-DD, got: {value}") from error


def _bool_field(item: dict[str, Any], *names: str, default: bool) -> bool:
    value = _optional_field(item, *names, default=default)
    if isinstance(value, bool):
        return value
    text = str(value).strip().casefold()
    if text in {"1", "true", "yes", "y", "да", "истина"}:
        return True
    if text in {"0", "false", "no", "n", "нет", "ложь"}:
        return False
    raise SystemExit(f"Boolean field must be true/false, got: {value}")


def _optional_bool_field(item: dict[str, Any], *names: str) -> bool | None:
    value = _optional_field(item, *names, default=None)
    if value is None or value == "":
        return None
    return _bool_field({names[0]: value}, names[0], default=False)


def _json_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
