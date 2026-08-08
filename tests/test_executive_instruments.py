from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest

from app.core.config import Settings
from app.services import executive_instruments

_PRODUCER_EXCHANGE_FIXTURE = (
    Path(__file__).parent / "fixtures" / "instruments_snapshot_v4_exchange_contract.json"
)
_PRODUCER_EXCHANGE_FIXTURE_SHA256 = (
    "fa7bd6590c5cb0566a871bcc517964f49333ebb1d544be2d0fc2fd41074bdb18"
)


def _settings(path: Path, *, max_lag_minutes: int = 30) -> Settings:
    return Settings(
        management_internal_api_token="test-token",
        executive_dashboard_instruments_snapshot_path=str(path),
        executive_dashboard_instruments_max_lag_minutes=max_lag_minutes,
    )


def _snapshot(*, generated_at: datetime) -> dict[str, object]:
    return {
        "schema_version": 2,
        "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
        "source_status": "partial",
        "freshness_status": "fresh",
        "summary": {
            "total_count": 1,
            "online_count": 1,
            "critical_count": 0,
            "warning_count": 1,
            "not_monitored_count": 0,
            "backup_gap_count": 1,
            "access_review_count": 1,
            "monitoring_coverage_24h_pct": 100,
        },
        "devices": [
            {
                "device_key": "onec-ka-bp-zup-win",
                "name": "Офисный Windows-сервер новой 1С КА/БП/ЗУП",
                "kind": "server",
                "lifecycle_status": "active",
                "health_status": "warning",
                "connectivity_status": "online",
                "criticality": "critical",
                "location": "RU/требует уточнения",
                "purpose": ["1С КА 2, Бухгалтерия предприятия и ЗУП"],
                "technical_owner_ids": ["115204"],
                "technical_owners": ["Технический владелец"],
                "business_owner": "Эльдар Ахмедов",
                "last_attempted_at": generated_at.isoformat().replace("+00:00", "Z"),
                "last_success_at": generated_at.isoformat().replace("+00:00", "Z"),
                "monitoring_coverage_24h_pct": 100,
                "monitoring_coverage_30d_pct": 100,
                "metrics": {"cpu_used_pct": 25, "disk_free_pct": 42},
                "services": [
                    {
                        "service_key": "onec-ka-bp-zup",
                        "name": "1С КА 2, Бухгалтерия предприятия и ЗУП",
                        "component_kind": "service",
                        "status": "warning",
                        "criticality": "critical",
                        "source_project": "1C_Dev_Workflow",
                    }
                ],
                "backup": {
                    "status": "warning",
                    "protected_datastores": 0,
                    "unprotected_datastores": 1,
                    "off_host_verified": False,
                    "readback_verified": False,
                },
                "integrations": {"status": "warning", "count": 1},
                "access": {
                    "status": "warning",
                    "active_grants": 1,
                    "pending_grants": 0,
                    "review_required_grants": 1,
                    "mfa_review_count": 0,
                    "unowned_credentials": 0,
                    "attention_grant_count": 1,
                },
                "issue": "Резервирование требует настройки или проверки",
                "recommended_action": "Проверить копию и выполнить restore-test",
            }
        ],
        "warnings": [],
        "capabilities": {
            "access_governance": "read_only",
            "access_mutations": False,
            "network_scanning": False,
        },
    }


def _exchange_block(*, generated_at: datetime) -> dict[str, object]:
    return {
        "status": "critical",
        "queue_items": 3600,
        "queue_status": "critical",
        "last_success_at": (generated_at - timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
        "last_error_at": generated_at.isoformat().replace("+00:00", "Z"),
        "consecutive_failures": 3,
        "active_job_seconds": 960,
        "stage_last": "init",
        "stage_file_missing_cycles": 2,
        "platform_cpu_pct": 96.5,
        "source_status": "partial",
    }


def _snapshot_v4(*, generated_at: datetime) -> dict[str, object]:
    payload = _snapshot(generated_at=generated_at)
    payload["schema_version"] = 4
    payload["devices"][0]["exchange"] = _exchange_block(generated_at=generated_at)  # type: ignore[index]
    payload["devices"][0]["problems"] = [  # type: ignore[index]
        {
            "problem_key": "service:ut103_site_exchange:failed",
            "category": "service",
            "severity": "critical",
            "title": "Обмен с сайтом не передаёт файл",
            "evidence": ["Циклов без передачи файла: 2"],
            "started_at": generated_at.isoformat().replace("+00:00", "Z"),
            "recommended_action": "Проверить очередь и историю обмена",
        },
        {
            "problem_key": "service:ut103_site_exchange:loop_overload",
            "category": "service",
            "severity": "critical",
            "title": "Обмен зациклен с перегрузкой",
            "evidence": ["Циклов без убывания очереди: 2"],
            "started_at": generated_at.isoformat().replace("+00:00", "Z"),
            "recommended_action": "Проверить активную работу обмена",
        },
    ]
    return payload


def test_loads_v4_snapshot_with_exchange_block(monkeypatch, tmp_path: Path) -> None:
    now = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
    path = tmp_path / "v4.json"
    path.write_text(
        json.dumps(_snapshot_v4(generated_at=now), ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setattr(executive_instruments, "get_settings", lambda: _settings(path))

    result = executive_instruments.load_executive_instruments_snapshot(now=now)

    assert result.schema_version == 4
    exchange = result.devices[0].exchange
    assert exchange.status == "critical"
    assert exchange.queue_items == 3600
    assert exchange.queue_status == "critical"
    assert exchange.consecutive_failures == 3
    assert exchange.active_job_seconds == 960
    assert exchange.stage_last == "init"
    assert exchange.stage_file_missing_cycles == 2
    # Producer нормализует общую загрузку платформы в диапазон 0..100.
    assert exchange.platform_cpu_pct == 96.5
    assert exchange.source_status == "partial"
    assert [problem.problem_key for problem in result.devices[0].problems] == [
        "service:ut103_site_exchange:failed",
        "service:ut103_site_exchange:loop_overload",
    ]


def test_loads_real_producer_v4_exchange_contract(monkeypatch, tmp_path: Path) -> None:
    generated_at = datetime(2026, 8, 6, 9, 0, tzinfo=UTC)
    payload = _snapshot(generated_at=generated_at)
    fixture_bytes = _PRODUCER_EXCHANGE_FIXTURE.read_bytes()
    assert sha256(fixture_bytes).hexdigest() == _PRODUCER_EXCHANGE_FIXTURE_SHA256
    producer_fragment = json.loads(fixture_bytes)
    payload["schema_version"] = producer_fragment["schema_version"]
    payload["devices"][0]["exchange"] = producer_fragment["exchange"]  # type: ignore[index]
    payload["devices"][0]["problems"] = producer_fragment["problems"]  # type: ignore[index]
    path = tmp_path / "producer-v4.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(executive_instruments, "get_settings", lambda: _settings(path))

    result = executive_instruments.load_executive_instruments_snapshot(path=path, now=generated_at)

    assert result.schema_version == 4
    assert result.devices[0].exchange.source_status == "ready"
    assert result.devices[0].exchange.last_error_at == datetime(2026, 8, 6, 8, 45, tzinfo=UTC)
    assert [problem.problem_key for problem in result.devices[0].problems] == [
        "service:ut103_site_exchange:failed",
        "service:ut103_site_exchange:loop_overload",
    ]


def test_v2_v3_and_v4_without_block_default_exchange_is_not_configured(
    monkeypatch, tmp_path: Path
) -> None:
    now = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    for version in (2, 3, 4):
        payload = _snapshot(generated_at=now)
        payload["schema_version"] = version
        path = tmp_path / f"v{version}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(
            executive_instruments, "get_settings", lambda path=path: _settings(path)
        )

        result = executive_instruments.load_executive_instruments_snapshot(now=now)

        exchange = result.devices[0].exchange
        assert exchange.status == "not_configured"
        assert exchange.source_status == "not_configured"
        assert exchange.queue_items is None
        assert exchange.queue_status is None
        assert exchange.consecutive_failures is None
        assert exchange.active_job_seconds is None
        assert exchange.stage_last is None
        assert exchange.stage_file_missing_cycles is None
        assert exchange.platform_cpu_pct is None
        assert exchange.last_success_at is None


def test_v4_not_configured_exchange_keeps_cpu_without_raising_status(
    monkeypatch, tmp_path: Path
) -> None:
    now = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
    payload = _snapshot_v4(generated_at=now)
    payload["devices"][0]["exchange"] = {  # type: ignore[index]
        "status": "not_configured",
        "platform_cpu_pct": 70,
        "source_status": "not_configured",
    }
    path = tmp_path / "v4-not-configured-with-cpu.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(executive_instruments, "get_settings", lambda: _settings(path))

    result = executive_instruments.load_executive_instruments_snapshot(now=now)

    exchange = result.devices[0].exchange
    assert exchange.status == "not_configured"
    assert exchange.source_status == "not_configured"
    assert exchange.platform_cpu_pct == 70
    assert exchange.consecutive_failures is None
    assert exchange.stage_file_missing_cycles is None


def test_v4_exchange_negative_matrix_is_rejected(monkeypatch, tmp_path: Path) -> None:
    now = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
    variants = []

    forbidden_key = _snapshot_v4(generated_at=now)
    forbidden_key["devices"][0]["exchange"]["endpoint"] = "internal"  # type: ignore[index]
    variants.append(forbidden_key)

    extra_field = _snapshot_v4(generated_at=now)
    extra_field["devices"][0]["exchange"]["unexpected_metric"] = 1  # type: ignore[index]
    variants.append(extra_field)

    forbidden_url = _snapshot_v4(generated_at=now)
    forbidden_url["devices"][0]["exchange"]["note"] = "https://shop.example.com/upload"  # type: ignore[index]
    variants.append(forbidden_url)

    forbidden_host = _snapshot_v4(generated_at=now)
    forbidden_host["devices"][0]["exchange"]["note"] = "ut103-1cserv:1541"  # type: ignore[index]
    variants.append(forbidden_host)

    for unsafe_evidence in (
        "tmp/exchange",
        r"tmp\exchange",
        "relative/path",
        r"relative\path",
        r"C:\temp\noms\1cbitrix",
        r"\\fileserver\exchange\noms",
        "/var/log/nginx",
        "/путь/к/обмену",
        "noms/1cbitrix",
        r"noms\1cbitrix",
        "https://shop.example.com/upload",
        "internal-node:1541",
        "password=not-a-real-secret",
        "Произвольный текст без канонического формата",
    ):
        problem_path = _snapshot_v4(generated_at=now)
        problem_path["devices"][0]["problems"] = [  # type: ignore[index]
            {
                "problem_key": "service:ut103_site_exchange:failed",
                "category": "service",
                "severity": "critical",
                "title": "Обмен с сайтом деградировал",
                "evidence": [unsafe_evidence],
                "recommended_action": "Проверить очередь обмена",
            }
        ]
        variants.append(problem_path)

    for field_name, invalid_timestamp in (
        ("last_success_at", "2026-08-06"),
        ("last_success_at", "2026-08-06T10:00:00"),
        ("last_error_at", "2026-08-06T13:00:00+03:00"),
        ("last_error_at", (now + timedelta(minutes=1)).isoformat().replace("+00:00", "Z")),
    ):
        invalid_time = _snapshot_v4(generated_at=now)
        invalid_time["devices"][0]["exchange"][field_name] = invalid_timestamp  # type: ignore[index]
        variants.append(invalid_time)

    for invalid_cpu_value in (True, "96.5", 100.1):
        invalid_cpu = _snapshot_v4(generated_at=now)
        invalid_cpu["devices"][0]["exchange"]["platform_cpu_pct"] = invalid_cpu_value  # type: ignore[index]
        variants.append(invalid_cpu)

    for invalid_float in (float("nan"), float("inf"), float("-inf")):
        non_finite_cpu = _snapshot_v4(generated_at=now)
        non_finite_cpu["devices"][0]["exchange"]["platform_cpu_pct"] = invalid_float  # type: ignore[index]
        variants.append(non_finite_cpu)

    for field_name in (
        "queue_items",
        "consecutive_failures",
        "active_job_seconds",
        "stage_file_missing_cycles",
    ):
        for invalid_count in (True, "1"):
            invalid_integer = _snapshot_v4(generated_at=now)
            invalid_integer["devices"][0]["exchange"][field_name] = invalid_count  # type: ignore[index]
            variants.append(invalid_integer)

    negative_count = _snapshot_v4(generated_at=now)
    negative_count["devices"][0]["exchange"]["consecutive_failures"] = -1  # type: ignore[index]
    variants.append(negative_count)

    for field_name, invalid_enum in (
        ("status", "red"),
        ("queue_status", "red"),
        ("stage_last", "upload"),
        ("source_status", "error"),
    ):
        bad_enum = _snapshot_v4(generated_at=now)
        bad_enum["devices"][0]["exchange"][field_name] = invalid_enum  # type: ignore[index]
        variants.append(bad_enum)

    unknown_schema = _snapshot_v4(generated_at=now)
    unknown_schema["schema_version"] = 5
    variants.append(unknown_schema)

    false_severity = _snapshot_v4(generated_at=now)
    false_severity["devices"][0]["exchange"]["source_status"] = "not_configured"  # type: ignore[index]
    false_severity["devices"][0]["exchange"]["status"] = "warning"  # type: ignore[index]
    variants.append(false_severity)

    for invalid_problem_key in (
        "exchange_failed",
        "exchange_loop_overload",
        "service:ut103_site_exchange:unknown",
    ):
        invalid_problem = _snapshot_v4(generated_at=now)
        invalid_problem["devices"][0]["problems"][0]["problem_key"] = (  # type: ignore[index]
            invalid_problem_key
        )
        variants.append(invalid_problem)

    for index, payload in enumerate(variants):
        path = tmp_path / f"v4-invalid-{index}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(
            executive_instruments, "get_settings", lambda path=path: _settings(path)
        )

        result = executive_instruments.load_executive_instruments_snapshot(now=now)

        assert result.source_status == "source_error"
        assert result.devices == []


def test_sanitizer_does_not_treat_dates_or_safe_evidence_as_paths() -> None:
    executive_instruments._assert_sanitized(  # noqa: SLF001 - адресный regression test
        {
            "observed_date": "/2026/08/06",
            "evidence": [
                "Стадия сайта: файл не передан",
                "Циклов без передачи файла: 2",
                "Статусы: ready/warning/critical/not_configured",
                "Наблюдение: 2026-08-06T12:00:00Z",
                "relative/path",
                r"relative\path",
            ],
        }
    )


@pytest.mark.parametrize(
    "unsafe_value",
    [
        "https://localhost/upload",
        "file:///var/log/exchange",
        "internal-node:1541",
        "ut103-1cserv",
        r"C:\temp\exchange",
        r"\\fileserver\exchange",
        "/tmp",
        "/var/log/exchange",
        "noms/1cbitrix",
        r"noms\1cbitrix",
        "noms/1cbitrix/file.xml",
        "prefix/noms/1cbitrix",
        r"prefix\NOMS\1CBITRIX\file.xml",
        "NOMS/1CBitrix",
        "password=not-a-real-secret",
    ],
)
def test_sanitizer_rejects_urls_hosts_paths_and_secrets(unsafe_value: str) -> None:
    with pytest.raises(ValueError):
        executive_instruments._assert_sanitized(  # noqa: SLF001 - security boundary
            {"evidence": [unsafe_value]}
        )


@pytest.mark.parametrize("unsafe_key", ["host", "hostname", "url", "uri"])
def test_sanitizer_rejects_host_and_url_fields(unsafe_key: str) -> None:
    with pytest.raises(ValueError):
        executive_instruments._assert_sanitized(  # noqa: SLF001 - security boundary
            {unsafe_key: "redacted"}
        )


def test_loads_sanitized_read_only_snapshot(monkeypatch, tmp_path: Path) -> None:
    now = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    path = tmp_path / "infrastructure.json"
    path.write_text(json.dumps(_snapshot(generated_at=now), ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(executive_instruments, "get_settings", lambda: _settings(path))

    result = executive_instruments.load_executive_instruments_snapshot(now=now)

    assert result.source_status == "partial"
    assert result.freshness_status == "fresh"
    assert result.summary.total_count == 1
    assert result.devices[0].connectivity_status == "online"
    assert result.devices[0].access.review_required_grants == 1
    assert result.devices[0].problems[0].problem_key == "configuration:legacy-issue"
    assert result.devices[0].problems[0].title == result.devices[0].issue
    assert result.capabilities.access_mutations is False


def test_loads_v3_snapshot_with_structured_problems(monkeypatch, tmp_path: Path) -> None:
    now = datetime(2026, 8, 4, 9, 0, tzinfo=UTC)
    payload = _snapshot(generated_at=now)
    payload["schema_version"] = 3
    payload["devices"][0]["problems"] = [  # type: ignore[index]
        {
            "problem_key": "resources:cpu",
            "category": "resources",
            "severity": "critical",
            "title": "Загрузка CPU вышла за безопасный порог",
            "evidence": ["Фактическое значение: 96%", "Порог critical: ≥ 95%"],
            "started_at": now.isoformat().replace("+00:00", "Z"),
            "recommended_action": "Проверить нагрузку",
        },
        {
            "problem_key": "backup:protection",
            "category": "backup",
            "severity": "warning",
            "title": "Резервирование требует проверки",
            "evidence": ["Restore-test не подтверждён"],
            "started_at": None,
            "recommended_action": "Выполнить restore-test",
        },
    ]
    path = tmp_path / "v3.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(executive_instruments, "get_settings", lambda: _settings(path))

    result = executive_instruments.load_executive_instruments_snapshot(now=now)

    assert result.schema_version == 3
    assert [problem.category for problem in result.devices[0].problems] == [
        "resources",
        "backup",
    ]
    assert result.devices[0].problems[0].evidence[0] == "Фактическое значение: 96%"


def test_missing_snapshot_is_explicit_not_zero_fact(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "missing.json"
    monkeypatch.setattr(executive_instruments, "get_settings", lambda: _settings(path))

    result = executive_instruments.load_executive_instruments_snapshot()

    assert result.source_status == "source_missing"
    assert result.freshness_status == "missing"
    assert result.devices == []
    assert "не опубликован" in (result.note or "")


def test_stale_snapshot_remains_visible_with_stale_status(monkeypatch, tmp_path: Path) -> None:
    now = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    path = tmp_path / "stale.json"
    path.write_text(
        json.dumps(_snapshot(generated_at=now - timedelta(hours=2)), ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(executive_instruments, "get_settings", lambda: _settings(path))

    result = executive_instruments.load_executive_instruments_snapshot(now=now)

    assert result.freshness_status == "stale"
    assert result.devices[0].name.startswith("Офисный Windows")


def test_snapshot_with_network_or_secret_field_is_rejected(monkeypatch, tmp_path: Path) -> None:
    now = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    payload = _snapshot(generated_at=now)
    payload["devices"][0]["public_host"] = "example.invalid"  # type: ignore[index]
    path = tmp_path / "unsafe.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(executive_instruments, "get_settings", lambda: _settings(path))

    result = executive_instruments.load_executive_instruments_snapshot(now=now)

    assert result.source_status == "source_error"
    assert result.devices == []
    assert "безопасную проверку" in (result.note or "")


def test_access_mutation_capability_is_rejected(monkeypatch, tmp_path: Path) -> None:
    now = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    payload = _snapshot(generated_at=now)
    payload["capabilities"]["access_mutations"] = True  # type: ignore[index]
    path = tmp_path / "unsafe-capability.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(executive_instruments, "get_settings", lambda: _settings(path))

    result = executive_instruments.load_executive_instruments_snapshot(now=now)

    assert result.source_status == "source_error"
    assert result.capabilities.access_mutations is False


def test_v1_snapshot_is_rejected(monkeypatch, tmp_path: Path) -> None:
    now = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    payload = _snapshot(generated_at=now)
    payload["schema_version"] = 1
    path = tmp_path / "v1.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(executive_instruments, "get_settings", lambda: _settings(path))

    result = executive_instruments.load_executive_instruments_snapshot(now=now)

    assert result.source_status == "source_error"
    assert result.devices == []


def test_duplicate_device_and_summary_mismatch_are_rejected(monkeypatch, tmp_path: Path) -> None:
    now = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    payload = _snapshot(generated_at=now)
    payload["devices"].append(dict(payload["devices"][0]))  # type: ignore[index,union-attr]
    path = tmp_path / "duplicate.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(executive_instruments, "get_settings", lambda: _settings(path))

    result = executive_instruments.load_executive_instruments_snapshot(now=now)

    assert result.source_status == "source_error"


def test_unknown_field_negative_count_and_future_observation_are_rejected(
    monkeypatch, tmp_path: Path
) -> None:
    now = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    variants = []
    unknown = _snapshot(generated_at=now)
    unknown["devices"][0]["unexpected"] = True  # type: ignore[index]
    variants.append(unknown)
    negative = _snapshot(generated_at=now)
    negative["devices"][0]["access"]["active_grants"] = -1  # type: ignore[index]
    variants.append(negative)
    future = _snapshot(generated_at=now)
    future["devices"][0]["last_success_at"] = (now + timedelta(hours=1)).isoformat()  # type: ignore[index]
    variants.append(future)

    for index, payload in enumerate(variants):
        path = tmp_path / f"invalid-{index}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(
            executive_instruments, "get_settings", lambda path=path: _settings(path)
        )
        result = executive_instruments.load_executive_instruments_snapshot(now=now)
        assert result.source_status == "source_error"


def test_network_scanning_and_host_port_value_are_rejected(monkeypatch, tmp_path: Path) -> None:
    now = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    payload = _snapshot(generated_at=now)
    payload["capabilities"]["network_scanning"] = True  # type: ignore[index]
    payload["devices"][0]["issue"] = "internal-node:22022"  # type: ignore[index]
    path = tmp_path / "unsafe-network.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(executive_instruments, "get_settings", lambda: _settings(path))

    result = executive_instruments.load_executive_instruments_snapshot(now=now)

    assert result.source_status == "source_error"


def test_ssh_alias_in_string_value_is_rejected(monkeypatch, tmp_path: Path) -> None:
    now = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    payload = _snapshot(generated_at=now)
    payload["devices"][0]["issue"] = "asr-win"  # type: ignore[index]
    path = tmp_path / "unsafe-alias.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(executive_instruments, "get_settings", lambda: _settings(path))

    result = executive_instruments.load_executive_instruments_snapshot(now=now)

    assert result.source_status == "source_error"
