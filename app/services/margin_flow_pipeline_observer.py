"""Append-only forward observations for the margin-flow pipeline contract.

The observer has one deliberately narrow job: read current open supplier-order
balances from 1C and preserve what was knowable at that observation time.  It
does not calculate an order recommendation and it has no application database
or external-integration write path.
"""

from __future__ import annotations

import csv
import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence
from zoneinfo import ZoneInfo

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection, Engine

OBSERVER_SCHEMA = "margin_flow_pipeline_forward_observer.v1"
OBSERVATION_SCHEMA = "margin_flow_pipeline_observation.v1"
LOT_SCHEMA = "margin_flow_pipeline_lot_observation.v1"
SKU_STATE_SCHEMA = "margin_flow_pipeline_sku_state.v1"
OPEN_BALANCE_PERIOD = datetime.fromisoformat("3999-11-01T00:00:00")
EMPTY_ONEC_DATE = datetime.fromisoformat("1753-01-01T00:00:00")
SOURCE_OBJECTS = (
    "dbo._AccumRgT7160",
    "dbo._Reference62",
    "dbo._Document133",
)
FORBIDDEN_SOURCE_PERMISSIONS = (
    "INSERT",
    "UPDATE",
    "DELETE",
    "ALTER",
    "CONTROL",
    "TAKE OWNERSHIP",
)
_SLOT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

PIPELINE_LOTS_SQL = """
SELECT
    NULLIF(LTRIM(RTRIM(product._Code)), N'') AS nomenclature_code,
    CONVERT(varchar(34), open_balance._Fld7149RRef, 1) AS order_ref_hex,
    CONVERT(varchar(34), open_balance._Fld7151RRef, 1) AS product_ref_hex,
    SUM(CAST(open_balance._Fld7156 AS decimal(18, 3))) AS quantity,
    COUNT_BIG(*) AS register_row_count,
    CONVERT(
        varchar(18), CONVERT(varbinary(8), supplier_order._Version), 1
    ) AS order_revision_fingerprint,
    supplier_order._Date_Time AS order_created_at_raw,
    supplier_order._Fld2493 AS expected_receipt_at_raw,
    supplier_order._Fld8852 AS cargo_handoff_at_raw,
    CONVERT(varchar(4), supplier_order._Marked, 1) AS marked_raw,
    CONVERT(varchar(4), supplier_order._Posted, 1) AS posted_raw
FROM dbo._AccumRgT7160 AS open_balance
JOIN dbo._Reference62 AS product
    ON product._IDRRef = open_balance._Fld7151RRef
LEFT JOIN dbo._Document133 AS supplier_order
    ON supplier_order._IDRRef = open_balance._Fld7149RRef
WHERE open_balance._Period = :balance_period
  AND open_balance._Fld7156 > 0
  AND NULLIF(LTRIM(RTRIM(product._Code)), N'') IN :codes
GROUP BY
    NULLIF(LTRIM(RTRIM(product._Code)), N''),
    open_balance._Fld7149RRef,
    open_balance._Fld7151RRef,
    supplier_order._Version,
    supplier_order._Date_Time,
    supplier_order._Fld2493,
    supplier_order._Fld8852,
    supplier_order._Marked,
    supplier_order._Posted
ORDER BY
    NULLIF(LTRIM(RTRIM(product._Code)), N''),
    CONVERT(varchar(34), open_balance._Fld7149RRef, 1)
"""


@dataclass(frozen=True)
class ObserverConfig:
    observer_id: str
    timezone_name: str
    minimum_matured_consecutive_days: int
    expected_scope_sha256: str
    expected_scope_code_count: int
    source_bundle: str
    raw: dict[str, Any]
    content_sha256: str


@dataclass(frozen=True)
class ObservationWriteResult:
    observation_slot: str
    observation_dir: Path
    manifest_sha256: str
    lot_count: int
    scope_code_count: int
    reused: bool


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_observer_config(path: Path) -> ObserverConfig:
    raw_bytes = path.read_bytes()
    payload = json.loads(raw_bytes.decode("utf-8"))
    if payload.get("schema") != OBSERVER_SCHEMA:
        raise ValueError("margin_flow_observer_config_schema_invalid")
    safety = payload.get("safety") or {}
    required_false = (
        "application_database_writes",
        "onec_writes",
        "bitrix_writes",
        "telegram_writes",
        "external_api_calls",
        "recommended_order_qty_calculation",
        "recommended_order_qty_writes",
        "order_creation",
        "status_changes",
        "release_changes",
        "production_cron_changes",
    )
    if any(safety.get(key) is not False for key in required_false):
        raise ValueError("margin_flow_observer_side_effects_not_disabled")
    if safety.get("onec_read_only_required") is not True:
        raise ValueError("margin_flow_observer_read_only_preflight_not_required")

    scope = payload.get("scope") or {}
    observer_id = str(payload.get("observer_id") or "").strip()
    timezone_name = str(payload.get("timezone") or "").strip()
    minimum_days = int(payload.get("minimum_matured_consecutive_days") or 0)
    expected_scope_sha256 = str(scope.get("expected_sha256") or "").strip().lower()
    expected_scope_code_count = int(scope.get("expected_code_count") or 0)
    if not observer_id:
        raise ValueError("margin_flow_observer_id_required")
    if timezone_name != "Europe/Moscow":
        raise ValueError("margin_flow_observer_timezone_must_be_europe_moscow")
    if minimum_days < 105:
        raise ValueError("margin_flow_observer_window_below_105_days")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_scope_sha256):
        raise ValueError("margin_flow_observer_scope_sha256_invalid")
    if expected_scope_code_count <= 0:
        raise ValueError("margin_flow_observer_scope_count_invalid")
    return ObserverConfig(
        observer_id=observer_id,
        timezone_name=timezone_name,
        minimum_matured_consecutive_days=minimum_days,
        expected_scope_sha256=expected_scope_sha256,
        expected_scope_code_count=expected_scope_code_count,
        source_bundle=str(scope.get("source_bundle") or ""),
        raw=payload,
        content_sha256=hashlib.sha256(raw_bytes).hexdigest(),
    )


def load_scope_codes(path: Path, config: ObserverConfig) -> list[str]:
    actual_sha256 = file_sha256(path)
    if actual_sha256 != config.expected_scope_sha256:
        raise ValueError(
            "margin_flow_observer_scope_checksum_mismatch:"
            f"{actual_sha256}:{config.expected_scope_sha256}"
        )
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if not reader.fieldnames or "nomenclature_code" not in reader.fieldnames:
            raise ValueError("margin_flow_observer_scope_code_column_missing")
        codes = [str(row.get("nomenclature_code") or "").strip() for row in reader]
    if any(not code for code in codes):
        raise ValueError("margin_flow_observer_scope_contains_empty_code")
    if len(codes) != len(set(codes)):
        raise ValueError("margin_flow_observer_scope_contains_duplicate_code")
    if len(codes) != config.expected_scope_code_count:
        raise ValueError(
            "margin_flow_observer_scope_count_mismatch:"
            f"{len(codes)}:{config.expected_scope_code_count}"
        )
    return sorted(codes)


def stable_lot_identity(*, order_ref_hex: str, product_ref_hex: str) -> str:
    order_ref = _required_hex(order_ref_hex, "margin_flow_observer_order_ref_missing")
    product_ref = _required_hex(product_ref_hex, "margin_flow_observer_product_ref_missing")
    return stable_hash(
        {
            "identity_schema": "onec_open_supplier_order_product.v1",
            "order_ref_hex": order_ref,
            "product_ref_hex": product_ref,
        }
    )


def assert_source_is_read_only(connection: Connection) -> dict[str, Any]:
    identity = (
        connection.execute(
            text("SELECT DB_NAME() AS database_name, SUSER_SNAME() AS principal_name")
        )
        .mappings()
        .one()
    )
    objects: list[dict[str, Any]] = []
    for object_name in SOURCE_OBJECTS:
        can_select = bool(
            connection.execute(
                text("SELECT HAS_PERMS_BY_NAME(:name, 'OBJECT', 'SELECT')"),
                {"name": object_name},
            ).scalar()
        )
        forbidden: dict[str, bool] = {}
        for permission_name in FORBIDDEN_SOURCE_PERMISSIONS:
            forbidden[permission_name.lower().replace(" ", "_")] = bool(
                connection.execute(
                    text("SELECT HAS_PERMS_BY_NAME(:name, 'OBJECT', :permission)"),
                    {"name": object_name, "permission": permission_name},
                ).scalar()
            )
        if not can_select:
            raise PermissionError(f"margin_flow_observer_source_select_denied:{object_name}")
        enabled = sorted(name for name, granted in forbidden.items() if granted)
        if enabled:
            raise PermissionError(
                "margin_flow_observer_source_write_permission_detected:"
                f"{object_name}:{','.join(enabled)}"
            )
        objects.append(
            {
                "object": object_name,
                "select": True,
                "forbidden_permissions": forbidden,
            }
        )
    return {
        "status": "pass_effective_object_permissions_read_only",
        "database_identity_sha256": stable_hash(str(identity["database_name"])),
        "principal_identity_sha256": stable_hash(str(identity["principal_name"])),
        "objects": objects,
    }


def fetch_pipeline_lots(connection: Connection, *, codes: Sequence[str]) -> list[dict[str, Any]]:
    if not codes:
        return []
    statement = text(PIPELINE_LOTS_SQL).bindparams(
        bindparam("codes", expanding=True),
        bindparam("balance_period", value=OPEN_BALANCE_PERIOD),
    )
    return [dict(row) for row in connection.execute(statement, {"codes": list(codes)}).mappings()]


def read_source_snapshot(
    engine: Engine, *, codes: Sequence[str]
) -> tuple[list[dict[str, Any]], dict[str, Any], str, str]:
    with engine.connect() as connection:
        permissions = assert_source_is_read_only(connection)
        started_at = _database_utc_now(connection)
        rows = fetch_pipeline_lots(connection, codes=codes)
        completed_at = _database_utc_now(connection)
        connection.rollback()
    return rows, permissions, started_at, completed_at


@contextmanager
def observer_lock(output_root: Path) -> Iterator[None]:
    output_root.mkdir(parents=True, exist_ok=True)
    lock_path = output_root / ".observer.lock"
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("margin_flow_observer_already_running") from exc
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def reuse_existing_observation(
    *,
    output_root: Path,
    observation_slot: str,
    config: ObserverConfig,
    scope_sha256: str,
) -> ObservationWriteResult | None:
    observation_dir = output_root / "observations" / observation_slot
    if not observation_dir.exists():
        return None
    manifest = validate_observation_dir(observation_dir)
    if manifest.get("config_sha256") != config.content_sha256:
        raise ValueError("margin_flow_observer_existing_slot_config_mismatch")
    if manifest.get("scope_sha256") != scope_sha256:
        raise ValueError("margin_flow_observer_existing_slot_scope_mismatch")
    return ObservationWriteResult(
        observation_slot=observation_slot,
        observation_dir=observation_dir,
        manifest_sha256=file_sha256(observation_dir / "manifest.json"),
        lot_count=int(manifest["counts"]["lot_count"]),
        scope_code_count=int(manifest["counts"]["scope_code_count"]),
        reused=True,
    )


def write_observation(
    *,
    output_root: Path,
    observation_slot: str,
    config: ObserverConfig,
    scope_path: Path,
    scope_codes: Sequence[str],
    raw_lots: Iterable[Mapping[str, Any]],
    permission_evidence: Mapping[str, Any],
    source_read_started_at: str,
    source_read_completed_at: str,
    command: Sequence[str],
) -> ObservationWriteResult:
    _validate_slot_matches_observation(
        observation_slot,
        observed_at=source_read_completed_at,
        timezone_name=config.timezone_name,
    )
    scope_sha256 = file_sha256(scope_path)
    existing = reuse_existing_observation(
        output_root=output_root,
        observation_slot=observation_slot,
        config=config,
        scope_sha256=scope_sha256,
    )
    if existing is not None:
        return existing

    observations_root = output_root / "observations"
    observations_root.mkdir(parents=True, exist_ok=True)
    observer_manifest = _ensure_observer_manifest(
        output_root=output_root,
        config=config,
        scope_sha256=scope_sha256,
        scope_code_count=len(scope_codes),
    )
    previous_sha256 = _previous_manifest_sha256(
        observations_root,
        observation_slot=observation_slot,
    )
    lots = _normalize_lots(
        raw_lots,
        scope_codes=set(scope_codes),
        observed_at=source_read_completed_at,
    )
    sku_states = _build_sku_states(
        scope_codes,
        lots,
        observed_at=source_read_completed_at,
    )
    final_dir = observations_root / observation_slot
    temp_dir = Path(tempfile.mkdtemp(prefix=f".{observation_slot}.", dir=observations_root))
    try:
        lots_path = temp_dir / "lot-observations.jsonl"
        sku_states_path = temp_dir / "sku-states.jsonl"
        _write_jsonl(lots_path, lots)
        _write_jsonl(sku_states_path, sku_states)
        total_open_qty = sum((Decimal(row["quantity"]) for row in lots), Decimal("0"))
        manifest = {
            "schema": OBSERVATION_SCHEMA,
            "observer_id": config.observer_id,
            "observation_slot": observation_slot,
            "observed_at": source_read_completed_at,
            "available_at": source_read_completed_at,
            "available_at_semantics": "known_after_completed_read_only_observer_capture",
            "source_read_started_at": source_read_started_at,
            "source_read_completed_at": source_read_completed_at,
            "config_sha256": config.content_sha256,
            "scope_sha256": scope_sha256,
            "scope_source_bundle": config.source_bundle,
            "observer_manifest_sha256": file_sha256(observer_manifest),
            "previous_observation_manifest_sha256": previous_sha256,
            "counts": {
                "scope_code_count": len(scope_codes),
                "sku_state_count": len(sku_states),
                "sku_with_open_pipeline_count": sum(
                    1 for row in sku_states if int(row["open_lot_count"]) > 0
                ),
                "lot_count": len(lots),
                "total_open_quantity": _decimal_text(total_open_qty),
            },
            "files": {
                "lot-observations.jsonl": {
                    "rows": len(lots),
                    "sha256": file_sha256(lots_path),
                },
                "sku-states.jsonl": {
                    "rows": len(sku_states),
                    "sha256": file_sha256(sku_states_path),
                },
            },
            "source_permission_preflight": dict(permission_evidence),
            "source_contract": {
                "open_balance_table": "_AccumRgT7160",
                "product_table": "_Reference62",
                "supplier_order_table": "_Document133",
                "stable_lot_grain": "supplier_order_ref + product_ref",
                "order_revision_timestamp": "unavailable_in_source",
                "order_revision_fingerprint": "_Document133._Version",
                "reliability_default": "unproven",
                "cargo_date_is_independent_reliability_evidence": False,
                "look_ahead_rule": "never_backfill_before_available_at",
            },
            "safety": dict(config.raw["safety"]),
            "command": list(command),
            "status": "complete_append_only_read_only_source",
        }
        manifest_path = temp_dir / "manifest.json"
        _write_json(manifest_path, manifest)
        for path in (lots_path, sku_states_path, manifest_path):
            path.chmod(0o440)
        if final_dir.exists():
            raise FileExistsError(f"margin_flow_observer_slot_already_exists:{final_dir}")
        os.rename(temp_dir, final_dir)
        final_dir.chmod(0o550)
    except Exception:
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
        raise
    return ObservationWriteResult(
        observation_slot=observation_slot,
        observation_dir=final_dir,
        manifest_sha256=file_sha256(final_dir / "manifest.json"),
        lot_count=len(lots),
        scope_code_count=len(scope_codes),
        reused=False,
    )


def validate_observation_dir(observation_dir: Path) -> dict[str, Any]:
    manifest_path = observation_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != OBSERVATION_SCHEMA:
        raise ValueError(f"margin_flow_observer_manifest_schema_invalid:{observation_dir.name}")
    if manifest.get("observation_slot") != observation_dir.name:
        raise ValueError(f"margin_flow_observer_slot_manifest_mismatch:{observation_dir.name}")
    for filename, expected in (manifest.get("files") or {}).items():
        path = observation_dir / filename
        if not path.is_file():
            raise ValueError(f"margin_flow_observer_file_missing:{observation_dir.name}:{filename}")
        actual_sha256 = file_sha256(path)
        if actual_sha256 != expected.get("sha256"):
            raise ValueError(
                f"margin_flow_observer_file_checksum_mismatch:{observation_dir.name}:{filename}"
            )
        actual_rows = sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line)
        if actual_rows != int(expected.get("rows", -1)):
            raise ValueError(
                f"margin_flow_observer_file_row_count_mismatch:{observation_dir.name}:{filename}"
            )
    return manifest


def validate_observer_bundle(output_root: Path) -> dict[str, Any]:
    observer_path = output_root / "observer.json"
    observer = json.loads(observer_path.read_text(encoding="utf-8"))
    if observer.get("schema") != OBSERVER_SCHEMA:
        raise ValueError("margin_flow_observer_root_manifest_schema_invalid")
    observation_dirs = _observation_dirs(output_root / "observations")
    previous_sha256: str | None = None
    dates: list[date] = []
    total_lots = 0
    for observation_dir in observation_dirs:
        manifest = validate_observation_dir(observation_dir)
        if manifest.get("previous_observation_manifest_sha256") != previous_sha256:
            raise ValueError(f"margin_flow_observer_chain_mismatch:{observation_dir.name}")
        previous_sha256 = file_sha256(observation_dir / "manifest.json")
        dates.append(date.fromisoformat(observation_dir.name))
        total_lots += int(manifest["counts"]["lot_count"])
    longest_run = _longest_consecutive_days(dates)
    minimum_days = int(observer["minimum_matured_consecutive_days"])
    return {
        "schema": "margin_flow_pipeline_observer_validation.v1",
        "observer_id": observer["observer_id"],
        "status": "matured" if longest_run >= minimum_days else "collecting",
        "observation_count": len(dates),
        "first_observation_slot": dates[0].isoformat() if dates else None,
        "latest_observation_slot": dates[-1].isoformat() if dates else None,
        "longest_consecutive_day_count": longest_run,
        "minimum_matured_consecutive_days": minimum_days,
        "remaining_consecutive_days": max(0, minimum_days - longest_run),
        "total_lot_observation_count": total_lots,
        "latest_manifest_sha256": previous_sha256,
        "chain_valid": True,
        "production_rollout": "NO_GO",
    }


def default_observation_slot(*, timezone_name: str, now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(ZoneInfo(timezone_name)).date().isoformat()


def _normalize_lots(
    rows: Iterable[Mapping[str, Any]],
    *,
    scope_codes: set[str],
    observed_at: str,
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    identities: set[str] = set()
    for row in rows:
        code = str(row.get("nomenclature_code") or "").strip()
        if code not in scope_codes:
            raise ValueError(f"margin_flow_observer_out_of_scope_source_row:{code}")
        quantity = Decimal(str(row.get("quantity") or "0"))
        if quantity <= 0:
            raise ValueError(f"margin_flow_observer_non_positive_lot_quantity:{code}")
        order_ref = _required_hex(
            str(row.get("order_ref_hex") or ""),
            "margin_flow_observer_order_ref_missing",
        )
        product_ref = _required_hex(
            str(row.get("product_ref_hex") or ""),
            "margin_flow_observer_product_ref_missing",
        )
        identity = stable_lot_identity(order_ref_hex=order_ref, product_ref_hex=product_ref)
        if identity in identities:
            raise ValueError(f"margin_flow_observer_duplicate_lot_identity:{identity}")
        identities.add(identity)
        expected_receipt_at = _onec_datetime(row.get("expected_receipt_at_raw"))
        cargo_handoff_at = _onec_datetime(row.get("cargo_handoff_at_raw"))
        revision = _optional_hex(row.get("order_revision_fingerprint"))
        reason_codes = ["cargo_date_not_independent_reliability_evidence"]
        if cargo_handoff_at is None:
            reason_codes.append("cargo_handoff_date_missing")
        normalized.append(
            {
                "schema": LOT_SCHEMA,
                "lot_identity": identity,
                "lot_identity_grain": "supplier_order_ref + product_ref",
                "nomenclature_code": code,
                "order_ref_hex": order_ref,
                "product_ref_hex": product_ref,
                "quantity": _decimal_text(quantity),
                "register_row_count": int(row.get("register_row_count") or 0),
                "observed_at": observed_at,
                "available_at": observed_at,
                "available_at_semantics": "known_after_completed_read_only_observer_capture",
                "order_revision_fingerprint": revision,
                "order_revision_at": None,
                "order_revision_at_status": "unavailable_in_source",
                "order_revision_observed_at": observed_at,
                "order_created_at_raw": _onec_datetime(row.get("order_created_at_raw")),
                "expected_receipt_at_raw": expected_receipt_at,
                "cargo_handoff_at_raw": cargo_handoff_at,
                "marked_raw": _optional_hex(row.get("marked_raw")),
                "posted_raw": _optional_hex(row.get("posted_raw")),
                "reliable_quantity": "0",
                "reliability_status": "unproven",
                "reliability_evidence": {
                    "raw_expected_receipt_at_present": expected_receipt_at is not None,
                    "raw_cargo_handoff_at_present": cargo_handoff_at is not None,
                    "independent_receipt_confirmation_present": False,
                    "reason_codes": reason_codes,
                },
            }
        )
    return sorted(normalized, key=lambda row: (row["nomenclature_code"], row["lot_identity"]))


def _build_sku_states(
    scope_codes: Sequence[str],
    lots: Sequence[Mapping[str, Any]],
    *,
    observed_at: str,
) -> list[dict[str, Any]]:
    by_code: dict[str, list[Mapping[str, Any]]] = {code: [] for code in scope_codes}
    for lot in lots:
        by_code[str(lot["nomenclature_code"])].append(lot)
    return [
        {
            "schema": SKU_STATE_SCHEMA,
            "nomenclature_code": code,
            "observed_at": observed_at,
            "available_at": observed_at,
            "open_lot_count": len(by_code[code]),
            "open_quantity": _decimal_text(
                sum((Decimal(str(row["quantity"])) for row in by_code[code]), Decimal("0"))
            ),
            "reliable_quantity": "0",
            "reliability_status": "unproven",
        }
        for code in sorted(scope_codes)
    ]


def _ensure_observer_manifest(
    *,
    output_root: Path,
    config: ObserverConfig,
    scope_sha256: str,
    scope_code_count: int,
) -> Path:
    path = output_root / "observer.json"
    expected = {
        "schema": OBSERVER_SCHEMA,
        "observer_id": config.observer_id,
        "timezone": config.timezone_name,
        "minimum_matured_consecutive_days": config.minimum_matured_consecutive_days,
        "config_sha256": config.content_sha256,
        "scope_sha256": scope_sha256,
        "scope_code_count": scope_code_count,
        "scope_source_bundle": config.source_bundle,
        "append_only": True,
        "production_rollout": "NO_GO",
    }
    if path.exists():
        if json.loads(path.read_text(encoding="utf-8")) != expected:
            raise ValueError("margin_flow_observer_root_manifest_mismatch")
        return path
    _write_json(path, expected, exclusive=True)
    path.chmod(0o440)
    return path


def _previous_manifest_sha256(observations_root: Path, *, observation_slot: str) -> str | None:
    existing = _observation_dirs(observations_root)
    later = [path.name for path in existing if path.name > observation_slot]
    if later:
        raise ValueError(f"margin_flow_observer_non_monotonic_slot:{observation_slot}:{later[0]}")
    previous = [path for path in existing if path.name < observation_slot]
    if not previous:
        return None
    previous_dir = previous[-1]
    validate_observation_dir(previous_dir)
    return file_sha256(previous_dir / "manifest.json")


def _observation_dirs(observations_root: Path) -> list[Path]:
    if not observations_root.exists():
        return []
    return sorted(
        path
        for path in observations_root.iterdir()
        if path.is_dir() and _SLOT_RE.fullmatch(path.name)
    )


def _longest_consecutive_days(days: Sequence[date]) -> int:
    longest = 0
    current = 0
    previous: date | None = None
    for day in sorted(set(days)):
        current = current + 1 if previous is not None and day == previous + timedelta(days=1) else 1
        longest = max(longest, current)
        previous = day
    return longest


def _database_utc_now(connection: Connection) -> str:
    value = connection.execute(text("SELECT SYSUTCDATETIME() AS observed_at")).scalar_one()
    if not isinstance(value, datetime):
        raise ValueError("margin_flow_observer_database_clock_invalid")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_slot_matches_observation(
    observation_slot: str,
    *,
    observed_at: str,
    timezone_name: str,
) -> None:
    if not _SLOT_RE.fullmatch(observation_slot):
        raise ValueError("margin_flow_observer_slot_invalid")
    parsed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    expected = parsed.astimezone(ZoneInfo(timezone_name)).date().isoformat()
    if observation_slot != expected:
        raise ValueError(f"margin_flow_observer_slot_clock_mismatch:{observation_slot}:{expected}")


def _required_hex(value: str, error: str) -> str:
    normalized = value.strip().upper()
    if not re.fullmatch(r"0X[0-9A-F]+", normalized):
        raise ValueError(error)
    return normalized


def _optional_hex(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().upper()
    if not normalized:
        return None
    if not re.fullmatch(r"0X[0-9A-F]+", normalized):
        raise ValueError("margin_flow_observer_source_hex_invalid")
    return normalized


def _onec_datetime(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise ValueError("margin_flow_observer_source_datetime_invalid")
    if value <= EMPTY_ONEC_DATE:
        return None
    return value.isoformat(timespec="microseconds")


def _decimal_text(value: Decimal) -> str:
    normalized = format(value, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized or "0"


def _write_json(path: Path, payload: Mapping[str, Any], *, exclusive: bool = False) -> None:
    mode = "x" if exclusive else "w"
    with path.open(mode, encoding="utf-8") as target:
        target.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        target.write("\n")
        target.flush()
        os.fsync(target.fileno())


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    with path.open("x", encoding="utf-8") as target:
        for row in rows:
            target.write(canonical_json(row))
            target.write("\n")
        target.flush()
        os.fsync(target.fileno())
