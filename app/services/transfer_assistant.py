from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from sqlalchemy import create_engine, text

from app.core.config import Settings, get_settings

STATUS_AVAILABLE_TO_TRANSFER = "available_to_transfer"
STATUS_RESERVED_FOR_ORDER = "reserved_for_order"
STATUS_PICKUP_WAITING = "pickup_waiting"
STATUS_PICKUP_EXPIRED = "pickup_expired"
STATUS_DISMANTLING_NEEDED = "dismantling_needed"
STATUS_MANUAL_REVIEW = "manual_review"

VALID_TRANSFER_ASSISTANT_STATUSES = {
    STATUS_AVAILABLE_TO_TRANSFER,
    STATUS_RESERVED_FOR_ORDER,
    STATUS_PICKUP_WAITING,
    STATUS_PICKUP_EXPIRED,
    STATUS_DISMANTLING_NEEDED,
    STATUS_MANUAL_REVIEW,
}

SOURCE_STOCK = "stock"
SOURCE_RESERVE = "reserve"
SOURCE_PLACEMENT = "placement"
SOURCE_ORDER = "order"
SOURCE_RTU = "rtu"
SOURCE_RETURN = "return"
SOURCE_TRANSFER = "transfer"

ALL_TRANSFER_ASSISTANT_SOURCE_KINDS = {
    SOURCE_STOCK,
    SOURCE_RESERVE,
    SOURCE_PLACEMENT,
    SOURCE_ORDER,
    SOURCE_RTU,
    SOURCE_RETURN,
    SOURCE_TRANSFER,
}


@dataclass(slots=True)
class TransferAssistantSourceRow:
    product_ref: str | None
    product_code: str | None
    product_name: str | None
    warehouse_ref: str | None
    warehouse_code: str | None
    warehouse_name: str | None
    quantity: Decimal
    fact_date: datetime | None
    data_source: str
    source_document_type: str | None = None
    source_document_ref: str | None = None
    source_document_number: str | None = None
    order_ref: str | None = None
    order_number: str | None = None
    site_order_number: str | None = None
    stock_quantity: Decimal = Decimal("0")
    reserved_quantity: Decimal = Decimal("0")
    placement_quantity: Decimal = Decimal("0")
    order_quantity: Decimal = Decimal("0")
    issued_quantity: Decimal = Decimal("0")
    return_quantity: Decimal = Decimal("0")
    pickup_deadline: datetime | None = None
    pickup_deadline_source: str | None = None
    delivery_method: str | None = None
    has_reserve_release: bool = False
    has_closing_document: bool = False
    has_issue_document: bool = False
    has_return_document: bool = False
    needs_dismantling: bool = False
    ambiguous_warehouse: bool = False
    missing_document: bool = False
    incomplete_data: bool = False
    manual_review_reason: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TransferAssistantCandidate:
    product: dict[str, Any]
    warehouse: dict[str, Any]
    order: dict[str, Any] | None
    source_document: dict[str, Any] | None
    quantity: Decimal
    status: str
    reason: str
    onec_document_keys: dict[str, str]
    fact_date: datetime | None
    data_source: str
    measures: dict[str, Decimal]
    pickup_deadline: datetime | None = None
    pickup_deadline_source: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "product": self.product,
            "warehouse": self.warehouse,
            "order": self.order,
            "source_document": self.source_document,
            "quantity": self.quantity,
            "status": self.status,
            "reason": self.reason,
            "onec_document_keys": self.onec_document_keys,
            "fact_date": self.fact_date,
            "data_source": self.data_source,
            "measures": self.measures,
            "pickup_deadline": self.pickup_deadline,
            "pickup_deadline_source": self.pickup_deadline_source,
        }


def list_transfer_assistant_candidates(
    *,
    date_from: date | datetime | None = None,
    date_to: date | datetime | None = None,
    warehouse_id: str | None = None,
    status: str | None = None,
    limit: int = 100,
    settings: Settings | None = None,
    source_rows: Iterable[dict[str, Any] | Any] | None = None,
    onec_engine: Any | None = None,
    as_of: datetime | None = None,
) -> list[dict[str, Any]]:
    if status and status not in VALID_TRANSFER_ASSISTANT_STATUSES:
        raise ValueError(f"unknown transfer assistant status: {status}")
    if status == STATUS_AVAILABLE_TO_TRANSFER and not warehouse_id:
        raise ValueError("available_to_transfer requires warehouse_id in v1")

    settings = settings or get_settings()
    bounded_limit = max(1, min(int(limit or 100), 1000))
    fetch_limit = 1000 if status else bounded_limit
    rows = (
        list(source_rows)
        if source_rows is not None
        else fetch_transfer_assistant_source_rows(
            settings=settings,
            onec_engine=onec_engine,
            date_from=date_from,
            date_to=date_to,
            warehouse_id=warehouse_id,
            limit=fetch_limit,
            source_kinds=_source_kinds_for_status(status),
        )
    )
    candidates = build_transfer_assistant_candidates(
        rows,
        as_of=as_of,
        pickup_hold_days=settings.logistics_transfer_assistant_pickup_hold_days,
    )
    if warehouse_id:
        candidates = [
            item
            for item in candidates
            if item.warehouse.get("ref") == warehouse_id
            or item.warehouse.get("code") == warehouse_id
            or str(item.warehouse.get("id") or "") == warehouse_id
        ]
    if status:
        candidates = [item for item in candidates if item.status == status]
    return [item.as_dict() for item in candidates[:bounded_limit]]


def build_transfer_assistant_candidates(
    rows: Iterable[dict[str, Any] | Any],
    *,
    as_of: datetime | None = None,
    pickup_hold_days: int | None = None,
) -> list[TransferAssistantCandidate]:
    now = as_of or datetime.now(timezone.utc)
    candidates: list[TransferAssistantCandidate] = []
    for raw_row in rows:
        row = normalize_transfer_assistant_source_row(raw_row)
        _apply_derived_pickup_deadline(row, pickup_hold_days=pickup_hold_days)
        candidate = classify_transfer_assistant_row(row, as_of=now)
        if candidate is not None:
            candidates.append(candidate)
    candidates.sort(
        key=lambda item: (
            _status_sort_key(item.status),
            item.fact_date or datetime.min.replace(tzinfo=timezone.utc),
            item.product.get("name") or "",
        ),
        reverse=True,
    )
    return candidates


def classify_transfer_assistant_row(
    row: TransferAssistantSourceRow,
    *,
    as_of: datetime,
) -> TransferAssistantCandidate | None:
    status, reason = _classify_status(row, as_of=as_of)
    if status is None:
        return None

    return TransferAssistantCandidate(
        product={
            "ref": row.product_ref,
            "code": row.product_code,
            "name": row.product_name,
        },
        warehouse={
            "ref": row.warehouse_ref,
            "code": row.warehouse_code,
            "name": row.warehouse_name,
        },
        order=(
            {
                "ref": row.order_ref,
                "number": row.order_number,
                "site_order_number": row.site_order_number,
            }
            if row.order_ref or row.order_number or row.site_order_number
            else None
        ),
        source_document=(
            {
                "type": row.source_document_type,
                "ref": row.source_document_ref,
                "number": row.source_document_number,
            }
            if row.source_document_type or row.source_document_ref or row.source_document_number
            else None
        ),
        quantity=_candidate_quantity(row, status),
        status=status,
        reason=reason,
        onec_document_keys=_document_keys(row),
        fact_date=row.fact_date,
        data_source=row.data_source,
        measures={
            "stock_quantity": row.stock_quantity,
            "reserved_quantity": row.reserved_quantity,
            "placement_quantity": row.placement_quantity,
            "order_quantity": row.order_quantity,
            "issued_quantity": row.issued_quantity,
            "return_quantity": row.return_quantity,
        },
        pickup_deadline=row.pickup_deadline,
        pickup_deadline_source=row.pickup_deadline_source,
    )


def fetch_transfer_assistant_source_rows(
    *,
    settings: Settings | None = None,
    onec_engine: Any | None = None,
    date_from: date | datetime | None = None,
    date_to: date | datetime | None = None,
    warehouse_id: str | None = None,
    limit: int = 100,
    source_kinds: set[str] | None = None,
) -> list[Any]:
    settings = settings or get_settings()
    engine = onec_engine
    if engine is None:
        if not settings.onec_database_url:
            raise RuntimeError("ONEC_DATABASE_URL is not configured")
        engine = create_engine(settings.onec_database_url, pool_pre_ping=True)

    normalized_source_kinds = set(source_kinds) if source_kinds is not None else None
    with engine.connect() as connection:
        if normalized_source_kinds == {SOURCE_STOCK}:
            return _fetch_transfer_assistant_stock_source_rows(
                connection,
                date_from=date_from,
                date_to=date_to,
                warehouse_id=warehouse_id,
                limit=limit,
            )
        statement, params = _transfer_assistant_source_statement(
            date_from=date_from,
            date_to=date_to,
            warehouse_id=warehouse_id,
            limit=limit,
            source_kinds=normalized_source_kinds,
        )
        return list(connection.execute(statement, params))


def _fetch_transfer_assistant_stock_source_rows(
    connection: Any,
    *,
    date_from: date | datetime | None,
    date_to: date | datetime | None,
    warehouse_id: str | None,
    limit: int,
) -> list[Any]:
    bounded_limit = max(1, min(int(limit or 100), 1000))
    stock_candidate_limit = min(max(bounded_limit * 5, bounded_limit), 5000)
    stock_date_from_filter = "AND stock._Period >= :date_from" if date_from else ""
    stock_date_to_filter = "AND stock._Period < :date_to" if date_to else ""
    stock_warehouse_filter = (
        "AND (CONVERT(varchar(34), stock._Fld7742RRef, 1) = :warehouse_id "
        "OR NULLIF(LTRIM(RTRIM(warehouse._Code)), N'') = :warehouse_id)"
        if warehouse_id
        else ""
    )
    params: dict[str, Any] = {}
    if date_from:
        params["date_from"] = _date_boundary(date_from, end=False)
    if date_to:
        params["date_to"] = _date_boundary(date_to, end=True)
    if warehouse_id:
        params["warehouse_id"] = warehouse_id

    connection.execute(text("""
        IF OBJECT_ID('tempdb..#transfer_assistant_stock_candidates') IS NOT NULL
            DROP TABLE #transfer_assistant_stock_candidates
        CREATE TABLE #transfer_assistant_stock_candidates (
            _Period datetime NULL,
            _Fld7738RRef binary(16) NOT NULL,
            _Fld7742RRef binary(16) NOT NULL,
            _Fld7743 decimal(18, 3) NOT NULL,
            product_ref_bin binary(16) NOT NULL,
            product_code nvarchar(100) NULL,
            product_name nvarchar(512) NULL,
            warehouse_ref_bin binary(16) NOT NULL,
            warehouse_code nvarchar(100) NULL,
            warehouse_name nvarchar(512) NULL
        )
        """))
    try:
        connection.execute(
            text(f"""
            INSERT INTO #transfer_assistant_stock_candidates (
                _Period,
                _Fld7738RRef,
                _Fld7742RRef,
                _Fld7743,
                product_ref_bin,
                product_code,
                product_name,
                warehouse_ref_bin,
                warehouse_code,
                warehouse_name
            )
            SELECT TOP ({stock_candidate_limit})
                stock._Period,
                stock._Fld7738RRef,
                stock._Fld7742RRef,
                CAST(stock._Fld7743 AS decimal(18, 3)),
                product._IDRRef,
                NULLIF(LTRIM(RTRIM(product._Code)), N''),
                NULLIF(LTRIM(RTRIM(product._Description)), N''),
                warehouse._IDRRef,
                NULLIF(LTRIM(RTRIM(warehouse._Code)), N''),
                NULLIF(LTRIM(RTRIM(warehouse._Description)), N'')
            FROM dbo._AccumRgT7745 AS stock WITH (NOLOCK)
            JOIN dbo._Reference62 AS product WITH (NOLOCK)
                ON product._IDRRef = stock._Fld7738RRef
            JOIN dbo._Reference80 AS warehouse WITH (NOLOCK)
                ON warehouse._IDRRef = stock._Fld7742RRef
            WHERE stock._Fld7743 > 0
              {stock_date_from_filter}
              {stock_date_to_filter}
              {stock_warehouse_filter}
            """),
            params,
        )
        return list(connection.execute(text(f"""
                SELECT TOP ({bounded_limit})
                    CONVERT(varchar(34), stock.product_ref_bin, 1) AS product_ref,
                    stock.product_code AS product_code,
                    stock.product_name AS product_name,
                    CONVERT(varchar(34), stock.warehouse_ref_bin, 1) AS warehouse_ref,
                    stock.warehouse_code AS warehouse_code,
                    stock.warehouse_name AS warehouse_name,
                    stock._Fld7743 AS quantity,
                    stock._Fld7743 AS stock_quantity,
                    CAST(0 AS decimal(18, 3)) AS reserved_quantity,
                    CAST(0 AS decimal(18, 3)) AS placement_quantity,
                    CAST(0 AS decimal(18, 3)) AS order_quantity,
                    CAST(0 AS decimal(18, 3)) AS issued_quantity,
                    CAST(0 AS decimal(18, 3)) AS return_quantity,
                    stock._Period AS fact_date,
                    N'1c:stock_totals' AS data_source,
                    NULL AS source_document_type,
                    NULL AS source_document_ref,
                    NULL AS source_document_number,
                    NULL AS order_ref,
                    NULL AS order_number,
                    NULL AS site_order_number,
                    NULL AS delivery_method,
                    NULL AS pickup_deadline,
                    NULL AS pickup_deadline_source,
                    0 AS has_reserve_release,
                    0 AS has_closing_document,
                    0 AS has_issue_document,
                    0 AS has_return_document,
                    0 AS needs_dismantling,
                    0 AS ambiguous_warehouse,
                    0 AS missing_document,
                    0 AS incomplete_data,
                    NULL AS manual_review_reason
                FROM #transfer_assistant_stock_candidates AS stock
                WHERE stock._Fld7743 > 0
                  AND NOT EXISTS (
                      SELECT 1
                      FROM dbo._Document132 AS block_order WITH (NOLOCK)
                      JOIN dbo._Document132_VT2427 AS block_order_line WITH (NOLOCK)
                          ON block_order_line._Document132_IDRRef = block_order._IDRRef
                      WHERE block_order._Marked = 0x00
                        AND block_order._Posted = 0x01
                        AND block_order_line._Fld2434RRef = stock._Fld7738RRef
                        AND block_order_line._Fld2431 > 0
                        AND (
                            CASE
                                WHEN block_order_line._Fld2437_RRRef
                                    <> 0x00000000000000000000000000000000
                                THEN block_order_line._Fld2437_RRRef
                                ELSE block_order._Fld2413_RRRef
                            END
                        ) = stock._Fld7742RRef
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM dbo._Document203 AS block_rtu WITH (NOLOCK)
                      JOIN dbo._Document203_VT4966 AS block_rtu_line WITH (NOLOCK)
                          ON block_rtu_line._Document203_IDRRef = block_rtu._IDRRef
                      WHERE block_rtu._Marked = 0x00
                        AND block_rtu._Posted = 0x01
                        AND block_rtu_line._Fld4974RRef = stock._Fld7738RRef
                        AND block_rtu_line._Fld4971 > 0
                        AND (
                            CASE
                                WHEN block_rtu_line._Fld4983RRef
                                    <> 0x00000000000000000000000000000000
                                THEN block_rtu_line._Fld4983RRef
                                ELSE block_rtu._Fld4940RRef
                            END
                        ) = stock._Fld7742RRef
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM dbo._Document178 AS block_transfer WITH (NOLOCK)
                      JOIN dbo._Document178_VT3822 AS block_transfer_line WITH (NOLOCK)
                          ON block_transfer_line._Document178_IDRRef = block_transfer._IDRRef
                      WHERE block_transfer._Marked = 0x00
                        AND block_transfer._Posted = 0x01
                        AND block_transfer_line._Fld3824RRef = stock._Fld7738RRef
                        AND block_transfer_line._Fld3829 > 0
                        AND block_transfer._Fld3819RRef = stock._Fld7742RRef
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM dbo._AccumRgT7662 AS block_reserve WITH (NOLOCK)
                      WHERE block_reserve._Fld7659 > 0
                        AND block_reserve._Fld7655RRef = stock._Fld7738RRef
                        AND block_reserve._Fld7654RRef = stock._Fld7742RRef
                        AND block_reserve._Fld7657_RTRef = 0x00000084
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM dbo._AccumRgT7606 AS block_placement WITH (NOLOCK)
                      JOIN dbo._Document133 AS block_supplier_order WITH (NOLOCK)
                          ON block_supplier_order._IDRRef = block_placement._Fld7601_RRRef
                      WHERE block_placement._Fld7602 > 0
                        AND block_placement._Fld7598RRef = stock._Fld7738RRef
                        AND block_placement._Fld7600_RTRef = 0x00000084
                        AND block_placement._Fld7601_RTRef = 0x00000085
                        AND block_supplier_order._Fld2506RRef = stock._Fld7742RRef
                  )
                """)))
    finally:
        try:
            connection.execute(text("""
                IF OBJECT_ID('tempdb..#transfer_assistant_stock_candidates') IS NOT NULL
                    DROP TABLE #transfer_assistant_stock_candidates
                """))
        except Exception:
            pass


def _source_kinds_for_status(status: str | None) -> set[str] | None:
    if status == STATUS_AVAILABLE_TO_TRANSFER:
        return {SOURCE_STOCK}
    if status == STATUS_RESERVED_FOR_ORDER:
        return {SOURCE_RESERVE, SOURCE_PLACEMENT, SOURCE_ORDER}
    if status in {STATUS_PICKUP_WAITING, STATUS_PICKUP_EXPIRED, STATUS_DISMANTLING_NEEDED}:
        return {SOURCE_RESERVE, SOURCE_PLACEMENT, SOURCE_ORDER, SOURCE_RTU}
    if status == STATUS_MANUAL_REVIEW:
        return {SOURCE_RESERVE, SOURCE_PLACEMENT, SOURCE_RTU, SOURCE_RETURN, SOURCE_TRANSFER}
    return None


def normalize_transfer_assistant_source_row(
    row: dict[str, Any] | Any,
) -> TransferAssistantSourceRow:
    values = _row_to_dict(row)
    stock_quantity = _decimal(values.get("stock_quantity"))
    reserved_quantity = _decimal(values.get("reserved_quantity"))
    placement_quantity = _decimal(values.get("placement_quantity"))
    order_quantity = _decimal(values.get("order_quantity"))
    quantity = _decimal(values.get("quantity"))
    if quantity == 0:
        quantity = max(
            stock_quantity,
            reserved_quantity,
            placement_quantity,
            order_quantity,
            _decimal(values.get("issued_quantity")),
            _decimal(values.get("return_quantity")),
        )
    return TransferAssistantSourceRow(
        product_ref=_clean_string(values.get("product_ref")),
        product_code=_clean_string(values.get("product_code")),
        product_name=_clean_string(values.get("product_name")),
        warehouse_ref=_clean_string(values.get("warehouse_ref")),
        warehouse_code=_clean_string(values.get("warehouse_code")),
        warehouse_name=_clean_string(values.get("warehouse_name")),
        quantity=quantity,
        fact_date=_datetime_value(values.get("fact_date")),
        data_source=_clean_string(values.get("data_source")) or "1c",
        source_document_type=_clean_string(values.get("source_document_type")),
        source_document_ref=_clean_string(values.get("source_document_ref")),
        source_document_number=_clean_string(values.get("source_document_number")),
        order_ref=_clean_string(values.get("order_ref")),
        order_number=_clean_string(values.get("order_number")),
        site_order_number=_clean_string(values.get("site_order_number")),
        stock_quantity=stock_quantity,
        reserved_quantity=reserved_quantity,
        placement_quantity=placement_quantity,
        order_quantity=order_quantity,
        issued_quantity=_decimal(values.get("issued_quantity")),
        return_quantity=_decimal(values.get("return_quantity")),
        pickup_deadline=_datetime_value(values.get("pickup_deadline")),
        pickup_deadline_source=_clean_string(values.get("pickup_deadline_source")),
        delivery_method=_clean_string(values.get("delivery_method")),
        has_reserve_release=_bool_value(values.get("has_reserve_release")),
        has_closing_document=_bool_value(values.get("has_closing_document")),
        has_issue_document=_bool_value(values.get("has_issue_document")),
        has_return_document=_bool_value(values.get("has_return_document")),
        needs_dismantling=_bool_value(values.get("needs_dismantling")),
        ambiguous_warehouse=_bool_value(values.get("ambiguous_warehouse")),
        missing_document=_bool_value(values.get("missing_document")),
        incomplete_data=_bool_value(values.get("incomplete_data")),
        manual_review_reason=_clean_string(values.get("manual_review_reason")),
        raw=values,
    )


def _apply_derived_pickup_deadline(
    row: TransferAssistantSourceRow,
    *,
    pickup_hold_days: int | None,
) -> None:
    if row.pickup_deadline or not _is_pickup(row) or not row.fact_date:
        return
    hold_days = max(int(pickup_hold_days or 0), 0)
    if hold_days <= 0:
        return
    row.pickup_deadline = row.fact_date + timedelta(days=hold_days)
    row.pickup_deadline_source = "derived"


def _classify_status(
    row: TransferAssistantSourceRow,
    *,
    as_of: datetime,
) -> tuple[str | None, str]:
    if row.has_closing_document or row.has_reserve_release:
        if row.stock_quantity > row.reserved_quantity + row.placement_quantity and not _has_order(
            row
        ):
            return (
                STATUS_AVAILABLE_TO_TRANSFER,
                "closing or reserve release exists; remaining stock is not blocked",
            )
        return None, "closed or reserve released"

    manual_reason = _manual_review_reason(row)
    if manual_reason:
        return STATUS_MANUAL_REVIEW, manual_reason

    if row.needs_dismantling:
        return STATUS_DISMANTLING_NEEDED, "pickup/order requires reserve release or dismantling"

    if _is_pickup(row):
        if row.pickup_deadline and row.pickup_deadline < _normalize_as_of(as_of):
            return (
                STATUS_PICKUP_EXPIRED,
                "pickup deadline passed but reserve, placement, or closing document still needs check",
            )
        if _blocked_quantity(row) > 0 or row.source_document_type == "rtu":
            return STATUS_PICKUP_WAITING, "pickup order is waiting for customer collection"

    if row.source_document_type == "rtu" or row.has_issue_document:
        return None, "issue document is already posted for a non-pickup order"

    if row.reserved_quantity > 0 or _has_order(row):
        return STATUS_RESERVED_FOR_ORDER, "stock is reserved or linked to a customer order"

    if row.stock_quantity > row.placement_quantity:
        return STATUS_AVAILABLE_TO_TRANSFER, "stock has no reserve, placement, or blocking order"

    if row.quantity > 0:
        return STATUS_MANUAL_REVIEW, "positive quantity has no clear operational status"
    return None, "empty quantity"


def _transfer_assistant_source_statement(
    *,
    date_from: date | datetime | None,
    date_to: date | datetime | None,
    warehouse_id: str | None,
    limit: int,
    source_kinds: set[str] | None = None,
):
    bounded_limit = max(1, min(int(limit or 100), 1000))
    stock_candidate_limit = min(max(bounded_limit * 5, bounded_limit), 5000)
    enabled_source_kinds = set(source_kinds or ALL_TRANSFER_ASSISTANT_SOURCE_KINDS)
    stock_source_filter = "" if SOURCE_STOCK in enabled_source_kinds else "AND 1 = 0"
    reserve_source_filter = "" if SOURCE_RESERVE in enabled_source_kinds else "AND 1 = 0"
    placement_source_filter = "" if SOURCE_PLACEMENT in enabled_source_kinds else "AND 1 = 0"
    order_source_filter = "" if SOURCE_ORDER in enabled_source_kinds else "AND 1 = 0"
    rtu_source_filter = "" if SOURCE_RTU in enabled_source_kinds else "AND 1 = 0"
    return_source_filter = "" if SOURCE_RETURN in enabled_source_kinds else "AND 1 = 0"
    transfer_source_filter = "" if SOURCE_TRANSFER in enabled_source_kinds else "AND 1 = 0"
    date_from_filter = "AND fact_date >= :date_from" if date_from else ""
    date_to_filter = "AND fact_date < :date_to" if date_to else ""
    warehouse_filter = (
        "AND (warehouse_ref = :warehouse_id OR warehouse_code = :warehouse_id)"
        if warehouse_id
        else ""
    )
    stock_date_from_filter = "AND stock._Period >= :date_from" if date_from else ""
    stock_date_to_filter = "AND stock._Period < :date_to" if date_to else ""
    stock_warehouse_filter = (
        "AND (CONVERT(varchar(34), stock._Fld7742RRef, 1) = :warehouse_id "
        "OR NULLIF(LTRIM(RTRIM(warehouse._Code)), N'') = :warehouse_id)"
        if warehouse_id
        else ""
    )
    order_date_from_filter = "AND customer_order._Date_Time >= :date_from" if date_from else ""
    order_date_to_filter = "AND customer_order._Date_Time < :date_to" if date_to else ""
    order_warehouse_filter = (
        "AND (CONVERT(varchar(34), warehouse._IDRRef, 1) = :warehouse_id "
        "OR NULLIF(LTRIM(RTRIM(warehouse._Code)), N'') = :warehouse_id)"
        if warehouse_id
        else ""
    )
    reserve_date_from_filter = (
        "AND COALESCE(customer_order._Date_Time, reserve._Period) >= :date_from"
        if date_from
        else ""
    )
    reserve_date_to_filter = (
        "AND COALESCE(customer_order._Date_Time, reserve._Period) < :date_to" if date_to else ""
    )
    reserve_warehouse_filter = order_warehouse_filter
    placement_date_from_filter = (
        "AND COALESCE(customer_order._Date_Time, supplier_order._Date_Time, placement._Period) "
        ">= :date_from"
        if date_from
        else ""
    )
    placement_date_to_filter = (
        "AND COALESCE(customer_order._Date_Time, supplier_order._Date_Time, placement._Period) "
        "< :date_to"
        if date_to
        else ""
    )
    placement_warehouse_filter = (
        "AND (CONVERT(varchar(34), supplier_warehouse._IDRRef, 1) = :warehouse_id "
        "OR NULLIF(LTRIM(RTRIM(supplier_warehouse._Code)), N'') = :warehouse_id)"
        if warehouse_id
        else ""
    )
    rtu_date_from_filter = "AND rtu._Date_Time >= :date_from" if date_from else ""
    rtu_date_to_filter = "AND rtu._Date_Time < :date_to" if date_to else ""
    rtu_warehouse_filter = order_warehouse_filter
    return_date_from_filter = "AND customer_return._Date_Time >= :date_from" if date_from else ""
    return_date_to_filter = "AND customer_return._Date_Time < :date_to" if date_to else ""
    return_warehouse_filter = order_warehouse_filter
    transfer_date_from_filter = "AND transfer_doc._Date_Time >= :date_from" if date_from else ""
    transfer_date_to_filter = "AND transfer_doc._Date_Time < :date_to" if date_to else ""
    transfer_warehouse_filter = (
        "AND (CONVERT(varchar(34), source_warehouse._IDRRef, 1) = :warehouse_id "
        "OR NULLIF(LTRIM(RTRIM(source_warehouse._Code)), N'') = :warehouse_id)"
        if warehouse_id
        else ""
    )
    params: dict[str, Any] = {}
    if date_from:
        params["date_from"] = _date_boundary(date_from, end=False)
    if date_to:
        params["date_to"] = _date_boundary(date_to, end=True)
    if warehouse_id:
        params["warehouse_id"] = warehouse_id

    statement = text(f"""
        WITH stock_candidates AS (
            SELECT TOP ({stock_candidate_limit})
                stock._Period AS _Period,
                stock._Fld7738RRef AS _Fld7738RRef,
                stock._Fld7742RRef AS _Fld7742RRef,
                stock._Fld7743 AS _Fld7743,
                product._IDRRef AS product_ref_bin,
                NULLIF(LTRIM(RTRIM(product._Code)), N'') AS product_code,
                NULLIF(LTRIM(RTRIM(product._Description)), N'') AS product_name,
                warehouse._IDRRef AS warehouse_ref_bin,
                NULLIF(LTRIM(RTRIM(warehouse._Code)), N'') AS warehouse_code,
                NULLIF(LTRIM(RTRIM(warehouse._Description)), N'') AS warehouse_name
            FROM dbo._AccumRgT7745 AS stock WITH (NOLOCK)
            JOIN dbo._Reference62 AS product WITH (NOLOCK)
                ON product._IDRRef = stock._Fld7738RRef
            JOIN dbo._Reference80 AS warehouse WITH (NOLOCK)
                ON warehouse._IDRRef = stock._Fld7742RRef
            WHERE stock._Fld7743 > 0
              {stock_source_filter}
              {stock_date_from_filter}
              {stock_date_to_filter}
              {stock_warehouse_filter}
        ),
        source_rows AS (
        SELECT TOP ({bounded_limit})
            CONVERT(varchar(34), stock.product_ref_bin, 1) AS product_ref,
            stock.product_code AS product_code,
            stock.product_name AS product_name,
            CONVERT(varchar(34), stock.warehouse_ref_bin, 1) AS warehouse_ref,
            stock.warehouse_code AS warehouse_code,
            stock.warehouse_name AS warehouse_name,
            CAST(stock._Fld7743 AS decimal(18, 3)) AS quantity,
            CAST(stock._Fld7743 AS decimal(18, 3)) AS stock_quantity,
            CAST(0 AS decimal(18, 3)) AS reserved_quantity,
            CAST(0 AS decimal(18, 3)) AS placement_quantity,
            CAST(0 AS decimal(18, 3)) AS order_quantity,
            CAST(0 AS decimal(18, 3)) AS issued_quantity,
            CAST(0 AS decimal(18, 3)) AS return_quantity,
            stock._Period AS fact_date,
            N'1c:stock_totals' AS data_source,
            NULL AS source_document_type,
            NULL AS source_document_ref,
            NULL AS source_document_number,
            NULL AS order_ref,
            NULL AS order_number,
            NULL AS site_order_number,
            NULL AS delivery_method,
            NULL AS pickup_deadline,
            NULL AS pickup_deadline_source,
            0 AS has_reserve_release,
            0 AS has_closing_document,
            0 AS has_issue_document,
            0 AS has_return_document,
            0 AS needs_dismantling,
            0 AS ambiguous_warehouse,
            0 AS missing_document,
            0 AS incomplete_data,
            NULL AS manual_review_reason
        FROM stock_candidates AS stock
        WHERE stock._Fld7743 > 0
          AND NOT EXISTS (
              SELECT 1
              FROM dbo._Document132 AS block_order WITH (NOLOCK)
              JOIN dbo._Document132_VT2427 AS block_order_line WITH (NOLOCK)
                  ON block_order_line._Document132_IDRRef = block_order._IDRRef
              WHERE block_order._Marked = 0x00
                AND block_order._Posted = 0x01
                AND block_order_line._Fld2434RRef = stock._Fld7738RRef
                AND block_order_line._Fld2431 > 0
                AND (
                    CASE
                        WHEN block_order_line._Fld2437_RRRef
                            <> 0x00000000000000000000000000000000
                        THEN block_order_line._Fld2437_RRRef
                        ELSE block_order._Fld2413_RRRef
                    END
                ) = stock._Fld7742RRef
          )
          AND NOT EXISTS (
              SELECT 1
              FROM dbo._Document203 AS block_rtu WITH (NOLOCK)
              JOIN dbo._Document203_VT4966 AS block_rtu_line WITH (NOLOCK)
                  ON block_rtu_line._Document203_IDRRef = block_rtu._IDRRef
              WHERE block_rtu._Marked = 0x00
                AND block_rtu._Posted = 0x01
                AND block_rtu_line._Fld4974RRef = stock._Fld7738RRef
                AND block_rtu_line._Fld4971 > 0
                AND (
                    CASE
                        WHEN block_rtu_line._Fld4983RRef
                            <> 0x00000000000000000000000000000000
                        THEN block_rtu_line._Fld4983RRef
                        ELSE block_rtu._Fld4940RRef
                    END
                ) = stock._Fld7742RRef
          )
          AND NOT EXISTS (
              SELECT 1
              FROM dbo._Document178 AS block_transfer WITH (NOLOCK)
              JOIN dbo._Document178_VT3822 AS block_transfer_line WITH (NOLOCK)
                  ON block_transfer_line._Document178_IDRRef = block_transfer._IDRRef
              WHERE block_transfer._Marked = 0x00
                AND block_transfer._Posted = 0x01
                AND block_transfer_line._Fld3824RRef = stock._Fld7738RRef
                AND block_transfer_line._Fld3829 > 0
                AND block_transfer._Fld3819RRef = stock._Fld7742RRef
          )
          AND NOT EXISTS (
              SELECT 1
              FROM dbo._AccumRgT7662 AS block_reserve WITH (NOLOCK)
              WHERE block_reserve._Fld7659 > 0
                AND block_reserve._Fld7655RRef = stock._Fld7738RRef
                AND block_reserve._Fld7654RRef = stock._Fld7742RRef
                AND block_reserve._Fld7657_RTRef = 0x00000084
          )
          AND NOT EXISTS (
              SELECT 1
              FROM dbo._AccumRgT7606 AS block_placement WITH (NOLOCK)
              JOIN dbo._Document133 AS block_supplier_order WITH (NOLOCK)
                  ON block_supplier_order._IDRRef = block_placement._Fld7601_RRRef
              WHERE block_placement._Fld7602 > 0
                AND block_placement._Fld7598RRef = stock._Fld7738RRef
                AND block_placement._Fld7600_RTRef = 0x00000084
                AND block_placement._Fld7601_RTRef = 0x00000085
                AND block_supplier_order._Fld2506RRef = stock._Fld7742RRef
          )

        UNION ALL

        SELECT TOP ({bounded_limit})
            CONVERT(varchar(34), product._IDRRef, 1) AS product_ref,
            NULLIF(LTRIM(RTRIM(product._Code)), N'') AS product_code,
            NULLIF(LTRIM(RTRIM(product._Description)), N'') AS product_name,
            CONVERT(varchar(34), warehouse._IDRRef, 1) AS warehouse_ref,
            NULLIF(LTRIM(RTRIM(warehouse._Code)), N'') AS warehouse_code,
            NULLIF(LTRIM(RTRIM(warehouse._Description)), N'') AS warehouse_name,
            CAST(reserve._Fld7659 AS decimal(18, 3)) AS quantity,
            CAST(0 AS decimal(18, 3)) AS stock_quantity,
            CAST(reserve._Fld7659 AS decimal(18, 3)) AS reserved_quantity,
            CAST(0 AS decimal(18, 3)) AS placement_quantity,
            CAST(0 AS decimal(18, 3)) AS order_quantity,
            CAST(0 AS decimal(18, 3)) AS issued_quantity,
            CAST(0 AS decimal(18, 3)) AS return_quantity,
            COALESCE(customer_order._Date_Time, reserve._Period) AS fact_date,
            N'1c:reserved_stock_totals' AS data_source,
            N'reserve_register' AS source_document_type,
            CONVERT(varchar(34), reserve._Fld7657_RRRef, 1) AS source_document_ref,
            NULLIF(LTRIM(RTRIM(customer_order._Number)), N'') AS source_document_number,
            CONVERT(varchar(34), customer_order._IDRRef, 1) AS order_ref,
            NULLIF(LTRIM(RTRIM(customer_order._Number)), N'') AS order_number,
            NULLIF(LTRIM(RTRIM(customer_order._Fld2425)), N'') AS site_order_number,
            NULLIF(LTRIM(RTRIM(customer_order._Fld9266)), N'') AS delivery_method,
            CAST(NULL AS datetime) AS pickup_deadline,
            CAST(NULL AS nvarchar(32)) AS pickup_deadline_source,
            0 AS has_reserve_release,
            CASE WHEN close_doc.order_ref IS NULL THEN 0 ELSE 1 END AS has_closing_document,
            0 AS has_issue_document,
            0 AS has_return_document,
            0 AS needs_dismantling,
            0 AS ambiguous_warehouse,
            CASE WHEN customer_order._IDRRef IS NULL THEN 1 ELSE 0 END AS missing_document,
            0 AS incomplete_data,
            NULL AS manual_review_reason
        FROM dbo._AccumRgT7662 AS reserve WITH (NOLOCK)
        JOIN dbo._Reference62 AS product WITH (NOLOCK)
            ON product._IDRRef = reserve._Fld7655RRef
        JOIN dbo._Reference80 AS warehouse WITH (NOLOCK)
            ON warehouse._IDRRef = reserve._Fld7654RRef
        LEFT JOIN dbo._Document132 AS customer_order WITH (NOLOCK)
            ON customer_order._IDRRef = reserve._Fld7657_RRRef
           AND reserve._Fld7657_RTRef = 0x00000084
        OUTER APPLY (
            SELECT TOP (1)
                close_line._Fld2571_RRRef AS order_ref
            FROM dbo._Document135 AS close_header WITH (NOLOCK)
            JOIN dbo._Document135_VT2569 AS close_line WITH (NOLOCK)
                ON close_line._Document135_IDRRef = close_header._IDRRef
            WHERE close_header._Marked = 0x00
              AND close_header._Posted = 0x01
              AND close_line._Fld2571_RTRef = 0x00000084
              AND close_line._Fld2571_RRRef = customer_order._IDRRef
        ) AS close_doc
        WHERE reserve._Fld7659 > 0
          {reserve_source_filter}
          AND reserve._Fld7655RRef <> 0x00000000000000000000000000000000
          AND reserve._Fld7654RRef <> 0x00000000000000000000000000000000
          AND reserve._Fld7657_RTRef = 0x00000084
          {reserve_date_from_filter}
          {reserve_date_to_filter}
          {reserve_warehouse_filter}

        UNION ALL

        SELECT TOP ({bounded_limit})
            CONVERT(varchar(34), product._IDRRef, 1) AS product_ref,
            NULLIF(LTRIM(RTRIM(product._Code)), N'') AS product_code,
            NULLIF(LTRIM(RTRIM(product._Description)), N'') AS product_name,
            CONVERT(varchar(34), supplier_warehouse._IDRRef, 1) AS warehouse_ref,
            NULLIF(LTRIM(RTRIM(supplier_warehouse._Code)), N'') AS warehouse_code,
            NULLIF(LTRIM(RTRIM(supplier_warehouse._Description)), N'') AS warehouse_name,
            CAST(placement._Fld7602 AS decimal(18, 3)) AS quantity,
            CAST(0 AS decimal(18, 3)) AS stock_quantity,
            CAST(0 AS decimal(18, 3)) AS reserved_quantity,
            CAST(placement._Fld7602 AS decimal(18, 3)) AS placement_quantity,
            CAST(0 AS decimal(18, 3)) AS order_quantity,
            CAST(0 AS decimal(18, 3)) AS issued_quantity,
            CAST(0 AS decimal(18, 3)) AS return_quantity,
            COALESCE(customer_order._Date_Time, supplier_order._Date_Time, placement._Period)
                AS fact_date,
            N'1c:customer_order_placements' AS data_source,
            N'supplier_order' AS source_document_type,
            CONVERT(varchar(34), supplier_order._IDRRef, 1) AS source_document_ref,
            NULLIF(LTRIM(RTRIM(supplier_order._Number)), N'') AS source_document_number,
            CONVERT(varchar(34), customer_order._IDRRef, 1) AS order_ref,
            NULLIF(LTRIM(RTRIM(customer_order._Number)), N'') AS order_number,
            NULLIF(LTRIM(RTRIM(customer_order._Fld2425)), N'') AS site_order_number,
            NULLIF(LTRIM(RTRIM(customer_order._Fld9266)), N'') AS delivery_method,
            CAST(NULL AS datetime) AS pickup_deadline,
            CAST(NULL AS nvarchar(32)) AS pickup_deadline_source,
            0 AS has_reserve_release,
            CASE WHEN close_doc.order_ref IS NULL THEN 0 ELSE 1 END AS has_closing_document,
            0 AS has_issue_document,
            0 AS has_return_document,
            0 AS needs_dismantling,
            0 AS ambiguous_warehouse,
            CASE
                WHEN customer_order._IDRRef IS NULL
                  OR supplier_order._IDRRef IS NULL
                  OR supplier_warehouse._IDRRef IS NULL
                THEN 1
                ELSE 0
            END AS missing_document,
            0 AS incomplete_data,
            NULL AS manual_review_reason
        FROM dbo._AccumRgT7606 AS placement WITH (NOLOCK)
        JOIN dbo._Reference62 AS product WITH (NOLOCK)
            ON product._IDRRef = placement._Fld7598RRef
        LEFT JOIN dbo._Document132 AS customer_order WITH (NOLOCK)
            ON customer_order._IDRRef = placement._Fld7600_RRRef
           AND placement._Fld7600_RTRef = 0x00000084
        LEFT JOIN dbo._Document133 AS supplier_order WITH (NOLOCK)
            ON supplier_order._IDRRef = placement._Fld7601_RRRef
           AND placement._Fld7601_RTRef = 0x00000085
        LEFT JOIN dbo._Reference80 AS supplier_warehouse WITH (NOLOCK)
            ON supplier_warehouse._IDRRef = supplier_order._Fld2506RRef
        OUTER APPLY (
            SELECT TOP (1)
                close_line._Fld2571_RRRef AS order_ref
            FROM dbo._Document135 AS close_header WITH (NOLOCK)
            JOIN dbo._Document135_VT2569 AS close_line WITH (NOLOCK)
                ON close_line._Document135_IDRRef = close_header._IDRRef
            WHERE close_header._Marked = 0x00
              AND close_header._Posted = 0x01
              AND close_line._Fld2571_RTRef = 0x00000084
              AND close_line._Fld2571_RRRef = customer_order._IDRRef
        ) AS close_doc
        WHERE placement._Fld7602 > 0
          {placement_source_filter}
          AND placement._Fld7598RRef <> 0x00000000000000000000000000000000
          AND placement._Fld7600_RTRef = 0x00000084
          AND placement._Fld7601_RTRef = 0x00000085
          {placement_date_from_filter}
          {placement_date_to_filter}
          {placement_warehouse_filter}

        UNION ALL

        SELECT TOP ({bounded_limit})
            CONVERT(varchar(34), product._IDRRef, 1) AS product_ref,
            NULLIF(LTRIM(RTRIM(product._Code)), N'') AS product_code,
            NULLIF(LTRIM(RTRIM(product._Description)), N'') AS product_name,
            CONVERT(varchar(34), warehouse._IDRRef, 1) AS warehouse_ref,
            NULLIF(LTRIM(RTRIM(warehouse._Code)), N'') AS warehouse_code,
            NULLIF(LTRIM(RTRIM(warehouse._Description)), N'') AS warehouse_name,
            CAST(order_line._Fld2431 AS decimal(18, 3)) AS quantity,
            CAST(0 AS decimal(18, 3)) AS stock_quantity,
            CAST(0 AS decimal(18, 3)) AS reserved_quantity,
            CAST(0 AS decimal(18, 3)) AS placement_quantity,
            CAST(order_line._Fld2431 AS decimal(18, 3)) AS order_quantity,
            CAST(0 AS decimal(18, 3)) AS issued_quantity,
            CAST(0 AS decimal(18, 3)) AS return_quantity,
            customer_order._Date_Time AS fact_date,
            N'1c:customer_order_lines' AS data_source,
            N'customer_order' AS source_document_type,
            CONVERT(varchar(34), customer_order._IDRRef, 1) AS source_document_ref,
            NULLIF(LTRIM(RTRIM(customer_order._Number)), N'') AS source_document_number,
            CONVERT(varchar(34), customer_order._IDRRef, 1) AS order_ref,
            NULLIF(LTRIM(RTRIM(customer_order._Number)), N'') AS order_number,
            NULLIF(LTRIM(RTRIM(customer_order._Fld2425)), N'') AS site_order_number,
            NULLIF(LTRIM(RTRIM(customer_order._Fld9266)), N'') AS delivery_method,
            CAST(NULL AS datetime) AS pickup_deadline,
            CAST(NULL AS nvarchar(32)) AS pickup_deadline_source,
            0 AS has_reserve_release,
            CASE WHEN close_doc.order_ref IS NULL THEN 0 ELSE 1 END AS has_closing_document,
            0 AS has_issue_document,
            0 AS has_return_document,
            0 AS needs_dismantling,
            0 AS ambiguous_warehouse,
            0 AS missing_document,
            0 AS incomplete_data,
            NULL AS manual_review_reason
        FROM dbo._Document132 AS customer_order WITH (NOLOCK)
        JOIN dbo._Document132_VT2427 AS order_line WITH (NOLOCK)
            ON order_line._Document132_IDRRef = customer_order._IDRRef
        JOIN dbo._Reference62 AS product WITH (NOLOCK)
            ON product._IDRRef = order_line._Fld2434RRef
        JOIN dbo._Reference80 AS warehouse WITH (NOLOCK)
            ON warehouse._IDRRef = (
                CASE
                    WHEN order_line._Fld2437_RRRef
                        <> 0x00000000000000000000000000000000
                    THEN order_line._Fld2437_RRRef
                    ELSE customer_order._Fld2413_RRRef
                END
            )
        OUTER APPLY (
            SELECT TOP (1)
                close_line._Fld2571_RRRef AS order_ref
            FROM dbo._Document135 AS close_header WITH (NOLOCK)
            JOIN dbo._Document135_VT2569 AS close_line WITH (NOLOCK)
                ON close_line._Document135_IDRRef = close_header._IDRRef
            WHERE close_header._Marked = 0x00
              AND close_header._Posted = 0x01
              AND close_line._Fld2571_RTRef = 0x00000084
              AND close_line._Fld2571_RRRef = customer_order._IDRRef
        ) AS close_doc
        WHERE customer_order._Marked = 0x00
          {order_source_filter}
          AND customer_order._Posted = 0x01
          AND order_line._Fld2434RRef <> 0x00000000000000000000000000000000
          AND order_line._Fld2431 > 0
          AND NOT EXISTS (
              SELECT 1
              FROM dbo._AccumRgT7662 AS order_reserve WITH (NOLOCK)
              WHERE order_reserve._Fld7659 > 0
                AND order_reserve._Fld7655RRef = order_line._Fld2434RRef
                AND order_reserve._Fld7657_RRRef = customer_order._IDRRef
                AND order_reserve._Fld7657_RTRef = 0x00000084
                AND order_reserve._Fld7654RRef = warehouse._IDRRef
          )
          AND NOT EXISTS (
              SELECT 1
              FROM dbo._AccumRgT7606 AS order_placement WITH (NOLOCK)
              JOIN dbo._Document133 AS order_supplier_order WITH (NOLOCK)
                  ON order_supplier_order._IDRRef = order_placement._Fld7601_RRRef
              WHERE order_placement._Fld7602 > 0
                AND order_placement._Fld7598RRef = order_line._Fld2434RRef
                AND order_placement._Fld7600_RRRef = customer_order._IDRRef
                AND order_placement._Fld7600_RTRef = 0x00000084
                AND order_placement._Fld7601_RTRef = 0x00000085
                AND order_supplier_order._Fld2506RRef = warehouse._IDRRef
          )
          {order_date_from_filter}
          {order_date_to_filter}
          {order_warehouse_filter}

        UNION ALL

        SELECT TOP ({bounded_limit})
            CONVERT(varchar(34), product._IDRRef, 1) AS product_ref,
            NULLIF(LTRIM(RTRIM(product._Code)), N'') AS product_code,
            NULLIF(LTRIM(RTRIM(product._Description)), N'') AS product_name,
            CONVERT(varchar(34), warehouse._IDRRef, 1) AS warehouse_ref,
            NULLIF(LTRIM(RTRIM(warehouse._Code)), N'') AS warehouse_code,
            NULLIF(LTRIM(RTRIM(warehouse._Description)), N'') AS warehouse_name,
            CAST(rtu_line._Fld4971 AS decimal(18, 3)) AS quantity,
            CAST(0 AS decimal(18, 3)) AS stock_quantity,
            CAST(0 AS decimal(18, 3)) AS reserved_quantity,
            CAST(0 AS decimal(18, 3)) AS placement_quantity,
            CAST(0 AS decimal(18, 3)) AS order_quantity,
            CAST(rtu_line._Fld4971 AS decimal(18, 3)) AS issued_quantity,
            CAST(0 AS decimal(18, 3)) AS return_quantity,
            rtu._Date_Time AS fact_date,
            N'1c:rtu_lines' AS data_source,
            N'rtu' AS source_document_type,
            CONVERT(varchar(34), rtu._IDRRef, 1) AS source_document_ref,
            NULLIF(LTRIM(RTRIM(rtu._Number)), N'') AS source_document_number,
            CONVERT(varchar(34), customer_order._IDRRef, 1) AS order_ref,
            NULLIF(LTRIM(RTRIM(customer_order._Number)), N'') AS order_number,
            NULLIF(LTRIM(RTRIM(customer_order._Fld2425)), N'') AS site_order_number,
            NULLIF(LTRIM(RTRIM(customer_order._Fld9266)), N'') AS delivery_method,
            CAST(NULL AS datetime) AS pickup_deadline,
            CAST(NULL AS nvarchar(32)) AS pickup_deadline_source,
            0 AS has_reserve_release,
            CASE WHEN close_doc.order_ref IS NULL THEN 0 ELSE 1 END AS has_closing_document,
            1 AS has_issue_document,
            0 AS has_return_document,
            0 AS needs_dismantling,
            0 AS ambiguous_warehouse,
            CASE WHEN customer_order._IDRRef IS NULL THEN 1 ELSE 0 END AS missing_document,
            0 AS incomplete_data,
            NULL AS manual_review_reason
        FROM dbo._Document203 AS rtu WITH (NOLOCK)
        JOIN dbo._Document203_VT4966 AS rtu_line WITH (NOLOCK)
            ON rtu_line._Document203_IDRRef = rtu._IDRRef
        LEFT JOIN dbo._Document132 AS customer_order WITH (NOLOCK)
            ON customer_order._IDRRef = rtu._Fld4939_RRRef
        JOIN dbo._Reference62 AS product WITH (NOLOCK)
            ON product._IDRRef = rtu_line._Fld4974RRef
        JOIN dbo._Reference80 AS warehouse WITH (NOLOCK)
            ON warehouse._IDRRef = (
                CASE
                    WHEN rtu_line._Fld4983RRef
                        <> 0x00000000000000000000000000000000
                    THEN rtu_line._Fld4983RRef
                    ELSE rtu._Fld4940RRef
                END
            )
        OUTER APPLY (
            SELECT TOP (1)
                close_line._Fld2571_RRRef AS order_ref
            FROM dbo._Document135 AS close_header WITH (NOLOCK)
            JOIN dbo._Document135_VT2569 AS close_line WITH (NOLOCK)
                ON close_line._Document135_IDRRef = close_header._IDRRef
            WHERE close_header._Marked = 0x00
              AND close_header._Posted = 0x01
              AND close_line._Fld2571_RTRef = 0x00000084
              AND close_line._Fld2571_RRRef = customer_order._IDRRef
        ) AS close_doc
        WHERE rtu._Marked = 0x00
          {rtu_source_filter}
          AND rtu._Posted = 0x01
          AND rtu_line._Fld4974RRef <> 0x00000000000000000000000000000000
          AND rtu_line._Fld4971 > 0
          {rtu_date_from_filter}
          {rtu_date_to_filter}
          {rtu_warehouse_filter}

        UNION ALL

        SELECT TOP ({bounded_limit})
            CONVERT(varchar(34), product._IDRRef, 1) AS product_ref,
            NULLIF(LTRIM(RTRIM(product._Code)), N'') AS product_code,
            NULLIF(LTRIM(RTRIM(product._Description)), N'') AS product_name,
            CONVERT(varchar(34), warehouse._IDRRef, 1) AS warehouse_ref,
            NULLIF(LTRIM(RTRIM(warehouse._Code)), N'') AS warehouse_code,
            NULLIF(LTRIM(RTRIM(warehouse._Description)), N'') AS warehouse_name,
            CAST(return_line._Fld1701 AS decimal(18, 3)) AS quantity,
            CAST(0 AS decimal(18, 3)) AS stock_quantity,
            CAST(0 AS decimal(18, 3)) AS reserved_quantity,
            CAST(0 AS decimal(18, 3)) AS placement_quantity,
            CAST(0 AS decimal(18, 3)) AS order_quantity,
            CAST(0 AS decimal(18, 3)) AS issued_quantity,
            CAST(return_line._Fld1701 AS decimal(18, 3)) AS return_quantity,
            customer_return._Date_Time AS fact_date,
            N'1c:return_lines' AS data_source,
            N'customer_return' AS source_document_type,
            CONVERT(varchar(34), customer_return._IDRRef, 1) AS source_document_ref,
            NULLIF(LTRIM(RTRIM(customer_return._Number)), N'') AS source_document_number,
            NULL AS order_ref,
            NULL AS order_number,
            NULL AS site_order_number,
            NULL AS delivery_method,
            CAST(NULL AS datetime) AS pickup_deadline,
            CAST(NULL AS nvarchar(32)) AS pickup_deadline_source,
            0 AS has_reserve_release,
            0 AS has_closing_document,
            0 AS has_issue_document,
            1 AS has_return_document,
            0 AS needs_dismantling,
            0 AS ambiguous_warehouse,
            0 AS missing_document,
            0 AS incomplete_data,
            N'customer return requires source order check' AS manual_review_reason
        FROM dbo._Document109 AS customer_return WITH (NOLOCK)
        JOIN dbo._Document109_VT1698 AS return_line WITH (NOLOCK)
            ON return_line._Document109_IDRRef = customer_return._IDRRef
        JOIN dbo._Reference62 AS product WITH (NOLOCK)
            ON product._IDRRef = return_line._Fld1700RRef
        JOIN dbo._Reference80 AS warehouse WITH (NOLOCK)
            ON warehouse._IDRRef = return_line._Fld1716RRef
        WHERE customer_return._Marked = 0x00
          {return_source_filter}
          AND customer_return._Posted = 0x01
          AND return_line._Fld1700RRef <> 0x00000000000000000000000000000000
          AND return_line._Fld1716RRef <> 0x00000000000000000000000000000000
          AND return_line._Fld1701 > 0
          {return_date_from_filter}
          {return_date_to_filter}
          {return_warehouse_filter}

        UNION ALL

        SELECT TOP ({bounded_limit})
            CONVERT(varchar(34), product._IDRRef, 1) AS product_ref,
            NULLIF(LTRIM(RTRIM(product._Code)), N'') AS product_code,
            NULLIF(LTRIM(RTRIM(product._Description)), N'') AS product_name,
            CONVERT(varchar(34), source_warehouse._IDRRef, 1) AS warehouse_ref,
            NULLIF(LTRIM(RTRIM(source_warehouse._Code)), N'') AS warehouse_code,
            NULLIF(LTRIM(RTRIM(source_warehouse._Description)), N'') AS warehouse_name,
            CAST(transfer_line._Fld3829 AS decimal(18, 3)) AS quantity,
            CAST(0 AS decimal(18, 3)) AS stock_quantity,
            CAST(0 AS decimal(18, 3)) AS reserved_quantity,
            CAST(0 AS decimal(18, 3)) AS placement_quantity,
            CAST(0 AS decimal(18, 3)) AS order_quantity,
            CAST(0 AS decimal(18, 3)) AS issued_quantity,
            CAST(0 AS decimal(18, 3)) AS return_quantity,
            transfer_doc._Date_Time AS fact_date,
            N'1c:transfer_lines' AS data_source,
            N'transfer' AS source_document_type,
            CONVERT(varchar(34), transfer_doc._IDRRef, 1) AS source_document_ref,
            NULLIF(LTRIM(RTRIM(transfer_doc._Number)), N'') AS source_document_number,
            NULL AS order_ref,
            NULL AS order_number,
            NULL AS site_order_number,
            NULL AS delivery_method,
            CAST(NULL AS datetime) AS pickup_deadline,
            CAST(NULL AS nvarchar(32)) AS pickup_deadline_source,
            0 AS has_reserve_release,
            0 AS has_closing_document,
            0 AS has_issue_document,
            0 AS has_return_document,
            0 AS needs_dismantling,
            0 AS ambiguous_warehouse,
            0 AS missing_document,
            0 AS incomplete_data,
            N'transfer document requires logistics state check' AS manual_review_reason
        FROM dbo._Document178 AS transfer_doc WITH (NOLOCK)
        JOIN dbo._Document178_VT3822 AS transfer_line WITH (NOLOCK)
            ON transfer_line._Document178_IDRRef = transfer_doc._IDRRef
        JOIN dbo._Reference62 AS product WITH (NOLOCK)
            ON product._IDRRef = transfer_line._Fld3824RRef
        JOIN dbo._Reference80 AS source_warehouse WITH (NOLOCK)
            ON source_warehouse._IDRRef = transfer_doc._Fld3819RRef
        WHERE transfer_doc._Marked = 0x00
          {transfer_source_filter}
          AND transfer_doc._Posted = 0x01
          AND transfer_line._Fld3824RRef <> 0x00000000000000000000000000000000
          AND transfer_line._Fld3829 > 0
          {transfer_date_from_filter}
          {transfer_date_to_filter}
          {transfer_warehouse_filter}
        )
        SELECT
            *
        FROM source_rows
        WHERE 1 = 1
          {date_from_filter}
          {date_to_filter}
          {warehouse_filter}
        ORDER BY fact_date DESC
        """)
    return statement, params


def _manual_review_reason(row: TransferAssistantSourceRow) -> str | None:
    if row.manual_review_reason:
        return row.manual_review_reason
    if not row.product_ref and not row.product_name:
        return "product is missing"
    if not row.warehouse_ref and not row.warehouse_name:
        return "warehouse is missing"
    if row.ambiguous_warehouse:
        return "warehouse is ambiguous"
    if row.missing_document:
        return "required 1C document is missing"
    if row.has_return_document and row.has_issue_document:
        return "return and issue facts conflict"
    if row.incomplete_data:
        return "source data is incomplete"
    if _has_order(row) and not (row.order_ref or row.order_number or row.site_order_number):
        return "customer order was not found"
    return None


def _candidate_quantity(row: TransferAssistantSourceRow, status: str) -> Decimal:
    if status == STATUS_AVAILABLE_TO_TRANSFER:
        return max(
            Decimal("0"), row.stock_quantity - row.reserved_quantity - row.placement_quantity
        )
    if status in {
        STATUS_RESERVED_FOR_ORDER,
        STATUS_PICKUP_WAITING,
        STATUS_PICKUP_EXPIRED,
        STATUS_DISMANTLING_NEEDED,
    }:
        return max(row.reserved_quantity, row.placement_quantity, row.order_quantity, row.quantity)
    return row.quantity


def _blocked_quantity(row: TransferAssistantSourceRow) -> Decimal:
    return row.reserved_quantity + row.placement_quantity + row.order_quantity


def _has_order(row: TransferAssistantSourceRow) -> bool:
    return bool(
        row.order_ref
        or row.order_number
        or row.site_order_number
        or row.order_quantity > 0
        or row.reserved_quantity > 0
        or row.placement_quantity > 0
    )


def _is_pickup(row: TransferAssistantSourceRow) -> bool:
    value = (row.delivery_method or "").casefold()
    return "самовывоз" in value or "pickup" in value or bool(row.pickup_deadline)


def _document_keys(row: TransferAssistantSourceRow) -> dict[str, str]:
    keys: dict[str, str] = {}
    if row.product_ref:
        keys["product_ref"] = row.product_ref
    if row.warehouse_ref:
        keys["warehouse_ref"] = row.warehouse_ref
    if row.order_ref:
        keys["order_ref"] = row.order_ref
    if row.source_document_ref:
        key = f"{row.source_document_type or 'document'}_ref"
        keys[key] = row.source_document_ref
    return keys


def _status_sort_key(status: str) -> int:
    order = {
        STATUS_MANUAL_REVIEW: 60,
        STATUS_PICKUP_EXPIRED: 50,
        STATUS_DISMANTLING_NEEDED: 40,
        STATUS_PICKUP_WAITING: 30,
        STATUS_RESERVED_FOR_ORDER: 20,
        STATUS_AVAILABLE_TO_TRANSFER: 10,
    }
    return order.get(status, 0)


def _row_to_dict(row: dict[str, Any] | Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    mapping = getattr(row, "_mapping", None)
    if mapping is not None:
        return dict(mapping)
    return dict(row)


def _clean_string(value: Any) -> str | None:
    if value is None:
        return None
    text_value = str(value).strip()
    return text_value or None


def _decimal(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value).replace(",", "."))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, bytes):
        return value != b"\x00"
    if isinstance(value, (int, float, Decimal)):
        return value != 0
    text_value = str(value).strip().casefold()
    return text_value in {"1", "true", "yes", "y", "on"}


def _datetime_value(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=timezone.utc)
    text_value = str(value).strip()
    if not text_value:
        return None
    try:
        parsed = datetime.fromisoformat(text_value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _date_boundary(value: date | datetime, *, end: bool) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.combine(value, time.max if end else time.min)


def _normalize_as_of(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
