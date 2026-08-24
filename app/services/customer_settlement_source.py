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
    normalize_guid,
    normalize_money,
    onec_guid_to_ref,
    onec_ref_to_guid,
)

SOURCE_MODE = "onec_canonical_mutual_statement_7002"
_ORGANIZATION_FIELD_RE = re.compile(r"^_Fld[0-9]+RRef$")
_COUNTERPARTY_FIELD_RE = re.compile(r"^_Fld[0-9]+$")


class CustomerSettlementSourceError(RuntimeError):
    pass


@dataclass(frozen=True)
class CustomerSettlementSourceResult:
    source_db_time: datetime
    as_of: datetime
    balances: tuple[SettlementBalanceInput, ...]
    isolation_level: str
    duration_seconds: float


@dataclass(frozen=True)
class ManualCustomerSettlementControl:
    counterparty_ref: str
    counterparty_guid: str
    counterparty_code: str
    counterparty_name: str
    counterparty_inn: str
    active_contract_currency_codes: tuple[str, ...]


def validate_organization_field(value: str | None) -> str:
    normalized = str(value or "").strip()
    if not _ORGANIZATION_FIELD_RE.fullmatch(normalized):
        raise CustomerSettlementSourceError("organization_dimension_not_configured")
    return normalized


def validate_counterparty_control_field(value: str | None) -> str:
    normalized = str(value or "").strip()
    if not _COUNTERPARTY_FIELD_RE.fullmatch(normalized):
        raise CustomerSettlementSourceError("counterparty_control_field_not_configured")
    return normalized


def _validate_query_timeout(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 30:
        raise CustomerSettlementSourceError("query_timeout_must_be_between_1_and_30_seconds")
    return value


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
    organization_guid: str | None = None,
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
    normalized_organization_guid = normalize_guid(
        organization_guid or onec_ref_to_guid(organization)
    )
    if onec_guid_to_ref(normalized_organization_guid) != organization:
        raise CustomerSettlementSourceError("organization_guid_does_not_match_ref")
    normalized_refs = tuple(
        sorted({normalize_counterparty_ref(value) for value in counterparty_refs})
    )
    if not normalized_refs:
        raise CustomerSettlementSourceError("pilot_counterparty_list_is_empty")
    if len(normalized_refs) > 100:
        raise CustomerSettlementSourceError("pilot_counterparty_limit_exceeded")
    query_timeout_seconds = _validate_query_timeout(query_timeout_seconds)

    started = time.monotonic()
    with onec_engine.connect() as connection:
        isolation_probe = _clock_row(connection)
        connection.rollback()
        isolation_level = (
            "SNAPSHOT"
            if int(isolation_probe.get("snapshot_isolation_state") or 0) == 1
            else "READ COMMITTED"
        )
        connection.exec_driver_sql(f"SET TRANSACTION ISOLATION LEVEL {isolation_level}")
        connection.rollback()

        statement = text(f"""
            WITH
            latest_opening_period AS (
                SELECT MAX(t._Period) AS period
                FROM _AccumRgT7009 AS t
                JOIN #CustomerSettlementPilot AS pilot
                  ON pilot.counterparty_ref = t._Fld7006RRef
                WHERE t._Period <= :opening_cutoff
                  AND t.{opening_org_field} = CONVERT(
                      binary(16),
                      CONVERT(varchar(34), :organization_ref),
                      1
                  )
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
                WHERE t.{opening_org_field} = CONVERT(
                    binary(16),
                    CONVERT(varchar(34), :organization_ref),
                    1
                )
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
                  AND r.{movement_org_field} = CONVERT(
                      binary(16),
                      CONVERT(varchar(34), :organization_ref),
                      1
                  )
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
                CASE
                    WHEN counterparty._IDRRef IS NOT NULL
                     AND counterparty._Folder = 0x01
                    THEN 1 ELSE 0
                END AS counterparty_exists,
                CASE WHEN counterparty._Marked = 0x01 THEN 1 ELSE 0 END AS marked_deleted
            FROM #CustomerSettlementPilot AS pilot
            LEFT JOIN balances
              ON balances.counterparty_rref = pilot.counterparty_ref
            LEFT JOIN _Reference54 AS counterparty
              ON counterparty._IDRRef = pilot.counterparty_ref
            ORDER BY pilot.ref_text
            """)
        with connection.begin():
            clock = _clock_row(connection)
            source_db_time = ensure_utc(clock["utc_now"])
            source_local_time = source_db_time.astimezone(ZoneInfo(onec_timezone)).replace(
                tzinfo=None
            )
            if as_of is None:
                query_as_of = source_local_time
                response_as_of = source_db_time
            else:
                response_as_of = ensure_utc(as_of)
                query_as_of = response_as_of.astimezone(ZoneInfo(onec_timezone)).replace(
                    tzinfo=None
                )
                if response_as_of > source_db_time:
                    raise CustomerSettlementSourceError("as_of_cannot_be_in_the_future")
            opening_cutoff = datetime(query_as_of.year, query_as_of.month, 1)
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
                    VALUES (
                        CONVERT(
                            binary(16),
                            CONVERT(varchar(34), :counterparty_ref),
                            1
                        ),
                        :counterparty_ref
                    )
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
            counterparty_guid=onec_ref_to_guid(str(row["counterparty_ref"])),
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


def fetch_manual_customer_settlement_controls(
    onec_engine: Engine,
    *,
    organization_ref: str,
    organization_guid: str,
    counterparty_guids: Sequence[str],
    counterparty_inn_field: str,
    query_timeout_seconds: int,
) -> tuple[ManualCustomerSettlementControl, ...]:
    """Read and validate identity controls for a small manually approved pilot batch."""

    if onec_engine.dialect.name != "mssql":
        raise CustomerSettlementSourceError("customer settlement source requires MSSQL")
    organization = normalize_counterparty_ref(organization_ref)
    normalized_organization_guid = normalize_guid(organization_guid)
    if onec_guid_to_ref(normalized_organization_guid) != organization:
        raise CustomerSettlementSourceError("organization_guid_does_not_match_ref")
    inn_field = validate_counterparty_control_field(counterparty_inn_field)
    normalized_guids = tuple(sorted({normalize_guid(value) for value in counterparty_guids}))
    if not normalized_guids:
        raise CustomerSettlementSourceError("manual_mapping_batch_is_empty")
    if len(normalized_guids) > 10:
        raise CustomerSettlementSourceError("manual_mapping_batch_limit_exceeded")
    query_timeout_seconds = _validate_query_timeout(query_timeout_seconds)
    refs_by_guid = {guid: onec_guid_to_ref(guid) for guid in normalized_guids}

    counterparty_statement = text(f"""
        SELECT
            pilot.ref_text AS counterparty_ref,
            RTRIM(counterparty._Code) AS counterparty_code,
            RTRIM(counterparty._Description) AS counterparty_name,
            RTRIM(CAST(counterparty.{inn_field} AS nvarchar(64))) AS counterparty_inn,
            CASE WHEN counterparty._Marked = 0x01 THEN 1 ELSE 0 END AS marked_deleted,
            -- In this UT 10.3 database, 0x01 marks elements and 0x00 folders.
            CASE WHEN counterparty._Folder = 0x01 THEN 1 ELSE 0 END AS is_element
        FROM #CustomerSettlementManualPilot AS pilot
        LEFT JOIN dbo._Reference54 AS counterparty
          ON counterparty._IDRRef = pilot.counterparty_ref
        ORDER BY pilot.ref_text
        """)
    currency_statement = text("""
        SELECT
            pilot.ref_text AS counterparty_ref,
            RTRIM(currency._Code) AS currency_code
        FROM #CustomerSettlementManualPilot AS pilot
        JOIN dbo._Reference37 AS contract
          ON contract._OwnerIDRRef = pilot.counterparty_ref
         AND contract._Marked = 0x00
        LEFT JOIN dbo._Reference20 AS currency
          ON currency._IDRRef = contract._Fld498RRef
        GROUP BY pilot.ref_text, currency._Code
        ORDER BY pilot.ref_text, currency._Code
        """)
    organization_statement = text("""
        SELECT TOP 2
            CASE WHEN organization._Marked = 0x01 THEN 1 ELSE 0 END AS marked_deleted
        FROM dbo._Reference66 AS organization
        WHERE organization._IDRRef = CONVERT(
            binary(16),
            CONVERT(varchar(34), :organization_ref),
            1
        )
        """)

    started = time.monotonic()
    with onec_engine.connect() as connection:
        clock = _clock_row(connection)
        connection.rollback()
        isolation_level = (
            "SNAPSHOT" if int(clock.get("snapshot_isolation_state") or 0) == 1 else "READ COMMITTED"
        )
        connection.exec_driver_sql(f"SET TRANSACTION ISOLATION LEVEL {isolation_level}")
        connection.rollback()
        with connection.begin():
            connection.exec_driver_sql(f"SET LOCK_TIMEOUT {query_timeout_seconds * 1000}")
            connection.exec_driver_sql("""
                CREATE TABLE #CustomerSettlementManualPilot (
                    counterparty_ref binary(16) NOT NULL PRIMARY KEY,
                    ref_text varchar(34) NOT NULL UNIQUE
                )
                """)
            connection.execute(
                text("""
                    INSERT INTO #CustomerSettlementManualPilot (counterparty_ref, ref_text)
                    VALUES (
                        CONVERT(
                            binary(16),
                            CONVERT(varchar(34), :counterparty_ref),
                            1
                        ),
                        :counterparty_ref
                    )
                    """),
                [{"counterparty_ref": refs_by_guid[guid]} for guid in normalized_guids],
            )
            organization_rows = tuple(
                connection.execute(
                    organization_statement,
                    {"organization_ref": organization},
                ).mappings()
            )
            counterparty_rows = tuple(connection.execute(counterparty_statement).mappings())
            currency_rows = tuple(connection.execute(currency_statement).mappings())

    if time.monotonic() - started > query_timeout_seconds:
        raise CustomerSettlementSourceError("customer_settlement_query_timeout")
    if len(organization_rows) != 1 or bool(organization_rows[0]["marked_deleted"]):
        raise CustomerSettlementSourceError("organization_not_found_or_inactive")
    if len(counterparty_rows) != len(normalized_guids):
        raise CustomerSettlementSourceError("incomplete_manual_mapping_controls")

    currencies_by_ref: dict[str, set[str]] = {}
    for row in currency_rows:
        counterparty_ref = normalize_counterparty_ref(str(row["counterparty_ref"]))
        currencies_by_ref.setdefault(counterparty_ref, set()).add(
            str(row.get("currency_code") or "").strip()
        )

    controls: list[ManualCustomerSettlementControl] = []
    for row in counterparty_rows:
        counterparty_ref = normalize_counterparty_ref(str(row["counterparty_ref"]))
        if (
            not row.get("counterparty_code")
            or not row.get("counterparty_name")
            or bool(row["marked_deleted"])
            or not bool(row["is_element"])
        ):
            raise CustomerSettlementSourceError("counterparty_not_found_or_inactive")
        currency_codes = tuple(sorted(currencies_by_ref.get(counterparty_ref, set())))
        if any(code != "643" for code in currency_codes):
            raise CustomerSettlementSourceError("counterparty_has_non_rub_contract")
        controls.append(
            ManualCustomerSettlementControl(
                counterparty_ref=counterparty_ref,
                counterparty_guid=onec_ref_to_guid(counterparty_ref),
                counterparty_code=str(row["counterparty_code"]).strip(),
                counterparty_name=str(row["counterparty_name"]).strip(),
                counterparty_inn=str(row.get("counterparty_inn") or "").strip(),
                active_contract_currency_codes=currency_codes,
            )
        )
    if {item.counterparty_guid for item in controls} != set(normalized_guids):
        raise CustomerSettlementSourceError("manual_mapping_guid_readback_mismatch")
    return tuple(sorted(controls, key=lambda item: item.counterparty_guid))
