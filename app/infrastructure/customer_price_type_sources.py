"""Read-only bulk source adapter for customer price-type facts."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.domains.customer_price_types import (
    ContractFact,
    CustomerPriceTypeFacts,
    proven_history_coverage_months,
)
from app.models import CounterpartyDuplicateCase, ReceivableLedgerEvent


@dataclass(frozen=True, slots=True)
class CustomerPriceTypeSourceEnrichments:
    """Optional bulk facts loaded by an application-level read-only provider."""

    economics: Mapping[str, dict[str, Any]] = field(default_factory=dict)
    payments: Mapping[str, dict[str, Any]] = field(default_factory=dict)
    return_signals: Mapping[str, dict[str, Any]] = field(default_factory=dict)
    key_account_refs: frozenset[str] = frozenset()


CustomerPriceTypeEnrichmentLoader = Callable[
    [date, Sequence[str]], CustomerPriceTypeSourceEnrichments
]

_BUYERS_CONTRACTS_SQL = text("""
    WITH buyers_groups AS (
        SELECT
            _IDRRef,
            CAST(NULL AS varbinary(16)) AS department_ref,
            CAST(NULL AS nvarchar(255)) AS department_name
        FROM _Reference54 WITH (NOLOCK)
        WHERE master.dbo.fn_varbintohexstr(_IDRRef) = :buyers_root_group_ref
        UNION ALL
        SELECT
            child._IDRRef,
            CASE
                WHEN parent.department_ref IS NULL THEN child._IDRRef
                ELSE parent.department_ref
            END AS department_ref,
            CASE
                WHEN parent.department_ref IS NULL THEN child._Description
                ELSE parent.department_name
            END AS department_name
        FROM _Reference54 AS child WITH (NOLOCK)
        JOIN buyers_groups AS parent ON child._ParentIDRRef = parent._IDRRef
        -- 1C _Reference54: group nodes have _Folder = 0x00 (elements are 0x01)
        WHERE child._Folder = 0x00 AND child._Marked = 0x00
    )
    SELECT
        master.dbo.fn_varbintohexstr(cp._IDRRef) AS counterparty_ref,
        RTRIM(cp._Code) AS counterparty_code,
        cp._Description AS counterparty_name,
        master.dbo.fn_varbintohexstr(buyers_group.department_ref) AS department_ref,
        buyers_group.department_name,
        master.dbo.fn_varbintohexstr(contract._IDRRef) AS contract_ref,
        contract._Description AS contract_name,
        price_type._Description AS price_type_name,
        CASE WHEN price_type._IDRRef IS NULL THEN 1 ELSE 0 END AS price_type_missing,
        CASE WHEN price_type._Marked = 0x01 THEN 1 ELSE 0 END AS price_type_marked
    FROM _Reference37 AS contract WITH (NOLOCK)
    JOIN _Reference54 AS cp WITH (NOLOCK)
      ON cp._IDRRef = contract._OwnerIDRRef
    LEFT JOIN _Reference87 AS price_type WITH (NOLOCK)
      ON price_type._IDRRef = contract._Fld513_RRRef
    JOIN buyers_groups AS buyers_group
      ON buyers_group._IDRRef = cp._ParentIDRRef
    WHERE contract._Marked = 0x00
      AND cp._Marked = 0x00
      AND cp._Folder = 0x01  -- element counterparty, not a group (see recursion note)
      AND master.dbo.fn_varbintohexstr(contract._Fld515RRef) = :contract_kind_ref
    OPTION (MAXRECURSION 100)
    """)

_DIRECT_MONTHLY_SQL = text("""
    WITH target_organization AS (
        SELECT _IDRRef FROM _Reference66 WITH (NOLOCK)
        WHERE _Description = N'MASTER MOBILE'
    ),
    eligible_rows AS (
        SELECT
            r._Fld7559RRef AS counterparty_ref,
            r._Period AS event_at,
            CASE
                WHEN r._RecorderTRef = 0x0000006D AND r._Fld7562 > 0
                    THEN -ABS(CAST(r._Fld7562 AS decimal(18, 2)))
                ELSE CAST(r._Fld7562 AS decimal(18, 2))
            END AS net_amount
        FROM _AccumRg7550 AS r WITH (NOLOCK)
        JOIN _Reference37 AS contract WITH (NOLOCK)
          ON contract._IDRRef = r._Fld7554RRef
        WHERE r._RecorderTRef IN (0x000000CB, 0x0000006D)
          AND r._Active = 0x01
          AND r._Fld7559RRef <> 0x00000000000000000000000000000000
          AND r._Fld7558RRef IN (SELECT _IDRRef FROM target_organization)
          AND master.dbo.fn_varbintohexstr(contract._Fld515RRef) = :contract_kind_ref
          AND r._Period < :period_end
    ),
    first_activity AS (
        SELECT counterparty_ref, MIN(event_at) AS first_activity_at
        FROM eligible_rows
        GROUP BY counterparty_ref
    )
    SELECT
        master.dbo.fn_varbintohexstr(counterparty._IDRRef) AS counterparty_ref,
        CONVERT(char(7), eligible.event_at, 120) AS month_key,
        SUM(eligible.net_amount) AS net_amount,
        first_activity.first_activity_at
    FROM eligible_rows AS eligible
    JOIN _Reference54 AS counterparty WITH (NOLOCK)
      ON counterparty._IDRRef = eligible.counterparty_ref
    JOIN first_activity
      ON first_activity.counterparty_ref = eligible.counterparty_ref
    WHERE eligible.event_at >= :period_start
    GROUP BY
        counterparty._IDRRef,
        CONVERT(char(7), eligible.event_at, 120),
        first_activity.first_activity_at
    """)

_CONTRACT_ACTIVITY_SQL = text("""
    WITH target_organization AS (
        SELECT _IDRRef FROM _Reference66 WITH (NOLOCK)
        WHERE _Description = N'MASTER MOBILE'
    ),
    sale_documents AS (
        SELECT
            r._Fld7559RRef AS counterparty_ref,
            r._Fld7554RRef AS contract_ref,
            r._RecorderRRef AS document_ref,
            MAX(r._Period) AS last_sale_at,
            SUM(CAST(r._Fld7562 AS decimal(18, 2))) AS sales_amount
        FROM _AccumRg7550 AS r WITH (NOLOCK)
        JOIN _Reference37 AS contract WITH (NOLOCK)
          ON contract._IDRRef = r._Fld7554RRef
        WHERE r._RecorderTRef = 0x000000CB
          AND r._Active = 0x01
          AND r._Fld7559RRef <> 0x00000000000000000000000000000000
          AND r._Fld7558RRef IN (SELECT _IDRRef FROM target_organization)
          AND master.dbo.fn_varbintohexstr(contract._Fld515RRef) = :contract_kind_ref
          AND r._Period >= :period_start
          AND r._Period < :period_end
        GROUP BY
            r._Fld7559RRef,
            r._Fld7554RRef,
            r._RecorderRRef
    )
    SELECT
        master.dbo.fn_varbintohexstr(counterparty_ref) AS counterparty_ref,
        master.dbo.fn_varbintohexstr(contract_ref) AS contract_ref,
        COUNT_BIG(*) AS sale_document_count_12m,
        SUM(sales_amount) AS sales_amount_12m,
        MAX(last_sale_at) AS last_sale_at
    FROM sale_documents
    GROUP BY counterparty_ref, contract_ref
    """)


def _add_months(value: date, months: int) -> date:
    total = value.year * 12 + value.month - 1 + months
    return date(total // 12, total % 12 + 1, 1)


class CustomerPriceTypeBulkSource:
    """Collect all required facts without mutating 1C or external systems."""

    def __init__(
        self,
        *,
        onec_engine: Engine,
        application_session: Session,
        buyers_root_group_ref: str,
        contract_kind_ref: str,
        key_account_price_type_prefixes: Sequence[str] = ("Key Account",),
        enrichment_loader: CustomerPriceTypeEnrichmentLoader | None = None,
    ) -> None:
        self.onec_engine = onec_engine
        self.application_session = application_session
        self.buyers_root_group_ref = buyers_root_group_ref
        self.contract_kind_ref = contract_kind_ref
        self.key_account_price_type_prefixes = tuple(key_account_price_type_prefixes)
        self.enrichment_loader = enrichment_loader

    def collect(
        self,
        *,
        snapshot_month: date,
        departments: Mapping[str, tuple[str | None, str | None]] | None = None,
        service_classes: Mapping[str, str] | None = None,
        duplicate_refs: set[str] | None = None,
        key_account_refs: set[str] | None = None,
        economics: Mapping[str, dict[str, Any]] | None = None,
        payments: Mapping[str, dict[str, Any]] | None = None,
        return_signals: Mapping[str, dict[str, Any]] | None = None,
        master_data_ready: bool = True,
    ) -> list[CustomerPriceTypeFacts]:
        period_start = _add_months(snapshot_month, -11)
        period_end = _add_months(snapshot_month, 1)
        contracts = self._contracts()
        direct, first_activity = self._direct_monthly(period_start, period_end)
        contract_activity = self._contract_activity(period_start, period_end)
        ledger, owners = self._ledger_monthly(period_start, period_end)
        loaded_enrichments = (
            self.enrichment_loader(snapshot_month, tuple(sorted(contracts)))
            if self.enrichment_loader is not None
            else CustomerPriceTypeSourceEnrichments()
        )
        if economics is None:
            economics = loaded_enrichments.economics
        if payments is None:
            payments = loaded_enrichments.payments
        if return_signals is None:
            return_signals = loaded_enrichments.return_signals
        if key_account_refs is None:
            key_account_refs = set(loaded_enrichments.key_account_refs)
        departments_by_ref = {
            str(ref).strip().lower(): value for ref, value in (departments or {}).items()
        }
        economics_by_ref = {
            str(ref).strip().lower(): value for ref, value in (economics or {}).items()
        }
        payments_by_ref = {
            str(ref).strip().lower(): value for ref, value in (payments or {}).items()
        }
        returns_by_ref = {
            str(ref).strip().lower(): value for ref, value in (return_signals or {}).items()
        }
        duplicate_ref_set = self._duplicate_refs()
        duplicate_ref_set.update(str(ref).strip().lower() for ref in (duplicate_refs or set()))
        key_account_ref_set = {str(ref).strip().lower() for ref in (key_account_refs or set())}
        result: list[CustomerPriceTypeFacts] = []
        for ref in sorted(contracts):
            item = contracts[ref]
            local_months = ledger.get(ref, {})
            direct_months = direct.get(ref, {})
            department = departments_by_ref.get(
                ref,
                (item.get("department_ref"), item.get("department_name")),
            )
            owner = owners.get(ref, (None, None))
            economics_payload = dict(economics_by_ref.get(ref, {}))
            payments_payload = dict(payments_by_ref.get(ref, {}))
            returns_payload = dict(returns_by_ref.get(ref, {}))
            first_activity_date = first_activity.get(ref)
            enriched_contracts = tuple(
                replace(
                    contract,
                    sale_document_count_12m=int(
                        contract_activity.get(str(contract.contract_ref or "").lower(), {}).get(
                            "sale_document_count_12m", 0
                        )
                    ),
                    sales_amount_12m=Decimal(
                        str(
                            contract_activity.get(str(contract.contract_ref or "").lower(), {}).get(
                                "sales_amount_12m", 0
                            )
                        )
                    ),
                    last_sale_at=contract_activity.get(
                        str(contract.contract_ref or "").lower(), {}
                    ).get("last_sale_at"),
                    is_working=int(
                        contract_activity.get(str(contract.contract_ref or "").lower(), {}).get(
                            "sale_document_count_12m", 0
                        )
                    )
                    > 0,
                )
                for contract in item["contracts"]
            )
            master_data_flags = []
            if not department[0]:
                master_data_flags.append("missing_department")
            if not owner[0]:
                master_data_flags.append("missing_owner")
            working_contracts = tuple(
                contract for contract in enriched_contracts if contract.is_working
            )
            price_type_contracts = working_contracts or enriched_contracts
            contract_is_key_account = any(
                str(contract.price_type_name or "").strip().casefold().startswith(prefix.casefold())
                for contract in price_type_contracts
                for prefix in self.key_account_price_type_prefixes
            )
            result.append(
                CustomerPriceTypeFacts(
                    counterparty_ref=ref,
                    counterparty_code=item["counterparty_code"],
                    counterparty_name=item["counterparty_name"],
                    snapshot_month=snapshot_month,
                    contracts=enriched_contracts,
                    monthly_sales=dict(direct_months),
                    source_statuses={
                        "contracts": "ready",
                        "sales_history": "ready",
                        "ledger_reconciliation": "ready",
                        "master_data": "ready" if master_data_ready else "missing",
                        "economics": "ready" if economics_payload else "missing",
                        "return_quality": "ready" if returns_payload else "missing",
                    },
                    department_ref=department[0],
                    department_name=department[1],
                    owner_ref=owner[0],
                    owner_name=owner[1],
                    service_class=(service_classes or {}).get(item["counterparty_code"] or ""),
                    duplicate_flag=ref in duplicate_ref_set,
                    key_account_flag=ref in key_account_ref_set or contract_is_key_account,
                    first_activity_date=first_activity_date,
                    history_coverage_months=proven_history_coverage_months(
                        first_activity_date, snapshot_month
                    ),
                    direct_onec_total_3m=self._window_total(direct_months, snapshot_month),
                    ledger_total_3m=self._window_total(local_months, snapshot_month),
                    economics_status=(
                        str(
                            economics_payload.get("status")
                            or economics_payload.get("source_status")
                        )
                        if economics_payload
                        else "missing"
                    ),
                    economics=economics_payload,
                    payments=payments_payload,
                    returns=returns_payload,
                    return_review_type=returns_payload.get("review_type"),
                    master_data_flags=tuple(sorted(master_data_flags)),
                )
            )
        return result

    def _contract_activity(self, period_start: date, period_end: date) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        with self.onec_engine.connect() as connection:
            rows = connection.execute(
                _CONTRACT_ACTIVITY_SQL,
                {
                    "contract_kind_ref": self.contract_kind_ref,
                    "period_start": datetime.combine(period_start, datetime.min.time()),
                    "period_end": datetime.combine(period_end, datetime.min.time()),
                },
            ).mappings()
            for row in rows:
                contract_ref = str(row.get("contract_ref") or "").strip().lower()
                if not contract_ref:
                    continue
                last_sale_at = row.get("last_sale_at")
                result[contract_ref] = {
                    "counterparty_ref": str(row.get("counterparty_ref") or "").strip().lower()
                    or None,
                    "sale_document_count_12m": int(row.get("sale_document_count_12m") or 0),
                    "sales_amount_12m": Decimal(str(row.get("sales_amount_12m") or 0)),
                    "last_sale_at": (
                        last_sale_at.date() if isinstance(last_sale_at, datetime) else last_sale_at
                    ),
                }
        return result

    def _contracts(self) -> dict[str, dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        with self.onec_engine.connect() as connection:
            rows = connection.execute(
                _BUYERS_CONTRACTS_SQL,
                {
                    "buyers_root_group_ref": self.buyers_root_group_ref,
                    "contract_kind_ref": self.contract_kind_ref,
                },
            ).mappings()
            for row in rows:
                ref = str(row.get("counterparty_ref") or "").strip().lower()
                if not ref:
                    continue
                item = grouped.setdefault(
                    ref,
                    {
                        "counterparty_code": str(row.get("counterparty_code") or "").strip()
                        or None,
                        "counterparty_name": str(row.get("counterparty_name") or "").strip()
                        or None,
                        "department_ref": str(row.get("department_ref") or "").strip().lower()
                        or None,
                        "department_name": str(row.get("department_name") or "").strip() or None,
                        "contracts": [],
                    },
                )
                item["contracts"].append(
                    ContractFact(
                        contract_ref=str(row.get("contract_ref") or "").strip() or None,
                        contract_name=str(row.get("contract_name") or "").strip() or None,
                        price_type_name=str(row.get("price_type_name") or "").strip() or None,
                        price_type_marked=bool(row.get("price_type_marked")),
                        price_type_missing=bool(row.get("price_type_missing")),
                    )
                )
        return grouped

    def _direct_monthly(
        self, period_start: date, period_end: date
    ) -> tuple[dict[str, dict[str, Decimal]], dict[str, date]]:
        result: dict[str, dict[str, Decimal]] = defaultdict(dict)
        first_activity: dict[str, date] = {}
        with self.onec_engine.connect() as connection:
            rows = connection.execute(
                _DIRECT_MONTHLY_SQL,
                {
                    "contract_kind_ref": self.contract_kind_ref,
                    "period_start": datetime.combine(period_start, datetime.min.time()),
                    "period_end": datetime.combine(period_end, datetime.min.time()),
                },
            ).mappings()
            for row in rows:
                ref = str(row.get("counterparty_ref") or "").strip().lower()
                month = str(row.get("month_key") or "").strip()
                if ref and month:
                    result[ref][month] = Decimal(str(row.get("net_amount") or 0))
                    first_at = row.get("first_activity_at")
                    if first_at is not None:
                        first_activity[ref] = (
                            first_at.date() if isinstance(first_at, datetime) else first_at
                        )
        return result, first_activity

    def _ledger_monthly(self, period_start: date, period_end: date) -> tuple[
        dict[str, dict[str, Decimal]],
        dict[str, tuple[str | None, str | None]],
    ]:
        dialect = (
            self.application_session.bind.dialect.name if self.application_session.bind else ""
        )
        month_expr = (
            func.strftime("%Y-%m", ReceivableLedgerEvent.external_document_date)
            if dialect == "sqlite"
            else func.to_char(ReceivableLedgerEvent.external_document_date, "YYYY-MM")
        )
        rows = self.application_session.execute(
            select(
                ReceivableLedgerEvent.counterparty_ref,
                month_expr.label("month_key"),
                ReceivableLedgerEvent.event_type,
                func.sum(ReceivableLedgerEvent.amount_delta).label("amount"),
            )
            .where(
                ReceivableLedgerEvent.external_document_date
                >= datetime.combine(period_start, datetime.min.time()),
                ReceivableLedgerEvent.external_document_date
                < datetime.combine(period_end, datetime.min.time()),
                ReceivableLedgerEvent.event_type.in_(("sale", "return")),
                ReceivableLedgerEvent.source_layer == "regular_receivables",
            )
            .group_by(
                ReceivableLedgerEvent.counterparty_ref,
                month_expr,
                ReceivableLedgerEvent.event_type,
            )
        ).all()
        raw: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
        for row in rows:
            ref = str(row.counterparty_ref or "").strip().lower()
            month = str(row.month_key or "").strip()
            amount = Decimal(str(row.amount or 0))
            if row.event_type == "return":
                amount = -abs(amount)
            raw[(ref, month)] += amount
        monthly: dict[str, dict[str, Decimal]] = defaultdict(dict)
        for (ref, month), amount in raw.items():
            monthly[ref][month] = amount
        ranked_owners = (
            select(
                ReceivableLedgerEvent.counterparty_ref.label("counterparty_ref"),
                ReceivableLedgerEvent.manager_ref.label("manager_ref"),
                ReceivableLedgerEvent.manager_name.label("manager_name"),
                func.row_number()
                .over(
                    partition_by=ReceivableLedgerEvent.counterparty_ref,
                    order_by=(
                        ReceivableLedgerEvent.external_document_date.desc(),
                        ReceivableLedgerEvent.id.desc(),
                    ),
                )
                .label("position"),
            )
            .where(
                ReceivableLedgerEvent.external_document_date
                < datetime.combine(period_end, datetime.min.time()),
                ReceivableLedgerEvent.event_type.in_(("sale", "return")),
                ReceivableLedgerEvent.source_layer == "regular_receivables",
                ReceivableLedgerEvent.contract_kind_name == "С покупателем",
            )
            .subquery()
        )
        owner_rows = self.application_session.execute(
            select(
                ranked_owners.c.counterparty_ref,
                ranked_owners.c.manager_ref,
                ranked_owners.c.manager_name,
            ).where(ranked_owners.c.position == 1)
        ).all()
        owners = {
            str(row.counterparty_ref or "")
            .strip()
            .lower(): (
                row.manager_ref,
                row.manager_name,
            )
            for row in owner_rows
            if row.counterparty_ref
        }
        return monthly, owners

    def _duplicate_refs(self) -> set[str]:
        rows = self.application_session.scalars(
            select(CounterpartyDuplicateCase).where(
                CounterpartyDuplicateCase.status.in_(("new", "in_progress", "confirmed_duplicate"))
            )
        ).all()
        refs: set[str] = set()
        for row in rows:
            for candidate in row.candidate_records or []:
                ref = str(candidate.get("counterparty_ref") or "").strip().lower()
                if ref:
                    refs.add(ref)
        return refs

    @staticmethod
    def _window_total(values: Mapping[str, Decimal], snapshot_month: date) -> Decimal:
        keys = [_add_months(snapshot_month, delta).strftime("%Y-%m") for delta in (-2, -1, 0)]
        return sum((Decimal(str(values.get(key, 0))) for key in keys), Decimal("0"))
