from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Mapping, Sequence

from sqlalchemy import text

from app.services.receivables import _build_ref_filter_clause, _hex_ref_expr, _with_nolock

CANONICAL_DEBT_HISTORY_START = date(2025, 1, 1)
CANONICAL_DEBT_SELECTION_RULE = "onec_canonical_continuous_balance_origin"
CANONICAL_DEBT_STATUS_MATCHED = "matched"
CANONICAL_DEBT_STATUS_BALANCE_MISMATCH = "canonical_balance_mismatch"
CANONICAL_DEBT_STATUS_ORIGIN_BEFORE_HISTORY = "origin_before_history"
CANONICAL_DEBT_STATUS_DOCUMENT_TOTAL_BELOW_BALANCE = "document_total_below_balance"
CANONICAL_DEBT_STATUS_UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class CanonicalDebtSaleCandidate:
    document_ref: str
    document_number: str | None
    document_date: datetime
    gross_amount: Decimal


@dataclass(frozen=True)
class CanonicalOpenDebtDocument:
    document_ref: str
    document_number: str | None
    document_date: datetime
    open_amount: Decimal
    gross_amount: Decimal
    closing_amount: Decimal


@dataclass(frozen=True)
class CanonicalDebtOriginResolution:
    status: str
    documents: tuple[CanonicalOpenDebtDocument, ...]
    opening_period: date
    opening_balance: Decimal
    computed_balance: Decimal
    current_balance: Decimal
    last_nonpositive_day: date | None


@dataclass(frozen=True)
class CanonicalDebtOriginBatch:
    supported: bool
    documents_by_counterparty: dict[str, tuple[CanonicalOpenDebtDocument, ...]]
    resolutions_by_counterparty: dict[str, CanonicalDebtOriginResolution]
    opening_period: date | None


def _ref_key(value: Any) -> str:
    return str(value or "").strip().casefold()


def _money(value: Any) -> Decimal:
    return Decimal(str(value or "0")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _coerce_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        return date.fromisoformat(value.strip()[:10])
    return None


def _coerce_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    if isinstance(value, str) and value.strip():
        return datetime.fromisoformat(value.strip())
    return None


def resolve_canonical_debt_origin(
    *,
    opening_period: date,
    opening_balance: Decimal,
    daily_movements: Mapping[date, Decimal],
    sale_candidates: Sequence[CanonicalDebtSaleCandidate],
    current_balance: Decimal,
) -> CanonicalDebtOriginResolution:
    """Resolve debt documents from a continuous full 1C mutual-settlement balance.

    The end-of-day balance deliberately ignores transient negative rows created by
    same-day reposting. Once the last stable non-positive day is found, sales are
    allocated oldest-first until their exact open amounts cover the canonical current
    balance. A disagreement of more than one kopeck is never guessed through.
    """

    opening = _money(opening_balance)
    expected = _money(current_balance)
    running = opening
    last_nonpositive_day = (
        opening_period - timedelta(days=1) if opening <= Decimal("0.00") else None
    )
    for movement_day, amount in sorted(daily_movements.items()):
        running = _money(running + _money(amount))
        if running <= Decimal("0.00"):
            last_nonpositive_day = movement_day

    if abs(running - expected) > Decimal("0.01"):
        return CanonicalDebtOriginResolution(
            status=CANONICAL_DEBT_STATUS_BALANCE_MISMATCH,
            documents=(),
            opening_period=opening_period,
            opening_balance=opening,
            computed_balance=running,
            current_balance=expected,
            last_nonpositive_day=last_nonpositive_day,
        )

    if expected <= Decimal("0.00"):
        return CanonicalDebtOriginResolution(
            status=CANONICAL_DEBT_STATUS_MATCHED,
            documents=(),
            opening_period=opening_period,
            opening_balance=opening,
            computed_balance=running,
            current_balance=expected,
            last_nonpositive_day=last_nonpositive_day,
        )

    if last_nonpositive_day is None:
        return CanonicalDebtOriginResolution(
            status=CANONICAL_DEBT_STATUS_ORIGIN_BEFORE_HISTORY,
            documents=(),
            opening_period=opening_period,
            opening_balance=opening,
            computed_balance=running,
            current_balance=expected,
            last_nonpositive_day=None,
        )

    remaining = expected
    documents: list[CanonicalOpenDebtDocument] = []
    for candidate in sorted(
        sale_candidates,
        key=lambda item: (item.document_date, item.document_ref),
    ):
        if candidate.document_date.date() <= last_nonpositive_day:
            continue
        gross_amount = _money(candidate.gross_amount)
        if gross_amount <= Decimal("0.00"):
            continue
        open_amount = min(gross_amount, remaining)
        documents.append(
            CanonicalOpenDebtDocument(
                document_ref=candidate.document_ref,
                document_number=candidate.document_number,
                document_date=candidate.document_date,
                open_amount=open_amount,
                gross_amount=gross_amount,
                closing_amount=_money(open_amount - gross_amount),
            )
        )
        remaining = _money(remaining - open_amount)
        if remaining <= Decimal("0.00"):
            break

    status = (
        CANONICAL_DEBT_STATUS_MATCHED
        if remaining <= Decimal("0.00")
        else CANONICAL_DEBT_STATUS_DOCUMENT_TOTAL_BELOW_BALANCE
    )
    return CanonicalDebtOriginResolution(
        status=status,
        documents=tuple(documents) if status == CANONICAL_DEBT_STATUS_MATCHED else (),
        opening_period=opening_period,
        opening_balance=opening,
        computed_balance=running,
        current_balance=expected,
        last_nonpositive_day=last_nonpositive_day,
    )


def _chunked(values: Sequence[str], size: int = 200):
    for index in range(0, len(values), size):
        yield list(values[index : index + size])


def fetch_canonical_open_debt_documents(
    onec_engine,
    *,
    counterparty_balances: Mapping[str, Decimal],
    snapshot_date: date,
    history_start: date = CANONICAL_DEBT_HISTORY_START,
) -> CanonicalDebtOriginBatch:
    """Read document origins from the full read-only 1C mutual-settlement register."""

    if onec_engine.dialect.name != "mssql" or snapshot_date < history_start:
        return CanonicalDebtOriginBatch(
            supported=False,
            documents_by_counterparty={},
            resolutions_by_counterparty={},
            opening_period=None,
        )

    balances = {
        _ref_key(counterparty_ref): _money(balance)
        for counterparty_ref, balance in counterparty_balances.items()
        if _ref_key(counterparty_ref)
    }
    refs = sorted(balances)
    if not refs:
        return CanonicalDebtOriginBatch(
            supported=True,
            documents_by_counterparty={},
            resolutions_by_counterparty={},
            opening_period=history_start,
        )

    nolock = _with_nolock(dialect_name="mssql")
    counterparty_ref_expr = _hex_ref_expr("source_rows.counterparty_ref", dialect_name="mssql")
    sale_counterparty_ref_expr = _hex_ref_expr("r._Fld7006RRef", dialect_name="mssql")
    sale_ref_expr = _hex_ref_expr("r._RecorderRRef", dialect_name="mssql")
    movement_end = datetime.combine(snapshot_date + timedelta(days=1), time.min)

    with onec_engine.connect() as conn:
        raw_opening_period = conn.execute(
            text(f"""
                SELECT MAX(t._Period) AS opening_period
                FROM _AccumRgT7009 AS t {nolock}
                WHERE t._Period <= :history_start
            """),
            {"history_start": datetime.combine(history_start, time.min)},
        ).scalar_one_or_none()
        opening_period = _coerce_date(raw_opening_period)
        if opening_period is None:
            return CanonicalDebtOriginBatch(
                supported=True,
                documents_by_counterparty={},
                resolutions_by_counterparty={},
                opening_period=None,
            )

        opening_by_ref: dict[str, Decimal] = {key: Decimal("0.00") for key in refs}
        daily_by_ref: dict[str, dict[date, Decimal]] = {key: defaultdict(Decimal) for key in refs}
        candidates_by_ref: dict[str, list[CanonicalDebtSaleCandidate]] = {key: [] for key in refs}

        for chunk_index, chunk in enumerate(_chunked(refs)):
            opening_filter, opening_params = _build_ref_filter_clause(
                dialect_name="mssql",
                refs=chunk,
                column_name="t._Fld7006RRef",
                prefix=f"canonical_opening_{chunk_index}",
            )
            movement_filter, movement_params = _build_ref_filter_clause(
                dialect_name="mssql",
                refs=chunk,
                column_name="r._Fld7006RRef",
                prefix=f"canonical_movement_{chunk_index}",
            )
            params: dict[str, Any] = {
                **opening_params,
                **movement_params,
                "opening_period": datetime.combine(opening_period, time.min),
                "movement_end": movement_end,
            }
            rows = conn.execute(
                text(f"""
                    SELECT
                        {counterparty_ref_expr} AS counterparty_ref,
                        source_rows.row_kind,
                        source_rows.event_day,
                        CAST(source_rows.amount AS decimal(18, 2)) AS amount
                    FROM (
                        SELECT
                            t._Fld7006RRef AS counterparty_ref,
                            'opening' AS row_kind,
                            CAST(:opening_period AS datetime) AS event_day,
                            SUM(CAST(t._Fld7008 AS decimal(18, 2))) AS amount
                        FROM _AccumRgT7009 AS t {nolock}
                        WHERE t._Period = :opening_period
                          AND {opening_filter}
                        GROUP BY t._Fld7006RRef

                        UNION ALL

                        SELECT
                            r._Fld7006RRef AS counterparty_ref,
                            'movement' AS row_kind,
                            CAST(CAST(r._Period AS date) AS datetime) AS event_day,
                            SUM(
                                CAST(
                                    CASE
                                        WHEN r._RecordKind = 0 THEN r._Fld7008
                                        ELSE -r._Fld7008
                                    END AS decimal(18, 2)
                                )
                            ) AS amount
                        FROM _AccumRg7002 AS r {nolock}
                        WHERE r._Active = 0x01
                          AND r._Period >= :opening_period
                          AND r._Period < :movement_end
                          AND {movement_filter}
                        GROUP BY r._Fld7006RRef, CAST(r._Period AS date)
                    ) AS source_rows
                    ORDER BY source_rows.counterparty_ref, source_rows.event_day
                """),
                params,
            ).mappings()
            for row in rows:
                key = _ref_key(row.get("counterparty_ref"))
                if key not in balances:
                    continue
                if row.get("row_kind") == "opening":
                    opening_by_ref[key] = _money(row.get("amount"))
                    continue
                movement_day = _coerce_date(row.get("event_day"))
                if movement_day is not None:
                    daily_by_ref[key][movement_day] += _money(row.get("amount"))

            sale_filter, sale_params = _build_ref_filter_clause(
                dialect_name="mssql",
                refs=chunk,
                column_name="r._Fld7006RRef",
                prefix=f"canonical_sales_{chunk_index}",
            )
            sale_params.update(
                {
                    "opening_period": datetime.combine(opening_period, time.min),
                    "movement_end": movement_end,
                }
            )
            sale_rows = conn.execute(
                text(f"""
                    SELECT
                        {sale_counterparty_ref_expr} AS counterparty_ref,
                        {sale_ref_expr} AS document_ref,
                        sale._Number AS document_number,
                        sale._Date_Time AS document_date,
                        SUM(
                            CAST(
                                CASE
                                    WHEN r._RecordKind = 0 THEN r._Fld7008
                                    ELSE -r._Fld7008
                                END AS decimal(18, 2)
                            )
                        ) AS gross_amount
                    FROM _AccumRg7002 AS r {nolock}
                    JOIN _Document203 AS sale {nolock}
                        ON r._RecorderTRef = 0x000000CB
                       AND sale._IDRRef = r._RecorderRRef
                    WHERE r._Active = 0x01
                      AND r._Period >= :opening_period
                      AND r._Period < :movement_end
                      AND {sale_filter}
                    GROUP BY
                        r._Fld7006RRef,
                        r._RecorderRRef,
                        sale._Number,
                        sale._Date_Time
                    HAVING SUM(
                        CAST(
                            CASE
                                WHEN r._RecordKind = 0 THEN r._Fld7008
                                ELSE -r._Fld7008
                            END AS decimal(18, 2)
                        )
                    ) > 0
                    ORDER BY r._Fld7006RRef, sale._Date_Time, r._RecorderRRef
                """),
                sale_params,
            ).mappings()
            for row in sale_rows:
                key = _ref_key(row.get("counterparty_ref"))
                document_ref = str(row.get("document_ref") or "").strip()
                document_date = _coerce_datetime(row.get("document_date"))
                if key not in balances or not document_ref or document_date is None:
                    continue
                document_number = str(row.get("document_number") or "").strip() or None
                candidates_by_ref[key].append(
                    CanonicalDebtSaleCandidate(
                        document_ref=document_ref,
                        document_number=document_number,
                        document_date=document_date,
                        gross_amount=_money(row.get("gross_amount")),
                    )
                )

    resolutions: dict[str, CanonicalDebtOriginResolution] = {}
    documents: dict[str, tuple[CanonicalOpenDebtDocument, ...]] = {}
    for key, current_balance in balances.items():
        resolution = resolve_canonical_debt_origin(
            opening_period=opening_period,
            opening_balance=opening_by_ref.get(key, Decimal("0.00")),
            daily_movements=daily_by_ref.get(key, {}),
            sale_candidates=candidates_by_ref.get(key, ()),
            current_balance=current_balance,
        )
        resolutions[key] = resolution
        documents[key] = resolution.documents

    return CanonicalDebtOriginBatch(
        supported=True,
        documents_by_counterparty=documents,
        resolutions_by_counterparty=resolutions,
        opening_period=opening_period,
    )
