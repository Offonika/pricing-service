#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except AttributeError:
    pass

try:
    from .weekly_asr_transcription import (
        ensure_remote_audio_dir,
        make_summary,
        normalize_error_reason,
        remove_remote_audio,
        resolve_ssh_worker_settings,
        transcribe_remote_audio,
        windows_join,
        windows_to_scp_path,
    )
except ImportError:  # pragma: no cover - script execution path
    from weekly_asr_transcription import (
        ensure_remote_audio_dir,
        make_summary,
        normalize_error_reason,
        remove_remote_audio,
        resolve_ssh_worker_settings,
        transcribe_remote_audio,
        windows_join,
        windows_to_scp_path,
    )

ENV_FILE = os.getenv("OPENCLAW_ENV_FILE", "/home/deploy/.openclaw/.env")
MSK = dt.timezone(dt.timedelta(hours=3))
AUDIO_EXTENSIONS = {".wav", ".mp3"}
TEMP_SUFFIXES = (".part", ".tmp", ".uploading", ".crdownload")
DEFAULT_PIPELINE_VERSION = "0.1.0-pilot.1"
DEFAULT_HARDWARE_PROFILE_VERSION = "sprecord-mic4-m1105hd-v1"
DEFAULT_ASR_PROFILE_VERSION = "offline-asr-ssh-v1"
DEFAULT_STORAGE_LAYOUT_VERSION = "raw-v1"


@dataclass(frozen=True)
class AudioProbe:
    duration_sec: float | None
    sample_rate_hz: int | None
    codec: str | None
    channel_count: int | None
    format_name: str | None
    file_size_bytes: int


@dataclass(frozen=True)
class CandidateFile:
    path: Path
    reason: str = "candidate"


@dataclass(frozen=True)
class IngestSettings:
    landing_dir: Path
    raw_dir: Path
    pipeline_version: str
    hardware_profile_version: str
    asr_profile_version: str
    storage_layout_version: str
    min_duration_seconds: int
    device_config: dict[str, Any]


def load_env(path: str) -> dict[str, str]:
    env = os.environ.copy()
    if not path or not Path(path).exists():
        return env
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def sql_literal(value: str) -> str:
    return "'" + (value or "").replace("'", "''") + "'"


def nullable_sql(value: str | None) -> str:
    if value is None or value == "":
        return "NULL"
    return sql_literal(value)


def number_sql(value: int | float | None) -> str:
    if value is None:
        return "NULL"
    return str(value)


def timestamptz_sql(value: dt.datetime | None) -> str:
    if value is None:
        return "NULL"
    return f"{sql_literal(value.isoformat())}::timestamptz"


def b64_text_sql(value: str) -> str:
    encoded = base64.b64encode((value or "").encode("utf-8")).decode("ascii")
    return f"convert_from(decode({sql_literal(encoded)}, 'base64'), 'UTF8')"


def json_sql(value: Any) -> str:
    return f"{b64_text_sql(json.dumps(value, ensure_ascii=False, sort_keys=True))}::json"


def run_psql(sql: str, env: dict[str, str], capture: bool = True) -> str:
    cmd = ["psql", env["DATABASE_URL"], "-v", "ON_ERROR_STOP=1", "-At", "-F", "\t", "-c", sql]
    result = subprocess.run(cmd, env=env, text=True, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "psql failed").strip())
    return result.stdout if capture else ""


def parse_device_config(env: dict[str, str]) -> dict[str, Any]:
    raw = (env.get("OFFLINE_AUDIO_DEVICE_CONFIG_JSON") or "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid OFFLINE_AUDIO_DEVICE_CONFIG_JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError("invalid OFFLINE_AUDIO_DEVICE_CONFIG_JSON: root must be object")
    return value


def resolve_settings(env: dict[str, str]) -> IngestSettings:
    return IngestSettings(
        landing_dir=Path(env.get("OFFLINE_AUDIO_LANDING_DIR", "/var/lib/mm-offline-audio/landing")),
        raw_dir=Path(env.get("OFFLINE_AUDIO_RAW_DIR", "/var/lib/mm-offline-audio/raw")),
        pipeline_version=env.get("OFFLINE_AUDIO_PIPELINE_VERSION", DEFAULT_PIPELINE_VERSION),
        hardware_profile_version=env.get(
            "OFFLINE_AUDIO_HARDWARE_PROFILE_VERSION",
            DEFAULT_HARDWARE_PROFILE_VERSION,
        ),
        asr_profile_version=env.get(
            "OFFLINE_AUDIO_ASR_PROFILE_VERSION", DEFAULT_ASR_PROFILE_VERSION
        ),
        storage_layout_version=env.get(
            "OFFLINE_AUDIO_STORAGE_LAYOUT_VERSION",
            DEFAULT_STORAGE_LAYOUT_VERSION,
        ),
        min_duration_seconds=int(env.get("OFFLINE_AUDIO_MIN_DURATION_SECONDS", "2")),
        device_config=parse_device_config(env),
    )


def is_temporary_file(path: Path) -> bool:
    lowered = path.name.lower()
    return any(lowered.endswith(suffix) for suffix in TEMP_SUFFIXES)


def scan_landing_files(landing_dir: Path) -> tuple[list[CandidateFile], Counter[str]]:
    counts: Counter[str] = Counter()
    candidates: list[CandidateFile] = []
    if not landing_dir.exists():
        counts["missing_landing_dir"] += 1
        return candidates, counts

    for path in sorted(landing_dir.rglob("*")):
        if not path.is_file():
            continue
        if is_temporary_file(path):
            counts["ignored_temp"] += 1
            continue
        if path.suffix.lower() not in AUDIO_EXTENSIONS:
            counts["ignored_non_audio"] += 1
            continue
        if path.stat().st_size == 0:
            counts["ignored_zero"] += 1
            continue
        candidates.append(CandidateFile(path=path))
        counts["candidate"] += 1
    return candidates, counts


def compute_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def probe_audio(path: Path) -> AudioProbe:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration,size,format_name:stream=codec_type,codec_name,sample_rate,channels",
        "-of",
        "json",
        str(path),
    ]
    result = subprocess.run(cmd, text=True, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "ffprobe failed").strip())
    payload = json.loads(result.stdout or "{}")
    audio_stream = next(
        (stream for stream in payload.get("streams", []) if stream.get("codec_type") == "audio"),
        None,
    )
    if not audio_stream:
        raise RuntimeError("ffprobe_no_audio_stream")
    fmt = payload.get("format") or {}
    duration_raw = fmt.get("duration")
    sample_rate_raw = audio_stream.get("sample_rate")
    return AudioProbe(
        duration_sec=float(duration_raw) if duration_raw not in (None, "N/A", "") else None,
        sample_rate_hz=int(sample_rate_raw) if sample_rate_raw not in (None, "N/A", "") else None,
        codec=audio_stream.get("codec_name"),
        channel_count=int(audio_stream.get("channels") or 0) or None,
        format_name=fmt.get("format_name"),
        file_size_bytes=int(fmt.get("size") or path.stat().st_size),
    )


def read_sidecar_manifest(path: Path) -> dict[str, Any]:
    candidates = [path.with_suffix(".json"), Path(str(path) + ".json")]
    for manifest_path in candidates:
        if manifest_path.exists():
            value = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise RuntimeError(f"manifest root must be object: {manifest_path}")
            return value
    return {}


def safe_slug(value: str, *, default: str = "unknown") -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "-", value or "").strip("-._")
    return text[:180] or default


def infer_device_id(path: Path, landing_dir: Path) -> str:
    try:
        parts = path.relative_to(landing_dir).parts
    except ValueError:
        parts = path.parts
    lowered = [part.lower() for part in parts]
    if "sprecord" in lowered:
        idx = lowered.index("sprecord")
        if idx + 1 < len(parts):
            return safe_slug(parts[idx + 1], default="unknown-device")
    if len(parts) >= 2:
        return safe_slug(parts[-3] if len(parts) >= 3 else parts[-2], default="unknown-device")
    return "unknown-device"


def parse_datetime_value(value: Any) -> dt.datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=MSK)
        return parsed
    except ValueError:
        return None


def infer_started_at(path: Path) -> dt.datetime:
    name = path.stem
    match = re.search(
        r"(20\d{2})[-_]?(\d{2})[-_]?(\d{2})[T _-]?(\d{2})[-_]?(\d{2})[-_]?(\d{2})",
        name,
    )
    if match:
        year, month, day, hour, minute, second = [int(part) for part in match.groups()]
        return dt.datetime(year, month, day, hour, minute, second, tzinfo=MSK)
    return dt.datetime.fromtimestamp(path.stat().st_mtime, tz=MSK)


def device_profile(settings: IngestSettings, device_id: str) -> dict[str, Any]:
    value = settings.device_config.get(device_id) or settings.device_config.get("default") or {}
    return value if isinstance(value, dict) else {}


def normalize_manifest(
    path: Path,
    settings: IngestSettings,
    sha256: str,
    probe: AudioProbe | None,
    manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source_manifest = read_sidecar_manifest(path) if manifest is None else manifest
    device_id = str(source_manifest.get("device_id") or infer_device_id(path, settings.landing_dir))
    profile = device_profile(settings, device_id)
    started_at = (
        parse_datetime_value(source_manifest.get("started_at"))
        or parse_datetime_value(source_manifest.get("start_time"))
        or infer_started_at(path)
    )
    duration_sec = source_manifest.get("duration_sec")
    if duration_sec is None and probe is not None:
        duration_sec = probe.duration_sec
    ended_at = parse_datetime_value(source_manifest.get("ended_at"))
    if ended_at is None and started_at and duration_sec:
        ended_at = started_at + dt.timedelta(seconds=float(duration_sec))

    started_compact = started_at.strftime("%Y%m%dT%H%M%S") if started_at else "unknown-time"
    dialog_id = source_manifest.get("dialog_id") or source_manifest.get("id")
    if not dialog_id:
        dialog_id = f"offline-{safe_slug(device_id)}-{started_compact}-{safe_slug(path.stem)}"

    channels = source_manifest.get("channels") or [{"index": 0, "role": "mixed"}]
    return {
        "manifest_schema_version": int(
            source_manifest.get("manifest_schema_version") or source_manifest.get("version") or 1
        ),
        "dialog_id": safe_slug(str(dialog_id), default=f"offline-{sha256[:16]}"),
        "source": source_manifest.get("source") or "offline_store",
        "store_id": source_manifest.get("store_id") or profile.get("store_id") or "unknown",
        "store_name": source_manifest.get("store_name") or profile.get("store_name"),
        "pc_id": source_manifest.get("pc_id") or profile.get("pc_id") or device_id,
        "device_id": device_id,
        "recorder_model": source_manifest.get("recorder_model")
        or profile.get("recorder_model")
        or "SpRecord MIC4",
        "recorder_serial": source_manifest.get("recorder_serial") or profile.get("recorder_serial"),
        "record_id": source_manifest.get("record_id") or source_manifest.get("recordId"),
        "microphone_model": source_manifest.get("microphone_model")
        or profile.get("microphone_model")
        or "STELBERRY M-1105HD",
        "upload_protocol": source_manifest.get("upload_protocol")
        or profile.get("upload_protocol")
        or "ftps",
        "ingest_pipeline_version": settings.pipeline_version,
        "hardware_profile_version": settings.hardware_profile_version,
        "asr_profile_version": settings.asr_profile_version,
        "storage_layout_version": settings.storage_layout_version,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_sec": float(duration_sec) if duration_sec not in (None, "") else None,
        "audio_format": source_manifest.get("audio_format"),
        "sample_rate_hz": probe.sample_rate_hz if probe else source_manifest.get("sample_rate_hz"),
        "codec": probe.codec if probe else source_manifest.get("codec"),
        "channel_count": (
            probe.channel_count if probe else source_manifest.get("channel_count") or 1
        ),
        "channels": channels,
        "sha256": sha256,
        "source_manifest": source_manifest,
    }


def quality_flags(path: Path, probe: AudioProbe, settings: IngestSettings) -> list[str]:
    flags: list[str] = ["mixed_channel"]
    ext = path.suffix.lower()
    codec = (probe.codec or "").lower()
    if ext not in AUDIO_EXTENSIONS:
        flags.append("unsupported_extension")
    if codec.startswith("gsm"):
        flags.append("unsupported_codec")
    if probe.duration_sec is None or probe.duration_sec < settings.min_duration_seconds:
        flags.append("short")
    if probe.sample_rate_hz and probe.sample_rate_hz != 16000:
        flags.append("needs_resample")
    return sorted(set(flags))


def is_rejected(probe: AudioProbe, flags: list[str]) -> bool:
    return "unsupported_extension" in flags or "unsupported_codec" in flags or "short" in flags


def layout_dir_name(storage_layout_version: str) -> str:
    if storage_layout_version.startswith("raw-"):
        return storage_layout_version.split("-", 1)[1]
    return storage_layout_version


def final_paths(
    settings: IngestSettings, metadata: dict[str, Any], audio_path: Path
) -> tuple[Path, Path]:
    started_at = metadata.get("started_at")
    if not isinstance(started_at, dt.datetime):
        started_at = dt.datetime.now(MSK)
    store_id = safe_slug(str(metadata.get("store_id") or "unknown"))
    raw_path = (
        settings.raw_dir
        / layout_dir_name(settings.storage_layout_version)
        / store_id
        / f"{started_at.year:04d}"
        / f"{started_at.month:02d}"
        / f"{started_at.day:02d}"
        / f"{metadata['dialog_id']}{audio_path.suffix.lower()}"
    )
    manifest_path = Path(str(raw_path) + ".manifest.json")
    return raw_path, manifest_path


def get_existing_dialog(env: dict[str, str], dialog_id: str) -> tuple[str, str, str] | None:
    sql = f"""
    SELECT
      COALESCE(audio_sha256, ''),
      COALESCE(audio_storage_path, ''),
      COALESCE(manifest_storage_path, '')
    FROM offline_dialog
    WHERE dialog_id = {sql_literal(dialog_id)}
    LIMIT 1;
    """
    out = run_psql(sql, env).strip()
    if not out:
        return None
    parts = out.split("\t", 2)
    while len(parts) < 3:
        parts.append("")
    return parts[0], parts[1], parts[2]


def write_raw_files(
    audio_path: Path,
    audio_storage_path: Path,
    manifest_storage_path: Path,
    manifest_json: dict[str, Any],
) -> None:
    audio_storage_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(audio_path), str(audio_storage_path))
    manifest_storage_path.write_text(
        json.dumps(manifest_json, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def mark_ingest_error(
    env: dict[str, str],
    metadata: dict[str, Any],
    audio_path: Path,
    sha256: str,
    error: str,
    flags: list[str],
    probe: AudioProbe | None = None,
) -> None:
    manifest_json = normalized_manifest_json(metadata, flags)
    sql = f"""
    INSERT INTO offline_dialog (
      dialog_id, source, store_id, store_name, pc_id, device_id,
      recorder_model, recorder_serial, record_id, microphone_model, upload_protocol,
      manifest_schema_version, ingest_pipeline_version, hardware_profile_version,
      asr_profile_version, storage_layout_version, started_at, ended_at, duration_sec,
      original_landing_path, original_filename, audio_sha256, format, codec,
      sample_rate_hz, channel_count, file_size_bytes, normalized_manifest_json,
      quality_flags, ingest_status, asr_status, analysis_status, ingest_error, updated_at
    ) VALUES (
      {sql_literal(metadata['dialog_id'])},
      {sql_literal(metadata.get('source') or 'offline_store')},
      {sql_literal(metadata.get('store_id') or 'unknown')},
      {nullable_sql(metadata.get('store_name'))},
      {nullable_sql(metadata.get('pc_id'))},
      {nullable_sql(metadata.get('device_id'))},
      {nullable_sql(metadata.get('recorder_model'))},
      {nullable_sql(metadata.get('recorder_serial'))},
      {nullable_sql(metadata.get('record_id'))},
      {nullable_sql(metadata.get('microphone_model'))},
      {nullable_sql(metadata.get('upload_protocol'))},
      {int(metadata.get('manifest_schema_version') or 1)},
      {sql_literal(metadata.get('ingest_pipeline_version') or DEFAULT_PIPELINE_VERSION)},
      {sql_literal(metadata.get('hardware_profile_version') or DEFAULT_HARDWARE_PROFILE_VERSION)},
      {sql_literal(metadata.get('asr_profile_version') or DEFAULT_ASR_PROFILE_VERSION)},
      {sql_literal(metadata.get('storage_layout_version') or DEFAULT_STORAGE_LAYOUT_VERSION)},
      {timestamptz_sql(metadata.get('started_at'))},
      {timestamptz_sql(metadata.get('ended_at'))},
      {number_sql(metadata.get('duration_sec'))},
      {sql_literal(str(audio_path))},
      {sql_literal(audio_path.name)},
      {sql_literal(sha256)},
      {nullable_sql(probe.format_name if probe else None)},
      {nullable_sql(probe.codec if probe else metadata.get('codec'))},
      {number_sql(probe.sample_rate_hz if probe else metadata.get('sample_rate_hz'))},
      {number_sql(probe.channel_count if probe else metadata.get('channel_count'))},
      {number_sql(probe.file_size_bytes if probe else audio_path.stat().st_size)},
      {json_sql(manifest_json)},
      {json_sql(flags)},
      'error',
      'skipped',
      'skipped',
      {b64_text_sql(error)},
      now()
    )
    ON CONFLICT (dialog_id) DO UPDATE SET
      ingest_status = 'error',
      asr_status = 'skipped',
      analysis_status = 'skipped',
      ingest_error = EXCLUDED.ingest_error,
      quality_flags = EXCLUDED.quality_flags,
      updated_at = now();
    """
    run_psql(sql, env, capture=False)


def normalized_manifest_json(metadata: dict[str, Any], flags: list[str]) -> dict[str, Any]:
    return {
        "manifest_schema_version": metadata.get("manifest_schema_version", 1),
        "dialog_id": metadata["dialog_id"],
        "source": metadata.get("source"),
        "store_id": metadata.get("store_id"),
        "store_name": metadata.get("store_name"),
        "pc_id": metadata.get("pc_id"),
        "device_id": metadata.get("device_id"),
        "recorder_model": metadata.get("recorder_model"),
        "recorder_serial": metadata.get("recorder_serial"),
        "record_id": metadata.get("record_id"),
        "microphone_model": metadata.get("microphone_model"),
        "upload_protocol": metadata.get("upload_protocol"),
        "ingest_pipeline_version": metadata.get("ingest_pipeline_version"),
        "hardware_profile_version": metadata.get("hardware_profile_version"),
        "asr_profile_version": metadata.get("asr_profile_version"),
        "storage_layout_version": metadata.get("storage_layout_version"),
        "started_at": metadata["started_at"].isoformat() if metadata.get("started_at") else None,
        "ended_at": metadata["ended_at"].isoformat() if metadata.get("ended_at") else None,
        "duration_sec": metadata.get("duration_sec"),
        "audio_format": metadata.get("audio_format"),
        "sample_rate_hz": metadata.get("sample_rate_hz"),
        "codec": metadata.get("codec"),
        "channel_count": metadata.get("channel_count"),
        "channels": metadata.get("channels") or [{"index": 0, "role": "mixed"}],
        "sha256": metadata.get("sha256"),
        "quality_flags": flags,
    }


def upsert_stored_dialog(
    env: dict[str, str],
    settings: IngestSettings,
    metadata: dict[str, Any],
    audio_path: Path,
    probe: AudioProbe,
    sha256: str,
    flags: list[str],
) -> str:
    existing = get_existing_dialog(env, metadata["dialog_id"])
    if existing:
        existing_sha, existing_path, existing_manifest_path = existing
        if existing_sha == sha256:
            manifest_json = normalized_manifest_json(metadata, flags)
            fallback_audio_path, fallback_manifest_path = final_paths(
                settings, metadata, audio_path
            )
            audio_storage_path = Path(existing_path) if existing_path else fallback_audio_path
            manifest_storage_path = (
                Path(existing_manifest_path) if existing_manifest_path else fallback_manifest_path
            )
            if audio_path.exists() and not audio_storage_path.exists():
                write_raw_files(
                    audio_path, audio_storage_path, manifest_storage_path, manifest_json
                )
            run_psql(
                f"""
                UPDATE offline_dialog
                SET updated_at = now(),
                    ingest_status = 'stored',
                    audio_storage_path = {sql_literal(str(audio_storage_path))},
                    manifest_storage_path = {sql_literal(str(manifest_storage_path))},
                    normalized_manifest_json = {json_sql(manifest_json)},
                    quality_flags = {json_sql(flags)}
                WHERE dialog_id = {sql_literal(metadata['dialog_id'])};
                """,
                env,
                capture=False,
            )
            if audio_storage_path.exists() and audio_path.exists():
                audio_path.unlink()
            return "noop"
        mark_ingest_error(
            env,
            metadata,
            audio_path,
            sha256,
            "dialog_id reused with different checksum",
            sorted(set(flags + ["checksum_mismatch"])),
            probe,
        )
        return "checksum_mismatch"

    audio_storage_path, manifest_storage_path = final_paths(settings, metadata, audio_path)
    manifest_json = normalized_manifest_json(metadata, flags)
    sql = f"""
    INSERT INTO offline_dialog (
      dialog_id, source, store_id, store_name, pc_id, device_id,
      recorder_model, recorder_serial, record_id, microphone_model, upload_protocol,
      manifest_schema_version, ingest_pipeline_version, hardware_profile_version,
      asr_profile_version, storage_layout_version, started_at, ended_at, duration_sec,
      audio_storage_path, manifest_storage_path, original_landing_path, original_filename,
      audio_sha256, format, codec, sample_rate_hz, channel_count, file_size_bytes,
      normalized_manifest_json, quality_flags, ingest_status, asr_status, analysis_status,
      ingest_error, stored_at, updated_at
    ) VALUES (
      {sql_literal(metadata['dialog_id'])},
      {sql_literal(metadata.get('source') or 'offline_store')},
      {sql_literal(metadata.get('store_id') or 'unknown')},
      {nullable_sql(metadata.get('store_name'))},
      {nullable_sql(metadata.get('pc_id'))},
      {nullable_sql(metadata.get('device_id'))},
      {nullable_sql(metadata.get('recorder_model'))},
      {nullable_sql(metadata.get('recorder_serial'))},
      {nullable_sql(metadata.get('record_id'))},
      {nullable_sql(metadata.get('microphone_model'))},
      {nullable_sql(metadata.get('upload_protocol'))},
      {int(metadata.get('manifest_schema_version') or 1)},
      {sql_literal(metadata.get('ingest_pipeline_version') or DEFAULT_PIPELINE_VERSION)},
      {sql_literal(metadata.get('hardware_profile_version') or DEFAULT_HARDWARE_PROFILE_VERSION)},
      {sql_literal(metadata.get('asr_profile_version') or DEFAULT_ASR_PROFILE_VERSION)},
      {sql_literal(metadata.get('storage_layout_version') or DEFAULT_STORAGE_LAYOUT_VERSION)},
      {timestamptz_sql(metadata.get('started_at'))},
      {timestamptz_sql(metadata.get('ended_at'))},
      {number_sql(metadata.get('duration_sec'))},
      {sql_literal(str(audio_storage_path))},
      {sql_literal(str(manifest_storage_path))},
      {sql_literal(str(audio_path))},
      {sql_literal(audio_path.name)},
      {sql_literal(sha256)},
      {nullable_sql(probe.format_name)},
      {nullable_sql(probe.codec)},
      {number_sql(probe.sample_rate_hz)},
      {number_sql(probe.channel_count)},
      {number_sql(probe.file_size_bytes)},
      {json_sql(manifest_json)},
      {json_sql(flags)},
      'stored',
      'pending',
      'pending',
      NULL,
      now(),
      now()
    );
    """
    run_psql(sql, env, capture=False)

    try:
        write_raw_files(audio_path, audio_storage_path, manifest_storage_path, manifest_json)
    except Exception as exc:
        mark_ingest_error(
            env, metadata, audio_path, sha256, f"raw_store_failed: {exc}", flags, probe
        )
        raise
    return "stored"


def process_landing_file(env: dict[str, str], settings: IngestSettings, path: Path) -> str:
    sha256 = compute_sha256(path)
    probe: AudioProbe | None = None
    try:
        probe = probe_audio(path)
        metadata = normalize_manifest(path, settings, sha256, probe)
        flags = quality_flags(path, probe, settings)
        if is_rejected(probe, flags):
            mark_ingest_error(
                env, metadata, path, sha256, "audio rejected by quality gate", flags, probe
            )
            return "ingest_error"
        return upsert_stored_dialog(env, settings, metadata, path, probe, sha256, flags)
    except Exception as exc:
        metadata = normalize_manifest(path, settings, sha256, probe, manifest={})
        mark_ingest_error(
            env, metadata, path, sha256, f"ingest_failed: {exc}", ["invalid_audio"], probe
        )
        return "ingest_error"


def get_asr_candidates(env: dict[str, str], max_files: int) -> list[tuple[str, str]]:
    sql = f"""
    SELECT dialog_id, audio_storage_path
    FROM offline_dialog
    WHERE ingest_status = 'stored'
      AND asr_status = 'pending'
      AND COALESCE(audio_storage_path, '') <> ''
    ORDER BY COALESCE(started_at, created_at) ASC
    LIMIT {int(max_files)};
    """
    out = run_psql(sql, env)
    rows: list[tuple[str, str]] = []
    for line in out.splitlines():
        dialog_id, audio_path = line.split("\t", 1)
        if dialog_id and audio_path:
            rows.append((dialog_id, audio_path))
    return rows


def transcribe_offline_audio(
    env: dict[str, str], dialog_id: str, audio_path: Path
) -> tuple[str, str]:
    mode = (env.get("OFFLINE_AUDIO_ASR_MODE") or env.get("ASR_MODE") or "ssh").strip().lower()
    if mode != "ssh":
        raise RuntimeError(f"offline_audio_asr_mode_unsupported: {mode}")
    settings = resolve_ssh_worker_settings(env)
    remote_audio_path = windows_join(
        settings["audio_dir"], safe_slug(dialog_id) + audio_path.suffix.lower()
    )
    remote_audio_target = f"{settings['host']}:{windows_to_scp_path(remote_audio_path)}"
    ensure_remote_audio_dir(env, settings)

    upload = subprocess.run(
        ["scp", "-q", str(audio_path), remote_audio_target],
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


def update_asr_status(
    env: dict[str, str], dialog_id: str, status: str, error: str | None = None
) -> None:
    started = "asr_started_at = now()," if status == "processing" else ""
    completed = "asr_completed_at = now()," if status in {"done", "error"} else ""
    sql = f"""
    UPDATE offline_dialog
    SET asr_status = {sql_literal(status)},
        {started}
        {completed}
        asr_error = {nullable_sql(error)},
        updated_at = now()
    WHERE dialog_id = {sql_literal(dialog_id)};
    """
    run_psql(sql, env, capture=False)


def store_transcript(
    env: dict[str, str],
    dialog_id: str,
    transcript_text: str,
    model_label: str,
    asr_profile_version: str,
) -> None:
    summary = make_summary(transcript_text, 240)
    sql = f"""
    INSERT INTO offline_dialog_transcript (
      dialog_id, language, model, asr_profile_version, transcript_text,
      segments_json, channel_roles_json, updated_at
    ) VALUES (
      {sql_literal(dialog_id)},
      'ru',
      {sql_literal(model_label)},
      {sql_literal(asr_profile_version)},
      {b64_text_sql(transcript_text)},
      NULL,
      {json_sql([{'index': 0, 'role': 'mixed'}])},
      now()
    )
    ON CONFLICT (dialog_id) DO UPDATE SET
      language = 'ru',
      model = EXCLUDED.model,
      asr_profile_version = EXCLUDED.asr_profile_version,
      transcript_text = EXCLUDED.transcript_text,
      updated_at = now();

    INSERT INTO offline_dialog_analysis (
      dialog_id, summary, sentiment, outcome, business_flags_json,
      quality_flags_json, analysis_model, analysis_profile_version, updated_at
    ) VALUES (
      {sql_literal(dialog_id)},
      {b64_text_sql(summary)},
      'unknown',
      'pending_review',
      {json_sql({'status': 'asr_only'})},
      (SELECT quality_flags FROM offline_dialog WHERE dialog_id = {sql_literal(dialog_id)}),
      {sql_literal(model_label)},
      {sql_literal(asr_profile_version)},
      now()
    )
    ON CONFLICT (dialog_id) DO UPDATE SET
      summary = EXCLUDED.summary,
      sentiment = EXCLUDED.sentiment,
      outcome = EXCLUDED.outcome,
      business_flags_json = EXCLUDED.business_flags_json,
      quality_flags_json = EXCLUDED.quality_flags_json,
      analysis_model = EXCLUDED.analysis_model,
      analysis_profile_version = EXCLUDED.analysis_profile_version,
      updated_at = now();

    UPDATE offline_dialog
    SET asr_status = 'done',
        analysis_status = 'done',
        asr_error = NULL,
        asr_completed_at = now(),
        updated_at = now()
    WHERE dialog_id = {sql_literal(dialog_id)};
    """
    run_psql(sql, env, capture=False)


def process_asr_candidate(
    env: dict[str, str], settings: IngestSettings, dialog_id: str, audio_path: str
) -> str:
    try:
        path = Path(audio_path)
        if not path.exists():
            raise RuntimeError(f"audio_missing: {audio_path}")
        update_asr_status(env, dialog_id, "processing")
        transcript, model_label = transcribe_offline_audio(env, dialog_id, path)
        store_transcript(env, dialog_id, transcript, model_label, settings.asr_profile_version)
        return "asr_done"
    except Exception as exc:
        category, details = normalize_error_reason(str(exc))
        update_asr_status(env, dialog_id, "error", f"{category}: {details}")
        return "asr_error"


def get_quality_stats(env: dict[str, str]) -> dict[str, int]:
    sql = """
    SELECT
      count(*)::text,
      sum(CASE WHEN ingest_status = 'stored' THEN 1 ELSE 0 END)::text,
      sum(CASE WHEN ingest_status = 'error' THEN 1 ELSE 0 END)::text,
      sum(CASE WHEN asr_status = 'done' THEN 1 ELSE 0 END)::text,
      sum(CASE WHEN asr_status = 'error' THEN 1 ELSE 0 END)::text
    FROM offline_dialog;
    """
    out = run_psql(sql, env).strip()
    values = [int(part or "0") for part in out.split("\t")[:5]] if out else [0, 0, 0, 0, 0]
    while len(values) < 5:
        values.append(0)
    return {
        "received": values[0],
        "stored": values[1],
        "ingest_error": values[2],
        "asr_done": values[3],
        "asr_error": values[4],
    }


def get_quality_flag_stats(env: dict[str, str]) -> dict[str, int]:
    tracked_flags = ["silent", "short", "clipped", "unsupported_codec", "checksum_mismatch"]
    sql = f"""
    SELECT flag, count(*)::text
    FROM offline_dialog,
      json_array_elements_text(COALESCE(quality_flags, '[]'::json)) AS flag
    WHERE flag IN ({", ".join(sql_literal(flag) for flag in tracked_flags)})
    GROUP BY flag;
    """
    out = run_psql(sql, env).strip()
    counts = {flag: 0 for flag in tracked_flags}
    for line in out.splitlines():
        flag, count_text = line.split("\t", 1)
        counts[flag] = int(count_text or "0")
    return counts


def get_freshness_hours(env: dict[str, str]) -> float | None:
    sql = """
    SELECT EXTRACT(EPOCH FROM (now() - max(received_at))) / 3600.0
    FROM offline_dialog
    WHERE ingest_status = 'stored';
    """
    out = run_psql(sql, env).strip()
    if not out:
        return None
    return round(float(out), 2)


def build_quality_report(env: dict[str, str], settings: IngestSettings) -> dict[str, Any]:
    freshness_hours = get_freshness_hours(env)
    freshness_threshold_hours = 2
    return {
        "status": "ok",
        "side_effects": False,
        "quality": get_quality_stats(env),
        "quality_flags": get_quality_flag_stats(env),
        "freshness_hours": freshness_hours,
        "freshness_threshold_hours": freshness_threshold_hours,
        "freshness_alert": freshness_hours is None or freshness_hours > freshness_threshold_hours,
        "versions": {
            "pipeline": settings.pipeline_version,
            "manifest_schema": 1,
            "hardware_profile": settings.hardware_profile_version,
            "asr_profile": settings.asr_profile_version,
            "storage_layout": settings.storage_layout_version,
        },
    }


def build_plan(env: dict[str, str]) -> dict[str, Any]:
    settings = resolve_settings(env)
    candidates, scan_counts = scan_landing_files(settings.landing_dir)
    errors: list[str] = []
    if not env.get("DATABASE_URL"):
        errors.append("missing env: DATABASE_URL")
    if not shutil.which("ffprobe"):
        errors.append("missing tool: ffprobe")
    return {
        "status": "blocked" if errors else "ready",
        "side_effects": False,
        "versions": {
            "pipeline": settings.pipeline_version,
            "manifest_schema": 1,
            "hardware_profile": settings.hardware_profile_version,
            "asr_profile": settings.asr_profile_version,
            "storage_layout": settings.storage_layout_version,
        },
        "paths": {
            "landing_dir": str(settings.landing_dir),
            "raw_dir": str(settings.raw_dir),
        },
        "planned_reads": [
            str(settings.landing_dir),
            "offline_dialog ASR candidates",
        ],
        "planned_writes": [
            "offline_dialog upsert",
            "offline_dialog_transcript upsert",
            "offline_dialog_analysis upsert",
            str(settings.raw_dir),
        ],
        "counts": {
            "landing_candidates": len(candidates),
            **scan_counts,
        },
        "errors": errors,
    }


def run_ingest(env: dict[str, str], settings: IngestSettings, max_files: int) -> Counter[str]:
    candidates, counts = scan_landing_files(settings.landing_dir)
    results = Counter(counts)
    for candidate in candidates[:max_files]:
        results[process_landing_file(env, settings, candidate.path)] += 1
    return results


def run_asr(env: dict[str, str], settings: IngestSettings, max_files: int) -> Counter[str]:
    results: Counter[str] = Counter()
    for dialog_id, audio_path in get_asr_candidates(env, max_files):
        results[process_asr_candidate(env, settings, dialog_id, audio_path)] += 1
    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ingest offline store audio and run ASR candidates."
    )
    parser.add_argument("--plan-only", action="store_true", help="Emit a read-only execution plan")
    parser.add_argument(
        "--report-only", action="store_true", help="Emit a read-only quality report"
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON summary")
    parser.add_argument(
        "--max-files", type=int, default=int(os.getenv("OFFLINE_AUDIO_MAX_FILES", "100"))
    )
    parser.add_argument(
        "--max-asr", type=int, default=int(os.getenv("OFFLINE_AUDIO_ASR_MAX_FILES", "20"))
    )
    parser.add_argument("--skip-asr", action="store_true", help="Only ingest files, do not run ASR")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    env = load_env(ENV_FILE)
    settings = resolve_settings(env)

    if args.plan_only:
        payload = build_plan(env)
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(f"status={payload['status']} side_effects=false")
            for key, value in payload["counts"].items():
                print(f"{key}={value}")
            for error in payload["errors"]:
                print(f"error={error}")
        return

    if args.report_only:
        if not env.get("DATABASE_URL"):
            raise RuntimeError("missing env: DATABASE_URL")
        payload = build_quality_report(env, settings)
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return

    if not env.get("DATABASE_URL"):
        raise RuntimeError("missing env: DATABASE_URL")

    ingest_counts = run_ingest(env, settings, args.max_files)
    asr_counts = Counter()
    if not args.skip_asr:
        asr_counts = run_asr(env, settings, args.max_asr)

    summary: dict[str, Any] = {
        "status": "ok",
        "side_effects": True,
        "ingest": dict(ingest_counts),
        "asr": dict(asr_counts),
        "quality": get_quality_stats(env),
        "freshness_hours": get_freshness_hours(env),
        "versions": {
            "pipeline": settings.pipeline_version,
            "manifest_schema": 1,
            "hardware_profile": settings.hardware_profile_version,
            "asr_profile": settings.asr_profile_version,
            "storage_layout": settings.storage_layout_version,
        },
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(130) from None
