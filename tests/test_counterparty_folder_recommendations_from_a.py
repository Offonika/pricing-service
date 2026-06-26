from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from infra.cron.counterparty_folder_recommendations_from_a import (
    REPORT_ENDPOINT,
    STATUS_MOVE_RECOMMENDED,
    STATUS_NEEDS_REVIEW,
    sync_counterparty_folder_recommendations,
)


def _report() -> dict[str, Any]:
    return {
        "as_of": "2026-05-29",
        "freshness_status": "fresh",
        "source_status": "ready",
        "report_revision": "abc123",
        "summary": {
            "total_count": 1,
            "source_snapshot_count": 5,
            "move_recommended_count": 1,
            "needs_review_count": 0,
            "below_min_balance_count": 2,
            "min_recommendation_balance": "500.00",
        },
        "payload": [
            {
                "counterparty_ref": "cp-site",
                "counterparty_code": "РБ053785",
                "counterparty_name": "Контрагент из папки Сайт",
                "current_balance": "12000.00",
                "current_folder_name": "08. Сайт",
                "recommended_folder_name": "02. СПБ",
                "debt_department_name": "СПБ",
                "debt_document_ref": "doc-open-spb",
                "debt_document_number": "РТУ-OPEN",
                "debt_document_date": "2026-05-03T10:00:00",
                "open_debt_documents": [
                    {
                        "document_ref": "doc-open-spb",
                        "document_number": "РТУ-OPEN",
                        "document_date": "2026-05-03T10:00:00",
                        "open_amount": "12000.00",
                        "debt_department_name": "СПБ",
                        "document_author_name": "Автор СПБ",
                        "statement_selection_rule": "statement_unmatched_open_sale",
                    }
                ],
                "debt_document_responsible_name": "Автор СПБ",
                "origin_document_ref": "doc-old-spb",
                "origin_document_number": "РТУ-1",
                "origin_document_date": "2026-05-01T10:00:00",
                "overdue_days": 21,
                "credit_depth_days": 7,
                "due_date": "2026-05-08T10:00:00",
                "effective_overdue_days": 21,
                "effective_credit_depth_days": 7,
                "effective_due_date": "2026-05-08T10:00:00",
                "effective_payment_term_source": "credit_depth_days",
                "document_structure_status": "confirmed_open",
                "document_structure_open_amount": "12000.00",
                "document_structure_sale_amount": "12000.00",
                "document_structure_closing_amount": "0.00",
                "document_structure_order_number": None,
                "document_structure_order_date": None,
                "document_structure_linked_documents": [],
                "status": STATUS_MOVE_RECOMMENDED,
                "review_reason": None,
            }
        ],
    }


def test_counterparty_folder_wrapper_exports_csv_and_dedupes(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    def fetch_json(path: str, params: dict[str, str]) -> dict[str, Any]:
        calls.append((path, params))
        return _report()

    state_path = tmp_path / "state.json"
    artifact_dir = tmp_path / "artifacts"
    summary = sync_counterparty_folder_recommendations(
        fetch_json=fetch_json,
        snapshot_date=date(2026, 5, 29),
        state_path=state_path,
        artifact_dir=artifact_dir,
    )

    assert calls == [
        (
            REPORT_ENDPOINT,
            {"date": "2026-05-29", "status": STATUS_MOVE_RECOMMENDED},
        )
    ]
    assert summary["action"] == "export"
    assert summary["exported"] == 1
    artifact_path = Path(summary["artifact_path"])
    assert artifact_path.exists()
    csv_text = artifact_path.read_text(encoding="utf-8-sig")
    assert "Контрагент из папки Сайт" in csv_text
    assert "Код клиента" in csv_text
    assert "РБ053785" in csv_text
    assert "Источник срока оплаты" in csv_text
    assert "Открытые документы по ведомостной логике 1С" in csv_text
    assert "Ответственный РТУ" in csv_text
    assert "Автор СПБ" in csv_text
    assert "Правило выбора источника" in csv_text
    assert "открытая РТУ по ведомостной логике" in csv_text
    assert "Документ витрины дебиторки" in csv_text
    assert "РТУ-OPEN" in csv_text
    assert "Статус проверки структуры" in csv_text
    assert "открытый остаток подтвержден структурой 1С" in csv_text
    assert "Причина проверки код" in csv_text
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state_key = f"2026-05-29|{STATUS_MOVE_RECOMMENDED}|abc123"
    assert state["reports"][state_key]["export_status"] == "exported"

    second_summary = sync_counterparty_folder_recommendations(
        fetch_json=fetch_json,
        snapshot_date=date(2026, 5, 29),
        state_path=state_path,
        artifact_dir=artifact_dir,
    )
    assert second_summary["action"] == "noop"
    assert second_summary["reason"] == "already_exported"


def test_counterparty_folder_wrapper_dry_run_has_no_side_effects(tmp_path: Path) -> None:
    def fetch_json(path: str, params: dict[str, str]) -> dict[str, Any]:
        assert path == REPORT_ENDPOINT
        assert params["status"] == STATUS_MOVE_RECOMMENDED
        return _report()

    state_path = tmp_path / "state.json"
    artifact_dir = tmp_path / "artifacts"
    summary = sync_counterparty_folder_recommendations(
        fetch_json=fetch_json,
        snapshot_date=date(2026, 5, 29),
        state_path=state_path,
        artifact_dir=artifact_dir,
        dry_run=True,
    )

    assert summary["action"] == "dry_run"
    assert summary["exported"] == 0
    assert not state_path.exists()
    assert not Path(summary["artifact_path"]).exists()


def test_counterparty_folder_wrapper_delivers_non_empty_report_to_bitrix(tmp_path: Path) -> None:
    comments: list[tuple[int, str]] = []

    def fetch_json(path: str, params: dict[str, str]) -> dict[str, Any]:
        assert path == REPORT_ENDPOINT
        assert params["status"] == STATUS_MOVE_RECOMMENDED
        return _report()

    def deliver_comment(task_id: int, message: str) -> int:
        comments.append((task_id, message))
        return 777

    state_path = tmp_path / "state.json"
    artifact_dir = tmp_path / "artifacts"
    summary = sync_counterparty_folder_recommendations(
        fetch_json=fetch_json,
        snapshot_date=date(2026, 5, 29),
        state_path=state_path,
        artifact_dir=artifact_dir,
        bitrix_task_id=756,
        deliver_comment=deliver_comment,
    )

    assert summary["delivery_action"] == "deliver"
    assert summary["bitrix_comment_id"] == 777
    assert summary["delivered"] == 1
    assert len(comments) == 1
    task_id, message = comments[0]
    assert task_id == 756
    assert "📌 Отчет по контролю папок контрагентов" in message
    assert "📊 Сводка" in message
    assert "🧾 Первые строки" in message
    assert "Контрагент из папки Сайт" in message
    assert "Контрагент из папки Сайт (код клиента: РБ053785)" in message
    assert "• ⏸️ Готовых рекомендаций к переносу: 1" in message
    assert "• 🧹 Скрыто мелких долгов ниже 500.00 ₽: 2" in message
    assert (
        "🧾 Открытые документы по ведомостной логике 1С: Реализация РТУ-OPEN"
        in message
    )
    assert "👤 Ответственный РТУ: Автор СПБ" in message
    assert "🧭 Правило выбора: открытая РТУ по ведомостной логике" in message
    assert "Накладная, выбранная витриной дебиторки" not in message
    assert "🔗 Структура 1С: открытый остаток подтвержден структурой 1С" in message

    second_summary = sync_counterparty_folder_recommendations(
        fetch_json=fetch_json,
        snapshot_date=date(2026, 5, 29),
        state_path=state_path,
        artifact_dir=artifact_dir,
        bitrix_task_id=756,
        deliver_comment=deliver_comment,
    )
    assert second_summary["delivery_action"] == "noop"
    assert second_summary["delivery_reason"] == "already_delivered"
    assert len(comments) == 1


def test_counterparty_folder_wrapper_mentions_needs_review_reasons(tmp_path: Path) -> None:
    comments: list[tuple[int, str]] = []

    def review_report() -> dict[str, Any]:
        payload = _report()
        payload["report_revision"] = "review123"
        payload["summary"] = {
            "total_count": 1,
            "source_snapshot_count": 5,
            "move_recommended_count": 0,
            "needs_review_count": 1,
            "review_reason_counts": {
                "folder_mismatch_payment_term_missing": 1,
                "origin_document_needs_order_payment_check": 1,
                "origin_document_structure_unconfirmed": 1,
                "origin_document_structure_confirmed_manual_review": 1,
                "origin_document_closed_by_structure": 1,
            },
        }
        payload["payload"][0]["status"] = STATUS_NEEDS_REVIEW
        payload["payload"][0]["review_reason"] = "folder_mismatch_payment_term_missing"
        return payload

    def fetch_json(path: str, params: dict[str, str]) -> dict[str, Any]:
        assert path == REPORT_ENDPOINT
        assert params["status"] == STATUS_NEEDS_REVIEW
        return review_report()

    def deliver_comment(task_id: int, message: str) -> int:
        comments.append((task_id, message))
        return 778

    summary = sync_counterparty_folder_recommendations(
        fetch_json=fetch_json,
        snapshot_date=date(2026, 5, 29),
        state_path=tmp_path / "state.json",
        artifact_dir=tmp_path / "artifacts",
        status=STATUS_NEEDS_REVIEW,
        bitrix_task_id=756,
        deliver_comment=deliver_comment,
    )

    assert summary["delivery_action"] == "deliver"
    assert len(comments) == 1
    assert "⚠️ Причины ручной проверки" in comments[0][1]
    assert "папка отличается, но срок оплаты не заполнен" in comments[0][1]
    assert "долг сайта: проверить заказ, оплату картой" in comments[0][1]
    assert "источник долга требует проверки структуры документа" in comments[0][1]
    assert "структура 1С подтверждает открытый остаток" in comments[0][1]
    assert "выбранный документ закрыт по структуре 1С" in comments[0][1]
    assert "folder_mismatch_payment_term_missing" not in comments[0][1]
    assert "origin_document_needs_order_payment_check" not in comments[0][1]
    assert "origin_document_structure_unconfirmed" not in comments[0][1]
    assert "origin_document_structure_confirmed_manual_review" not in comments[0][1]
    assert "origin_document_closed_by_structure" not in comments[0][1]


def test_counterparty_folder_wrapper_skips_empty_bitrix_delivery(tmp_path: Path) -> None:
    def empty_report() -> dict[str, Any]:
        payload = _report()
        payload["report_revision"] = "empty123"
        payload["summary"] = {
            "total_count": 0,
            "source_snapshot_count": 5,
            "move_recommended_count": 0,
            "needs_review_count": 0,
        }
        payload["payload"] = []
        return payload

    def fetch_json(path: str, params: dict[str, str]) -> dict[str, Any]:
        assert path == REPORT_ENDPOINT
        return empty_report()

    def deliver_comment(task_id: int, message: str) -> int:
        raise AssertionError("empty report should not be delivered")

    state_path = tmp_path / "state.json"
    artifact_dir = tmp_path / "artifacts"
    summary = sync_counterparty_folder_recommendations(
        fetch_json=fetch_json,
        snapshot_date=date(2026, 5, 29),
        state_path=state_path,
        artifact_dir=artifact_dir,
        bitrix_task_id=756,
        deliver_comment=deliver_comment,
    )

    assert summary["action"] == "export"
    assert summary["delivery_action"] == "skip_no_rows"
    assert summary["delivered"] == 0
