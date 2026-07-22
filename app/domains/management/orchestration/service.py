"""Application service for durable job and delivery orchestration."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any, Callable
from uuid import UUID, uuid4

from .contracts import OrchestrationRepository
from .entities import ApiRequest, DeliveryAttempt, DeliveryIntent, JobRun

FINAL_RUN_STATUSES = {"succeeded", "partial", "failed", "skipped", "blocked"}
FINAL_DELIVERY_STATUSES = {"delivered", "unknown", "manual_review", "cancelled"}


class OrchestrationConflictError(RuntimeError):
    """A durable key was reused for an incompatible command."""


class OrchestrationNotFoundError(RuntimeError):
    """A referenced run or delivery intent does not exist."""


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _as_utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _sha256(payload: Any) -> str:
    rendered = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _clean_error_detail(value: str | None) -> str | None:
    if value is None:
        return None
    return " ".join(value.split())[:1000]


class OrchestrationService:
    def __init__(
        self,
        repository: OrchestrationRepository,
        *,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.repository = repository
        self.clock = clock

    def _idempotent(
        self,
        *,
        operation: str,
        idempotency_key: str,
        request_payload: dict[str, Any],
        command: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        request_sha256 = _sha256(request_payload)
        previous = self.repository.get_api_request(idempotency_key)
        if previous is not None:
            if previous.operation != operation or previous.request_sha256 != request_sha256:
                raise OrchestrationConflictError(
                    "idempotency key already used for a different orchestration command"
                )
            response = {**previous.response_payload, "idempotency_replayed": True}
            # A replayed claim is an acknowledgement, never permission to execute
            # the job or external delivery for a second time.
            if "should_execute" in response:
                response["should_execute"] = False
                response["resource_replayed"] = True
            if "should_send" in response:
                response["should_send"] = False
            return response

        response = {**command(), "idempotency_replayed": False}
        self.repository.add_api_request(
            ApiRequest(
                idempotency_key=idempotency_key,
                operation=operation,
                request_sha256=request_sha256,
                response_payload=response,
            )
        )
        return response

    def claim_run(
        self,
        *,
        idempotency_key: str,
        job_id: str,
        run_key: str,
        scheduled_for: datetime,
        input_sha256: str,
        mode: str,
        worker_id: str,
        lease_seconds: int,
    ) -> dict[str, Any]:
        scheduled_for = _as_utc_naive(scheduled_for)
        payload = {
            "job_id": job_id,
            "run_key": run_key,
            "scheduled_for": scheduled_for.isoformat(),
            "input_sha256": input_sha256,
            "mode": mode,
            "worker_id": worker_id,
            "lease_seconds": lease_seconds,
        }

        def command() -> dict[str, Any]:
            previous = self.repository.get_run(job_id, run_key)
            if previous is not None:
                if (
                    previous.input_sha256 != input_sha256
                    or previous.mode != mode
                    or previous.scheduled_for != scheduled_for
                ):
                    raise OrchestrationConflictError(
                        "run key already exists with different input, mode or schedule"
                    )
                return self._run_response(
                    previous,
                    should_execute=False,
                    resource_replayed=True,
                )

            now = self.clock()
            run = JobRun(
                id=uuid4(),
                job_id=job_id,
                run_key=run_key,
                scheduled_for=scheduled_for,
                input_sha256=input_sha256,
                mode=mode,
                status="claimed",
                worker_id=worker_id,
                claimed_at=now,
                heartbeat_at=now,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
            )
            self.repository.add_run(run)
            return self._run_response(run, should_execute=True, resource_replayed=False)

        return self._idempotent(
            operation="run.claim",
            idempotency_key=idempotency_key,
            request_payload=payload,
            command=command,
        )

    def heartbeat_run(
        self,
        *,
        idempotency_key: str,
        run_id: UUID,
        worker_id: str,
        lease_seconds: int,
    ) -> dict[str, Any]:
        payload = {
            "run_id": str(run_id),
            "worker_id": worker_id,
            "lease_seconds": lease_seconds,
        }

        def command() -> dict[str, Any]:
            run = self._require_run(run_id)
            now = self.clock()
            if run.status not in {"claimed", "running"}:
                raise OrchestrationConflictError("only an active run can be heartbeated")
            if run.worker_id != worker_id:
                raise OrchestrationConflictError("run is claimed by another worker")
            if run.lease_expires_at < now:
                raise OrchestrationConflictError("run lease expired")
            run.status = "running"
            run.heartbeat_at = now
            run.lease_expires_at = now + timedelta(seconds=lease_seconds)
            self.repository.save_run(run)
            return self._run_response(run, should_execute=True, resource_replayed=False)

        return self._idempotent(
            operation="run.heartbeat",
            idempotency_key=idempotency_key,
            request_payload=payload,
            command=command,
        )

    def finish_run(
        self,
        *,
        idempotency_key: str,
        run_id: UUID,
        worker_id: str,
        status: str,
        result: dict[str, Any],
        error_code: str | None,
        error_detail: str | None,
    ) -> dict[str, Any]:
        payload = {
            "run_id": str(run_id),
            "worker_id": worker_id,
            "status": status,
            "result": result,
            "error_code": error_code,
            "error_detail": _clean_error_detail(error_detail),
        }

        def command() -> dict[str, Any]:
            run = self._require_run(run_id)
            result_sha256 = _sha256(payload)
            if run.status in FINAL_RUN_STATUSES:
                if run.result_sha256 != result_sha256:
                    raise OrchestrationConflictError("run already finished with a different result")
                return self._run_response(
                    run,
                    should_execute=False,
                    resource_replayed=True,
                )
            if run.worker_id != worker_id:
                raise OrchestrationConflictError("run is claimed by another worker")
            now = self.clock()
            run.status = status
            run.completed_at = now
            run.heartbeat_at = now
            run.result_payload = result
            run.result_sha256 = result_sha256
            run.error_code = error_code
            run.error_detail = _clean_error_detail(error_detail)
            self.repository.save_run(run)
            return self._run_response(run, should_execute=False, resource_replayed=False)

        return self._idempotent(
            operation="run.finish",
            idempotency_key=idempotency_key,
            request_payload=payload,
            command=command,
        )

    def claim_delivery(
        self,
        *,
        idempotency_key: str,
        run_id: UUID,
        job_id: str,
        channel: str,
        dedupe_key: str,
        target_fingerprint: str,
        payload_sha256: str,
        artifact_refs: list[str],
        worker_id: str,
        lease_seconds: int,
        max_attempts: int,
    ) -> dict[str, Any]:
        payload = {
            "run_id": str(run_id),
            "job_id": job_id,
            "channel": channel,
            "dedupe_key": dedupe_key,
            "target_fingerprint": target_fingerprint,
            "payload_sha256": payload_sha256,
            "artifact_refs": artifact_refs,
            "worker_id": worker_id,
            "lease_seconds": lease_seconds,
            "max_attempts": max_attempts,
        }

        def command() -> dict[str, Any]:
            run = self._require_run(run_id)
            if run.job_id != job_id:
                raise OrchestrationConflictError("delivery job_id does not match its run")
            intent = self.repository.get_delivery_intent(job_id, channel, dedupe_key)
            if intent is not None:
                if (
                    intent.run_id != run_id
                    or intent.target_fingerprint != target_fingerprint
                    or intent.payload_sha256 != payload_sha256
                    or intent.artifact_refs != artifact_refs
                ):
                    raise OrchestrationConflictError(
                        "delivery dedupe key already exists with different content"
                    )
                if intent.status in FINAL_DELIVERY_STATUSES:
                    return self._delivery_response(intent, should_send=False)
                now = self.clock()
                if intent.status == "sending":
                    if intent.lease_expires_at is not None and intent.lease_expires_at < now:
                        intent.status = "unknown"
                        intent.completed_at = now
                        intent.last_error_code = "delivery_lease_expired"
                        intent.last_error_detail = (
                            "delivery outcome is unknown; automatic resend is disabled"
                        )
                        attempt = self.repository.get_open_delivery_attempt(intent.id)
                        if attempt is not None:
                            attempt.completed_at = now
                            attempt.outcome = "unknown"
                            attempt.error_code = intent.last_error_code
                            attempt.error_detail = intent.last_error_detail
                            self.repository.save_delivery_attempt(attempt)
                        self.repository.save_delivery_intent(intent)
                    return self._delivery_response(intent, should_send=False)
                if intent.attempts_count >= max_attempts:
                    intent.status = "manual_review"
                    intent.completed_at = now
                    intent.last_error_code = "delivery_attempt_limit_reached"
                    self.repository.save_delivery_intent(intent)
                    return self._delivery_response(intent, should_send=False)
            else:
                intent = DeliveryIntent(
                    id=uuid4(),
                    run_id=run_id,
                    job_id=job_id,
                    channel=channel,
                    dedupe_key=dedupe_key,
                    target_fingerprint=target_fingerprint,
                    payload_sha256=payload_sha256,
                    artifact_refs=artifact_refs,
                    status="pending",
                )
                self.repository.add_delivery_intent(intent)

            now = self.clock()
            intent.status = "sending"
            intent.attempts_count += 1
            intent.claimed_by = worker_id
            intent.claimed_at = now
            intent.lease_expires_at = now + timedelta(seconds=lease_seconds)
            intent.completed_at = None
            self.repository.save_delivery_intent(intent)
            self.repository.add_delivery_attempt(
                DeliveryAttempt(
                    id=uuid4(),
                    intent_id=intent.id,
                    attempt_number=intent.attempts_count,
                    worker_id=worker_id,
                    started_at=now,
                )
            )
            return self._delivery_response(intent, should_send=True)

        return self._idempotent(
            operation="delivery.claim",
            idempotency_key=idempotency_key,
            request_payload=payload,
            command=command,
        )

    def finish_delivery(
        self,
        *,
        idempotency_key: str,
        intent_id: UUID,
        worker_id: str,
        outcome: str,
        external_ref: str | None,
        error_code: str | None,
        error_detail: str | None,
    ) -> dict[str, Any]:
        payload = {
            "intent_id": str(intent_id),
            "worker_id": worker_id,
            "outcome": outcome,
            "external_ref": external_ref,
            "error_code": error_code,
            "error_detail": _clean_error_detail(error_detail),
        }

        def command() -> dict[str, Any]:
            intent = self._require_delivery_intent(intent_id)
            result_sha256 = _sha256(payload)
            if intent.status in FINAL_DELIVERY_STATUSES or intent.status == "failed":
                if intent.result_sha256 != result_sha256:
                    raise OrchestrationConflictError(
                        "delivery already finished with a different result"
                    )
                return self._delivery_response(intent, should_send=False)
            if intent.status != "sending":
                raise OrchestrationConflictError("delivery intent is not sending")
            if intent.claimed_by != worker_id:
                raise OrchestrationConflictError("delivery is claimed by another worker")

            now = self.clock()
            intent.status = outcome
            intent.completed_at = now
            intent.external_ref = external_ref
            intent.result_sha256 = result_sha256
            intent.last_error_code = error_code
            intent.last_error_detail = _clean_error_detail(error_detail)
            self.repository.save_delivery_intent(intent)

            attempt = self.repository.get_open_delivery_attempt(intent.id)
            if attempt is not None:
                attempt.completed_at = now
                attempt.outcome = outcome
                attempt.external_ref = external_ref
                attempt.error_code = error_code
                attempt.error_detail = _clean_error_detail(error_detail)
                self.repository.save_delivery_attempt(attempt)
            return self._delivery_response(intent, should_send=False)

        return self._idempotent(
            operation="delivery.finish",
            idempotency_key=idempotency_key,
            request_payload=payload,
            command=command,
        )

    def health(self) -> dict[str, Any]:
        health = self.repository.get_health(self.clock())
        manual_review_count = sum(
            health.delivery_status_counts.get(status, 0) for status in ("unknown", "manual_review")
        )
        status = "degraded" if health.stale_run_count > 0 or manual_review_count > 0 else "ready"
        return {
            "status": status,
            "checked_at": self.clock().isoformat(),
            "run_status_counts": health.run_status_counts,
            "delivery_status_counts": health.delivery_status_counts,
            "stale_run_count": health.stale_run_count,
            "manual_review_count": manual_review_count,
        }

    def _require_run(self, run_id: UUID) -> JobRun:
        run = self.repository.get_run_by_id(run_id)
        if run is None:
            raise OrchestrationNotFoundError("orchestration run not found")
        return run

    def _require_delivery_intent(self, intent_id: UUID) -> DeliveryIntent:
        intent = self.repository.get_delivery_intent_by_id(intent_id)
        if intent is None:
            raise OrchestrationNotFoundError("delivery intent not found")
        return intent

    @staticmethod
    def _run_response(
        run: JobRun,
        *,
        should_execute: bool,
        resource_replayed: bool,
    ) -> dict[str, Any]:
        return {
            "run_id": str(run.id),
            "job_id": run.job_id,
            "run_key": run.run_key,
            "status": run.status,
            "should_execute": should_execute,
            "resource_replayed": resource_replayed,
            "lease_expires_at": run.lease_expires_at.isoformat(),
            "completed_at": (
                run.completed_at.isoformat() if run.completed_at is not None else None
            ),
        }

    @staticmethod
    def _delivery_response(
        intent: DeliveryIntent,
        *,
        should_send: bool,
    ) -> dict[str, Any]:
        return {
            "intent_id": str(intent.id),
            "run_id": str(intent.run_id),
            "job_id": intent.job_id,
            "channel": intent.channel,
            "dedupe_key": intent.dedupe_key,
            "status": intent.status,
            "should_send": should_send,
            "attempts_count": intent.attempts_count,
            "manual_review_required": intent.status in {"unknown", "manual_review"},
            "external_ref": intent.external_ref,
        }
