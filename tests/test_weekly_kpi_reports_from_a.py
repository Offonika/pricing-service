from __future__ import annotations

import json
import urllib.error
from datetime import date
from pathlib import Path

from infra.cron.weekly_kpi_reports_from_a import (
    _is_retail_director_manifest,
    _render_weekly_kpi_overview,
    _resolve_webhook_url,
    sync_weekly_kpi_reports,
)


def _manifest(
    *, report_id: int = 1, revision: int = 1, box_id: str | None = "2001"
) -> dict[str, object]:
    return {
        "report_id": report_id,
        "report_key": "emp_manager|2026-04-05",
        "revision": revision,
        "overall_signal": "good",
        "summary_payload": {
            "header": {
                "title": "Отчет за неделю 2026-03-30 — 2026-04-05",
                "subtitle": "Иван Иванов / Менеджер по продажам",
            },
            "wins": ["Выручка выше прошлой недели", "Средний чек удержан"],
            "risks": ["Снижение количества продаж в одном канале"],
            "next_actions": ["Проверить конверсию по проблемному каналу"],
            "overall_signal": "good",
        },
        "employee": {
            "employee_key": "emp_manager",
            "employee_name": "Иван Иванов",
            "role_code": "sales_manager",
            "position_name": "Менеджер по продажам",
            "bitrix_user_id": "1001",
            "bitrix_box_user_id": box_id,
        },
        "period": {
            "week_start": "2026-03-30",
            "week_end": "2026-04-05",
            "source_as_of": "2026-04-05",
        },
        "artifact_url": "/api/management/weekly-kpi-reports/1/artifact",
    }


def test_sync_weekly_kpi_reports_deduplicates_and_downloads_once(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "reports": {
                    "emp_manager|2026-04-05|r1": {
                        "report_key": "emp_manager|2026-04-05",
                        "revision": 1,
                        "delivery_status": "delivered",
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    downloaded: list[str] = []
    delivered: list[dict[str, object]] = []

    def fetch_json(path: str, params: dict[str, str]) -> dict[str, object]:
        assert params == {"week_end": "2026-04-05"}
        if path.endswith("/health"):
            return {"status": "ready"}
        if path.endswith("/weekly-kpi-reports"):
            return {"payload": [_manifest()]}
        raise AssertionError(f"unexpected path {path}")

    def download_artifact(url: str) -> bytes:
        downloaded.append(url)
        return b"xlsx"

    def deliver_report(**kwargs):
        delivered.append(kwargs)
        return {"notify_id": 5001, "disk_object_id": 7001, "artifact_url": "https://bitrix/file"}

    summary = sync_weekly_kpi_reports(
        fetch_json=fetch_json,
        download_artifact=download_artifact,
        deliver_report=deliver_report,
        week_end=date(2026, 4, 5),
        state_path=state_path,
        artifact_dir=tmp_path / "artifacts",
    )

    assert summary["delivered"] == 0
    assert summary["noop"] == 1
    assert downloaded == []
    assert delivered == []


def test_sync_weekly_kpi_reports_delivers_correction_once(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "reports": {
                    "emp_manager|2026-04-05|r1": {
                        "report_key": "emp_manager|2026-04-05",
                        "revision": 1,
                        "delivery_status": "delivered",
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    delivered: list[dict[str, object]] = []

    def fetch_json(path: str, params: dict[str, str]) -> dict[str, object]:
        if path.endswith("/health"):
            return {"status": "ready"}
        return {"payload": [_manifest(report_id=2, revision=2)]}

    def download_artifact(url: str) -> bytes:
        assert url == "/api/management/weekly-kpi-reports/1/artifact"
        return b"xlsx"

    def deliver_report(**kwargs):
        delivered.append(kwargs)
        return {"notify_id": 9001}

    summary = sync_weekly_kpi_reports(
        fetch_json=fetch_json,
        download_artifact=download_artifact,
        deliver_report=deliver_report,
        week_end=date(2026, 4, 5),
        state_path=state_path,
        artifact_dir=tmp_path / "artifacts",
    )

    assert summary["delivered"] == 1
    assert summary["failed"] == 0
    assert delivered[0]["is_correction"] is True
    assert delivered[0]["target_user_id"] == 2001

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["reports"]["emp_manager|2026-04-05|r2"]["delivery_status"] == "delivered"


def test_sync_weekly_kpi_reports_marks_missing_target_as_undelivered(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"

    def fetch_json(path: str, params: dict[str, str]) -> dict[str, object]:
        if path.endswith("/health"):
            return {"status": "ready"}
        return {"payload": [_manifest(box_id=None)]}

    summary = sync_weekly_kpi_reports(
        fetch_json=fetch_json,
        download_artifact=lambda url: b"xlsx",
        deliver_report=lambda **kwargs: {"notify_id": 1},
        week_end=date(2026, 4, 5),
        state_path=state_path,
        artifact_dir=tmp_path / "artifacts",
        target_mode="box",
    )

    assert summary["undelivered"] == 1
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["reports"]["emp_manager|2026-04-05|r1"]["delivery_status"] == "undelivered"


def test_sync_weekly_kpi_reports_returns_error_when_source_unavailable(tmp_path: Path) -> None:
    def fetch_json(path: str, params: dict[str, str]) -> dict[str, object]:
        raise urllib.error.URLError("server-a down")

    summary = sync_weekly_kpi_reports(
        fetch_json=fetch_json,
        download_artifact=lambda url: b"xlsx",
        deliver_report=lambda **kwargs: {"notify_id": 1},
        week_end=date(2026, 4, 5),
        state_path=tmp_path / "state.json",
        artifact_dir=tmp_path / "artifacts",
    )

    assert summary["status"] == "error"
    assert summary["failed"] == 1
    assert summary["health_status"] == "unavailable"


def test_render_weekly_kpi_overview_uses_summary_payload_only() -> None:
    manifest = _manifest()
    manifest["metrics"] = [{"metric_code": "should_not_be_rendered"}]

    text = _render_weekly_kpi_overview(manifest, is_correction=True)

    assert "Исправленная версия weekly KPI-отчета." in text
    assert "Сильные стороны:" in text
    assert "Зоны внимания:" in text
    assert "Следующие шаги:" in text
    assert "should_not_be_rendered" not in text


def test_render_weekly_kpi_overview_appends_retail_director_monthly_kpi() -> None:
    manifest = _manifest()
    manifest["employee"]["role_code"] = "retail_director"
    manifest["employee"]["position_name"] = "Руководитель сети торговых точек"

    text = _render_weekly_kpi_overview(
        manifest,
        is_correction=False,
        retail_director_monthly_kpi={
            "month": "2026-03",
            "writeoff_amount": 1229121.82,
            "receipt_amount": 526672.97,
            "shrinkage_amount": 702448.85,
            "shrinkage_pct": 0.8499,
            "kpi_index_sum": 0.7214,
            "kpi_bonus_amount": 54105.0,
            "to_pay": 234105.0,
        },
    )

    assert "Закрытый месяц 2026-03:" in text
    assert "чистые потери 702 449 ₽" in text
    assert "уровень 0,8499%" in text
    assert "бонус 54 105 ₽" in text


def test_is_retail_director_manifest_accepts_retail_network_head() -> None:
    manifest = _manifest()
    manifest["employee"]["role_code"] = "retail_network_head"

    assert _is_retail_director_manifest(manifest) is True


def test_resolve_webhook_url_uses_existing_bitrix_box_env_as_fallback() -> None:
    assert (
        _resolve_webhook_url({"BITRIX24_BOX_WEBHOOK_URL": "https://example.test/rest"})
        == "https://example.test/rest"
    )
