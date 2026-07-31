from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.core.config import Settings
from app.services import executive_instruments


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
    assert result.capabilities.access_mutations is False


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
