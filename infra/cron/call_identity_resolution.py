#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
from pathlib import Path

DEFAULT_ENV_FILE = "/home/deploy/.openclaw/.env"
MSK = dt.timezone(dt.timedelta(hours=3))


def load_env(path: str) -> dict[str, str]:
    env = os.environ.copy()
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def sql_literal(value: str) -> str:
    return "'" + (value or "").replace("'", "''") + "'"


def run_psql(sql: str, env: dict[str, str], capture: bool = True) -> str:
    proc = subprocess.run(
        ["psql", env["DATABASE_URL"], "-v", "ON_ERROR_STOP=1", "-At", "-F", "\t", "-c", sql],
        text=True,
        capture_output=True,
        env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "psql failed").strip())
    return (proc.stdout or "") if capture else ""


def _normalized_phone_sql(column_sql: str) -> str:
    digits = f"regexp_replace(COALESCE({column_sql}, ''), '\\D', '', 'g')"
    return (
        "CASE "
        f"WHEN length({digits}) = 11 AND left({digits}, 1) = '8' "
        f"THEN '+7' || substring({digits} from 2) "
        f"WHEN length({digits}) > 0 AND left({digits}, 1) = '7' "
        f"THEN '+' || {digits} "
        f"WHEN length({digits}) > 0 THEN '+' || {digits} "
        "ELSE '' "
        "END"
    )


def ensure_resolution_schema(env: dict[str, str]) -> None:
    sql = """
    ALTER TABLE calls ADD COLUMN IF NOT EXISTS resolved_manager_id bigint;
    ALTER TABLE calls ADD COLUMN IF NOT EXISTS resolved_manager_name text;
    ALTER TABLE calls ADD COLUMN IF NOT EXISTS resolved_store_id text;
    ALTER TABLE calls ADD COLUMN IF NOT EXISTS resolved_store_name text;
    ALTER TABLE calls ADD COLUMN IF NOT EXISTS resolved_line_id text;
    ALTER TABLE calls ADD COLUMN IF NOT EXISTS resolution_source text NOT NULL DEFAULT 'unresolved';
    ALTER TABLE calls ADD COLUMN IF NOT EXISTS manager_resolution_conflict boolean NOT NULL DEFAULT false;

    CREATE INDEX IF NOT EXISTS idx_calls_resolved_manager_started_at
    ON calls(resolved_manager_id, started_at DESC);

    CREATE INDEX IF NOT EXISTS idx_calls_resolved_store_started_at
    ON calls(resolved_store_id, started_at DESC);

    CREATE INDEX IF NOT EXISTS idx_calls_resolved_line_started_at
    ON calls(resolved_line_id, started_at DESC);

    CREATE INDEX IF NOT EXISTS idx_calls_resolution_source_started_at
    ON calls(resolution_source, started_at DESC);

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

    ALTER TABLE retail_line_map_stage ADD COLUMN IF NOT EXISTS store_names text NOT NULL DEFAULT '';
    ALTER TABLE retail_line_map_stage ADD COLUMN IF NOT EXISTS employee_names text NOT NULL DEFAULT '';
    ALTER TABLE retail_line_map_stage ADD COLUMN IF NOT EXISTS bitrix_user_ids text NOT NULL DEFAULT '';
    ALTER TABLE retail_line_map_stage ADD COLUMN IF NOT EXISTS primary_bitrix_user_id text NOT NULL DEFAULT '';
    ALTER TABLE retail_line_map_stage ADD COLUMN IF NOT EXISTS primary_employee_name text NOT NULL DEFAULT '';
    ALTER TABLE retail_line_map_stage ADD COLUMN IF NOT EXISTS primary_store_name text NOT NULL DEFAULT '';
    """
    run_psql(sql, env, capture=False)


def _build_where_sql(
    *,
    call_id: str | None = None,
    start_at: str | None = None,
    end_at: str | None = None,
    source: str | None = None,
) -> str:
    clauses: list[str] = []
    if call_id:
        clauses.append(f"c.call_id = {sql_literal(call_id)}")
    if start_at:
        clauses.append(f"c.started_at >= {sql_literal(start_at)}::timestamptz")
    if end_at:
        clauses.append(f"c.started_at < {sql_literal(end_at)}::timestamptz")
    if source:
        clauses.append(f"COALESCE(c.source, 'bitrix') = {sql_literal(source)}")
    if not clauses:
        now_msk = dt.datetime.now(MSK)
        start_at = (now_msk - dt.timedelta(days=2)).strftime("%Y-%m-%dT00:00:00+03:00")
        end_at = now_msk.strftime("%Y-%m-%dT23:59:59+03:00")
        clauses.extend(
            [
                f"c.started_at >= {sql_literal(start_at)}::timestamptz",
                f"c.started_at < {sql_literal(end_at)}::timestamptz",
            ]
        )
    return " AND ".join(clauses)


def _resolution_cte(where_sql: str) -> str:
    phone_sql = _normalized_phone_sql("c.phone")
    portal_phone_sql = _normalized_phone_sql("c.portal_number")
    return f"""
    WITH targets AS (
        SELECT
            c.id,
            c.call_id,
            c.started_at,
            COALESCE(c.manager_id::text, '') AS raw_manager_id,
            COALESCE(NULLIF(c.line_id, ''), '') AS raw_line_id,
            COALESCE(NULLIF(c.store_id, ''), 'unknown') AS raw_store_id,
            COALESCE(NULLIF(c.external_call_id, ''), '') AS external_call_id,
            COALESCE(NULLIF(c.call_record_url, ''), '') AS call_record_url,
            {phone_sql} AS phone_norm,
            {portal_phone_sql} AS portal_number_norm
        FROM calls c
        WHERE {where_sql}
    ),
    peer_retail AS (
        SELECT
            t.id,
            max(NULLIF(peer.line_id, '')) AS peer_line_id
        FROM targets t
        LEFT JOIN calls peer
          ON peer.id <> t.id
         AND COALESCE(peer.source, '') = 'retail_megafon'
         AND (
              (t.external_call_id <> '' AND COALESCE(peer.external_call_id, '') = t.external_call_id)
           OR (t.call_record_url <> '' AND COALESCE(peer.call_record_url, '') = t.call_record_url)
         )
        GROUP BY t.id
    ),
    phone_route AS (
        SELECT
            t.id,
            max(s.line_id) FILTER (WHERE COALESCE(s.line_id, '') <> '') AS phone_line_id
        FROM targets t
        LEFT JOIN retail_line_map_stage s
          ON COALESCE(s.phone_number, '') <> ''
         AND COALESCE(s.phone_number, '') IN (t.portal_number_norm, t.phone_norm)
        GROUP BY t.id
    ),
    effective_line AS (
        SELECT
            t.id,
            COALESCE(
                NULLIF(t.raw_line_id, ''),
                NULLIF(pr.peer_line_id, ''),
                NULLIF(ph.phone_line_id, ''),
                ''
            ) AS line_id,
            CASE
                WHEN NULLIF(t.raw_line_id, '') IS NOT NULL THEN 'raw_line'
                WHEN NULLIF(pr.peer_line_id, '') IS NOT NULL THEN 'peer_retail'
                WHEN NULLIF(ph.phone_line_id, '') IS NOT NULL THEN 'phone_map'
                ELSE ''
            END AS line_origin
        FROM targets t
        LEFT JOIN peer_retail pr ON pr.id = t.id
        LEFT JOIN phone_route ph ON ph.id = t.id
    ),
    employee_by_line AS (
        SELECT
            el.id,
            count(DISTINCT es.bitrix_user_id) FILTER (WHERE COALESCE(es.bitrix_user_id, '') <> '') AS active_bitrix_count,
            max(es.bitrix_user_id) FILTER (WHERE COALESCE(es.bitrix_user_id, '') <> '') AS bitrix_user_id,
            max(
                COALESCE(
                    NULLIF(es.bitrix_full_name, ''),
                    NULLIF(es.physical_person_name, ''),
                    NULLIF(es.user_name, '')
                )
            ) FILTER (WHERE COALESCE(es.bitrix_user_id, '') <> '') AS manager_name,
            max(
                COALESCE(
                    NULLIF(NULLIF(es.store_ref_hex, '0x00000000000000000000000000000000'), ''),
                    NULLIF(es.store_code, ''),
                    NULLIF(NULLIF(es.staff_store_ref, '0x00000000000000000000000000000000'), ''),
                    'unknown'
                )
            ) FILTER (WHERE COALESCE(es.bitrix_user_id, '') <> '') AS store_id,
            max(
                COALESCE(
                    NULLIF(es.store_name, ''),
                    NULLIF(es.department_name, ''),
                    NULLIF(es.staff_department_name, ''),
                    NULLIF(es.staff_store_name, ''),
                    'unknown'
                )
            ) FILTER (WHERE COALESCE(es.bitrix_user_id, '') <> '') AS store_name
        FROM effective_line el
        LEFT JOIN telephony_employee_snapshot_stage es
          ON COALESCE(es.extension, '') = el.line_id
         AND lower(COALESCE(es.employment_status, '')) = 'active'
        GROUP BY el.id
    ),
    line_stage AS (
        SELECT
            el.id,
            COALESCE(NULLIF(s.mapping_mode, ''), '') AS mapping_mode,
            COALESCE(NULLIF(s.primary_bitrix_user_id, ''), '') AS primary_bitrix_user_id,
            COALESCE(NULLIF(s.primary_employee_name, ''), '') AS primary_employee_name,
            COALESCE(NULLIF(s.store_id, ''), '') AS line_store_id,
            COALESCE(NULLIF(s.store_name, ''), '') AS line_store_name
        FROM effective_line el
        LEFT JOIN retail_line_map_stage s
          ON s.line_id = el.line_id
    ),
    employee_by_primary_user AS (
        SELECT
            ls.id,
            count(DISTINCT es.bitrix_user_id) FILTER (WHERE COALESCE(es.bitrix_user_id, '') <> '') AS match_count,
            max(es.bitrix_user_id) FILTER (WHERE COALESCE(es.bitrix_user_id, '') <> '') AS bitrix_user_id,
            max(
                COALESCE(
                    NULLIF(es.bitrix_full_name, ''),
                    NULLIF(es.physical_person_name, ''),
                    NULLIF(es.user_name, '')
                )
            ) FILTER (WHERE COALESCE(es.bitrix_user_id, '') <> '') AS manager_name,
            max(
                COALESCE(
                    NULLIF(NULLIF(es.store_ref_hex, '0x00000000000000000000000000000000'), ''),
                    NULLIF(es.store_code, ''),
                    NULLIF(NULLIF(es.staff_store_ref, '0x00000000000000000000000000000000'), ''),
                    'unknown'
                )
            ) FILTER (WHERE COALESCE(es.bitrix_user_id, '') <> '') AS store_id,
            max(
                COALESCE(
                    NULLIF(es.store_name, ''),
                    NULLIF(es.department_name, ''),
                    NULLIF(es.staff_department_name, ''),
                    NULLIF(es.staff_store_name, ''),
                    'unknown'
                )
            ) FILTER (WHERE COALESCE(es.bitrix_user_id, '') <> '') AS store_name
        FROM line_stage ls
        LEFT JOIN telephony_employee_snapshot_stage es
          ON COALESCE(es.bitrix_user_id, '') = ls.primary_bitrix_user_id
         AND lower(COALESCE(es.employment_status, '')) = 'active'
        GROUP BY ls.id
    ),
    employee_by_raw_manager AS (
        SELECT
            t.id,
            count(DISTINCT es.bitrix_user_id) FILTER (WHERE COALESCE(es.bitrix_user_id, '') <> '') AS match_count,
            max(es.bitrix_user_id) FILTER (WHERE COALESCE(es.bitrix_user_id, '') <> '') AS bitrix_user_id,
            max(
                COALESCE(
                    NULLIF(es.bitrix_full_name, ''),
                    NULLIF(es.physical_person_name, ''),
                    NULLIF(es.user_name, '')
                )
            ) FILTER (WHERE COALESCE(es.bitrix_user_id, '') <> '') AS manager_name,
            max(
                COALESCE(
                    NULLIF(NULLIF(es.store_ref_hex, '0x00000000000000000000000000000000'), ''),
                    NULLIF(es.store_code, ''),
                    NULLIF(NULLIF(es.staff_store_ref, '0x00000000000000000000000000000000'), ''),
                    'unknown'
                )
            ) FILTER (WHERE COALESCE(es.bitrix_user_id, '') <> '') AS store_id,
            max(
                COALESCE(
                    NULLIF(es.store_name, ''),
                    NULLIF(es.department_name, ''),
                    NULLIF(es.staff_department_name, ''),
                    NULLIF(es.staff_store_name, ''),
                    'unknown'
                )
            ) FILTER (WHERE COALESCE(es.bitrix_user_id, '') <> '') AS store_name
        FROM targets t
        LEFT JOIN telephony_employee_snapshot_stage es
          ON COALESCE(es.bitrix_user_id, '') = t.raw_manager_id
         AND lower(COALESCE(es.employment_status, '')) = 'active'
        GROUP BY t.id
    ),
    final AS (
        SELECT
            t.id,
            CASE
                WHEN ebl.active_bitrix_count = 1 THEN NULLIF(ebl.bitrix_user_id, '')::bigint
                WHEN ls.mapping_mode = 'single_active_bitrix_user' AND epu.match_count >= 1
                    THEN NULLIF(epu.bitrix_user_id, '')::bigint
                WHEN erm.match_count = 1 THEN NULLIF(erm.bitrix_user_id, '')::bigint
                ELSE NULL
            END AS resolved_manager_id,
            CASE
                WHEN ebl.active_bitrix_count = 1 THEN COALESCE(NULLIF(ebl.manager_name, ''), '')
                WHEN ls.mapping_mode = 'single_active_bitrix_user' AND epu.match_count >= 1
                    THEN COALESCE(NULLIF(epu.manager_name, ''), NULLIF(ls.primary_employee_name, ''), '')
                WHEN erm.match_count = 1 THEN COALESCE(NULLIF(erm.manager_name, ''), '')
                ELSE ''
            END AS resolved_manager_name,
            CASE
                WHEN ebl.active_bitrix_count = 1 THEN COALESCE(NULLIF(ebl.store_id, ''), 'unknown')
                WHEN ls.mapping_mode = 'single_active_bitrix_user' AND epu.match_count >= 1
                    THEN COALESCE(NULLIF(epu.store_id, ''), NULLIF(ls.line_store_id, ''), 'unknown')
                WHEN ls.mapping_mode IN ('shared_extension', 'service_overlay', 'no_active_owner', 'single_active_without_bitrix')
                    AND COALESCE(NULLIF(el.line_id, ''), '') <> ''
                    THEN COALESCE(NULLIF(ls.line_store_id, ''), 'telephony_line_' || el.line_id)
                WHEN erm.match_count = 1 THEN COALESCE(NULLIF(erm.store_id, ''), 'unknown')
                ELSE 'unknown'
            END AS resolved_store_id,
            CASE
                WHEN ebl.active_bitrix_count = 1 THEN COALESCE(NULLIF(ebl.store_name, ''), '')
                WHEN ls.mapping_mode = 'single_active_bitrix_user' AND epu.match_count >= 1
                    THEN COALESCE(NULLIF(epu.store_name, ''), NULLIF(ls.line_store_name, ''), '')
                WHEN ls.mapping_mode IN ('shared_extension', 'service_overlay', 'no_active_owner', 'single_active_without_bitrix')
                    AND COALESCE(NULLIF(el.line_id, ''), '') <> ''
                    THEN COALESCE(NULLIF(ls.line_store_name, ''), 'Line ' || el.line_id)
                WHEN erm.match_count = 1 THEN COALESCE(NULLIF(erm.store_name, ''), '')
                ELSE ''
            END AS resolved_store_name,
            NULLIF(el.line_id, '') AS resolved_line_id,
            CASE
                WHEN ebl.active_bitrix_count = 1 THEN 'onec_extension'
                WHEN ls.mapping_mode = 'single_active_bitrix_user' AND epu.match_count >= 1 AND el.line_origin = 'phone_map'
                    THEN 'phone_map'
                WHEN ls.mapping_mode = 'single_active_bitrix_user' AND epu.match_count >= 1
                    THEN 'line_map'
                WHEN ls.mapping_mode IN ('shared_extension', 'service_overlay', 'no_active_owner', 'single_active_without_bitrix')
                    AND COALESCE(NULLIF(el.line_id, ''), '') <> '' AND el.line_origin = 'phone_map'
                    THEN 'phone_map'
                WHEN ls.mapping_mode IN ('shared_extension', 'service_overlay', 'no_active_owner', 'single_active_without_bitrix')
                    AND COALESCE(NULLIF(el.line_id, ''), '') <> ''
                    THEN 'line_map'
                WHEN erm.match_count = 1 THEN 'bitrix_raw_fallback'
                ELSE 'unresolved'
            END AS resolution_source,
            CASE
                WHEN t.raw_manager_id <> ''
                 AND (
                    CASE
                        WHEN ebl.active_bitrix_count = 1 THEN COALESCE(ebl.bitrix_user_id, '')
                        WHEN ls.mapping_mode = 'single_active_bitrix_user' AND epu.match_count >= 1
                            THEN COALESCE(epu.bitrix_user_id, '')
                        WHEN erm.match_count = 1 THEN COALESCE(erm.bitrix_user_id, '')
                        ELSE ''
                    END
                 ) <> ''
                 AND t.raw_manager_id <> (
                    CASE
                        WHEN ebl.active_bitrix_count = 1 THEN COALESCE(ebl.bitrix_user_id, '')
                        WHEN ls.mapping_mode = 'single_active_bitrix_user' AND epu.match_count >= 1
                            THEN COALESCE(epu.bitrix_user_id, '')
                        WHEN erm.match_count = 1 THEN COALESCE(erm.bitrix_user_id, '')
                        ELSE ''
                    END
                 )
                THEN true
                ELSE false
            END AS manager_resolution_conflict
        FROM targets t
        LEFT JOIN effective_line el ON el.id = t.id
        LEFT JOIN employee_by_line ebl ON ebl.id = t.id
        LEFT JOIN line_stage ls ON ls.id = t.id
        LEFT JOIN employee_by_primary_user epu ON epu.id = t.id
        LEFT JOIN employee_by_raw_manager erm ON erm.id = t.id
    )
    """


def resolve_call_identities(
    env: dict[str, str],
    *,
    call_id: str | None = None,
    start_at: str | None = None,
    end_at: str | None = None,
    source: str | None = None,
) -> dict[str, int | str]:
    ensure_resolution_schema(env)
    where_sql = _build_where_sql(call_id=call_id, start_at=start_at, end_at=end_at, source=source)
    cte = _resolution_cte(where_sql)

    update_sql = cte + """
    UPDATE calls c
    SET resolved_manager_id = final.resolved_manager_id,
        resolved_manager_name = NULLIF(final.resolved_manager_name, ''),
        resolved_store_id = final.resolved_store_id,
        resolved_store_name = NULLIF(final.resolved_store_name, ''),
        resolved_line_id = final.resolved_line_id,
        resolution_source = final.resolution_source,
        manager_resolution_conflict = final.manager_resolution_conflict,
        updated_at = now()
    FROM final
    WHERE c.id = final.id;
    """
    run_psql(update_sql, env, capture=False)

    summary_sql = cte + """
    SELECT
        count(*)::text,
        sum(CASE WHEN final.resolution_source <> 'unresolved' THEN 1 ELSE 0 END)::text,
        sum(CASE WHEN final.resolution_source = 'unresolved' THEN 1 ELSE 0 END)::text,
        sum(CASE WHEN final.manager_resolution_conflict THEN 1 ELSE 0 END)::text,
        sum(CASE WHEN final.resolution_source = 'onec_extension' THEN 1 ELSE 0 END)::text,
        sum(CASE WHEN final.resolution_source = 'line_map' THEN 1 ELSE 0 END)::text,
        sum(CASE WHEN final.resolution_source = 'phone_map' THEN 1 ELSE 0 END)::text,
        sum(CASE WHEN final.resolution_source = 'bitrix_raw_fallback' THEN 1 ELSE 0 END)::text
    FROM final;
    """
    parts = (run_psql(summary_sql, env, capture=True).strip() or "").split("\t")
    values = [int(part or "0") for part in parts[:8]]
    while len(values) < 8:
        values.append(0)
    summary = {
        "matched_rows": values[0],
        "resolved_rows": values[1],
        "unresolved_rows": values[2],
        "conflict_rows": values[3],
        "onec_extension_rows": values[4],
        "line_map_rows": values[5],
        "phone_map_rows": values[6],
        "bitrix_raw_fallback_rows": values[7],
        "where_sql": where_sql,
    }

    if summary["resolved_rows"] > 0:
        done_sql = f"""
        INSERT INTO events_log(event_type, call_id, level, message, payload)
        VALUES (
          'resolve.call_identity.done',
          'batch',
          'info',
          'Call identity resolution completed',
          jsonb_build_object(
            'matched_rows', {summary['matched_rows']},
            'resolved_rows', {summary['resolved_rows']},
            'onec_extension_rows', {summary['onec_extension_rows']},
            'line_map_rows', {summary['line_map_rows']},
            'phone_map_rows', {summary['phone_map_rows']},
            'bitrix_raw_fallback_rows', {summary['bitrix_raw_fallback_rows']},
            'where_sql', {sql_literal(where_sql)}
          )
        );
        """
        run_psql(done_sql, env, capture=False)

    if summary["conflict_rows"] > 0:
        conflict_sql = f"""
        INSERT INTO events_log(event_type, call_id, level, message, payload)
        VALUES (
          'resolve.call_identity.conflict',
          'batch',
          'warn',
          'Call identity resolution found Bitrix/1C conflicts',
          jsonb_build_object(
            'conflict_rows', {summary['conflict_rows']},
            'where_sql', {sql_literal(where_sql)}
          )
        );
        """
        run_psql(conflict_sql, env, capture=False)

    if summary["unresolved_rows"] > 0:
        unresolved_sql = f"""
        INSERT INTO events_log(event_type, call_id, level, message, payload)
        VALUES (
          'resolve.call_identity.unresolved',
          'batch',
          'warn',
          'Call identity resolution left unresolved rows',
          jsonb_build_object(
            'unresolved_rows', {summary['unresolved_rows']},
            'where_sql', {sql_literal(where_sql)}
          )
        );
        """
        run_psql(unresolved_sql, env, capture=False)

    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Resolve manager/store/line for calls using telephony snapshot from A."
    )
    parser.add_argument("--env-file", default=os.getenv("OPENCLAW_ENV_FILE", DEFAULT_ENV_FILE))
    parser.add_argument("--call-id", help="Resolve a single call_id")
    parser.add_argument("--from-date", help="Start date in YYYY-MM-DD (MSK)")
    parser.add_argument("--to-date", help="End date in YYYY-MM-DD inclusive (MSK)")
    parser.add_argument("--start-at", help="Explicit window start as ISO timestamp")
    parser.add_argument("--end-at", help="Explicit window end as ISO timestamp")
    parser.add_argument("--source", help="Optional source filter")
    parser.add_argument("--json", action="store_true", help="Emit JSON")
    return parser.parse_args()


def _resolve_window(args: argparse.Namespace) -> tuple[str | None, str | None]:
    if args.start_at or args.end_at:
        return args.start_at, args.end_at
    if args.from_date or args.to_date:
        start_day = dt.date.fromisoformat(args.from_date or args.to_date)
        end_day = dt.date.fromisoformat(args.to_date or args.from_date)
        if end_day < start_day:
            raise SystemExit("--to-date must be greater than or equal to --from-date")
        return (
            f"{start_day.isoformat()}T00:00:00+03:00",
            f"{end_day.isoformat()}T23:59:59+03:00",
        )
    return None, None


def main() -> None:
    args = parse_args()
    env = load_env(args.env_file)
    if not env.get("DATABASE_URL"):
        raise SystemExit("Missing required env: DATABASE_URL")
    start_at, end_at = _resolve_window(args)
    summary = resolve_call_identities(
        env,
        call_id=args.call_id,
        start_at=start_at,
        end_at=end_at,
        source=args.source,
    )
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        return
    print(
        "matched_rows={matched_rows} resolved_rows={resolved_rows} "
        "unresolved_rows={unresolved_rows} conflict_rows={conflict_rows} "
        "onec_extension_rows={onec_extension_rows} line_map_rows={line_map_rows} "
        "phone_map_rows={phone_map_rows} bitrix_raw_fallback_rows={bitrix_raw_fallback_rows}".format(
            **summary
        )
    )


if __name__ == "__main__":
    main()
