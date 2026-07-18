from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from sqlalchemy import delete, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.models.executive_dashboard import ExecutiveSourceFreshness
from app.models.receivable_ledger_event import ReceivableLedgerEvent

PROFIT_LOSS_DEBT_ADJUSTMENT_SOURCE_KEY = "finance.profit_loss_debt_adjustments"
PROFIT_LOSS_DEBT_ADJUSTMENT_SOURCE_LAYER = "profit_loss_debt_adjustments"
PROFIT_LOSS_DEBT_ADJUSTMENT_SOURCE = "onec_debt_writeoff"
TARGET_ORGANIZATION_NAME = "MASTER MOBILE"

_MONEY_QUANTUM = Decimal("0.01")
_ZERO_REF = "0x00000000000000000000000000000000"


ONEC_DEBT_WRITEOFF_SQL = """
SELECT
    master.dbo.fn_varbintohexstr(doc._IDRRef) AS external_document_ref,
    RTRIM(doc._Number) AS external_document_number,
    doc._Date_Time AS external_document_date,
    CAST(line._LineNo3170 AS int) AS line_no,
    CAST(debt_type._EnumOrder AS int) AS debt_type_order,
    CAST(line._Fld3173 AS decimal(18, 2)) AS amount,
    master.dbo.fn_varbintohexstr(contract._IDRRef) AS contract_ref,
    contract._Description AS contract_name,
    master.dbo.fn_varbintohexstr(contract._Fld515RRef) AS contract_kind_ref,
    CASE master.dbo.fn_varbintohexstr(contract._Fld515RRef)
        WHEN '0x9363c6f0a10557bf4822a55db4862286' THEN N'С покупателем'
        WHEN '0x95db9a602e142ed645d7ccf13094909f' THEN N'С поставщиком'
        WHEN '0xa49b7e34b5f2cbb643d8f36270f8009f' THEN N'Прочее'
        ELSE N'Неизвестно'
    END AS contract_kind_name,
    master.dbo.fn_varbintohexstr(counterparty._IDRRef) AS counterparty_ref,
    counterparty._Description AS counterparty_name,
    master.dbo.fn_varbintohexstr(organization._IDRRef) AS organization_ref,
    organization._Description AS organization_name
FROM _Document160 AS doc WITH (NOLOCK)
JOIN _Enum276 AS operation WITH (NOLOCK)
    ON operation._IDRRef = doc._Fld3161RRef
   AND operation._EnumOrder = 2
JOIN _Reference66 AS organization WITH (NOLOCK)
    ON organization._IDRRef = doc._Fld3154RRef
   AND organization._Description = :organization_name
JOIN _Document160_VT3169 AS line WITH (NOLOCK)
    ON line._Document160_IDRRef = doc._IDRRef
LEFT JOIN _Enum258 AS debt_type WITH (NOLOCK)
    ON debt_type._IDRRef = line._Fld3177RRef
LEFT JOIN _Reference37 AS contract WITH (NOLOCK)
    ON contract._IDRRef = line._Fld3171RRef
LEFT JOIN _Reference54 AS counterparty WITH (NOLOCK)
    ON counterparty._IDRRef = contract._OwnerIDRRef
WHERE doc._Marked = 0x00
  AND doc._Posted = 0x01
  AND doc._Date_Time >= :period_start
  AND doc._Date_Time < :period_end
ORDER BY doc._Date_Time, doc._IDRRef, line._LineNo3170
"""


@dataclass(frozen=True)
class DebtWriteoffRecord:
    business_key: str
    external_document_ref: str
    external_document_number: str | None
    external_document_date: datetime
    line_no: int
    debt_kind: str
    amount_delta: Decimal
    counterparty_ref: str
    counterparty_name: str | None
    contract_ref: str
    contract_name: str | None
    contract_kind_ref: str | None
    contract_kind_name: str | None
    organization_ref: str
    organization_name: str


@dataclass(frozen=True)
class DebtWriteoffBatch:
    records: tuple[DebtWriteoffRecord, ...]
    source_line_count: int
    rejected_count: int
    rejection_reasons: dict[str, int]
    content_sha256: str

    @property
    def source_status(self) -> str:
        return "partial" if self.rejected_count else "ready"

    @property
    def income(self) -> Decimal:
        return sum(
            (record.amount_delta for record in self.records if record.amount_delta > 0),
            Decimal("0"),
        )

    @property
    def expense(self) -> Decimal:
        return sum(
            (-record.amount_delta for record in self.records if record.amount_delta < 0),
            Decimal("0"),
        )

    @property
    def document_count(self) -> int:
        return len({record.external_document_ref for record in self.records})

    def publication_payload(self, *, period_start: date, period_end: date) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "organization_name": TARGET_ORGANIZATION_NAME,
            "operation": "СписаниеЗадолженности",
            "source_document_count": self.document_count,
            "source_line_count": self.source_line_count,
            "published_event_count": len(self.records),
            "rejected_count": self.rejected_count,
            "rejection_reasons": dict(sorted(self.rejection_reasons.items())),
            "income_amount": str(self.income.quantize(_MONEY_QUANTUM)),
            "expense_amount": str(self.expense.quantize(_MONEY_QUANTUM)),
            "net_amount": str((self.income - self.expense).quantize(_MONEY_QUANTUM)),
            "content_sha256": self.content_sha256,
            "classification": {
                "debt_type_0": "other_expense",
                "debt_type_1": "other_income",
            },
        }


def fetch_onec_debt_writeoff_rows(
    onec_engine: Engine,
    *,
    period_start: date,
    period_end: date,
    organization_name: str = TARGET_ORGANIZATION_NAME,
) -> list[dict[str, Any]]:
    if period_end < period_start:
        raise ValueError("period_end must not be earlier than period_start")
    window_start = datetime.combine(period_start, time.min)
    window_end = datetime.combine(period_end + timedelta(days=1), time.min)
    with onec_engine.connect() as connection:
        rows = connection.execute(
            text(ONEC_DEBT_WRITEOFF_SQL),
            {
                "organization_name": organization_name,
                "period_start": window_start,
                "period_end": window_end,
            },
        ).mappings()
        return [dict(row) for row in rows]


def classify_debt_writeoff_rows(rows: Sequence[Mapping[str, Any]]) -> DebtWriteoffBatch:
    records: list[DebtWriteoffRecord] = []
    rejection_reasons: Counter[str] = Counter()
    seen_keys: set[str] = set()

    for row in rows:
        try:
            debt_type_order = int(row.get("debt_type_order"))
        except (TypeError, ValueError):
            rejection_reasons["unknown_debt_type"] += 1
            continue
        if debt_type_order not in {0, 1}:
            rejection_reasons["unknown_debt_type"] += 1
            continue

        amount = _money(row.get("amount"))
        if amount is None or amount <= 0:
            rejection_reasons["invalid_amount"] += 1
            continue

        external_document_ref = _required_ref(row.get("external_document_ref"))
        contract_ref = _required_ref(row.get("contract_ref"))
        counterparty_ref = _required_ref(row.get("counterparty_ref"))
        organization_ref = _required_ref(row.get("organization_ref"))
        external_document_date = _as_datetime(row.get("external_document_date"))
        line_no = _as_int(row.get("line_no"))
        if not external_document_ref or external_document_date is None or line_no is None:
            rejection_reasons["missing_document_identity"] += 1
            continue
        if not contract_ref or not counterparty_ref:
            rejection_reasons["missing_counterparty_contract"] += 1
            continue
        if not organization_ref:
            rejection_reasons["missing_organization"] += 1
            continue

        debt_kind = "receivable" if debt_type_order == 0 else "payable"
        business_key = hashlib.sha256(
            f"{PROFIT_LOSS_DEBT_ADJUSTMENT_SOURCE_LAYER}|{external_document_ref}|{line_no}|{debt_kind}".encode()
        ).hexdigest()
        if business_key in seen_keys:
            rejection_reasons["duplicate_business_key"] += 1
            continue
        seen_keys.add(business_key)

        records.append(
            DebtWriteoffRecord(
                business_key=business_key,
                external_document_ref=external_document_ref,
                external_document_number=_optional_text(row.get("external_document_number")),
                external_document_date=external_document_date,
                line_no=line_no,
                debt_kind=debt_kind,
                amount_delta=(-amount if debt_kind == "receivable" else amount),
                counterparty_ref=counterparty_ref,
                counterparty_name=_optional_text(row.get("counterparty_name")),
                contract_ref=contract_ref,
                contract_name=_optional_text(row.get("contract_name")),
                contract_kind_ref=_optional_ref(row.get("contract_kind_ref")),
                contract_kind_name=_optional_text(row.get("contract_kind_name")),
                organization_ref=organization_ref,
                organization_name=_optional_text(row.get("organization_name"))
                or TARGET_ORGANIZATION_NAME,
            )
        )

    records.sort(
        key=lambda item: (
            item.external_document_date,
            item.external_document_ref,
            item.line_no,
            item.debt_kind,
        )
    )
    content_sha256 = _records_sha256(records)
    return DebtWriteoffBatch(
        records=tuple(records),
        source_line_count=len(rows),
        rejected_count=sum(rejection_reasons.values()),
        rejection_reasons=dict(rejection_reasons),
        content_sha256=content_sha256,
    )


def publish_debt_writeoff_batch(
    session: Session,
    *,
    batch: DebtWriteoffBatch,
    period_start: date,
    period_end: date,
    source_as_of: datetime | None = None,
) -> ExecutiveSourceFreshness:
    if period_end < period_start:
        raise ValueError("period_end must not be earlier than period_start")
    window_start = datetime.combine(period_start, time.min)
    window_end = datetime.combine(period_end + timedelta(days=1), time.min)
    session.execute(
        delete(ReceivableLedgerEvent).where(
            ReceivableLedgerEvent.source_layer == PROFIT_LOSS_DEBT_ADJUSTMENT_SOURCE_LAYER,
            ReceivableLedgerEvent.external_document_date >= window_start,
            ReceivableLedgerEvent.external_document_date < window_end,
        )
    )
    for record in batch.records:
        session.add(
            ReceivableLedgerEvent(
                source=PROFIT_LOSS_DEBT_ADJUSTMENT_SOURCE,
                business_key=record.business_key,
                event_type="debt_adjustment",
                external_document_ref=record.external_document_ref,
                external_document_number=record.external_document_number,
                external_document_date=record.external_document_date,
                counterparty_ref=record.counterparty_ref,
                counterparty_name=record.counterparty_name,
                contract_ref=record.contract_ref,
                contract_name=record.contract_name,
                contract_kind_ref=record.contract_kind_ref,
                contract_kind_name=record.contract_kind_name,
                store_ref=record.organization_ref,
                store_name=record.organization_name,
                source_layer=PROFIT_LOSS_DEBT_ADJUSTMENT_SOURCE_LAYER,
                line_no=record.line_no,
                amount_delta=record.amount_delta,
            )
        )

    publication = session.scalar(
        select(ExecutiveSourceFreshness).where(
            ExecutiveSourceFreshness.source_key == PROFIT_LOSS_DEBT_ADJUSTMENT_SOURCE_KEY,
            ExecutiveSourceFreshness.business_date == period_end,
        )
    )
    observed_at = source_as_of or datetime.now(UTC).replace(tzinfo=None)
    payload = batch.publication_payload(period_start=period_start, period_end=period_end)
    if publication is None:
        publication = ExecutiveSourceFreshness(
            source_key=PROFIT_LOSS_DEBT_ADJUSTMENT_SOURCE_KEY,
            business_date=period_end,
            source_status=batch.source_status,
            source_as_of=observed_at,
            payload=payload,
        )
        session.add(publication)
    else:
        publication.source_status = batch.source_status
        publication.source_as_of = observed_at
        publication.payload = payload
    session.flush()
    return publication


def _money(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)).quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        return None


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).replace(tzinfo=None)
        except ValueError:
            return None
    return None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_text(value: Any) -> str | None:
    text_value = str(value).strip() if value is not None else ""
    return text_value or None


def _optional_ref(value: Any) -> str | None:
    ref = _optional_text(value)
    return None if ref is None or ref.lower() == _ZERO_REF else ref.lower()


def _required_ref(value: Any) -> str | None:
    return _optional_ref(value)


def _records_sha256(records: Sequence[DebtWriteoffRecord]) -> str:
    canonical = [
        {
            "document_ref": record.external_document_ref,
            "document_date": record.external_document_date.isoformat(),
            "line_no": record.line_no,
            "debt_kind": record.debt_kind,
            "amount_delta": str(record.amount_delta),
            "counterparty_ref": record.counterparty_ref,
            "contract_ref": record.contract_ref,
            "organization_ref": record.organization_ref,
        }
        for record in records
    ]
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
