"""Build a deterministic read-only inventory of proposed display families."""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import os
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from sqlalchemy import inspect, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from app.infrastructure.db import get_onec_engine, session_scope
from app.infrastructure.db.engines import DatabaseNotConfiguredError
from app.models.competitor_item import CompetitorItem
from app.models.competitor_item_compatibility import CompetitorItemCompatibility
from app.models.competitor_item_match import (
    CompetitorItemMatch,
    CompetitorItemMatchStatus,
)
from app.models.device_model import PhoneModel
from app.models.procurement_order_formation import (
    ProcurementOrderFormation,
    ProcurementOrderFormationLine,
)
from app.models.product import Product
from app.models.product_phone_model import ProductPhoneModel
from app.services.assortment_lifecycle_classification_store import (
    ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE,
)
from app.services.display_family_inventory import (
    AcceptedCompetitorMatchEvidence,
    DisplayInventoryScopeEvidence,
    build_display_family_inventory,
)
from app.services.display_identity import display_identity_for_competitor
from app.services.onec_stock_availability import (
    CurrentStockSnapshot,
    build_current_stock_snapshot,
    fetch_current_stock_snapshot,
)

DEFAULT_OUTPUT_ROOT = Path("reports/assortment_lifecycle")
TERMINAL_ORDER_STATUSES = {"deferred", "superseded"}
BUSINESS_TIMEZONE = ZoneInfo("Europe/Moscow")
MANIFEST_SCHEMA = "display_family_registry_preflight_manifest.v2"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def _product_code(product: Product) -> str:
    return str(product.code_1c or product.article or product.fact_sku or product.id).strip()


def _table_exists(session: Session, table_name: str) -> bool:
    return inspect(session.get_bind()).has_table(table_name)


def _column_names(session: Session, table_name: str) -> set[str]:
    return {str(column["name"]) for column in inspect(session.get_bind()).get_columns(table_name)}


def _date_value(value: object | None) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value:
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            return None
    return None


def load_inventory_sources(
    session: Session,
    *,
    as_of: date,
    history_cutoff: date,
    current_stock_by_code: Mapping[str, Decimal],
) -> tuple[
    list[Product],
    dict[str, DisplayInventoryScopeEvidence],
    list[str],
    dict[str, dict[str, Any]],
]:
    products = list(
        session.scalars(
            select(Product)
            .options(
                selectinload(Product.phone_model_links).selectinload(ProductPhoneModel.phone_model),
                selectinload(Product.compatibilities),
            )
            .order_by(Product.id)
        )
        .unique()
        .all()
    )
    warnings: list[str] = []
    sources: dict[str, dict[str, Any]] = {
        "application_catalog": {
            "status": "ready" if products else "empty",
            "row_count": len(products),
        }
    }
    history: dict[str, date | None] = {}
    table_name = ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE.name
    if _table_exists(session, table_name):
        lifecycle_columns = _column_names(session, table_name)
        if "last_sale_at" in lifecycle_columns:
            history = {
                str(code or "").strip(): _date_value(last_sale)
                for code, last_sale in session.execute(
                    select(
                        ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE.c.nomenclature_code,
                        ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE.c.last_sale_at,
                    )
                )
                if str(code or "").strip()
            }
            sources["lifecycle_history"] = {
                "status": "ready",
                "mode": "last_sale_at_column",
                "row_count": len(history),
            }
        elif "source_record" in lifecycle_columns:
            history = {
                str(code or "").strip(): _date_value((source_record or {}).get("last_sale_at"))
                for code, source_record in session.execute(
                    select(
                        ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE.c.nomenclature_code,
                        ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE.c.source_record,
                    )
                )
                if str(code or "").strip() and isinstance(source_record, dict)
            }
            warnings.append("last_sale_at_loaded_from_lifecycle_source_record")
            sources["lifecycle_history"] = {
                "status": "ready",
                "mode": "source_record",
                "row_count": len(history),
            }
        else:
            warnings.append("lifecycle_last_sale_at_unavailable")
            sources["lifecycle_history"] = {
                "status": "unavailable",
                "mode": "missing_column",
                "row_count": 0,
            }
    else:
        warnings.append("assortment_lifecycle_classification_table_missing")
        sources["lifecycle_history"] = {
            "status": "unavailable",
            "mode": "missing_table",
            "row_count": 0,
        }

    open_order_codes: set[str] = set()
    order_tables_exist = _table_exists(
        session, ProcurementOrderFormation.__tablename__
    ) and _table_exists(session, ProcurementOrderFormationLine.__tablename__)
    required_order_columns = {
        "id",
        "order_date",
        "status",
    }
    required_line_columns = {
        "order_id",
        "nomenclature_code",
        "final_quantity",
        "removed",
    }
    order_columns_ready = order_tables_exist and required_order_columns.issubset(
        _column_names(session, ProcurementOrderFormation.__tablename__)
    )
    line_columns_ready = order_tables_exist and required_line_columns.issubset(
        _column_names(session, ProcurementOrderFormationLine.__tablename__)
    )
    if order_columns_ready and line_columns_ready:
        open_order_codes = {
            str(code or "").strip()
            for code in session.scalars(
                select(ProcurementOrderFormationLine.nomenclature_code)
                .join(
                    ProcurementOrderFormation,
                    ProcurementOrderFormation.id == ProcurementOrderFormationLine.order_id,
                )
                .where(
                    ProcurementOrderFormation.order_date >= history_cutoff,
                    ProcurementOrderFormation.order_date <= as_of,
                    ProcurementOrderFormation.status.not_in(TERMINAL_ORDER_STATUSES),
                    ProcurementOrderFormationLine.removed.is_(False),
                    ProcurementOrderFormationLine.final_quantity > 0,
                    ProcurementOrderFormationLine.nomenclature_code.is_not(None),
                )
            )
            if str(code or "").strip()
        }
        sources["procurement_orders"] = {
            "status": "ready",
            "open_or_recent_sku_count": len(open_order_codes),
        }
    else:
        warnings.append("procurement_order_source_unavailable")
        sources["procurement_orders"] = {
            "status": "unavailable",
            "open_or_recent_sku_count": 0,
        }

    evidence_by_code: dict[str, DisplayInventoryScopeEvidence] = {}
    for product in products:
        code = _product_code(product)
        evidence_by_code[code] = DisplayInventoryScopeEvidence(
            last_sale_at=history.get(code),
            current_stock_qty=current_stock_by_code.get(code, Decimal("0")),
            has_recent_or_open_order=code in open_order_codes,
        )
    return products, evidence_by_code, warnings, sources


def load_accepted_matching_evidence(
    session: Session,
    *,
    product_ids: set[int],
) -> tuple[
    dict[int, list[AcceptedCompetitorMatchEvidence]],
    dict[str, Any],
    list[str],
]:
    required_tables = {
        CompetitorItemMatch.__tablename__,
        CompetitorItem.__tablename__,
        CompetitorItemCompatibility.__tablename__,
        PhoneModel.__tablename__,
    }
    missing_tables = sorted(
        table_name for table_name in required_tables if not _table_exists(session, table_name)
    )
    if missing_tables:
        return (
            {},
            {
                "status": "unavailable",
                "accepted_link_count": 0,
                "missing_tables": missing_tables,
            },
            ["accepted_matching_source_unavailable"],
        )

    if not product_ids:
        return {}, {"status": "ready", "accepted_link_count": 0}, []

    matches = list(
        session.scalars(
            select(CompetitorItemMatch)
            .options(
                selectinload(CompetitorItemMatch.competitor_item)
                .selectinload(CompetitorItem.compatibilities)
                .selectinload(CompetitorItemCompatibility.phone_model)
            )
            .where(
                CompetitorItemMatch.status == CompetitorItemMatchStatus.ACCEPTED,
                CompetitorItemMatch.product_id.in_(sorted(product_ids)),
            )
            .order_by(CompetitorItemMatch.product_id, CompetitorItemMatch.competitor_item_id)
        )
        .unique()
        .all()
    )
    evidence_by_product_id: dict[int, list[AcceptedCompetitorMatchEvidence]] = {}
    for match in matches:
        item = match.competitor_item
        if item is None:
            continue
        method = getattr(match.method, "value", match.method)
        evidence_by_product_id.setdefault(int(match.product_id), []).append(
            AcceptedCompetitorMatchEvidence(
                competitor_item_id=int(match.competitor_item_id),
                competitor=str(item.competitor or "").strip(),
                competitor_name=str(item.name or item.normalized_title or "").strip(),
                method=str(method or "unknown"),
                identity=display_identity_for_competitor(item),
            )
        )
    loaded_count = sum(len(items) for items in evidence_by_product_id.values())
    warnings = []
    if loaded_count != len(matches):
        warnings.append("accepted_matching_item_missing")
    return (
        evidence_by_product_id,
        {
            "status": "ready",
            "accepted_link_count": loaded_count,
            "selected_match_row_count": len(matches),
        },
        warnings,
    )


def build_source_quality(
    *,
    as_of: date,
    stock_snapshot: CurrentStockSnapshot,
    application_sources: Mapping[str, Mapping[str, Any]],
    matching_source: Mapping[str, Any],
) -> dict[str, Any]:
    captured_business_date = stock_snapshot.captured_at.astimezone(BUSINESS_TIMEZONE).date()
    gate_values = {
        "application_catalog_nonempty": (
            application_sources.get("application_catalog", {}).get("status") == "ready"
        ),
        "lifecycle_history_readable": (
            application_sources.get("lifecycle_history", {}).get("status") == "ready"
        ),
        "procurement_orders_readable": (
            application_sources.get("procurement_orders", {}).get("status") == "ready"
        ),
        "current_stock_snapshot_nonempty": stock_snapshot.source_status == "ready",
        "current_stock_snapshot_fresh_for_as_of": captured_business_date == as_of,
        "accepted_matching_readable": matching_source.get("status") == "ready",
    }
    gates = {name: {"status": "pass" if passed else "fail"} for name, passed in gate_values.items()}
    stock_source = {
        "status": stock_snapshot.source_status,
        "captured_at": stock_snapshot.captured_at.isoformat(),
        "captured_business_date": captured_business_date.isoformat(),
        "source_period": stock_snapshot.source_period.isoformat(),
        "source_row_count": stock_snapshot.source_row_count,
        "product_code_count": stock_snapshot.product_code_count,
        "positive_row_count": stock_snapshot.positive_row_count,
        "positive_product_code_count": stock_snapshot.positive_product_code_count,
        "total_positive_quantity": str(stock_snapshot.total_positive_quantity),
        "total_net_quantity": str(stock_snapshot.total_net_quantity),
        "source_key": stock_snapshot.source_key,
        "source_title": stock_snapshot.source_title,
    }
    status = "ready" if all(gate_values.values()) else "blocked"
    return {
        "schema": "display_family_registry_preflight_source_quality.v1",
        "status": status,
        "as_of": as_of.isoformat(),
        "gates": gates,
        "sources": {
            **{key: dict(value) for key, value in application_sources.items()},
            "onec_current_stock": stock_source,
            "accepted_matching": dict(matching_source),
        },
    }


def _flatten_item(row: Mapping[str, Any]) -> dict[str, Any]:
    matching_audit = row.get("matching_audit") or {}
    return {
        "product_id": row.get("product_id"),
        "nomenclature_code": row.get("nomenclature_code"),
        "article": row.get("article"),
        "name": row.get("name"),
        "scope_reasons": ";".join(row.get("scope_reasons") or []),
        "scope_classification_reason": row.get("scope_classification_reason"),
        "scope_classification_warnings": ";".join(row.get("scope_classification_warnings") or []),
        "last_sale_at": row.get("last_sale_at"),
        "current_stock_qty": row.get("current_stock_qty"),
        "phone_model_ids": ";".join(str(value) for value in row.get("phone_model_ids") or []),
        "phone_models": "; ".join(
            " ".join(
                str(value or "").strip()
                for value in (model.get("brand"), model.get("model_name"), model.get("variant"))
                if str(value or "").strip()
            )
            for model in row.get("phone_models") or []
        ),
        "quality": row.get("quality"),
        "display_type": row.get("display_type"),
        "construction": row.get("construction"),
        "has_frame": row.get("has_frame"),
        "has_ic_pad": row.get("has_ic_pad"),
        "segment_id": row.get("segment_id"),
        "proposed_family_id": row.get("proposed_family_id"),
        "proposal_status": row.get("proposal_status"),
        "proposal_warnings": ";".join(row.get("proposal_warnings") or []),
        "proposal_notes": ";".join(row.get("proposal_notes") or []),
        "accepted_matching_count": matching_audit.get("accepted_count", 0),
        "accepted_matching_relations": ";".join(
            f"{key}={value}"
            for key, value in sorted((matching_audit.get("relation_counts") or {}).items())
        ),
        "accepted_matching_warnings": ";".join(matching_audit.get("warnings") or []),
        "requires_manual_review": row.get("requires_manual_review"),
        "identity_schema_version": row.get("identity_schema_version"),
        "identity_rules_version": row.get("identity_rules_version"),
        "available_at_status": row.get("available_at_status"),
    }


def _write_csv(path: Path, items: list[dict[str, Any]]) -> None:
    rows = [_flatten_item(item) for item in items]
    fields = list(rows[0]) if rows else ["nomenclature_code"]
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)


def _html_report(payload: Mapping[str, Any]) -> str:
    summary = payload["summary"]
    source_quality = payload["source_quality"]
    gate_rows = []
    for gate_name, gate in source_quality["gates"].items():
        gate_rows.append(
            "<tr>"
            f"<td>{html.escape(str(gate_name))}</td>"
            f"<td>{html.escape(str(gate['status']))}</td>"
            "</tr>"
        )
    rows = []
    for item in payload["items"]:
        matching_audit = item.get("matching_audit") or {}
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(item['nomenclature_code']))}</td>"
            f"<td>{html.escape(str(item['name']))}</td>"
            f"<td>{html.escape(str(item['scope_classification_reason']))}</td>"
            f"<td>{html.escape(str(item['current_stock_qty']))}</td>"
            f"<td>{html.escape(str(item['proposed_family_id']))}</td>"
            f"<td>{html.escape(str(item['segment_id']))}</td>"
            f"<td>{html.escape(str(matching_audit.get('accepted_count', 0)))}</td>"
            f"<td>{html.escape(', '.join(item['proposal_warnings']))}</td>"
            f"<td>{html.escape(', '.join(item.get('proposal_notes') or []))}</td>"
            "</tr>"
        )
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><title>Инвентаризация семейств дисплеев</title>
<style>body{{font:14px system-ui;margin:24px;color:#17202a}}table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #ccd1d1;padding:6px;vertical-align:top}}th{{background:#f4f6f7;position:sticky;top:0}}code{{background:#f4f6f7;padding:2px 4px}}</style></head>
<body><h1>Инвентаризация семейств дисплеев</h1>
<p>Дата: <code>{html.escape(str(payload['as_of']))}</code>; SKU: <strong>{summary['included_display_sku_count']}</strong>; предложенных семей: <strong>{summary['proposed_family_count']}</strong>; ручная проверка: <strong>{summary['manual_review_sku_count']}</strong>.</p>
<p>Режим: read-only. Заказы, статусы, 1С и production не изменялись.</p>
<h2>Качество источников: {html.escape(str(source_quality['status']))}</h2>
<table><thead><tr><th>Проверка</th><th>Статус</th></tr></thead><tbody>{''.join(gate_rows)}</tbody></table>
<h2>Предложения</h2>
<table><thead><tr><th>Код</th><th>Товар</th><th>Причина включения</th><th>Остаток</th><th>Предложенная семья</th><th>Сегмент</th><th>Accepted Matching</th><th>Предупреждения</th><th>Примечания</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
</body></html>"""


def write_artifacts(
    output_dir: Path,
    *,
    payload: dict[str, Any],
    source_quality: dict[str, Any],
    source_warnings: list[str],
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        **payload,
        "source_quality": source_quality,
        "source_warnings": sorted(set(source_warnings)),
    }
    json_path = output_dir / "inventory.json"
    csv_path = output_dir / "inventory.csv"
    html_path = output_dir / "report.html"
    _atomic_write(
        json_path,
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n",
    )
    _write_csv(csv_path, payload["items"])
    _atomic_write(html_path, _html_report(payload))
    source_quality_checksum = hashlib.sha256(
        json.dumps(
            source_quality,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "as_of": payload["as_of"],
        "inventory_checksum": payload["inventory_checksum"],
        "scope_policy_version": payload["scope_audit"]["scope_policy_version"],
        "scope_excluded_count": payload["scope_audit"]["excluded_item_count"],
        "scope_excluded_reason_counts": payload["scope_audit"]["excluded_reason_counts"],
        "source_quality_checksum": source_quality_checksum,
        "source_quality_status": source_quality["status"],
        "source_gates": source_quality["gates"],
        "status": (
            "complete_read_only"
            if source_quality["status"] == "ready"
            else "blocked_source_quality"
        ),
        "production_authorized": False,
        "external_writes": False,
        "artifact_sha256": {
            "inventory.json": _sha256(json_path),
            "inventory.csv": _sha256(csv_path),
            "report.html": _sha256(html_path),
        },
    }
    _atomic_write(
        output_dir / "manifest.json",
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )
    return manifest


def _subtract_months(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 - months
    year, month_offset = divmod(month_index, 12)
    month = month_offset + 1
    lengths = (
        31,
        29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
        31,
        30,
        31,
        30,
        31,
        31,
        30,
        31,
        30,
        31,
    )
    return date(year, month, min(value.day, lengths[month - 1]))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a read-only preflight inventory of all display families"
    )
    parser.add_argument(
        "--as-of",
        type=date.fromisoformat,
        default=datetime.now(BUSINESS_TIMEZONE).date(),
    )
    parser.add_argument("--history-months", type=int, default=24)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    if args.history_months <= 0:
        parser.error("--history-months must be positive")
    output_dir = args.output_dir or (
        DEFAULT_OUTPUT_ROOT / f"display-family-registry-preflight-v2-{args.as_of.isoformat()}"
    )
    cutoff = _subtract_months(args.as_of, args.history_months)
    captured_at = datetime.now(UTC)
    source_warnings: list[str] = []
    try:
        stock_snapshot = fetch_current_stock_snapshot(
            get_onec_engine(),
            captured_at=captured_at,
        )
    except (DatabaseNotConfiguredError, SQLAlchemyError):
        stock_snapshot = build_current_stock_snapshot([], captured_at=captured_at)
        source_warnings.append("onec_current_stock_source_unavailable")

    with session_scope(read_only=True) as session:
        products, evidence, application_warnings, application_sources = load_inventory_sources(
            session,
            as_of=args.as_of,
            history_cutoff=cutoff,
            current_stock_by_code=stock_snapshot.quantities_by_code,
        )
        provisional_payload = build_display_family_inventory(
            products,
            evidence_by_code=evidence,
            as_of=args.as_of,
            history_months=args.history_months,
        )
        included_product_ids = {
            int(item["product_id"])
            for item in provisional_payload["items"]
            if item.get("product_id") is not None
        }
        try:
            matching_evidence, matching_source, matching_warnings = load_accepted_matching_evidence(
                session,
                product_ids=included_product_ids,
            )
        except SQLAlchemyError:
            matching_evidence = {}
            matching_source = {
                "status": "unavailable",
                "accepted_link_count": 0,
                "reason": "query_failed",
            }
            matching_warnings = ["accepted_matching_query_failed"]
        payload = build_display_family_inventory(
            products,
            evidence_by_code=evidence,
            matching_evidence_by_product_id=matching_evidence,
            as_of=args.as_of,
            history_months=args.history_months,
        )
    source_warnings.extend(application_warnings)
    source_warnings.extend(matching_warnings)
    source_quality = build_source_quality(
        as_of=args.as_of,
        stock_snapshot=stock_snapshot,
        application_sources=application_sources,
        matching_source=matching_source,
    )
    manifest = write_artifacts(
        output_dir,
        payload=payload,
        source_quality=source_quality,
        source_warnings=source_warnings,
    )
    print(json.dumps({"output_dir": str(output_dir), **manifest}, ensure_ascii=False, indent=2))
    if manifest["status"] != "complete_read_only":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
