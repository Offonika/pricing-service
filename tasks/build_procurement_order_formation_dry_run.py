from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from sqlalchemy import bindparam, select, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.infrastructure.db.engines import build_engine
from app.models.procurement_order_formation import (
    ProcurementOrderFormation,
    ProcurementOrderFormationEvent,
    ProcurementOrderFormationLine,
)
from app.services.bitrix_order_formation import (
    BitrixCatalogProduct,
    load_order_formation_mapping,
    resolve_catalog_products_by_xml_ids,
)
from app.services.master_mobile_catalog import (
    PHOTO_SOURCE,
    MasterMobileCatalogResolver,
    ProductMediaResolution,
)
from app.services.procurement_order_formation import (
    ensure_order_editable,
    invalidate_order_approval,
)
from tasks.report_display_auto_order_adaptive_lead_time_comparison import (
    build_lead_time_indexes,
    choose_lead_time_candidate,
)
from tasks.report_display_supplier_lead_time_history import display_group_key

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT_DIR = REPO_ROOT / "reports/assortment_lifecycle" / date.today().isoformat()
DEFAULT_INPUT = DEFAULT_REPORT_DIR / "display-auto-order-adaptive-sync-ready.csv"
DEFAULT_LEAD_TIME = DEFAULT_REPORT_DIR / "display-supplier-lead-time-history.csv"
DEFAULT_OUTPUT_JSON = DEFAULT_REPORT_DIR / "procurement-order-formation-dry-run.json"
DEFAULT_OUTPUT_CSV = DEFAULT_REPORT_DIR / "procurement-order-formation-lines-dry-run.csv"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build grouped supplier-order cards without writing Bitrix or 1C."
    )
    parser.add_argument("--input-csv", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--lead-time-csv", type=Path, default=DEFAULT_LEAD_TIME)
    parser.add_argument("--contracts-json", type=Path)
    parser.add_argument("--contract-ref", default="")
    parser.add_argument("--contract-code", default="")
    parser.add_argument("--contract-name", default="Основной договор")
    parser.add_argument("--warehouse-ref", default="")
    parser.add_argument("--warehouse-code", default="")
    parser.add_argument("--warehouse-name", default="Центральный склад")
    parser.add_argument("--currency", default="RUB")
    parser.add_argument("--procurement-contour", default="ordinary")
    parser.add_argument("--route", default="ordinary")
    parser.add_argument("--batch-id", default=date.today().isoformat())
    parser.add_argument("--order-date", type=date.fromisoformat, default=date.today())
    parser.add_argument("--calculation-id", default="")
    parser.add_argument("--source-run-id", default="")
    parser.add_argument("--source-summary-json", type=Path)
    parser.add_argument("--responsible-bitrix-user-id", default="")
    parser.add_argument("--output-json", type=Path, default=DEFAULT_OUTPUT_JSON)
    parser.add_argument("--output-csv", type=Path, default=DEFAULT_OUTPUT_CSV)
    parser.add_argument(
        "--skip-bitrix-catalog",
        action="store_true",
        help="Do not call Bitrix; every line gets catalog_not_checked blocker.",
    )
    parser.add_argument(
        "--skip-public-catalog",
        action="store_true",
        help="Do not resolve product cards and original photos at master-mobile.ru.",
    )
    parser.add_argument(
        "--persist-db",
        action="store_true",
        help="Persist pricing-service drafts only; still never writes Bitrix or 1C.",
    )
    parser.add_argument(
        "--supersede-open-batches",
        action="store_true",
        help="Hide older open display-auto-order batches after a successful DB persist.",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--fail-on-blockers", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = get_settings()
    source_rows = read_csv(args.input_csv)
    lead_time_rows = read_csv(args.lead_time_csv)
    source_summary = (
        json.loads(args.source_summary_json.read_text(encoding="utf-8-sig"))
        if args.source_summary_json and args.source_summary_json.exists()
        else {}
    )
    source_run_id = str(
        args.source_run_id
        or (source_summary.get("classification_run_id") if isinstance(source_summary, dict) else "")
        or ""
    )
    calculation_id = (
        args.calculation_id
        or (f"display-auto-order-{source_run_id}" if source_run_id else "")
        or f"display-auto-order-{args.order_date.isoformat()}"
    )
    selected_rows = select_order_rows(source_rows)
    nomenclature = fetch_nomenclature_by_codes(
        settings.onec_database_url,
        [str(row.get("nomenclature_code") or "") for row in selected_rows],
    )
    contracts = load_contracts(args)
    selected_supplier_refs = choose_selected_supplier_refs(selected_rows, lead_time_rows)
    order_dimensions = fetch_latest_order_dimensions(
        settings.onec_database_url,
        codes=[str(row.get("nomenclature_code") or "") for row in selected_rows],
        supplier_refs=selected_supplier_refs,
    )
    catalog_mapping = None
    if not args.skip_bitrix_catalog:
        catalog_mapping = load_order_formation_mapping(settings)

    catalog_products = (
        {}
        if args.skip_bitrix_catalog
        else resolve_catalog_products_by_xml_ids(
            [
                _onec_binary_ref_to_guid_or_empty(
                    str(
                        (nomenclature.get(str(row.get("nomenclature_code") or "")) or {}).get(
                            "nomenclature_ref"
                        )
                        or ""
                    )
                )
                for row in selected_rows
            ],
            settings=settings,
            mapping=catalog_mapping,
        )
    )

    def catalog_resolver(xml_id: str) -> BitrixCatalogProduct | None:
        return catalog_products.get(_normalize_guid(xml_id))

    product_media = (
        {}
        if args.skip_public_catalog
        else MasterMobileCatalogResolver(
            base_url=settings.master_mobile_catalog_base_url,
            timeout_seconds=settings.master_mobile_catalog_timeout_seconds,
            max_attempts=settings.master_mobile_catalog_max_attempts,
            max_workers=settings.master_mobile_catalog_max_workers,
        ).resolve_many(
            [
                _clean(
                    (nomenclature.get(_clean(row.get("nomenclature_code"))) or {}).get("article")
                )
                or _clean(row.get("nomenclature_code"))
                for row in selected_rows
            ]
        )
    )

    def product_media_resolver(article: str) -> ProductMediaResolution | None:
        return product_media.get(str(article).strip())

    orders = build_grouped_orders(
        selected_rows,
        lead_time_rows,
        nomenclature_by_code=nomenclature,
        catalog_resolver=catalog_resolver,
        product_media_resolver=product_media_resolver,
        skip_catalog=args.skip_bitrix_catalog,
        contracts=contracts,
        order_dimensions=order_dimensions,
        warehouse={
            "ref": args.warehouse_ref,
            "code": args.warehouse_code,
            "name": args.warehouse_name,
        },
        currency=args.currency,
        procurement_contour=args.procurement_contour,
        route=args.route,
        batch_id=args.batch_id,
        order_date=args.order_date,
        calculation_id=calculation_id,
        source_run_id=source_run_id,
        responsible_bitrix_user_id=args.responsible_bitrix_user_id,
    )
    summary = build_summary(source_rows=source_rows, selected_rows=selected_rows, orders=orders)
    blocked_by_gate = bool(args.fail_on_blockers and summary["blocking_line_count"])
    payload = {
        "summary": summary,
        "orders": orders,
        "safety": {
            "bitrix_write": False,
            "onec_write": False,
            "onec_document_posting": False,
            "public_catalog_write": False,
            "pricing_service_db_write": bool(args.persist_db and not blocked_by_gate),
        },
    }
    write_json(args.output_json, payload)
    write_lines_csv(args.output_csv, orders)
    persisted_ids: list[int] = []
    if args.persist_db and not blocked_by_gate:
        engine = build_engine(settings.database_url)
        with Session(engine) as db:
            persisted_ids = persist_grouped_orders(
                db,
                orders,
                supersede_open_batches=args.supersede_open_batches,
            )
        payload["persisted_order_ids"] = persisted_ids
        write_json(args.output_json, payload)
    output = {
        **summary,
        "output_json": str(args.output_json),
        "output_csv": str(args.output_csv),
        "persisted_order_ids": persisted_ids,
        "bitrix_write": False,
        "onec_write": False,
    }
    print(
        json.dumps(
            output,
            ensure_ascii=False,
            sort_keys=True if args.json else False,
            indent=None if args.json else 2,
        )
    )
    return 2 if blocked_by_gate else 0


def select_order_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in rows
        if _clean(row.get("dry_run_decision")) == "order"
        and (_decimal(row.get("recommended_order_qty")) or Decimal("0")) > 0
    ]


def build_grouped_orders(
    selected_rows: Sequence[Mapping[str, Any]],
    lead_time_rows: Sequence[Mapping[str, Any]],
    *,
    nomenclature_by_code: Mapping[str, Mapping[str, Any]],
    catalog_resolver: Callable[[str], BitrixCatalogProduct | None],
    skip_catalog: bool,
    contracts: Mapping[str, Any],
    order_dimensions: Mapping[str, Mapping[tuple[str, str], Mapping[str, Any]]] | None = None,
    warehouse: Mapping[str, str],
    currency: str,
    procurement_contour: str,
    route: str,
    batch_id: str,
    order_date: date,
    calculation_id: str,
    source_run_id: str = "",
    responsible_bitrix_user_id: str = "",
    product_media_resolver: Callable[[str], ProductMediaResolution | None] | None = None,
) -> list[dict[str, Any]]:
    code_index, group_index = build_lead_time_indexes(lead_time_rows)
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    group_headers: dict[tuple[str, ...], dict[str, Any]] = {}
    for row in selected_rows:
        code = _clean(row.get("nomenclature_code"))
        nomenclature = nomenclature_by_code.get(code) or {}
        ref_hex = _clean(nomenclature.get("nomenclature_ref"))
        xml_id = _onec_binary_ref_to_guid_or_empty(ref_hex)
        lead_candidate, _source_level = choose_lead_time_candidate(
            code,
            display_group_key(row),
            code_index=code_index,
            group_index=group_index,
        )
        supplier = {
            "ref": _clean((lead_candidate or {}).get("supplier_ref")),
            "code": _clean((lead_candidate or {}).get("supplier_code")),
            "name": _clean((lead_candidate or {}).get("supplier_name")),
        }
        dimension = order_dimension_for_line(
            supplier,
            code,
            order_dimensions=order_dimensions or {},
        )
        contract = contract_for_supplier(
            supplier,
            contracts=contracts,
            dimension=dimension,
        )
        effective_warehouse = {
            "ref": _clean(warehouse.get("ref")) or _clean(dimension.get("warehouse_ref")),
            "code": _clean(warehouse.get("code")) or _clean(dimension.get("warehouse_code")),
            "name": (
                _clean(warehouse.get("name"))
                if _clean(warehouse.get("ref")) or _clean(warehouse.get("code"))
                else _clean(dimension.get("warehouse_name")) or _clean(warehouse.get("name"))
            ),
        }
        key = (
            supplier["ref"],
            supplier["code"],
            contract["ref"],
            contract["code"],
            currency,
            effective_warehouse["ref"],
            effective_warehouse["code"],
            procurement_contour,
            route,
            batch_id,
        )
        group_headers[key] = {
            "supplier": supplier,
            "contract": contract,
            "warehouse": effective_warehouse,
            "currency": currency,
            "procurement_contour": procurement_contour,
            "route": route,
            "batch_id": batch_id,
            "order_date": order_date.isoformat(),
            "responsible_name": _clean((lead_candidate or {}).get("responsible_name")),
            "calculation_id": calculation_id,
            "source_run_id": source_run_id,
            "responsible_bitrix_user_id": responsible_bitrix_user_id,
            "payload": {
                "supplier_profile": supplier_profile_payload(lead_candidate or {}),
            },
        }
        product = catalog_resolver(xml_id) if xml_id else None
        blockers = _split_codes(row.get("blockers"))
        if not code:
            blockers.append("nomenclature_code_missing")
        if not ref_hex or not xml_id:
            blockers.append("nomenclature_guid_missing")
        if not supplier["ref"] and not supplier["code"]:
            blockers.append("supplier_1c_reference_missing")
        if not contract["ref"] and not contract["code"]:
            blockers.append("contract_1c_reference_missing")
        if not effective_warehouse["ref"] and not effective_warehouse["code"]:
            blockers.append("warehouse_1c_reference_missing")
        if skip_catalog:
            blockers.append("catalog_not_checked")
        elif product is None:
            blockers.append("catalog_product_missing")
        elif _normalize_guid(product.xml_id) != _normalize_guid(xml_id):
            blockers.append("catalog_xml_id_mismatch")
        quantity = _decimal(row.get("recommended_order_qty")) or Decimal("0")
        price = _decimal(row.get("latest_purchase_price")) or Decimal("0")
        b2b_customer_demand = _b2b_customer_demand_payload(row)
        public_article = _clean(nomenclature.get("article")) or code
        public_media = product_media_resolver(public_article) if product_media_resolver else None
        line_payload = procurement_assistant_line_payload(
            row,
            public_media=public_media,
            lead_candidate=lead_candidate or {},
        )
        if b2b_customer_demand:
            line_payload["b2b_customer_demand"] = b2b_customer_demand
        if price <= 0:
            blockers.append("purchase_price_missing")
        groups[key].append(
            {
                "stable_key": f"{calculation_id}:{code}",
                "nomenclature_ref": ref_hex,
                "nomenclature_guid": xml_id,
                "nomenclature_code": code,
                "nomenclature_name": _clean(row.get("name")),
                "bitrix_product_id": product.product_id if product else None,
                "bitrix_product_xml_id": product.xml_id if product else xml_id,
                "recommended_quantity": str(quantity),
                "final_quantity": str(quantity),
                "purchase_price": str(price),
                "amount": str((quantity * price).quantize(Decimal("0.01"))),
                "currency": currency,
                "source_kind": "automatic",
                "explicit_demand": False,
                "risk_level": _risk_level(row, blockers),
                "risk_codes": _split_codes(row.get("warnings")),
                "recommendation_reason": _clean(row.get("reason_ru")),
                "blockers": list(dict.fromkeys(blockers)),
                "assortment_status": product.assortment_status if product else "",
                "lifecycle_status": _clean(row.get("status_label")),
                "quality": product.quality if product else _clean(row.get("quality_raw")),
                "procurement_profile": product.procurement_profile if product else "",
                "manual_minimum": (
                    str(product.manual_minimum)
                    if product and product.manual_minimum is not None
                    else None
                ),
                "product_media_status": public_media.status if public_media else "not_checked",
                "payload": line_payload,
            }
        )

    orders: list[dict[str, Any]] = []
    for key in sorted(groups):
        header = group_headers[key]
        lines = groups[key]
        stable_source = "|".join(key)
        stable_hash = hashlib.sha256(stable_source.encode("utf-8")).hexdigest()[:20]
        for index, line in enumerate(lines, start=1):
            line["line_number"] = index
        orders.append(
            {
                "stable_key": f"proc-order:{batch_id}:{stable_hash}",
                **header,
                "lines": lines,
                "blockers": list(
                    dict.fromkeys(blocker for line in lines for blocker in line["blockers"])
                ),
            }
        )
    return orders


def contract_for_supplier(
    supplier: Mapping[str, str],
    *,
    contracts: Mapping[str, Any],
    dimension: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    by_ref = contracts.get("by_supplier_ref") or {}
    by_code = contracts.get("by_supplier_code") or {}
    value = by_ref.get(supplier.get("ref")) or by_code.get(supplier.get("code"))
    if not isinstance(value, dict) or not (_clean(value.get("ref")) or _clean(value.get("code"))):
        value = {
            "ref": _clean((dimension or {}).get("contract_ref")),
            "code": _clean((dimension or {}).get("contract_code")),
            "name": _clean((dimension or {}).get("contract_name")),
        }
    if not (_clean(value.get("ref")) or _clean(value.get("code"))):
        value = contracts.get("default") or {}
    return {
        "ref": _clean(value.get("ref")),
        "code": _clean(value.get("code")),
        "name": _clean(value.get("name")) or "Основной договор",
    }


def load_contracts(args: argparse.Namespace) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if args.contracts_json:
        loaded = json.loads(args.contracts_json.read_text(encoding="utf-8-sig"))
        if not isinstance(loaded, dict):
            raise ValueError("contracts JSON must be an object")
        payload.update(loaded)
    payload.setdefault(
        "default",
        {"ref": args.contract_ref, "code": args.contract_code, "name": args.contract_name},
    )
    return payload


def choose_selected_supplier_refs(
    selected_rows: Sequence[Mapping[str, Any]],
    lead_time_rows: Sequence[Mapping[str, Any]],
) -> list[str]:
    code_index, group_index = build_lead_time_indexes(lead_time_rows)
    refs: set[str] = set()
    for row in selected_rows:
        code = _clean(row.get("nomenclature_code"))
        candidate, _level = choose_lead_time_candidate(
            code,
            display_group_key(row),
            code_index=code_index,
            group_index=group_index,
        )
        ref = _clean((candidate or {}).get("supplier_ref"))
        if ref:
            refs.add(ref)
    return sorted(refs)


def fetch_latest_order_dimensions(
    database_url: str | None,
    *,
    codes: Sequence[str],
    supplier_refs: Sequence[str],
) -> dict[str, dict[tuple[str, str], dict[str, Any]]]:
    clean_codes = sorted({_clean(code) for code in codes if _clean(code)})
    clean_suppliers = sorted({_clean(ref) for ref in supplier_refs if _clean(ref)})
    result: dict[str, dict[tuple[str, str], dict[str, Any]]] = {
        "exact": {},
        "supplier": {},
    }
    if not database_url or not clean_codes or not clean_suppliers:
        return result
    query = text("""
        WITH dimensions AS (
            SELECT
                NULLIF(LTRIM(RTRIM(product._Code)), N'') AS nomenclature_code,
                CONVERT(varchar(34), doc._Fld2498RRef, 1) AS supplier_ref,
                CONVERT(varchar(34), doc._Fld2494RRef, 1) AS contract_ref,
                NULLIF(LTRIM(RTRIM(contract._Code)), N'') AS contract_code,
                NULLIF(LTRIM(RTRIM(contract._Description)), N'') AS contract_name,
                CONVERT(varchar(34), doc._Fld2506RRef, 1) AS warehouse_ref,
                NULLIF(LTRIM(RTRIM(warehouse._Code)), N'') AS warehouse_code,
                NULLIF(LTRIM(RTRIM(warehouse._Description)), N'') AS warehouse_name,
                doc._Date_Time AS order_date,
                ROW_NUMBER() OVER (
                    PARTITION BY doc._Fld2498RRef, product._Code
                    ORDER BY doc._Date_Time DESC, doc._Number DESC
                ) AS rn
            FROM dbo._Document133_VT2515 AS line WITH (NOLOCK)
            JOIN dbo._Document133 AS doc WITH (NOLOCK)
                ON doc._IDRRef = line._Document133_IDRRef
            JOIN dbo._Reference62 AS product WITH (NOLOCK)
                ON product._IDRRef = line._Fld2523RRef
            LEFT JOIN dbo._Reference37 AS contract WITH (NOLOCK)
                ON contract._IDRRef = doc._Fld2494RRef
            LEFT JOIN dbo._Reference80 AS warehouse WITH (NOLOCK)
                ON warehouse._IDRRef = doc._Fld2506RRef
            WHERE doc._Marked = 0x00
              AND doc._Posted = 0x01
              AND LTRIM(RTRIM(product._Code)) IN :codes
              AND CONVERT(varchar(34), doc._Fld2498RRef, 1) IN :supplier_refs
        )
        SELECT * FROM dimensions WHERE rn = 1
    """).bindparams(
        bindparam("codes", expanding=True),
        bindparam("supplier_refs", expanding=True),
    )
    engine = build_engine(database_url, pool_pre_ping=True)
    with engine.connect() as connection:
        rows = [
            dict(row)
            for row in connection.execute(
                query,
                {"codes": clean_codes, "supplier_refs": clean_suppliers},
            ).mappings()
        ]
    latest_by_supplier: dict[str, dict[str, Any]] = {}
    for row in rows:
        supplier_ref = _clean(row.get("supplier_ref"))
        code = _clean(row.get("nomenclature_code"))
        result["exact"][(supplier_ref, code)] = row
        current = latest_by_supplier.get(supplier_ref)
        if current is None or str(row.get("order_date") or "") > str(
            current.get("order_date") or ""
        ):
            latest_by_supplier[supplier_ref] = row
    result["supplier"] = {
        (supplier_ref, "*"): row for supplier_ref, row in latest_by_supplier.items()
    }
    return result


def order_dimension_for_line(
    supplier: Mapping[str, str],
    code: str,
    *,
    order_dimensions: Mapping[str, Mapping[tuple[str, str], Mapping[str, Any]]],
) -> Mapping[str, Any]:
    supplier_ref = _clean(supplier.get("ref"))
    exact = order_dimensions.get("exact") or {}
    supplier_fallback = order_dimensions.get("supplier") or {}
    return exact.get((supplier_ref, code)) or supplier_fallback.get((supplier_ref, "*")) or {}


def fetch_nomenclature_by_codes(
    database_url: str | None, codes: Sequence[str]
) -> dict[str, dict[str, Any]]:
    clean_codes = sorted({_clean(code) for code in codes if _clean(code)})
    if not database_url:
        raise RuntimeError("ONEC_DATABASE_URL is not configured")
    if not clean_codes:
        return {}
    query = text("""
        SELECT
            CONVERT(varchar(34), item._IDRRef, 1) AS nomenclature_ref,
            NULLIF(LTRIM(RTRIM(item._Code)), N'') AS nomenclature_code,
            NULLIF(LTRIM(RTRIM(item._Description)), N'') AS nomenclature_name,
            NULLIF(LTRIM(RTRIM(CAST(item._Fld836 AS nvarchar(max)))), N'') AS article
        FROM dbo._Reference62 AS item WITH (NOLOCK)
        WHERE item._Marked = 0x00
          AND LTRIM(RTRIM(item._Code)) IN :codes
    """).bindparams(bindparam("codes", expanding=True))
    engine = build_engine(database_url, pool_pre_ping=True)
    with engine.connect() as connection:
        rows = [dict(row) for row in connection.execute(query, {"codes": clean_codes}).mappings()]
    return {_clean(row.get("nomenclature_code")): row for row in rows}


def build_summary(
    *,
    source_rows: Sequence[Mapping[str, Any]],
    selected_rows: Sequence[Mapping[str, Any]],
    orders: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    lines = [line for order in orders for line in order.get("lines", [])]
    return {
        "source_row_count": len(source_rows),
        "selected_order_line_count": len(selected_rows),
        "grouped_order_count": len(orders),
        "catalog_matched_line_count": sum(bool(line.get("bitrix_product_id")) for line in lines),
        "product_card_matched_line_count": sum(
            bool((line.get("payload") or {}).get("product_card_url")) for line in lines
        ),
        "original_photo_matched_line_count": sum(
            bool(((line.get("payload") or {}).get("photos") or [{}])[0].get("original"))
            for line in lines
        ),
        "product_media_status_counts": dict(
            sorted(
                Counter(
                    str(line.get("product_media_status") or "not_checked") for line in lines
                ).items()
            )
        ),
        "blocking_line_count": sum(bool(line.get("blockers")) for line in lines),
        "total_quantity": str(
            sum((_decimal(line.get("final_quantity")) or Decimal("0")) for line in lines)
        ),
        "total_amount": str(sum((_decimal(line.get("amount")) or Decimal("0")) for line in lines)),
    }


def persist_grouped_orders(
    db: Session,
    orders: Sequence[Mapping[str, Any]],
    *,
    supersede_open_batches: bool = False,
) -> list[int]:
    persisted_ids: list[int] = []
    for payload in orders:
        requested_stable_key = str(payload["stable_key"])
        order = db.scalar(
            select(ProcurementOrderFormation).where(
                ProcurementOrderFormation.stable_key == requested_stable_key
            )
        )
        if order is not None and _order_is_immutable(order):
            requested_stable_key = _immutable_revision_stable_key(payload)
            order = db.scalar(
                select(ProcurementOrderFormation).where(
                    ProcurementOrderFormation.stable_key == requested_stable_key
                )
            )
            if order is not None and _order_is_immutable(order):
                persisted_ids.append(order.id)
                continue
        created = order is None
        if order is None:
            order = ProcurementOrderFormation(
                stable_key=requested_stable_key,
                status="draft",
                version=1,
                supplier_ref=payload["supplier"].get("ref") or None,
                supplier_code=payload["supplier"].get("code") or None,
                supplier_name=payload["supplier"].get("name") or "Не определён",
                contract_ref=payload["contract"].get("ref") or None,
                contract_code=payload["contract"].get("code") or None,
                contract_name=payload["contract"].get("name") or "Не определён",
                warehouse_ref=payload["warehouse"].get("ref") or None,
                warehouse_code=payload["warehouse"].get("code") or None,
                warehouse_name=payload["warehouse"].get("name") or "Не определён",
                currency=str(payload["currency"]),
                procurement_contour=str(payload["procurement_contour"]),
                route=str(payload["route"]),
                batch_id=str(payload["batch_id"]),
                order_date=date.fromisoformat(str(payload["order_date"])),
                responsible_name=payload.get("responsible_name") or None,
                responsible_bitrix_user_id=(payload.get("responsible_bitrix_user_id") or None),
                calculation_id=str(payload["calculation_id"]),
                source_run_id=payload.get("source_run_id") or None,
                payload={
                    "dry_run_source": True,
                    "sync_source": "display_auto_order",
                    **dict(payload.get("payload") or {}),
                },
            )
            db.add(order)
            db.flush()
        changed = False
        if not created:
            ensure_order_editable(order)
            header_values = {
                "calculation_id": str(payload["calculation_id"]),
                "source_run_id": payload.get("source_run_id") or None,
                "responsible_bitrix_user_id": (payload.get("responsible_bitrix_user_id") or None),
                "responsible_name": payload.get("responsible_name") or None,
            }
            for field_name, value in header_values.items():
                if getattr(order, field_name) != value:
                    setattr(order, field_name, value)
                    changed = True
            expected_payload = {
                **(order.payload or {}),
                "dry_run_source": True,
                "sync_source": "display_auto_order",
                **dict(payload.get("payload") or {}),
            }
            if order.payload != expected_payload:
                order.payload = expected_payload
                changed = True
        existing = {line.stable_key: line for line in order.lines}
        seen: set[str] = set()
        for line_payload in payload.get("lines", []):
            stable_key = str(line_payload["stable_key"])
            seen.add(stable_key)
            line = existing.get(stable_key)
            if line is None:
                line = ProcurementOrderFormationLine(order=order, stable_key=stable_key)
                db.add(line)
                changed = True
                line_changed = False
            else:
                line_changed = False
            values = {
                "line_number": int(line_payload["line_number"]),
                "bitrix_product_id": line_payload.get("bitrix_product_id"),
                "bitrix_product_xml_id": str(line_payload["bitrix_product_xml_id"]),
                "nomenclature_ref": str(line_payload["nomenclature_ref"]),
                "nomenclature_code": line_payload.get("nomenclature_code"),
                "nomenclature_name": str(line_payload["nomenclature_name"]),
                "recommended_quantity": Decimal(str(line_payload["recommended_quantity"])),
                "final_quantity": Decimal(str(line_payload["final_quantity"])),
                "purchase_price": Decimal(str(line_payload["purchase_price"])),
                "amount": Decimal(str(line_payload["amount"])),
                "currency": str(line_payload["currency"]),
                "source_kind": str(line_payload["source_kind"]),
                "explicit_demand": bool(line_payload["explicit_demand"]),
                "risk_level": line_payload.get("risk_level"),
                "risk_codes": list(line_payload.get("risk_codes") or []),
                "recommendation_reason": line_payload.get("recommendation_reason"),
                "blockers": list(line_payload.get("blockers") or []),
                "assortment_status": line_payload.get("assortment_status") or None,
                "lifecycle_status": line_payload.get("lifecycle_status") or None,
                "quality": line_payload.get("quality") or None,
                "procurement_profile": line_payload.get("procurement_profile") or None,
                "manual_minimum": (
                    Decimal(str(line_payload["manual_minimum"]))
                    if line_payload.get("manual_minimum") not in (None, "")
                    else None
                ),
                "payload": dict(line_payload.get("payload") or {}),
                "removed": False,
            }
            for field_name, value in values.items():
                if getattr(line, field_name, None) != value:
                    setattr(line, field_name, value)
                    changed = True
                    line_changed = True
            if line_changed and line.id is not None:
                line.version += 1
        for stable_key, line in existing.items():
            if stable_key not in seen and not line.removed:
                line.removed = True
                line.version += 1
                changed = True
        if changed and not created:
            invalidate_order_approval(order)
        db.flush()
        if created or changed:
            db.add(
                ProcurementOrderFormationEvent(
                    order_id=order.id,
                    entity_type="order",
                    entity_id=str(order.id),
                    event_type="automatic_order_sync",
                    actor="display-auto-order-sync",
                    bitrix_user_id=(payload.get("responsible_bitrix_user_id") or None),
                    idempotency_key=(
                        f"auto-order-sync:{order.id}:v{order.version}:"
                        f"{payload.get('calculation_id')}"
                    ),
                    payload={
                        "calculation_id": payload.get("calculation_id"),
                        "source_run_id": payload.get("source_run_id"),
                        "created": created,
                    },
                )
            )
        persisted_ids.append(order.id)
    if supersede_open_batches:
        _supersede_previous_open_batches(db, active_order_ids=set(persisted_ids), orders=orders)
    db.commit()
    return persisted_ids


def _order_is_immutable(order: ProcurementOrderFormation) -> bool:
    return order.status in {"transmitting", "transmitted"} or order.onec_status in {
        "pending",
        "transmitted",
    }


def _immutable_revision_stable_key(payload: Mapping[str, Any]) -> str:
    calculation = str(payload.get("calculation_id") or payload.get("source_run_id") or "new")
    digest = hashlib.sha256(calculation.encode("utf-8")).hexdigest()[:12]
    return f"{payload['stable_key']}:revision:{digest}"


def _supersede_previous_open_batches(
    db: Session,
    *,
    active_order_ids: set[int],
    orders: Sequence[Mapping[str, Any]],
) -> None:
    current_calculation_ids = {str(order.get("calculation_id") or "") for order in orders}
    candidates = db.scalars(select(ProcurementOrderFormation)).all()
    for order in candidates:
        payload = order.payload or {}
        is_display_auto_order = bool(payload.get("dry_run_source")) or (
            payload.get("sync_source") == "display_auto_order"
        )
        if (
            order.id in active_order_ids
            or not is_display_auto_order
            or _order_is_immutable(order)
            or order.status == "superseded"
            or order.calculation_id in current_calculation_ids
        ):
            continue
        previous_status = order.status
        if order.status in {"approved", "review", "error"} or order.approved_version is not None:
            invalidate_order_approval(order)
        order.status = "superseded"
        order.payload = {
            **payload,
            "superseded_by_calculation_ids": sorted(current_calculation_ids),
        }
        db.add(
            ProcurementOrderFormationEvent(
                order_id=order.id,
                entity_type="order",
                entity_id=str(order.id),
                event_type="automatic_order_superseded",
                actor="display-auto-order-sync",
                idempotency_key=(
                    f"auto-order-supersede:{order.id}:"
                    f"{','.join(sorted(current_calculation_ids))}"
                ),
                before={"status": previous_status},
                after={"status": "superseded"},
            )
        )


def read_csv(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        return [dict(row) for row in csv.DictReader(source)]


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_lines_csv(path: Path, orders: Sequence[Mapping[str, Any]]) -> None:
    rows = []
    for order in orders:
        for line in order.get("lines", []):
            rows.append(
                {
                    "order_stable_key": order["stable_key"],
                    "supplier_name": order["supplier"].get("name"),
                    "supplier_code": order["supplier"].get("code"),
                    "contract_name": order["contract"].get("name"),
                    "warehouse_name": order["warehouse"].get("name"),
                    **line,
                    "risk_codes": "; ".join(line.get("risk_codes") or []),
                    "blockers": "; ".join(line.get("blockers") or []),
                }
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0]) if rows else ["order_stable_key"]
    with path.open("w", encoding="utf-8-sig", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _onec_binary_ref_to_guid_or_empty(value: str) -> str:
    text_value = _clean(value).lower().removeprefix("0x")
    if len(text_value) != 32:
        return ""
    return "-".join(
        (
            text_value[24:32],
            text_value[20:24],
            text_value[16:20],
            text_value[0:4],
            text_value[4:16],
        )
    )


def _normalize_guid(value: Any) -> str:
    return _clean(value).strip("{}").lower()


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).replace(" ", "").replace(",", "."))
    except (InvalidOperation, ValueError):
        return None


def _split_codes(value: Any) -> list[str]:
    return [item.strip() for item in _clean(value).replace(",", ";").split(";") if item.strip()]


def procurement_assistant_line_payload(
    row: Mapping[str, Any],
    *,
    public_media: ProductMediaResolution | None,
    lead_candidate: Mapping[str, Any],
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if public_media and public_media.found:
        payload["photos"] = [
            {
                "thumbnail": public_media.photo_thumbnail_url or public_media.photo_original_url,
                "original": public_media.photo_original_url,
            }
        ]
        payload["product_card_url"] = public_media.product_card_url
        payload["photo_source"] = PHOTO_SOURCE
    optional_values = {
        "profitability_pct": _clean(
            row.get("profitability_pct") or row.get("gross_margin_pct") or row.get("margin_pct")
        ),
        "supplier_defect_pct": _clean(row.get("supplier_defect_pct") or row.get("defect_pct")),
        "supplier_defect_history_units": _clean(
            row.get("supplier_defect_history_units") or row.get("defect_history_units")
        ),
        "price_change_pct": _clean(row.get("price_change_pct")),
        "delivery_days": _clean(
            lead_candidate.get("recommended_supplier_prepare_days") or row.get("delivery_days")
        ),
    }
    payload.update({key: value for key, value in optional_values.items() if value})
    return payload


def supplier_profile_payload(candidate: Mapping[str, Any]) -> dict[str, Any]:
    advantages = [
        item.strip() for item in _clean(candidate.get("advantages")).split(";") if item.strip()
    ]
    values: dict[str, Any] = {
        "qualification_class": _clean(
            candidate.get("qualification_class") or candidate.get("supplier_class")
        ),
        "qualification_label": _clean(candidate.get("qualification_label")),
        "profitability_pct": _clean(candidate.get("profitability_pct")),
        "defect_pct": _clean(candidate.get("defect_pct") or candidate.get("supplier_defect_pct")),
        "defect_history_units": _integer(
            candidate.get("defect_history_units") or candidate.get("supplier_defect_history_units")
        ),
        "on_time_pct": _clean(candidate.get("on_time_pct")),
        "payment_terms": _clean(candidate.get("payment_terms")),
        "credit_days": _integer(candidate.get("credit_days")),
        "credit_limit": _clean(candidate.get("credit_limit")),
        "advantages": advantages,
        "history_order_count": _integer(candidate.get("history_order_count")),
        "updated_at": _clean(
            candidate.get("updated_at") or candidate.get("latest_supplier_order_at")
        ),
    }
    return {key: value for key, value in values.items() if value not in (None, "", [])}


def _b2b_customer_demand_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    mode = _clean(row.get("b2b_demand_mode"))
    if not mode:
        return {}
    return {
        "mode": mode,
        "profile_as_of_exclusive": _clean(row.get("b2b_profile_as_of_exclusive")),
        "profile_age_days": _integer(row.get("b2b_profile_age_days")),
        "dependency_class": _clean(row.get("b2b_dependency_class")),
        "active_customer_count": _integer(row.get("b2b_active_customer_count")),
        "passive_customer_count": _integer(row.get("b2b_passive_customer_count")),
        "due_customer_count": _integer(row.get("b2b_due_customer_count")),
        "managed_sales_qty_window": _clean(row.get("b2b_managed_sales_qty_window")),
        "active_daily_rate": _clean(row.get("b2b_active_daily_rate")),
        "client_forecast_qty": _clean(row.get("b2b_client_forecast_qty")),
        "ordinary_net_sales_qty_window": _clean(row.get("b2b_ordinary_net_sales_qty_window")),
        "replacement_target_stock_qty": _clean(row.get("b2b_replacement_target_stock_qty")),
        "replacement_decision": _clean(row.get("b2b_replacement_decision")),
        "replacement_recommended_order_qty": _clean(
            row.get("b2b_replacement_recommended_order_qty")
        ),
        "order_delta_qty": _clean(row.get("b2b_order_delta_qty")),
        "reason_ru": _clean(row.get("b2b_reason_ru")),
    }


def _integer(value: Any) -> int | None:
    decimal_value = _decimal(value)
    return int(decimal_value) if decimal_value is not None else None


def _risk_level(row: Mapping[str, Any], blockers: Sequence[str]) -> str:
    if blockers:
        return "blocked"
    warnings = _split_codes(row.get("warnings"))
    return "medium" if warnings else "low"


if __name__ == "__main__":
    raise SystemExit(main())
