#!/usr/bin/env python3
import argparse
import base64
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import urllib.parse
from collections import Counter

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "scripts"))

try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except AttributeError:
    pass

try:
    from .asr_runtime import ensure_runtime
    from .calls_unified_projection import build_asr_candidates_sql, build_asr_window_stats_sql
except ImportError:  # pragma: no cover - script execution path
    from asr_runtime import ensure_runtime
    from calls_unified_projection import build_asr_candidates_sql, build_asr_window_stats_sql

ENV_FILE = os.getenv("OPENCLAW_ENV_FILE", "/home/deploy/.openclaw/.env")
LOCAL_TRANSCRIBE_PY = os.path.join(os.path.dirname(__file__), "faster_whisper_transcribe_file.py")
API_TRANSCRIBE_SH = "/home/deploy/.openclaw/workspace/scripts/transcribe.sh"
RELAY_URL = "http://10.66.67.3:8500/fetch"
BATCH_SIZE = 10
LOOKBACK_DAYS = int(os.environ.get("ASR_LOOKBACK_DAYS", "1"))
MAX_CALLS = int(os.environ.get("ASR_MAX_CALLS", "80"))
ASR_MODE = (os.environ.get("ASR_MODE", "local") or "local").strip().lower()
MIN_DURATION_SECONDS = int(os.environ.get("ASR_MIN_DURATION_SECONDS", "2"))
PER_FILE_TIMEOUT_SECONDS = int(os.environ.get("ASR_PER_FILE_TIMEOUT_SECONDS", "240"))
MSK = dt.timezone(dt.timedelta(hours=3))


def load_env(path):
    env = os.environ.copy()
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip().strip('"').strip("'")
            env[k.strip()] = v
    return env


def sql_literal(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def b64_sql_text(s: str) -> str:
    import base64

    b64 = base64.b64encode(s.encode("utf-8")).decode("ascii")
    return f"convert_from(decode({sql_literal(b64)}, 'base64'), 'UTF8')"


def run_psql(sql: str, env, capture=True):
    cmd = ["psql", env["DATABASE_URL"], "-v", "ON_ERROR_STOP=1", "-At", "-F", "\t", "-c", sql]
    res = subprocess.run(cmd, env=env, text=True, capture_output=capture)
    if res.returncode != 0:
        raise RuntimeError((res.stderr or res.stdout or "psql failed").strip())
    return res.stdout if capture else ""


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
    ALTER TABLE calls ADD COLUMN IF NOT EXISTS resolved_manager_id bigint;
    ALTER TABLE calls ADD COLUMN IF NOT EXISTS resolved_manager_name text;
    ALTER TABLE calls ADD COLUMN IF NOT EXISTS resolved_store_id text;
    ALTER TABLE calls ADD COLUMN IF NOT EXISTS resolved_store_name text;
    ALTER TABLE calls ADD COLUMN IF NOT EXISTS resolved_line_id text;
    ALTER TABLE calls ADD COLUMN IF NOT EXISTS resolution_source text NOT NULL DEFAULT 'unresolved';
    ALTER TABLE calls ADD COLUMN IF NOT EXISTS manager_resolution_conflict boolean NOT NULL DEFAULT false;
    """
    run_psql(sql, env, capture=False)


def resolve_time_window(env):
    target_date = (env.get("ASR_TARGET_DATE") or "").strip()
    start_at = (env.get("ASR_DATE_FROM") or "").strip()
    end_at = (env.get("ASR_DATE_TO") or "").strip()

    if target_date:
        day = dt.datetime.strptime(target_date, "%Y-%m-%d").date()
        start_dt = dt.datetime.combine(day, dt.time.min, tzinfo=MSK)
        end_dt = start_dt + dt.timedelta(days=1)
        return start_dt.isoformat(), end_dt.isoformat()

    if start_at and end_at:
        return start_at, end_at

    now_msk = dt.datetime.now(MSK)
    start_dt = (now_msk - dt.timedelta(days=LOOKBACK_DAYS)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    end_dt = (now_msk + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return start_dt.isoformat(), end_dt.isoformat()


def log_event(env, event_type: str, call_id: str, level: str, message: str, payload_expr: str):
    sql = f"""
    INSERT INTO events_log(event_type, call_id, level, message, payload)
    VALUES (
      {sql_literal(event_type)},
      {sql_literal(call_id)},
      {sql_literal(level)},
      {sql_literal(message)},
      {payload_expr}
    );
    """
    run_psql(sql, env, capture=False)


def build_candidates_sql(start_at: str, end_at: str) -> str:
    return build_asr_candidates_sql(start_at, end_at, MAX_CALLS, MIN_DURATION_SECONDS)


def get_window_stats(env, start_at: str, end_at: str):
    sql = build_asr_window_stats_sql(start_at, end_at)
    out = run_psql(sql, env).strip()
    parts = out.split("\t") if out else []
    values = [int(part or "0") for part in parts[:4]]
    while len(values) < 4:
        values.append(0)
    return {
        "total_calls": values[0],
        "with_record": values[1],
        "no_record": values[2],
        "with_transcript": values[3],
    }


def get_candidates(env, start_at: str, end_at: str):
    sql = build_candidates_sql(start_at, end_at)
    out = run_psql(sql, env)
    rows = []
    for line in out.splitlines():
        parts = line.split("\t", 4)
        if len(parts) == 5 and parts[0] and parts[1]:
            rows.append((parts[0], parts[1], parts[2], parts[3], parts[4]))
    return rows


def make_summary(text: str, max_len: int = 240) -> str:
    s = " ".join(text.split())
    if len(s) <= max_len:
        return s
    return s[: max_len - 1].rstrip() + "…"


def parse_key_list(value: str):
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


def resolve_relay_api_keys(env):
    # Приоритет: RELAY_API_KEY -> RELAY_API_KEY_FALLBACK -> OPENCLAW_GATEWAY_TOKEN.
    keys = []
    for name in ("RELAY_API_KEY", "RELAY_API_KEY_FALLBACK", "OPENCLAW_GATEWAY_TOKEN"):
        for key in parse_key_list(env.get(name, "")):
            if key and key not in keys:
                keys.append(key)
    return keys


def resolve_relay_url(env):
    explicit = (env.get("ASR_RELAY_URL") or env.get("RELAY_URL") or "").strip()
    if explicit:
        return explicit
    return RELAY_URL


def resolve_local_whisper_python(env):
    explicit = (env.get("OFFLINE_ASR_PYTHON") or "").strip()
    if explicit:
        return explicit
    venv = (env.get("OFFLINE_ASR_VENV") or "").strip()
    if not venv:
        venv = os.path.expanduser("~/.cache/offline-asr-review/venv")
    return os.path.join(venv, "bin", "python3")


def ensure_local_whisper_runtime(env):
    return ensure_runtime(env)


def windows_join(base: str, leaf: str) -> str:
    return base.rstrip("\\/") + "\\" + leaf.lstrip("\\/")


def windows_to_scp_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    if len(normalized) >= 2 and normalized[1] == ":":
        return "/" + normalized
    return normalized


def powershell_single_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def powershell_encoded_command(script: str) -> str:
    return base64.b64encode(script.encode("utf-16le")).decode("ascii")


def powershell_utf8_script(script: str) -> str:
    return (
        "$ProgressPreference = 'SilentlyContinue'\n"
        "$InformationPreference = 'SilentlyContinue'\n"
        "$OutputEncoding = [System.Text.UTF8Encoding]::new()\n"
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()\n" + script
    )


def resolve_ssh_worker_settings(env):
    default_model = (env.get("ASR_LOCAL_MODEL") or "tiny").strip() or "tiny"
    default_language = (env.get("ASR_LOCAL_LANGUAGE") or "ru").strip() or "ru"
    default_compute = (env.get("ASR_LOCAL_COMPUTE_TYPE") or "int8").strip() or "int8"
    default_device = (env.get("ASR_LOCAL_DEVICE") or "cpu").strip() or "cpu"
    return {
        "host": (env.get("ASR_SSH_HOST") or "asr-win").strip() or "asr-win",
        "audio_dir": (env.get("ASR_SSH_AUDIO_DIR") or r"C:\asr-worker\incoming").strip()
        or r"C:\asr-worker\incoming",
        "script_path": (
            env.get("ASR_SSH_SCRIPT") or r"C:\asr-worker\run_faster_whisper.ps1"
        ).strip()
        or r"C:\asr-worker\run_faster_whisper.ps1",
        "model": (env.get("ASR_SSH_MODEL") or default_model).strip() or default_model,
        "language": (env.get("ASR_SSH_LANGUAGE") or default_language).strip() or default_language,
        "compute_type": (env.get("ASR_SSH_COMPUTE_TYPE") or default_compute).strip()
        or default_compute,
        "device": (env.get("ASR_SSH_DEVICE") or default_device).strip() or default_device,
    }


def ensure_remote_audio_dir(env, settings):
    script = (
        "New-Item -ItemType Directory -Force -Path "
        f"{powershell_single_quote(settings['audio_dir'])} | Out-Null"
    )
    prepare = subprocess.run(
        [
            "ssh",
            settings["host"],
            "powershell",
            "-NoProfile",
            "-EncodedCommand",
            powershell_encoded_command(script),
        ],
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    if prepare.returncode != 0:
        raise RuntimeError(
            f"ssh_prepare_failed: {(prepare.stderr or prepare.stdout).strip()[:500]}"
        )


def transcribe_remote_audio(env, settings, remote_audio_path: str) -> tuple[str, str]:
    script = powershell_utf8_script(
        "& {script_path} -AudioPath {audio_path} -Model {model} "
        "-Language {language} -Device {device} -ComputeType {compute_type}".format(
            script_path=powershell_single_quote(settings["script_path"]),
            audio_path=powershell_single_quote(remote_audio_path),
            model=powershell_single_quote(settings["model"]),
            language=powershell_single_quote(settings["language"]),
            device=powershell_single_quote(settings["device"]),
            compute_type=powershell_single_quote(settings["compute_type"]),
        )
    )
    transcribe = subprocess.run(
        [
            "ssh",
            settings["host"],
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            powershell_encoded_command(script),
        ],
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=PER_FILE_TIMEOUT_SECONDS,
    )
    if transcribe.returncode != 0:
        raise RuntimeError(
            f"ssh_transcribe_failed: {(transcribe.stderr or transcribe.stdout).strip()[:500]}"
        )
    transcript_text = (transcribe.stdout or "").strip()
    if not transcript_text:
        raise RuntimeError("ssh_transcribe_failed: empty_transcript")
    return transcript_text, f"ssh-faster-whisper:{settings['model']}@{settings['host']}"


def remove_remote_audio(env, settings, remote_audio_path: str):
    subprocess.run(
        [
            "ssh",
            settings["host"],
            f"cmd /c if exist {remote_audio_path} del /q {remote_audio_path}",
        ],
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )


def run_ssh_whisper_from_relay(
    env,
    relay_api_keys,
    call_id: str,
    encoded_record_url: str,
) -> tuple[str, str]:
    settings = resolve_ssh_worker_settings(env)
    remote_audio_path = windows_join(settings["audio_dir"], f"{call_id}.mp3")
    relay_url = (env.get("ASR_SSH_RELAY_URL") or "http://127.0.0.1:8500/fetch").strip()
    ensure_remote_audio_dir(env, settings)

    last_err = ""
    unauthorized_keys = 0
    try:
        for relay_api_key in relay_api_keys:
            script = powershell_utf8_script(f"""
$ErrorActionPreference = 'Stop'
$headers = @{{'X-Relay-Key' = {powershell_single_quote(relay_api_key)}}}
Invoke-WebRequest -UseBasicParsing -Headers $headers -Uri {powershell_single_quote(relay_url + '?url=' + encoded_record_url)} -OutFile {powershell_single_quote(remote_audio_path)} -TimeoutSec 90
""")
            download = subprocess.run(
                [
                    "ssh",
                    settings["host"],
                    "powershell",
                    "-NoProfile",
                    "-EncodedCommand",
                    powershell_encoded_command(script),
                ],
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=120,
            )
            if download.returncode == 0:
                return transcribe_remote_audio(env, settings, remote_audio_path)
            reason = (download.stderr or download.stdout or "").strip()[:500]
            last_err = reason
            if "401" in reason or "Unauthorized" in reason:
                unauthorized_keys += 1
                continue
            raise RuntimeError(f"relay_download_failed: {reason}")
        if unauthorized_keys == len(relay_api_keys):
            raise RuntimeError(
                f"relay_download_failed: unauthorized (401) for all keys ({len(relay_api_keys)} tried) - "
                "check RELAY_API_KEY / RELAY_API_KEY_FALLBACK / OPENCLAW_GATEWAY_TOKEN"
            )
        raise RuntimeError(f"relay_download_failed: {last_err or 'unknown_error'}")
    finally:
        remove_remote_audio(env, settings, remote_audio_path)


def run_ssh_whisper(env, call_id: str, audio_path: str) -> tuple[str, str]:
    settings = resolve_ssh_worker_settings(env)
    remote_audio_path = windows_join(settings["audio_dir"], f"{call_id}.mp3")
    remote_audio_target = f"{settings['host']}:{windows_to_scp_path(remote_audio_path)}"
    ensure_remote_audio_dir(env, settings)

    upload = subprocess.run(
        ["scp", "-q", audio_path, remote_audio_target],
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    if upload.returncode != 0:
        raise RuntimeError(f"ssh_upload_failed: {(upload.stderr or upload.stdout).strip()[:500]}")

    try:
        return transcribe_remote_audio(env, settings, remote_audio_path)
    finally:
        remove_remote_audio(env, settings, remote_audio_path)


def normalize_error_reason(reason: str):
    text = (reason or "").strip()
    if text.startswith("api_transcribe_failed:"):
        return "api_transcribe_failed", text.split(":", 1)[1].strip()
    if text.startswith("api_transcribe_timeout:"):
        return "api_transcribe_timeout", text.split(":", 1)[1].strip()
    if text.startswith("local_whisper_python_not_found:"):
        return "python_missing", text.split(":", 1)[1].strip()
    if text.startswith("python_missing:"):
        return "python_missing", text.split(":", 1)[1].strip()
    if text.startswith("venv_bootstrap_failed:"):
        return "venv_bootstrap_failed", text.split(":", 1)[1].strip()
    if text.startswith("tool_missing:"):
        return "tool_missing", text.split(":", 1)[1].strip()
    if text.startswith("relay_download_failed:"):
        return "audio_unavailable", text.split(":", 1)[1].strip()
    if text.startswith("ssh_prepare_failed:"):
        return "worker_prepare_failed", text.split(":", 1)[1].strip()
    if text.startswith("ssh_upload_failed:"):
        return "worker_upload_failed", text.split(":", 1)[1].strip()
    if text.startswith("ssh_transcribe_failed:"):
        return "transcribe_failed", text.split(":", 1)[1].strip()
    if text.startswith("model_cache_corrupted:"):
        return "model_cache_corrupted", text.split(":", 1)[1].strip()
    if text.startswith("model_download_failed:"):
        return "model_download_failed", text.split(":", 1)[1].strip()
    if text.startswith("local_whisper_failed:"):
        return "transcribe_failed", text.split(":", 1)[1].strip()
    return "unknown_error", text


def process_one(
    env,
    relay_api_keys,
    call_id: str,
    record_url: str,
    source: str,
    store_id: str,
    line_id: str,
    tmpdir: str,
):
    encoded = urllib.parse.quote(record_url, safe="")
    transcript_text = ""
    model_label = ""
    if ASR_MODE == "ssh" and (
        env.get("ASR_SSH_RELAY_DOWNLOAD", "true").strip().lower() not in {"0", "false", "no"}
    ):
        transcript_text, model_label = run_ssh_whisper_from_relay(
            env, relay_api_keys, call_id, encoded
        )
    else:
        transcript_text, model_label = transcribe_from_local_relay_download(
            env, relay_api_keys, call_id, encoded, tmpdir
        )

    if not transcript_text:
        raise RuntimeError(f"{ASR_MODE}_transcribe_failed: empty_transcript")

    summary = make_summary(transcript_text, 240)
    source_lit = sql_literal(source or "bitrix")
    store_lit = sql_literal(store_id or "unknown")
    line_lit = sql_literal(line_id or "")

    sql = f"""
    INSERT INTO transcripts(call_id, transcript_text, language, model, created_at)
    VALUES (
      {sql_literal(call_id)},
      {b64_sql_text(transcript_text)},
      'ru',
      {sql_literal(model_label)},
      now()
    )
    ON CONFLICT (call_id) DO UPDATE
      SET transcript_text = EXCLUDED.transcript_text,
          language = 'ru',
          model = EXCLUDED.model,
          created_at = now();

    INSERT INTO call_analysis(call_id, outcome, sentiment, summary, analysis_json)
    VALUES (
      {sql_literal(call_id)},
      'pending_review',
      'unknown',
      {b64_sql_text(summary)},
      jsonb_build_object('status','asr_only','source',{source_lit},'store_id',{store_lit},'line_id',{line_lit})
    )
    ON CONFLICT (call_id) DO UPDATE
      SET outcome = 'pending_review',
          sentiment = 'unknown',
          summary = EXCLUDED.summary,
          analysis_json = COALESCE(call_analysis.analysis_json, '{{}}'::jsonb) ||
              jsonb_build_object('status','asr_only','source',{source_lit},'store_id',{store_lit},'line_id',{line_lit});
    """
    run_psql(sql, env, capture=False)

    payload = f"jsonb_build_object('status','ok','model',{sql_literal(model_label)},'source',{source_lit},'store_id',{store_lit},'line_id',{line_lit})"
    log_event(env, "asr.done", call_id, "info", "ASR completed", payload)


def transcribe_from_local_relay_download(
    env,
    relay_api_keys,
    call_id: str,
    encoded: str,
    tmpdir: str,
) -> tuple[str, str]:
    # Важно: Whisper определяет формат по расширению файла.
    # Ранее .audio давал ошибки "Invalid file format".
    audio_path = os.path.join(tmpdir, f"{call_id}.mp3")

    last_err = ""
    unauthorized_keys = 0
    for relay_api_key in relay_api_keys:
        curl_cmd = [
            "curl",
            "-fSLo",
            audio_path,
            "--connect-timeout",
            "8",
            "--max-time",
            "45",
            "--retry",
            "1",
            "--retry-delay",
            "1",
            "-H",
            f"X-Relay-Key: {relay_api_key}",
            f"{resolve_relay_url(env)}?url={encoded}",
        ]
        c = subprocess.run(curl_cmd, env=env, text=True, capture_output=True)
        if c.returncode == 0:
            break

        curl_err = (c.stderr or c.stdout or "").strip()[:500]
        last_err = curl_err
        if "401" in curl_err:
            unauthorized_keys += 1
            continue
        raise RuntimeError(f"relay_download_failed: {curl_err}")
    else:
        if unauthorized_keys == len(relay_api_keys):
            raise RuntimeError(
                f"relay_download_failed: unauthorized (401) for all keys ({len(relay_api_keys)} tried) - "
                "check RELAY_API_KEY / RELAY_API_KEY_FALLBACK / OPENCLAW_GATEWAY_TOKEN"
            )
        raise RuntimeError(f"relay_download_failed: {last_err or 'unknown_error'}")

    if ASR_MODE == "api":
        api_model = (
            env.get("ASR_API_MODEL") or env.get("OPENAI_TRANSCRIPTION_MODEL") or "whisper-1"
        ).strip() or "whisper-1"
        api_language = (env.get("ASR_API_LANGUAGE") or "ru").strip() or "ru"
        api_out = os.path.join(tmpdir, f"{call_id}.api.txt")
        if not os.path.exists(API_TRANSCRIBE_SH):
            raise RuntimeError(f"api_transcribe_failed: script_not_found {API_TRANSCRIBE_SH}")
        transcribe = subprocess.run(
            [
                API_TRANSCRIBE_SH,
                audio_path,
                "--model",
                api_model,
                "--out",
                api_out,
                "--language",
                api_language,
            ],
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=PER_FILE_TIMEOUT_SECONDS,
        )
        if transcribe.returncode != 0:
            raise RuntimeError(
                f"api_transcribe_failed: {(transcribe.stderr or transcribe.stdout).strip()[:500]}"
            )
        with open(api_out, encoding="utf-8") as f:
            transcript_text = f.read().strip()
        model_label = f"openai-api:{api_model}"
    elif ASR_MODE == "ssh":
        transcript_text, model_label = run_ssh_whisper(env, call_id, audio_path)
    else:
        local_model = (env.get("ASR_LOCAL_MODEL") or "large-v3").strip() or "large-v3"
        local_language = (env.get("ASR_LOCAL_LANGUAGE") or "ru").strip() or "ru"
        local_compute_type = (env.get("ASR_LOCAL_COMPUTE_TYPE") or "int8").strip() or "int8"
        local_device = (env.get("ASR_LOCAL_DEVICE") or "cpu").strip() or "cpu"
        local_python = ensure_local_whisper_runtime(env)
        if not os.path.exists(LOCAL_TRANSCRIBE_PY):
            raise RuntimeError(f"local_whisper_script_not_found: {LOCAL_TRANSCRIBE_PY}")

        transcribe = subprocess.run(
            [
                local_python,
                LOCAL_TRANSCRIBE_PY,
                audio_path,
                "--model",
                local_model,
                "--language",
                local_language,
                "--compute-type",
                local_compute_type,
                "--device",
                local_device,
            ],
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=PER_FILE_TIMEOUT_SECONDS,
        )
        if transcribe.returncode != 0:
            raise RuntimeError(
                f"local_whisper_failed: {(transcribe.stderr or transcribe.stdout).strip()[:500]}"
            )
        transcript_text = (transcribe.stdout or "").strip()
        model_label = f"faster-whisper:{local_model}"
    return transcript_text, model_label


def parse_args():
    parser = argparse.ArgumentParser(description="Transcribe call recordings into call_analytics.")
    parser.add_argument("--plan-only", action="store_true", help="Emit a read-only execution plan")
    parser.add_argument("--json", action="store_true", help="Emit JSON for --plan-only")
    return parser.parse_args()


def build_plan(env):
    errors = []
    start_at, end_at = resolve_time_window(env)
    relay_api_keys = resolve_relay_api_keys(env)
    if not env.get("DATABASE_URL"):
        errors.append("missing env: DATABASE_URL")
    if not relay_api_keys:
        errors.append(
            "missing env: RELAY_API_KEY or RELAY_API_KEY_FALLBACK or OPENCLAW_GATEWAY_TOKEN"
        )

    candidates = []
    window_stats = {"total_calls": 0, "with_record": 0, "no_record": 0, "with_transcript": 0}
    if env.get("DATABASE_URL"):
        try:
            candidates = get_candidates(env, start_at, end_at)
            window_stats = get_window_stats(env, start_at, end_at)
        except Exception as exc:
            errors.append(f"candidate query failed: {str(exc)[:300]}")

    worker = resolve_ssh_worker_settings(env) if ASR_MODE == "ssh" else {}
    return {
        "status": "blocked" if errors else "ready",
        "side_effects": False,
        "window": {"from": start_at, "to": end_at},
        "planned_reads": [
            "call_analytics.calls",
            "call_analytics.transcripts",
            "call_analytics.call_analysis",
            "call_record_url relay config",
            f"ASR_MODE={ASR_MODE}",
            f"ASR_MIN_DURATION_SECONDS={MIN_DURATION_SECONDS}",
        ],
        "planned_writes": [
            "transcripts upsert",
            "call_analysis upsert",
            "events_log insert",
        ],
        "would_touch_tables": ["transcripts", "call_analysis", "events_log"],
        "counts": {
            "candidate_calls": len(candidates),
            "max_calls": MAX_CALLS,
            "min_duration_seconds": MIN_DURATION_SECONDS,
            "relay_key_candidates": len(relay_api_keys),
            "total_calls": window_stats["total_calls"],
            "with_record": window_stats["with_record"],
            "no_record": window_stats["no_record"],
            "with_transcript": window_stats["with_transcript"],
            "ssh_worker_configured": bool(worker),
        },
        "errors": errors,
    }


def main():
    args = parse_args()
    env = load_env(ENV_FILE)
    if args.plan_only:
        plan = build_plan(env)
        if args.json:
            print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(
                "status={status} side_effects={side_effects} candidates={candidates} from={from_} to={to}".format(
                    status=plan["status"],
                    side_effects=plan["side_effects"],
                    candidates=plan["counts"]["candidate_calls"],
                    from_=plan["window"]["from"],
                    to=plan["window"]["to"],
                )
            )
        return

    required = ["DATABASE_URL"]
    miss = [k for k in required if not env.get(k)]
    relay_api_keys = resolve_relay_api_keys(env)
    if not relay_api_keys:
        miss.append("RELAY_API_KEY or RELAY_API_KEY_FALLBACK or OPENCLAW_GATEWAY_TOKEN")
    if miss:
        raise SystemExit(f"Missing required env keys: {', '.join(miss)}")

    ensure_calls_schema(env)
    start_at, end_at = resolve_time_window(env)
    candidates = get_candidates(env, start_at, end_at)
    window_stats = get_window_stats(env, start_at, end_at)
    total = len(candidates)
    success = 0
    errors = 0
    reasons = Counter()
    reason_categories = Counter()

    print(
        f"Found {total} calls for ASR "
        f"(window={start_at}..{end_at}, max={MAX_CALLS}, total_calls={window_stats['total_calls']}, "
        f"with_record={window_stats['with_record']}, no_record={window_stats['no_record']}, "
        f"existing_transcripts={window_stats['with_transcript']}, mode={ASR_MODE})"
    )
    print(f"Relay key candidates: {len(relay_api_keys)}")

    with tempfile.TemporaryDirectory(prefix="weekly_asr_") as tmpdir:
        for i in range(0, total, BATCH_SIZE):
            batch = candidates[i : i + BATCH_SIZE]
            print(f"Batch {i//BATCH_SIZE + 1}: {len(batch)} calls")
            for call_id, url, source, store_id, line_id in batch:
                try:
                    process_one(
                        env, relay_api_keys, call_id, url, source, store_id, line_id, tmpdir
                    )
                    success += 1
                    print(f"  OK   {call_id}")
                except subprocess.TimeoutExpired:
                    errors += 1
                    reason = f"{ASR_MODE}_transcribe_timeout: exceeded {PER_FILE_TIMEOUT_SECONDS}s"
                    reasons[reason] += 1
                    category, details = (
                        "transcribe_timeout",
                        f"{ASR_MODE} exceeded {PER_FILE_TIMEOUT_SECONDS}s",
                    )
                    reason_categories[category] += 1
                    payload = (
                        f"jsonb_build_object('status','error','reason',{b64_sql_text(reason)},"
                        f"'reason_category',{sql_literal(category)},"
                        f"'reason_details',{b64_sql_text(details)},"
                        f"'source',{sql_literal(source or 'bitrix')},'store_id',{sql_literal(store_id or 'unknown')},"
                        f"'line_id',{sql_literal(line_id or '')})"
                    )
                    err_event_type = (
                        "asr.retail.error" if (source or "") == "retail_megafon" else "asr.error"
                    )
                    try:
                        log_event(env, err_event_type, call_id, "error", reason[:200], payload)
                    except Exception:
                        pass
                    print(f"  ERR  {call_id} :: {reason}")
                except Exception as e:
                    errors += 1
                    reason = str(e)
                    reasons[reason] += 1
                    category, details = normalize_error_reason(reason)
                    reason_categories[category] += 1
                    short = reason[:700]
                    short_details = details[:700]
                    payload = (
                        f"jsonb_build_object('status','error','reason',{b64_sql_text(short)},"
                        f"'reason_category',{sql_literal(category)},"
                        f"'reason_details',{b64_sql_text(short_details)},"
                        f"'source',{sql_literal(source or 'bitrix')},'store_id',{sql_literal(store_id or 'unknown')},"
                        f"'line_id',{sql_literal(line_id or '')})"
                    )
                    err_event_type = (
                        "asr.retail.error" if (source or "") == "retail_megafon" else "asr.error"
                    )
                    try:
                        log_event(env, err_event_type, call_id, "error", short[:200], payload)
                    except Exception:
                        pass
                    print(f"  ERR  {call_id} :: {short}")

    print("=== SUMMARY ===")
    print(f"total={total}")
    print(f"success={success}")
    print(f"errors={errors}")
    print(f"skipped_no_record={window_stats['no_record']}")
    print("top_errors:")
    for reason, cnt in reasons.most_common(5):
        print(f"{cnt}\t{reason}")
    print("top_error_kinds:")
    for category, cnt in reason_categories.most_common(5):
        print(f"{cnt}\t{category}")

    summary_payload = (
        "jsonb_build_object("
        f"'window_start',{sql_literal(start_at)},"
        f"'window_end',{sql_literal(end_at)},"
        f"'total',{total},"
        f"'success',{success},"
        f"'errors',{errors},"
        f"'skipped_no_record',{window_stats['no_record']},"
        f"'total_calls',{window_stats['total_calls']},"
        f"'with_record',{window_stats['with_record']},"
        f"'existing_transcripts',{window_stats['with_transcript']},"
        f"'top_error_kinds',{b64_sql_text(', '.join(f'{k}:{v}' for k, v in reason_categories.most_common(5)))}"
        ")"
    )
    log_event(env, "asr.batch.done", "batch", "info", "ASR batch completed", summary_payload)


if __name__ == "__main__":
    main()
