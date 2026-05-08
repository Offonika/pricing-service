from __future__ import annotations

import json
import subprocess
import sys

from infra.cron import weekly_asr_transcription


def test_plan_only_json_does_not_run_asr_or_write_db(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        weekly_asr_transcription,
        "load_env",
        lambda _path: {
            "DATABASE_URL": "postgresql://example/masked",
            "RELAY_API_KEY": "present",
            "ASR_TARGET_DATE": "2026-05-06",
        },
    )
    monkeypatch.setattr(
        weekly_asr_transcription,
        "get_candidates",
        lambda _env, _start, _end: [
            ("call-1", "https://record.invalid/1.mp3", "bitrix", "bitrix", "")
        ],
    )
    monkeypatch.setattr(
        weekly_asr_transcription,
        "get_window_stats",
        lambda _env, _start, _end: {
            "total_calls": 3,
            "with_record": 2,
            "no_record": 1,
            "with_transcript": 1,
        },
    )
    monkeypatch.setattr(
        weekly_asr_transcription,
        "process_one",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("ASR must not run in plan-only")
        ),
    )
    monkeypatch.setattr(
        weekly_asr_transcription,
        "log_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("DB writes must not run in plan-only")
        ),
    )
    monkeypatch.setattr(sys, "argv", ["weekly_asr_transcription.py", "--plan-only", "--json"])

    weekly_asr_transcription.main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ready"
    assert payload["side_effects"] is False
    assert payload["window"] == {
        "from": "2026-05-06T00:00:00+03:00",
        "to": "2026-05-07T00:00:00+03:00",
    }
    assert payload["counts"]["candidate_calls"] == 1
    assert payload["counts"]["with_transcript"] == 1


def test_plan_only_reports_missing_relay_key() -> None:
    payload = weekly_asr_transcription.build_plan({"DATABASE_URL": "postgresql://example/masked"})

    assert payload["status"] == "blocked"
    assert payload["side_effects"] is False
    assert any("RELAY_API_KEY" in item for item in payload["errors"])


def test_asr_candidates_apply_min_duration_filter(monkeypatch) -> None:
    monkeypatch.setattr(weekly_asr_transcription, "MIN_DURATION_SECONDS", 2)

    sql = weekly_asr_transcription.build_candidates_sql(
        "2026-05-06T00:00:00+03:00",
        "2026-05-07T00:00:00+03:00",
    )

    assert "grouped.duration_sec >= 2" in sql


def test_ssh_mode_downloads_recording_on_worker(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        calls.append(list(cmd))
        if cmd[0] == "ssh" and "-EncodedCommand" in cmd:
            encoded = cmd[cmd.index("-EncodedCommand") + 1]
            script = weekly_asr_transcription.base64.b64decode(encoded).decode("utf-16le")
            if "run_faster_whisper.ps1" in script:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="тестовая расшифровка\n", stderr=""
                )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(weekly_asr_transcription.subprocess, "run", fake_run)

    transcript, model = weekly_asr_transcription.run_ssh_whisper_from_relay(
        {
            "ASR_SSH_HOST": "asr-worker",
            "ASR_SSH_AUDIO_DIR": r"C:\asr-worker\incoming",
            "ASR_SSH_SCRIPT": r"C:\asr-worker\run_faster_whisper.ps1",
            "ASR_SSH_MODEL": "tiny",
            "ASR_SSH_LANGUAGE": "ru",
            "ASR_SSH_DEVICE": "cpu",
            "ASR_SSH_COMPUTE_TYPE": "int8",
        },
        ["relay-key"],
        "call-1",
        "https%3A%2F%2Frecord.invalid%2F1.mp3",
    )

    assert transcript == "тестовая расшифровка"
    assert model == "ssh-faster-whisper:tiny@asr-worker"
    assert all(call[0] != "curl" for call in calls)
    assert all(call[0] != "scp" for call in calls)
    assert any(call[:4] == ["ssh", "asr-worker", "powershell", "-NoProfile"] for call in calls)
