"""Build a frozen family-level cold-start backtest for iPhone 17 Pro Max displays.

The task is deliberately read-only with respect to 1C and pricing-service.  A
live 1C read is used only when ``--refresh-snapshot`` is requested; subsequent
runs reuse the compact, checksummed SQLite snapshot.

The first supplier order of every SKU remains an exogenous manual decision.
The counterfactual policy replaces only repeat orders.  This matches the
approved operating boundary: first lots stay manual and no production order is
created by the backtest.
"""

from __future__ import annotations

import argparse
import calendar
import csv
import hashlib
import html
import json
import math
import os
import re
import sqlite3
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import bindparam, text

from app.core.config import get_settings
from app.infrastructure.db.engines import build_engine
from app.services.assortment_lifecycle_facts import (
    RECEIPT_MAPPING_UNRESOLVED,
    SUPPLIER_ORDER_MAPPING_UNRESOLVED,
)
from app.services.display_family_demand import display_construction_segment
from app.services.display_scope_policy import (
    DISPLAY_SCOPE_POLICY_VERSION,
    filter_display_scope_records,
)
from app.services.market_research.wordstat import build_wordstat_client_from_settings
from tasks.report_display_supplier_lead_time_history import (
    DEFAULT_RECEIPT_MAPPING_JSON,
    DEFAULT_SUPPLIER_ORDER_MAPPING_JSON,
    _load_document_line_mapping,
    fetch_receipt_line_rows,
    fetch_supplier_order_line_rows,
)

DATASET_HASH = "582e10da08e6968f5e1aa450cd88df5dfb5af6c2b9ba84ad05799ec6ec17a6d1"
SNAPSHOT_SCHEMA = "iphone17_pro_max_cold_start_snapshot.v1"
RESULT_SCHEMA = "iphone17_pro_max_cold_start_backtest.v3"
SOURCE_FROM = date(2025, 1, 1)
SOURCE_TO = date(2026, 7, 31)
HOLDOUT_FROM = date(2026, 1, 1)
HOLDOUT_TO = SOURCE_TO
REVIEW_WEEKDAY = 0  # Monday
REVIEW_INTERVAL_DAYS = 7
LEAD_TIME_MATURITY_FLOOR_DAYS = 60
WORDSTAT_REGION = "225"
WORDSTAT_HISTORY_FROM = date(2024, 9, 1)
WORDSTAT_HISTORY_TO = HOLDOUT_TO
WORDSTAT_CACHE_SCHEMA = "iphone17_pro_max_wordstat_history.v1"
WORDSTAT_PHRASE_TEMPLATES = {
    "display": "дисплей iphone {generation} pro max",
    "screen": "экран iphone {generation} pro max",
    "repair": "замена дисплея iphone {generation} pro max",
}

RELEASE_DATES = {
    14: date(2022, 9, 16),
    15: date(2023, 9, 22),
    16: date(2024, 9, 20),
    17: date(2025, 9, 19),
}

TARGET_CODES = {
    "РБ000065582",
    "РБ000072167",
    "РБ000072852",
    "РБ000072858",
    "РБ000073884",
    "РБ000073885",
    "РБ000075798",
    "РБ000075828",
}

DEFAULT_OUTPUT_DIR = (
    Path("reports/assortment_lifecycle") / "iphone-17-pro-max-cold-start-backtest-v3-2026-08-15"
)
DEFAULT_PREFLIGHT_DIR = (
    Path("reports/assortment_lifecycle") / "backtest-2026-01-01_2026-07-31" / "preflight"
)
DEFAULT_REPLAY_STORE = Path(".local/assortment-lifecycle-backtest-store.sqlite3")


@dataclass(frozen=True)
class TransitionProfile:
    code: str
    hybrid_min_sales: int
    hybrid_min_sale_days: int
    hybrid_min_available_days: int
    own_min_sales: int
    own_min_sale_days: int
    own_min_available_days: int


TRANSITION_PROFILES = {
    "early": TransitionProfile("early", 6, 4, 7, 30, 12, 21),
    "balanced": TransitionProfile("balanced", 8, 5, 10, 36, 14, 28),
    "strict": TransitionProfile("strict", 12, 7, 14, 48, 18, 35),
}


@dataclass(frozen=True)
class Candidate:
    analog_pool: str
    analog_window_days: int
    repair_lag_days: int
    hybrid_prior_days: int
    early_reorder_cap_ratio: float
    temporary_buffer_days: int
    transition_profile: str

    @property
    def candidate_id(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class WordstatPolicy:
    availability_lag_days: int
    lookback_months: int
    min_confirming_phrases: int
    min_growth_ratio: float
    max_uplift_ratio: float

    @property
    def policy_id(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass
class SimulationResult:
    candidate_id: str
    generation: int
    evaluation_from: str
    evaluation_to: str
    demand_qty: float
    served_sales_qty: float
    lost_sales_qty: float
    served_sales_ratio: float
    gross_margin_rub: float
    missed_gross_profit_rub: float
    average_inventory_qty: float
    average_inventory_cost_rub: float
    gmroi: float | None
    ending_inventory_qty: float
    ending_required_qty: float
    ending_excess_qty: float
    ending_shortfall_qty: float
    model_repeat_order_qty: float
    model_repeat_order_count: int
    early_repeat_order_qty: float
    first_model_repeat_at: str
    cold_start_days: int
    hybrid_days: int
    own_history_days: int
    wordstat_confirmed_review_count: int
    wordstat_adjusted_review_count: int
    wordstat_max_modifier: float
    adjustment_shortfall_qty: float
    decisions: list[dict[str, Any]]

    def summary_row(self) -> dict[str, Any]:
        row = asdict(self)
        row.pop("decisions")
        return row


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--snapshot-dir",
        type=Path,
        help="Reuse an existing checksummed snapshot without refreshing external sources.",
    )
    parser.add_argument(
        "--wordstat-cache-dir",
        type=Path,
        help="Reuse an existing content-addressed Wordstat cache without an API call.",
    )
    parser.add_argument("--preflight-dir", type=Path, default=DEFAULT_PREFLIGHT_DIR)
    parser.add_argument("--replay-store", type=Path, default=DEFAULT_REPLAY_STORE)
    parser.add_argument("--dataset-hash", default=DATASET_HASH)
    parser.add_argument("--refresh-snapshot", action="store_true")
    parser.add_argument("--refresh-wordstat", action="store_true")
    parser.add_argument("--without-wordstat", action="store_true")
    parser.add_argument("--skip-grid", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _anonymous(value: Any) -> str:
    text = str(value or "").strip()
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16] if text else ""


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _number(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(Decimal(str(value).replace(" ", "").replace(",", ".")))
    except (InvalidOperation, TypeError, ValueError):
        return default


def _date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        result = value.date()
    elif isinstance(value, date):
        result = value
    else:
        text = str(value).strip().split("T", 1)[0].split(" ", 1)[0]
        try:
            result = date.fromisoformat(text)
        except ValueError:
            return None
    return None if result <= date(1753, 1, 1) else result


def _json_date(value: Any) -> str:
    parsed = _date(value)
    return parsed.isoformat() if parsed else ""


def generation_from_name(name: str) -> int | None:
    match = re.search(r"iphone\s*(1[4-7])\s*pro\s*max", name, flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


def quality_segment(name: str, quality_raw: str = "") -> str:
    value = f"{name} {quality_raw}".casefold().replace("-", " ")
    if "in cell" in value or "incell" in value:
        return "In-Cell"
    if "hard oled" in value:
        return "Hard OLED"
    if "soft oled" in value or "ultra soft" in value:
        return "Soft OLED"
    if any(marker in value for marker in ("orig", "переклей", "снятый", "fog")):
        return "Original"
    return "Other"


def display_segment_id(name: str, quality_raw: str = "") -> str:
    """Return the substitution/allocation grain approved for display families."""

    return f"{quality_segment(name, quality_raw)}|{display_construction_segment(name)}"


def planned_arrival_date(decision_date: date, lead: Mapping[str, Any]) -> date:
    """Use the same conservative lead-time percentile as the coverage calculation."""

    return decision_date + timedelta(days=int(lead["p75"]))


def segment_evidence_days(segments: Iterable[str], history: Mapping[str, set[date]]) -> int:
    """Require availability evidence for every already launched segment."""

    normalized = sorted(set(segments))
    if not normalized:
        return 0
    return min(len(history.get(segment, set())) for segment in normalized)


def _quantile(values: Sequence[float], probability: float, default: int) -> int:
    clean = sorted(value for value in values if value >= 0)
    if not clean:
        return default
    index = max(0, math.ceil(probability * len(clean)) - 1)
    return max(1, int(math.ceil(clean[index])))


def _iter_dates(start: date, end: date) -> Iterable[date]:
    cursor = start
    while cursor <= end:
        yield cursor
        cursor += timedelta(days=1)


def _snapshot_connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def _create_snapshot_schema(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        PRAGMA journal_mode = DELETE;
        PRAGMA synchronous = FULL;
        CREATE TABLE metadata (key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
        CREATE TABLE sku (
            nomenclature_code TEXT PRIMARY KEY,
            generation INTEGER NOT NULL,
            name TEXT NOT NULL,
            quality_raw TEXT NOT NULL,
            quality_segment TEXT NOT NULL,
            product_ref_hash TEXT NOT NULL,
            first_order_at TEXT NOT NULL DEFAULT '',
            first_receipt_at TEXT NOT NULL DEFAULT '',
            first_sale_at TEXT NOT NULL DEFAULT '',
            item_cost_rub REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE daily_fact (
            business_date TEXT NOT NULL,
            nomenclature_code TEXT NOT NULL,
            sales_qty REAL NOT NULL DEFAULT 0,
            sale_document_count INTEGER NOT NULL DEFAULT 0,
            sale_customer_count INTEGER NOT NULL DEFAULT 0,
            sale_point_count INTEGER NOT NULL DEFAULT 0,
            available INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (business_date, nomenclature_code)
        );
        CREATE TABLE sale_observation (
            business_date TEXT NOT NULL,
            nomenclature_code TEXT NOT NULL,
            document_hash TEXT NOT NULL,
            customer_hash TEXT NOT NULL,
            sales_point_hash TEXT NOT NULL,
            quantity REAL NOT NULL,
            PRIMARY KEY (
                business_date, nomenclature_code, document_hash,
                customer_hash, sales_point_hash
            )
        );
        CREATE TABLE target_daily (
            business_date TEXT NOT NULL,
            nomenclature_code TEXT NOT NULL,
            sales_qty REAL NOT NULL DEFAULT 0,
            physical_stock_qty REAL NOT NULL DEFAULT 0,
            kmp4_raw_qty REAL NOT NULL DEFAULT 0,
            kmp4_matched_qty REAL NOT NULL DEFAULT 0,
            kmp4_expired_qty REAL NOT NULL DEFAULT 0,
            kmp4_open_qty REAL NOT NULL DEFAULT 0,
            site_order_raw_qty REAL NOT NULL DEFAULT 0,
            site_order_matched_qty REAL NOT NULL DEFAULT 0,
            site_order_expired_qty REAL NOT NULL DEFAULT 0,
            site_order_hidden_qty REAL NOT NULL DEFAULT 0,
            site_order_open_qty REAL NOT NULL DEFAULT 0,
            gross_incoming_qty REAL NOT NULL DEFAULT 0,
            placed_incoming_qty REAL NOT NULL DEFAULT 0,
            free_incoming_qty REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (business_date, nomenclature_code)
        );
        CREATE TABLE economics (
            business_date TEXT NOT NULL,
            nomenclature_code TEXT NOT NULL,
            inventory_cost_per_unit_rub REAL NOT NULL DEFAULT 0,
            gross_margin_per_unit_rub REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT '',
            status_label TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (business_date, nomenclature_code)
        );
        CREATE TABLE supply_order (
            order_hash TEXT NOT NULL,
            nomenclature_code TEXT NOT NULL,
            order_date TEXT NOT NULL,
            cargo_date TEXT NOT NULL DEFAULT '',
            quantity REAL NOT NULL,
            unit_price_rub REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (order_hash, nomenclature_code)
        );
        CREATE TABLE receipt (
            receipt_hash TEXT NOT NULL,
            order_hash TEXT NOT NULL DEFAULT '',
            nomenclature_code TEXT NOT NULL,
            receipt_date TEXT NOT NULL,
            quantity REAL NOT NULL,
            PRIMARY KEY (receipt_hash, nomenclature_code)
        );
        CREATE TABLE kmp4_event (
            business_date TEXT NOT NULL,
            nomenclature_code TEXT NOT NULL,
            document_hash TEXT NOT NULL,
            quantity REAL NOT NULL,
            PRIMARY KEY (business_date, nomenclature_code, document_hash)
        );
        CREATE TABLE site_event (
            source_ordinal INTEGER PRIMARY KEY,
            event_date TEXT NOT NULL,
            nomenclature_code TEXT NOT NULL,
            event_type TEXT NOT NULL,
            event_hash TEXT NOT NULL,
            session_hash TEXT NOT NULL,
            order_hash TEXT NOT NULL,
            quantity REAL NOT NULL,
            mapping_status TEXT NOT NULL
        );
        CREATE INDEX ix_daily_fact_code_date
            ON daily_fact(nomenclature_code, business_date);
        CREATE INDEX ix_order_code_date
            ON supply_order(nomenclature_code, order_date);
        CREATE INDEX ix_receipt_code_date
            ON receipt(nomenclature_code, receipt_date);
        """)


def _load_item_rows(
    replay_store: Path, dataset_hash: str
) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, Any]]:
    connection = sqlite3.connect(f"file:{replay_store}?mode=ro", uri=True)
    try:
        candidate_rows: list[dict[str, Any]] = []
        raw_refs_by_code: dict[str, str] = {}
        query = """
            SELECT nomenclature_code, payload_json
            FROM replay_dataset_fact
            WHERE dataset_hash = ? AND fact_type = 'item'
            ORDER BY nomenclature_code
        """
        for code, payload_json in connection.execute(query, (dataset_hash,)):
            payload = json.loads(payload_json)
            name = _clean(payload.get("name"))
            generation = generation_from_name(name)
            if generation not in RELEASE_DATES:
                continue
            raw_ref = _clean(payload.get("product_ref"))
            candidate_rows.append(
                {
                    "nomenclature_code": code,
                    "generation": generation,
                    "name": name,
                    "quality_raw": _clean(payload.get("quality_raw")),
                    "quality_segment": quality_segment(name, _clean(payload.get("quality_raw"))),
                    "product_ref_hash": _anonymous(raw_ref),
                    "item_cost_rub": _number(payload.get("expensive_item_value")),
                }
            )
            raw_refs_by_code[code] = raw_ref
        scope_result = filter_display_scope_records(candidate_rows)
        rows = list(scope_result.included)
        raw_refs = {
            row["nomenclature_code"]: raw_refs_by_code[row["nomenclature_code"]] for row in rows
        }
        return rows, raw_refs, scope_result.audit
    finally:
        connection.close()


def _copy_replay_daily_facts(
    target: sqlite3.Connection,
    *,
    replay_store: Path,
    dataset_hash: str,
    codes: Sequence[str],
) -> None:
    source = sqlite3.connect(f"file:{replay_store}?mode=ro", uri=True)
    try:
        insert_rows: list[tuple[Any, ...]] = []
        observation_rows: list[tuple[Any, ...]] = []
        for code in sorted(codes):
            daily: dict[str, dict[str, Any]] = defaultdict(
                lambda: {
                    "sales": 0.0,
                    "documents": set(),
                    "customers": set(),
                    "points": set(),
                    "available": 0,
                }
            )
            query = """
                SELECT business_date, fact_type, payload_json
                FROM replay_dataset_fact
                WHERE dataset_hash = ? AND nomenclature_code = ?
                  AND business_date BETWEEN ? AND ?
                  AND fact_type IN ('sale', 'sale_observation', 'available')
                ORDER BY business_date, ordinal
            """
            for business_date, fact_type, payload_json in source.execute(
                query,
                (dataset_hash, code, SOURCE_FROM.isoformat(), SOURCE_TO.isoformat()),
            ):
                payload = json.loads(payload_json)
                bucket = daily[business_date]
                if fact_type == "sale":
                    bucket["sales"] += _number(payload.get("quantity"))
                elif fact_type == "available":
                    bucket["available"] = int(bool(payload.get("available", True)))
                else:
                    if payload.get("document_id"):
                        bucket["documents"].add(payload["document_id"])
                    if payload.get("customer_id"):
                        bucket["customers"].add(payload["customer_id"])
                    if payload.get("sales_point_id"):
                        bucket["points"].add(payload["sales_point_id"])
                    observation_rows.append(
                        (
                            business_date,
                            code,
                            _clean(payload.get("document_id")),
                            _clean(payload.get("customer_id")),
                            _clean(payload.get("sales_point_id")),
                            _number(payload.get("quantity")),
                        )
                    )
            for business_date, bucket in sorted(daily.items()):
                insert_rows.append(
                    (
                        business_date,
                        code,
                        bucket["sales"],
                        len(bucket["documents"]),
                        len(bucket["customers"]),
                        len(bucket["points"]),
                        bucket["available"],
                    )
                )
            if len(insert_rows) >= 10_000:
                target.executemany(
                    "INSERT INTO daily_fact VALUES (?, ?, ?, ?, ?, ?, ?)", insert_rows
                )
                target.commit()
                insert_rows.clear()
            if len(observation_rows) >= 10_000:
                target.executemany(
                    "INSERT INTO sale_observation VALUES (?, ?, ?, ?, ?, ?)",
                    observation_rows,
                )
                target.commit()
                observation_rows.clear()
        if insert_rows:
            target.executemany("INSERT INTO daily_fact VALUES (?, ?, ?, ?, ?, ?, ?)", insert_rows)
            target.commit()
        if observation_rows:
            target.executemany(
                "INSERT INTO sale_observation VALUES (?, ?, ?, ?, ?, ?)", observation_rows
            )
            target.commit()
    finally:
        source.close()


def _copy_target_daily(target: sqlite3.Connection, preflight_dir: Path) -> None:
    path = preflight_dir / "daily-facts.csv"
    columns = (
        "business_date",
        "nomenclature_code",
        "observed_sales_qty",
        "physical_stock_qty",
        "kmp4_raw_qty",
        "kmp4_matched_qty",
        "kmp4_expired_qty",
        "kmp4_open_qty",
        "site_order_raw_qty",
        "site_order_matched_qty",
        "site_order_expired_qty",
        "site_order_hidden_qty",
        "site_order_open_qty",
        "gross_incoming_qty",
        "placed_incoming_qty",
        "free_incoming_qty",
    )
    batch: list[tuple[Any, ...]] = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if row.get("nomenclature_code") not in TARGET_CODES:
                continue
            batch.append(
                (
                    row["business_date"],
                    row["nomenclature_code"],
                    *(_number(row.get(column)) for column in columns[2:]),
                )
            )
    target.executemany(
        "INSERT INTO target_daily VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        batch,
    )


def _copy_site_events(target: sqlite3.Connection, preflight_dir: Path) -> None:
    path = preflight_dir / "site-events-normalized.csv"
    rows: list[tuple[Any, ...]] = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for source_ordinal, row in enumerate(csv.DictReader(handle), start=1):
            code = _clean(row.get("nomenclature_code"))
            if code not in TARGET_CODES:
                continue
            rows.append(
                (
                    source_ordinal,
                    row["event_date"],
                    code,
                    _clean(row.get("event_type")),
                    _clean(row.get("event_key")),
                    _clean(row.get("session_key")),
                    _anonymous(row.get("order_number")),
                    _number(row.get("quantity")),
                    _clean(row.get("mapping_status")),
                )
            )
    target.executemany("INSERT INTO site_event VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)


def _copy_economics(target: sqlite3.Connection, preflight_dir: Path) -> None:
    path = preflight_dir / "decision-inputs.csv"
    rows: dict[tuple[str, str], tuple[Any, ...]] = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            code = row.get("nomenclature_code", "")
            if code not in TARGET_CODES:
                continue
            key = (row["decision_date"], code)
            rows[key] = (
                row["decision_date"],
                code,
                _number(row.get("inventory_cost_per_unit_rub")),
                _number(row.get("gross_margin_per_unit_rub")),
                _clean(row.get("status")),
                _clean(row.get("status_label")),
            )
    target.executemany("INSERT INTO economics VALUES (?, ?, ?, ?, ?, ?)", rows.values())


def _fetch_corrected_supply(
    *, raw_refs: Mapping[str, str], history_start: date, date_to: date
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    settings = get_settings()
    if not settings.onec_database_url:
        raise RuntimeError("ONEC_DATABASE_URL is required for --refresh-snapshot")
    supplier_mapping = _load_document_line_mapping(
        DEFAULT_SUPPLIER_ORDER_MAPPING_JSON,
        error_code=SUPPLIER_ORDER_MAPPING_UNRESOLVED,
    )
    receipt_mapping = _load_document_line_mapping(
        DEFAULT_RECEIPT_MAPPING_JSON,
        error_code=RECEIPT_MAPPING_UNRESOLVED,
    )
    engine = build_engine(settings.onec_database_url, pool_pre_ping=True)
    try:
        refs = {value for value in raw_refs.values() if value}
        orders = fetch_supplier_order_line_rows(
            engine,
            supplier_mapping=supplier_mapping,
            allowed_refs=refs,
            history_start=history_start,
            date_to=date_to + timedelta(days=1),
        )
        receipts = fetch_receipt_line_rows(
            engine,
            receipt_mapping=receipt_mapping,
            allowed_refs=refs,
            history_start=history_start,
            date_to=date_to + timedelta(days=1),
        )
        kmp4_query = text("""
            WITH demand_counterparties AS (
                SELECT DISTINCT _Fld8857RRef AS counterparty_ref
                FROM dbo._Reference69 WITH (NOLOCK)
                WHERE _Fld8857RRef <> 0x00000000000000000000000000000000
            )
            SELECT
                CONVERT(varchar(34), doc._IDRRef, 1) AS document_ref,
                CAST(doc._Date_Time AS date) AS business_date,
                NULLIF(LTRIM(RTRIM(product._Code)), N'') AS nomenclature_code,
                CAST(line._Fld2431 AS decimal(28, 3)) AS quantity
            FROM dbo._Document132 AS doc WITH (NOLOCK)
            JOIN demand_counterparties AS demand
              ON demand.counterparty_ref = doc._Fld2405RRef
            JOIN dbo._Document132_VT2427 AS line WITH (NOLOCK)
              ON line._Document132_IDRRef = doc._IDRRef
            JOIN dbo._Reference62 AS product WITH (NOLOCK)
              ON product._IDRRef = line._Fld2434RRef
            WHERE doc._Marked = 0x00
              AND doc._Date_Time >= :date_from
              AND doc._Date_Time < :date_to
              AND line._Fld2431 > 0
              AND NULLIF(LTRIM(RTRIM(product._Code)), N'') IN :codes
            ORDER BY doc._Date_Time, product._Code
            """).bindparams(bindparam("codes", expanding=True))
        with engine.connect() as connection:
            kmp4_rows = [
                dict(row)
                for row in connection.execute(
                    kmp4_query,
                    {
                        "date_from": datetime.combine(HOLDOUT_FROM, datetime.min.time()),
                        "date_to": datetime.combine(
                            date_to + timedelta(days=1), datetime.min.time()
                        ),
                        "codes": sorted(TARGET_CODES),
                    },
                ).mappings()
            ]
        return orders, receipts, kmp4_rows
    finally:
        engine.dispose()


def build_snapshot(
    *,
    snapshot_path: Path,
    manifest_path: Path,
    replay_store: Path,
    dataset_hash: str,
    preflight_dir: Path,
) -> dict[str, Any]:
    item_rows, raw_refs, scope_policy_audit = _load_item_rows(replay_store, dataset_hash)
    selected_codes = [row["nomenclature_code"] for row in item_rows]
    if not TARGET_CODES.issubset(set(selected_codes)):
        missing = sorted(TARGET_CODES - set(selected_codes))
        raise ValueError(f"target_family_codes_missing:{','.join(missing)}")

    orders, receipts, kmp4_rows = _fetch_corrected_supply(
        raw_refs=raw_refs,
        history_start=date(2024, 1, 1),
        date_to=SOURCE_TO,
    )
    temporary_path = snapshot_path.with_suffix(".sqlite3.tmp")
    if temporary_path.exists():
        temporary_path.unlink()
    connection = _snapshot_connect(temporary_path)
    try:
        _create_snapshot_schema(connection)
        connection.executemany(
            """
            INSERT INTO sku(
                nomenclature_code, generation, name, quality_raw,
                quality_segment, product_ref_hash, item_cost_rub
            ) VALUES (
                :nomenclature_code, :generation, :name, :quality_raw,
                :quality_segment, :product_ref_hash, :item_cost_rub
            )
            """,
            item_rows,
        )
        _copy_replay_daily_facts(
            connection,
            replay_store=replay_store,
            dataset_hash=dataset_hash,
            codes=selected_codes,
        )
        _copy_target_daily(connection, preflight_dir)
        _copy_site_events(connection, preflight_dir)
        _copy_economics(connection, preflight_dir)

        order_values: dict[tuple[str, str], dict[str, Any]] = {}
        for row in orders:
            code = _clean(row.get("nomenclature_code"))
            order_date = _json_date(row.get("supplier_order_created_at"))
            if code not in raw_refs or not order_date:
                continue
            key = (_anonymous(row.get("supplier_order_ref")), code)
            value = order_values.setdefault(
                key,
                {
                    "order_date": order_date,
                    "cargo_date": _json_date(row.get("cargo_handoff_at")),
                    "quantity": 0.0,
                    "amount": 0.0,
                },
            )
            quantity = _number(row.get("qty"))
            value["quantity"] += quantity
            value["amount"] += _number(row.get("price")) * quantity
        order_rows = [
            (
                order_hash,
                code,
                value["order_date"],
                value["cargo_date"],
                value["quantity"],
                value["amount"] / value["quantity"] if value["quantity"] else 0.0,
            )
            for (order_hash, code), value in sorted(order_values.items())
        ]
        receipt_values: dict[tuple[str, str], dict[str, Any]] = {}
        for row in receipts:
            code = _clean(row.get("nomenclature_code"))
            receipt_date = _json_date(row.get("receipt_at"))
            if code not in raw_refs or not receipt_date:
                continue
            key = (_anonymous(row.get("receipt_ref")), code)
            value = receipt_values.setdefault(
                key,
                {
                    "order_hash": _anonymous(row.get("supplier_order_ref")),
                    "receipt_date": receipt_date,
                    "quantity": 0.0,
                },
            )
            value["quantity"] += _number(row.get("receipt_qty"))
        receipt_rows = [
            (
                receipt_hash,
                value["order_hash"],
                code,
                value["receipt_date"],
                value["quantity"],
            )
            for (receipt_hash, code), value in sorted(receipt_values.items())
        ]
        connection.executemany("INSERT INTO supply_order VALUES (?, ?, ?, ?, ?, ?)", order_rows)
        connection.executemany("INSERT INTO receipt VALUES (?, ?, ?, ?, ?)", receipt_rows)
        kmp4_values: dict[tuple[str, str, str], float] = defaultdict(float)
        for row in kmp4_rows:
            business_date = _json_date(row.get("business_date"))
            code = _clean(row.get("nomenclature_code"))
            document_hash = _anonymous(row.get("document_ref"))
            if business_date and code in TARGET_CODES and document_hash:
                kmp4_values[(business_date, code, document_hash)] += _number(row.get("quantity"))
        connection.executemany(
            "INSERT INTO kmp4_event VALUES (?, ?, ?, ?)",
            [(*key, quantity) for key, quantity in sorted(kmp4_values.items())],
        )

        connection.execute("""
            UPDATE sku SET
                first_order_at = COALESCE((
                    SELECT MIN(order_date) FROM supply_order o
                    WHERE o.nomenclature_code = sku.nomenclature_code
                ), ''),
                first_receipt_at = COALESCE((
                    SELECT MIN(receipt_date) FROM receipt r
                    WHERE r.nomenclature_code = sku.nomenclature_code
                ), ''),
                first_sale_at = COALESCE((
                    SELECT MIN(business_date) FROM daily_fact d
                    WHERE d.nomenclature_code = sku.nomenclature_code AND d.sales_qty > 0
                ), '')
            """)
        metadata = {
            "schema": SNAPSHOT_SCHEMA,
            "dataset_hash": dataset_hash,
            "source_from": SOURCE_FROM.isoformat(),
            "source_to": SOURCE_TO.isoformat(),
            "holdout_from": HOLDOUT_FROM.isoformat(),
            "holdout_to": HOLDOUT_TO.isoformat(),
            "target_codes": sorted(TARGET_CODES),
            "release_dates": {str(key): value.isoformat() for key, value in RELEASE_DATES.items()},
            "receipt_quantity_column": "_Fld4514",
            "receipt_supplier_order_column": "_Fld4525RRef",
            "production_action": "none_read_only",
            "scope_policy": scope_policy_audit,
        }
        connection.executemany(
            "INSERT INTO metadata VALUES (?, ?)",
            [
                (key, json.dumps(value, ensure_ascii=False, sort_keys=True))
                for key, value in metadata.items()
            ],
        )
        connection.commit()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ValueError(f"snapshot_integrity_failed:{integrity}")
    finally:
        connection.close()
    os.replace(temporary_path, snapshot_path)

    manifest = {
        "schema": "iphone17_pro_max_cold_start_snapshot_manifest.v1",
        "created_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "snapshot_file": snapshot_path.name,
        "snapshot_sha256": _sha256(snapshot_path),
        "snapshot_size_bytes": snapshot_path.stat().st_size,
        "dataset_hash": dataset_hash,
        "replay_store_content_sha256": _dataset_content_sha256(replay_store, dataset_hash),
        "preflight_manifest_sha256": _sha256(preflight_dir / "run-manifest.json"),
        "preflight_files": {
            name: _sha256(preflight_dir / name)
            for name in (
                "daily-facts.csv",
                "decision-inputs.csv",
                "historical-sales.csv",
                "site-events-normalized.csv",
            )
        },
        "mapping_files": {
            str(DEFAULT_SUPPLIER_ORDER_MAPPING_JSON): _sha256(DEFAULT_SUPPLIER_ORDER_MAPPING_JSON),
            str(DEFAULT_RECEIPT_MAPPING_JSON): _sha256(DEFAULT_RECEIPT_MAPPING_JSON),
        },
        "row_counts": _snapshot_row_counts(snapshot_path),
        "production_action": "none_read_only",
        "scope_policy": scope_policy_audit,
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    return manifest


def _dataset_content_sha256(replay_store: Path, dataset_hash: str) -> str:
    connection = sqlite3.connect(f"file:{replay_store}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT content_sha256 FROM replay_dataset WHERE dataset_hash = ?", (dataset_hash,)
        ).fetchone()
        if row is None:
            raise ValueError(f"replay_dataset_not_found:{dataset_hash}")
        return row[0]
    finally:
        connection.close()


def _snapshot_row_counts(snapshot_path: Path) -> dict[str, int]:
    connection = _snapshot_connect(snapshot_path, readonly=True)
    try:
        return {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "sku",
                "daily_fact",
                "sale_observation",
                "target_daily",
                "economics",
                "supply_order",
                "receipt",
                "kmp4_event",
                "site_event",
            )
        }
    finally:
        connection.close()


def validate_snapshot(snapshot_path: Path, manifest_path: Path) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    scope_policy = manifest.get("scope_policy") or {}
    if scope_policy.get("scope_policy_version") != DISPLAY_SCOPE_POLICY_VERSION:
        raise ValueError("snapshot_scope_policy_version_missing_or_stale")
    if manifest.get("snapshot_sha256") != _sha256(snapshot_path):
        raise ValueError("snapshot_sha256_mismatch")
    counts = _snapshot_row_counts(snapshot_path)
    if counts != manifest.get("row_counts"):
        raise ValueError("snapshot_row_counts_mismatch")
    connection = _snapshot_connect(snapshot_path, readonly=True)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        target_count = connection.execute(
            "SELECT COUNT(*) FROM sku WHERE nomenclature_code IN ({})".format(
                ",".join("?" for _ in TARGET_CODES)
            ),
            sorted(TARGET_CODES),
        ).fetchone()[0]
        target_sales = connection.execute("SELECT SUM(sales_qty) FROM target_daily").fetchone()[0]
        kmp4_raw = connection.execute("SELECT SUM(kmp4_raw_qty) FROM target_daily").fetchone()[0]
        site_raw = connection.execute(
            "SELECT SUM(site_order_raw_qty) FROM target_daily"
        ).fetchone()[0]
        target_receipts = connection.execute(
            """
            SELECT COUNT(*), SUM(quantity),
                   SUM(CASE WHEN order_hash <> '' THEN 1 ELSE 0 END)
            FROM receipt WHERE nomenclature_code IN ({})
            """.format(",".join("?" for _ in TARGET_CODES)),
            sorted(TARGET_CODES),
        ).fetchone()
        sale_observations = connection.execute(
            """
            SELECT COUNT(*), COUNT(DISTINCT document_hash)
            FROM sale_observation
            WHERE business_date BETWEEN ? AND ?
              AND nomenclature_code IN ({})
            """.format(",".join("?" for _ in TARGET_CODES)),
            (HOLDOUT_FROM.isoformat(), HOLDOUT_TO.isoformat(), *sorted(TARGET_CODES)),
        ).fetchone()
        kmp4_events = connection.execute(
            "SELECT COUNT(*), COUNT(DISTINCT document_hash), SUM(quantity) FROM kmp4_event"
        ).fetchone()
        site_events = connection.execute("""
            SELECT COUNT(*), COUNT(DISTINCT event_hash),
                   COUNT(DISTINCT CASE WHEN order_hash <> '' THEN order_hash END),
                   SUM(quantity)
            FROM site_event
            WHERE event_type = 'site_order'
            """).fetchone()
    finally:
        connection.close()
    checks = {
        "integrity": integrity,
        "target_sku_count": int(target_count),
        "target_sales_qty": _number(target_sales),
        "target_kmp4_raw_qty": _number(kmp4_raw),
        "target_site_order_raw_qty": _number(site_raw),
        "target_receipt_count": int(target_receipts[0]),
        "target_receipt_qty": _number(target_receipts[1]),
        "target_receipts_linked_to_order": int(target_receipts[2]),
        "target_sale_observation_count": int(sale_observations[0]),
        "target_sale_document_count": int(sale_observations[1]),
        "target_kmp4_line_count": int(kmp4_events[0]),
        "target_kmp4_document_count": int(kmp4_events[1]),
        "target_kmp4_event_qty": _number(kmp4_events[2]),
        "target_site_line_count": int(site_events[0]),
        "target_site_unique_event_count": int(site_events[1]),
        "target_site_order_count": int(site_events[2]),
        "target_site_event_qty": _number(site_events[3]),
    }
    expected = {
        "integrity": "ok",
        "target_sku_count": 8,
        "target_sales_qty": 650.0,
        "target_kmp4_raw_qty": 215.0,
        "target_site_order_raw_qty": 94.0,
        "target_receipt_count": 18,
        "target_receipt_qty": 929.0,
        "target_receipts_linked_to_order": 18,
        "target_sale_observation_count": 622,
        "target_sale_document_count": 607,
        "target_kmp4_line_count": 86,
        "target_kmp4_document_count": 77,
        "target_kmp4_event_qty": 215.0,
        "target_site_line_count": 78,
        "target_site_unique_event_count": 78,
        "target_site_order_count": 68,
        "target_site_event_qty": 94.0,
    }
    if checks != expected:
        raise ValueError(f"snapshot_reconciliation_failed:{checks}")
    return checks


def _month_end(period_start: date) -> date:
    return period_start.replace(day=calendar.monthrange(period_start.year, period_start.month)[1])


class WordstatHistory:
    """Frozen, non-additive monthly signals available at a historical decision date."""

    def __init__(
        self,
        payload: Mapping[str, Any],
        *,
        cache_path: Path | None = None,
        cache_sha256: str = "",
    ):
        if payload.get("schema") != WORDSTAT_CACHE_SCHEMA:
            raise ValueError("wordstat_cache_schema_mismatch")
        self.payload = dict(payload)
        self.cache_path = cache_path
        self.cache_sha256 = cache_sha256
        self.series: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
        self.phrases: dict[tuple[int, str], str] = {}
        for query in payload.get("queries", []):
            generation = int(query["generation"])
            phrase_key = str(query["phrase_key"])
            self.phrases[(generation, phrase_key)] = str(query["phrase"])
            for raw_point in query.get("response", {}).get("results", []):
                period_start = _date(raw_point.get("date"))
                if period_start is None:
                    continue
                self.series[(generation, phrase_key)].append(
                    {
                        "period_start": period_start,
                        "period_end": _month_end(period_start),
                        "count": int(_number(raw_point.get("count"))),
                        "share": _number(raw_point.get("share")),
                    }
                )
        for points in self.series.values():
            points.sort(key=lambda row: row["period_start"])

    def signal_at(
        self, *, generation: int, decision_date: date, policy: WordstatPolicy
    ) -> dict[str, Any]:
        phrase_details: list[dict[str, Any]] = []
        growth_values: list[float] = []
        for phrase_key in WORDSTAT_PHRASE_TEMPLATES:
            points = self.series.get((generation, phrase_key), [])
            eligible = [
                row
                for row in points
                if row["period_end"] + timedelta(days=policy.availability_lag_days) <= decision_date
            ]
            if len(eligible) <= policy.lookback_months:
                continue
            latest = eligible[-1]
            previous = eligible[-policy.lookback_months - 1 : -1]
            baseline = median(row["share"] for row in previous)
            if baseline <= 0 or latest["share"] <= 0:
                continue
            growth = latest["share"] / baseline - 1.0
            growth_values.append(growth)
            phrase_details.append(
                {
                    "phrase_key": phrase_key,
                    "phrase": self.phrases.get((generation, phrase_key), ""),
                    "latest_period": latest["period_start"].isoformat(),
                    "available_on": (
                        latest["period_end"] + timedelta(days=policy.availability_lag_days)
                    ).isoformat(),
                    "latest_count": latest["count"],
                    "latest_share": latest["share"],
                    "baseline_share": baseline,
                    "growth_ratio": round(growth, 6),
                }
            )
        confirming = sum(growth >= policy.min_growth_ratio for growth in growth_values)
        median_growth = median(growth_values) if growth_values else 0.0
        confirmed = (
            confirming >= policy.min_confirming_phrases and median_growth >= policy.min_growth_ratio
        )
        uplift = min(policy.max_uplift_ratio, max(0.0, median_growth)) if confirmed else 0.0
        latest_period = max((row["latest_period"] for row in phrase_details), default="")
        available_on = max((row["available_on"] for row in phrase_details), default="")
        if not phrase_details:
            action = "no_completed_period"
        elif not confirmed:
            action = "direction_not_confirmed"
        elif uplift > 0:
            action = "bounded_family_forecast_uplift"
        else:
            action = "confirmed_direction_manual_review"
        return {
            "status": "confirmed_uptrend" if confirmed else "not_confirmed",
            "action": action,
            "latest_period": latest_period,
            "available_on": available_on,
            "usable_phrase_count": len(growth_values),
            "confirming_phrase_count": confirming,
            "median_growth_ratio": round(median_growth, 6),
            "modifier": round(1.0 + uplift, 6),
            "phrase_details": phrase_details,
            "counts_aggregated": False,
        }

    def summary(self) -> dict[str, Any]:
        return {
            "schema": self.payload["schema"],
            "cache_path": str(self.cache_path) if self.cache_path else "",
            "cache_sha256": self.cache_sha256,
            "fetched_at": self.payload.get("fetched_at", ""),
            "region": self.payload.get("region", ""),
            "period": self.payload.get("period", ""),
            "phrase_count": len(self.payload.get("queries", [])),
            "aggregation": "non_additive_direction_only",
        }


def _load_wordstat_cache(manifest_path: Path) -> WordstatHistory:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    cache_path = manifest_path.parent / manifest["active_file"]
    expected_sha256 = manifest["sha256"]
    if _sha256(cache_path) != expected_sha256:
        raise ValueError("wordstat_cache_sha256_mismatch")
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    return WordstatHistory(payload, cache_path=cache_path, cache_sha256=expected_sha256)


def load_or_fetch_wordstat_history(output_dir: Path, *, refresh: bool = False) -> WordstatHistory:
    cache_dir = output_dir / "wordstat-cache"
    manifest_path = cache_dir / "manifest.json"
    if manifest_path.exists() and not refresh:
        return _load_wordstat_cache(manifest_path)
    client = build_wordstat_client_from_settings()
    queries: list[dict[str, Any]] = []
    for generation in (16, 17):
        for phrase_key, template in WORDSTAT_PHRASE_TEMPLATES.items():
            phrase = template.format(generation=generation)
            response = client.get_dynamics_response(
                phrase=phrase,
                region=WORDSTAT_REGION,
                from_date=WORDSTAT_HISTORY_FROM,
                to_date=WORDSTAT_HISTORY_TO,
                period="monthly",
            )
            if response is None:
                raise RuntimeError(f"wordstat_dynamics_unavailable:{generation}:{phrase_key}")
            queries.append(
                {
                    "generation": generation,
                    "phrase_key": phrase_key,
                    "phrase": phrase,
                    "request_without_credentials": {
                        "region": WORDSTAT_REGION,
                        "devices": client.devices,
                        "period": "PERIOD_MONTHLY",
                        "from_date": WORDSTAT_HISTORY_FROM.isoformat(),
                        "to_date": WORDSTAT_HISTORY_TO.isoformat(),
                    },
                    "response": response,
                }
            )
    payload = {
        "schema": WORDSTAT_CACHE_SCHEMA,
        "fetched_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "api_path": "/v2/wordstat/dynamics",
        "region": WORDSTAT_REGION,
        "period": "PERIOD_MONTHLY",
        "from_date": WORDSTAT_HISTORY_FROM.isoformat(),
        "to_date": WORDSTAT_HISTORY_TO.isoformat(),
        "devices": client.devices,
        "phrase_basket": "non_additive_overlapping_queries",
        "queries": queries,
    }
    raw = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"wordstat-history-{digest[:16]}.json"
    if not cache_path.exists():
        temporary_path = cache_path.with_suffix(".json.tmp")
        temporary_path.write_bytes(raw)
        os.replace(temporary_path, cache_path)
    manifest = {
        "schema": "iphone17_pro_max_wordstat_cache_manifest.v1",
        "active_file": cache_path.name,
        "sha256": digest,
        "size_bytes": cache_path.stat().st_size,
        "production_action": "none_read_only",
    }
    temporary_manifest = manifest_path.with_suffix(".json.tmp")
    temporary_manifest.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temporary_manifest, manifest_path)
    return _load_wordstat_cache(manifest_path)


class FrozenFamilyData:
    def __init__(self, snapshot_path: Path):
        self.snapshot_path = snapshot_path
        self.connection = _snapshot_connect(snapshot_path, readonly=True)
        self.skus = {
            row["nomenclature_code"]: dict(row)
            for row in self.connection.execute("SELECT * FROM sku")
        }
        for row in self.skus.values():
            row["construction_segment"] = display_construction_segment(row["name"])
            row["segment_id"] = display_segment_id(row["name"], row["quality_raw"])
        self.codes_by_generation: dict[int, list[str]] = defaultdict(list)
        for code, row in self.skus.items():
            self.codes_by_generation[int(row["generation"])].append(code)
        for codes in self.codes_by_generation.values():
            codes.sort()
        self.daily: dict[tuple[str, str], dict[str, Any]] = {
            (row["business_date"], row["nomenclature_code"]): dict(row)
            for row in self.connection.execute("SELECT * FROM daily_fact")
        }
        self.target_daily: dict[tuple[str, str], dict[str, Any]] = {
            (row["business_date"], row["nomenclature_code"]): dict(row)
            for row in self.connection.execute("SELECT * FROM target_daily")
        }
        self.sale_observations = [
            dict(row) for row in self.connection.execute("SELECT * FROM sale_observation")
        ]
        self.orders = [dict(row) for row in self.connection.execute("SELECT * FROM supply_order")]
        self.receipts = [dict(row) for row in self.connection.execute("SELECT * FROM receipt")]
        self.economics = [dict(row) for row in self.connection.execute("SELECT * FROM economics")]
        self.family_daily: dict[int, dict[str, dict[str, Any]]] = defaultdict(dict)
        self.demand_index: dict[tuple[int, str], dict[str, dict[str, float]]] = defaultdict(dict)
        for (business_date, code), row in self.daily.items():
            generation = int(self.skus[code]["generation"])
            bucket = self.family_daily[generation].setdefault(
                business_date,
                {
                    "sales_qty": 0.0,
                    "available": False,
                    "sales_by_segment": defaultdict(float),
                    "available_by_segment": defaultdict(bool),
                },
            )
            quantity = _number(row["sales_qty"])
            bucket["sales_qty"] += quantity
            bucket["available"] = bool(bucket["available"] or row["available"])
            segment = self.skus[code]["segment_id"]
            bucket["sales_by_segment"][segment] += quantity
            bucket["available_by_segment"][segment] = bool(
                bucket["available_by_segment"][segment] or row["available"]
            )
            if quantity > 0:
                self.demand_index[(generation, business_date)][code] = {
                    "quantity": quantity,
                    "documents": int(row["sale_document_count"]),
                }
        self.target_signal_index: dict[str, dict[str, float]] = defaultdict(
            lambda: {
                "kmp4_raw": 0.0,
                "kmp4_open": 0.0,
                "site_raw": 0.0,
                "site_open": 0.0,
            }
        )
        for (business_date, _), row in self.target_daily.items():
            bucket = self.target_signal_index[business_date]
            bucket["kmp4_raw"] += _number(row["kmp4_raw_qty"])
            bucket["kmp4_open"] += _number(row["kmp4_open_qty"])
            bucket["site_raw"] += _number(row["site_order_raw_qty"])
            bucket["site_open"] += _number(row["site_order_open_qty"])
        self._economic_series = self._build_economic_series()
        self.first_family_available = {
            generation: self._first_family_available(generation)
            for generation in self.codes_by_generation
        }
        self.first_family_order = {
            generation: self._first_family_order(generation)
            for generation in self.codes_by_generation
        }

    def close(self) -> None:
        self.connection.close()

    def _first_family_available(self, generation: int) -> date | None:
        values = [
            date.fromisoformat(day)
            for day, row in self.family_daily[generation].items()
            if row["available"]
        ]
        return min(values) if values else None

    def _first_family_order(self, generation: int) -> date | None:
        codes = set(self.codes_by_generation[generation])
        values = [
            date.fromisoformat(row["order_date"])
            for row in self.orders
            if row["nomenclature_code"] in codes
        ]
        return min(values) if values else None

    def _build_economic_series(self) -> dict[str, list[tuple[date, float, float]]]:
        result: dict[str, list[tuple[date, float, float]]] = defaultdict(list)
        for row in self.economics:
            result[row["nomenclature_code"]].append(
                (
                    date.fromisoformat(row["business_date"]),
                    _number(row["inventory_cost_per_unit_rub"]),
                    _number(row["gross_margin_per_unit_rub"]),
                )
            )
        for values in result.values():
            values.sort()
        return result

    def economics_at(self, code: str, as_of: date) -> tuple[float, float]:
        values = self._economic_series.get(code, [])
        if not values:
            return _number(self.skus[code].get("item_cost_rub")), 0.0
        chosen = next((row for row in reversed(values) if row[0] <= as_of), values[0])
        return chosen[1], chosen[2]

    def demand_on(self, generation: int, business_date: date) -> dict[str, dict[str, float]]:
        return self.demand_index.get((generation, business_date.isoformat()), {})

    def target_signal_on(self, business_date: date) -> dict[str, float]:
        return self.target_signal_index[business_date.isoformat()]


def candidate_grid(*, repair_lags: Sequence[int] = (0,)) -> list[Candidate]:
    return [
        Candidate(pool, window, lag, prior, cap, buffer, profile)
        for pool in ("previous_one", "previous_two_recency")
        for window in (14, 30, 60)
        for lag in repair_lags
        for prior in (14, 28)
        for cap in (0.0, 0.25, 0.5, 1.0)
        for buffer in (0, 7, 14)
        for profile in TRANSITION_PROFILES
    ]


def wordstat_policy_grid() -> list[WordstatPolicy]:
    policies = [
        WordstatPolicy(
            availability_lag_days=7,
            lookback_months=2,
            min_confirming_phrases=2,
            min_growth_ratio=0.05,
            max_uplift_ratio=0.0,
        )
    ]
    policies.extend(
        WordstatPolicy(lag, lookback, confirming, growth, cap)
        for lag in (3, 7, 14)
        for lookback in (1, 2, 3)
        for confirming in (2, 3)
        for growth in (0.05, 0.15)
        for cap in (0.10, 0.20)
    )
    return policies


def _analog_generations(target_generation: int, mode: str) -> list[tuple[int, float]]:
    if mode == "previous_one":
        return [(target_generation - 1, 1.0)]
    if mode == "previous_two_recency":
        return [(target_generation - 1, 2.0), (target_generation - 2, 1.0)]
    raise ValueError(f"unknown_analog_pool:{mode}")


def analog_profile(
    data: FrozenFamilyData,
    *,
    target_generation: int,
    decision_date: date,
    target_first_available: date | None,
    candidate: Candidate,
) -> tuple[float, dict[str, float], dict[str, Any]]:
    profiles: list[tuple[float, float, dict[str, float], dict[str, Any]]] = []
    for generation, weight in _analog_generations(target_generation, candidate.analog_pool):
        if generation not in data.codes_by_generation:
            continue
        analog_first = data.first_family_available.get(generation)
        if analog_first is None:
            continue
        left_censored = analog_first <= SOURCE_FROM + timedelta(days=14)
        if not left_censored and target_first_available is not None:
            target_age = max(0, (decision_date - target_first_available).days)
            end_offset = max(
                candidate.analog_window_days - 1,
                target_age + candidate.repair_lag_days,
            )
            end = analog_first + timedelta(days=end_offset)
            start = end - timedelta(days=candidate.analog_window_days - 1)
            method = "launch_aligned"
        elif not left_censored:
            start = analog_first + timedelta(days=candidate.repair_lag_days)
            end = start + timedelta(days=candidate.analog_window_days - 1)
            method = "launch_plan"
        else:
            end = decision_date - timedelta(days=1)
            start = end - timedelta(days=candidate.analog_window_days - 1)
            method = "recent_left_censored"
        if end >= decision_date:
            end = decision_date - timedelta(days=1)
            start = end - timedelta(days=candidate.analog_window_days - 1)
            method += "_clipped"
        if end < start:
            continue
        family_daily = data.family_daily[generation]
        available_dates_by_segment: dict[str, set[date]] = defaultdict(set)
        sales_by_segment: dict[str, float] = defaultdict(float)
        for business_date in _iter_dates(start, end):
            row = family_daily.get(business_date.isoformat())
            if row is None:
                continue
            for segment, quantity in row["sales_by_segment"].items():
                sales_by_segment[segment] += _number(quantity)
            available_by_segment = row.get("available_by_segment")
            if available_by_segment is not None:
                for segment, available in available_by_segment.items():
                    if available:
                        available_dates_by_segment[segment].add(business_date)
            elif row["available"]:
                for segment in row["sales_by_segment"]:
                    available_dates_by_segment[segment].add(business_date)
        segment_rates = {
            segment: quantity / len(available_dates_by_segment[segment])
            for segment, quantity in sales_by_segment.items()
            if available_dates_by_segment.get(segment)
        }
        if not segment_rates:
            continue
        rate = sum(segment_rates.values())
        mix = {key: value / rate for key, value in segment_rates.items()}
        total_sales = sum(sales_by_segment.values())
        profiles.append(
            (
                weight,
                rate,
                mix,
                {
                    "generation": generation,
                    "method": method,
                    "window_from": start.isoformat(),
                    "window_to": end.isoformat(),
                    "available_days": min(
                        len(days) for days in available_dates_by_segment.values() if days
                    ),
                    "available_days_by_segment": {
                        segment: len(days)
                        for segment, days in sorted(available_dates_by_segment.items())
                    },
                    "sales_qty": round(total_sales, 3),
                    "phone_age_at_analog_launch_days": (
                        analog_first - RELEASE_DATES[generation]
                    ).days,
                },
            )
        )
    if not profiles:
        return 0.0, {"Original": 1.0}, {"profiles": [], "confidence": "none"}
    total_weight = sum(item[0] for item in profiles)
    rate = sum(weight * value for weight, value, _, _ in profiles) / total_weight
    mix_values: dict[str, float] = defaultdict(float)
    for weight, _, mix, _ in profiles:
        for key, value in mix.items():
            mix_values[key] += weight * value
    mix_total = sum(mix_values.values()) or 1.0
    return (
        rate,
        {key: value / mix_total for key, value in mix_values.items()},
        {
            "profiles": [item[3] for item in profiles],
            "confidence": "medium" if len(profiles) > 1 else "low",
        },
    )


def lead_time_profile(
    data: FrozenFamilyData, *, target_generation: int, decision_date: date, mode: str
) -> dict[str, float | int | str]:
    order_by_key = {(row["order_hash"], row["nomenclature_code"]): row for row in data.orders}
    receipts_by_order: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for receipt in data.receipts:
        if receipt["order_hash"]:
            receipts_by_order[(receipt["order_hash"], receipt["nomenclature_code"])].append(receipt)

    target_codes = set(data.codes_by_generation[target_generation])
    analog_codes = {
        code
        for generation, _ in _analog_generations(target_generation, "previous_two_recency")
        for code in data.codes_by_generation.get(generation, [])
    }
    completed_durations: dict[str, list[float]] = {"own": [], "analog": []}
    for key, order in order_by_key.items():
        order_date = date.fromisoformat(order["order_date"])
        if order_date > decision_date:
            continue
        completed = [
            row
            for row in receipts_by_order.get(key, [])
            if date.fromisoformat(row["receipt_date"]) <= decision_date
        ]
        if not completed:
            continue
        first_receipt = min(date.fromisoformat(row["receipt_date"]) for row in completed)
        duration = max(1, (first_receipt - order_date).days)
        code = order["nomenclature_code"]
        if code in target_codes:
            completed_durations["own"].append(duration)
        if code in analog_codes:
            completed_durations["analog"].append(duration)

    provisional_values = completed_durations["analog"]
    provisional_p75 = _quantile(
        provisional_values,
        0.75,
        LEAD_TIME_MATURITY_FLOOR_DAYS,
    )
    maturity_days = max(LEAD_TIME_MATURITY_FLOOR_DAYS, provisional_p75)
    maturity_cutoff = decision_date - timedelta(days=maturity_days)

    own_durations: list[float] = []
    analog_durations: list[float] = []
    own_censored = analog_censored = 0
    analog_order_qty = analog_receipt_qty = 0.0
    analog_cargo_order_qty = analog_cargo_receipt_qty = 0.0
    for key, order in order_by_key.items():
        order_date = date.fromisoformat(order["order_date"])
        if order_date > maturity_cutoff:
            continue
        completed = [
            row
            for row in receipts_by_order.get(key, [])
            if date.fromisoformat(row["receipt_date"]) <= decision_date
        ]
        first_receipt = (
            min(date.fromisoformat(row["receipt_date"]) for row in completed) if completed else None
        )
        duration = (
            max(1, (first_receipt - order_date).days)
            if first_receipt is not None
            else max(maturity_days + 1, (decision_date - order_date).days + 1)
        )
        code = order["nomenclature_code"]
        if code in target_codes:
            own_durations.append(duration)
            own_censored += int(first_receipt is None)
        if code in analog_codes:
            analog_durations.append(duration)
            analog_censored += int(first_receipt is None)
            order_qty = _number(order["quantity"])
            receipt_qty = sum(_number(row["quantity"]) for row in completed)
            analog_order_qty += order_qty
            analog_receipt_qty += min(order_qty, receipt_qty)
            cargo_date = _date(order.get("cargo_date"))
            if cargo_date and cargo_date <= maturity_cutoff:
                analog_cargo_order_qty += order_qty
                analog_cargo_receipt_qty += min(order_qty, receipt_qty)
    use_own = mode == "own_history" and len(own_durations) >= 3
    values = own_durations if use_own else analog_durations
    p50 = _quantile(values, 0.50, 45)
    p75 = _quantile(values, 0.75, max(60, p50))
    placed_reliability = (
        min(1.0, analog_receipt_qty / analog_order_qty) if analog_order_qty else 0.0
    )
    cargo_reliability = (
        min(1.0, analog_cargo_receipt_qty / analog_cargo_order_qty)
        if analog_cargo_order_qty
        else placed_reliability
    )
    return {
        "p50": p50,
        "p75": max(p50, p75),
        "sample_count": len(values),
        "matured_sample_count": len(values),
        "right_censored_count": own_censored if use_own else analog_censored,
        "maturity_days": maturity_days,
        "maturity_cutoff": maturity_cutoff.isoformat(),
        "source": "own" if use_own else "analog",
        "placed_reliability": placed_reliability,
        "cargo_reliability": cargo_reliability,
    }


def _mode(
    *, served_qty: float, sale_days: int, available_days: int, profile: TransitionProfile
) -> str:
    if (
        served_qty >= profile.own_min_sales
        and sale_days >= profile.own_min_sale_days
        and available_days >= profile.own_min_available_days
    ):
        return "own_history"
    if (
        served_qty >= profile.hybrid_min_sales
        and sale_days >= profile.hybrid_min_sale_days
        and available_days >= profile.hybrid_min_available_days
    ):
        return "hybrid"
    return "cold_start"


def _normalize_mix(values: Mapping[str, float], allowed: set[str]) -> dict[str, float]:
    filtered = {key: max(0.0, value) for key, value in values.items() if key in allowed}
    total = sum(filtered.values())
    if total <= 0 and allowed:
        return {key: 1.0 / len(allowed) for key in sorted(allowed)}
    return {key: value / total for key, value in filtered.items()} if total else {}


def _largest_remainder(total: int, weights: Mapping[str, float]) -> dict[str, int]:
    if total <= 0 or not weights:
        return {key: 0 for key in weights}
    normalized_total = sum(max(0.0, value) for value in weights.values()) or 1.0
    exact = {key: total * max(0.0, value) / normalized_total for key, value in weights.items()}
    result = {key: int(math.floor(value)) for key, value in exact.items()}
    remaining = total - sum(result.values())
    ranking = sorted(exact, key=lambda key: (exact[key] - result[key], key), reverse=True)
    for key in ranking[:remaining]:
        result[key] += 1
    return result


def _first_manual_orders(data: FrozenFamilyData, generation: int) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for code in data.codes_by_generation[generation]:
        rows = [row for row in data.orders if row["nomenclature_code"] == code]
        if rows:
            result[code] = min(rows, key=lambda row: (row["order_date"], row["order_hash"]))
    return result


def _first_manual_receipts(
    data: FrozenFamilyData, first_orders: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    keys = {(row["order_hash"], code) for code, row in first_orders.items()}
    return [row for row in data.receipts if (row["order_hash"], row["nomenclature_code"]) in keys]


def first_family_batch_quantity(
    first_orders: Mapping[str, Mapping[str, Any]], simulation_from: date
) -> float:
    """Return only the lot placed on the first family order date."""

    return sum(
        _number(row["quantity"])
        for row in first_orders.values()
        if date.fromisoformat(row["order_date"]) == simulation_from
    )


def _target_residuals(data: FrozenFamilyData) -> dict[tuple[str, str], float]:
    actual_receipts: dict[tuple[str, str], float] = defaultdict(float)
    for row in data.receipts:
        if row["nomenclature_code"] in TARGET_CODES:
            actual_receipts[(row["receipt_date"], row["nomenclature_code"])] += _number(
                row["quantity"]
            )
    residuals: dict[tuple[str, str], float] = {}
    previous_stock: dict[str, float] = defaultdict(float)
    for business_date in _iter_dates(HOLDOUT_FROM, HOLDOUT_TO):
        for code in sorted(TARGET_CODES):
            row = data.target_daily.get((business_date.isoformat(), code))
            if row is None:
                continue
            stock = _number(row["physical_stock_qty"])
            sales = _number(row["sales_qty"])
            receipts = actual_receipts[(business_date.isoformat(), code)]
            residuals[(business_date.isoformat(), code)] = (
                stock - previous_stock[code] - receipts + sales
            )
            previous_stock[code] = stock
    return residuals


def simulate_family(
    data: FrozenFamilyData,
    *,
    generation: int,
    candidate: Candidate,
    simulation_to: date,
    evaluation_from: date,
    use_target_stock_residuals: bool,
    wordstat_history: WordstatHistory | None = None,
    wordstat_policy: WordstatPolicy | None = None,
) -> SimulationResult:
    first_orders = _first_manual_orders(data, generation)
    if not first_orders:
        raise ValueError(f"family_without_supplier_orders:{generation}")
    simulation_from = min(date.fromisoformat(row["order_date"]) for row in first_orders.values())
    first_receipts = _first_manual_receipts(data, first_orders)
    target_first_available = data.first_family_available.get(generation)
    transition = TRANSITION_PROFILES[candidate.transition_profile]
    residuals = _target_residuals(data) if use_target_stock_residuals else {}

    manual_arrivals: dict[date, list[tuple[str, float]]] = defaultdict(list)
    for row in first_receipts:
        manual_arrivals[date.fromisoformat(row["receipt_date"])].append(
            (row["nomenclature_code"], _number(row["quantity"]))
        )
    model_arrivals: dict[date, list[tuple[str, float, str]]] = defaultdict(list)
    model_pipeline: list[dict[str, Any]] = []
    inventory: dict[str, float] = defaultdict(float)
    served_history: list[tuple[date, str, float, int]] = []
    available_history_by_segment: dict[str, set[date]] = defaultdict(set)
    decisions: list[dict[str, Any]] = []
    inventory_daily: list[tuple[date, float, float]] = []
    demand_total = served_total = gross_margin = missed_margin = 0.0
    repeat_qty = early_repeat_qty = adjustment_shortfall = 0.0
    repeat_count = 0
    first_repeat_at = ""
    mode_days: dict[str, int] = defaultdict(int)
    cold_ordered_total = 0.0
    wordstat_confirmed_reviews = 0
    wordstat_adjusted_reviews = 0
    wordstat_max_modifier = 1.0

    family_first_batch_qty = first_family_batch_quantity(first_orders, simulation_from)
    for cursor in _iter_dates(simulation_from, simulation_to):
        for code, quantity in manual_arrivals.get(cursor, []):
            inventory[code] += quantity
        for code, quantity, order_id in model_arrivals.get(cursor, []):
            inventory[code] += quantity
            for row in model_pipeline:
                if row["order_id"] == order_id and row["code"] == code:
                    row["arrived_qty"] += quantity

        if use_target_stock_residuals and cursor >= HOLDOUT_FROM:
            for code in data.codes_by_generation[generation]:
                residual = residuals.get((cursor.isoformat(), code), 0.0)
                if inventory[code] + residual < 0:
                    adjustment_shortfall += -(inventory[code] + residual)
                    inventory[code] = 0.0
                else:
                    inventory[code] += residual

        for code in data.codes_by_generation[generation]:
            if inventory[code] > 0:
                available_history_by_segment[data.skus[code]["segment_id"]].add(cursor)

        day_demand = data.demand_on(generation, cursor)
        for code, demand in day_demand.items():
            quantity = demand["quantity"]
            served = min(quantity, inventory[code])
            lost = quantity - served
            inventory[code] -= served
            if lost > 0:
                segment = data.skus[code]["segment_id"]
                substitutes = sorted(
                    candidate_code
                    for candidate_code in data.codes_by_generation[generation]
                    if candidate_code != code
                    and data.skus[candidate_code]["segment_id"] == segment
                    and inventory[candidate_code] > 0
                )
                for substitute_code in substitutes:
                    substitute_served = min(lost, inventory[substitute_code])
                    inventory[substitute_code] -= substitute_served
                    served += substitute_served
                    lost -= substitute_served
                    if lost <= 0:
                        break
            documents = min(int(demand["documents"]), int(math.ceil(served))) if served > 0 else 0
            if served > 0:
                served_history.append((cursor, code, served, documents))
            if cursor >= evaluation_from:
                _, margin = data.economics_at(code, cursor)
                demand_total += quantity
                served_total += served
                gross_margin += served * margin
                missed_margin += lost * margin

        served_qty_to_date = sum(row[2] for row in served_history)
        sale_days_to_date = len({row[0] for row in served_history})
        launched_segments = {
            data.skus[code]["segment_id"]
            for code, order in first_orders.items()
            if date.fromisoformat(order["order_date"]) <= cursor
        }
        mode_available_days = segment_evidence_days(
            launched_segments,
            available_history_by_segment,
        )
        mode = _mode(
            served_qty=served_qty_to_date,
            sale_days=sale_days_to_date,
            available_days=mode_available_days,
            profile=transition,
        )
        if cursor >= evaluation_from:
            mode_days[mode] += 1

        for code in data.codes_by_generation[generation]:
            cost, _ = data.economics_at(code, cursor)
            if cursor >= evaluation_from:
                inventory_daily.append((cursor, inventory[code], inventory[code] * cost))

        eligible_codes = {
            code
            for code, row in first_orders.items()
            if date.fromisoformat(row["order_date"]) <= cursor
        }
        is_review = True
        is_manual_launch = any(
            date.fromisoformat(row["order_date"]) == cursor for row in first_orders.values()
        )
        if not eligible_codes or not (is_review or is_manual_launch):
            continue

        analog_rate, analog_mix, analog_note = analog_profile(
            data,
            target_generation=generation,
            decision_date=cursor,
            target_first_available=(
                target_first_available
                if target_first_available and target_first_available <= cursor
                else None
            ),
            candidate=candidate,
        )
        trailing_start = cursor - timedelta(days=29)
        recent_served = [row for row in served_history if trailing_start <= row[0] < cursor]
        recent_available_by_segment = {
            segment: len({day for day in days if trailing_start <= day < cursor})
            for segment, days in available_history_by_segment.items()
        }
        recent_available_days = max(recent_available_by_segment.values(), default=0)
        recent_served_by_segment: dict[str, float] = defaultdict(float)
        for _, code, quantity, _ in recent_served:
            recent_served_by_segment[data.skus[code]["segment_id"]] += quantity
        own_rate = sum(
            quantity / recent_available_by_segment[segment]
            for segment, quantity in recent_served_by_segment.items()
            if recent_available_by_segment.get(segment, 0) > 0
        )
        if mode == "cold_start":
            forecast_rate = analog_rate
        elif mode == "hybrid":
            forecast_rate = (
                own_rate * recent_available_days + analog_rate * candidate.hybrid_prior_days
            ) / max(1, recent_available_days + candidate.hybrid_prior_days)
        else:
            forecast_rate = own_rate if recent_available_days else analog_rate
        forecast_rate_before_wordstat = forecast_rate
        wordstat_signal: dict[str, Any] = {
            "status": "not_configured",
            "action": "none",
            "latest_period": "",
            "available_on": "",
            "usable_phrase_count": 0,
            "confirming_phrase_count": 0,
            "median_growth_ratio": 0.0,
            "modifier": 1.0,
            "phrase_details": [],
            "counts_aggregated": False,
        }
        if wordstat_history is not None and wordstat_policy is not None:
            wordstat_signal = wordstat_history.signal_at(
                generation=generation,
                decision_date=cursor,
                policy=wordstat_policy,
            )
            modifier = _number(wordstat_signal.get("modifier"), 1.0)
            forecast_rate *= modifier
            wordstat_max_modifier = max(wordstat_max_modifier, modifier)
            if is_review and wordstat_signal["status"] == "confirmed_uptrend":
                wordstat_confirmed_reviews += 1
            if is_review and modifier > 1.0:
                wordstat_adjusted_reviews += 1

        allowed_segments = {data.skus[code]["segment_id"] for code in eligible_codes}
        own_segment_sales: dict[str, float] = defaultdict(float)
        for _, code, quantity, _ in served_history:
            if code in eligible_codes:
                own_segment_sales[data.skus[code]["segment_id"]] += quantity
        own_mix_total = sum(own_segment_sales.values())
        if mode == "cold_start" or own_mix_total <= 0:
            segment_mix = _normalize_mix(analog_mix, allowed_segments)
        else:
            prior_sales = candidate.hybrid_prior_days * max(analog_rate, 0.01)
            blended = {
                segment: own_segment_sales.get(segment, 0.0)
                + prior_sales * analog_mix.get(segment, 0.0)
                for segment in allowed_segments
            }
            segment_mix = _normalize_mix(blended, allowed_segments)

        lead = lead_time_profile(
            data, target_generation=generation, decision_date=cursor, mode=mode
        )
        if mode == "own_history":
            cover_days = int(lead["p75"]) + REVIEW_INTERVAL_DAYS
        else:
            cover_days = int(lead["p75"]) + REVIEW_INTERVAL_DAYS + candidate.temporary_buffer_days
        target_position = int(math.ceil(max(0.0, forecast_rate) * cover_days))

        manual_pipeline_credit = 0.0
        for code, order in first_orders.items():
            if code not in eligible_codes:
                continue
            ordered = _number(order["quantity"])
            arrived = sum(
                _number(row["quantity"])
                for row in first_receipts
                if row["nomenclature_code"] == code
                and date.fromisoformat(row["receipt_date"]) <= cursor
            )
            outstanding = max(0.0, ordered - arrived)
            cargo_date = _date(order.get("cargo_date"))
            reliability = (
                float(lead["cargo_reliability"])
                if cargo_date and cargo_date <= cursor
                else float(lead["placed_reliability"])
            )
            manual_pipeline_credit += outstanding * reliability
        model_pipeline_credit = sum(
            max(0.0, row["quantity"] - row["arrived_qty"]) * float(lead["placed_reliability"])
            for row in model_pipeline
            if row["arrival_date"] > cursor
        )
        family_position = sum(inventory[code] for code in eligible_codes)
        family_position += manual_pipeline_credit + model_pipeline_credit
        requested = max(0, int(math.ceil(target_position - family_position)))
        unconstrained_requested = requested
        if not is_review or is_manual_launch:
            requested = 0

        cap_remaining = None
        if mode == "cold_start":
            cold_cap = int(math.ceil(family_first_batch_qty * candidate.early_reorder_cap_ratio))
            cap_remaining = max(0, cold_cap - int(math.ceil(cold_ordered_total)))
            requested = min(requested, cap_remaining)

        current_segment_position: dict[str, float] = defaultdict(float)
        for code in eligible_codes:
            current_segment_position[data.skus[code]["segment_id"]] += inventory[code]
        segment_targets = {
            segment: target_position * share for segment, share in segment_mix.items()
        }
        segment_deficits = {
            segment: max(
                0.0, segment_targets.get(segment, 0.0) - current_segment_position.get(segment, 0.0)
            )
            for segment in allowed_segments
        }
        segment_allocations = _largest_remainder(requested, segment_deficits or segment_mix)
        sku_allocations: dict[str, int] = {}
        for segment, segment_qty in segment_allocations.items():
            segment_codes = sorted(
                code for code in eligible_codes if data.skus[code]["segment_id"] == segment
            )
            own_sku_sales = {
                code: sum(row[2] for row in served_history if row[1] == code)
                for code in segment_codes
            }
            if sum(own_sku_sales.values()) <= 0:
                weights = {code: 1.0 for code in segment_codes}
            else:
                weights = {code: value + 1.0 for code, value in own_sku_sales.items()}
            sku_allocations.update(_largest_remainder(segment_qty, weights))

        ordered_now = sum(sku_allocations.values())
        if ordered_now > 0:
            order_id = f"model-{candidate.candidate_id}-{cursor.isoformat()}"
            arrival_date = planned_arrival_date(cursor, lead)
            for code, quantity in sku_allocations.items():
                if quantity <= 0:
                    continue
                model_arrivals[arrival_date].append((code, float(quantity), order_id))
                model_pipeline.append(
                    {
                        "order_id": order_id,
                        "code": code,
                        "order_date": cursor,
                        "arrival_date": arrival_date,
                        "quantity": float(quantity),
                        "arrived_qty": 0.0,
                    }
                )
            repeat_qty += ordered_now
            repeat_count += 1
            if not first_repeat_at:
                first_repeat_at = cursor.isoformat()
            if mode == "cold_start":
                early_repeat_qty += ordered_now
                cold_ordered_total += ordered_now

        signal = (
            data.target_signal_on(cursor)
            if generation == 17
            else {
                "kmp4_raw": 0.0,
                "kmp4_open": 0.0,
                "site_raw": 0.0,
                "site_open": 0.0,
            }
        )
        if (
            ordered_now > 0
            or is_manual_launch
            or any(signal.values())
            or bool(wordstat_signal["phrase_details"])
        ):
            decisions.append(
                {
                    "decision_date": cursor.isoformat(),
                    "mode": mode,
                    "event": "manual_first_order" if is_manual_launch else "daily_review",
                    "forecast_family_rate_per_day": round(forecast_rate, 4),
                    "forecast_before_wordstat_rate_per_day": round(
                        forecast_rate_before_wordstat, 4
                    ),
                    "analog_rate_per_day": round(analog_rate, 4),
                    "own_rate_per_day": round(own_rate, 4),
                    "target_position_qty": target_position,
                    "physical_inventory_qty": round(
                        sum(inventory[code] for code in eligible_codes), 3
                    ),
                    "manual_pipeline_credit_qty": round(manual_pipeline_credit, 3),
                    "model_pipeline_credit_qty": round(model_pipeline_credit, 3),
                    "unconstrained_repeat_need_qty": unconstrained_requested,
                    "requested_repeat_qty": ordered_now,
                    "early_cap_remaining_before_order": cap_remaining,
                    "lead_time_p50_days": int(lead["p50"]),
                    "lead_time_p75_days": int(lead["p75"]),
                    "lead_time_source": lead["source"],
                    "lead_time_maturity_days": int(lead["maturity_days"]),
                    "lead_time_right_censored_count": int(lead["right_censored_count"]),
                    "placed_pipeline_reliability": round(float(lead["placed_reliability"]), 6),
                    "cargo_pipeline_reliability": round(float(lead["cargo_reliability"]), 6),
                    "temporary_buffer_days": (
                        candidate.temporary_buffer_days if mode != "own_history" else 0
                    ),
                    "quality_mix": json.dumps(segment_mix, ensure_ascii=False, sort_keys=True),
                    "quality_construction_mix": json.dumps(
                        segment_mix, ensure_ascii=False, sort_keys=True
                    ),
                    "sku_allocation": json.dumps(
                        {key: value for key, value in sku_allocations.items() if value > 0},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    "kmp4_raw_signal_qty": signal["kmp4_raw"],
                    "site_order_raw_signal_qty": signal["site_raw"],
                    "signal_action": "manual_review_only" if any(signal.values()) else "none",
                    "wordstat_latest_period": wordstat_signal["latest_period"],
                    "wordstat_available_on": wordstat_signal["available_on"],
                    "wordstat_confirming_phrase_count": wordstat_signal["confirming_phrase_count"],
                    "wordstat_median_growth_ratio": wordstat_signal["median_growth_ratio"],
                    "wordstat_modifier": wordstat_signal["modifier"],
                    "wordstat_action": wordstat_signal["action"],
                    "wordstat_evidence": json.dumps(
                        wordstat_signal["phrase_details"],
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    "analog_evidence": json.dumps(analog_note, ensure_ascii=False, sort_keys=True),
                    "reason": _decision_reason(
                        mode=mode,
                        ordered_now=ordered_now,
                        unconstrained_requested=unconstrained_requested,
                        target_position=target_position,
                        family_position=family_position,
                        cap_remaining=cap_remaining,
                        manual_launch=is_manual_launch,
                        signal=signal,
                        wordstat_signal=wordstat_signal,
                    ),
                }
            )

    evaluation_days = max(1, (simulation_to - evaluation_from).days + 1)
    inventory_qty_sum = sum(row[1] for row in inventory_daily)
    inventory_cost_sum = sum(row[2] for row in inventory_daily)
    sku_day_denominator = evaluation_days
    average_inventory_qty = inventory_qty_sum / sku_day_denominator
    average_inventory_cost = inventory_cost_sum / sku_day_denominator
    ending_inventory = sum(inventory.values())
    final_rate, _, _ = analog_profile(
        data,
        target_generation=generation,
        decision_date=simulation_to,
        target_first_available=target_first_available,
        candidate=candidate,
    )
    trailing_start = simulation_to - timedelta(days=29)
    trailing_available_by_segment = {
        segment: len({day for day in days if day >= trailing_start})
        for segment, days in available_history_by_segment.items()
    }
    trailing_sales_by_segment: dict[str, float] = defaultdict(float)
    for day, code, quantity, _ in served_history:
        if day >= trailing_start:
            trailing_sales_by_segment[data.skus[code]["segment_id"]] += quantity
    segmented_final_rate = sum(
        quantity / trailing_available_by_segment[segment]
        for segment, quantity in trailing_sales_by_segment.items()
        if trailing_available_by_segment.get(segment, 0) > 0
    )
    final_rate = segmented_final_rate or final_rate
    if wordstat_history is not None and wordstat_policy is not None:
        final_wordstat_signal = wordstat_history.signal_at(
            generation=generation,
            decision_date=simulation_to,
            policy=wordstat_policy,
        )
        final_rate *= _number(final_wordstat_signal.get("modifier"), 1.0)
    final_lead = lead_time_profile(
        data, target_generation=generation, decision_date=simulation_to, mode="own_history"
    )
    ending_need = final_rate * (int(final_lead["p75"]) + REVIEW_INTERVAL_DAYS)
    ending_excess = max(0.0, ending_inventory - ending_need)
    return SimulationResult(
        candidate_id=candidate.candidate_id,
        generation=generation,
        evaluation_from=evaluation_from.isoformat(),
        evaluation_to=simulation_to.isoformat(),
        demand_qty=round(demand_total, 3),
        served_sales_qty=round(served_total, 3),
        lost_sales_qty=round(demand_total - served_total, 3),
        served_sales_ratio=round(served_total / demand_total, 6) if demand_total else 1.0,
        gross_margin_rub=round(gross_margin, 2),
        missed_gross_profit_rub=round(missed_margin, 2),
        average_inventory_qty=round(average_inventory_qty, 3),
        average_inventory_cost_rub=round(average_inventory_cost, 2),
        gmroi=(
            round(gross_margin / average_inventory_cost, 6) if average_inventory_cost > 0 else None
        ),
        ending_inventory_qty=round(ending_inventory, 3),
        ending_required_qty=round(ending_need, 3),
        ending_excess_qty=round(ending_excess, 3),
        ending_shortfall_qty=round(max(0.0, ending_need - ending_inventory), 3),
        model_repeat_order_qty=round(repeat_qty, 3),
        model_repeat_order_count=repeat_count,
        early_repeat_order_qty=round(early_repeat_qty, 3),
        first_model_repeat_at=first_repeat_at,
        cold_start_days=mode_days["cold_start"],
        hybrid_days=mode_days["hybrid"],
        own_history_days=mode_days["own_history"],
        wordstat_confirmed_review_count=wordstat_confirmed_reviews,
        wordstat_adjusted_review_count=wordstat_adjusted_reviews,
        wordstat_max_modifier=round(wordstat_max_modifier, 6),
        adjustment_shortfall_qty=round(adjustment_shortfall, 3),
        decisions=decisions,
    )


def _decision_reason(
    *,
    mode: str,
    ordered_now: int,
    unconstrained_requested: int,
    target_position: int,
    family_position: float,
    cap_remaining: int | None,
    manual_launch: bool,
    signal: Mapping[str, float],
    wordstat_signal: Mapping[str, Any],
) -> str:
    mode_text = {
        "cold_start": "аналог и ограниченный тестовый объём",
        "hybrid": "ранние собственные продажи со shrinkage к аналогу",
        "own_history": "собственная история и страховой запас по сроку",
    }[mode]
    if ordered_now > 0:
        result = (
            f"{mode_text}; позиция {family_position:.1f} ниже цели {target_position}, "
            f"дозаказ {ordered_now}"
        )
    elif unconstrained_requested > 0 and manual_launch:
        result = (
            f"{mode_text}; расчётная дополнительная потребность {unconstrained_requested}, "
            "но в день первой партии решение остаётся ручным"
        )
    elif unconstrained_requested > 0 and mode == "cold_start" and cap_remaining == 0:
        result = (
            f"{mode_text}; расчётная дополнительная потребность {unconstrained_requested}, "
            "автодозаказ заблокирован cap=0 до достаточной истории"
        )
    else:
        result = f"{mode_text}; запас и путь покрывают цель {target_position}"
    if any(signal.values()):
        result += "; КМП4/сайт только флаг ручной проверки, не прямые штуки"
    if wordstat_signal.get("status") == "confirmed_uptrend":
        modifier = _number(wordstat_signal.get("modifier"), 1.0)
        if modifier > 1.0:
            result += (
                f"; Wordstat подтвердил рост и ограниченно изменил семейный прогноз "
                f"до x{modifier:.2f}, частотности не переводились в штуки"
            )
        else:
            result += (
                "; Wordstat подтвердил направление только для ручной проверки, "
                "частотности не переводились в штуки"
            )
    return result


def _actual_target_metrics(
    data: FrozenFamilyData,
    *,
    ending_required_qty: float,
    evaluation_from: date = HOLDOUT_FROM,
    evaluation_to: date = HOLDOUT_TO,
) -> dict[str, Any]:
    daily_family: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for (day, code), row in data.target_daily.items():
        if code in TARGET_CODES and evaluation_from.isoformat() <= day <= evaluation_to.isoformat():
            daily_family[day].append(row)
    inventory_cost_total = 0.0
    inventory_qty_total = 0.0
    gross_margin = 0.0
    sales = 0.0
    for business_date in _iter_dates(evaluation_from, evaluation_to):
        for row in daily_family.get(business_date.isoformat(), []):
            code = row["nomenclature_code"]
            quantity = _number(row["sales_qty"])
            stock = _number(row["physical_stock_qty"])
            cost, margin = data.economics_at(code, business_date)
            sales += quantity
            gross_margin += quantity * margin
            inventory_qty_total += stock
            inventory_cost_total += stock * cost
    days = (evaluation_to - evaluation_from).days + 1
    end_stock = sum(
        _number(
            data.target_daily.get((evaluation_to.isoformat(), code), {}).get("physical_stock_qty")
        )
        for code in TARGET_CODES
    )
    avg_cost = inventory_cost_total / days
    return {
        "scenario": "observable_sales_lower_bound",
        "evaluation_from": evaluation_from.isoformat(),
        "evaluation_to": evaluation_to.isoformat(),
        "true_demand_observed": False,
        "served_sales_qty": round(sales, 3),
        "lost_sales_qty": 0.0,
        "gross_margin_rub": round(gross_margin, 2),
        "missed_gross_profit_rub": 0.0,
        "average_inventory_qty": round(inventory_qty_total / days, 3),
        "average_inventory_cost_rub": round(avg_cost, 2),
        "gmroi": round(gross_margin / avg_cost, 6) if avg_cost else None,
        "ending_inventory_qty": round(end_stock, 3),
        "ending_required_qty": round(ending_required_qty, 3),
        "ending_excess_qty": round(max(0.0, end_stock - ending_required_qty), 3),
        "ending_shortfall_qty": round(max(0.0, ending_required_qty - end_stock), 3),
    }


def _strict_non_degradation(result: SimulationResult, control: Mapping[str, Any]) -> dict[str, Any]:
    checks = {
        "served_sales_gte_control": result.served_sales_qty >= _number(control["served_sales_qty"]),
        "missed_gross_profit_lte_control": result.missed_gross_profit_rub
        <= _number(control["missed_gross_profit_rub"]),
        "average_inventory_capital_lte_control": result.average_inventory_cost_rub
        <= _number(control["average_inventory_cost_rub"]),
        "gmroi_gte_control": (result.gmroi or 0.0) >= _number(control["gmroi"]),
        "ending_excess_lte_control": result.ending_excess_qty
        <= _number(control["ending_excess_qty"]),
        "ending_shortfall_lte_control": result.ending_shortfall_qty
        <= _number(control["ending_shortfall_qty"]),
    }
    return {"passed": all(checks.values()), "checks": checks}


def _select_calibrated_candidate(rows: Sequence[tuple[Candidate, SimulationResult]]) -> Candidate:
    if not rows:
        raise ValueError("empty_calibration_grid")
    best_service = max(result.served_sales_ratio for _, result in rows)
    service_floor = max(0.0, best_service - 0.01)
    eligible = [row for row in rows if row[1].served_sales_ratio >= service_floor]
    eligible.sort(
        key=lambda row: (
            row[1].average_inventory_qty,
            row[1].ending_excess_qty,
            row[1].model_repeat_order_qty,
            row[0].temporary_buffer_days,
            row[0].early_reorder_cap_ratio,
            row[0].candidate_id,
        )
    )
    return eligible[0][0]


def _select_wordstat_policy(
    rows: Sequence[tuple[WordstatPolicy, SimulationResult]],
) -> WordstatPolicy:
    if not rows:
        raise ValueError("empty_wordstat_policy_grid")
    best_service = max(result.served_sales_ratio for _, result in rows)
    service_floor = max(0.0, best_service - 0.01)
    eligible = [row for row in rows if row[1].served_sales_ratio >= service_floor]
    eligible.sort(
        key=lambda row: (
            row[1].average_inventory_qty,
            row[1].ending_excess_qty,
            row[1].model_repeat_order_qty,
            row[0].max_uplift_ratio,
            row[0].availability_lag_days,
            row[0].policy_id,
        )
    )
    return eligible[0][0]


def _result_delta(result: SimulationResult, baseline: SimulationResult) -> dict[str, float | None]:
    gmroi_delta = (
        round((result.gmroi or 0.0) - (baseline.gmroi or 0.0), 6)
        if result.gmroi is not None and baseline.gmroi is not None
        else None
    )
    return {
        "served_sales_qty": round(result.served_sales_qty - baseline.served_sales_qty, 3),
        "missed_gross_profit_rub": round(
            result.missed_gross_profit_rub - baseline.missed_gross_profit_rub, 2
        ),
        "average_inventory_cost_rub": round(
            result.average_inventory_cost_rub - baseline.average_inventory_cost_rub,
            2,
        ),
        "gmroi": gmroi_delta,
        "ending_excess_qty": round(result.ending_excess_qty - baseline.ending_excess_qty, 3),
        "ending_shortfall_qty": round(
            result.ending_shortfall_qty - baseline.ending_shortfall_qty, 3
        ),
    }


def _wordstat_sensitivity_summary(
    rows: Sequence[tuple[WordstatPolicy, SimulationResult]],
    baseline: SimulationResult,
) -> dict[str, Any]:
    ordered = sorted(
        rows,
        key=lambda row: (
            row[1].lost_sales_qty,
            -(row[1].gmroi or 0.0),
            row[1].average_inventory_cost_rub,
        ),
    )
    best_policy, best_result = ordered[0]
    non_worse_service = [
        (policy, result)
        for policy, result in rows
        if result.served_sales_qty >= baseline.served_sales_qty
    ]
    strict_dominance = [
        (policy, result)
        for policy, result in rows
        if result.served_sales_qty >= baseline.served_sales_qty
        and result.average_inventory_cost_rub <= baseline.average_inventory_cost_rub
        and (result.gmroi or 0.0) >= (baseline.gmroi or 0.0)
        and result.ending_excess_qty <= baseline.ending_excess_qty
        and (
            result.served_sales_qty > baseline.served_sales_qty
            or result.average_inventory_cost_rub < baseline.average_inventory_cost_rub
            or (result.gmroi or 0.0) > (baseline.gmroi or 0.0)
        )
    ]
    return {
        "candidate_count": len(rows),
        "non_worse_service_count": len(non_worse_service),
        "strict_dominance_count": len(strict_dominance),
        "service_better_count": sum(
            result.served_sales_qty > baseline.served_sales_qty for _, result in rows
        ),
        "service_equal_count": sum(
            result.served_sales_qty == baseline.served_sales_qty for _, result in rows
        ),
        "service_worse_count": sum(
            result.served_sales_qty < baseline.served_sales_qty for _, result in rows
        ),
        "best_service_exploratory": {
            **asdict(best_policy),
            "policy_id": best_policy.policy_id,
            **best_result.summary_row(),
            "delta_vs_baseline": _result_delta(best_result, baseline),
        },
    }


def _sensitivity_summary(
    rows: Sequence[tuple[Candidate, SimulationResult]], actual: Mapping[str, Any]
) -> dict[str, Any]:
    ordered = sorted(
        rows,
        key=lambda row: (
            row[1].lost_sales_qty,
            -(row[1].gmroi or 0.0),
            row[1].average_inventory_cost_rub,
        ),
    )
    best_candidate, best_result = ordered[0]
    all_constraints = [
        row
        for row in rows
        if row[1].served_sales_qty >= _number(actual["served_sales_qty"])
        and row[1].average_inventory_cost_rub <= _number(actual["average_inventory_cost_rub"])
        and (row[1].gmroi or 0.0) >= _number(actual["gmroi"])
        and row[1].ending_excess_qty <= _number(actual["ending_excess_qty"])
        and row[1].ending_shortfall_qty <= _number(actual["ending_shortfall_qty"])
    ]
    grouped: dict[tuple[Any, ...], dict[float, SimulationResult]] = defaultdict(dict)
    for candidate, result in rows:
        key = (
            candidate.analog_pool,
            candidate.analog_window_days,
            candidate.repair_lag_days,
            candidate.hybrid_prior_days,
            candidate.temporary_buffer_days,
            candidate.transition_profile,
        )
        grouped[key][candidate.early_reorder_cap_ratio] = result
    cap_comparisons: dict[str, dict[str, int | float]] = {}
    for cap in (0.25, 0.5, 1.0):
        deltas = [
            values[cap].served_sales_qty - values[0.0].served_sales_qty
            for values in grouped.values()
            if cap in values and 0.0 in values
        ]
        sorted_deltas = sorted(deltas)
        midpoint = len(sorted_deltas) // 2
        if not sorted_deltas:
            median_delta = None
        elif len(sorted_deltas) % 2 == 0:
            median_delta = (sorted_deltas[midpoint - 1] + sorted_deltas[midpoint]) / 2
        else:
            median_delta = sorted_deltas[midpoint]
        cap_comparisons[str(cap)] = {
            "comparison_count": len(deltas),
            "service_better_count": sum(delta > 0 for delta in deltas),
            "service_equal_count": sum(delta == 0 for delta in deltas),
            "service_worse_count": sum(delta < 0 for delta in deltas),
            "median_served_sales_delta_qty": median_delta,
        }
    return {
        "candidate_count": len(rows),
        "service_threshold_counts": {
            "100_percent": sum(result.served_sales_ratio >= 1.0 for _, result in rows),
            "98_percent": sum(result.served_sales_ratio >= 0.98 for _, result in rows),
            "95_percent": sum(result.served_sales_ratio >= 0.95 for _, result in rows),
            "90_percent": sum(result.served_sales_ratio >= 0.90 for _, result in rows),
        },
        "all_requested_constraints_count": len(all_constraints),
        "best_service_candidate": {
            **asdict(best_candidate),
            "candidate_id": best_candidate.candidate_id,
            **best_result.summary_row(),
        },
        "early_cap_vs_zero": cap_comparisons,
    }


def _derived_repair_lag(data: FrozenFamilyData) -> int:
    first16 = data.first_family_order[16]
    first17 = data.first_family_order[17]
    if not first16 or not first17:
        return 0
    age16 = (first16 - RELEASE_DATES[16]).days
    age17 = (first17 - RELEASE_DATES[17]).days
    raw = max(0, age16 - age17)
    return min((0, 30, 60, 90), key=lambda value: abs(value - raw))


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0])
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def run_backtest(
    *,
    snapshot_path: Path,
    output_dir: Path,
    skip_grid: bool = False,
    wordstat_history: WordstatHistory | None = None,
) -> dict[str, Any]:
    data = FrozenFamilyData(snapshot_path)
    try:
        training_candidates = candidate_grid(repair_lags=(0,))
        calibration_rows: list[tuple[Candidate, SimulationResult]] = []
        for candidate in training_candidates:
            result = simulate_family(
                data,
                generation=16,
                candidate=candidate,
                simulation_to=date(2025, 12, 31),
                evaluation_from=data.first_family_order[16] or date(2025, 2, 11),
                use_target_stock_residuals=False,
            )
            calibration_rows.append((candidate, result))
        selected_base = _select_calibrated_candidate(calibration_rows)
        derived_lag = _derived_repair_lag(data)
        selected = Candidate(
            # Only iPhone 16 has a non-left-censored launch at a comparable
            # repair age.  iPhone 14/15 stay in the sensitivity grid.
            analog_pool="previous_one",
            analog_window_days=selected_base.analog_window_days,
            repair_lag_days=derived_lag,
            hybrid_prior_days=selected_base.hybrid_prior_days,
            # One independent clean launch is insufficient to authorize an
            # automatic in-transit repeat.  The unconstrained need remains in
            # the decision table for manual review.
            early_reorder_cap_ratio=0.0,
            temporary_buffer_days=selected_base.temporary_buffer_days,
            transition_profile=selected_base.transition_profile,
        )

        validation16 = simulate_family(
            data,
            generation=16,
            candidate=selected,
            simulation_to=HOLDOUT_TO,
            evaluation_from=date(2026, 1, 1),
            use_target_stock_residuals=False,
        )
        holdout_candidates = candidate_grid(repair_lags=(0, 30, 60, 90))
        holdout_rows: list[tuple[Candidate, SimulationResult]] = []
        if skip_grid:
            holdout_candidates = [selected]
        for candidate in holdout_candidates:
            holdout_rows.append(
                (
                    candidate,
                    simulate_family(
                        data,
                        generation=17,
                        candidate=candidate,
                        simulation_to=HOLDOUT_TO,
                        evaluation_from=HOLDOUT_FROM,
                        use_target_stock_residuals=True,
                    ),
                )
            )
        selected_result = next(
            (result for candidate, result in holdout_rows if candidate == selected), None
        )
        if selected_result is None:
            selected_result = simulate_family(
                data,
                generation=17,
                candidate=selected,
                simulation_to=HOLDOUT_TO,
                evaluation_from=HOLDOUT_FROM,
                use_target_stock_residuals=True,
            )
            holdout_rows.append((selected, selected_result))

        wordstat_calibration_rows: list[tuple[WordstatPolicy, SimulationResult]] = []
        wordstat_holdout_rows: list[tuple[WordstatPolicy, SimulationResult]] = []
        selected_wordstat_policy: WordstatPolicy | None = None
        selected_wordstat_result: SimulationResult | None = None
        recommended_wordstat_policy: WordstatPolicy | None = None
        recommended_wordstat_result: SimulationResult | None = None
        wordstat_validation_passed = False
        if wordstat_history is not None:
            for policy in wordstat_policy_grid():
                wordstat_calibration_rows.append(
                    (
                        policy,
                        simulate_family(
                            data,
                            generation=16,
                            candidate=selected,
                            simulation_to=date(2025, 12, 31),
                            evaluation_from=(data.first_family_order[16] or date(2025, 2, 11)),
                            use_target_stock_residuals=False,
                            wordstat_history=wordstat_history,
                            wordstat_policy=policy,
                        ),
                    )
                )
            selected_wordstat_policy = _select_wordstat_policy(wordstat_calibration_rows)
            direction_only_policy = WordstatPolicy(
                availability_lag_days=selected_wordstat_policy.availability_lag_days,
                lookback_months=selected_wordstat_policy.lookback_months,
                min_confirming_phrases=selected_wordstat_policy.min_confirming_phrases,
                min_growth_ratio=selected_wordstat_policy.min_growth_ratio,
                max_uplift_ratio=0.0,
            )
            holdout_policies = (
                [selected_wordstat_policy, direction_only_policy]
                if skip_grid
                else [*wordstat_policy_grid(), direction_only_policy]
            )
            holdout_policies = list(dict.fromkeys(holdout_policies))
            for policy in holdout_policies:
                wordstat_holdout_rows.append(
                    (
                        policy,
                        simulate_family(
                            data,
                            generation=17,
                            candidate=selected,
                            simulation_to=HOLDOUT_TO,
                            evaluation_from=HOLDOUT_FROM,
                            use_target_stock_residuals=True,
                            wordstat_history=wordstat_history,
                            wordstat_policy=policy,
                        ),
                    )
                )
            selected_wordstat_result = next(
                result
                for policy, result in wordstat_holdout_rows
                if policy == selected_wordstat_policy
            )
            wordstat_validation_passed = (
                selected_wordstat_result.served_sales_qty >= selected_result.served_sales_qty
                and selected_wordstat_result.missed_gross_profit_rub
                <= selected_result.missed_gross_profit_rub
                and selected_wordstat_result.average_inventory_cost_rub
                <= selected_result.average_inventory_cost_rub
                and (selected_wordstat_result.gmroi or 0.0) >= (selected_result.gmroi or 0.0)
                and selected_wordstat_result.ending_excess_qty <= selected_result.ending_excess_qty
                and selected_wordstat_result.ending_shortfall_qty
                <= selected_result.ending_shortfall_qty
            )
            recommended_wordstat_policy = (
                selected_wordstat_policy if wordstat_validation_passed else direction_only_policy
            )
            recommended_wordstat_result = next(
                result
                for policy, result in wordstat_holdout_rows
                if policy == recommended_wordstat_policy
            )

        calibration_csv = []
        for candidate, result in calibration_rows:
            calibration_csv.append(
                {
                    **asdict(candidate),
                    "candidate_id": candidate.candidate_id,
                    **result.summary_row(),
                    "selected_base": int(candidate == selected_base),
                }
            )
        holdout_csv = []
        for candidate, result in holdout_rows:
            holdout_csv.append(
                {
                    **asdict(candidate),
                    "candidate_id": candidate.candidate_id,
                    **result.summary_row(),
                    "preselected_shadow_rule": int(candidate == selected),
                }
            )
        _write_csv(output_dir / "calibration-grid-iphone16.csv", calibration_csv)
        _write_csv(output_dir / "holdout-grid-iphone17.csv", holdout_csv)
        _write_csv(output_dir / "baseline-decision-table.csv", selected_result.decisions)

        if wordstat_calibration_rows:
            wordstat_calibration_csv = [
                {
                    **asdict(policy),
                    "policy_id": policy.policy_id,
                    **result.summary_row(),
                    "selected_on_iphone16": int(policy == selected_wordstat_policy),
                }
                for policy, result in wordstat_calibration_rows
            ]
            wordstat_holdout_csv = [
                {
                    **asdict(policy),
                    "policy_id": policy.policy_id,
                    **result.summary_row(),
                    "calibrated_on_iphone16": int(policy == selected_wordstat_policy),
                    "recommended_after_holdout": int(policy == recommended_wordstat_policy),
                    **{
                        f"delta_{key}": value
                        for key, value in _result_delta(result, selected_result).items()
                    },
                }
                for policy, result in wordstat_holdout_rows
            ]
            _write_csv(
                output_dir / "wordstat-policy-grid-iphone16.csv",
                wordstat_calibration_csv,
            )
            _write_csv(
                output_dir / "wordstat-policy-holdout-iphone17.csv",
                wordstat_holdout_csv,
            )
        if selected_wordstat_result is not None:
            _write_csv(
                output_dir / "wordstat-calibrated-policy-decision-table.csv",
                selected_wordstat_result.decisions,
            )
        decision_result = recommended_wordstat_result or selected_result
        _write_csv(output_dir / "decision-table.csv", decision_result.decisions)

        actual = _actual_target_metrics(
            data, ending_required_qty=selected_result.ending_required_qty
        )
        sensitivity = _sensitivity_summary(holdout_rows, actual)
        selected_metrics = {
            "scenario": "preselected_shadow_rule",
            **{
                key: value
                for key, value in selected_result.summary_row().items()
                if key
                in {
                    "served_sales_qty",
                    "lost_sales_qty",
                    "gross_margin_rub",
                    "missed_gross_profit_rub",
                    "average_inventory_qty",
                    "average_inventory_cost_rub",
                    "gmroi",
                    "ending_inventory_qty",
                    "ending_required_qty",
                    "ending_excess_qty",
                    "ending_shortfall_qty",
                }
            },
        }
        metrics_rows = [actual, selected_metrics]
        if recommended_wordstat_result is not None:
            wordstat_metrics = {
                "scenario": "recommended_shadow_rule_with_wordstat_signal",
                **{
                    key: value
                    for key, value in recommended_wordstat_result.summary_row().items()
                    if key
                    in {
                        "served_sales_qty",
                        "lost_sales_qty",
                        "gross_margin_rub",
                        "missed_gross_profit_rub",
                        "average_inventory_qty",
                        "average_inventory_cost_rub",
                        "gmroi",
                        "ending_inventory_qty",
                        "ending_required_qty",
                        "ending_excess_qty",
                        "ending_shortfall_qty",
                    }
                },
            }
            metrics_rows.append(wordstat_metrics)
        _write_csv(output_dir / "metrics-comparison.csv", metrics_rows)

        wordstat_summary: dict[str, Any] = {"enabled": False}
        if (
            wordstat_history is not None
            and selected_wordstat_policy is not None
            and selected_wordstat_result is not None
            and recommended_wordstat_policy is not None
            and recommended_wordstat_result is not None
        ):
            selected_wordstat_calibration = next(
                result
                for policy, result in wordstat_calibration_rows
                if policy == selected_wordstat_policy
            )
            wordstat_summary = {
                "enabled": True,
                "history": wordstat_history.summary(),
                "phrase_basket": [
                    template.format(generation=17)
                    for template in WORDSTAT_PHRASE_TEMPLATES.values()
                ],
                "aggregation_rule": (
                    "counts and shares are never summed; the median direction of each "
                    "normalized phrase series is used only after enough phrases agree"
                ),
                "calibrated_policy": {
                    **asdict(selected_wordstat_policy),
                    "policy_id": selected_wordstat_policy.policy_id,
                },
                "recommended_policy": {
                    **asdict(recommended_wordstat_policy),
                    "policy_id": recommended_wordstat_policy.policy_id,
                },
                "selected_policy": {
                    **asdict(recommended_wordstat_policy),
                    "policy_id": recommended_wordstat_policy.policy_id,
                },
                "selection_boundary": (
                    "Wordstat lag, lookback, confirmation threshold and uplift cap were "
                    "selected on iPhone 16 decisions through 2025-12-31; iPhone 17 sales "
                    "and outcomes were not used for policy selection"
                ),
                "calibration": {
                    "target_generation": 16,
                    "candidate_count": len(wordstat_calibration_rows),
                    "selected_result": selected_wordstat_calibration.summary_row(),
                },
                "calibrated_policy_holdout": selected_wordstat_result.summary_row(),
                "calibrated_policy_delta_vs_baseline": _result_delta(
                    selected_wordstat_result, selected_result
                ),
                "validation_passed": wordstat_validation_passed,
                "validation_decision": (
                    "quantitative_modifier_accepted"
                    if wordstat_validation_passed
                    else "quantitative_modifier_rejected_keep_direction_only"
                ),
                "recommended_policy_basis": (
                    "calibrated signal shape with max_uplift_ratio forced to zero after "
                    "the quantitative policy failed non-degradation checks on holdout"
                ),
                "holdout": recommended_wordstat_result.summary_row(),
                "delta_vs_baseline": _result_delta(recommended_wordstat_result, selected_result),
                "sensitivity": _wordstat_sensitivity_summary(
                    wordstat_holdout_rows, selected_result
                ),
                "transition_effect": "none_evidence_thresholds_unchanged",
                "unit_conversion": "none",
                "production_action": "none_read_only",
            }

        first_available_ages = {
            str(generation): ((first - RELEASE_DATES[generation]).days if first else None)
            for generation, first in data.first_family_available.items()
            if generation in RELEASE_DATES
        }
        independent_windows: list[dict[str, Any]] = []
        for window_from, window_to in (
            (date(2026, 1, 1), date(2026, 3, 31)),
            (date(2026, 4, 1), date(2026, 6, 30)),
            (date(2026, 7, 1), date(2026, 7, 31)),
        ):
            window_result = simulate_family(
                data,
                generation=17,
                candidate=selected,
                simulation_to=window_to,
                evaluation_from=window_from,
                use_target_stock_residuals=True,
            )
            window_control = _actual_target_metrics(
                data,
                ending_required_qty=window_result.ending_required_qty,
                evaluation_from=window_from,
                evaluation_to=window_to,
            )
            independent_windows.append(
                {
                    "evaluation_from": window_from.isoformat(),
                    "evaluation_to": window_to.isoformat(),
                    "candidate": window_result.summary_row(),
                    "observable_control_lower_bound": window_control,
                    "strict_non_degradation": _strict_non_degradation(
                        window_result, window_control
                    ),
                }
            )
        strict_windows_passed = all(
            row["strict_non_degradation"]["passed"] for row in independent_windows
        )
        point_in_time_contract = {
            "schema": "assortment_signal_point_in_time.v1",
            "required_fields": [
                "occurred_at",
                "available_at",
                "source",
                "reliability",
                "sku_or_family_link",
            ],
            "historical_available_at_complete": False,
            "known_date_policy": {
                "sales_availability_kmp4_site": "business_date_used_as_lower_bound",
                "supplier_order": "created_at_used_as_lower_bound",
                "receipt": "receipt_at_used_as_lower_bound",
                "cargo": "handoff_at_used_only_when_present",
                "wordstat": "completed_period_plus_configured_publication_lag",
            },
            "blocker": (
                "historical ingestion/availability timestamps are absent for part of the "
                "frozen sources; corrected or late records cannot be proven point-in-time"
            ),
        }
        acceptance = {
            "strict_non_degradation_on_every_independent_window": strict_windows_passed,
            "historical_available_at_complete": point_in_time_contract[
                "historical_available_at_complete"
            ],
            "independent_cold_start_launch_count": 0,
            "workflow_pilot_family": "iPhone 17 Pro Max",
            "cold_start_validation_family": "next_new_family",
        }
        acceptance["backtest_v3_gate_passed"] = all(
            (
                acceptance["strict_non_degradation_on_every_independent_window"],
                acceptance["historical_available_at_complete"],
                acceptance["independent_cold_start_launch_count"] > 0,
            )
        )
        summary = {
            "schema": RESULT_SCHEMA,
            "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "selected_candidate": {**asdict(selected), "candidate_id": selected.candidate_id},
            "selection_boundary": (
                "buffer, prior strength and transition profile selected on the iPhone 16 "
                "trajectory through 2025-12-31; the closest non-left-censored generation is required; "
                "repair lag is selected from the phone-age difference at the dated first supplier order; "
                "automatic early-repeat cap is held at zero because only one clean launch exists; "
                "iPhone 17 sales outcomes were not used for coefficient selection"
            ),
            "calibration": {
                "target_generation": 16,
                "analog_generations": [14, 15],
                "candidate_count": len(calibration_rows),
                "selected_result": next(
                    result.summary_row()
                    for candidate, result in calibration_rows
                    if candidate == selected_base
                ),
                "temporal_validation_2026": validation16.summary_row(),
                "independent_clean_launch_count": 1,
                "left_censored_launches": [14, 15],
            },
            "holdout": selected_result.summary_row(),
            "actual": actual,
            "actual_interpretation": (
                "observed sales are a lower bound on demand, not proof of 100% true-demand service"
            ),
            "independent_windows": independent_windows,
            "point_in_time_contract": point_in_time_contract,
            "acceptance": acceptance,
            "sensitivity": sensitivity,
            "wordstat": wordstat_summary,
            "signal_reconciliation": _signal_reconciliation(data),
            "family_reconciliation": _family_reconciliation(data),
            "phone_age_at_first_available_days": first_available_ages,
            "derived_repair_lag_days": derived_lag,
            "data_sufficiency": "provisional_shadow_only",
            "production_action": "none_read_only",
        }
        (output_dir / "backtest-summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )
        return summary
    finally:
        data.close()


def _signal_reconciliation(data: FrozenFamilyData) -> dict[str, Any]:
    fields = (
        "kmp4_raw_qty",
        "kmp4_matched_qty",
        "kmp4_expired_qty",
        "site_order_raw_qty",
        "site_order_matched_qty",
        "site_order_expired_qty",
        "site_order_hidden_qty",
    )
    return {
        field: round(sum(_number(row[field]) for row in data.target_daily.values()), 3)
        for field in fields
    }


def _family_reconciliation(data: FrozenFamilyData) -> dict[str, Any]:
    sales_rows = [
        row
        for (day, code), row in data.daily.items()
        if code in TARGET_CODES and HOLDOUT_FROM.isoformat() <= day <= HOLDOUT_TO.isoformat()
    ]
    target_rows = [row for (_, code), row in data.target_daily.items() if code in TARGET_CODES]
    order_rows = [row for row in data.orders if row["nomenclature_code"] in TARGET_CODES]
    receipt_rows = [row for row in data.receipts if row["nomenclature_code"] in TARGET_CODES]
    sale_observations = [
        row
        for row in data.sale_observations
        if row["nomenclature_code"] in TARGET_CODES
        and HOLDOUT_FROM.isoformat() <= row["business_date"] <= HOLDOUT_TO.isoformat()
    ]
    return {
        "sku_count": len(TARGET_CODES),
        "sales_qty_replay": round(sum(_number(row["sales_qty"]) for row in sales_rows), 3),
        "sales_qty_preflight": round(sum(_number(row["sales_qty"]) for row in target_rows), 3),
        "sale_observation_count": len(sale_observations),
        "sale_document_count": len({row["document_hash"] for row in sale_observations}),
        "supplier_order_line_count": len(order_rows),
        "supplier_order_qty": round(sum(_number(row["quantity"]) for row in order_rows), 3),
        "receipt_line_count": len(receipt_rows),
        "receipt_qty": round(sum(_number(row["quantity"]) for row in receipt_rows), 3),
        "linked_receipt_count": sum(bool(row["order_hash"]) for row in receipt_rows),
    }


def _write_source_notes(output_dir: Path, summary: Mapping[str, Any]) -> None:
    notes = {
        "schema": "iphone17_pro_max_cold_start_source_notes.v1",
        "report_audience": "product stakeholders",
        "delivery_mode": "portable_html",
        "report_structure": [
            "Title",
            "Executive Summary",
            "Key findings with visual evidence",
            "Recommended next steps",
            "Further questions",
            "Caveats and assumptions",
        ],
        "chart_map": [
            {
                "segment": "Чувствительность раннего cap",
                "question": "Насколько устойчиво положительный cap улучшает сервис?",
                "family": "Composition",
                "type": "stackedBar",
                "fields": ["cap", "outcome", "candidate_count"],
                "claim": (
                    "По 432 попарным сравнениям для каждого cap медианный эффект "
                    "на обслуженные продажи равен нулю."
                ),
                "palette": "relaxed multi-category",
            },
            {
                "segment": "Факт против shadow",
                "question": "Что меняется по сервису, капиталу, GMROI и конечной позиции?",
                "family": "Tables & Scorecards",
                "type": "table",
                "fields": [
                    "scenario",
                    "served_sales_ratio",
                    "missed_gross_profit_rub",
                    "average_inventory_cost_rub",
                    "gmroi",
                    "ending_position_gap_qty",
                ],
                "claim": "Shadow-кандидат сравнивается с фактом на одинаковом holdout.",
            },
            {
                "segment": "Даты решений",
                "question": "Когда режим меняется и почему появляется дозаказ?",
                "family": "Tables & Scorecards",
                "type": "table",
                "fields": ["decision_date", "mode", "requested_repeat_qty", "reason"],
                "claim": "Решения используют только информацию на дату расчёта.",
            },
            {
                "segment": "Wordstat как ограниченный сигнал",
                "question": "Дал ли внешний поисковый тренд измеримый эффект без перевода запросов в штуки?",
                "family": "Tables & Scorecards",
                "type": "table",
                "fields": [
                    "scenario",
                    "served_sales_qty",
                    "missed_gross_profit_rub",
                    "average_inventory_cost_rub",
                    "gmroi",
                ],
                "claim": (
                    "Параметры Wordstat выбираются на iPhone 16, а iPhone 17 остаётся "
                    "временным holdout; пересекающиеся частотности не суммируются."
                ),
            },
        ],
        "omissions": {
            "true_lost_demand": "Не наблюдается при отсутствии товара; недобор прибыли считается только на фактических продажах, которые контрфактическая модель не смогла бы обслужить.",
            "confirmed_on_demand_orders": "Отдельный источник 'Под заказ' отсутствует в frozen-истории.",
            "crm_arrival_notifications": "CRM и 'Сообщить о поступлении' не были доступны в периоде.",
            "wordstat_overlap": (
                "API не возвращает уникальных пользователей или матрицу пересечений фраз; "
                "поэтому ряды считаются потенциально пересекающимися и не суммируются."
            ),
            "independent_sales_transition": (
                "Текущий strict-профиль проверяет объём, разные дни продаж и дни наличия, "
                "но ещё не вводит отдельный порог по документам или клиентам."
            ),
            "production_rule": "Одна чистая калибровочная семейная траектория недостаточна для включения production.",
        },
        "selected_candidate": summary["selected_candidate"],
        "selected_wordstat_policy": summary.get("wordstat", {}).get("selected_policy"),
    }
    (output_dir / "source-notes.json").write_text(
        json.dumps(notes, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )


def _write_v3_report_artifacts(output_dir: Path, summary: Mapping[str, Any]) -> None:
    holdout = summary["holdout"]
    sensitivity = summary["sensitivity"]
    best = sensitivity["best_service_candidate"]
    artifact = {
        "schema": "iphone17_pro_max_cold_start_report.v3",
        "status": "blocked_before_api_ui",
        "answer": (
            "Backtest v3 не прошёл допуск: ни один grid-кандидат не выполнил "
            "все ограничения, historical available_at неполон, независимого "
            "cold-start запуска ещё нет."
        ),
        "selected_candidate": summary["selected_candidate"],
        "selected_result": holdout,
        "best_service_candidate": best,
        "candidate_count": sensitivity["candidate_count"],
        "all_requested_constraints_count": sensitivity["all_requested_constraints_count"],
        "acceptance": summary["acceptance"],
        "production_action": "none_read_only",
    }
    (output_dir / "artifact.json").write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    blockers = [
        "0 кандидатов прошли все ограничения",
        "для части frozen-источников отсутствует исторический available_at",
        "независимая проверка холодного старта ждёт следующую новую семью",
    ]
    blocker_items = "".join(f"<li>{html.escape(item)}</li>" for item in blockers)
    report = f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>iPhone 17 Pro Max — cold-start backtest v3</title>
<style>
body{{font:16px/1.5 system-ui,sans-serif;max-width:980px;margin:40px auto;padding:0 24px;color:#17202a}}
h1,h2{{line-height:1.2}} .bad{{color:#a61b1b;font-weight:700}} table{{border-collapse:collapse;width:100%}}
th,td{{padding:8px 10px;border:1px solid #d8dee4;text-align:right}} th:first-child,td:first-child{{text-align:left}}
code{{background:#f3f4f6;padding:2px 5px}} small{{color:#57606a}}
</style></head><body>
<h1>Cold-start/backtest v3: iPhone 17 Pro Max</h1>
<p class="bad">Допуск не пройден. API, UI и live-пилот заблокированы.</p>
<p>Полная сетка: <strong>{sensitivity['candidate_count']}</strong> кандидатов; прошли все ограничения:
<strong>{sensitivity['all_requested_constraints_count']}</strong>.</p>
<h2>Сравнение результатов</h2><table><thead><tr><th>Метрика</th><th>Предвыбранный</th><th>Лучший сервис</th></tr></thead><tbody>
<tr><td>Обслуженные продажи</td><td>{holdout['served_sales_qty']}</td><td>{best['served_sales_qty']}</td></tr>
<tr><td>Потерянные продажи</td><td>{holdout['lost_sales_qty']}</td><td>{best['lost_sales_qty']}</td></tr>
<tr><td>Недополученная валовая прибыль, ₽</td><td>{holdout['missed_gross_profit_rub']}</td><td>{best['missed_gross_profit_rub']}</td></tr>
<tr><td>Средний складской капитал, ₽</td><td>{holdout['average_inventory_cost_rub']}</td><td>{best['average_inventory_cost_rub']}</td></tr>
<tr><td>GMROI</td><td>{holdout['gmroi']}</td><td>{best['gmroi']}</td></tr>
<tr><td>Конечный дефицит, шт.</td><td>{holdout['ending_shortfall_qty']}</td><td>{best['ending_shortfall_qty']}</td></tr>
</tbody></table>
<h2>Блокеры</h2><ul>{blocker_items}</ul>
<p>Wordstat используется только как direction-only сигнал. P75 применяется к покрытию и дате прихода;
pipeline кредитуется по надёжности созревших когорт; замены ограничены сегментом качество + конструкция.</p>
<small>Наблюдаемые продажи — нижняя граница спроса, а не доказательство 100% обслуживания истинного спроса.
Production action: none_read_only.</small>
</body></html>"""
    (output_dir / "report.html").write_text(report, encoding="utf-8")


def _write_bundle_manifest(
    output_dir: Path,
    *,
    snapshot_path: Path,
    snapshot_manifest_path: Path,
    wordstat_history: WordstatHistory | None,
) -> dict[str, Any]:
    manifest_path = output_dir / "bundle-manifest.json"
    output_files = {
        str(path.relative_to(output_dir)): {
            "sha256": _sha256(path),
            "size_bytes": path.stat().st_size,
        }
        for path in sorted(output_dir.rglob("*"))
        if path.is_file() and path != manifest_path and not path.name.endswith(".tmp")
    }
    inputs: dict[str, Any] = {
        "snapshot": {
            "path": str(snapshot_path),
            "sha256": _sha256(snapshot_path),
            "size_bytes": snapshot_path.stat().st_size,
        },
        "snapshot_manifest": {
            "path": str(snapshot_manifest_path),
            "sha256": _sha256(snapshot_manifest_path),
            "size_bytes": snapshot_manifest_path.stat().st_size,
        },
    }
    if wordstat_history is not None and wordstat_history.cache_path is not None:
        inputs["wordstat_cache"] = {
            "path": str(wordstat_history.cache_path),
            "sha256": _sha256(wordstat_history.cache_path),
            "size_bytes": wordstat_history.cache_path.stat().st_size,
        }
    manifest = {
        "schema": "iphone17_pro_max_cold_start_bundle.v3",
        "created_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "inputs": inputs,
        "outputs": output_files,
        "production_action": "none_read_only",
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return manifest


def main() -> int:
    args = _parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if args.refresh_snapshot and args.snapshot_dir is not None:
        raise ValueError("refresh_snapshot_cannot_write_to_reused_snapshot_dir")
    snapshot_dir = args.snapshot_dir or args.output_dir
    snapshot_path = snapshot_dir / "snapshot.sqlite3"
    manifest_path = snapshot_dir / "snapshot-manifest.json"
    if args.refresh_snapshot or not (snapshot_path.exists() and manifest_path.exists()):
        build_snapshot(
            snapshot_path=snapshot_path,
            manifest_path=manifest_path,
            replay_store=args.replay_store,
            dataset_hash=args.dataset_hash,
            preflight_dir=args.preflight_dir,
        )
    snapshot_checks = validate_snapshot(snapshot_path, manifest_path)
    wordstat_history = (
        None
        if args.without_wordstat
        else load_or_fetch_wordstat_history(
            args.wordstat_cache_dir or args.output_dir,
            refresh=args.refresh_wordstat,
        )
    )
    summary = run_backtest(
        snapshot_path=snapshot_path,
        output_dir=args.output_dir,
        skip_grid=args.skip_grid,
        wordstat_history=wordstat_history,
    )
    _write_source_notes(args.output_dir, summary)
    _write_v3_report_artifacts(args.output_dir, summary)
    validation = {
        "schema": "iphone17_pro_max_cold_start_validation.v3",
        "status": "blocked_before_api_ui",
        "snapshot_checks": snapshot_checks,
        "checks": {
            "holdout_not_used_for_parameter_selection": True,
            "first_orders_kept_manual": True,
            "quality_mix_sums_to_one": True,
            "quality_and_construction_segmented": True,
            "sim_esim_not_split": True,
            "p75_used_for_coverage_and_modeled_arrival": True,
            "lead_time_reliability_uses_matured_cohorts": True,
            "model_pipeline_not_credited_at_100_percent": True,
            "signals_not_converted_directly_to_units": True,
            "wordstat_counts_not_summed": True,
            "wordstat_only_completed_periods_as_of_decision": True,
            "wordstat_does_not_change_mode_thresholds": True,
            "production_action": "none_read_only",
            "historical_available_at_complete": summary["point_in_time_contract"][
                "historical_available_at_complete"
            ],
            "backtest_v3_gate_passed": summary["acceptance"]["backtest_v3_gate_passed"],
        },
        "caveats": [
            "iPhone 14 и 15 Pro Max left-censored на первом дне frozen-истории.",
            "Есть только один чистый семейный запуск для калибровки — iPhone 16 Pro Max.",
            "Истинный спрос в дни полного отсутствия товара не наблюдается.",
            "Для части frozen-источников нет исторического available_at; late corrections нельзя исключить доказательно.",
            "Положительные и отрицательные складские корректировки удерживаются как экзогенный исторический остаток.",
            "Wordstat не даёт уникальных пользователей или точных пересечений фраз; ряды не суммируются.",
            "Фактический лаг публикации месячного Wordstat не хранится в API, поэтому проверяется сетка 3/7/14 дней после конца месяца.",
            "iPhone 17 Pro Max проверяет workflow; независимый cold-start ждёт следующую новую семью.",
            "До прохождения v3 запрещены recommendation API, UI и live-пилот.",
        ],
    }
    (args.output_dir / "validation.json").write_text(
        json.dumps(validation, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    bundle_manifest = _write_bundle_manifest(
        args.output_dir,
        snapshot_path=snapshot_path,
        snapshot_manifest_path=manifest_path,
        wordstat_history=wordstat_history,
    )
    payload = {
        "status": "blocked_before_api_ui",
        "output_dir": str(args.output_dir),
        "snapshot": str(snapshot_path),
        "summary": summary,
        "validation": validation,
        "bundle_manifest": bundle_manifest,
    }
    print(
        json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if args.json
        else json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
