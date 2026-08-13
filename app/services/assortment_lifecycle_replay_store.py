from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping

REPLAY_STORE_SCHEMA_VERSION = 1
FACTS_SCHEMA = "assortment_lifecycle_replay_facts.v1"
TRAJECTORY_SCHEMA = "assortment_lifecycle_replay_trajectory.v1"
DEFAULT_REPLAY_STORE_PATH = Path(".local/assortment-lifecycle-backtest-store.sqlite3")


@dataclass(frozen=True)
class ReplayStoreWriteResult:
    key: str
    content_sha256: str
    row_count: int
    reused: bool


@dataclass(frozen=True)
class StoredTrajectory:
    trajectory_hash: str
    dataset_hash: str
    model_version: str
    policy_hash: str
    period_from: date
    period_to: date
    content_sha256: str
    row_count: int
    metadata: dict[str, Any]


class AssortmentLifecycleReplayStore:
    """Append-only local store for historical facts and model trajectories.

    This database is deliberately separate from the application classification
    database.  A cache key can be reused only when facts, model version, policy
    hash and period match exactly.  Existing rows cannot be updated or deleted.
    """

    def __init__(self, path: Path | str = DEFAULT_REPLAY_STORE_PATH) -> None:
        self.path = Path(path)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS replay_store_meta (
                    schema_version INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS replay_dataset (
                    dataset_hash TEXT PRIMARY KEY,
                    schema_name TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    observation_from TEXT NOT NULL,
                    observation_to TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    fact_count INTEGER NOT NULL,
                    source_manifest_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS replay_dataset_fact (
                    dataset_hash TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    business_date TEXT NOT NULL,
                    nomenclature_code TEXT NOT NULL,
                    fact_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    PRIMARY KEY (dataset_hash, ordinal),
                    FOREIGN KEY (dataset_hash) REFERENCES replay_dataset(dataset_hash)
                );
                CREATE INDEX IF NOT EXISTS ix_replay_fact_sku_date
                    ON replay_dataset_fact(dataset_hash, nomenclature_code, business_date);
                CREATE INDEX IF NOT EXISTS ix_replay_fact_type_date
                    ON replay_dataset_fact(dataset_hash, fact_type, business_date);

                CREATE TABLE IF NOT EXISTS replay_trajectory (
                    trajectory_hash TEXT PRIMARY KEY,
                    schema_name TEXT NOT NULL,
                    dataset_hash TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    policy_hash TEXT NOT NULL,
                    period_from TEXT NOT NULL,
                    period_to TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    row_count INTEGER NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (dataset_hash, model_version, policy_hash, period_from, period_to),
                    FOREIGN KEY (dataset_hash) REFERENCES replay_dataset(dataset_hash)
                );
                CREATE INDEX IF NOT EXISTS ix_replay_trajectory_lookup
                    ON replay_trajectory(dataset_hash, model_version, policy_hash,
                                         period_from, period_to);

                CREATE TABLE IF NOT EXISTS replay_trajectory_row (
                    trajectory_hash TEXT NOT NULL,
                    business_date TEXT NOT NULL,
                    nomenclature_code TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    PRIMARY KEY (trajectory_hash, business_date, nomenclature_code),
                    FOREIGN KEY (trajectory_hash) REFERENCES replay_trajectory(trajectory_hash)
                );
                CREATE INDEX IF NOT EXISTS ix_replay_trajectory_row_sku_date
                    ON replay_trajectory_row(trajectory_hash, nomenclature_code, business_date);
                """)
            current = connection.execute(
                "SELECT schema_version FROM replay_store_meta LIMIT 1"
            ).fetchone()
            if current is None:
                connection.execute(
                    "INSERT INTO replay_store_meta(schema_version, created_at) VALUES (?, ?)",
                    (REPLAY_STORE_SCHEMA_VERSION, _utc_now()),
                )
            elif int(current[0]) != REPLAY_STORE_SCHEMA_VERSION:
                raise ValueError(
                    "unsupported_replay_store_schema:" f"{current[0]}:{REPLAY_STORE_SCHEMA_VERSION}"
                )
            self._install_immutability_triggers(connection)

    def put_dataset(
        self,
        *,
        scope: str,
        observation_from: date,
        observation_to: date,
        facts: Iterable[Mapping[str, Any]],
        source_manifest: Mapping[str, Any] | None = None,
    ) -> ReplayStoreWriteResult:
        if observation_from > observation_to:
            raise ValueError("replay_dataset_period_invalid")
        normalized = sorted((_normalize_fact(row) for row in facts), key=_canonical_json)
        if any(
            not observation_from <= date.fromisoformat(row["business_date"]) <= observation_to
            for row in normalized
        ):
            raise ValueError("replay_fact_outside_dataset_period")
        identity = {
            "schema": FACTS_SCHEMA,
            "scope": _required_text(scope, "replay_dataset_scope_required"),
            "observation_from": observation_from,
            "observation_to": observation_to,
            "facts": normalized,
        }
        content_sha256 = stable_hash(normalized)
        dataset_hash = stable_hash(identity)
        source_manifest_json = _canonical_json(dict(source_manifest or {}))
        self.initialize()
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT content_sha256, fact_count, source_manifest_json "
                "FROM replay_dataset WHERE dataset_hash = ?",
                (dataset_hash,),
            ).fetchone()
            if existing is not None:
                if existing[0] != content_sha256 or int(existing[1]) != len(normalized):
                    raise ValueError(f"replay_dataset_hash_conflict:{dataset_hash}")
                return ReplayStoreWriteResult(
                    key=dataset_hash,
                    content_sha256=content_sha256,
                    row_count=len(normalized),
                    reused=True,
                )
            connection.execute(
                """
                INSERT INTO replay_dataset(
                    dataset_hash, schema_name, scope, observation_from, observation_to,
                    content_sha256, fact_count, source_manifest_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    dataset_hash,
                    FACTS_SCHEMA,
                    identity["scope"],
                    observation_from.isoformat(),
                    observation_to.isoformat(),
                    content_sha256,
                    len(normalized),
                    source_manifest_json,
                    _utc_now(),
                ),
            )
            connection.executemany(
                """
                INSERT INTO replay_dataset_fact(
                    dataset_hash, ordinal, business_date, nomenclature_code,
                    fact_type, payload_json, payload_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        dataset_hash,
                        ordinal,
                        row["business_date"],
                        row["nomenclature_code"],
                        row["fact_type"],
                        _canonical_json(row["payload"]),
                        stable_hash(row["payload"]),
                    )
                    for ordinal, row in enumerate(normalized)
                ],
            )
        return ReplayStoreWriteResult(
            key=dataset_hash,
            content_sha256=content_sha256,
            row_count=len(normalized),
            reused=False,
        )

    def put_trajectory(
        self,
        *,
        dataset_hash: str,
        model_version: str,
        policy_hash: str,
        period_from: date,
        period_to: date,
        rows: Iterable[Mapping[str, Any]],
        metadata: Mapping[str, Any] | None = None,
    ) -> ReplayStoreWriteResult:
        if period_from > period_to:
            raise ValueError("replay_trajectory_period_invalid")
        model = _required_text(model_version, "replay_model_version_required")
        policy = _required_text(policy_hash, "replay_policy_hash_required")
        normalized = sorted(
            (_normalize_trajectory_row(row) for row in rows),
            key=lambda row: (row["business_date"], row["nomenclature_code"]),
        )
        if any(
            not period_from <= date.fromisoformat(row["business_date"]) <= period_to
            for row in normalized
        ):
            raise ValueError("replay_trajectory_row_outside_period")
        keys = [(row["business_date"], row["nomenclature_code"]) for row in normalized]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate_replay_trajectory_sku_date")
        content_sha256 = stable_hash(normalized)
        trajectory_identity = {
            "schema": TRAJECTORY_SCHEMA,
            "dataset_hash": dataset_hash,
            "model_version": model,
            "policy_hash": policy,
            "period_from": period_from,
            "period_to": period_to,
        }
        trajectory_hash = stable_hash(trajectory_identity)
        metadata_json = _canonical_json(dict(metadata or {}))
        self.initialize()
        with self._connect() as connection:
            dataset = connection.execute(
                "SELECT 1 FROM replay_dataset WHERE dataset_hash = ?", (dataset_hash,)
            ).fetchone()
            if dataset is None:
                raise ValueError(f"replay_dataset_not_found:{dataset_hash}")
            existing = connection.execute(
                """
                SELECT trajectory_hash, content_sha256, row_count, metadata_json
                FROM replay_trajectory
                WHERE dataset_hash = ? AND model_version = ? AND policy_hash = ?
                  AND period_from = ? AND period_to = ?
                """,
                (
                    dataset_hash,
                    model,
                    policy,
                    period_from.isoformat(),
                    period_to.isoformat(),
                ),
            ).fetchone()
            if existing is not None:
                if (
                    existing[0] != trajectory_hash
                    or existing[1] != content_sha256
                    or int(existing[2]) != len(normalized)
                    or existing[3] != metadata_json
                ):
                    raise ValueError(f"replay_trajectory_key_conflict:{trajectory_hash}")
                return ReplayStoreWriteResult(
                    key=trajectory_hash,
                    content_sha256=content_sha256,
                    row_count=len(normalized),
                    reused=True,
                )
            connection.execute(
                """
                INSERT INTO replay_trajectory(
                    trajectory_hash, schema_name, dataset_hash, model_version, policy_hash,
                    period_from, period_to, content_sha256, row_count, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trajectory_hash,
                    TRAJECTORY_SCHEMA,
                    dataset_hash,
                    model,
                    policy,
                    period_from.isoformat(),
                    period_to.isoformat(),
                    content_sha256,
                    len(normalized),
                    metadata_json,
                    _utc_now(),
                ),
            )
            connection.executemany(
                """
                INSERT INTO replay_trajectory_row(
                    trajectory_hash, business_date, nomenclature_code,
                    payload_json, payload_sha256
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        trajectory_hash,
                        row["business_date"],
                        row["nomenclature_code"],
                        _canonical_json(row),
                        stable_hash(row),
                    )
                    for row in normalized
                ],
            )
        return ReplayStoreWriteResult(
            key=trajectory_hash,
            content_sha256=content_sha256,
            row_count=len(normalized),
            reused=False,
        )

    def find_trajectory(
        self,
        *,
        dataset_hash: str,
        model_version: str,
        policy_hash: str,
        period_from: date,
        period_to: date,
    ) -> StoredTrajectory | None:
        if not self.path.exists():
            return None
        self.initialize()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT trajectory_hash, dataset_hash, model_version, policy_hash,
                       period_from, period_to, content_sha256, row_count, metadata_json
                FROM replay_trajectory
                WHERE dataset_hash = ? AND model_version = ? AND policy_hash = ?
                  AND period_from = ? AND period_to = ?
                """,
                (
                    dataset_hash,
                    model_version,
                    policy_hash,
                    period_from.isoformat(),
                    period_to.isoformat(),
                ),
            ).fetchone()
        if row is None:
            return None
        return StoredTrajectory(
            trajectory_hash=row[0],
            dataset_hash=row[1],
            model_version=row[2],
            policy_hash=row[3],
            period_from=date.fromisoformat(row[4]),
            period_to=date.fromisoformat(row[5]),
            content_sha256=row[6],
            row_count=int(row[7]),
            metadata=json.loads(row[8]),
        )

    def load_trajectory_rows(self, trajectory_hash: str) -> list[dict[str, Any]]:
        if not self.path.exists():
            raise ValueError(f"replay_store_not_found:{self.path}")
        self.initialize()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT payload_json, payload_sha256
                FROM replay_trajectory_row
                WHERE trajectory_hash = ?
                ORDER BY business_date, nomenclature_code
                """,
                (trajectory_hash,),
            ).fetchall()
            expected = connection.execute(
                "SELECT content_sha256, row_count FROM replay_trajectory WHERE trajectory_hash = ?",
                (trajectory_hash,),
            ).fetchone()
        if expected is None:
            raise ValueError(f"replay_trajectory_not_found:{trajectory_hash}")
        payloads: list[dict[str, Any]] = []
        for payload_json, payload_sha256 in rows:
            payload = json.loads(payload_json)
            if stable_hash(payload) != payload_sha256:
                raise ValueError(f"replay_trajectory_row_checksum_mismatch:{trajectory_hash}")
            payloads.append(payload)
        if len(payloads) != int(expected[1]) or stable_hash(payloads) != expected[0]:
            raise ValueError(f"replay_trajectory_checksum_mismatch:{trajectory_hash}")
        return payloads

    def load_dataset_facts(self, dataset_hash: str) -> list[dict[str, Any]]:
        if not self.path.exists():
            raise ValueError(f"replay_store_not_found:{self.path}")
        self.initialize()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT business_date, nomenclature_code, fact_type, payload_json, payload_sha256
                FROM replay_dataset_fact
                WHERE dataset_hash = ?
                ORDER BY ordinal
                """,
                (dataset_hash,),
            ).fetchall()
            expected = connection.execute(
                "SELECT content_sha256, fact_count FROM replay_dataset WHERE dataset_hash = ?",
                (dataset_hash,),
            ).fetchone()
        if expected is None:
            raise ValueError(f"replay_dataset_not_found:{dataset_hash}")
        facts: list[dict[str, Any]] = []
        for business_date, code, fact_type, payload_json, payload_sha256 in rows:
            payload = json.loads(payload_json)
            if stable_hash(payload) != payload_sha256:
                raise ValueError(f"replay_dataset_fact_checksum_mismatch:{dataset_hash}")
            facts.append(
                {
                    "business_date": business_date,
                    "nomenclature_code": code,
                    "fact_type": fact_type,
                    "payload": payload,
                }
            )
        if len(facts) != int(expected[1]) or stable_hash(facts) != expected[0]:
            raise ValueError(f"replay_dataset_checksum_mismatch:{dataset_hash}")
        return facts

    def manifest(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "schema": "assortment_lifecycle_replay_store_manifest.v1",
                "path": str(self.path),
                "exists": False,
                "datasets": [],
                "trajectories": [],
            }
        self.initialize()
        with self._connect() as connection:
            datasets = [dict(row) for row in connection.execute("""
                    SELECT dataset_hash, scope, observation_from, observation_to,
                           content_sha256, fact_count, created_at
                    FROM replay_dataset ORDER BY created_at, dataset_hash
                    """).fetchall()]
            trajectories = [dict(row) for row in connection.execute("""
                    SELECT trajectory_hash, dataset_hash, model_version, policy_hash,
                           period_from, period_to, content_sha256, row_count, created_at
                    FROM replay_trajectory ORDER BY created_at, trajectory_hash
                    """).fetchall()]
        return {
            "schema": "assortment_lifecycle_replay_store_manifest.v1",
            "path": str(self.path),
            "exists": True,
            "schema_version": REPLAY_STORE_SCHEMA_VERSION,
            "datasets": datasets,
            "trajectories": trajectories,
        }

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=60)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @staticmethod
    def _install_immutability_triggers(connection: sqlite3.Connection) -> None:
        for table in (
            "replay_store_meta",
            "replay_dataset",
            "replay_dataset_fact",
            "replay_trajectory",
            "replay_trajectory_row",
        ):
            for action in ("UPDATE", "DELETE"):
                trigger = f"trg_{table}_{action.casefold()}_immutable"
                connection.execute(f"""
                    CREATE TRIGGER IF NOT EXISTS {trigger}
                    BEFORE {action} ON {table}
                    BEGIN
                        SELECT RAISE(ABORT, 'immutable_replay_store');
                    END
                    """)


def stable_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _normalize_fact(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "business_date": _iso_date(row.get("business_date"), "replay_fact_date_required"),
        "nomenclature_code": _required_text(
            row.get("nomenclature_code"), "replay_fact_sku_required"
        ),
        "fact_type": _required_text(row.get("fact_type"), "replay_fact_type_required"),
        "payload": _json_value(dict(_mapping(row.get("payload"), "replay_fact_payload_required"))),
    }


def _normalize_trajectory_row(row: Mapping[str, Any]) -> dict[str, Any]:
    payload = _json_value(dict(row))
    payload["business_date"] = _iso_date(
        payload.get("business_date"), "replay_trajectory_date_required"
    )
    payload["nomenclature_code"] = _required_text(
        payload.get("nomenclature_code"), "replay_trajectory_sku_required"
    )
    return payload


def _mapping(value: Any, error: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(error)
    return value


def _required_text(value: Any, error: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValueError(error)
    return result


def _iso_date(value: Any, error: str) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raw = str(value or "").strip()
    try:
        return date.fromisoformat(raw[:10]).isoformat()
    except ValueError as exc:
        raise ValueError(error) from exc


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_json_value(item) for item in value), key=_canonical_json)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
