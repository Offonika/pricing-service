from __future__ import annotations

import json
import urllib.error
from pathlib import Path

from infra.cron.telephony_line_map_from_a import (
    render_summary,
    sync_telephony_retail_line_map,
    upsert_retail_line_map_rows,
)


def _payload(*, store_name: str = "Савелово 531") -> dict[str, object]:
    return {
        "snapshot_date": "2026-04-18",
        "payload": [
            {
                "line_id": "531",
                "phone_number": "",
                "store_id": "telephony_user_10837",
                "store_name": store_name,
                "mapping_mode": "single_active_bitrix_user",
                "active_user_count": 1,
                "total_user_count": 1,
                "store_names": ["Савелово"],
                "employee_names": ["Асадбек Олимжонов"],
                "bitrix_user_ids": ["10837"],
                "primary_bitrix_user_id": "10837",
                "primary_employee_name": "Асадбек Олимжонов",
                "primary_store_name": "Савелово",
            }
        ],
    }


def _employee_payload() -> dict[str, object]:
    return {
        "snapshot_date": "2026-04-18",
        "payload": [
            {
                "snapshot_date": "2026-04-18",
                "mapping_source": "active_extension",
                "user_ref_hex": "0x001",
                "user_name": "user.531",
                "physical_person_ref_hex": "0x101",
                "physical_person_name": "Асадбек Олимжонов",
                "computer_name": "pc-531",
                "extension": "531",
                "store_ref_hex": "0x201",
                "store_code": "531",
                "store_name": "Савелово",
                "department_ref_hex": "0x301",
                "department_code": "retail",
                "department_name": "Розница",
                "employment_status": "active",
                "staff_store_ref": "0x201",
                "staff_store_name": "Савелово",
                "staff_department_ref": "0x301",
                "staff_department_name": "Розница",
                "bitrix_user_id": "10837",
                "bitrix_full_name": "Асадбек Олимжонов",
                "mdm_employee_code": "E531",
                "bitrix_status": "active",
                "is_marked": False,
                "has_extension": True,
                "has_bitrix": True,
            }
        ],
    }


def test_sync_telephony_line_map_deduplicates_by_revision(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    upserts: list[dict[str, object]] = []

    def fetch_json(path: str, params: dict[str, str]) -> dict[str, object]:
        if path.endswith("/health"):
            return {"status": "ready"}
        return _payload()

    first = sync_telephony_retail_line_map(
        fetch_json=fetch_json,
        upsert_rows=lambda items, deactivate_missing, preserve_line_ids: upserts.append(
            {
                "items": items,
                "deactivate_missing": deactivate_missing,
                "preserve_line_ids": tuple(preserve_line_ids),
            }
        )
        or {"inserted": 1, "updated": 0, "deactivated": 0},
        state_path=state_path,
        artifact_dir=tmp_path / "artifacts",
    )
    second = sync_telephony_retail_line_map(
        fetch_json=fetch_json,
        upsert_rows=lambda items, deactivate_missing, preserve_line_ids: {
            "inserted": 1,
            "updated": 0,
            "deactivated": 0,
        },
        state_path=state_path,
        artifact_dir=tmp_path / "artifacts",
    )

    assert first["status"] == "ok"
    assert first["delivered"] == 1
    assert second["noop"] == 1
    assert len(upserts) == 1


def test_sync_telephony_line_map_delivers_correction_when_mapping_changes(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    delivered: list[dict[str, object]] = []

    def fetch_json_v1(path: str, params: dict[str, str]) -> dict[str, object]:
        if path.endswith("/health"):
            return {"status": "ready"}
        return _payload(store_name="Савелово 531")

    def fetch_json_v2(path: str, params: dict[str, str]) -> dict[str, object]:
        if path.endswith("/health"):
            return {"status": "ready"}
        return _payload(store_name="Асадбек Олимжонов")

    sync_telephony_retail_line_map(
        fetch_json=fetch_json_v1,
        upsert_rows=lambda items, deactivate_missing, preserve_line_ids: delivered.append(
            {"items": items, "preserve_line_ids": tuple(preserve_line_ids)}
        )
        or {"inserted": 1, "updated": 0, "deactivated": 0},
        state_path=state_path,
        artifact_dir=tmp_path / "artifacts",
    )
    second = sync_telephony_retail_line_map(
        fetch_json=fetch_json_v2,
        upsert_rows=lambda items, deactivate_missing, preserve_line_ids: delivered.append(
            {"items": items, "preserve_line_ids": tuple(preserve_line_ids)}
        )
        or {"inserted": 0, "updated": 1, "deactivated": 0},
        state_path=state_path,
        artifact_dir=tmp_path / "artifacts",
    )

    assert second["delivered"] == 1
    assert len(delivered) == 2
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert len(state["reports"]) == 2


def test_sync_telephony_line_map_returns_error_when_source_unavailable(tmp_path: Path) -> None:
    def fetch_json(path: str, params: dict[str, str]) -> dict[str, object]:
        raise urllib.error.URLError("server-a down")

    summary = sync_telephony_retail_line_map(
        fetch_json=fetch_json,
        upsert_rows=lambda items, deactivate_missing, preserve_line_ids: {
            "inserted": 0,
            "updated": 0,
            "deactivated": 0,
        },
        state_path=tmp_path / "state.json",
        artifact_dir=tmp_path / "artifacts",
    )

    assert summary["status"] == "error"
    assert summary["failed"] == 1


def test_sync_telephony_line_map_rejects_unhealthy_source(tmp_path: Path) -> None:
    def fetch_json(path: str, params: dict[str, str]) -> dict[str, object]:
        if path.endswith("/health"):
            return {"status": "degraded"}
        return _payload()

    summary = sync_telephony_retail_line_map(
        fetch_json=fetch_json,
        upsert_rows=lambda items, deactivate_missing, preserve_line_ids: {
            "inserted": 0,
            "updated": 0,
            "deactivated": 0,
        },
        state_path=tmp_path / "state.json",
        artifact_dir=tmp_path / "artifacts",
    )

    assert summary["status"] == "error"
    assert summary["health_status"] == "degraded"
    assert summary["failed"] == 1


def test_sync_telephony_line_map_includes_diff_and_review_preservation(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    stage_calls: list[dict[str, object]] = []
    upserts: list[dict[str, object]] = []

    def fetch_json(path: str, params: dict[str, str]) -> dict[str, object]:
        if path.endswith("/health"):
            return {"status": "ok"}
        return _payload()

    summary = sync_telephony_retail_line_map(
        fetch_json=fetch_json,
        upsert_rows=lambda items, deactivate_missing, preserve_line_ids: upserts.append(
            {"preserve_line_ids": tuple(preserve_line_ids)}
        )
        or {"inserted": 0, "updated": 1, "deactivated": 0},
        state_path=state_path,
        artifact_dir=tmp_path / "artifacts",
        preserve_line_ids={"584"},
        load_existing_rows=lambda: {
            "531": {
                "line_id": "531",
                "phone_number": "",
                "store_id": "telephony_line_531",
                "store_name": "Старое имя",
            },
            "584": {
                "line_id": "584",
                "phone_number": "",
                "store_id": "telephony_user_7535",
                "store_name": "Вячеслав Шевцов",
            },
        },
        stage_rows=lambda items, snapshot_date, revision: stage_calls.append(
            {
                "snapshot_date": snapshot_date,
                "revision": revision,
                "rows": len(items),
            }
        )
        or {"staged_rows": len(items)},
    )

    assert summary["status"] == "ok"
    assert summary["diff"]["changed"] == 1
    assert summary["diff"]["preserved_missing"] == 1
    assert summary["diff"]["preserved_missing_line_ids"] == ["584"]
    assert summary["stage"]["staged_rows"] == 1
    assert stage_calls
    assert upserts[0]["preserve_line_ids"] == ("584",)


def test_sync_telephony_line_map_stages_employee_snapshot(tmp_path: Path) -> None:
    employee_stage_calls: list[dict[str, object]] = []

    def fetch_json(path: str, params: dict[str, str]) -> dict[str, object]:
        if path.endswith("/health"):
            return {"status": "ready"}
        if path.endswith("/employee-line-map"):
            assert params["active_only"] == "true"
            assert params["with_extension_only"] == "true"
            return _employee_payload()
        return _payload()

    summary = sync_telephony_retail_line_map(
        fetch_json=fetch_json,
        upsert_rows=lambda items, deactivate_missing, preserve_line_ids: {
            "inserted": 1,
            "updated": 0,
            "deactivated": 0,
        },
        state_path=tmp_path / "state.json",
        artifact_dir=tmp_path / "artifacts",
        stage_employee_rows=lambda items, snapshot_date, revision: employee_stage_calls.append(
            {
                "snapshot_date": snapshot_date,
                "revision": revision,
                "items": items,
            }
        )
        or {"staged_rows": len(items)},
    )

    assert summary["status"] == "ok"
    assert summary["employee_fetched"] == 1
    assert summary["employee_stage"]["staged_rows"] == 1
    assert employee_stage_calls[0]["items"][0]["user_ref_hex"] == "0x001"
    assert employee_stage_calls[0]["items"][0]["has_extension"] is True
    assert Path(summary["actions"][0]["employee_artifact_path"]).exists()


def test_upsert_retail_line_map_rows_keeps_review_line_ids_active(tmp_path: Path) -> None:
    database_url = f"postgresql://postgres:postgres@127.0.0.1:1/not-used-{tmp_path.name}"
    calls: list[str] = []

    def fake_run_psql(sql: str, *, database_url: str) -> str:
        calls.append(sql)
        if "SELECT COALESCE(line_id, '')" in sql:
            return "531\n584\n"
        if "SELECT count(*)::text FROM changed" in sql:
            return "0\n"
        return ""

    from infra.cron import telephony_line_map_from_a as module

    original = module._run_psql
    module._run_psql = fake_run_psql
    try:
        upsert_retail_line_map_rows(
            [
                {
                    "line_id": "531",
                    "phone_number": "",
                    "store_id": "telephony_user_10837",
                    "store_name": "Асадбек Олимжонов",
                }
            ],
            database_url=database_url,
            deactivate_missing=True,
            preserve_line_ids={"584"},
        )
    finally:
        module._run_psql = original

    deactivate_sql = next(
        sql for sql in calls if "UPDATE retail_line_map" in sql and "SET is_active = false" in sql
    )
    assert "line_id NOT IN ('584')" in deactivate_sql


def test_render_summary_includes_revision() -> None:
    summary = {
        "status": "ok",
        "snapshot_date": "2026-04-18",
        "health_status": "ready",
        "fetched": 41,
        "delivered": 1,
        "noop": 0,
        "failed": 0,
        "revision": "abc123",
        "employee_stage": {"staged_rows": 7},
        "actions": [
            {
                "action": "deliver",
                "report_key": "telephony-retail-line-map|2026-04-18",
                "revision": "abc123",
            }
        ],
    }

    rendered = render_summary(summary)

    assert "telephony_line_map_from_a: ok" in rendered
    assert "revision: abc123" in rendered
    assert "employee_staged_rows: 7" in rendered
