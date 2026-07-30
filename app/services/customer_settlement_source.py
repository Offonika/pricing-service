from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Sequence
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.services.customer_settlements import (
    SettlementBalanceInput,
    ensure_utc,
    normalize_counterparty_ref,
    normalize_money,
)

SOURCE_MODE = "onec_canonical_mutual_statement_7002"
_ORGANIZATION_FIELD_RE = re.compile(r"^_Fld[0-9]+RRef$")


class CustomerSettlementSourceError(RuntimeError):
    pass


@dataclass(frozen=True)
class CustomerSettlementSourceResult:
    source_db_time: datetime
    as_of: datetime
    balances: tuple[SettlementBalanceInput, ...]
    isolation_level: str
    duration_seconds: float


def validate_organization_field(value: str | None) -> str:
    normalized = str(value or "").strip()
    if not _ORGANIZATION_FIELD_RE.fullmatch(normalized):
        raise CustomerSettlementSourceError("organization_dimension_not_configured")
    return normalized


def _clock_row(connection) -> dict:
    row = connection.execute(text("""
            SELECT
                SYSUTCDATETIME() AS utc_now,
                SYSDATETIME() AS local_now,
                snapshot_isolation_state
            FROM sys.databases
            WHERE name = DB_NAME()
            """)).mappings().one()
    return dict(row)


def fetch_customer_settlement_balances(
    onec_engine: Engine,
    *,
    organization_ref: str,
    opening_organization_field: str,
    movement_organization_field: str,
    counterparty_refs: Sequence[str],
    query_timeout_seconds: int,
    onec_timezone: str = "Europe/Moscow",
    as_of: datetime | None = None,
) -> CustomerSettlementSourceResult:
    if onec_engine.dialect.name != "mssql":
        raise CustomerSettlementSourceError("customer settlement source requires MSSQL")
    opening_org_field = validate_organization_field(opening_organization_field)
    movement_org_field = validate_organization_field(movement_organization_field)
    organization = normalize_counterparty_ref(organization_ref)
    normalized_refs = tuple(
        sorted({normalize_counterparty_ref(value) for value in counterparty_refs})
    )
    if not normalized_refs:
        raise CustomerSettlementSourceError("pilot_counterparty_list_is_empty")
    if len(normalized_refs) > 100:
        raise CustomerSettlementSourceError("pilot_counterparty_limit_exceeded")
    if query_timeout_seconds < 1 or query_timeout_seconds > 30:
        raise CustomerSettlementSourceError("query_timeout_must_be_between_1_and_30_seconds")

    started = time.monotonic()
    with onec_engine.connect() as connection:
        clock = _clock_row(connection)
        connection.rollback()
        isolation_level = (
            "SNAPSHOT" if int(clock.get("snapshot_isolation_state") or 0) == 1 else "READ COMMITTED"
        )
        connection = connection.execution_options(isolation_level=isolation_level)
        source_db_time = ensure_utc(clock["utc_now"])
        source_local_time = clock["local_now"]
        if isinstance(source_local_time, datetime) and source_local_time.tzinfo is not None:
            source_local_time = source_local_time.replace(tzinfo=None)
        if as_of is None:
            query_as_of = source_local_time
            response_as_of = source_db_time
        else:
            response_as_of = ensure_utc(as_of)
            query_as_of = response_as_of.astimezone(ZoneInfo(onec_timezone)).replace(tzinfo=None)
            if response_as_of > source_db_time:
                raise CustomerSettlementSourceError("as_of_cannot_be_in_the_future")
        opening_cutoff = datetime(query_as_of.year, query_as_of.month, 1)

        statement = text(f"""
            WITH
            latest_opening_period AS (
                SELECT MAX(t._Period) AS period
                FROM _AccumRgT7009 AS t
                JOIN #CustomerSettlementPilot AS pilot
                  ON pilot.counterparty_ref = t._Fld7006RRef
                WHERE t._Period <= :opening_cutoff
                  AND t.{opening_org_field} = CONVERT(binary(16), :organization_ref, 1)
            ),
            opening_rows AS (
                SELECT
                    t._Fld7006RRef AS counterparty_rref,
                    SUM(CAST(t._Fld7008 AS decimal(18, 2))) AS amount
                FROM _AccumRgT7009 AS t
                JOIN #CustomerSettlementPilot AS pilot
                  ON pilot.counterparty_ref = t._Fld7006RRef
                JOIN latest_opening_period AS p
                  ON t._Period = p.period
                WHERE t.{opening_org_field} = CONVERT(binary(16), :organization_ref, 1)
                GROUP BY t._Fld7006RRef
            ),
            movement_rows AS (
                SELECT
                    r._Fld7006RRef AS counterparty_rref,
                    SUM(
                        CAST(
                            CASE
                                WHEN r._RecordKind = 0 THEN r._Fld7008
                                ELSE -r._Fld7008
                            END AS decimal(18, 2)
                        )
                    ) AS amount
                FROM _AccumRg7002 AS r
                JOIN #CustomerSettlementPilot AS pilot
                  ON pilot.counterparty_ref = r._Fld7006RRef
                WHERE r._Active = 0x01
                  AND r.{movement_org_field} = CONVERT(binary(16), :organization_ref, 1)
                  AND r._Period >= :opening_cutoff
                  AND r._Period < :movement_end
                GROUP BY r._Fld7006RRef
            ),
            balances AS (
                SELECT
                    source_rows.counterparty_rref,
                    SUM(source_rows.amount) AS signed_balance
                FROM (
                    SELECT counterparty_rref, amount FROM opening_rows
                    UNION ALL
                    SELECT counterparty_rref, amount FROM movement_rows
                ) AS source_rows
                GROUP BY source_rows.counterparty_rref
            )
            SELECT
                pilot.ref_text AS counterparty_ref,
                CAST(COALESCE(balances.signed_balance, 0) AS decimal(18, 2))
                    AS signed_balance,
                CASE WHEN counterparty._IDRRef IS NULL THEN 0 ELSE 1 END AS counterparty_exists,
                CASE WHEN counterparty._Marked = 0x01 THEN 1 ELSE 0 END AS marked_deleted
            FROM #CustomerSettlementPilot AS pilot
            LEFT JOIN balances
              ON balances.counterparty_rref = pilot.counterparty_ref
            LEFT JOIN _Reference54 AS counterparty
              ON counterparty._IDRRef = pilot.counterparty_ref
            ORDER BY pilot.ref_text
            """)
        with connection.begin():
            connection.exec_driver_sql(f"SET LOCK_TIMEOUT {query_timeout_seconds * 1000}")
            connection.exec_driver_sql("""
                CREATE TABLE #CustomerSettlementPilot (
                    counterparty_ref binary(16) NOT NULL PRIMARY KEY,
                    ref_text varchar(34) NOT NULL UNIQUE
                )
                """)
            connection.execute(
                text("""
                    INSERT INTO #CustomerSettlementPilot (counterparty_ref, ref_text)
                    VALUES (CONVERT(binary(16), :counterparty_ref, 1), :counterparty_ref)
                    """),
                [{"counterparty_ref": value} for value in normalized_refs],
            )
            raw_rows = tuple(
                connection.execute(
                    statement,
                    {
                        "opening_cutoff": opening_cutoff,
                        "movement_end": query_as_of,
                        "organization_ref": organization,
                    },
                ).mappings()
            )

    duration_seconds = time.monotonic() - started
    if duration_seconds > query_timeout_seconds:
        raise CustomerSettlementSourceError("customer_settlement_query_timeout")
    if len(raw_rows) != len(normalized_refs):
        raise CustomerSettlementSourceError("incomplete_customer_settlement_source")
    balances = tuple(
        SettlementBalanceInput(
            counterparty_ref=str(row["counterparty_ref"]),
            signed_balance=normalize_money(Decimal(row["signed_balance"])),
            currency="RUB",
            exists=bool(row["counterparty_exists"]),
            marked_deleted=bool(row["marked_deleted"]),
        )
        for row in raw_rows
    )
    return CustomerSettlementSourceResult(
        source_db_time=source_db_time,
        as_of=response_as_of,
        balances=balances,
        isolation_level=isolation_level,
        duration_seconds=duration_seconds,
    )
