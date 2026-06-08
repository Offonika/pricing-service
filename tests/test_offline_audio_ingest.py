from __future__ import annotations

import json
import sys
from pathlib import Path

from infra.cron import offline_audio_ingest


def _settings(tmp_path: Path) -> offline_audio_ingest.IngestSettings:
    return offline_audio_ingest.resolve_settings(
        {
            "OFFLINE_AUDIO_LANDING_DIR": str(tmp_path / "landing"),
            "OFFLINE_AUDIO_RAW_DIR": str(tmp_path / "raw"),
            "OFFLINE_AUDIO_PIPELINE_VERSION": "0.1.0-pilot.1",
            "OFFLINE_AUDIO_HARDWARE_PROFILE_VERSION": "sprecord-mic4-m1105hd-v1",
            "OFFLINE_AUDIO_ASR_PROFILE_VERSION": "offline-asr-ssh-v1",
            "OFFLINE_AUDIO_STORAGE_LAYOUT_VERSION": "raw-v1",
            "OFFLINE_AUDIO_DEVICE_CONFIG_JSON": json.dumps(
                {
                    "sprecord-001": {
                        "store_id": "store-001",
                        "store_name": "Пилот",
                        "pc_id": "pc-01",
                        "recorder_serial": "S001",
                    }
                },
                ensure_ascii=False,
            ),
        }
    )


def test_plan_only_json_does_not_write_db_or_move_files(monkeypatch, tmp_path, capsys) -> None:
    landing = tmp_path / "landing" / "sprecord" / "sprecord-001" / "incoming"
    landing.mkdir(parents=True)
    (landing / "20260514_102030.wav").write_bytes(b"audio")
    (landing / "20260514_102030.wav.part").write_bytes(b"partial")
    (landing / "empty.wav").write_bytes(b"")

    monkeypatch.setattr(
        offline_audio_ingest,
        "load_env",
        lambda _path: {
            "DATABASE_URL": "postgresql://example/masked",
            "OFFLINE_AUDIO_LANDING_DIR": str(tmp_path / "landing"),
            "OFFLINE_AUDIO_RAW_DIR": str(tmp_path / "raw"),
        },
    )
    monkeypatch.setattr(offline_audio_ingest.shutil, "which", lambda _name: "/usr/bin/ffprobe")
    monkeypatch.setattr(
        offline_audio_ingest,
        "run_psql",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("plan-only must not touch DB")
        ),
    )
    monkeypatch.setattr(sys, "argv", ["offline_audio_ingest.py", "--plan-only", "--json"])

    offline_audio_ingest.main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ready"
    assert payload["side_effects"] is False
    assert payload["counts"]["landing_candidates"] == 1
    assert payload["counts"]["ignored_temp"] == 1
    assert payload["counts"]["ignored_zero"] == 1
    assert (landing / "20260514_102030.wav").exists()


def test_scan_landing_ignores_part_zero_and_non_audio(tmp_path) -> None:
    landing = tmp_path / "landing"
    landing.mkdir()
    (landing / "record.wav").write_bytes(b"audio")
    (landing / "record.wav.part").write_bytes(b"partial")
    (landing / "empty.mp3").write_bytes(b"")
    (landing / "manifest.json").write_text("{}", encoding="utf-8")

    candidates, counts = offline_audio_ingest.scan_landing_files(landing)

    assert [candidate.path.name for candidate in candidates] == ["record.wav"]
    assert counts["candidate"] == 1
    assert counts["ignored_temp"] == 1
    assert counts["ignored_zero"] == 1
    assert counts["ignored_non_audio"] == 1


def test_fallback_manifest_uses_device_config_and_versions(tmp_path) -> None:
    settings = _settings(tmp_path)
    path = settings.landing_dir / "sprecord" / "sprecord-001" / "incoming" / "20260514_102030.wav"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"audio")
    probe = offline_audio_ingest.AudioProbe(
        duration_sec=12.5,
        sample_rate_hz=16000,
        codec="pcm_s16le",
        channel_count=1,
        format_name="wav",
        file_size_bytes=5,
    )

    metadata = offline_audio_ingest.normalize_manifest(path, settings, "a" * 64, probe)

    assert metadata["manifest_schema_version"] == 1
    assert metadata["device_id"] == "sprecord-001"
    assert metadata["store_id"] == "store-001"
    assert metadata["store_name"] == "Пилот"
    assert metadata["pc_id"] == "pc-01"
    assert metadata["recorder_serial"] == "S001"
    assert metadata["ingest_pipeline_version"] == "0.1.0-pilot.1"
    assert metadata["hardware_profile_version"] == "sprecord-mic4-m1105hd-v1"
    assert metadata["asr_profile_version"] == "offline-asr-ssh-v1"
    assert metadata["dialog_id"].startswith("offline-sprecord-001-20260514T102030")
    assert metadata["channels"] == [{"index": 0, "role": "mixed"}]


def test_stored_dialog_upsert_noops_on_same_dialog_and_checksum(monkeypatch, tmp_path) -> None:
    settings = _settings(tmp_path)
    audio_path = (
        settings.landing_dir / "sprecord" / "sprecord-001" / "incoming" / "20260514_102030.wav"
    )
    audio_path.parent.mkdir(parents=True)
    audio_path.write_bytes(b"audio")
    existing_path = tmp_path / "raw" / "existing.wav"
    existing_path.parent.mkdir(parents=True)
    existing_path.write_bytes(b"audio")
    sha = offline_audio_ingest.compute_sha256(audio_path)
    probe = offline_audio_ingest.AudioProbe(12, 16000, "pcm_s16le", 1, "wav", 5)
    metadata = offline_audio_ingest.normalize_manifest(audio_path, settings, sha, probe)
    calls: list[str] = []

    monkeypatch.setattr(
        offline_audio_ingest,
        "get_existing_dialog",
        lambda _env, _dialog_id: (sha, str(existing_path), str(existing_path) + ".manifest.json"),
    )
    monkeypatch.setattr(
        offline_audio_ingest,
        "run_psql",
        lambda sql, *_args, **_kwargs: calls.append(sql) or "",
    )
    monkeypatch.setattr(
        offline_audio_ingest.shutil,
        "move",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("same checksum must not move raw audio again")
        ),
    )

    result = offline_audio_ingest.upsert_stored_dialog(
        {"DATABASE_URL": "postgresql://example/masked"},
        settings,
        metadata,
        audio_path,
        probe,
        sha,
        ["mixed_channel"],
    )

    assert result == "noop"
    assert calls
    assert not audio_path.exists()


def test_stored_dialog_upsert_recovers_missing_raw_on_same_checksum(monkeypatch, tmp_path) -> None:
    settings = _settings(tmp_path)
    audio_path = (
        settings.landing_dir / "sprecord" / "sprecord-001" / "incoming" / "20260514_102030.wav"
    )
    audio_path.parent.mkdir(parents=True)
    audio_path.write_bytes(b"audio")
    existing_path = tmp_path / "raw" / "existing.wav"
    sha = offline_audio_ingest.compute_sha256(audio_path)
    probe = offline_audio_ingest.AudioProbe(12, 16000, "pcm_s16le", 1, "wav", 5)
    metadata = offline_audio_ingest.normalize_manifest(audio_path, settings, sha, probe)
    calls: list[str] = []

    monkeypatch.setattr(
        offline_audio_ingest,
        "get_existing_dialog",
        lambda _env, _dialog_id: (sha, str(existing_path), str(existing_path) + ".manifest.json"),
    )
    monkeypatch.setattr(
        offline_audio_ingest,
        "run_psql",
        lambda sql, *_args, **_kwargs: calls.append(sql) or "",
    )

    result = offline_audio_ingest.upsert_stored_dialog(
        {"DATABASE_URL": "postgresql://example/masked"},
        settings,
        metadata,
        audio_path,
        probe,
        sha,
        ["mixed_channel"],
    )

    assert result == "noop"
    assert calls
    assert existing_path.exists()
    assert Path(str(existing_path) + ".manifest.json").exists()
    assert not audio_path.exists()


def test_corrupt_audio_is_marked_as_ingest_error(monkeypatch, tmp_path) -> None:
    settings = _settings(tmp_path)
    audio_path = settings.landing_dir / "sprecord" / "sprecord-001" / "incoming" / "bad.wav"
    audio_path.parent.mkdir(parents=True)
    audio_path.write_bytes(b"not-a-wav")
    errors: list[tuple[str, list[str]]] = []

    monkeypatch.setattr(
        offline_audio_ingest,
        "probe_audio",
        lambda _path: (_ for _ in ()).throw(RuntimeError("ffprobe failed")),
    )
    monkeypatch.setattr(
        offline_audio_ingest,
        "mark_ingest_error",
        lambda _env, _metadata, _path, _sha, error, flags, _probe=None: errors.append(
            (error, flags)
        ),
    )

    result = offline_audio_ingest.process_landing_file(
        {"DATABASE_URL": "postgresql://example/masked"},
        settings,
        audio_path,
    )

    assert result == "ingest_error"
    assert errors
    assert "ffprobe failed" in errors[0][0]
    assert errors[0][1] == ["invalid_audio"]


def test_bad_sidecar_manifest_falls_back_to_error_metadata(monkeypatch, tmp_path) -> None:
    settings = _settings(tmp_path)
    audio_path = settings.landing_dir / "sprecord" / "sprecord-001" / "incoming" / "bad.wav"
    audio_path.parent.mkdir(parents=True)
    audio_path.write_bytes(b"audio")
    audio_path.with_suffix(".json").write_text("{not-json", encoding="utf-8")
    errors: list[tuple[dict, str, list[str]]] = []

    monkeypatch.setattr(
        offline_audio_ingest,
        "probe_audio",
        lambda _path: offline_audio_ingest.AudioProbe(12, 16000, "pcm_s16le", 1, "wav", 5),
    )
    monkeypatch.setattr(
        offline_audio_ingest,
        "mark_ingest_error",
        lambda _env, metadata, _path, _sha, error, flags, _probe=None: errors.append(
            (metadata, error, flags)
        ),
    )

    result = offline_audio_ingest.process_landing_file(
        {"DATABASE_URL": "postgresql://example/masked"},
        settings,
        audio_path,
    )

    assert result == "ingest_error"
    assert errors
    assert errors[0][0]["device_id"] == "sprecord-001"
    assert "Expecting property name" in errors[0][1]
    assert errors[0][2] == ["invalid_audio"]


def test_quality_report_is_read_only_and_contains_versions(monkeypatch, tmp_path) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(
        offline_audio_ingest,
        "get_quality_stats",
        lambda _env: {
            "received": 30,
            "stored": 28,
            "ingest_error": 2,
            "asr_done": 25,
            "asr_error": 1,
        },
    )
    monkeypatch.setattr(
        offline_audio_ingest,
        "get_quality_flag_stats",
        lambda _env: {
            "silent": 0,
            "short": 1,
            "clipped": 0,
            "unsupported_codec": 1,
            "checksum_mismatch": 0,
        },
    )
    monkeypatch.setattr(offline_audio_ingest, "get_freshness_hours", lambda _env: 2.5)

    report = offline_audio_ingest.build_quality_report(
        {"DATABASE_URL": "postgresql://example/masked"}, settings
    )

    assert report["side_effects"] is False
    assert report["quality"]["stored"] == 28
    assert report["quality_flags"]["unsupported_codec"] == 1
    assert report["freshness_alert"] is True
    assert report["versions"]["pipeline"] == "0.1.0-pilot.1"
    assert report["versions"]["manifest_schema"] == 1


def test_asr_candidate_flow_keeps_raw_audio(monkeypatch, tmp_path) -> None:
    audio_path = tmp_path / "raw" / "dialog.wav"
    audio_path.parent.mkdir()
    audio_path.write_bytes(b"audio")
    statuses: list[str] = []
    transcripts: list[tuple[str, str]] = []
    settings = _settings(tmp_path)

    monkeypatch.setattr(
        offline_audio_ingest,
        "update_asr_status",
        lambda _env, _dialog_id, status, error=None: statuses.append(f"{status}:{error or ''}"),
    )
    monkeypatch.setattr(
        offline_audio_ingest,
        "transcribe_offline_audio",
        lambda _env, _dialog_id, _path: ("тестовая расшифровка", "ssh-faster-whisper:tiny@asr-win"),
    )
    monkeypatch.setattr(
        offline_audio_ingest,
        "store_transcript",
        lambda _env, dialog_id, text, model, _profile: transcripts.append((dialog_id, model)),
    )

    result = offline_audio_ingest.process_asr_candidate(
        {"DATABASE_URL": "postgresql://example/masked"},
        settings,
        "dialog-1",
        str(audio_path),
    )

    assert result == "asr_done"
    assert statuses == ["processing:"]
    assert transcripts == [("dialog-1", "ssh-faster-whisper:tiny@asr-win")]
    assert audio_path.exists()
