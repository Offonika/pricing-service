from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import and_, delete, exists, select, text
from sqlalchemy.orm import Session

from app.models import (
    CounterpartyManagerAssignment,
    ReceivableBalanceSnapshot,
    ReceivableCase,
    ReceivableLedgerEvent,
    ReceivableReconciliationSnapshot,
    StaffMember,
)
from app.services.importers.onec_mutual_settlements import (
    CurrentBalanceCounterpartyFilterMode,
    load_onec_mutual_settlements_current_balances_file,
    load_onec_mutual_settlements_opening_file,
)

EVENT_SALE = "sale"
EVENT_PAYMENT = "payment"
EVENT_RETURN = "return"
EVENT_OPENING_BALANCE = "opening_balance"
EVENT_DEBT_ADJUSTMENT = "debt_adjustment"
EVENT_SETTLEMENT = "settlement"
EVENT_MANAGER_REASSIGNMENT = "manager_reassignment"

ACTIVITY_ACTIVE = "active"
ACTIVITY_LOW_ACTIVE = "low_active"
ACTIVITY_INACTIVE = "inactive"

CASE_NEW_DAILY = "new_daily"
CASE_BUYERS = "buyers"
CASE_OVERDUE = "overdue"
CASE_INACTIVE = "inactive"
CASE_EMPLOYEE = "employee"
CASE_FIRED_MANAGER = "fired_manager"
CASE_ADJUSTMENT_CANDIDATE = "adjustment_candidates"
NEW_DAILY_GRACE_DAYS = 3

DEFAULT_RECEIVABLE_SYNC_BATCH_SIZE = 5000
SQLSERVER_PARAM_CHUNK_SIZE = 1000
CONTRACT_KIND_REF_TO_NAME = {
    "0x9363c6f0a10557bf4822a55db4862286": "С покупателем",
    "0x95db9a602e142ed645d7ccf13094909f": "С поставщиком",
    "0xa49b7e34b5f2cbb643d8f36270f8009f": "Прочее",
}
CONTRACT_KIND_NAME_TO_REF = {name: ref for ref, name in CONTRACT_KIND_REF_TO_NAME.items()}

EMPLOYEE_COUNTERPARTY_REFS_SQL = """
WITH tree AS (
    SELECT
        c._IDRRef,
        c._ParentIDRRef,
        c._Description,
        c._Folder,
        CAST(
            CASE
                WHEN LOWER(COALESCE(c._Description, N'')) LIKE N'%сотрудн%' THEN 1
                ELSE 0
            END AS int
        ) AS is_employee_branch
    FROM _Reference54 AS c WITH (NOLOCK)
    WHERE c._ParentIDRRef = 0x00000000000000000000000000000000

    UNION ALL

    SELECT
        child._IDRRef,
        child._ParentIDRRef,
        child._Description,
        child._Folder,
        CAST(
            CASE
                WHEN parent.is_employee_branch = 1 THEN 1
                WHEN LOWER(COALESCE(child._Description, N'')) LIKE N'%сотрудн%' THEN 1
                ELSE 0
            END AS int
        ) AS is_employee_branch
    FROM _Reference54 AS child WITH (NOLOCK)
    JOIN tree AS parent
        ON child._ParentIDRRef = parent._IDRRef
)
SELECT DISTINCT
    master.dbo.fn_varbintohexstr(_IDRRef) AS counterparty_ref
FROM tree
WHERE _Folder = 0x01
  AND is_employee_branch = 1
"""

ONEC_STAFF_MEMBER_SQL = """
WITH tree AS (
    SELECT
        p._IDRRef,
        p._ParentIDRRef,
        p._Description,
        p._Folder,
        p._Fld9270RRef,
        p._Fld9507,
        CAST(
            CASE
                WHEN LOWER(COALESCE(p._Description, N'')) LIKE N'%сотрудник%' THEN 1
                WHEN LOWER(COALESCE(p._Description, N'')) LIKE N'%уволен%' THEN 1
                ELSE 0
            END AS int
        ) AS is_staff_branch,
        CAST(
            CASE
                WHEN LOWER(COALESCE(p._Description, N'')) LIKE N'%уволен%' THEN 1
                ELSE 0
            END AS int
        ) AS is_fired_branch
    FROM _Reference94 AS p WITH (NOLOCK)
    WHERE p._ParentIDRRef = 0x00000000000000000000000000000000

    UNION ALL

    SELECT
        child._IDRRef,
        child._ParentIDRRef,
        child._Description,
        child._Folder,
        child._Fld9270RRef,
        child._Fld9507,
        CAST(
            CASE
                WHEN parent.is_staff_branch = 1 THEN 1
                WHEN LOWER(COALESCE(child._Description, N'')) LIKE N'%сотрудник%' THEN 1
                WHEN LOWER(COALESCE(child._Description, N'')) LIKE N'%уволен%' THEN 1
                ELSE 0
            END AS int
        ) AS is_staff_branch,
        CAST(
            CASE
                WHEN parent.is_fired_branch = 1 THEN 1
                WHEN LOWER(COALESCE(child._Description, N'')) LIKE N'%уволен%' THEN 1
                ELSE 0
            END AS int
        ) AS is_fired_branch
    FROM _Reference94 AS child WITH (NOLOCK)
    JOIN tree AS parent
        ON child._ParentIDRRef = parent._IDRRef
)
SELECT
    master.dbo.fn_varbintohexstr(tree._IDRRef) AS external_ref,
    tree._Description AS full_name,
    master.dbo.fn_varbintohexstr(tree._ParentIDRRef) AS department_ref,
    parent._Description AS department_name,
    master.dbo.fn_varbintohexstr(tree._Fld9270RRef) AS counterparty_ref,
    counterparty._Description AS counterparty_name,
    CASE
        WHEN tree.is_fired_branch = 1 THEN N'fired'
        WHEN LOWER(COALESCE(tree._Description, N'')) LIKE N'%бывш%' THEN N'fired'
        WHEN LOWER(COALESCE(tree._Description, N'')) LIKE N'%уволен%' THEN N'fired'
        ELSE N'active'
    END AS employment_status,
    CASE
        WHEN tree.is_fired_branch = 1 AND tree._Fld9507 > CAST('1900-01-01' AS datetime)
            THEN CAST(tree._Fld9507 AS date)
        ELSE NULL
    END AS termination_date
FROM tree
LEFT JOIN _Reference94 AS parent
    ON parent._IDRRef = tree._ParentIDRRef
LEFT JOIN _Reference54 AS counterparty
    ON counterparty._IDRRef = tree._Fld9270RRef
WHERE tree._Folder = 0x01
  AND tree.is_staff_branch = 1
"""

COUNTERPARTY_GROUP_MEMBER_NAMES_SQL = """
WITH tree AS (
    SELECT
        c._IDRRef,
        c._ParentIDRRef,
        c._Description,
        c._Folder,
        CAST(
            CASE
                WHEN UPPER(LTRIM(RTRIM(COALESCE(c._Description, N'')))) = UPPER(:group_name) THEN 1
                ELSE 0
            END AS int
        ) AS is_group_branch
    FROM _Reference54 AS c WITH (NOLOCK)
    WHERE c._ParentIDRRef = 0x00000000000000000000000000000000

    UNION ALL

    SELECT
        child._IDRRef,
        child._ParentIDRRef,
        child._Description,
        child._Folder,
        CAST(
            CASE
                WHEN parent.is_group_branch = 1 THEN 1
                WHEN UPPER(LTRIM(RTRIM(COALESCE(child._Description, N'')))) = UPPER(:group_name) THEN 1
                ELSE 0
            END AS int
        ) AS is_group_branch
    FROM _Reference54 AS child WITH (NOLOCK)
    JOIN tree AS parent
        ON child._ParentIDRRef = parent._IDRRef
)
SELECT DISTINCT
    _Description AS counterparty_name
FROM tree
WHERE _Folder = 0x01
  AND is_group_branch = 1
"""

COUNTERPARTY_GROUP_MEMBERS_SQL = """
WITH tree AS (
    SELECT
        c._IDRRef,
        c._ParentIDRRef,
        c._Code,
        c._Description,
        c._Folder,
        CAST(
            CASE
                WHEN UPPER(LTRIM(RTRIM(COALESCE(c._Description, N'')))) = UPPER(:group_name) THEN 1
                ELSE 0
            END AS int
        ) AS is_group_branch
    FROM _Reference54 AS c WITH (NOLOCK)
    WHERE c._ParentIDRRef = 0x00000000000000000000000000000000

    UNION ALL

    SELECT
        child._IDRRef,
        child._ParentIDRRef,
        child._Code,
        child._Description,
        child._Folder,
        CAST(
            CASE
                WHEN parent.is_group_branch = 1 THEN 1
                WHEN UPPER(LTRIM(RTRIM(COALESCE(child._Description, N'')))) = UPPER(:group_name) THEN 1
                ELSE 0
            END AS int
        ) AS is_group_branch
    FROM _Reference54 AS child WITH (NOLOCK)
    JOIN tree AS parent
        ON child._ParentIDRRef = parent._IDRRef
)
SELECT DISTINCT
    master.dbo.fn_varbintohexstr(_IDRRef) AS counterparty_ref,
    RTRIM(_Code) AS counterparty_code,
    _Description AS counterparty_name
FROM tree
WHERE _Folder = 0x01
  AND is_group_branch = 1
"""

COUNTERPARTY_BUYER_DEPARTMENTS_SQL = """
WITH tree AS (
    SELECT
        c._IDRRef,
        c._ParentIDRRef,
        c._Description,
        c._Folder,
        CAST(
            CASE
                WHEN UPPER(LTRIM(RTRIM(COALESCE(c._Description, N'')))) = UPPER(:buyers_group_name)
                    THEN 1
                ELSE 0
            END AS int
        ) AS is_buyers_branch,
        CAST(NULL AS varbinary(16)) AS department_ref,
        CAST(NULL AS nvarchar(255)) AS department_name
    FROM _Reference54 AS c WITH (NOLOCK)
    WHERE c._ParentIDRRef = 0x00000000000000000000000000000000

    UNION ALL

    SELECT
        child._IDRRef,
        child._ParentIDRRef,
        child._Description,
        child._Folder,
        CAST(
            CASE
                WHEN parent.is_buyers_branch = 1 THEN 1
                WHEN UPPER(LTRIM(RTRIM(COALESCE(child._Description, N'')))) = UPPER(:buyers_group_name)
                    THEN 1
                ELSE 0
            END AS int
        ) AS is_buyers_branch,
        CASE
            WHEN parent.is_buyers_branch = 1
                 AND parent.department_ref IS NULL
                 AND UPPER(LTRIM(RTRIM(COALESCE(child._Description, N'')))) <> UPPER(:buyers_group_name)
                THEN child._IDRRef
            ELSE parent.department_ref
        END AS department_ref,
        CASE
            WHEN parent.is_buyers_branch = 1
                 AND parent.department_ref IS NULL
                 AND UPPER(LTRIM(RTRIM(COALESCE(child._Description, N'')))) <> UPPER(:buyers_group_name)
                THEN child._Description
            ELSE parent.department_name
        END AS department_name
    FROM _Reference54 AS child WITH (NOLOCK)
    JOIN tree AS parent
        ON child._ParentIDRRef = parent._IDRRef
)
SELECT DISTINCT
    master.dbo.fn_varbintohexstr(_IDRRef) AS counterparty_ref,
    _Description AS counterparty_name,
    master.dbo.fn_varbintohexstr(department_ref) AS department_ref,
    department_name AS department_name
FROM tree
WHERE is_buyers_branch = 1
  AND department_ref IS NOT NULL
"""

COUNTERPARTY_CONTACT_INFO_OWNER_TYPE_REF = "0x00000036"
COUNTERPARTY_PHONE_KIND_PRIORITY = (
    "Телефон контрагента",
    "Рабочий",
    "Доп. телефон для переноса",
)


def _clean_string(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.hex().upper()
    value = str(value).strip()
    return value or None


def _normalize_person_name(value: Any) -> str | None:
    cleaned = _clean_string(value)
    if cleaned is None:
        return None
    return " ".join(cleaned.casefold().split())


def _normalize_counterparty_match_key(value: Any) -> str | None:
    return _normalize_person_name(value)


def _clean_staff_directory_name(value: Any) -> str | None:
    cleaned = _clean_string(value)
    if cleaned is None:
        return None
    cleaned = re.sub(r"^[*\-\s]+", "", cleaned)
    cleaned = re.sub(r"\(\s*сотрудник\s*\)", "", cleaned, flags=re.IGNORECASE)
    cleaned = " ".join(cleaned.split())
    return cleaned or None


def _to_decimal(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    if value is None:
        return default
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _to_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.strip())
    raise TypeError(f"unsupported datetime value: {value!r}")


def _optional_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    parsed = _to_datetime(value)
    if parsed <= datetime(1753, 1, 1):
        return None
    return parsed


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, Decimal):
        return int(value)
    return int(str(value))


def _to_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, bytes):
        return value not in {b"", b"\x00", b"\x00" * len(value)}
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return bool(value)


def _chunked_strings(
    values: Sequence[str], *, size: int = SQLSERVER_PARAM_CHUNK_SIZE
) -> Iterator[list[str]]:
    chunk: list[str] = []
    for value in values:
        chunk.append(value)
        if len(chunk) >= size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def _quantize_amount(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _event_value(event: Any, key: str) -> Any:
    if isinstance(event, Mapping):
        return event.get(key)
    return getattr(event, key)


def _event_to_origin_mapping(event: Any) -> dict[str, Any]:
    return {
        "id": _event_value(event, "id"),
        "external_document_ref": _event_value(event, "external_document_ref"),
        "external_document_number": _event_value(event, "external_document_number"),
        "external_document_date": _event_value(event, "external_document_date"),
        "manager_ref": _event_value(event, "manager_ref"),
        "manager_name": _event_value(event, "manager_name"),
        "store_ref": _event_value(event, "store_ref"),
        "store_name": _event_value(event, "store_name"),
        "amount_delta": _event_value(event, "amount_delta"),
    }


def _is_debt_increase_event(event: Any) -> bool:
    amount_delta = Decimal(_event_value(event, "amount_delta"))
    return amount_delta > 0 and _event_value(event, "event_type") != EVENT_OPENING_BALANCE


def _find_unpaid_origin_event(
    events: Sequence[Any],
    *,
    current_balance: Decimal,
) -> dict[str, Any] | None:
    target_balance = _quantize_amount(current_balance)
    if target_balance <= 0:
        return None

    accumulated = Decimal("0.00")
    origin_event: Any | None = None
    for event in reversed(events):
        if not _is_debt_increase_event(event):
            continue
        accumulated = _quantize_amount(accumulated + Decimal(_event_value(event, "amount_delta")))
        origin_event = event
        if accumulated >= target_balance:
            return _event_to_origin_mapping(event)
    return _event_to_origin_mapping(origin_event) if origin_event is not None else None


def _resolve_counterparty_department(
    counterparty_departments_by_ref: (
        Mapping[str, CounterpartyDepartmentRow | Mapping[str, Any]] | None
    ),
    counterparty_ref: str | None,
) -> tuple[str | None, str | None]:
    if not counterparty_departments_by_ref or not counterparty_ref:
        return None, None
    item = counterparty_departments_by_ref.get(counterparty_ref)
    if item is None:
        return None, None
    if isinstance(item, CounterpartyDepartmentRow):
        return item.department_ref, item.department_name
    return _clean_string(item.get("department_ref")), _clean_string(item.get("department_name"))


def _build_synthetic_receivable_ref(prefix: str, code: str) -> str:
    digest = hashlib.sha256(f"{prefix}:{code}".encode()).hexdigest()
    prefix_digest = hashlib.sha1(prefix.encode()).hexdigest()[:12]
    return f"synthetic:{prefix_digest}:{digest[:41]}"


def normalize_receivable_event_type(value: Any) -> str:
    normalized = (_clean_string(value) or "").lower()
    aliases = {
        "sale": EVENT_SALE,
        "реализация": EVENT_SALE,
        "payment": EVENT_PAYMENT,
        "оплата": EVENT_PAYMENT,
        "return": EVENT_RETURN,
        "возврат": EVENT_RETURN,
        "opening_balance": EVENT_OPENING_BALANCE,
        "opening": EVENT_OPENING_BALANCE,
        "остаток_на_дату": EVENT_OPENING_BALANCE,
        "debt_adjustment": EVENT_DEBT_ADJUSTMENT,
        "adjustment": EVENT_DEBT_ADJUSTMENT,
        "корректировка": EVENT_DEBT_ADJUSTMENT,
        "settlement": EVENT_SETTLEMENT,
        "взаимозачет": EVENT_SETTLEMENT,
        "взаимозачёт": EVENT_SETTLEMENT,
        "manager_reassignment": EVENT_MANAGER_REASSIGNMENT,
        "manager_assignment": EVENT_MANAGER_REASSIGNMENT,
        "смена_ответственного": EVENT_MANAGER_REASSIGNMENT,
    }
    if normalized in aliases:
        return aliases[normalized]
    raise ValueError(f"unsupported receivable event_type: {value!r}")


def build_receivable_event_business_key(
    *,
    source: str,
    event_type: str,
    external_document_ref: str,
    counterparty_ref: str,
    line_no: int | None,
) -> str:
    raw = "|".join(
        [
            source,
            event_type,
            external_document_ref,
            counterparty_ref,
            str(line_no or 0),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def compute_aged_bucket(origin_date: date | None, snapshot_date: date) -> str:
    if origin_date is None:
        return "unknown"
    age_days = max((snapshot_date - origin_date).days, 0)
    if age_days <= 7:
        return "0-7"
    if age_days <= 30:
        return "8-30"
    if age_days <= 60:
        return "31-60"
    if age_days <= 90:
        return "61-90"
    return "90+"


def compute_activity_segment(last_sale_at: datetime | None, snapshot_date: date) -> str:
    if last_sale_at is None:
        return ACTIVITY_INACTIVE
    days_since_sale = max((snapshot_date - last_sale_at.date()).days, 0)
    if days_since_sale <= 30:
        return ACTIVITY_ACTIVE
    if days_since_sale <= 90:
        return ACTIVITY_LOW_ACTIVE
    return ACTIVITY_INACTIVE


@dataclass(slots=True)
class ReceivableLedgerRow:
    source: str
    event_type: str
    external_document_ref: str
    external_document_number: str | None
    external_document_date: datetime
    counterparty_ref: str
    counterparty_name: str | None
    contract_ref: str | None
    contract_name: str | None
    contract_kind_ref: str | None
    contract_kind_name: str | None
    manager_ref: str | None
    manager_name: str | None
    store_ref: str | None
    store_name: str | None
    source_layer: str
    planned_payment_date: datetime | None
    credit_depth_days: int | None
    shipment_ban: bool | None
    line_no: int | None
    amount_delta: Decimal
    skip_ingest: bool = False

    @property
    def business_key(self) -> str:
        return build_receivable_event_business_key(
            source=self.source,
            event_type=self.event_type,
            external_document_ref=self.external_document_ref,
            counterparty_ref=self.counterparty_ref,
            line_no=self.line_no,
        )

    @classmethod
    def from_mapping(
        cls, row: dict[str, Any], *, default_source: str = "onec"
    ) -> ReceivableLedgerRow:
        line_no = row.get("line_no")
        return cls(
            source=_clean_string(row.get("source")) or default_source,
            event_type=normalize_receivable_event_type(row.get("event_type")),
            external_document_ref=_clean_string(row.get("external_document_ref")) or "",
            external_document_number=_clean_string(row.get("external_document_number")),
            external_document_date=_to_datetime(row["external_document_date"]),
            counterparty_ref=_clean_string(row.get("counterparty_ref")) or "",
            counterparty_name=_clean_string(row.get("counterparty_name")),
            contract_ref=_clean_string(row.get("contract_ref")),
            contract_name=_clean_string(row.get("contract_name")),
            contract_kind_ref=_clean_string(row.get("contract_kind_ref")),
            contract_kind_name=_clean_string(row.get("contract_kind_name")),
            manager_ref=_clean_string(row.get("manager_ref")),
            manager_name=_clean_string(row.get("manager_name")),
            store_ref=_clean_string(row.get("store_ref")),
            store_name=_clean_string(row.get("store_name")),
            source_layer=_clean_string(row.get("source_layer")) or "regular_receivables",
            planned_payment_date=_optional_datetime(row.get("planned_payment_date")),
            credit_depth_days=_to_int(row.get("credit_depth_days")),
            shipment_ban=_to_bool(row.get("shipment_ban")),
            line_no=int(line_no) if line_no is not None else None,
            amount_delta=_quantize_amount(_to_decimal(row.get("amount_delta"))),
            skip_ingest=bool(_to_bool(row.get("skip_ingest"))),
        )


@dataclass(slots=True)
class AuthoritativeReceivableBalanceRow:
    counterparty_ref: str
    counterparty_name: str | None
    current_balance: Decimal
    current_manager_ref: str | None = None
    current_manager_name: str | None = None
    source: str = "onec_authoritative_balance"


@dataclass(slots=True, frozen=True)
class CounterpartyDepartmentRow:
    counterparty_ref: str
    counterparty_name: str | None
    department_ref: str
    department_name: str


class OneCReceivableLedgerExtractor:
    def __init__(self, onec_engine, operations_sql: str | None = None):
        self.onec_engine = onec_engine
        self.operations_sql = operations_sql

    def iter_receivable_events(
        self,
        *,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
        opening_balance_date: date | None = None,
        batch_size: int = DEFAULT_RECEIVABLE_SYNC_BATCH_SIZE,
    ) -> Iterator[ReceivableLedgerRow]:
        for batch in self.iter_receivable_event_batches(
            window_start=window_start,
            window_end=window_end,
            opening_balance_date=opening_balance_date,
            batch_size=batch_size,
        ):
            yield from batch

    def iter_receivable_event_batches(
        self,
        *,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
        opening_balance_date: date | None = None,
        batch_size: int = DEFAULT_RECEIVABLE_SYNC_BATCH_SIZE,
    ) -> Iterator[list[ReceivableLedgerRow]]:
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        if not self.operations_sql:
            raise RuntimeError(
                "Receivable ledger SQL is not configured. "
                "Pass a normalized SQL query via operations_sql or CLI --sql-file."
            )

        params: dict[str, Any] = {
            "window_start": window_start,
            "window_end": window_end,
            "opening_balance_date": opening_balance_date,
        }

        with self.onec_engine.connect() as conn:
            if conn.dialect.name == "mssql":
                # sqlalchemy-pytds may return empty iteration with stream_results=True
                # on complex CTE/UNION queries; use default cursor mode for MSSQL.
                stream_conn = conn
            else:
                stream_conn = conn.execution_options(
                    stream_results=True,
                    max_row_buffer=batch_size,
                )
            batch: list[ReceivableLedgerRow] = []
            for row in stream_conn.execute(text(self.operations_sql), params).mappings():
                item = ReceivableLedgerRow.from_mapping(dict(row))
                if item.skip_ingest:
                    continue
                batch.append(item)
                if len(batch) >= batch_size:
                    yield batch
                    batch = []
            if batch:
                yield batch

    def fetch_receivable_events(
        self,
        *,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
        opening_balance_date: date | None = None,
    ) -> list[ReceivableLedgerRow]:
        return list(
            self.iter_receivable_events(
                window_start=window_start,
                window_end=window_end,
                opening_balance_date=opening_balance_date,
            )
        )


def fetch_employee_counterparty_refs_from_onec(onec_engine) -> tuple[str, ...]:
    with onec_engine.connect() as conn:
        rows = conn.execute(text(EMPLOYEE_COUNTERPARTY_REFS_SQL)).mappings().all()
    refs = sorted(
        {
            _clean_string(row.get("counterparty_ref")) or ""
            for row in rows
            if _clean_string(row.get("counterparty_ref"))
        }
    )
    return tuple(refs)


def fetch_staff_members_from_onec(onec_engine) -> tuple[dict[str, Any], ...]:
    with onec_engine.connect() as conn:
        rows = conn.execute(text(ONEC_STAFF_MEMBER_SQL)).mappings().all()

    items: list[dict[str, Any]] = []
    for row in rows:
        full_name = _clean_staff_directory_name(row.get("full_name"))
        external_ref = _clean_string(row.get("external_ref"))
        if not external_ref or not full_name:
            continue
        termination_date = row.get("termination_date")
        if isinstance(termination_date, datetime):
            termination_date = termination_date.date()
        items.append(
            {
                "source": "onec_physical_person",
                "external_ref": external_ref,
                "full_name": full_name,
                "role_code": None,
                "role_name": None,
                "department_ref": _clean_string(row.get("department_ref")),
                "department_name": _clean_string(row.get("department_name")),
                "store_ref": _clean_string(row.get("counterparty_ref")),
                "store_name": _clean_string(row.get("counterparty_name")),
                "employment_status": _clean_string(row.get("employment_status")) or "active",
                "hire_date": None,
                "termination_date": termination_date,
                "manager_ref": None,
                "manager_name": None,
            }
        )
    items.sort(
        key=lambda item: (
            item["employment_status"],
            item["department_name"] or "",
            item["full_name"],
        )
    )
    return tuple(items)


def fetch_counterparty_match_keys_from_onec_group(
    onec_engine,
    *,
    group_name: str,
) -> set[str]:
    with onec_engine.connect() as conn:
        rows = conn.execute(
            text(COUNTERPARTY_GROUP_MEMBER_NAMES_SQL),
            {"group_name": group_name},
        ).mappings()

        keys = {
            key
            for key in (
                _normalize_counterparty_match_key(row.get("counterparty_name")) for row in rows
            )
            if key is not None
        }
    return keys


def fetch_counterparty_refs_from_onec_group(
    onec_engine,
    *,
    group_name: str,
) -> tuple[str, ...]:
    with onec_engine.connect() as conn:
        rows = conn.execute(
            text(COUNTERPARTY_GROUP_MEMBERS_SQL),
            {"group_name": group_name},
        ).mappings()

        refs = sorted(
            {
                counterparty_ref
                for counterparty_ref in (_clean_string(row.get("counterparty_ref")) for row in rows)
                if counterparty_ref is not None
            }
        )
    return tuple(refs)


def fetch_counterparty_ref_mapping_from_onec_group(
    onec_engine,
    *,
    group_name: str,
) -> dict[str, dict[str, str]]:
    with onec_engine.connect() as conn:
        rows = conn.execute(
            text(COUNTERPARTY_GROUP_MEMBERS_SQL),
            {"group_name": group_name},
        ).mappings()

        items: dict[str, dict[str, str]] = {}
        for row in rows:
            key = _normalize_counterparty_match_key(row.get("counterparty_name"))
            counterparty_ref = _clean_string(row.get("counterparty_ref"))
            counterparty_code = _clean_string(row.get("counterparty_code"))
            counterparty_name = _clean_string(row.get("counterparty_name"))
            if key is None or counterparty_ref is None or counterparty_name is None:
                continue
            items.setdefault(
                key,
                {
                    "counterparty_ref": counterparty_ref,
                    "counterparty_code": counterparty_code or "",
                    "counterparty_name": counterparty_name,
                },
            )
    return items


def fetch_counterparty_code_mapping_from_onec_group(
    onec_engine,
    *,
    group_name: str,
) -> dict[str, str]:
    with onec_engine.connect() as conn:
        rows = conn.execute(
            text(COUNTERPARTY_GROUP_MEMBERS_SQL),
            {"group_name": group_name},
        ).mappings()

        items: dict[str, str] = {}
        for row in rows:
            counterparty_ref = _clean_string(row.get("counterparty_ref"))
            counterparty_code = _clean_string(row.get("counterparty_code"))
            if counterparty_ref is None or counterparty_code is None:
                continue
            items.setdefault(counterparty_ref.upper(), counterparty_code)
    return items


def fetch_counterparty_departments_from_onec_buyers_group(
    onec_engine,
    *,
    buyers_group_name: str = "ПОКУПАТЕЛИ",
) -> dict[str, CounterpartyDepartmentRow]:
    with onec_engine.connect() as conn:
        rows = conn.execute(
            text(COUNTERPARTY_BUYER_DEPARTMENTS_SQL),
            {"buyers_group_name": buyers_group_name},
        ).mappings()

        items: dict[str, CounterpartyDepartmentRow] = {}
        for row in rows:
            counterparty_ref = _clean_string(row.get("counterparty_ref"))
            department_ref = _clean_string(row.get("department_ref"))
            department_name = _clean_string(row.get("department_name"))
            if not counterparty_ref or not department_ref or not department_name:
                continue
            items.setdefault(
                counterparty_ref,
                CounterpartyDepartmentRow(
                    counterparty_ref=counterparty_ref,
                    counterparty_name=_clean_string(row.get("counterparty_name")),
                    department_ref=department_ref,
                    department_name=department_name,
                ),
            )
    return items


def _hex_ref_expr(column_name: str, *, dialect_name: str) -> str:
    if dialect_name == "mssql":
        return f"master.dbo.fn_varbintohexstr({column_name})"
    return column_name


def _with_nolock(*, dialect_name: str) -> str:
    return "WITH (NOLOCK)" if dialect_name == "mssql" else ""


def _sql_string_literal(value: str, *, dialect_name: str) -> str:
    escaped = value.replace("'", "''")
    prefix = "N" if dialect_name == "mssql" else ""
    return f"{prefix}'{escaped}'"


def _build_in_clause(values: Sequence[str], *, prefix: str) -> tuple[str, dict[str, str]]:
    params = {f"{prefix}_{index}": value for index, value in enumerate(values)}
    placeholders = ", ".join(f":{name}" for name in params)
    return placeholders, params


def _build_ref_filter_clause(
    *,
    dialect_name: str,
    refs: Sequence[str],
    column_name: str,
    prefix: str,
) -> tuple[str, dict[str, str]]:
    if dialect_name == "mssql":
        hex_refs = [
            value.lower()
            for value in refs
            if re.fullmatch(r"0[xX][0-9A-Fa-f]{32}", str(value or ""))
        ]
        if not hex_refs:
            return "1 = 0", {}
        return f"{column_name} IN ({', '.join(hex_refs)})", {}

    params = {f"{prefix}_{index}": value for index, value in enumerate(refs)}
    placeholders = ", ".join(f":{name}" for name in params)
    return f"{column_name} IN ({placeholders})", params


def normalize_counterparty_phone(value: Any) -> str | None:
    text_value = _clean_string(value)
    if not text_value:
        return None
    digits = re.sub(r"\D+", "", text_value)
    if not digits:
        return None
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    if len(digits) < 10:
        return None
    return f"+{digits}"


def fetch_counterparty_phones_from_onec(
    onec_engine,
    *,
    counterparty_refs: Sequence[str],
) -> dict[str, str]:
    refs = sorted({value for value in counterparty_refs if value})
    if not refs:
        return {}

    dialect_name = onec_engine.dialect.name
    counterparty_ref_expr = _hex_ref_expr("ci._Fld6403_RRRef", dialect_name=dialect_name)
    nolock = _with_nolock(dialect_name=dialect_name)
    where_clause, params = _build_ref_filter_clause(
        dialect_name=dialect_name,
        refs=refs,
        column_name="ci._Fld6403_RRRef",
        prefix="counterparty_ref",
    )
    if dialect_name == "mssql":
        owner_type_filter = f"ci._Fld6403_RTRef = {COUNTERPARTY_CONTACT_INFO_OWNER_TYPE_REF}"
    else:
        owner_type_filter = "ci._Fld6403_RTRef = :counterparty_owner_type_ref"
        params["counterparty_owner_type_ref"] = COUNTERPARTY_CONTACT_INFO_OWNER_TYPE_REF

    kind_literals = [
        _sql_string_literal(kind, dialect_name=dialect_name)
        for kind in COUNTERPARTY_PHONE_KIND_PRIORITY
    ]
    kind_priority_case = "\n".join(
        f"                    WHEN {literal} THEN {index}"
        for index, literal in enumerate(kind_literals, start=1)
    )
    stmt = text(f"""
        WITH preferred_phone AS (
            SELECT
                {counterparty_ref_expr} AS counterparty_ref,
                CAST(ci._Fld6406 AS nvarchar(255)) AS phone_value,
                ROW_NUMBER() OVER (
                    PARTITION BY ci._Fld6403_RRRef
                    ORDER BY
                        CASE COALESCE(kind._Description, '')
{kind_priority_case}
                            ELSE 99
                        END,
                        CAST(ci._Fld6406 AS nvarchar(255)) DESC
                ) AS rn
            FROM _InfoRg6402 AS ci {nolock}
            LEFT JOIN _Reference25 AS kind {nolock}
                ON kind._IDRRef = ci._Fld6405_RRRef
            WHERE {owner_type_filter}
              AND ci._Fld6406 IS NOT NULL
              AND LTRIM(RTRIM(CAST(ci._Fld6406 AS nvarchar(255)))) <> ''
              AND COALESCE(kind._Description, '') IN ({", ".join(kind_literals)})
              AND {where_clause}
        )
        SELECT counterparty_ref, phone_value
        FROM preferred_phone
        WHERE rn = 1
    """)

    phones: dict[str, str] = {}
    with onec_engine.connect() as conn:
        rows = conn.execute(stmt, params).mappings()
        for row in rows:
            counterparty_ref = _clean_string(row.get("counterparty_ref"))
            phone = normalize_counterparty_phone(row.get("phone_value"))
            if counterparty_ref is None or phone is None:
                continue
            phones.setdefault(counterparty_ref, phone)
    return phones


def fetch_counterparty_code_mapping_from_onec(
    onec_engine,
    *,
    counterparty_codes: Sequence[str],
) -> dict[str, dict[str, Any]]:
    if not counterparty_codes:
        return {}

    bind = onec_engine
    dialect_name = bind.dialect.name
    ref_expr = _hex_ref_expr("c._IDRRef", dialect_name=dialect_name)
    nolock = _with_nolock(dialect_name=dialect_name)

    mapping: dict[str, dict[str, Any]] = {}
    with onec_engine.connect() as conn:
        stream_conn = (
            conn
            if conn.dialect.name == "mssql"
            else conn.execution_options(
                stream_results=True,
                max_row_buffer=SQLSERVER_PARAM_CHUNK_SIZE,
            )
        )
        for chunk in _chunked_strings(sorted(set(counterparty_codes))):
            placeholders, params = _build_in_clause(chunk, prefix="counterparty_code")
            stmt = text(f"""
                SELECT
                    {ref_expr} AS counterparty_ref,
                    RTRIM(c._Code) AS counterparty_code,
                    c._Description AS counterparty_name,
                    NULLIF(c._Fld9516, CAST('1753-01-01' AS datetime)) AS planned_payment_date,
                    CAST(c._Fld9865 AS int) AS credit_depth_days,
                    CASE
                        WHEN c._Fld9866 = 0x01 THEN 1
                        ELSE 0
                    END AS shipment_ban
                FROM _Reference54 AS c {nolock}
                WHERE RTRIM(c._Code) IN ({placeholders})
                """)
            for row in stream_conn.execute(stmt, params).mappings():
                code = _clean_string(row.get("counterparty_code"))
                if not code:
                    continue
                mapping[code] = {
                    "counterparty_ref": _clean_string(row.get("counterparty_ref")),
                    "counterparty_name": _clean_string(row.get("counterparty_name")),
                    "planned_payment_date": _optional_datetime(row.get("planned_payment_date")),
                    "credit_depth_days": _to_int(row.get("credit_depth_days")),
                    "shipment_ban": _to_bool(row.get("shipment_ban")),
                }
    return mapping


def fetch_contract_price_type_mapping_from_onec(
    onec_engine,
    *,
    contract_refs: Sequence[str],
) -> dict[str, str]:
    refs = sorted({value for value in contract_refs if value})
    if not refs:
        return {}

    dialect_name = onec_engine.dialect.name
    contract_ref_expr = _hex_ref_expr("contract._IDRRef", dialect_name=dialect_name)
    nolock = _with_nolock(dialect_name=dialect_name)
    where_clause, params = _build_ref_filter_clause(
        dialect_name=dialect_name,
        refs=refs,
        column_name="contract._IDRRef",
        prefix="contract_ref",
    )
    stmt = text(f"""
        SELECT
            {contract_ref_expr} AS contract_ref,
            price_type._Description AS price_type_name
        FROM _Reference37 AS contract {nolock}
        LEFT JOIN _Reference87 AS price_type {nolock}
            ON price_type._IDRRef = contract._Fld513_RRRef
        WHERE {where_clause}
    """)

    with onec_engine.connect() as conn:
        rows = conn.execute(stmt, params).mappings()

        items: dict[str, str] = {}
        for row in rows:
            contract_ref = _clean_string(row.get("contract_ref"))
            price_type_name = _clean_string(row.get("price_type_name"))
            if contract_ref is None or price_type_name is None:
                continue
            items.setdefault(contract_ref.upper(), price_type_name)
    return items


def fetch_counterparty_purchase_amounts_from_onec_sales_returns(
    onec_engine,
    *,
    period_start: datetime,
    period_end: datetime,
    counterparty_refs: Sequence[str] = (),
) -> dict[str, Decimal]:
    dialect_name = onec_engine.dialect.name
    counterparty_ref_expr = _hex_ref_expr("counterparty._IDRRef", dialect_name=dialect_name)
    nolock = _with_nolock(dialect_name=dialect_name)
    ref_filter = ""
    params: dict[str, Any] = {
        "period_start": period_start,
        "period_end": period_end,
    }
    if counterparty_refs:
        where_clause, ref_params = _build_ref_filter_clause(
            dialect_name=dialect_name,
            refs=counterparty_refs,
            column_name="counterparty._IDRRef",
            prefix="counterparty_ref",
        )
        ref_filter = f"AND {where_clause}"
        params.update(ref_params)

    stmt = text(f"""
        WITH target_organization AS (
            SELECT _IDRRef
            FROM _Reference66 {nolock}
            WHERE _Description = N'MASTER MOBILE'
        )
        SELECT
            {counterparty_ref_expr} AS counterparty_ref,
            SUM(
                CASE
                    WHEN r._RecorderTRef = 0x0000006D AND r._Fld7562 > 0
                        THEN -ABS(CAST(r._Fld7562 AS decimal(18, 2)))
                    ELSE CAST(r._Fld7562 AS decimal(18, 2))
                END
            ) AS purchase_amount
        FROM _AccumRg7550 AS r {nolock}
        JOIN _Reference54 AS counterparty {nolock}
            ON counterparty._IDRRef = r._Fld7559RRef
        JOIN _Reference37 AS contract {nolock}
            ON contract._IDRRef = r._Fld7554RRef
        WHERE r._RecorderTRef IN (0x000000CB, 0x0000006D)
          AND r._Active = 0x01
          AND r._Fld7559RRef <> 0x00000000000000000000000000000000
          AND r._Fld7558RRef IN (SELECT _IDRRef FROM target_organization)
          AND contract._Fld515RRef = 0x9363c6f0a10557bf4822a55db4862286
          AND r._Period >= :period_start
          AND r._Period < :period_end
          {ref_filter}
        GROUP BY counterparty._IDRRef
    """)

    with onec_engine.connect() as conn:
        rows = conn.execute(stmt, params).mappings()

        items: dict[str, Decimal] = {}
        for row in rows:
            counterparty_ref = _clean_string(row.get("counterparty_ref"))
            if counterparty_ref is None:
                continue
            items[counterparty_ref.upper()] = _quantize_amount(
                _to_decimal(row.get("purchase_amount"))
            )
    return items


def fetch_contract_code_mapping_from_onec(
    onec_engine,
    *,
    contract_codes: Sequence[str],
) -> dict[str, dict[str, Any]]:
    if not contract_codes:
        return {}

    bind = onec_engine
    dialect_name = bind.dialect.name
    ref_expr = _hex_ref_expr("c._IDRRef", dialect_name=dialect_name)
    kind_expr = _hex_ref_expr("c._Fld515RRef", dialect_name=dialect_name)
    owner_expr = _hex_ref_expr("c._OwnerIDRRef", dialect_name=dialect_name)
    nolock = _with_nolock(dialect_name=dialect_name)

    mapping: dict[str, dict[str, Any]] = {}
    with onec_engine.connect() as conn:
        stream_conn = (
            conn
            if conn.dialect.name == "mssql"
            else conn.execution_options(
                stream_results=True,
                max_row_buffer=SQLSERVER_PARAM_CHUNK_SIZE,
            )
        )
        for chunk in _chunked_strings(sorted(set(contract_codes))):
            placeholders, params = _build_in_clause(chunk, prefix="contract_code")
            stmt = text(f"""
                SELECT
                    {ref_expr} AS contract_ref,
                    RTRIM(c._Code) AS contract_code,
                    c._Description AS contract_name,
                    {kind_expr} AS contract_kind_ref,
                    {owner_expr} AS owner_counterparty_ref,
                    RTRIM(owner._Code) AS owner_counterparty_code,
                    owner._Description AS owner_counterparty_name,
                    NULLIF(owner._Fld9516, CAST('1753-01-01' AS datetime)) AS owner_planned_payment_date,
                    CAST(owner._Fld9865 AS int) AS owner_credit_depth_days,
                    CASE
                        WHEN owner._Fld9866 = 0x01 THEN 1
                        ELSE 0
                    END AS owner_shipment_ban
                FROM _Reference37 AS c {nolock}
                LEFT JOIN _Reference54 AS owner {nolock}
                    ON owner._IDRRef = c._OwnerIDRRef
                WHERE RTRIM(c._Code) IN ({placeholders})
                """)
            for row in stream_conn.execute(stmt, params).mappings():
                code = _clean_string(row.get("contract_code"))
                if not code:
                    continue
                contract_kind_ref = _clean_string(row.get("contract_kind_ref"))
                mapping[code] = {
                    "contract_ref": _clean_string(row.get("contract_ref")),
                    "contract_name": _clean_string(row.get("contract_name")),
                    "contract_kind_ref": contract_kind_ref,
                    "contract_kind_name": CONTRACT_KIND_REF_TO_NAME.get(
                        contract_kind_ref or "", "Неизвестно"
                    ),
                    "owner_counterparty_ref": _clean_string(row.get("owner_counterparty_ref")),
                    "owner_counterparty_code": _clean_string(row.get("owner_counterparty_code")),
                    "owner_counterparty_name": _clean_string(row.get("owner_counterparty_name")),
                    "owner_planned_payment_date": _optional_datetime(
                        row.get("owner_planned_payment_date")
                    ),
                    "owner_credit_depth_days": _to_int(row.get("owner_credit_depth_days")),
                    "owner_shipment_ban": _to_bool(row.get("owner_shipment_ban")),
                }
    return mapping


def build_receivable_opening_import_events(
    onec_engine,
    *,
    report_path: Path,
    default_source: str = "onec_opening_import",
) -> list[ReceivableLedgerRow]:
    rows = load_onec_mutual_settlements_opening_file(report_path)
    if not rows:
        return []

    counterparty_mapping = fetch_counterparty_code_mapping_from_onec(
        onec_engine,
        counterparty_codes=[row.counterparty_code for row in rows],
    )
    contract_mapping = fetch_contract_code_mapping_from_onec(
        onec_engine,
        contract_codes=[row.contract_code for row in rows],
    )

    events: list[ReceivableLedgerRow] = []
    for row in rows:
        contract_item = contract_mapping.get(row.contract_code)
        counterparty_item = counterparty_mapping.get(row.counterparty_code)
        if contract_item is None:
            contract_kind_ref = CONTRACT_KIND_NAME_TO_REF.get(row.contract_kind_name)
            contract_item = {
                "contract_ref": _build_synthetic_receivable_ref("contract", row.contract_code),
                "contract_name": row.contract_name,
                "contract_kind_ref": contract_kind_ref,
                "contract_kind_name": row.contract_kind_name,
                "owner_counterparty_ref": (
                    counterparty_item["counterparty_ref"] if counterparty_item is not None else None
                ),
                "owner_counterparty_code": row.counterparty_code,
                "owner_counterparty_name": (
                    counterparty_item["counterparty_name"]
                    if counterparty_item is not None
                    else None
                ),
                "owner_planned_payment_date": (
                    counterparty_item["planned_payment_date"]
                    if counterparty_item is not None
                    else None
                ),
                "owner_credit_depth_days": (
                    counterparty_item["credit_depth_days"]
                    if counterparty_item is not None
                    else None
                ),
                "owner_shipment_ban": (
                    counterparty_item["shipment_ban"] if counterparty_item is not None else None
                ),
            }
        if counterparty_item is None:
            owner_ref = contract_item.get("owner_counterparty_ref")
            if not owner_ref:
                counterparty_item = {
                    "counterparty_ref": _build_synthetic_receivable_ref(
                        "counterparty", row.counterparty_code
                    ),
                    "counterparty_name": row.counterparty_code,
                    "planned_payment_date": None,
                    "credit_depth_days": None,
                    "shipment_ban": None,
                }
            else:
                counterparty_item = {
                    "counterparty_ref": owner_ref,
                    "counterparty_name": contract_item.get("owner_counterparty_name"),
                    "planned_payment_date": contract_item.get("owner_planned_payment_date"),
                    "credit_depth_days": contract_item.get("owner_credit_depth_days"),
                    "shipment_ban": contract_item.get("owner_shipment_ban"),
                }
        event_date = datetime.combine(row.snapshot_date, time.min)
        events.append(
            ReceivableLedgerRow(
                source=default_source,
                event_type=EVENT_OPENING_BALANCE,
                external_document_ref=(
                    f"opening-import:{row.snapshot_date.isoformat()}:{row.source_row}"
                ),
                external_document_number="Импорт начального остатка 1С",
                external_document_date=event_date,
                counterparty_ref=counterparty_item["counterparty_ref"] or row.counterparty_code,
                counterparty_name=counterparty_item["counterparty_name"],
                contract_ref=contract_item["contract_ref"],
                contract_name=contract_item["contract_name"] or row.contract_name,
                contract_kind_ref=contract_item["contract_kind_ref"],
                contract_kind_name=contract_item["contract_kind_name"] or row.contract_kind_name,
                manager_ref=None,
                manager_name=None,
                store_ref=None,
                store_name=None,
                source_layer="opening_import_1c",
                planned_payment_date=counterparty_item["planned_payment_date"],
                credit_depth_days=counterparty_item["credit_depth_days"],
                shipment_ban=counterparty_item["shipment_ban"],
                line_no=row.source_row,
                amount_delta=_quantize_amount(row.opening_balance_rub),
            )
        )
    return events


def load_receivable_current_balance_overrides(
    report_path: Path,
    *,
    counterparty_filter_mode: CurrentBalanceCounterpartyFilterMode = "buyers",
) -> tuple[date, dict[str, Decimal]]:
    snapshot_date, overrides, _ = load_receivable_current_balance_override_payload(
        report_path,
        counterparty_filter_mode=counterparty_filter_mode,
    )
    return snapshot_date, overrides


def load_receivable_current_balance_override_payload(
    report_path: Path,
    *,
    counterparty_filter_mode: CurrentBalanceCounterpartyFilterMode = "buyers",
) -> tuple[date, dict[str, Decimal], dict[str, str]]:
    rows = load_onec_mutual_settlements_current_balances_file(
        report_path,
        counterparty_filter_mode=counterparty_filter_mode,
    )
    snapshot_dates = {row.snapshot_date for row in rows}
    if not snapshot_dates:
        raise ValueError(f"В файле current_import нет строк: {report_path}")
    if len(snapshot_dates) != 1:
        raise ValueError(f"В файле current_import несколько дат snapshot: {sorted(snapshot_dates)}")
    overrides: dict[str, Decimal] = {}
    override_names: dict[str, str] = {}
    for row in rows:
        key = _normalize_counterparty_match_key(row.counterparty_name)
        if key is None:
            continue
        overrides[key] = _quantize_amount(
            overrides.get(key, Decimal("0.00")) + row.current_balance_rub
        )
        if key not in override_names:
            override_names[key] = _clean_string(row.counterparty_name) or row.counterparty_name
    return next(iter(snapshot_dates)), overrides, override_names


def load_receivable_current_balance_rows(
    report_path: Path,
    *,
    counterparty_mapping: dict[str, dict[str, str]] | None = None,
    counterparty_filter_mode: CurrentBalanceCounterpartyFilterMode = "buyers",
    synthetic_ref_prefix: str = "current-balance",
    source: str = "onec_current_import",
) -> tuple[date, list[AuthoritativeReceivableBalanceRow]]:
    rows = load_onec_mutual_settlements_current_balances_file(
        report_path,
        counterparty_filter_mode=counterparty_filter_mode,
    )
    snapshot_dates = {row.snapshot_date for row in rows}
    if not snapshot_dates:
        raise ValueError(f"В файле current_import нет строк: {report_path}")
    if len(snapshot_dates) != 1:
        raise ValueError(f"В файле current_import несколько дат snapshot: {sorted(snapshot_dates)}")

    aggregated: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = _normalize_counterparty_match_key(row.counterparty_name)
        if key is None:
            continue

        mapping_item = counterparty_mapping.get(key) if counterparty_mapping is not None else None

        counterparty_ref = (
            mapping_item["counterparty_ref"]
            if mapping_item is not None
            else _build_synthetic_receivable_ref(synthetic_ref_prefix, key)
        )
        counterparty_name = (
            mapping_item["counterparty_name"]
            if mapping_item is not None
            else (_clean_string(row.counterparty_name) or row.counterparty_name)
        )
        state = aggregated.setdefault(
            counterparty_ref,
            {
                "counterparty_ref": counterparty_ref,
                "counterparty_name": counterparty_name,
                "current_balance": Decimal("0.00"),
            },
        )
        state["current_balance"] = _quantize_amount(
            Decimal(state["current_balance"]) + row.current_balance_rub
        )
        if state["counterparty_name"] is None and counterparty_name is not None:
            state["counterparty_name"] = counterparty_name

    snapshot_date = next(iter(snapshot_dates))
    items = [
        AuthoritativeReceivableBalanceRow(
            counterparty_ref=item["counterparty_ref"],
            counterparty_name=item["counterparty_name"],
            current_balance=_quantize_amount(Decimal(item["current_balance"])),
            source=source,
        )
        for item in aggregated.values()
    ]
    items.sort(key=lambda item: (item.counterparty_name or "", item.counterparty_ref))
    return snapshot_date, items


def _is_month_end(snapshot_date: date) -> bool:
    return (snapshot_date + timedelta(days=1)).month != snapshot_date.month


def _first_day_of_next_month(snapshot_date: date) -> date:
    if snapshot_date.month == 12:
        return date(snapshot_date.year + 1, 1, 1)
    return date(snapshot_date.year, snapshot_date.month + 1, 1)


def fetch_regular_current_balance_rows_from_onec(
    onec_engine,
    *,
    snapshot_date: date,
    employee_counterparty_refs: Sequence[str] = (),
) -> list[AuthoritativeReceivableBalanceRow]:
    if not _is_month_end(snapshot_date):
        return []

    bind = onec_engine
    dialect_name = bind.dialect.name
    ref_expr = _hex_ref_expr("counterparty._IDRRef", dialect_name=dialect_name)
    nolock = _with_nolock(dialect_name=dialect_name)
    params: dict[str, Any] = {
        "totals_period": datetime.combine(_first_day_of_next_month(snapshot_date), time.min),
    }
    employee_counterparty_refs_set = {ref.casefold() for ref in employee_counterparty_refs if ref}

    regular_stmt = text(f"""
        SELECT
            {ref_expr} AS counterparty_ref,
            counterparty._Description AS counterparty_name,
            CAST(SUM(CAST(t._Fld7562 AS decimal(18, 2))) AS decimal(18, 2)) AS current_balance
        FROM _AccumRgTn7571 AS t {nolock}
        JOIN _Reference54 AS counterparty {nolock}
            ON counterparty._IDRRef = t._Fld7559RRef
        WHERE t._Period = :totals_period
          AND t._Fld7559RRef <> 0x00000000000000000000000000000000
        GROUP BY
            counterparty._IDRRef,
            counterparty._Description
        HAVING SUM(CAST(t._Fld7562 AS decimal(18, 2))) <> 0
        """)
    summary_stmt = text(f"""
        SELECT
            {ref_expr} AS counterparty_ref,
            counterparty._Description AS counterparty_name,
            CAST(SUM(CAST(t._Fld7620 AS decimal(18, 2))) AS decimal(18, 2)) AS current_balance
        FROM _AccumRgT7622 AS t {nolock}
        JOIN _Reference54 AS counterparty {nolock}
            ON counterparty._IDRRef = t._Fld7619RRef
        LEFT JOIN _Reference37 AS contract {nolock}
            ON contract._IDRRef = t._Fld7615RRef
        WHERE t._Period = :totals_period
          AND t._Fld7619RRef <> 0x00000000000000000000000000000000
        GROUP BY
            counterparty._IDRRef,
            counterparty._Description
        HAVING SUM(CAST(t._Fld7620 AS decimal(18, 2))) <> 0
        """)

    items: dict[str, AuthoritativeReceivableBalanceRow] = {}
    with onec_engine.connect() as conn:
        for stmt in (regular_stmt, summary_stmt):
            for row in conn.execute(stmt, params).mappings():
                counterparty_ref = _clean_string(row.get("counterparty_ref"))
                if counterparty_ref is None:
                    continue
                if counterparty_ref.casefold() in employee_counterparty_refs_set:
                    continue
                items[counterparty_ref] = AuthoritativeReceivableBalanceRow(
                    counterparty_ref=counterparty_ref,
                    counterparty_name=_clean_string(row.get("counterparty_name")),
                    current_balance=_quantize_amount(_to_decimal(row.get("current_balance"))),
                    source="onec_month_end_totals",
                )
    return sorted(
        items.values(), key=lambda item: (item.counterparty_name or "", item.counterparty_ref)
    )


def fetch_regular_current_balance_overrides_from_onec(
    onec_engine,
    *,
    snapshot_date: date,
    employee_counterparty_refs: Sequence[str] = (),
) -> dict[str, Decimal]:
    overrides: dict[str, Decimal] = {}
    for row in fetch_regular_current_balance_rows_from_onec(
        onec_engine,
        snapshot_date=snapshot_date,
        employee_counterparty_refs=employee_counterparty_refs,
    ):
        key = _normalize_counterparty_match_key(row.counterparty_name)
        if key is None:
            continue
        overrides[key] = _quantize_amount(row.current_balance)
    return overrides


def _merge_authoritative_balance_rows(
    rows: Sequence[AuthoritativeReceivableBalanceRow],
) -> list[AuthoritativeReceivableBalanceRow]:
    items: dict[str, AuthoritativeReceivableBalanceRow] = {}
    for row in rows:
        items[row.counterparty_ref] = AuthoritativeReceivableBalanceRow(
            counterparty_ref=row.counterparty_ref,
            counterparty_name=row.counterparty_name
            or (
                items[row.counterparty_ref].counterparty_name
                if row.counterparty_ref in items
                else None
            ),
            current_balance=_quantize_amount(row.current_balance),
            current_manager_ref=row.current_manager_ref
            or (
                items[row.counterparty_ref].current_manager_ref
                if row.counterparty_ref in items
                else None
            ),
            current_manager_name=row.current_manager_name
            or (
                items[row.counterparty_ref].current_manager_name
                if row.counterparty_ref in items
                else None
            ),
            source=row.source,
        )
    return sorted(
        items.values(), key=lambda item: (item.counterparty_name or "", item.counterparty_ref)
    )


def _last_day_of_previous_month(snapshot_date: date) -> date:
    return snapshot_date.replace(day=1) - timedelta(days=1)


def _authoritative_opening_balance_date(snapshot_date: date) -> date:
    if _is_month_end(snapshot_date):
        return snapshot_date
    return _last_day_of_previous_month(snapshot_date)


def _latest_employee_summary_opening_balance_date(
    onec_engine,
    *,
    snapshot_date: date,
) -> date | None:
    bind = onec_engine
    dialect_name = bind.dialect.name
    nolock = _with_nolock(dialect_name=dialect_name)
    stmt = text(f"""
        SELECT MAX(CAST(t._Period AS date)) AS period
        FROM _AccumRgT7622 AS t {nolock}
        WHERE t._Period <= :snapshot_cutoff
    """)
    params = {
        "snapshot_cutoff": datetime.combine(snapshot_date, time.min),
    }
    with onec_engine.connect() as conn:
        period = conn.execute(stmt, params).scalar()
    if period is None:
        return None
    if isinstance(period, datetime):
        return period.date()
    return period


def _authoritative_layer_opening_dates(
    onec_engine,
    *,
    snapshot_date: date,
) -> dict[str, date]:
    regular_opening_balance_date = _authoritative_opening_balance_date(snapshot_date)
    employee_opening_balance_date = _latest_employee_summary_opening_balance_date(
        onec_engine,
        snapshot_date=snapshot_date,
    )
    if employee_opening_balance_date is None:
        employee_opening_balance_date = regular_opening_balance_date
    return {
        "regular_opening": regular_opening_balance_date,
        "employee_opening": employee_opening_balance_date,
    }


def _filter_authoritative_layer_rows(
    *,
    layer_name: str,
    rows: Sequence[ReceivableLedgerRow],
    employee_counterparty_refs: Sequence[str] = (),
) -> list[ReceivableLedgerRow]:
    employee_counterparty_refs_cf = {
        ref.casefold() for ref in employee_counterparty_refs if isinstance(ref, str) and ref
    }
    if not employee_counterparty_refs_cf:
        return list(rows)

    if layer_name in {"regular_opening", "sales_returns", "payments", "settlements"}:
        return [
            row
            for row in rows
            if row.counterparty_ref.casefold() not in employee_counterparty_refs_cf
        ]
    if layer_name in {"employee_opening", "employee_movements"}:
        return [
            row for row in rows if row.counterparty_ref.casefold() in employee_counterparty_refs_cf
        ]
    return list(rows)


def _iter_authoritative_extractor_rows(
    onec_engine,
    *,
    opening_balance_date: date,
    snapshot_date: date,
    employee_counterparty_refs: Sequence[str] = (),
):
    # Import lazily to avoid a circular dependency between receivables services and layer SQL.
    from app.services.receivables_extractors import (
        RECEIVABLE_DAILY_LAYER_NAMES,
        RECEIVABLE_OPENING_LAYER_NAMES,
        build_receivable_layer_extractors,
    )

    layer_extractors = build_receivable_layer_extractors(onec_engine)
    layer_opening_dates = _authoritative_layer_opening_dates(
        onec_engine,
        snapshot_date=snapshot_date,
    )

    opening_dates_by_layer = {
        "regular_opening": layer_opening_dates["regular_opening"],
        "employee_opening": layer_opening_dates["employee_opening"],
    }
    for layer_name in RECEIVABLE_OPENING_LAYER_NAMES:
        yield layer_name, _filter_authoritative_layer_rows(
            layer_name=layer_name,
            rows=layer_extractors[layer_name].iter_receivable_events(
                opening_balance_date=opening_dates_by_layer[layer_name],
            ),
            employee_counterparty_refs=employee_counterparty_refs,
        )

    layer_anchor_dates = {
        "sales_returns": layer_opening_dates["regular_opening"],
        "payments": layer_opening_dates["regular_opening"],
        "settlements": layer_opening_dates["regular_opening"],
        "employee_movements": layer_opening_dates["employee_opening"],
    }
    window_end = datetime.combine(snapshot_date + timedelta(days=1), time.min)
    for layer_name in RECEIVABLE_DAILY_LAYER_NAMES:
        layer_anchor_date = layer_anchor_dates[layer_name]
        if snapshot_date <= layer_anchor_date:
            yield layer_name, []
            continue
        if layer_name == "employee_movements":
            # Employee monthly totals are stored at the first day of the month and represent
            # the start-of-day balance, so movements from that same date must be included.
            window_start = datetime.combine(layer_anchor_date, time.min)
        else:
            window_start = datetime.combine(layer_anchor_date + timedelta(days=1), time.min)
        yield layer_name, _filter_authoritative_layer_rows(
            layer_name=layer_name,
            rows=layer_extractors[layer_name].iter_receivable_events(
                window_start=window_start,
                window_end=window_end,
            ),
            employee_counterparty_refs=employee_counterparty_refs,
        )


def _authoritative_balance_rows_from_events(
    events: Sequence[ReceivableLedgerRow],
) -> list[AuthoritativeReceivableBalanceRow]:
    employee_summary_duplicates = {
        (
            event.counterparty_ref,
            event.external_document_date,
            _quantize_amount(Decimal(str(event.amount_delta))),
        )
        for event in events
        if event.source_layer == "employee_summary" and event.event_type == EVENT_DEBT_ADJUSTMENT
    }

    balances: dict[str, Decimal] = {}
    latest_counterparty_names: dict[str, tuple[tuple[datetime, int, str], str]] = {}
    latest_managers: dict[str, tuple[tuple[datetime, int, str], str | None, str | None]] = {}

    for event in events:
        if event.skip_ingest or not event.counterparty_ref:
            continue

        if (
            event.source_layer == "regular_receivables"
            and (
                event.counterparty_ref,
                event.external_document_date,
                _quantize_amount(Decimal(str(event.amount_delta))),
            )
            in employee_summary_duplicates
        ):
            continue

        balances[event.counterparty_ref] = balances.get(
            event.counterparty_ref, Decimal("0.00")
        ) + _quantize_amount(Decimal(str(event.amount_delta)))

        order_key = (
            event.external_document_date,
            int(event.line_no or 0),
            event.external_document_ref,
        )

        if event.counterparty_name is not None:
            current_name = latest_counterparty_names.get(event.counterparty_ref)
            if current_name is None or order_key >= current_name[0]:
                latest_counterparty_names[event.counterparty_ref] = (
                    order_key,
                    event.counterparty_name,
                )

        if event.manager_ref is not None or event.manager_name is not None:
            current_manager = latest_managers.get(event.counterparty_ref)
            if current_manager is None or order_key >= current_manager[0]:
                latest_managers[event.counterparty_ref] = (
                    order_key,
                    event.manager_ref,
                    event.manager_name,
                )

    rows: list[AuthoritativeReceivableBalanceRow] = []
    for counterparty_ref, balance in balances.items():
        current_balance = _quantize_amount(balance)
        if current_balance == 0:
            continue
        latest_name = latest_counterparty_names.get(counterparty_ref)
        latest_manager = latest_managers.get(counterparty_ref)
        rows.append(
            AuthoritativeReceivableBalanceRow(
                counterparty_ref=counterparty_ref,
                counterparty_name=latest_name[1] if latest_name is not None else None,
                current_balance=current_balance,
                current_manager_ref=latest_manager[1] if latest_manager is not None else None,
                current_manager_name=latest_manager[2] if latest_manager is not None else None,
                source="onec_authoritative_daily_extractor",
            )
        )
    rows.sort(key=lambda item: (item.counterparty_name or "", item.counterparty_ref))
    return rows


def _fetch_open_debt_managers_from_onec(
    onec_engine,
    *,
    counterparty_refs: Sequence[str],
    movement_end: datetime,
) -> dict[str, tuple[str | None, str | None]]:
    refs = sorted({value for value in counterparty_refs if value})
    if not refs:
        return {}

    dialect_name = onec_engine.dialect.name
    nolock = _with_nolock(dialect_name=dialect_name)
    counterparty_ref_expr = _hex_ref_expr("r._Fld7006RRef", dialect_name=dialect_name)
    manager_ref_expr = _hex_ref_expr("sale._Fld4950RRef", dialect_name=dialect_name)
    sale_tref_expr = "0x000000CB" if dialect_name == "mssql" else ":sale_tref"
    open_debt_managers: dict[str, tuple[str | None, str | None]] = {}
    chunk_size = 1

    with onec_engine.connect() as conn:
        for offset in range(0, len(refs), chunk_size):
            chunk = refs[offset : offset + chunk_size]
            where_clause, params = _build_ref_filter_clause(
                dialect_name=dialect_name,
                refs=chunk,
                column_name="r._Fld7006RRef",
                prefix=f"counterparty_ref_{offset}",
            )
            params["movement_end"] = movement_end
            if dialect_name != "mssql":
                params["sale_tref"] = "0x000000CB"

            stmt = text(f"""
                SELECT
                    {counterparty_ref_expr} AS counterparty_ref,
                    r._Period AS period,
                    r._LineNo AS line_no,
                    CAST(
                        CASE
                            WHEN r._RecordKind = 0 THEN r._Fld7008
                            ELSE -r._Fld7008
                        END AS decimal(18, 2)
                    ) AS signed_amount,
                    CASE WHEN r._RecorderTRef = {sale_tref_expr} THEN 1 ELSE 0 END AS is_sale,
                    {manager_ref_expr} AS manager_ref,
                    manager._Description AS manager_name
                FROM _AccumRg7002 AS r {nolock}
                LEFT JOIN _Document203 AS sale {nolock}
                    ON r._RecorderTRef = {sale_tref_expr}
                   AND sale._IDRRef = r._RecorderRRef
                LEFT JOIN _Reference69 AS manager {nolock}
                    ON manager._IDRRef = sale._Fld4950RRef
                WHERE r._Active = 0x01
                  AND r._Period < :movement_end
                  AND {where_clause}
                ORDER BY
                    r._Fld7006RRef,
                    r._Period,
                    r._LineNo,
                    r._RecorderRRef
            """)
            current_ref: str | None = None
            running_balance = Decimal("0")
            for row in conn.execute(stmt, params).mappings():
                counterparty_ref = _clean_string(row.get("counterparty_ref"))
                if counterparty_ref is None:
                    continue
                if current_ref != counterparty_ref:
                    current_ref = counterparty_ref
                    running_balance = Decimal("0")

                signed_amount = _quantize_amount(_to_decimal(row.get("signed_amount")))
                previous_balance = running_balance
                running_balance = _quantize_amount(running_balance + signed_amount)
                if running_balance <= 0:
                    open_debt_managers.pop(counterparty_ref, None)
                    continue
                if _to_int(row.get("is_sale")) == 1 and signed_amount > 0 and previous_balance <= 0:
                    open_debt_managers[counterparty_ref] = (
                        _clean_string(row.get("manager_ref")),
                        _clean_string(row.get("manager_name")),
                    )

    return open_debt_managers


def _fetch_canonical_summary_current_balance_rows_from_onec(
    onec_engine,
    *,
    snapshot_date: date,
) -> tuple[list[AuthoritativeReceivableBalanceRow], dict[str, Any]]:
    """Build one signed balance per counterparty from the 1C mutual-settlement summary."""

    opening_cutoff = date(snapshot_date.year, snapshot_date.month, 1)
    movement_start = datetime.combine(opening_cutoff, time.min)
    movement_end = datetime.combine(snapshot_date + timedelta(days=1), time.min)

    stmt = text("""
        WITH
        latest_opening_period AS (
            SELECT MAX(t._Period) AS period
            FROM _AccumRgT7009 AS t WITH (NOLOCK)
            WHERE t._Period <= :opening_cutoff
        ),
        opening_rows AS (
            SELECT
                t._Fld7006RRef AS counterparty_rref,
                SUM(CAST(t._Fld7008 AS decimal(18, 2))) AS amount
            FROM _AccumRgT7009 AS t WITH (NOLOCK)
            JOIN latest_opening_period AS p
                ON t._Period = p.period
            WHERE t._Fld7006RRef <> 0x00000000000000000000000000000000
            GROUP BY
                t._Fld7006RRef
            HAVING SUM(CAST(t._Fld7008 AS decimal(18, 2))) <> 0
        ),
        movement_rows AS (
            SELECT
                r._Fld7006RRef AS counterparty_rref,
                SUM(
                    CAST(
                        CASE
                            -- _Fld7008 is the report's RUB amount for the full
                            -- mutual-settlement statement register.
                            WHEN r._RecordKind = 0 THEN r._Fld7008
                            ELSE -r._Fld7008
                        END AS decimal(18, 2)
                    )
                ) AS amount
            FROM _AccumRg7002 AS r WITH (NOLOCK)
            WHERE r._Active = 0x01
              AND r._Fld7006RRef <> 0x00000000000000000000000000000000
              AND r._Period >= :movement_start
              AND r._Period < :movement_end
            GROUP BY
                r._Fld7006RRef
            HAVING SUM(
                CAST(
                    CASE
                        WHEN r._RecordKind = 0 THEN r._Fld7008
                        ELSE -r._Fld7008
                    END AS decimal(18, 2)
                )
            ) <> 0
        ),
        balances AS (
            SELECT
                source_rows.counterparty_rref,
                SUM(source_rows.amount) AS current_balance
            FROM (
                SELECT counterparty_rref, amount
                FROM opening_rows

                UNION ALL

                SELECT counterparty_rref, amount
                FROM movement_rows
            ) AS source_rows
            GROUP BY
                source_rows.counterparty_rref
            HAVING SUM(source_rows.amount) <> 0
        ),
        latest_sale_managers AS (
            SELECT
                r._Fld7006RRef AS counterparty_rref,
                sale._Fld4950RRef AS manager_rref,
                manager._Description AS manager_name,
                ROW_NUMBER() OVER (
                    PARTITION BY r._Fld7006RRef
                    ORDER BY sale._Date_Time DESC, r._Period DESC, r._LineNo DESC
                ) AS rn
            FROM _AccumRg7002 AS r WITH (NOLOCK)
            JOIN _Document203 AS sale WITH (NOLOCK)
                ON r._RecorderTRef = 0x000000CB
               AND sale._IDRRef = r._RecorderRRef
            LEFT JOIN _Reference69 AS manager WITH (NOLOCK)
                ON manager._IDRRef = sale._Fld4950RRef
            WHERE r._Active = 0x01
              AND r._Fld7006RRef <> 0x00000000000000000000000000000000
              AND r._Period < :movement_end
        )
        SELECT
            master.dbo.fn_varbintohexstr(counterparty._IDRRef) AS counterparty_ref,
            counterparty._Description AS counterparty_name,
            CAST(balances.current_balance AS decimal(18, 2)) AS current_balance,
            master.dbo.fn_varbintohexstr(latest_manager.manager_rref) AS current_manager_ref,
            latest_manager.manager_name AS current_manager_name,
            CAST((SELECT period FROM latest_opening_period) AS date) AS opening_period,
            (SELECT COUNT(*) FROM opening_rows) AS opening_row_count,
            (SELECT COUNT(*) FROM movement_rows) AS daily_movement_row_count
        FROM balances
        JOIN _Reference54 AS counterparty WITH (NOLOCK)
            ON counterparty._IDRRef = balances.counterparty_rref
        LEFT JOIN latest_sale_managers AS latest_manager
            ON latest_manager.counterparty_rref = balances.counterparty_rref
           AND latest_manager.rn = 1
        ORDER BY
            counterparty._Description,
            counterparty._IDRRef
        """)
    params = {
        "opening_cutoff": datetime.combine(opening_cutoff, time.min),
        "movement_start": movement_start,
        "movement_end": movement_end,
    }

    rows: list[AuthoritativeReceivableBalanceRow] = []
    opening_period: date | None = None
    opening_row_count = 0
    daily_movement_row_count = 0
    with onec_engine.connect() as conn:
        for row in conn.execute(stmt, params).mappings():
            counterparty_ref = _clean_string(row.get("counterparty_ref"))
            if counterparty_ref is None:
                continue
            if opening_period is None:
                raw_opening_period = row.get("opening_period")
                if isinstance(raw_opening_period, datetime):
                    opening_period = raw_opening_period.date()
                elif isinstance(raw_opening_period, date):
                    opening_period = raw_opening_period
            opening_row_count = _to_int(row.get("opening_row_count")) or opening_row_count
            daily_movement_row_count = (
                _to_int(row.get("daily_movement_row_count")) or daily_movement_row_count
            )
            rows.append(
                AuthoritativeReceivableBalanceRow(
                    counterparty_ref=counterparty_ref,
                    counterparty_name=_clean_string(row.get("counterparty_name")),
                    current_balance=_quantize_amount(_to_decimal(row.get("current_balance"))),
                    current_manager_ref=_clean_string(row.get("current_manager_ref")),
                    current_manager_name=_clean_string(row.get("current_manager_name")),
                    source="onec_canonical_mutual_statement",
                )
            )

    open_debt_managers = _fetch_open_debt_managers_from_onec(
        onec_engine,
        counterparty_refs=[row.counterparty_ref for row in rows if row.current_balance > 0],
        movement_end=movement_end,
    )
    if open_debt_managers:
        rows = [
            AuthoritativeReceivableBalanceRow(
                counterparty_ref=row.counterparty_ref,
                counterparty_name=row.counterparty_name,
                current_balance=row.current_balance,
                current_manager_ref=open_debt_managers.get(row.counterparty_ref, (None, None))[0]
                or row.current_manager_ref,
                current_manager_name=open_debt_managers.get(row.counterparty_ref, (None, None))[1]
                or row.current_manager_name,
                source=row.source,
            )
            for row in rows
        ]

    meta = {
        "regular_current_override_count": 0,
        "current_import_override_count": 0,
        "total_current_override_count": 0,
        "employee_current_import_override_count": 0,
        "authoritative_balance_row_count": len(rows),
        "opening_balance_date": opening_cutoff,
        "opening_balance_dates": [opening_cutoff],
        "regular_opening_balance_date": opening_cutoff,
        "employee_opening_balance_date": None,
        "canonical_opening_register_period": opening_period,
        "opening_row_count": opening_row_count,
        "daily_movement_row_count": daily_movement_row_count,
        "balance_source_mode": "onec_canonical_mutual_statement_7002",
    }
    return rows, meta


def fetch_current_balances_from_onec(
    onec_engine,
    *,
    snapshot_date: date,
    employee_counterparty_refs: Sequence[str] = (),
    current_import_path: str | None = None,
    current_import_counterparty_group: str | None = "ПОКУПАТЕЛИ",
    employee_current_import_path: str | None = None,
    employee_current_import_counterparty_group: str | None = "СОТРУДНИКИ",
) -> tuple[list[AuthoritativeReceivableBalanceRow], dict[str, Any]]:
    _ = (
        employee_counterparty_refs,
        current_import_counterparty_group,
        employee_current_import_counterparty_group,
    )
    if current_import_path or employee_current_import_path:
        raise ValueError(
            "Excel current-import больше не поддерживается в production balance path: "
            "authoritative остатки строятся только из daily extractor 1С."
        )

    if getattr(onec_engine.dialect, "name", "") == "mssql":
        return _fetch_canonical_summary_current_balance_rows_from_onec(
            onec_engine,
            snapshot_date=snapshot_date,
        )

    opening_balance_date = _authoritative_opening_balance_date(snapshot_date)
    layer_opening_dates = _authoritative_layer_opening_dates(
        onec_engine,
        snapshot_date=snapshot_date,
    )
    extractor_rows_by_layer: dict[str, list[ReceivableLedgerRow]] = {}
    for layer_name, layer_rows in _iter_authoritative_extractor_rows(
        onec_engine,
        opening_balance_date=opening_balance_date,
        snapshot_date=snapshot_date,
        employee_counterparty_refs=employee_counterparty_refs,
    ):
        extractor_rows_by_layer[layer_name] = layer_rows

    rows = _authoritative_balance_rows_from_events(
        [row for layer_rows in extractor_rows_by_layer.values() for row in layer_rows]
    )
    return (
        rows,
        {
            "regular_current_override_count": 0,
            "current_import_override_count": 0,
            "total_current_override_count": 0,
            "employee_current_import_override_count": 0,
            "authoritative_balance_row_count": len(rows),
            "opening_balance_date": opening_balance_date,
            "opening_balance_dates": sorted(set(layer_opening_dates.values())),
            "regular_opening_balance_date": layer_opening_dates["regular_opening"],
            "employee_opening_balance_date": layer_opening_dates["employee_opening"],
            "opening_row_count": sum(
                len(extractor_rows_by_layer.get(layer_name, []))
                for layer_name in ("regular_opening", "employee_opening")
            ),
            "daily_movement_row_count": sum(
                len(extractor_rows_by_layer.get(layer_name, []))
                for layer_name in (
                    "sales_returns",
                    "payments",
                    "settlements",
                    "employee_movements",
                )
            ),
            "balance_source_mode": "authoritative_from_onec_daily_extractor",
        },
    )


def _iter_event_batches(
    events: Iterable[ReceivableLedgerRow],
    *,
    batch_size: int,
) -> Iterator[list[ReceivableLedgerRow]]:
    batch: list[ReceivableLedgerRow] = []
    for event in events:
        batch.append(event)
        if len(batch) >= batch_size:
            yield batch
            batch = []
    if batch:
        yield batch


def upsert_receivable_ledger_events(
    session: Session, events: Sequence[ReceivableLedgerRow]
) -> dict[str, Any]:
    if not events:
        return {"processed": 0, "inserted": 0, "updated": 0, "existing": 0}

    business_keys = [event.business_key for event in events]
    existing_rows = (
        session.execute(
            select(ReceivableLedgerEvent).where(
                ReceivableLedgerEvent.business_key.in_(business_keys)
            )
        )
        .scalars()
        .all()
    )
    existing_by_key = {row.business_key: row for row in existing_rows}

    inserted = 0
    updated = 0
    for event in events:
        existing = existing_by_key.get(event.business_key)
        if existing is not None:
            changed = False
            field_names = (
                "event_type",
                "external_document_ref",
                "external_document_number",
                "external_document_date",
                "counterparty_ref",
                "counterparty_name",
                "contract_ref",
                "contract_name",
                "contract_kind_ref",
                "contract_kind_name",
                "manager_ref",
                "manager_name",
                "store_ref",
                "store_name",
                "source_layer",
                "planned_payment_date",
                "credit_depth_days",
                "shipment_ban",
                "line_no",
                "amount_delta",
            )
            for field_name in field_names:
                new_value = getattr(event, field_name)
                if getattr(existing, field_name) != new_value:
                    setattr(existing, field_name, new_value)
                    changed = True
            if changed:
                updated += 1
            continue
        session.add(
            ReceivableLedgerEvent(
                source=event.source,
                business_key=event.business_key,
                event_type=event.event_type,
                external_document_ref=event.external_document_ref,
                external_document_number=event.external_document_number,
                external_document_date=event.external_document_date,
                counterparty_ref=event.counterparty_ref,
                counterparty_name=event.counterparty_name,
                contract_ref=event.contract_ref,
                contract_name=event.contract_name,
                contract_kind_ref=event.contract_kind_ref,
                contract_kind_name=event.contract_kind_name,
                manager_ref=event.manager_ref,
                manager_name=event.manager_name,
                store_ref=event.store_ref,
                store_name=event.store_name,
                source_layer=event.source_layer,
                planned_payment_date=event.planned_payment_date,
                credit_depth_days=event.credit_depth_days,
                shipment_ban=event.shipment_ban,
                line_no=event.line_no,
                amount_delta=event.amount_delta,
            )
        )
        inserted += 1

    return {
        "processed": len(events),
        "inserted": inserted,
        "updated": updated,
        "existing": len(events) - inserted - updated,
    }


def rebuild_counterparty_manager_assignments(session: Session) -> dict[str, int]:
    session.execute(delete(CounterpartyManagerAssignment))

    inserted = 0
    ledger_event = ReceivableLedgerEvent.__table__
    stmt = (
        select(
            ledger_event.c.id,
            ledger_event.c.source,
            ledger_event.c.business_key,
            ledger_event.c.event_type,
            ledger_event.c.external_document_date,
            ledger_event.c.counterparty_ref,
            ledger_event.c.counterparty_name,
            ledger_event.c.manager_ref,
            ledger_event.c.manager_name,
        )
        .select_from(ledger_event)
        .where(ledger_event.c.manager_ref.is_not(None))
        .order_by(
            ledger_event.c.counterparty_ref,
            ledger_event.c.external_document_date,
            ledger_event.c.id,
        )
        .execution_options(
            stream_results=True,
            max_row_buffer=DEFAULT_RECEIVABLE_SYNC_BATCH_SIZE,
        )
    )

    current_counterparty_ref: str | None = None
    previous_manager_ref: str | None = None
    pending_assignment: CounterpartyManagerAssignment | None = None

    bind = session.get_bind()
    if bind is None:
        raise RuntimeError("Database bind is not available for manager assignment rebuild")
    dialect_name = bind.dialect.name

    if dialect_name == "postgresql":
        read_connection_cm = bind.connect().execution_options(
            stream_results=True,
            max_row_buffer=DEFAULT_RECEIVABLE_SYNC_BATCH_SIZE,
        )
    else:
        read_connection_cm = nullcontext(
            session.connection().execution_options(
                stream_results=True,
                max_row_buffer=DEFAULT_RECEIVABLE_SYNC_BATCH_SIZE,
            )
        )

    with read_connection_cm as read_connection:
        for row in read_connection.execute(stmt).mappings():
            counterparty_ref = row["counterparty_ref"]
            manager_ref = row["manager_ref"]

            if current_counterparty_ref != counterparty_ref:
                if pending_assignment is not None:
                    session.add(pending_assignment)
                    inserted += 1
                    pending_assignment = None
                current_counterparty_ref = counterparty_ref
                previous_manager_ref = None

            if manager_ref == previous_manager_ref:
                continue

            if pending_assignment is not None:
                pending_assignment.effective_to = row["external_document_date"]
                session.add(pending_assignment)
                inserted += 1
                if inserted % DEFAULT_RECEIVABLE_SYNC_BATCH_SIZE == 0:
                    session.flush()
                    session.expunge_all()

            pending_assignment = CounterpartyManagerAssignment(
                source=row["source"],
                business_key=hashlib.sha256(
                    (
                        f"{row['counterparty_ref']}|{row['manager_ref']}|"
                        f"{row['external_document_date'].isoformat()}|{row['business_key']}"
                    ).encode()
                ).hexdigest(),
                counterparty_ref=row["counterparty_ref"],
                counterparty_name=row["counterparty_name"],
                manager_ref=manager_ref or "",
                manager_name=row["manager_name"],
                effective_from=row["external_document_date"],
                assignment_reason=row["event_type"],
                source_event_id=row["id"],
            )
            previous_manager_ref = manager_ref

    if pending_assignment is not None:
        session.add(pending_assignment)
        inserted += 1

    return {"assignments": inserted}


def _resolve_current_assignment(
    assignments: Sequence[CounterpartyManagerAssignment], snapshot_end: datetime
) -> CounterpartyManagerAssignment | None:
    for assignment in reversed(assignments):
        if assignment.effective_from <= snapshot_end and (
            assignment.effective_to is None or assignment.effective_to > snapshot_end
        ):
            return assignment
    return assignments[-1] if assignments else None


def _find_origin_event(events: Sequence[ReceivableLedgerEvent]) -> ReceivableLedgerEvent | None:
    balance = Decimal("0.00")
    for event in events:
        balance = _quantize_amount(balance + Decimal(event.amount_delta))
    if balance <= 0:
        return None
    origin_mapping = _find_unpaid_origin_event(events, current_balance=balance)
    if origin_mapping is None:
        return None
    origin_id = origin_mapping["id"]
    return next((event for event in events if event.id == origin_id), None)


def _resolve_counterparty_credit_terms(
    events: Sequence[ReceivableLedgerEvent],
    *,
    origin_event: ReceivableLedgerEvent | None,
    snapshot_date: date,
) -> dict[str, Any]:
    planned_payment_date: datetime | None = None
    credit_depth_days: int | None = None
    shipment_ban: bool | None = None

    for event in reversed(events):
        if planned_payment_date is None and event.planned_payment_date is not None:
            planned_payment_date = event.planned_payment_date
        if credit_depth_days is None and event.credit_depth_days is not None:
            credit_depth_days = event.credit_depth_days
        if shipment_ban is None and event.shipment_ban is not None:
            shipment_ban = event.shipment_ban
        if (
            planned_payment_date is not None
            and credit_depth_days is not None
            and shipment_ban is not None
        ):
            break

    return _resolve_counterparty_credit_terms_from_values(
        planned_payment_date=planned_payment_date,
        credit_depth_days=credit_depth_days,
        shipment_ban=shipment_ban,
        origin_document_date=origin_event.external_document_date if origin_event else None,
        snapshot_date=snapshot_date,
    )


def _resolve_counterparty_credit_terms_from_values(
    *,
    planned_payment_date: datetime | None,
    credit_depth_days: int | None,
    shipment_ban: bool | None,
    origin_document_date: datetime | None,
    snapshot_date: date,
) -> dict[str, Any]:
    normalized_planned_payment_date = planned_payment_date
    normalized_credit_depth_days = credit_depth_days
    normalized_shipment_ban = shipment_ban

    if (
        normalized_planned_payment_date is not None
        and origin_document_date is not None
        and normalized_planned_payment_date < origin_document_date
    ):
        normalized_planned_payment_date = None

    if normalized_credit_depth_days is not None and normalized_credit_depth_days <= 0:
        normalized_credit_depth_days = None

    due_date: datetime | None = None
    payment_term_source = "missing"
    if normalized_planned_payment_date is not None:
        due_date = normalized_planned_payment_date
        payment_term_source = "planned_payment_date"
    elif normalized_credit_depth_days is not None and origin_document_date is not None:
        due_date = origin_document_date + timedelta(days=normalized_credit_depth_days)
        payment_term_source = "credit_depth_days"

    overdue_days: int | None = None
    is_overdue = False
    if due_date is not None:
        overdue_days = max((snapshot_date - due_date.date()).days, 0)
        is_overdue = overdue_days > 0

    return {
        "planned_payment_date": normalized_planned_payment_date,
        "credit_depth_days": normalized_credit_depth_days,
        "shipment_ban": normalized_shipment_ban if normalized_shipment_ban is not None else False,
        "payment_term_source": payment_term_source,
        "due_date": due_date,
        "overdue_days": overdue_days,
        "is_overdue": is_overdue,
    }


def _build_receivable_ledger_enrichment_by_counterparty(
    session: Session,
    *,
    snapshot_date: date,
    authoritative_opening_balance_dates: Sequence[date] | None = None,
    counterparty_departments_by_ref: (
        Mapping[str, CounterpartyDepartmentRow | Mapping[str, Any]] | None
    ) = None,
) -> dict[str, dict[str, Any]]:
    snapshot_end = datetime.combine(snapshot_date, time.min) + timedelta(days=1)
    ledger_event = ReceivableLedgerEvent.__table__
    stmt = (
        select(
            ledger_event.c.id,
            ledger_event.c.event_type,
            ledger_event.c.external_document_ref,
            ledger_event.c.external_document_number,
            ledger_event.c.external_document_date,
            ledger_event.c.counterparty_ref,
            ledger_event.c.counterparty_name,
            ledger_event.c.manager_ref,
            ledger_event.c.manager_name,
            ledger_event.c.store_ref,
            ledger_event.c.store_name,
            ledger_event.c.planned_payment_date,
            ledger_event.c.credit_depth_days,
            ledger_event.c.shipment_ban,
            ledger_event.c.amount_delta,
        )
        .select_from(ledger_event)
        .where(ledger_event.c.external_document_date < snapshot_end)
        .order_by(
            ledger_event.c.counterparty_ref,
            ledger_event.c.external_document_date,
            ledger_event.c.id,
        )
        .execution_options(
            stream_results=True,
            max_row_buffer=DEFAULT_RECEIVABLE_SYNC_BATCH_SIZE,
        )
    )

    def empty_state() -> dict[str, Any]:
        return {
            "counterparty_ref": None,
            "counterparty_name": None,
            "running_balance": Decimal("0.00"),
            "origin_event": None,
            "last_sale_at": None,
            "last_payment_at": None,
            "planned_payment_date": None,
            "credit_depth_days": None,
            "shipment_ban": None,
            "current_manager_ref": None,
            "current_manager_name": None,
            "department_ref": None,
            "department_name": None,
            "debt_increase_events": [],
        }

    def finalize_state(state: dict[str, Any]) -> None:
        if state["counterparty_ref"] is None:
            return
        department_ref, department_name = _resolve_counterparty_department(
            counterparty_departments_by_ref,
            state["counterparty_ref"],
        )
        enrichment[state["counterparty_ref"]] = {
            "counterparty_name": state["counterparty_name"],
            "origin_event": state["origin_event"],
            "last_sale_at": state["last_sale_at"],
            "last_payment_at": state["last_payment_at"],
            "planned_payment_date": state["planned_payment_date"],
            "credit_depth_days": state["credit_depth_days"],
            "shipment_ban": state["shipment_ban"],
            "current_manager_ref": state["current_manager_ref"],
            "current_manager_name": state["current_manager_name"],
            "department_ref": department_ref,
            "department_name": department_name,
            "debt_increase_events": state["debt_increase_events"],
        }

    enrichment: dict[str, dict[str, Any]] = {}
    authoritative_opening_balance_dates_set = (
        {item for item in authoritative_opening_balance_dates if item is not None}
        if authoritative_opening_balance_dates is not None
        else None
    )
    bind = session.get_bind()
    if bind is None:
        raise RuntimeError("Database bind is not available for receivable enrichment build")
    dialect_name = bind.dialect.name

    if dialect_name == "postgresql":
        read_connection_cm = bind.connect().execution_options(
            stream_results=True,
            max_row_buffer=DEFAULT_RECEIVABLE_SYNC_BATCH_SIZE,
        )
    else:
        read_connection_cm = nullcontext(
            session.connection().execution_options(
                stream_results=True,
                max_row_buffer=DEFAULT_RECEIVABLE_SYNC_BATCH_SIZE,
            )
        )

    state = empty_state()
    with read_connection_cm as read_connection:
        for row in read_connection.execute(stmt).mappings():
            counterparty_ref = row["counterparty_ref"]
            amount_delta = Decimal(row["amount_delta"])
            row_external_document_date = row["external_document_date"]

            if (
                authoritative_opening_balance_dates_set is not None
                and row["event_type"] == EVENT_OPENING_BALANCE
                and row_external_document_date is not None
                and row_external_document_date.date() not in authoritative_opening_balance_dates_set
            ):
                continue

            if (
                state["counterparty_ref"] is not None
                and state["counterparty_ref"] != counterparty_ref
            ):
                finalize_state(state)
                state = empty_state()

            if state["counterparty_ref"] is None:
                state["counterparty_ref"] = counterparty_ref

            state["counterparty_name"] = row["counterparty_name"] or state["counterparty_name"]
            if _is_debt_increase_event(row):
                state["debt_increase_events"].append(dict(row))
            next_balance = _quantize_amount(state["running_balance"] + amount_delta)
            state["running_balance"] = next_balance

            if row["event_type"] == EVENT_SALE and amount_delta > 0:
                state["last_sale_at"] = row["external_document_date"]
            if row["event_type"] in {EVENT_PAYMENT, EVENT_SETTLEMENT} and amount_delta < 0:
                state["last_payment_at"] = row["external_document_date"]
            if row["planned_payment_date"] is not None:
                state["planned_payment_date"] = row["planned_payment_date"]
            if row["credit_depth_days"] is not None:
                state["credit_depth_days"] = row["credit_depth_days"]
            if row["shipment_ban"] is not None:
                state["shipment_ban"] = row["shipment_ban"]
            if row["manager_ref"] is not None:
                state["current_manager_ref"] = row["manager_ref"]
                state["current_manager_name"] = row["manager_name"]
    finalize_state(state)
    return enrichment


def build_receivable_balance_snapshots(
    session: Session,
    *,
    snapshot_date: date,
    authoritative_balance_rows: Sequence[AuthoritativeReceivableBalanceRow] | None = None,
    authoritative_opening_balance_dates: Sequence[date] | None = None,
    current_balance_overrides: dict[str, Decimal] | None = None,
    current_balance_override_names: dict[str, str] | None = None,
    strict_current_balance_overrides: bool = False,
    employee_counterparty_refs: Sequence[str] = (),
    employee_current_balance_overrides: dict[str, Decimal] | None = None,
    employee_current_balance_override_names: dict[str, str] | None = None,
    employee_current_balance_override_refs: dict[str, str] | None = None,
    strict_employee_current_balance_overrides: bool = False,
    counterparty_departments_by_ref: (
        Mapping[str, CounterpartyDepartmentRow | Mapping[str, Any]] | None
    ) = None,
) -> dict[str, int]:
    if authoritative_balance_rows is not None:
        session.execute(
            delete(ReceivableBalanceSnapshot).where(
                ReceivableBalanceSnapshot.snapshot_date == snapshot_date
            )
        )
        enrichment_by_counterparty = _build_receivable_ledger_enrichment_by_counterparty(
            session,
            snapshot_date=snapshot_date,
            authoritative_opening_balance_dates=authoritative_opening_balance_dates,
            counterparty_departments_by_ref=counterparty_departments_by_ref,
        )
        inserted = 0
        for row in authoritative_balance_rows:
            current_balance = _quantize_amount(row.current_balance)
            if current_balance == 0:
                continue
            enrichment = enrichment_by_counterparty.get(row.counterparty_ref, {})
            origin_event = _find_unpaid_origin_event(
                enrichment.get("debt_increase_events", ()),
                current_balance=current_balance,
            )
            current_manager_ref = row.current_manager_ref or enrichment.get("current_manager_ref")
            current_manager_name = row.current_manager_name or enrichment.get(
                "current_manager_name"
            )
            credit_terms = _resolve_counterparty_credit_terms_from_values(
                planned_payment_date=enrichment.get("planned_payment_date"),
                credit_depth_days=enrichment.get("credit_depth_days"),
                shipment_ban=enrichment.get("shipment_ban"),
                origin_document_date=(
                    origin_event["external_document_date"] if origin_event else None
                ),
                snapshot_date=snapshot_date,
            )
            session.add(
                ReceivableBalanceSnapshot(
                    snapshot_date=snapshot_date,
                    counterparty_ref=row.counterparty_ref,
                    counterparty_name=row.counterparty_name or enrichment.get("counterparty_name"),
                    current_balance=current_balance,
                    origin_event_id=origin_event["id"] if origin_event else None,
                    origin_document_ref=(
                        origin_event["external_document_ref"] if origin_event else None
                    ),
                    origin_document_number=(
                        origin_event["external_document_number"] if origin_event else None
                    ),
                    origin_document_date=(
                        origin_event["external_document_date"] if origin_event else None
                    ),
                    origin_manager_ref=origin_event["manager_ref"] if origin_event else None,
                    origin_manager_name=origin_event["manager_name"] if origin_event else None,
                    current_manager_ref=current_manager_ref,
                    current_manager_name=current_manager_name,
                    department_ref=enrichment.get("department_ref"),
                    department_name=enrichment.get("department_name"),
                    last_sale_at=enrichment.get("last_sale_at"),
                    last_payment_at=enrichment.get("last_payment_at"),
                    planned_payment_date=credit_terms["planned_payment_date"],
                    credit_depth_days=credit_terms["credit_depth_days"],
                    shipment_ban=credit_terms["shipment_ban"],
                    payment_term_source=credit_terms["payment_term_source"],
                    due_date=credit_terms["due_date"],
                    overdue_days=credit_terms["overdue_days"],
                    is_overdue=credit_terms["is_overdue"],
                    aged_bucket=compute_aged_bucket(
                        origin_event["external_document_date"].date() if origin_event else None,
                        snapshot_date,
                    ),
                    activity_segment=compute_activity_segment(
                        enrichment.get("last_sale_at"),
                        snapshot_date,
                    ),
                )
            )
            inserted += 1
            if inserted % DEFAULT_RECEIVABLE_SYNC_BATCH_SIZE == 0:
                session.flush()
                session.expunge_all()
        return {"snapshots": inserted, "overrides_applied": 0}

    snapshot_end = datetime.combine(snapshot_date, time.min) + timedelta(days=1)
    session.execute(
        delete(ReceivableBalanceSnapshot).where(
            ReceivableBalanceSnapshot.snapshot_date == snapshot_date
        )
    )

    inserted = 0
    overrides_applied = 0
    applied_override_keys: set[str] = set()
    employee_override_keys_applied: set[str] = set()
    employee_ref_set = {ref for ref in employee_counterparty_refs if ref}
    ledger_event = ReceivableLedgerEvent.__table__
    employee_duplicate = ledger_event.alias("employee_duplicate")
    stmt = (
        select(
            ledger_event.c.id,
            ledger_event.c.event_type,
            ledger_event.c.external_document_ref,
            ledger_event.c.external_document_number,
            ledger_event.c.external_document_date,
            ledger_event.c.counterparty_ref,
            ledger_event.c.counterparty_name,
            ledger_event.c.manager_ref,
            ledger_event.c.manager_name,
            ledger_event.c.store_ref,
            ledger_event.c.store_name,
            ledger_event.c.planned_payment_date,
            ledger_event.c.credit_depth_days,
            ledger_event.c.shipment_ban,
            ledger_event.c.amount_delta,
        )
        .select_from(ledger_event)
        .where(ledger_event.c.external_document_date < snapshot_end)
        .where(
            ~and_(
                ledger_event.c.source_layer == "regular_receivables",
                exists(
                    select(1).where(
                        employee_duplicate.c.id != ledger_event.c.id,
                        employee_duplicate.c.counterparty_ref == ledger_event.c.counterparty_ref,
                        employee_duplicate.c.external_document_date
                        == ledger_event.c.external_document_date,
                        employee_duplicate.c.amount_delta == ledger_event.c.amount_delta,
                        employee_duplicate.c.source_layer == "employee_summary",
                        employee_duplicate.c.event_type == EVENT_DEBT_ADJUSTMENT,
                        employee_duplicate.c.external_document_date < snapshot_end,
                    )
                ),
            )
        )
        .order_by(
            ledger_event.c.counterparty_ref,
            ledger_event.c.external_document_date,
            ledger_event.c.id,
        )
        .execution_options(
            stream_results=True,
            max_row_buffer=DEFAULT_RECEIVABLE_SYNC_BATCH_SIZE,
        )
    )

    def empty_state() -> dict[str, Any]:
        return {
            "counterparty_ref": None,
            "counterparty_name": None,
            "balance": Decimal("0.00"),
            "origin_event": None,
            "last_sale_at": None,
            "last_payment_at": None,
            "planned_payment_date": None,
            "credit_depth_days": None,
            "shipment_ban": None,
            "current_manager_ref": None,
            "current_manager_name": None,
            "department_ref": None,
            "department_name": None,
            "debt_increase_events": [],
        }

    def finalize_state(state: dict[str, Any]) -> None:
        nonlocal inserted, overrides_applied
        if state["counterparty_ref"] is None:
            return

        effective_balance = state["balance"]
        override_key: str | None = None
        employee_override_key: str | None = None
        is_employee_counterparty = state["counterparty_ref"] in employee_ref_set
        if current_balance_overrides:
            override_key = _normalize_counterparty_match_key(state["counterparty_name"])
            if override_key is not None and override_key in current_balance_overrides:
                if strict_current_balance_overrides and override_key in applied_override_keys:
                    return
                effective_balance = current_balance_overrides[override_key]
                overrides_applied += 1
                applied_override_keys.add(override_key)
            elif strict_current_balance_overrides and not (
                is_employee_counterparty
                and employee_current_balance_overrides
                and override_key is not None
                and override_key in employee_current_balance_overrides
            ):
                return

        if employee_current_balance_overrides and is_employee_counterparty:
            employee_override_key = _normalize_counterparty_match_key(state["counterparty_name"])
            if (
                employee_override_key is not None
                and employee_override_key in employee_current_balance_overrides
            ):
                if (
                    strict_employee_current_balance_overrides
                    and employee_override_key in employee_override_keys_applied
                ):
                    return
                effective_balance = employee_current_balance_overrides[employee_override_key]
                overrides_applied += 1
                employee_override_keys_applied.add(employee_override_key)
            elif strict_employee_current_balance_overrides:
                return

        if effective_balance == 0:
            return

        origin_event = _find_unpaid_origin_event(
            state["debt_increase_events"],
            current_balance=effective_balance,
        )
        department_ref, department_name = _resolve_counterparty_department(
            counterparty_departments_by_ref,
            state["counterparty_ref"],
        )
        credit_terms = _resolve_counterparty_credit_terms_from_values(
            planned_payment_date=state["planned_payment_date"],
            credit_depth_days=state["credit_depth_days"],
            shipment_ban=state["shipment_ban"],
            origin_document_date=origin_event["external_document_date"] if origin_event else None,
            snapshot_date=snapshot_date,
        )
        session.add(
            ReceivableBalanceSnapshot(
                snapshot_date=snapshot_date,
                counterparty_ref=state["counterparty_ref"],
                counterparty_name=state["counterparty_name"],
                current_balance=effective_balance,
                origin_event_id=origin_event["id"] if origin_event else None,
                origin_document_ref=origin_event["external_document_ref"] if origin_event else None,
                origin_document_number=(
                    origin_event["external_document_number"] if origin_event else None
                ),
                origin_document_date=(
                    origin_event["external_document_date"] if origin_event else None
                ),
                origin_manager_ref=origin_event["manager_ref"] if origin_event else None,
                origin_manager_name=origin_event["manager_name"] if origin_event else None,
                current_manager_ref=state["current_manager_ref"],
                current_manager_name=state["current_manager_name"],
                department_ref=department_ref,
                department_name=department_name,
                last_sale_at=state["last_sale_at"],
                last_payment_at=state["last_payment_at"],
                planned_payment_date=credit_terms["planned_payment_date"],
                credit_depth_days=credit_terms["credit_depth_days"],
                shipment_ban=credit_terms["shipment_ban"],
                payment_term_source=credit_terms["payment_term_source"],
                due_date=credit_terms["due_date"],
                overdue_days=credit_terms["overdue_days"],
                is_overdue=credit_terms["is_overdue"],
                aged_bucket=compute_aged_bucket(
                    origin_event["external_document_date"].date() if origin_event else None,
                    snapshot_date,
                ),
                activity_segment=compute_activity_segment(state["last_sale_at"], snapshot_date),
            )
        )
        inserted += 1
        if inserted % DEFAULT_RECEIVABLE_SYNC_BATCH_SIZE == 0:
            session.flush()
            session.expunge_all()

    state = empty_state()
    bind = session.get_bind()
    if bind is None:
        raise RuntimeError("Database bind is not available for snapshot build")
    dialect_name = bind.dialect.name

    if dialect_name == "postgresql":
        read_connection_cm = bind.connect().execution_options(
            stream_results=True,
            max_row_buffer=DEFAULT_RECEIVABLE_SYNC_BATCH_SIZE,
        )
    else:
        read_connection_cm = nullcontext(
            session.connection().execution_options(
                stream_results=True,
                max_row_buffer=DEFAULT_RECEIVABLE_SYNC_BATCH_SIZE,
            )
        )

    with read_connection_cm as read_connection:
        for row in read_connection.execute(stmt).mappings():
            counterparty_ref = row["counterparty_ref"]
            amount_delta = Decimal(row["amount_delta"])

            if (
                state["counterparty_ref"] is not None
                and state["counterparty_ref"] != counterparty_ref
            ):
                finalize_state(state)
                state = empty_state()

            if state["counterparty_ref"] is None:
                state["counterparty_ref"] = counterparty_ref

            state["counterparty_name"] = row["counterparty_name"] or state["counterparty_name"]
            if _is_debt_increase_event(row):
                state["debt_increase_events"].append(dict(row))
            next_balance = _quantize_amount(state["balance"] + amount_delta)
            state["balance"] = next_balance

            if row["event_type"] == EVENT_SALE and amount_delta > 0:
                state["last_sale_at"] = row["external_document_date"]
            if row["event_type"] in {EVENT_PAYMENT, EVENT_SETTLEMENT} and amount_delta < 0:
                state["last_payment_at"] = row["external_document_date"]
            if row["planned_payment_date"] is not None:
                state["planned_payment_date"] = row["planned_payment_date"]
            if row["credit_depth_days"] is not None:
                state["credit_depth_days"] = row["credit_depth_days"]
            if row["shipment_ban"] is not None:
                state["shipment_ban"] = row["shipment_ban"]
            if row["manager_ref"] is not None:
                state["current_manager_ref"] = row["manager_ref"]
                state["current_manager_name"] = row["manager_name"]
    finalize_state(state)

    if strict_current_balance_overrides and current_balance_overrides:
        for override_key, override_balance in current_balance_overrides.items():
            if override_key in applied_override_keys:
                continue
            if override_balance == 0:
                continue
            session.add(
                ReceivableBalanceSnapshot(
                    snapshot_date=snapshot_date,
                    counterparty_ref=f"override:{hashlib.sha1(override_key.encode('utf-8')).hexdigest()}",
                    counterparty_name=(
                        current_balance_override_names.get(override_key)
                        if current_balance_override_names
                        else override_key
                    ),
                    current_balance=override_balance,
                    origin_event_id=None,
                    origin_document_ref=None,
                    origin_document_number=None,
                    origin_document_date=None,
                    origin_manager_ref=None,
                    origin_manager_name=None,
                    current_manager_ref=None,
                    current_manager_name=None,
                    last_sale_at=None,
                    last_payment_at=None,
                    planned_payment_date=None,
                    credit_depth_days=None,
                    shipment_ban=False,
                    payment_term_source="missing",
                    due_date=None,
                    overdue_days=None,
                    is_overdue=False,
                    aged_bucket="unknown",
                    activity_segment=ACTIVITY_INACTIVE,
                )
            )
            inserted += 1
            overrides_applied += 1
            if inserted % DEFAULT_RECEIVABLE_SYNC_BATCH_SIZE == 0:
                session.flush()
                session.expunge_all()

    if strict_employee_current_balance_overrides and employee_current_balance_overrides:
        for override_key, override_balance in employee_current_balance_overrides.items():
            if override_key in employee_override_keys_applied:
                continue
            if override_balance == 0:
                continue
            counterparty_ref = (
                employee_current_balance_override_refs.get(override_key)
                if employee_current_balance_override_refs
                else None
            )
            session.add(
                ReceivableBalanceSnapshot(
                    snapshot_date=snapshot_date,
                    counterparty_ref=counterparty_ref
                    or f"override:{hashlib.sha1(f'employee:{override_key}'.encode()).hexdigest()}",
                    counterparty_name=(
                        employee_current_balance_override_names.get(override_key)
                        if employee_current_balance_override_names
                        else override_key
                    ),
                    current_balance=override_balance,
                    origin_event_id=None,
                    origin_document_ref=None,
                    origin_document_number=None,
                    origin_document_date=None,
                    origin_manager_ref=None,
                    origin_manager_name=None,
                    current_manager_ref=None,
                    current_manager_name=None,
                    last_sale_at=None,
                    last_payment_at=None,
                    planned_payment_date=None,
                    credit_depth_days=None,
                    shipment_ban=False,
                    payment_term_source="missing",
                    due_date=None,
                    overdue_days=None,
                    is_overdue=False,
                    aged_bucket="unknown",
                    activity_segment=ACTIVITY_INACTIVE,
                )
            )
            inserted += 1
            overrides_applied += 1
            if inserted % DEFAULT_RECEIVABLE_SYNC_BATCH_SIZE == 0:
                session.flush()
                session.expunge_all()

    return {"snapshots": inserted, "overrides_applied": overrides_applied}


def build_receivable_reconciliation_snapshots(
    session: Session,
    *,
    snapshot_date: date,
    authoritative_balance_rows: Sequence[AuthoritativeReceivableBalanceRow] | None = None,
    authoritative_opening_balance_dates: Sequence[date] | None = None,
    current_balance_overrides: dict[str, Decimal] | None = None,
    current_balance_override_names: dict[str, str] | None = None,
    strict_current_balance_overrides: bool = False,
    employee_counterparty_refs: Sequence[str] = (),
    employee_current_balance_overrides: dict[str, Decimal] | None = None,
    employee_current_balance_override_names: dict[str, str] | None = None,
    employee_current_balance_override_refs: dict[str, str] | None = None,
    strict_employee_current_balance_overrides: bool = False,
) -> dict[str, int]:
    if authoritative_balance_rows is not None:
        session.execute(
            delete(ReceivableReconciliationSnapshot).where(
                ReceivableReconciliationSnapshot.snapshot_date == snapshot_date
            )
        )
        enrichment_by_counterparty = _build_receivable_ledger_enrichment_by_counterparty(
            session,
            snapshot_date=snapshot_date,
            authoritative_opening_balance_dates=authoritative_opening_balance_dates,
        )
        inserted = 0
        for row in authoritative_balance_rows:
            current_balance = _quantize_amount(row.current_balance)
            if current_balance == 0:
                continue
            enrichment = enrichment_by_counterparty.get(row.counterparty_ref, {})
            current_manager_ref = row.current_manager_ref or enrichment.get("current_manager_ref")
            current_manager_name = row.current_manager_name or enrichment.get(
                "current_manager_name"
            )
            session.add(
                ReceivableReconciliationSnapshot(
                    snapshot_date=snapshot_date,
                    counterparty_ref=row.counterparty_ref,
                    counterparty_name=row.counterparty_name or enrichment.get("counterparty_name"),
                    signed_balance=current_balance,
                    absolute_balance=_quantize_amount(abs(current_balance)),
                    current_manager_ref=current_manager_ref,
                    current_manager_name=current_manager_name,
                )
            )
            inserted += 1
            if inserted % DEFAULT_RECEIVABLE_SYNC_BATCH_SIZE == 0:
                session.flush()
                session.expunge_all()
        return {"reconciliation_snapshots": inserted, "overrides_applied": 0}

    snapshot_end = datetime.combine(snapshot_date, time.min) + timedelta(days=1)
    session.execute(
        delete(ReceivableReconciliationSnapshot).where(
            ReceivableReconciliationSnapshot.snapshot_date == snapshot_date
        )
    )

    inserted = 0
    overrides_applied = 0
    applied_override_keys: set[str] = set()
    employee_override_keys_applied: set[str] = set()
    employee_ref_set = {ref for ref in employee_counterparty_refs if ref}
    ledger_event = ReceivableLedgerEvent.__table__
    employee_duplicate = ledger_event.alias("employee_duplicate")
    stmt = (
        select(
            ledger_event.c.counterparty_ref,
            ledger_event.c.counterparty_name,
            ledger_event.c.manager_ref,
            ledger_event.c.manager_name,
            ledger_event.c.amount_delta,
        )
        .select_from(ledger_event)
        .where(ledger_event.c.external_document_date < snapshot_end)
        .where(
            ~and_(
                ledger_event.c.source_layer == "regular_receivables",
                exists(
                    select(1).where(
                        employee_duplicate.c.id != ledger_event.c.id,
                        employee_duplicate.c.counterparty_ref == ledger_event.c.counterparty_ref,
                        employee_duplicate.c.external_document_date
                        == ledger_event.c.external_document_date,
                        employee_duplicate.c.amount_delta == ledger_event.c.amount_delta,
                        employee_duplicate.c.source_layer == "employee_summary",
                        employee_duplicate.c.event_type == EVENT_DEBT_ADJUSTMENT,
                        employee_duplicate.c.external_document_date < snapshot_end,
                    )
                ),
            )
        )
        .order_by(
            ledger_event.c.counterparty_ref,
            ledger_event.c.external_document_date,
            ledger_event.c.id,
        )
        .execution_options(
            stream_results=True,
            max_row_buffer=DEFAULT_RECEIVABLE_SYNC_BATCH_SIZE,
        )
    )

    def empty_state() -> dict[str, Any]:
        return {
            "counterparty_ref": None,
            "counterparty_name": None,
            "balance": Decimal("0.00"),
            "current_manager_ref": None,
            "current_manager_name": None,
        }

    def finalize_state(state: dict[str, Any]) -> None:
        nonlocal inserted, overrides_applied
        if state["counterparty_ref"] is None:
            return

        effective_balance = state["balance"]
        override_key: str | None = None
        employee_override_key: str | None = None
        is_employee_counterparty = state["counterparty_ref"] in employee_ref_set
        if current_balance_overrides:
            override_key = _normalize_counterparty_match_key(state["counterparty_name"])
            if override_key is not None and override_key in current_balance_overrides:
                if strict_current_balance_overrides and override_key in applied_override_keys:
                    return
                effective_balance = current_balance_overrides[override_key]
                overrides_applied += 1
                applied_override_keys.add(override_key)
            elif strict_current_balance_overrides and not (
                is_employee_counterparty
                and employee_current_balance_overrides
                and override_key is not None
                and override_key in employee_current_balance_overrides
            ):
                return

        if employee_current_balance_overrides and is_employee_counterparty:
            employee_override_key = _normalize_counterparty_match_key(state["counterparty_name"])
            if (
                employee_override_key is not None
                and employee_override_key in employee_current_balance_overrides
            ):
                if (
                    strict_employee_current_balance_overrides
                    and employee_override_key in employee_override_keys_applied
                ):
                    return
                effective_balance = employee_current_balance_overrides[employee_override_key]
                overrides_applied += 1
                employee_override_keys_applied.add(employee_override_key)
            elif strict_employee_current_balance_overrides:
                return

        if effective_balance == 0:
            return

        absolute_balance = _quantize_amount(abs(effective_balance))
        session.add(
            ReceivableReconciliationSnapshot(
                snapshot_date=snapshot_date,
                counterparty_ref=state["counterparty_ref"],
                counterparty_name=state["counterparty_name"],
                signed_balance=effective_balance,
                absolute_balance=absolute_balance,
                current_manager_ref=state["current_manager_ref"],
                current_manager_name=state["current_manager_name"],
            )
        )
        inserted += 1
        if inserted % DEFAULT_RECEIVABLE_SYNC_BATCH_SIZE == 0:
            session.flush()
            session.expunge_all()

    bind = session.get_bind()
    if bind is None:
        raise RuntimeError("Database bind is not available for reconciliation snapshot build")
    dialect_name = bind.dialect.name

    if dialect_name == "postgresql":
        read_connection_cm = bind.connect().execution_options(
            stream_results=True,
            max_row_buffer=DEFAULT_RECEIVABLE_SYNC_BATCH_SIZE,
        )
    else:
        read_connection_cm = nullcontext(
            session.connection().execution_options(
                stream_results=True,
                max_row_buffer=DEFAULT_RECEIVABLE_SYNC_BATCH_SIZE,
            )
        )

    state = empty_state()
    with read_connection_cm as read_connection:
        for row in read_connection.execute(stmt).mappings():
            counterparty_ref = row["counterparty_ref"]
            amount_delta = Decimal(row["amount_delta"])

            if (
                state["counterparty_ref"] is not None
                and state["counterparty_ref"] != counterparty_ref
            ):
                finalize_state(state)
                state = empty_state()

            if state["counterparty_ref"] is None:
                state["counterparty_ref"] = counterparty_ref

            state["counterparty_name"] = row["counterparty_name"] or state["counterparty_name"]
            state["balance"] = _quantize_amount(state["balance"] + amount_delta)
            if row["manager_ref"] is not None:
                state["current_manager_ref"] = row["manager_ref"]
                state["current_manager_name"] = row["manager_name"]

    finalize_state(state)

    if strict_current_balance_overrides and current_balance_overrides:
        for override_key, override_balance in current_balance_overrides.items():
            if override_key in applied_override_keys:
                continue
            if override_balance == 0:
                continue
            absolute_balance = _quantize_amount(abs(override_balance))
            session.add(
                ReceivableReconciliationSnapshot(
                    snapshot_date=snapshot_date,
                    counterparty_ref=f"override:{hashlib.sha1(override_key.encode('utf-8')).hexdigest()}",
                    counterparty_name=(
                        current_balance_override_names.get(override_key)
                        if current_balance_override_names
                        else override_key
                    ),
                    signed_balance=override_balance,
                    absolute_balance=absolute_balance,
                    current_manager_ref=None,
                    current_manager_name=None,
                )
            )
            inserted += 1
            overrides_applied += 1
            if inserted % DEFAULT_RECEIVABLE_SYNC_BATCH_SIZE == 0:
                session.flush()
                session.expunge_all()

    if strict_employee_current_balance_overrides and employee_current_balance_overrides:
        for override_key, override_balance in employee_current_balance_overrides.items():
            if override_key in employee_override_keys_applied:
                continue
            if override_balance == 0:
                continue
            counterparty_ref = (
                employee_current_balance_override_refs.get(override_key)
                if employee_current_balance_override_refs
                else None
            )
            absolute_balance = _quantize_amount(abs(override_balance))
            session.add(
                ReceivableReconciliationSnapshot(
                    snapshot_date=snapshot_date,
                    counterparty_ref=counterparty_ref
                    or f"override:{hashlib.sha1(f'employee:{override_key}'.encode()).hexdigest()}",
                    counterparty_name=(
                        employee_current_balance_override_names.get(override_key)
                        if employee_current_balance_override_names
                        else override_key
                    ),
                    signed_balance=override_balance,
                    absolute_balance=absolute_balance,
                    current_manager_ref=None,
                    current_manager_name=None,
                )
            )
            inserted += 1
            overrides_applied += 1
            if inserted % DEFAULT_RECEIVABLE_SYNC_BATCH_SIZE == 0:
                session.flush()
                session.expunge_all()

    return {"reconciliation_snapshots": inserted, "overrides_applied": overrides_applied}


def rebuild_receivable_read_models(
    session: Session,
    *,
    snapshot_date: date,
    authoritative_balance_rows: Sequence[AuthoritativeReceivableBalanceRow] | None = None,
    authoritative_opening_balance_dates: Sequence[date] | None = None,
    current_balance_overrides: dict[str, Decimal] | None = None,
    current_balance_override_names: dict[str, str] | None = None,
    strict_current_balance_overrides: bool = False,
    employee_counterparty_refs: Sequence[str] = (),
    employee_current_balance_overrides: dict[str, Decimal] | None = None,
    employee_current_balance_override_names: dict[str, str] | None = None,
    employee_current_balance_override_refs: dict[str, str] | None = None,
    strict_employee_current_balance_overrides: bool = False,
    counterparty_departments_by_ref: (
        Mapping[str, CounterpartyDepartmentRow | Mapping[str, Any]] | None
    ) = None,
    buyer_counterparty_refs: Sequence[str] = (),
    fired_manager_refs: Sequence[str] = (),
) -> dict[str, Any]:
    assignment_result = rebuild_counterparty_manager_assignments(session)
    snapshot_result = build_receivable_balance_snapshots(
        session,
        snapshot_date=snapshot_date,
        authoritative_balance_rows=authoritative_balance_rows,
        authoritative_opening_balance_dates=authoritative_opening_balance_dates,
        current_balance_overrides=current_balance_overrides,
        current_balance_override_names=current_balance_override_names,
        strict_current_balance_overrides=strict_current_balance_overrides,
        employee_counterparty_refs=employee_counterparty_refs,
        employee_current_balance_overrides=employee_current_balance_overrides,
        employee_current_balance_override_names=employee_current_balance_override_names,
        employee_current_balance_override_refs=employee_current_balance_override_refs,
        strict_employee_current_balance_overrides=strict_employee_current_balance_overrides,
        counterparty_departments_by_ref=counterparty_departments_by_ref,
    )
    reconciliation_result = build_receivable_reconciliation_snapshots(
        session,
        snapshot_date=snapshot_date,
        authoritative_balance_rows=authoritative_balance_rows,
        authoritative_opening_balance_dates=authoritative_opening_balance_dates,
        current_balance_overrides=current_balance_overrides,
        current_balance_override_names=current_balance_override_names,
        strict_current_balance_overrides=strict_current_balance_overrides,
        employee_counterparty_refs=employee_counterparty_refs,
        employee_current_balance_overrides=employee_current_balance_overrides,
        employee_current_balance_override_names=employee_current_balance_override_names,
        employee_current_balance_override_refs=employee_current_balance_override_refs,
        strict_employee_current_balance_overrides=strict_employee_current_balance_overrides,
    )
    case_result = build_receivable_cases(
        session,
        snapshot_date=snapshot_date,
        employee_counterparty_refs=employee_counterparty_refs,
        buyer_counterparty_refs=buyer_counterparty_refs,
        fired_manager_refs=fired_manager_refs,
    )
    return {
        "assignments": assignment_result["assignments"],
        "snapshots": snapshot_result["snapshots"],
        "reconciliation_snapshots": reconciliation_result["reconciliation_snapshots"],
        "cases": case_result["cases"],
        "case_segments": case_result["segments"],
        "snapshot_overrides_applied": snapshot_result.get("overrides_applied", 0),
        "reconciliation_snapshot_overrides_applied": reconciliation_result.get(
            "overrides_applied", 0
        ),
    }


def sync_receivable_ledger(
    session: Session,
    events: Iterable[ReceivableLedgerRow],
    *,
    snapshot_date: date | None = None,
    authoritative_balance_rows: Sequence[AuthoritativeReceivableBalanceRow] | None = None,
    current_balance_overrides: dict[str, Decimal] | None = None,
    current_balance_override_names: dict[str, str] | None = None,
    strict_current_balance_overrides: bool = False,
    employee_counterparty_refs: Sequence[str] = (),
    employee_current_balance_overrides: dict[str, Decimal] | None = None,
    employee_current_balance_override_names: dict[str, str] | None = None,
    employee_current_balance_override_refs: dict[str, str] | None = None,
    strict_employee_current_balance_overrides: bool = False,
    counterparty_departments_by_ref: (
        Mapping[str, CounterpartyDepartmentRow | Mapping[str, Any]] | None
    ) = None,
    fired_manager_refs: Sequence[str] = (),
    replace_existing: bool = False,
    ingest_batch_size: int = DEFAULT_RECEIVABLE_SYNC_BATCH_SIZE,
    rebuild_read_models: bool = True,
) -> dict[str, Any]:
    if ingest_batch_size <= 0:
        raise ValueError("ingest_batch_size must be positive")

    reset_result = {
        "ledger_events_deleted": 0,
        "manager_assignments_deleted": 0,
        "snapshots_deleted": 0,
        "reconciliation_snapshots_deleted": 0,
        "cases_deleted": 0,
    }
    if replace_existing:
        bind = session.get_bind()
        dialect_name = bind.dialect.name if bind is not None else ""
        if dialect_name == "postgresql":
            reset_result["cases_deleted"] = (
                session.execute(text("SELECT COUNT(*) FROM receivable_case")).scalar() or 0
            )
            reset_result["snapshots_deleted"] = (
                session.execute(text("SELECT COUNT(*) FROM receivable_balance_snapshot")).scalar()
                or 0
            )
            reset_result["reconciliation_snapshots_deleted"] = (
                session.execute(
                    text("SELECT COUNT(*) FROM receivable_reconciliation_snapshot")
                ).scalar()
                or 0
            )
            reset_result["manager_assignments_deleted"] = (
                session.execute(
                    text("SELECT COUNT(*) FROM counterparty_manager_assignment")
                ).scalar()
                or 0
            )
            reset_result["ledger_events_deleted"] = (
                session.execute(text("SELECT COUNT(*) FROM receivable_ledger_event")).scalar() or 0
            )
            session.execute(text("""
                    TRUNCATE TABLE
                        receivable_case,
                        receivable_balance_snapshot,
                        receivable_reconciliation_snapshot,
                        counterparty_manager_assignment,
                        receivable_ledger_event
                    RESTART IDENTITY
                    """))
        else:
            reset_result["cases_deleted"] = session.execute(delete(ReceivableCase)).rowcount or 0
            reset_result["snapshots_deleted"] = (
                session.execute(delete(ReceivableBalanceSnapshot)).rowcount or 0
            )
            reset_result["reconciliation_snapshots_deleted"] = (
                session.execute(delete(ReceivableReconciliationSnapshot)).rowcount or 0
            )
            reset_result["manager_assignments_deleted"] = (
                session.execute(delete(CounterpartyManagerAssignment)).rowcount or 0
            )
            reset_result["ledger_events_deleted"] = (
                session.execute(delete(ReceivableLedgerEvent)).rowcount or 0
            )
        session.flush()

    processed = 0
    inserted = 0
    updated = 0
    existing = 0

    if isinstance(events, Sequence):
        event_batches: Iterable[list[ReceivableLedgerRow]] = [list(events)] if events else []
        should_detach_between_batches = False
    else:
        event_batches = _iter_event_batches(events, batch_size=ingest_batch_size)
        should_detach_between_batches = True

    for batch in event_batches:
        upsert_result = upsert_receivable_ledger_events(session, batch)
        processed += upsert_result["processed"]
        inserted += upsert_result["inserted"]
        updated += upsert_result["updated"]
        existing += upsert_result["existing"]
        session.flush()
        if should_detach_between_batches:
            session.expunge_all()

    assignment_result = {"assignments": 0}
    snapshot_result = {"snapshots": 0}
    case_result = {"cases": 0, "segments": {}}
    reconciliation_result = {"reconciliation_snapshots": 0}
    rebuild_result: dict[str, Any] = {}
    if rebuild_read_models:
        if snapshot_date is None:
            assignment_result = rebuild_counterparty_manager_assignments(session)
        else:
            rebuild_result = rebuild_receivable_read_models(
                session,
                snapshot_date=snapshot_date,
                authoritative_balance_rows=authoritative_balance_rows,
                current_balance_overrides=current_balance_overrides,
                current_balance_override_names=current_balance_override_names,
                strict_current_balance_overrides=strict_current_balance_overrides,
                employee_counterparty_refs=employee_counterparty_refs,
                employee_current_balance_overrides=employee_current_balance_overrides,
                employee_current_balance_override_names=employee_current_balance_override_names,
                employee_current_balance_override_refs=employee_current_balance_override_refs,
                strict_employee_current_balance_overrides=(
                    strict_employee_current_balance_overrides
                ),
                counterparty_departments_by_ref=counterparty_departments_by_ref,
                fired_manager_refs=fired_manager_refs,
            )
            assignment_result = {"assignments": rebuild_result["assignments"]}
            snapshot_result = {"snapshots": rebuild_result["snapshots"]}
            reconciliation_result = {
                "reconciliation_snapshots": rebuild_result["reconciliation_snapshots"]
            }
            case_result = {
                "cases": rebuild_result["cases"],
                "segments": rebuild_result["case_segments"],
            }
    return {
        "processed": processed,
        "inserted": inserted,
        "updated": updated,
        "existing": existing,
        "assignments": assignment_result["assignments"],
        "snapshots": snapshot_result["snapshots"],
        "reconciliation_snapshots": reconciliation_result["reconciliation_snapshots"],
        "cases": case_result["cases"],
        "case_segments": case_result["segments"],
        "snapshot_overrides_applied": rebuild_result.get(
            "snapshot_overrides_applied",
            snapshot_result.get("overrides_applied", 0),
        ),
        "reconciliation_snapshot_overrides_applied": rebuild_result.get(
            "reconciliation_snapshot_overrides_applied",
            reconciliation_result.get("overrides_applied", 0),
        ),
        "reset": reset_result,
    }


def _build_case_chain_documents(
    events: Sequence[ReceivableLedgerEvent], origin_document_date: datetime | None
) -> list[dict[str, Any]]:
    if origin_document_date is None:
        return []
    items: list[dict[str, Any]] = []
    for event in events:
        if event.external_document_date < origin_document_date:
            continue
        if event.amount_delta <= 0:
            continue
        if event.event_type == EVENT_OPENING_BALANCE:
            continue
        items.append(
            {
                "event_type": event.event_type,
                "document_ref": event.external_document_ref,
                "document_number": event.external_document_number,
                "document_date": event.external_document_date.isoformat(),
                "amount_delta": float(event.amount_delta),
            }
        )
    return items


def _case_owner_and_recommendation(
    *,
    segment: str,
) -> tuple[str, str]:
    mapping = {
        CASE_BUYERS: (
            "current_manager",
            "Взять долг в работу по обычному buyer-контуру.",
        ),
        CASE_NEW_DAILY: (
            "current_manager",
            "Проверить новый долг и установить срок первой реакции.",
        ),
        CASE_OVERDUE: (
            "current_manager",
            "Проверить просрочку и согласовать план погашения.",
        ),
        CASE_INACTIVE: ("finance", "Определить решение: взыскание, корректировка или заморозка."),
        CASE_EMPLOYEE: ("finance_hr", "Передать кейс в HR и финансы для отдельного разбора."),
        CASE_FIRED_MANAGER: (
            "finance_pool",
            "Передать кейс в финансовый пул, так как ответственный менеджер уволен.",
        ),
        CASE_ADJUSTMENT_CANDIDATE: ("finance", "Проверить кейс на корректировку долга."),
    }
    return mapping[segment]


def _load_fired_manager_names(session: Session, *, snapshot_date: date) -> set[str]:
    items = (
        session.execute(select(StaffMember).where(StaffMember.employment_status == "fired"))
        .scalars()
        .all()
    )
    result: set[str] = set()
    for item in items:
        if item.termination_date and item.termination_date > snapshot_date:
            continue
        normalized = _normalize_person_name(item.full_name)
        if normalized:
            result.add(normalized)
    return result


def _is_new_daily_case(snapshot_date: date, origin_document_date: datetime | None) -> bool:
    if origin_document_date is None:
        return False
    age_days = (snapshot_date - origin_document_date.date()).days
    # Give payment posting a 3-day grace window; raise the case only on the first day
    # after the debt becomes older than that threshold.
    return age_days == NEW_DAILY_GRACE_DAYS + 1


def _has_new_daily_balance_growth(
    current_balance: Decimal | None,
    previous_balance: Decimal | None,
) -> bool:
    current = _quantize_amount(current_balance or Decimal("0.00"))
    previous = _quantize_amount(previous_balance or Decimal("0.00"))
    # Treat prior overpayments / prepayments as zero debt for "new daily" purposes:
    # only the portion above yesterday's debt should count as a newly emerged debt.
    debt_baseline = previous if previous > 0 else Decimal("0.00")
    return current > debt_baseline


def build_receivable_cases(
    session: Session,
    *,
    snapshot_date: date,
    employee_counterparty_refs: Sequence[str] = (),
    buyer_counterparty_refs: Sequence[str] = (),
    fired_manager_refs: Sequence[str] = (),
) -> dict[str, Any]:
    snapshot_end = datetime.combine(snapshot_date, time.min) + timedelta(days=1)
    employee_refs = set(employee_counterparty_refs)
    buyer_refs = set(buyer_counterparty_refs)
    fired_refs = set(fired_manager_refs)
    fired_names = _load_fired_manager_names(session, snapshot_date=snapshot_date)
    snapshot_table = ReceivableBalanceSnapshot.__table__
    ledger_event = ReceivableLedgerEvent.__table__
    connection = session.connection()

    snapshots = (
        connection.execute(
            select(
                snapshot_table.c.counterparty_ref,
                snapshot_table.c.counterparty_name,
                snapshot_table.c.current_balance,
                snapshot_table.c.aged_bucket,
                snapshot_table.c.activity_segment,
                snapshot_table.c.origin_document_ref,
                snapshot_table.c.origin_document_number,
                snapshot_table.c.origin_document_date,
                snapshot_table.c.origin_manager_ref,
                snapshot_table.c.origin_manager_name,
                snapshot_table.c.current_manager_ref,
                snapshot_table.c.current_manager_name,
                snapshot_table.c.department_ref,
                snapshot_table.c.department_name,
                snapshot_table.c.planned_payment_date,
                snapshot_table.c.credit_depth_days,
                snapshot_table.c.shipment_ban,
                snapshot_table.c.payment_term_source,
                snapshot_table.c.due_date,
                snapshot_table.c.overdue_days,
                snapshot_table.c.is_overdue,
            )
            .select_from(snapshot_table)
            .where(
                snapshot_table.c.snapshot_date == snapshot_date,
                snapshot_table.c.current_balance != 0,
            )
            .order_by(snapshot_table.c.counterparty_ref)
        )
        .mappings()
        .all()
    )
    debt_snapshots = [item for item in snapshots if item["current_balance"] > 0]
    snapshots_by_counterparty = {item["counterparty_ref"]: item for item in debt_snapshots}
    previous_snapshot_date = snapshot_date - timedelta(days=1)
    previous_balance_by_counterparty = {
        item["counterparty_ref"]: item["current_balance"]
        for item in connection.execute(
            select(
                snapshot_table.c.counterparty_ref,
                snapshot_table.c.current_balance,
            )
            .select_from(snapshot_table)
            .where(snapshot_table.c.snapshot_date == previous_snapshot_date)
        ).mappings()
    }

    session.execute(delete(ReceivableCase).where(ReceivableCase.snapshot_date == snapshot_date))

    segment_counts: dict[str, int] = defaultdict(int)
    inserted = 0
    chain_documents_by_counterparty: dict[str, list[dict[str, Any]]] = {}

    event_stmt = (
        select(
            ledger_event.c.counterparty_ref,
            ledger_event.c.event_type,
            ledger_event.c.external_document_ref,
            ledger_event.c.external_document_number,
            ledger_event.c.external_document_date,
            ledger_event.c.amount_delta,
        )
        .select_from(ledger_event)
        .where(ledger_event.c.external_document_date < snapshot_end)
        .order_by(
            ledger_event.c.counterparty_ref,
            ledger_event.c.external_document_date,
            ledger_event.c.id,
        )
        .execution_options(
            stream_results=True,
            max_row_buffer=DEFAULT_RECEIVABLE_SYNC_BATCH_SIZE,
        )
    )

    current_counterparty_ref: str | None = None
    current_chain_documents: list[dict[str, Any]] = []
    for row in connection.execute(event_stmt).mappings():
        counterparty_ref = row["counterparty_ref"]
        if current_counterparty_ref is not None and current_counterparty_ref != counterparty_ref:
            if current_chain_documents:
                chain_documents_by_counterparty[current_counterparty_ref] = current_chain_documents
            current_chain_documents = []
        current_counterparty_ref = counterparty_ref

        snapshot = snapshots_by_counterparty.get(counterparty_ref)
        if snapshot is None:
            continue
        origin_document_date = snapshot["origin_document_date"]
        if origin_document_date is None:
            continue
        if row["external_document_date"] < origin_document_date:
            continue
        if Decimal(row["amount_delta"]) <= 0:
            continue
        if row["event_type"] == EVENT_OPENING_BALANCE:
            continue
        current_chain_documents.append(
            {
                "event_type": row["event_type"],
                "document_ref": row["external_document_ref"],
                "document_number": row["external_document_number"],
                "document_date": row["external_document_date"].isoformat(),
                "amount_delta": float(row["amount_delta"]),
            }
        )

    if current_counterparty_ref is not None and current_chain_documents:
        chain_documents_by_counterparty[current_counterparty_ref] = current_chain_documents

    for snapshot in snapshots:
        segments: list[str] = []
        is_buyer = (
            snapshot["counterparty_ref"] in buyer_refs
            if buyer_refs
            else snapshot["counterparty_ref"] not in employee_refs
        )
        is_employee = snapshot["counterparty_ref"] in employee_refs
        if is_buyer:
            segments.append(CASE_BUYERS)
        debt_case_segments = list(segments)
        if snapshot["current_balance"] > 0:
            if _is_new_daily_case(
                snapshot_date, snapshot["origin_document_date"]
            ) and _has_new_daily_balance_growth(
                snapshot["current_balance"],
                previous_balance_by_counterparty.get(snapshot["counterparty_ref"]),
            ):
                debt_case_segments.append(CASE_NEW_DAILY)
            if snapshot["is_overdue"]:
                debt_case_segments.append(CASE_OVERDUE)
            if snapshot["activity_segment"] == ACTIVITY_INACTIVE:
                debt_case_segments.append(CASE_INACTIVE)
            if is_employee:
                debt_case_segments.append(CASE_EMPLOYEE)
            if (
                snapshot["origin_manager_ref"] in fired_refs
                or snapshot["current_manager_ref"] in fired_refs
                or _normalize_person_name(snapshot["origin_manager_name"]) in fired_names
                or _normalize_person_name(snapshot["current_manager_name"]) in fired_names
            ):
                debt_case_segments.append(CASE_FIRED_MANAGER)
            if is_buyer and (
                snapshot["activity_segment"] == ACTIVITY_INACTIVE
                or snapshot["aged_bucket"] == "90+"
            ):
                debt_case_segments.append(CASE_ADJUSTMENT_CANDIDATE)

        chain_documents = chain_documents_by_counterparty.get(snapshot["counterparty_ref"], [])
        for segment in dict.fromkeys(debt_case_segments):
            owner_type, recommendation = _case_owner_and_recommendation(segment=segment)
            session.add(
                ReceivableCase(
                    snapshot_date=snapshot_date,
                    segment=segment,
                    owner_type=owner_type,
                    recommendation=recommendation,
                    counterparty_ref=snapshot["counterparty_ref"],
                    counterparty_name=snapshot["counterparty_name"],
                    current_balance=snapshot["current_balance"],
                    aged_bucket=snapshot["aged_bucket"],
                    activity_segment=snapshot["activity_segment"],
                    origin_document_ref=snapshot["origin_document_ref"],
                    origin_document_number=snapshot["origin_document_number"],
                    origin_document_date=snapshot["origin_document_date"],
                    origin_manager_ref=snapshot["origin_manager_ref"],
                    origin_manager_name=snapshot["origin_manager_name"],
                    current_manager_ref=snapshot["current_manager_ref"],
                    current_manager_name=snapshot["current_manager_name"],
                    department_ref=snapshot["department_ref"],
                    department_name=snapshot["department_name"],
                    planned_payment_date=snapshot["planned_payment_date"],
                    credit_depth_days=snapshot["credit_depth_days"],
                    shipment_ban=snapshot["shipment_ban"],
                    payment_term_source=snapshot["payment_term_source"],
                    due_date=snapshot["due_date"],
                    overdue_days=snapshot["overdue_days"],
                    is_overdue=snapshot["is_overdue"],
                    chain_documents=chain_documents,
                )
            )
            inserted += 1
            segment_counts[segment] += 1
            if inserted % DEFAULT_RECEIVABLE_SYNC_BATCH_SIZE == 0:
                session.flush()
                session.expunge_all()

    return {"cases": inserted, "segments": dict(segment_counts)}


def list_receivable_cases(
    session: Session,
    *,
    snapshot_date: date,
    segment: str | None = None,
) -> list[ReceivableCase]:
    stmt = select(ReceivableCase).where(ReceivableCase.snapshot_date == snapshot_date)
    if segment is not None:
        stmt = stmt.where(ReceivableCase.segment == segment)
    return (
        session.execute(
            stmt.order_by(
                ReceivableCase.segment,
                ReceivableCase.current_balance.desc(),
                ReceivableCase.counterparty_ref,
            )
        )
        .scalars()
        .all()
    )


def summarize_receivables_by_manager(
    session: Session,
    *,
    snapshot_date: date,
) -> list[dict[str, Any]]:
    snapshots = (
        session.execute(
            select(ReceivableBalanceSnapshot)
            .where(ReceivableBalanceSnapshot.snapshot_date == snapshot_date)
            .order_by(
                ReceivableBalanceSnapshot.current_manager_name,
                ReceivableBalanceSnapshot.counterparty_ref,
            )
        )
        .scalars()
        .all()
    )
    cases = list_receivable_cases(session, snapshot_date=snapshot_date)
    cases_by_counterparty: dict[str, list[ReceivableCase]] = defaultdict(list)
    for item in cases:
        cases_by_counterparty[item.counterparty_ref].append(item)

    grouped: dict[tuple[str | None, str | None], dict[str, Any]] = {}
    for snapshot in snapshots:
        key = (snapshot.current_manager_ref, snapshot.current_manager_name)
        if key not in grouped:
            grouped[key] = {
                "manager_ref": snapshot.current_manager_ref,
                "manager_name": snapshot.current_manager_name,
                "counterparty_count": 0,
                "total_balance": Decimal("0.00"),
                "new_daily_count": 0,
                "inactive_count": 0,
                "employee_count": 0,
                "fired_manager_count": 0,
                "adjustment_candidates_count": 0,
            }

        summary = grouped[key]
        summary["counterparty_count"] += 1
        summary["total_balance"] = _quantize_amount(
            Decimal(summary["total_balance"]) + Decimal(snapshot.current_balance)
        )

        counterparty_cases = cases_by_counterparty.get(snapshot.counterparty_ref, [])
        segments = {item.segment for item in counterparty_cases}
        summary["new_daily_count"] += 1 if CASE_NEW_DAILY in segments else 0
        summary["inactive_count"] += 1 if CASE_INACTIVE in segments else 0
        summary["employee_count"] += 1 if CASE_EMPLOYEE in segments else 0
        summary["fired_manager_count"] += 1 if CASE_FIRED_MANAGER in segments else 0
        summary["adjustment_candidates_count"] += 1 if CASE_ADJUSTMENT_CANDIDATE in segments else 0

    return sorted(
        grouped.values(),
        key=lambda item: (
            Decimal(item["total_balance"]) * Decimal("-1"),
            item["manager_name"] or "",
        ),
    )
