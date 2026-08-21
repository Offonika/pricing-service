"""Read-only 1C nomenclature fields shared by procurement calculations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import bindparam, text

from app.services.query_batching import normalized_text_batches


def fetch_onec_nomenclature_by_codes(
    engine: Any,
    *,
    codes: Sequence[object],
) -> dict[str, dict[str, Any]]:
    """Return one stable snapshot, including the card's main supplier."""

    batches = normalized_text_batches(codes)
    if not batches:
        return {}
    query = text("""
        SELECT
            CONVERT(varchar(34), item._IDRRef, 1) AS nomenclature_ref,
            NULLIF(LTRIM(RTRIM(item._Code)), N'') AS nomenclature_code,
            NULLIF(LTRIM(RTRIM(item._Description)), N'') AS nomenclature_name,
            NULLIF(LTRIM(RTRIM(CAST(item._Fld836 AS nvarchar(max)))), N'') AS article,
            CONVERT(
                varchar(34),
                NULLIF(item._Fld851RRef, 0x00000000000000000000000000000000),
                1
            ) AS main_supplier_ref,
            NULLIF(LTRIM(RTRIM(main_supplier._Code)), N'') AS main_supplier_code,
            NULLIF(LTRIM(RTRIM(main_supplier._Description)), N'') AS main_supplier_name
        FROM dbo._Reference62 AS item WITH (NOLOCK)
        LEFT JOIN dbo._Reference54 AS main_supplier WITH (NOLOCK)
            ON main_supplier._IDRRef = item._Fld851RRef
        WHERE item._Marked = 0x00
          AND LTRIM(RTRIM(item._Code)) IN :codes
    """).bindparams(bindparam("codes", expanding=True))
    result: dict[str, dict[str, Any]] = {}
    with engine.connect() as connection:
        for batch in batches:
            rows = connection.execute(query, {"codes": batch}).mappings()
            for row in rows:
                payload = dict(row)
                code = _clean(payload.get("nomenclature_code"))
                if code:
                    result[code] = payload
    return result


def main_supplier_payload(source: Mapping[str, Any]) -> dict[str, str] | None:
    """Return a normalized main-supplier payload when the card field is set."""

    ref = _clean(source.get("main_supplier_ref"))
    if not ref:
        return None
    return {
        "ref": ref,
        "code": _clean(source.get("main_supplier_code")),
        "name": _clean(source.get("main_supplier_name")),
    }


def search_onec_suppliers(
    engine: Any,
    *,
    query: str,
    limit: int = 20,
) -> list[dict[str, str]]:
    """Search active 1C counterparties for the in-app supplier picker."""

    clean_query = _clean(query)
    if len(clean_query) < 2:
        return []
    safe_limit = max(1, min(int(limit), 50))
    statement = text(f"""
        SELECT TOP ({safe_limit})
            CONVERT(varchar(34), supplier._IDRRef, 1) AS supplier_ref,
            NULLIF(LTRIM(RTRIM(supplier._Code)), N'') AS supplier_code,
            NULLIF(LTRIM(RTRIM(supplier._Description)), N'') AS supplier_name
        FROM dbo._Reference54 AS supplier WITH (NOLOCK)
        WHERE supplier._Marked = 0x00
          AND (
              LTRIM(RTRIM(supplier._Code)) LIKE :pattern
              OR LTRIM(RTRIM(supplier._Description)) LIKE :pattern
          )
        ORDER BY
            CASE WHEN LTRIM(RTRIM(supplier._Code)) = :exact THEN 0 ELSE 1 END,
            supplier._Description,
            supplier._Code
    """)
    with engine.connect() as connection:
        rows = connection.execute(
            statement,
            {"pattern": f"%{clean_query}%", "exact": clean_query},
        ).mappings()
        return [
            {
                "ref": _clean(row.get("supplier_ref")),
                "code": _clean(row.get("supplier_code")),
                "name": _clean(row.get("supplier_name")),
            }
            for row in rows
            if _clean(row.get("supplier_ref"))
        ]


def fetch_onec_supplier_by_ref(engine: Any, *, supplier_ref: str) -> dict[str, str] | None:
    """Resolve one active 1C counterparty by its binary-reference text."""

    clean_ref = _clean(supplier_ref)
    if not clean_ref:
        return None
    statement = text("""
        SELECT
            CONVERT(varchar(34), supplier._IDRRef, 1) AS supplier_ref,
            NULLIF(LTRIM(RTRIM(supplier._Code)), N'') AS supplier_code,
            NULLIF(LTRIM(RTRIM(supplier._Description)), N'') AS supplier_name
        FROM dbo._Reference54 AS supplier WITH (NOLOCK)
        WHERE supplier._Marked = 0x00
          AND CONVERT(varchar(34), supplier._IDRRef, 1) = :supplier_ref
    """)
    with engine.connect() as connection:
        row = connection.execute(statement, {"supplier_ref": clean_ref}).mappings().first()
    if row is None:
        return None
    return {
        "ref": _clean(row.get("supplier_ref")),
        "code": _clean(row.get("supplier_code")),
        "name": _clean(row.get("supplier_name")),
    }


def _clean(value: Any) -> str:
    return str(value or "").strip()
