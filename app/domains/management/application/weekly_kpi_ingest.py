"""Weekly KPI ingestion application service."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy.orm import Session

from app.domains.management.contracts import (
    WeeklyKpiReportSnapshotIngest,
    WeeklyKpiSnapshotBatchIngest,
)
from app.models.weekly_kpi_report import (
    WeeklyKpiIngestRequest,
    WeeklyKpiReportMetricSnapshot,
    WeeklyKpiReportSnapshot,
)


class WeeklyKpiIdempotencyConflictError(RuntimeError):
    """The same idempotency key was reused for a different payload."""


def _sha256(payload: Any) -> str:
    rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _report_hash(report: WeeklyKpiReportSnapshotIngest) -> str:
    return _sha256(report.model_dump(mode="json"))


def _replace_metrics(
    snapshot: WeeklyKpiReportSnapshot,
    report: WeeklyKpiReportSnapshotIngest,
) -> None:
    snapshot.metrics.clear()
    snapshot.metrics.extend(
        WeeklyKpiReportMetricSnapshot(**metric.model_dump()) for metric in report.metrics
    )


def _apply_report(
    snapshot: WeeklyKpiReportSnapshot,
    report: WeeklyKpiReportSnapshotIngest,
    *,
    content_sha256: str,
) -> None:
    for field in (
        "week_start",
        "week_end",
        "employee_key",
        "employee_name",
        "role_code",
        "position_code",
        "position_name",
        "bitrix_user_id",
        "bitrix_box_user_id",
        "eligibility_status",
        "eligibility_reason",
        "overall_signal",
        "summary_payload",
        "source_as_of",
        "generated_at",
    ):
        setattr(snapshot, field, getattr(report, field))
    snapshot.lifecycle_status = "draft"
    snapshot.artifact_status = "pending"
    snapshot.source_content_sha256 = content_sha256
    _replace_metrics(snapshot, report)


def ingest_weekly_kpi_snapshots(
    session: Session,
    *,
    batch: WeeklyKpiSnapshotBatchIngest,
    idempotency_key: str,
) -> dict[str, Any]:
    payload = batch.model_dump(mode="json")
    payload_sha256 = _sha256(payload)
    previous_request = (
        session.query(WeeklyKpiIngestRequest)
        .filter(WeeklyKpiIngestRequest.idempotency_key == idempotency_key)
        .one_or_none()
    )
    if previous_request is not None:
        if previous_request.payload_sha256 != payload_sha256:
            raise WeeklyKpiIdempotencyConflictError(
                "idempotency key already used for a different weekly KPI payload"
            )
        return {**previous_request.result_payload, "replayed": True}

    result: dict[str, Any] = {
        "contract_version": batch.contract_version,
        "inserted": 0,
        "updated": 0,
        "noop": 0,
        "quarantined": 0,
        "replayed": False,
    }
    for report in batch.reports:
        content_sha256 = _report_hash(report)
        snapshot = (
            session.query(WeeklyKpiReportSnapshot)
            .filter(
                WeeklyKpiReportSnapshot.report_key == report.report_key,
                WeeklyKpiReportSnapshot.revision == report.revision,
            )
            .one_or_none()
        )
        if snapshot is not None and snapshot.source_content_sha256 == content_sha256:
            result["noop"] += 1
            if report.eligibility_status == "quarantine":
                result["quarantined"] += 1
            continue
        if snapshot is not None and snapshot.lifecycle_status != "draft":
            result["quarantined"] += 1
            continue
        if snapshot is None:
            snapshot = WeeklyKpiReportSnapshot(
                report_key=report.report_key,
                revision=report.revision,
            )
            session.add(snapshot)
            result["inserted"] += 1
        else:
            result["updated"] += 1
        _apply_report(snapshot, report, content_sha256=content_sha256)
        if report.eligibility_status == "quarantine":
            result["quarantined"] += 1

    session.flush()
    session.add(
        WeeklyKpiIngestRequest(
            idempotency_key=idempotency_key,
            payload_sha256=payload_sha256,
            result_payload=result,
        )
    )
    return result
