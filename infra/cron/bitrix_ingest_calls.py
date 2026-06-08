#!/usr/bin/env python3
import argparse
import datetime as dt
import json
import os
import signal
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

try:
    from .call_identity_resolution import ensure_resolution_schema, resolve_call_identities
except ImportError:  # pragma: no cover - script execution path
    from call_identity_resolution import ensure_resolution_schema, resolve_call_identities

ENV_FILE = os.getenv("OPENCLAW_ENV_FILE", "/home/deploy/.openclaw/.env")
DEFAULT_STATE_FILE = "/var/lib/mm-management-orchestrator/bitrix-ingest-calls/progress.json"
MSK = dt.timezone(dt.timedelta(hours=3))
STATE = {
    "stage": "init",
    "page": 0,
    "start": 0,
    "rows_fetched": 0,
    "rows_processed": 0,
}


def load_env(path: str):
    env = os.environ.copy()
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def log(message: str):
    ts = dt.datetime.now(MSK).strftime("%Y-%m-%d %H:%M:%S %Z")
    print(f"[bitrix_ingest_calls {ts}] {message}", flush=True)


def sql_literal(s: str) -> str:
    return "'" + (s or "").replace("'", "''") + "'"


def run_psql(sql: str, env, capture=False):
    cmd = ["psql", env["DATABASE_URL"], "-v", "ON_ERROR_STOP=1", "-At", "-F", "\t", "-c", sql]
    res = subprocess.run(cmd, env=env, text=True, capture_output=True)
    if res.returncode != 0:
        raise RuntimeError((res.stderr or res.stdout or "psql failed").strip())
    return (res.stdout or "") if capture else ""


def ensure_calls_schema(env):
    sql = """
    ALTER TABLE calls ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'bitrix';
    ALTER TABLE calls ADD COLUMN IF NOT EXISTS line_id text;
    ALTER TABLE calls ADD COLUMN IF NOT EXISTS store_id text NOT NULL DEFAULT 'unknown';
    ALTER TABLE calls ADD COLUMN IF NOT EXISTS direction text NOT NULL DEFAULT 'unknown';
    ALTER TABLE calls ADD COLUMN IF NOT EXISTS external_call_id text;
    ALTER TABLE calls ADD COLUMN IF NOT EXISTS portal_number text;
    ALTER TABLE calls ADD COLUMN IF NOT EXISTS call_failed_code text;
    ALTER TABLE calls ADD COLUMN IF NOT EXISTS provider_name text;
    """
    run_psql(sql, env, capture=False)


def b24_call(base: str, method: str, params=None, timeout=240, retries=3):
    params = params or []
    url = f"{base}/{method}.json"
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)

    last_err = None
    for attempt in range(1, retries + 1):
        try:
            started_at = time.monotonic()
            with urllib.request.urlopen(url, timeout=timeout) as r:
                payload = json.loads(r.read().decode("utf-8"))
                elapsed = time.monotonic() - started_at
                log(f"Bitrix {method} ok attempt={attempt}/{retries} elapsed={elapsed:.1f}s")
                return payload
        except Exception as e:
            last_err = e
            log(f"Bitrix {method} failed attempt={attempt}/{retries}: {e}")
            if attempt < retries:
                time.sleep(2 * attempt)
                continue
            raise

    raise RuntimeError(f"Bitrix call failed: {last_err}")


def load_progress(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"progress state unreadable, ignoring: {e}")
        return None


def save_progress(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def clear_progress(path: Path):
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def parse_args():
    parser = argparse.ArgumentParser(
        description="Ingest Bitrix telephony calls into call_analytics."
    )
    parser.add_argument("--plan-only", action="store_true", help="Emit a read-only execution plan")
    parser.add_argument("--json", action="store_true", help="Emit JSON for --plan-only")
    return parser.parse_args()


def resolve_run_settings(env):
    lookback_days = int(env.get("BITRIX_INGEST_LOOKBACK_DAYS", "2"))
    max_pages = int(env.get("BITRIX_INGEST_MAX_PAGES", "200"))
    b24_timeout = int(env.get("BITRIX_INGEST_TIMEOUT_SEC", "90"))
    b24_retries = int(env.get("BITRIX_INGEST_RETRIES", "3"))
    progress_every = max(1, int(env.get("BITRIX_INGEST_PROGRESS_EVERY", "25")))
    state_path = Path(env.get("BITRIX_INGEST_STATE_FILE", DEFAULT_STATE_FILE))
    now_msk = dt.datetime.now(MSK)
    frm = (now_msk - dt.timedelta(days=lookback_days)).strftime("%Y-%m-%dT00:00:00+03:00")
    to = now_msk.strftime("%Y-%m-%dT23:59:59+03:00")
    return {
        "lookback_days": lookback_days,
        "max_pages": max_pages,
        "b24_timeout": b24_timeout,
        "b24_retries": b24_retries,
        "progress_every": progress_every,
        "state_path": state_path,
        "from": frm,
        "to": to,
    }


def resolve_bitrix_ingest_base(env):
    contour = (env.get("BITRIX_INGEST_CONTOUR") or "").strip().lower()
    if contour == "box":
        base = (env.get("BITRIX24_BOX_WEBHOOK_URL") or "").rstrip("/")
        if not base:
            raise ValueError("missing env: BITRIX24_BOX_WEBHOOK_URL for BITRIX_INGEST_CONTOUR=box")
        return base
    if contour and contour not in {"cloud", "legacy"}:
        raise ValueError("BITRIX_INGEST_CONTOUR must be one of: box, cloud, legacy")

    base = (env.get("BITRIX_INGEST_WEBHOOK_URL") or env.get("BITRIX24_WEBHOOK_URL") or "").rstrip(
        "/"
    )
    if not base:
        raise ValueError("missing env: BITRIX_INGEST_WEBHOOK_URL or BITRIX24_WEBHOOK_URL")
    return base


def build_plan(env):
    settings = resolve_run_settings(env)
    errors = []
    try:
        resolve_bitrix_ingest_base(env)
    except ValueError as exc:
        errors.append(str(exc))
    if not env.get("DATABASE_URL"):
        errors.append("missing env: DATABASE_URL")
    state_path = settings["state_path"]
    progress_data = load_progress(state_path)
    stale_progress = bool(
        progress_data
        and (
            progress_data.get("from") != settings["from"]
            or progress_data.get("to") != settings["to"]
        )
    )
    return {
        "status": "blocked" if errors else "ready",
        "side_effects": False,
        "window": {"from": settings["from"], "to": settings["to"]},
        "planned_reads": [
            "Bitrix24 voximplant.statistic.get",
            str(state_path),
        ],
        "planned_writes": [
            "calls upsert",
            "events_log insert",
            "calls identity resolution update",
        ],
        "would_touch_tables": [
            "calls",
            "events_log",
            "retail_line_map_stage",
            "telephony_employee_snapshot_stage",
        ],
        "counts": {
            "lookback_days": settings["lookback_days"],
            "max_pages": settings["max_pages"],
            "progress_exists": state_path.exists(),
            "progress_stale": stale_progress,
            "pending_batch_rows": len((progress_data or {}).get("pending_batch") or []),
        },
        "errors": errors,
    }


def make_progress_payload(
    *,
    frm: str,
    to: str,
    lookback_days: int,
    max_pages: int,
    b24_timeout: int,
    b24_retries: int,
    progress_every: int,
    next_page: int,
    next_start: int,
    rows_fetched: int,
    rows_processed: int,
    inserted: int,
    updated: int,
    skipped: int,
    pending_batch,
    pending_page,
    pending_start,
    pending_index,
    fetch_complete,
):
    return {
        "version": 1,
        "from": frm,
        "to": to,
        "lookback_days": lookback_days,
        "max_pages": max_pages,
        "b24_timeout": b24_timeout,
        "b24_retries": b24_retries,
        "progress_every": progress_every,
        "next_page": next_page,
        "next_start": next_start,
        "rows_fetched": rows_fetched,
        "rows_processed": rows_processed,
        "inserted_done": inserted,
        "updated_no_record": updated,
        "skipped": skipped,
        "pending_batch": pending_batch,
        "pending_page": pending_page,
        "pending_start": pending_start,
        "pending_index": pending_index,
        "fetch_complete": fetch_complete,
    }


def parse_dt(s: str):
    if not s:
        return None
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d
    except Exception:
        return None


def process_batch(batch, env, counters, progress_every, state_path: Path, progress_data: dict):
    total_known = counters["rows_fetched"]
    pending_index = int(progress_data.get("pending_index") or 0)
    total_batch = len(batch)
    for batch_idx in range(pending_index, total_batch):
        r = batch[batch_idx]
        call_id = str(r.get("CALL_ID") or "").strip()
        if not call_id:
            counters["skipped"] += 1
            counters["rows_processed"] += 1
            progress_data["pending_index"] = batch_idx + 1
            progress_data["rows_processed"] = counters["rows_processed"]
            progress_data["skipped"] = counters["skipped"]
            save_progress(state_path, progress_data)
            continue

        bitrix_id = str(r.get("ID") or "0")
        started = parse_dt(r.get("CALL_START_DATE") or "")
        duration = int(r.get("CALL_DURATION") or 0)
        manager_id = str(r.get("PORTAL_USER_ID") or "0")
        phone = str(r.get("PHONE_NUMBER") or "")
        portal_number = str(r.get("PORTAL_NUMBER") or "")
        crm_type = str(r.get("CRM_ENTITY_TYPE") or "")
        crm_id = str(r.get("CRM_ENTITY_ID") or "0")
        record_url = str(r.get("CALL_RECORD_URL") or "")
        external_call_id = str(r.get("EXTERNAL_CALL_ID") or "")
        call_failed_code = str(r.get("CALL_FAILED_CODE") or "")
        provider_name = str(r.get("REST_APP_NAME") or "")
        call_type = str(r.get("CALL_TYPE") or "").strip()
        direction = (
            "incoming" if call_type == "1" else ("outgoing" if call_type == "2" else "unknown")
        )
        status = "done" if record_url else "no_record"

        sql = f"""
        INSERT INTO calls(
            call_id, bitrix_id, started_at, duration_sec, manager_id, phone,
            crm_entity_type, crm_entity_id, call_record_url, status,
            source, line_id, store_id, direction, external_call_id, portal_number,
            call_failed_code, provider_name, created_at, updated_at
        ) VALUES (
            {sql_literal(call_id)},
            {bitrix_id if bitrix_id.isdigit() else 'NULL'},
            {sql_literal(started.isoformat()) if started else 'now()'},
            {duration},
            {manager_id if manager_id.isdigit() else 'NULL'},
            {sql_literal(phone)},
            {sql_literal(crm_type)},
            {crm_id if crm_id.isdigit() else 'NULL'},
            {sql_literal(record_url)},
            {sql_literal(status)},
            'bitrix',
            NULL,
            'bitrix',
            {sql_literal(direction)},
            {sql_literal(external_call_id)},
            {sql_literal(portal_number)},
            {sql_literal(call_failed_code)},
            {sql_literal(provider_name)},
            now(), now()
        )
        ON CONFLICT (call_id) DO UPDATE
        SET bitrix_id = EXCLUDED.bitrix_id,
            started_at = EXCLUDED.started_at,
            duration_sec = EXCLUDED.duration_sec,
            manager_id = EXCLUDED.manager_id,
            phone = EXCLUDED.phone,
            crm_entity_type = EXCLUDED.crm_entity_type,
            crm_entity_id = EXCLUDED.crm_entity_id,
            call_record_url = EXCLUDED.call_record_url,
            status = EXCLUDED.status,
            source = EXCLUDED.source,
            line_id = EXCLUDED.line_id,
            store_id = EXCLUDED.store_id,
            direction = EXCLUDED.direction,
            external_call_id = EXCLUDED.external_call_id,
            portal_number = EXCLUDED.portal_number,
            call_failed_code = EXCLUDED.call_failed_code,
            provider_name = EXCLUDED.provider_name,
            updated_at = now();
        """
        run_psql(sql, env, capture=False)

        if status == "done":
            counters["inserted"] += 1
        else:
            counters["updated"] += 1
        counters["rows_processed"] += 1

        progress_data["pending_index"] = batch_idx + 1
        progress_data["rows_processed"] = counters["rows_processed"]
        progress_data["inserted_done"] = counters["inserted"]
        progress_data["updated_no_record"] = counters["updated"]
        progress_data["skipped"] = counters["skipped"]
        save_progress(state_path, progress_data)

        if (
            counters["rows_processed"] == 1
            or counters["rows_processed"] % progress_every == 0
            or batch_idx + 1 == total_batch
        ):
            log(
                f"progress processed={counters['rows_processed']}/{total_known} "
                f"inserted_done={counters['inserted']} updated_no_record={counters['updated']} skipped={counters['skipped']}"
            )


def main():
    args = parse_args()
    env = load_env(ENV_FILE)
    if args.plan_only:
        plan = build_plan(env)
        if args.json:
            print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(
                "status={status} side_effects={side_effects} from={from_} to={to}".format(
                    status=plan["status"],
                    side_effects=plan["side_effects"],
                    from_=plan["window"]["from"],
                    to=plan["window"]["to"],
                )
            )
        return

    try:
        base = resolve_bitrix_ingest_base(env)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if not env.get("DATABASE_URL"):
        raise SystemExit("Missing required env: DATABASE_URL")

    ensure_calls_schema(env)
    ensure_resolution_schema(env)

    # Берём последние 2 дня по МСК и обязательно выбираем все страницы (pagination)
    def on_signal(signum, _frame):
        name = signal.Signals(signum).name
        log(
            "received "
            f"{name} stage={STATE['stage']} page={STATE['page']} start={STATE['start']} "
            f"rows_fetched={STATE['rows_fetched']} rows_processed={STATE['rows_processed']}"
        )
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    settings = resolve_run_settings(env)
    lookback_days = settings["lookback_days"]
    max_pages = settings["max_pages"]
    b24_timeout = settings["b24_timeout"]
    b24_retries = settings["b24_retries"]
    progress_every = settings["progress_every"]
    state_path = settings["state_path"]
    frm = settings["from"]
    to = settings["to"]
    log(
        "start "
        f"lookback_days={lookback_days} from={frm} to={to} "
        f"timeout={b24_timeout}s retries={b24_retries} max_pages={max_pages}"
    )

    progress_data = load_progress(state_path)
    if progress_data and (progress_data.get("from") != frm or progress_data.get("to") != to):
        log(
            "stale progress state detected, resetting "
            f"(state_window={progress_data.get('from')}..{progress_data.get('to')})"
        )
        clear_progress(state_path)
        progress_data = None

    if progress_data:
        log(
            "resume from progress "
            f"next_page={progress_data.get('next_page')} next_start={progress_data.get('next_start')} "
            f"rows_fetched={progress_data.get('rows_fetched')} rows_processed={progress_data.get('rows_processed')}"
        )
    else:
        progress_data = make_progress_payload(
            frm=frm,
            to=to,
            lookback_days=lookback_days,
            max_pages=max_pages,
            b24_timeout=b24_timeout,
            b24_retries=b24_retries,
            progress_every=progress_every,
            next_page=1,
            next_start=0,
            rows_fetched=0,
            rows_processed=0,
            inserted=0,
            updated=0,
            skipped=0,
            pending_batch=[],
            pending_page=0,
            pending_start=0,
            pending_index=0,
            fetch_complete=False,
        )
        save_progress(state_path, progress_data)

    counters = {
        "rows_fetched": int(progress_data.get("rows_fetched") or 0),
        "rows_processed": int(progress_data.get("rows_processed") or 0),
        "inserted": int(progress_data.get("inserted_done") or 0),
        "updated": int(progress_data.get("updated_no_record") or 0),
        "skipped": int(progress_data.get("skipped") or 0),
    }
    STATE["rows_fetched"] = counters["rows_fetched"]
    STATE["rows_processed"] = counters["rows_processed"]

    pending_batch = progress_data.get("pending_batch") or []
    if pending_batch:
        STATE["stage"] = "resume_pending_batch"
        STATE["page"] = int(
            progress_data.get("pending_page") or progress_data.get("next_page") or 1
        )
        STATE["start"] = int(
            progress_data.get("pending_start") or progress_data.get("next_start") or 0
        )
        log(
            "resume pending batch "
            f"page={STATE['page']} start={STATE['start']} remaining={len(pending_batch) - int(progress_data.get('pending_index') or 0)}"
        )
        process_batch(pending_batch, env, counters, progress_every, state_path, progress_data)
        progress_data["pending_batch"] = []
        progress_data["pending_page"] = 0
        progress_data["pending_start"] = 0
        progress_data["pending_index"] = 0
        save_progress(state_path, progress_data)

    start = int(progress_data.get("next_start") or 0)
    next_page = int(progress_data.get("next_page") or 1)
    fetch_complete = bool(progress_data.get("fetch_complete"))
    STATE["stage"] = "fetch_pages"
    for page in ([] if fetch_complete else range(next_page, max_pages + 1)):
        STATE["page"] = page
        STATE["start"] = start
        log(f"fetch page={page} start={start}")
        payload = b24_call(
            base,
            "voximplant.statistic.get",
            params=[
                ("FILTER[>=CALL_START_DATE]", frm),
                ("FILTER[<=CALL_START_DATE]", to),
                ("start", str(start)),
            ],
            timeout=b24_timeout,
            retries=b24_retries,
        )
        batch = payload.get("result") or []
        nxt = payload.get("next")
        counters["rows_fetched"] += len(batch)
        STATE["rows_fetched"] = counters["rows_fetched"]
        progress_data["rows_fetched"] = counters["rows_fetched"]
        progress_data["pending_batch"] = batch
        progress_data["pending_page"] = page
        progress_data["pending_start"] = start
        progress_data["pending_index"] = 0
        progress_data["fetch_complete"] = not isinstance(nxt, int)
        # Persist the "next page" cursor before we start upserting this batch.
        # If the process dies after the batch is fully written but before the
        # cursor is advanced on disk, resume would re-fetch the same page and
        # inflate fetched-row counters.
        progress_data["next_page"] = page + 1
        progress_data["next_start"] = nxt if isinstance(nxt, int) else start
        log(
            f"page={page} batch_rows={len(batch)} total_rows={counters['rows_fetched']} "
            f"next={nxt if nxt is not None else 'none'}"
        )
        save_progress(state_path, progress_data)

        STATE["stage"] = "upsert_rows"
        process_batch(batch, env, counters, progress_every, state_path, progress_data)
        progress_data["pending_batch"] = []
        progress_data["pending_page"] = 0
        progress_data["pending_start"] = 0
        progress_data["pending_index"] = 0
        save_progress(state_path, progress_data)
        if isinstance(nxt, int):
            start = nxt
            STATE["stage"] = "fetch_pages"
        else:
            break

    STATE["stage"] = "write_event"
    evt = f"""
    INSERT INTO events_log(event_type, call_id, level, message, payload)
    VALUES ('ingest.calls.done', 'batch', 'info', 'Bitrix ingest done',
      jsonb_build_object('rows', {counters['rows_fetched']}, 'inserted_done', {counters['inserted']}, 'updated_no_record', {counters['updated']}, 'skipped', {counters['skipped']}, 'from', {sql_literal(frm)}, 'to', {sql_literal(to)}));
    """
    run_psql(evt, env, capture=False)

    resolution_summary = resolve_call_identities(
        env,
        start_at=frm,
        end_at=to,
    )
    log(
        "resolution "
        f"matched={resolution_summary['matched_rows']} "
        f"resolved={resolution_summary['resolved_rows']} "
        f"unresolved={resolution_summary['unresolved_rows']} "
        f"conflicts={resolution_summary['conflict_rows']}"
    )

    STATE["stage"] = "done"
    clear_progress(state_path)
    print(
        f"rows={counters['rows_fetched']} inserted_done={counters['inserted']} "
        f"updated_no_record={counters['updated']} skipped={counters['skipped']}"
    )


if __name__ == "__main__":
    main()
