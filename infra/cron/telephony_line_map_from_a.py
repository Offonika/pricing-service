#!/usr/bin/env python3
"""Pull telephony retail line-map from server A and upsert it into Openclaw retail_line_map."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Collection
from datetime import date
from pathlib import Path
from typing import Any, Callable

DEFAULT_LOCAL_SOURCE_URL = "http://127.0.0.1:18080"
DEFAULT_LOCAL_ENV_FILE = "/opt/MM/pricing-service/.env"
DEFAULT_STATE_PATH = "/home/deploy/.openclaw/workspace/.data/telephony-line-map/state.json"
DEFAULT_ARTIFACT_DIR = "/home/deploy/.openclaw/workspace/.data/telephony-line-map/artifacts"
REPORT_KEY_PREFIX = "telephony-retail-line-map"


def _load_env(path: str | None) -> dict[str, str]:
    env = os.environ.copy()
    if not path:
        return env
    env_path = Path(path)
    if not env_path.exists():
        return env
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Pull telephony retail line-map from server A and update Openclaw retail_line_map."
    )
    parser.add_argument(
        "--snapshot-date",
        help="Optional snapshot date in YYYY-MM-DD; defaults to the latest snapshot on server A",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and render actions without updating retail_line_map",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable summary")
    return parser.parse_args()


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value)


def _env_flag(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_line_id_csv(value: str | None) -> set[str]:
    if not value:
        return set()
    stripped = value.strip()
    if stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            return {str(item).strip() for item in parsed if str(item).strip()}
    return {chunk.strip() for chunk in value.split(",") if chunk.strip()}


def _http_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: int,
) -> Any:
    request = urllib.request.Request(url, headers=headers or {})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
        if not body:
            return {}
        return json.loads(body)


def _build_fetcher(
    *,
    source_url: str,
    token: str,
    timeout: int,
    retries: int,
    retry_delay: float,
) -> Callable[[str, dict[str, str]], Any]:
    base = source_url.rstrip("/")
    headers = {"Authorization": f"Bearer {token}"}

    def _fetch(path: str, params: dict[str, str]) -> Any:
        query = urllib.parse.urlencode(params)
        url = f"{base}{path}"
        if query:
            url = f"{url}?{query}"

        attempts = max(1, retries + 1)
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                return _http_json(url, headers=headers, timeout=timeout)
            except (
                urllib.error.HTTPError,
                urllib.error.URLError,
                TimeoutError,
                ValueError,
            ) as error:
                last_error = error
                if attempt + 1 >= attempts:
                    break
                time.sleep(retry_delay)
        assert last_error is not None
        raise last_error

    return _fetch


def _normalize_phone(value: str | None) -> str:
    digits = "".join(ch for ch in (value or "") if ch.isdigit())
    if not digits:
        return ""
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return "+" + digits


def _payload_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        items = payload.get("payload", [])
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
    return []


def _normalize_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in items:
        line_id = str(item.get("line_id") or "").strip()
        store_id = str(item.get("store_id") or "").strip()
        store_name = str(item.get("store_name") or "").strip()
        if not line_id or not store_id:
            continue
        normalized.append(
            {
                "line_id": line_id,
                "phone_number": _normalize_phone(str(item.get("phone_number") or "").strip()),
                "store_id": store_id,
                "store_name": store_name,
                "mapping_mode": str(item.get("mapping_mode") or "").strip(),
                "active_user_count": int(item.get("active_user_count") or 0),
                "total_user_count": int(item.get("total_user_count") or 0),
                "store_names": list(item.get("store_names") or []),
                "employee_names": list(item.get("employee_names") or []),
                "bitrix_user_ids": list(item.get("bitrix_user_ids") or []),
                "primary_bitrix_user_id": str(item.get("primary_bitrix_user_id") or "").strip(),
                "primary_employee_name": str(item.get("primary_employee_name") or "").strip(),
                "primary_store_name": str(item.get("primary_store_name") or "").strip(),
            }
        )
    return sorted(normalized, key=lambda item: item["line_id"])


def _normalize_employee_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in items:
        user_ref_hex = str(item.get("user_ref_hex") or "").strip()
        if not user_ref_hex:
            continue
        normalized.append(
            {
                "snapshot_date": str(item.get("snapshot_date") or "").strip(),
                "mapping_source": str(item.get("mapping_source") or "").strip(),
                "user_ref_hex": user_ref_hex,
                "user_name": str(item.get("user_name") or "").strip(),
                "physical_person_ref_hex": str(item.get("physical_person_ref_hex") or "").strip(),
                "physical_person_name": str(item.get("physical_person_name") or "").strip(),
                "computer_name": str(item.get("computer_name") or "").strip(),
                "extension": str(item.get("extension") or "").strip(),
                "store_ref_hex": str(item.get("store_ref_hex") or "").strip(),
                "store_code": str(item.get("store_code") or "").strip(),
                "store_name": str(item.get("store_name") or "").strip(),
                "department_ref_hex": str(item.get("department_ref_hex") or "").strip(),
                "department_code": str(item.get("department_code") or "").strip(),
                "department_name": str(item.get("department_name") or "").strip(),
                "employment_status": str(item.get("employment_status") or "").strip(),
                "staff_store_ref": str(item.get("staff_store_ref") or "").strip(),
                "staff_store_name": str(item.get("staff_store_name") or "").strip(),
                "staff_department_ref": str(item.get("staff_department_ref") or "").strip(),
                "staff_department_name": str(item.get("staff_department_name") or "").strip(),
                "bitrix_user_id": str(item.get("bitrix_user_id") or "").strip(),
                "bitrix_full_name": str(item.get("bitrix_full_name") or "").strip(),
                "mdm_employee_code": str(item.get("mdm_employee_code") or "").strip(),
                "bitrix_status": str(item.get("bitrix_status") or "").strip(),
                "is_marked": bool(item.get("is_marked")),
                "has_extension": bool(item.get("has_extension")),
                "has_bitrix": bool(item.get("has_bitrix")),
            }
        )
    return sorted(normalized, key=lambda item: item["user_ref_hex"])


def _build_revision(
    snapshot_date: str,
    items: list[dict[str, Any]],
    *,
    employee_items: list[dict[str, Any]] | None = None,
) -> str:
    payload = json.dumps(
        {
            "snapshot_date": snapshot_date,
            "items": items,
            "employee_items": employee_items or [],
        },
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:12]


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"reports": {}}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return {"reports": {}}
    payload.setdefault("reports", {})
    return payload


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _write_artifact_csv(
    *,
    artifact_dir: Path,
    snapshot_date: str,
    revision: str,
    items: list[dict[str, Any]],
) -> Path:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    path = artifact_dir / f"telephony-retail-line-map-{snapshot_date}-{revision}.csv"
    fieldnames = list(items[0].keys()) if items else []
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if fieldnames:
            writer.writeheader()
            writer.writerows(items)
    return path


def _write_employee_artifact_csv(
    *,
    artifact_dir: Path,
    snapshot_date: str,
    revision: str,
    items: list[dict[str, Any]],
) -> Path:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    path = artifact_dir / f"telephony-employee-line-map-{snapshot_date}-{revision}.csv"
    fieldnames = list(items[0].keys()) if items else []
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if fieldnames:
            writer.writeheader()
            writer.writerows(items)
    return path


def _sql_literal(value: str) -> str:
    return "'" + (value or "").replace("'", "''") + "'"


def _run_psql(sql: str, *, database_url: str) -> str:
    proc = subprocess.run(
        ["psql", database_url, "-v", "ON_ERROR_STOP=1", "-At", "-F", "\t", "-c", sql],
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "psql failed").strip())
    return proc.stdout or ""


def _ensure_schema(*, database_url: str) -> None:
    sql = """
    CREATE TABLE IF NOT EXISTS retail_line_map (
        id bigserial PRIMARY KEY,
        line_id text,
        phone_number text,
        store_id text NOT NULL,
        store_name text NOT NULL DEFAULT '',
        is_active boolean NOT NULL DEFAULT true,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now()
    );

    CREATE UNIQUE INDEX IF NOT EXISTS ux_retail_line_map_line_id
    ON retail_line_map(line_id)
    WHERE line_id IS NOT NULL AND line_id <> '';

    CREATE UNIQUE INDEX IF NOT EXISTS ux_retail_line_map_phone_number
    ON retail_line_map(phone_number)
    WHERE phone_number IS NOT NULL AND phone_number <> '';

    CREATE TABLE IF NOT EXISTS retail_line_map_stage (
        line_id text PRIMARY KEY,
        phone_number text,
        store_id text NOT NULL,
        store_name text NOT NULL DEFAULT '',
        mapping_mode text NOT NULL DEFAULT '',
        active_user_count integer NOT NULL DEFAULT 0,
        total_user_count integer NOT NULL DEFAULT 0,
        store_names text NOT NULL DEFAULT '',
        employee_names text NOT NULL DEFAULT '',
        bitrix_user_ids text NOT NULL DEFAULT '',
        primary_bitrix_user_id text NOT NULL DEFAULT '',
        primary_employee_name text NOT NULL DEFAULT '',
        primary_store_name text NOT NULL DEFAULT '',
        snapshot_date date,
        revision text,
        loaded_at timestamptz NOT NULL DEFAULT now()
    );

    ALTER TABLE retail_line_map_stage ADD COLUMN IF NOT EXISTS store_names text NOT NULL DEFAULT '';
    ALTER TABLE retail_line_map_stage ADD COLUMN IF NOT EXISTS employee_names text NOT NULL DEFAULT '';
    ALTER TABLE retail_line_map_stage ADD COLUMN IF NOT EXISTS bitrix_user_ids text NOT NULL DEFAULT '';
    ALTER TABLE retail_line_map_stage ADD COLUMN IF NOT EXISTS primary_bitrix_user_id text NOT NULL DEFAULT '';
    ALTER TABLE retail_line_map_stage ADD COLUMN IF NOT EXISTS primary_employee_name text NOT NULL DEFAULT '';
    ALTER TABLE retail_line_map_stage ADD COLUMN IF NOT EXISTS primary_store_name text NOT NULL DEFAULT '';

    CREATE TABLE IF NOT EXISTS telephony_employee_snapshot_stage (
        user_ref_hex text PRIMARY KEY,
        snapshot_date date,
        mapping_source text NOT NULL DEFAULT '',
        user_name text,
        physical_person_ref_hex text,
        physical_person_name text,
        computer_name text,
        extension text,
        store_ref_hex text,
        store_code text,
        store_name text,
        department_ref_hex text,
        department_code text,
        department_name text,
        employment_status text,
        staff_store_ref text,
        staff_store_name text,
        staff_department_ref text,
        staff_department_name text,
        bitrix_user_id text,
        bitrix_full_name text,
        mdm_employee_code text,
        bitrix_status text,
        is_marked boolean NOT NULL DEFAULT false,
        has_extension boolean NOT NULL DEFAULT false,
        has_bitrix boolean NOT NULL DEFAULT false,
        revision text,
        loaded_at timestamptz NOT NULL DEFAULT now()
    );

    CREATE INDEX IF NOT EXISTS idx_telephony_employee_snapshot_stage_extension
    ON telephony_employee_snapshot_stage(extension);

    CREATE INDEX IF NOT EXISTS idx_telephony_employee_snapshot_stage_bitrix_user_id
    ON telephony_employee_snapshot_stage(bitrix_user_id);
    """
    _run_psql(sql, database_url=database_url)


def _fetch_existing_line_ids(*, database_url: str) -> set[str]:
    sql = """
    SELECT COALESCE(line_id, '')
    FROM retail_line_map
    WHERE COALESCE(line_id, '') <> '';
    """
    return {
        line.strip()
        for line in _run_psql(sql, database_url=database_url).splitlines()
        if line.strip()
    }


def _fetch_existing_rows(*, database_url: str) -> dict[str, dict[str, str]]:
    sql = """
    SELECT
        COALESCE(line_id, ''),
        COALESCE(phone_number, ''),
        COALESCE(store_id, ''),
        COALESCE(store_name, ''),
        COALESCE(is_active, true)::text
    FROM retail_line_map
    WHERE COALESCE(line_id, '') <> ''
      AND COALESCE(is_active, true) = true;
    """
    rows: dict[str, dict[str, str]] = {}
    for raw in _run_psql(sql, database_url=database_url).splitlines():
        line_id, phone_number, store_id, store_name, is_active = (raw.split("\t") + [""] * 5)[:5]
        normalized_line_id = line_id.strip()
        if not normalized_line_id or is_active.strip().lower() not in {"true", "t", "1"}:
            continue
        rows[normalized_line_id] = {
            "line_id": normalized_line_id,
            "phone_number": _normalize_phone(phone_number.strip()),
            "store_id": store_id.strip(),
            "store_name": store_name.strip(),
        }
    return rows


def _replace_stage_rows(
    items: list[dict[str, Any]],
    *,
    database_url: str,
    snapshot_date: str,
    revision: str,
) -> dict[str, Any]:
    _ensure_schema(database_url=database_url)
    statements = ["TRUNCATE retail_line_map_stage;"]
    if items:
        value_rows = []
        for item in items:
            value_rows.append(
                "("
                f"{_sql_literal(item['line_id'])}, "
                f"{_sql_literal(item['phone_number'])}, "
                f"{_sql_literal(item['store_id'])}, "
                f"{_sql_literal(item['store_name'])}, "
                f"{_sql_literal(item.get('mapping_mode') or '')}, "
                f"{int(item.get('active_user_count') or 0)}, "
                f"{int(item.get('total_user_count') or 0)}, "
                f"{_sql_literal('; '.join(str(v).strip() for v in item.get('store_names') or [] if str(v).strip()))}, "
                f"{_sql_literal('; '.join(str(v).strip() for v in item.get('employee_names') or [] if str(v).strip()))}, "
                f"{_sql_literal('; '.join(str(v).strip() for v in item.get('bitrix_user_ids') or [] if str(v).strip()))}, "
                f"{_sql_literal(item.get('primary_bitrix_user_id') or '')}, "
                f"{_sql_literal(item.get('primary_employee_name') or '')}, "
                f"{_sql_literal(item.get('primary_store_name') or '')}, "
                f"{_sql_literal(snapshot_date)}::date, "
                f"{_sql_literal(revision)}, "
                "now()"
                ")"
            )
        statements.append("""
            INSERT INTO retail_line_map_stage(
                line_id,
                phone_number,
                store_id,
                store_name,
                mapping_mode,
                active_user_count,
                total_user_count,
                store_names,
                employee_names,
                bitrix_user_ids,
                primary_bitrix_user_id,
                primary_employee_name,
                primary_store_name,
                snapshot_date,
                revision,
                loaded_at
            )
            VALUES
            """ + ",\n".join(value_rows) + ";")
    _run_psql("\n".join(statements), database_url=database_url)
    return {
        "staged_rows": len(items),
        "snapshot_date": snapshot_date,
        "revision": revision,
    }


def _replace_employee_stage_rows(
    items: list[dict[str, Any]],
    *,
    database_url: str,
    snapshot_date: str,
    revision: str,
) -> dict[str, Any]:
    _ensure_schema(database_url=database_url)
    statements = ["TRUNCATE telephony_employee_snapshot_stage;"]
    if items:
        value_rows = []
        for item in items:
            value_rows.append(
                "("
                f"{_sql_literal(item['user_ref_hex'])}, "
                f"{_sql_literal(item.get('snapshot_date') or snapshot_date)}::date, "
                f"{_sql_literal(item.get('mapping_source') or '')}, "
                f"{_sql_literal(item.get('user_name') or '')}, "
                f"{_sql_literal(item.get('physical_person_ref_hex') or '')}, "
                f"{_sql_literal(item.get('physical_person_name') or '')}, "
                f"{_sql_literal(item.get('computer_name') or '')}, "
                f"{_sql_literal(item.get('extension') or '')}, "
                f"{_sql_literal(item.get('store_ref_hex') or '')}, "
                f"{_sql_literal(item.get('store_code') or '')}, "
                f"{_sql_literal(item.get('store_name') or '')}, "
                f"{_sql_literal(item.get('department_ref_hex') or '')}, "
                f"{_sql_literal(item.get('department_code') or '')}, "
                f"{_sql_literal(item.get('department_name') or '')}, "
                f"{_sql_literal(item.get('employment_status') or '')}, "
                f"{_sql_literal(item.get('staff_store_ref') or '')}, "
                f"{_sql_literal(item.get('staff_store_name') or '')}, "
                f"{_sql_literal(item.get('staff_department_ref') or '')}, "
                f"{_sql_literal(item.get('staff_department_name') or '')}, "
                f"{_sql_literal(item.get('bitrix_user_id') or '')}, "
                f"{_sql_literal(item.get('bitrix_full_name') or '')}, "
                f"{_sql_literal(item.get('mdm_employee_code') or '')}, "
                f"{_sql_literal(item.get('bitrix_status') or '')}, "
                f"{str(bool(item.get('is_marked'))).lower()}, "
                f"{str(bool(item.get('has_extension'))).lower()}, "
                f"{str(bool(item.get('has_bitrix'))).lower()}, "
                f"{_sql_literal(revision)}, "
                "now()"
                ")"
            )
        statements.append("""
            INSERT INTO telephony_employee_snapshot_stage(
                user_ref_hex,
                snapshot_date,
                mapping_source,
                user_name,
                physical_person_ref_hex,
                physical_person_name,
                computer_name,
                extension,
                store_ref_hex,
                store_code,
                store_name,
                department_ref_hex,
                department_code,
                department_name,
                employment_status,
                staff_store_ref,
                staff_store_name,
                staff_department_ref,
                staff_department_name,
                bitrix_user_id,
                bitrix_full_name,
                mdm_employee_code,
                bitrix_status,
                is_marked,
                has_extension,
                has_bitrix,
                revision,
                loaded_at
            )
            VALUES
            """ + ",\n".join(value_rows) + ";")
    _run_psql("\n".join(statements), database_url=database_url)
    return {
        "staged_rows": len(items),
        "snapshot_date": snapshot_date,
        "revision": revision,
    }


def _build_line_map_diff(
    items: list[dict[str, Any]],
    *,
    existing_rows: dict[str, dict[str, str]] | None,
    preserve_line_ids: Collection[str] | None = None,
) -> dict[str, Any]:
    preserved = {
        line_id.strip() for line_id in preserve_line_ids or [] if line_id and line_id.strip()
    }
    desired_by_line = {
        str(item["line_id"]).strip(): item
        for item in items
        if str(item.get("line_id") or "").strip()
    }
    current = existing_rows or {}

    unchanged: list[str] = []
    changed: list[dict[str, Any]] = []
    stage_only: list[str] = []
    production_only: list[str] = []
    preserved_missing: list[str] = []

    for line_id, desired in desired_by_line.items():
        current_row = current.get(line_id)
        if current_row is None:
            stage_only.append(line_id)
            continue
        comparable_current = {
            "phone_number": _normalize_phone(current_row.get("phone_number") or ""),
            "store_id": str(current_row.get("store_id") or "").strip(),
            "store_name": str(current_row.get("store_name") or "").strip(),
        }
        comparable_desired = {
            "phone_number": _normalize_phone(str(desired.get("phone_number") or "").strip()),
            "store_id": str(desired.get("store_id") or "").strip(),
            "store_name": str(desired.get("store_name") or "").strip(),
        }
        if comparable_current == comparable_desired:
            unchanged.append(line_id)
            continue
        changed.append(
            {
                "line_id": line_id,
                "current": comparable_current,
                "desired": comparable_desired,
            }
        )

    for line_id in sorted(current):
        if line_id in desired_by_line:
            continue
        if line_id in preserved:
            preserved_missing.append(line_id)
            continue
        production_only.append(line_id)

    return {
        "current_active_rows": len(current),
        "desired_rows": len(desired_by_line),
        "unchanged": len(unchanged),
        "changed": len(changed),
        "stage_only": len(stage_only),
        "production_only": len(production_only),
        "preserved_missing": len(preserved_missing),
        "changed_line_ids": [item["line_id"] for item in changed[:20]],
        "stage_only_line_ids": stage_only[:20],
        "production_only_line_ids": production_only[:20],
        "preserved_missing_line_ids": preserved_missing[:20],
    }


def _write_diff_artifact(
    *,
    artifact_dir: Path,
    snapshot_date: str,
    revision: str,
    diff_summary: dict[str, Any],
) -> Path:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    path = artifact_dir / f"telephony-retail-line-map-diff-{snapshot_date}-{revision}.json"
    path.write_text(
        json.dumps(diff_summary, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return path


def upsert_retail_line_map_rows(
    items: list[dict[str, Any]],
    *,
    database_url: str,
    deactivate_missing: bool,
    preserve_line_ids: Collection[str] | None = None,
) -> dict[str, int]:
    _ensure_schema(database_url=database_url)
    existing_line_ids = _fetch_existing_line_ids(database_url=database_url)
    inserted = 0
    updated = 0
    preserved = {
        line_id.strip() for line_id in preserve_line_ids or [] if line_id and line_id.strip()
    }

    for item in items:
        line_id = item["line_id"]
        phone_number = item["phone_number"]
        store_id = item["store_id"]
        store_name = item["store_name"]
        sql = f"""
        WITH updated AS (
            UPDATE retail_line_map
            SET phone_number = { _sql_literal(phone_number) },
                store_id = { _sql_literal(store_id) },
                store_name = { _sql_literal(store_name) },
                is_active = true,
                updated_at = now()
            WHERE line_id = { _sql_literal(line_id) }
            RETURNING 1
        )
        INSERT INTO retail_line_map(line_id, phone_number, store_id, store_name, is_active, created_at, updated_at)
        SELECT
            { _sql_literal(line_id) },
            { _sql_literal(phone_number) },
            { _sql_literal(store_id) },
            { _sql_literal(store_name) },
            true,
            now(),
            now()
        WHERE NOT EXISTS (SELECT 1 FROM updated);
        """
        _run_psql(sql, database_url=database_url)
        if line_id in existing_line_ids:
            updated += 1
        else:
            inserted += 1

    deactivated = 0
    if deactivate_missing and items:
        line_ids = [item["line_id"] for item in items]
        line_list = ",".join(_sql_literal(line_id) for line_id in line_ids)
        preserved_clause = ""
        if preserved:
            preserved_list = ",".join(_sql_literal(line_id) for line_id in sorted(preserved))
            preserved_clause = f"\n              AND line_id NOT IN ({preserved_list})"
        sql = f"""
        WITH changed AS (
            UPDATE retail_line_map
            SET is_active = false,
                updated_at = now()
            WHERE COALESCE(line_id, '') <> ''
              AND line_id NOT IN ({line_list})
              {preserved_clause}
              AND COALESCE(is_active, true) = true
            RETURNING 1
        )
        SELECT count(*)::text FROM changed;
        """
        output = _run_psql(sql, database_url=database_url).strip()
        deactivated = int(output or "0")

    return {
        "inserted": inserted,
        "updated": updated,
        "deactivated": deactivated,
    }


def sync_telephony_retail_line_map(
    *,
    fetch_json: Callable[[str, dict[str, str]], Any],
    upsert_rows: Callable[..., dict[str, int]],
    state_path: Path,
    artifact_dir: Path,
    snapshot_date: date | None = None,
    deactivate_missing: bool = True,
    preserve_line_ids: Collection[str] | None = None,
    load_existing_rows: Callable[[], dict[str, dict[str, str]]] | None = None,
    stage_rows: Callable[[list[dict[str, Any]], str, str], dict[str, Any]] | None = None,
    stage_employee_rows: Callable[[list[dict[str, Any]], str, str], dict[str, Any]] | None = None,
    accepted_health_statuses: Collection[str] = ("ok", "ready"),
    dry_run: bool = False,
) -> dict[str, Any]:
    params = {"active_only": "true"}
    if snapshot_date is not None:
        params["snapshot_date"] = snapshot_date.isoformat()

    try:
        health = fetch_json(
            "/api/management/telephony/health",
            {"date": snapshot_date.isoformat()} if snapshot_date is not None else {},
        )
        payload = fetch_json("/api/management/telephony/retail-line-map", params)
        employee_payload = fetch_json(
            "/api/management/telephony/employee-line-map",
            {
                **(
                    {"snapshot_date": snapshot_date.isoformat()}
                    if snapshot_date is not None
                    else {}
                ),
                "active_only": "true",
                "with_extension_only": "true",
            },
        )
    except Exception as error:
        return {
            "status": "error",
            "snapshot_date": snapshot_date.isoformat() if snapshot_date is not None else None,
            "health_status": "unavailable",
            "fetched": 0,
            "delivered": 0,
            "noop": 0,
            "failed": 1,
            "actions": [{"action": "error", "error": str(error)}],
        }

    health_status = str(health.get("status") or "unknown")
    accepted_statuses = {status.strip().lower() for status in accepted_health_statuses}
    if health_status.strip().lower() not in accepted_statuses:
        return {
            "status": "error",
            "snapshot_date": snapshot_date.isoformat() if snapshot_date is not None else None,
            "health_status": health_status,
            "fetched": 0,
            "delivered": 0,
            "noop": 0,
            "failed": 1,
            "actions": [
                {
                    "action": "error",
                    "error": "telephony source health is not acceptable",
                    "health_status": health_status,
                }
            ],
        }

    items = _normalize_items(_payload_items(payload))
    employee_items = _normalize_employee_items(_payload_items(employee_payload))
    effective_snapshot_date = str(payload.get("snapshot_date") or snapshot_date or date.today())
    revision = _build_revision(
        effective_snapshot_date,
        items,
        employee_items=employee_items,
    )
    report_key = f"{REPORT_KEY_PREFIX}|{effective_snapshot_date}"
    artifact_path = _write_artifact_csv(
        artifact_dir=artifact_dir,
        snapshot_date=effective_snapshot_date,
        revision=revision,
        items=items,
    )
    employee_artifact_path = _write_employee_artifact_csv(
        artifact_dir=artifact_dir,
        snapshot_date=effective_snapshot_date,
        revision=revision,
        items=employee_items,
    )
    diff_summary: dict[str, Any] | None = None
    diff_artifact_path: Path | None = None
    if load_existing_rows is not None:
        diff_summary = _build_line_map_diff(
            items,
            existing_rows=load_existing_rows(),
            preserve_line_ids=preserve_line_ids,
        )
        diff_artifact_path = _write_diff_artifact(
            artifact_dir=artifact_dir,
            snapshot_date=effective_snapshot_date,
            revision=revision,
            diff_summary=diff_summary,
        )
    stage_summary: dict[str, Any] | None = None
    if stage_rows is not None:
        stage_summary = stage_rows(items, effective_snapshot_date, revision)
    employee_stage_summary: dict[str, Any] | None = None
    if stage_employee_rows is not None:
        employee_stage_summary = stage_employee_rows(
            employee_items,
            effective_snapshot_date,
            revision,
        )

    state = _load_state(state_path)
    state_key = f"{report_key}|r{revision}"
    existing = state["reports"].get(state_key)
    has_production_drift = bool(
        diff_summary
        and (
            diff_summary.get("changed")
            or diff_summary.get("stage_only")
            or diff_summary.get("production_only")
        )
    )
    if existing and existing.get("delivery_status") == "delivered" and not has_production_drift:
        return {
            "status": "ok",
            "snapshot_date": effective_snapshot_date,
            "health_status": health_status,
            "fetched": len(items),
            "delivered": 0,
            "noop": 1,
            "failed": 0,
            "revision": revision,
            "diff": diff_summary,
            "stage": stage_summary,
            "employee_fetched": len(employee_items),
            "employee_stage": employee_stage_summary,
            "actions": [
                {
                    "action": "noop",
                    "report_key": report_key,
                    "revision": revision,
                    "artifact_path": str(artifact_path),
                    "employee_artifact_path": str(employee_artifact_path),
                    "diff_artifact_path": str(diff_artifact_path) if diff_artifact_path else None,
                }
            ],
        }

    if not items:
        return {
            "status": "ok",
            "snapshot_date": effective_snapshot_date,
            "health_status": health_status,
            "fetched": 0,
            "delivered": 0,
            "noop": 1,
            "failed": 0,
            "revision": revision,
            "diff": diff_summary,
            "stage": stage_summary,
            "employee_fetched": len(employee_items),
            "employee_stage": employee_stage_summary,
            "actions": [
                {
                    "action": "noop_empty",
                    "report_key": report_key,
                    "revision": revision,
                    "artifact_path": str(artifact_path),
                    "employee_artifact_path": str(employee_artifact_path),
                    "diff_artifact_path": str(diff_artifact_path) if diff_artifact_path else None,
                }
            ],
        }

    upsert_summary = {"inserted": 0, "updated": 0, "deactivated": 0}
    if not dry_run:
        upsert_summary = upsert_rows(
            items,
            deactivate_missing=deactivate_missing,
            preserve_line_ids=preserve_line_ids or (),
        )
        state["reports"][state_key] = {
            "report_key": report_key,
            "snapshot_date": effective_snapshot_date,
            "revision": revision,
            "delivery_status": "delivered",
            "fetched": len(items),
            "artifact_path": str(artifact_path),
            "employee_fetched": len(employee_items),
            "employee_artifact_path": str(employee_artifact_path),
            "diff": diff_summary,
            "diff_artifact_path": str(diff_artifact_path) if diff_artifact_path else None,
            "stage": stage_summary,
            "employee_stage": employee_stage_summary,
            **upsert_summary,
        }
        _save_state(state_path, state)

    action = {
        "action": "deliver" if not dry_run else "dry_run",
        "report_key": report_key,
        "revision": revision,
        "artifact_path": str(artifact_path),
        "employee_artifact_path": str(employee_artifact_path),
        "diff_artifact_path": str(diff_artifact_path) if diff_artifact_path else None,
        **upsert_summary,
    }
    return {
        "status": "ok",
        "snapshot_date": effective_snapshot_date,
        "health_status": health_status,
        "fetched": len(items),
        "delivered": 0 if dry_run else 1,
        "noop": 0,
        "failed": 0,
        "revision": revision,
        "diff": diff_summary,
        "stage": stage_summary,
        "employee_fetched": len(employee_items),
        "employee_stage": employee_stage_summary,
        "actions": [action],
        **upsert_summary,
    }


def render_summary(summary: dict[str, Any]) -> str:
    lines = [
        f"telephony_line_map_from_a: {summary.get('status', 'unknown')}",
        f"snapshot_date: {summary.get('snapshot_date')}",
        f"health_status: {summary.get('health_status')}",
        f"fetched: {summary.get('fetched', 0)}",
        f"delivered: {summary.get('delivered', 0)}",
        f"noop: {summary.get('noop', 0)}",
        f"failed: {summary.get('failed', 0)}",
    ]
    if summary.get("revision"):
        lines.append(f"revision: {summary['revision']}")
    diff = summary.get("diff") or {}
    if diff:
        lines.append(
            "diff: "
            f"unchanged={diff.get('unchanged', 0)} "
            f"changed={diff.get('changed', 0)} "
            f"stage_only={diff.get('stage_only', 0)} "
            f"production_only={diff.get('production_only', 0)} "
            f"preserved_missing={diff.get('preserved_missing', 0)}"
        )
    stage = summary.get("stage") or {}
    if stage:
        lines.append(f"staged_rows: {stage.get('staged_rows', 0)}")
    employee_stage = summary.get("employee_stage") or {}
    if employee_stage:
        lines.append(f"employee_staged_rows: {employee_stage.get('staged_rows', 0)}")
    if summary.get("actions"):
        for action in summary["actions"]:
            lines.append(
                "action: "
                f"{action.get('action')} "
                f"report_key={action.get('report_key')} "
                f"revision={action.get('revision')}"
            )
    return "\n".join(lines)


def main() -> None:
    args = _parse_args()
    env = _load_env(
        os.getenv("TELEPHONY_LINE_MAP_ENV_FILE")
        or os.getenv("OPENCLAW_ENV_FILE")
        or os.getenv("PRICING_ENV_FILE")
        or DEFAULT_LOCAL_ENV_FILE
    )
    source_url = (
        env.get("TELEPHONY_LINE_MAP_SOURCE_URL")
        or env.get("MANAGEMENT_SOURCE_URL")
        or DEFAULT_LOCAL_SOURCE_URL
    )
    source_token = (
        env.get("TELEPHONY_LINE_MAP_SOURCE_TOKEN")
        or env.get("MANAGEMENT_SOURCE_TOKEN")
        or env.get("MANAGEMENT_INTERNAL_API_TOKEN")
        or ""
    )
    database_url = env.get("DATABASE_URL") or ""
    state_path = Path(env.get("TELEPHONY_LINE_MAP_STATE_PATH") or DEFAULT_STATE_PATH)
    artifact_dir = Path(env.get("TELEPHONY_LINE_MAP_ARTIFACT_DIR") or DEFAULT_ARTIFACT_DIR)
    timeout = int(env.get("TELEPHONY_LINE_MAP_TIMEOUT_SECONDS") or 20)
    retries = int(env.get("TELEPHONY_LINE_MAP_RETRIES") or 2)
    retry_delay = float(env.get("TELEPHONY_LINE_MAP_RETRY_DELAY_SECONDS") or 1.0)
    deactivate_missing = _env_flag(
        env.get("TELEPHONY_LINE_MAP_DEACTIVATE_MISSING"),
        default=True,
    )
    review_line_ids = _parse_line_id_csv(env.get("TELEPHONY_LINE_MAP_REVIEW_LINE_IDS"))

    if not source_token:
        raise SystemExit("Missing telephony source token")
    if not database_url and not args.dry_run:
        raise SystemExit("Missing DATABASE_URL for retail_line_map sync")

    fetch_json = _build_fetcher(
        source_url=source_url,
        token=source_token,
        timeout=timeout,
        retries=retries,
        retry_delay=retry_delay,
    )

    def upsert(
        items: list[dict[str, Any]],
        deactivate_missing: bool,
        preserve_line_ids: Collection[str],
    ) -> dict[str, int]:
        return upsert_retail_line_map_rows(
            items,
            database_url=database_url,
            deactivate_missing=deactivate_missing,
            preserve_line_ids=preserve_line_ids,
        )

    summary = sync_telephony_retail_line_map(
        fetch_json=fetch_json,
        upsert_rows=upsert,
        state_path=state_path,
        artifact_dir=artifact_dir,
        snapshot_date=_parse_date(args.snapshot_date),
        deactivate_missing=deactivate_missing,
        preserve_line_ids=review_line_ids,
        load_existing_rows=(
            None if not database_url else lambda: _fetch_existing_rows(database_url=database_url)
        ),
        stage_rows=(
            None
            if not database_url
            else lambda items, snapshot_date, revision: _replace_stage_rows(
                items,
                database_url=database_url,
                snapshot_date=snapshot_date,
                revision=revision,
            )
        ),
        stage_employee_rows=(
            None
            if not database_url
            else lambda items, snapshot_date, revision: _replace_employee_stage_rows(
                items,
                database_url=database_url,
                snapshot_date=snapshot_date,
                revision=revision,
            )
        ),
        dry_run=args.dry_run,
    )

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return
    print(render_summary(summary))


if __name__ == "__main__":
    main()
