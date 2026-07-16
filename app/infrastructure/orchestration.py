"""SQLAlchemy adapter for durable management orchestration."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domains.management.orchestration.entities import (
    ApiRequest,
    DeliveryAttempt,
    DeliveryIntent,
    JobRun,
    OrchestrationHealth,
)
from app.models.orchestration import (
    OrchestrationApiRequest,
    OrchestrationDeliveryAttempt,
    OrchestrationDeliveryIntent,
    OrchestrationJobRun,
)


class SqlAlchemyOrchestrationRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_api_request(self, idempotency_key: str) -> ApiRequest | None:
        row = self.session.scalar(
            select(OrchestrationApiRequest).where(
                OrchestrationApiRequest.idempotency_key == idempotency_key
            )
        )
        if row is None:
            return None
        return ApiRequest(
            idempotency_key=row.idempotency_key,
            operation=row.operation,
            request_sha256=row.request_sha256,
            response_payload=row.response_payload,
        )

    def add_api_request(self, request: ApiRequest) -> None:
        self.session.add(
            OrchestrationApiRequest(
                idempotency_key=request.idempotency_key,
                operation=request.operation,
                request_sha256=request.request_sha256,
                response_payload=request.response_payload,
            )
        )

    def get_run(self, job_id: str, run_key: str) -> JobRun | None:
        row = self.session.scalar(
            select(OrchestrationJobRun).where(
                OrchestrationJobRun.job_id == job_id,
                OrchestrationJobRun.run_key == run_key,
            )
        )
        return self._run_entity(row) if row is not None else None

    def get_run_by_id(self, run_id: UUID) -> JobRun | None:
        row = self.session.get(OrchestrationJobRun, run_id)
        return self._run_entity(row) if row is not None else None

    def add_run(self, run: JobRun) -> None:
        self.session.add(self._run_model(run))

    def save_run(self, run: JobRun) -> None:
        row = self.session.get(OrchestrationJobRun, run.id)
        if row is None:
            self.session.add(self._run_model(run))
            return
        for field in (
            "status",
            "worker_id",
            "heartbeat_at",
            "lease_expires_at",
            "completed_at",
            "result_payload",
            "result_sha256",
            "error_code",
            "error_detail",
        ):
            setattr(row, field, getattr(run, field))

    def get_delivery_intent(
        self,
        job_id: str,
        channel: str,
        dedupe_key: str,
    ) -> DeliveryIntent | None:
        row = self.session.scalar(
            select(OrchestrationDeliveryIntent).where(
                OrchestrationDeliveryIntent.job_id == job_id,
                OrchestrationDeliveryIntent.channel == channel,
                OrchestrationDeliveryIntent.dedupe_key == dedupe_key,
            )
        )
        return self._intent_entity(row) if row is not None else None

    def get_delivery_intent_by_id(self, intent_id: UUID) -> DeliveryIntent | None:
        row = self.session.get(OrchestrationDeliveryIntent, intent_id)
        return self._intent_entity(row) if row is not None else None

    def add_delivery_intent(self, intent: DeliveryIntent) -> None:
        self.session.add(self._intent_model(intent))

    def save_delivery_intent(self, intent: DeliveryIntent) -> None:
        row = self.session.get(OrchestrationDeliveryIntent, intent.id)
        if row is None:
            self.session.add(self._intent_model(intent))
            return
        for field in (
            "status",
            "attempts_count",
            "claimed_by",
            "claimed_at",
            "lease_expires_at",
            "completed_at",
            "external_ref",
            "result_sha256",
            "last_error_code",
            "last_error_detail",
        ):
            setattr(row, field, getattr(intent, field))

    def add_delivery_attempt(self, attempt: DeliveryAttempt) -> None:
        self.session.add(
            OrchestrationDeliveryAttempt(
                id=attempt.id,
                intent_id=attempt.intent_id,
                attempt_number=attempt.attempt_number,
                worker_id=attempt.worker_id,
                started_at=attempt.started_at,
                completed_at=attempt.completed_at,
                outcome=attempt.outcome,
                external_ref=attempt.external_ref,
                error_code=attempt.error_code,
                error_detail=attempt.error_detail,
            )
        )

    def get_open_delivery_attempt(self, intent_id: UUID) -> DeliveryAttempt | None:
        row = self.session.scalar(
            select(OrchestrationDeliveryAttempt)
            .where(
                OrchestrationDeliveryAttempt.intent_id == intent_id,
                OrchestrationDeliveryAttempt.completed_at.is_(None),
            )
            .order_by(OrchestrationDeliveryAttempt.attempt_number.desc())
        )
        if row is None:
            return None
        return DeliveryAttempt(
            id=row.id,
            intent_id=row.intent_id,
            attempt_number=row.attempt_number,
            worker_id=row.worker_id,
            started_at=row.started_at,
            completed_at=row.completed_at,
            outcome=row.outcome,
            external_ref=row.external_ref,
            error_code=row.error_code,
            error_detail=row.error_detail,
        )

    def save_delivery_attempt(self, attempt: DeliveryAttempt) -> None:
        row = self.session.get(OrchestrationDeliveryAttempt, attempt.id)
        if row is None:
            self.add_delivery_attempt(attempt)
            return
        row.completed_at = attempt.completed_at
        row.outcome = attempt.outcome
        row.external_ref = attempt.external_ref
        row.error_code = attempt.error_code
        row.error_detail = attempt.error_detail

    def get_health(self, now: datetime) -> OrchestrationHealth:
        run_counts = Counter(
            dict(
                self.session.execute(
                    select(OrchestrationJobRun.status, func.count()).group_by(
                        OrchestrationJobRun.status
                    )
                ).all()
            )
        )
        delivery_counts = Counter(
            dict(
                self.session.execute(
                    select(OrchestrationDeliveryIntent.status, func.count()).group_by(
                        OrchestrationDeliveryIntent.status
                    )
                ).all()
            )
        )
        stale_run_count = int(
            self.session.scalar(
                select(func.count())
                .select_from(OrchestrationJobRun)
                .where(
                    OrchestrationJobRun.status.in_(("claimed", "running")),
                    OrchestrationJobRun.lease_expires_at < now,
                )
            )
            or 0
        )
        return OrchestrationHealth(
            run_status_counts=dict(run_counts),
            delivery_status_counts=dict(delivery_counts),
            stale_run_count=stale_run_count,
        )

    @staticmethod
    def _run_model(run: JobRun) -> OrchestrationJobRun:
        return OrchestrationJobRun(
            id=run.id,
            job_id=run.job_id,
            run_key=run.run_key,
            scheduled_for=run.scheduled_for,
            input_sha256=run.input_sha256,
            mode=run.mode,
            status=run.status,
            worker_id=run.worker_id,
            claimed_at=run.claimed_at,
            heartbeat_at=run.heartbeat_at,
            lease_expires_at=run.lease_expires_at,
            completed_at=run.completed_at,
            result_payload=run.result_payload,
            result_sha256=run.result_sha256,
            error_code=run.error_code,
            error_detail=run.error_detail,
        )

    @staticmethod
    def _run_entity(row: OrchestrationJobRun) -> JobRun:
        return JobRun(
            id=row.id,
            job_id=row.job_id,
            run_key=row.run_key,
            scheduled_for=row.scheduled_for,
            input_sha256=row.input_sha256,
            mode=row.mode,
            status=row.status,
            worker_id=row.worker_id,
            claimed_at=row.claimed_at,
            heartbeat_at=row.heartbeat_at,
            lease_expires_at=row.lease_expires_at,
            completed_at=row.completed_at,
            result_payload=row.result_payload,
            result_sha256=row.result_sha256,
            error_code=row.error_code,
            error_detail=row.error_detail,
        )

    @staticmethod
    def _intent_model(intent: DeliveryIntent) -> OrchestrationDeliveryIntent:
        return OrchestrationDeliveryIntent(
            id=intent.id,
            run_id=intent.run_id,
            job_id=intent.job_id,
            channel=intent.channel,
            dedupe_key=intent.dedupe_key,
            target_fingerprint=intent.target_fingerprint,
            payload_sha256=intent.payload_sha256,
            artifact_refs=intent.artifact_refs,
            status=intent.status,
            attempts_count=intent.attempts_count,
            claimed_by=intent.claimed_by,
            claimed_at=intent.claimed_at,
            lease_expires_at=intent.lease_expires_at,
            completed_at=intent.completed_at,
            external_ref=intent.external_ref,
            result_sha256=intent.result_sha256,
            last_error_code=intent.last_error_code,
            last_error_detail=intent.last_error_detail,
        )

    @staticmethod
    def _intent_entity(row: OrchestrationDeliveryIntent) -> DeliveryIntent:
        return DeliveryIntent(
            id=row.id,
            run_id=row.run_id,
            job_id=row.job_id,
            channel=row.channel,
            dedupe_key=row.dedupe_key,
            target_fingerprint=row.target_fingerprint,
            payload_sha256=row.payload_sha256,
            artifact_refs=list(row.artifact_refs),
            status=row.status,
            attempts_count=row.attempts_count,
            claimed_by=row.claimed_by,
            claimed_at=row.claimed_at,
            lease_expires_at=row.lease_expires_at,
            completed_at=row.completed_at,
            external_ref=row.external_ref,
            result_sha256=row.result_sha256,
            last_error_code=row.last_error_code,
            last_error_detail=row.last_error_detail,
        )
