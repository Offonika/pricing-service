"""Ports owned by the orchestration application layer."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from .entities import (
    ApiRequest,
    DeliveryAttempt,
    DeliveryIntent,
    JobRun,
    OrchestrationHealth,
)


class OrchestrationRepository(Protocol):
    def get_api_request(self, idempotency_key: str) -> ApiRequest | None: ...

    def add_api_request(self, request: ApiRequest) -> None: ...

    def get_run(self, job_id: str, run_key: str) -> JobRun | None: ...

    def get_run_by_id(self, run_id: UUID) -> JobRun | None: ...

    def add_run(self, run: JobRun) -> None: ...

    def save_run(self, run: JobRun) -> None: ...

    def get_delivery_intent(
        self,
        job_id: str,
        channel: str,
        dedupe_key: str,
    ) -> DeliveryIntent | None: ...

    def get_delivery_intent_by_id(self, intent_id: UUID) -> DeliveryIntent | None: ...

    def add_delivery_intent(self, intent: DeliveryIntent) -> None: ...

    def save_delivery_intent(self, intent: DeliveryIntent) -> None: ...

    def add_delivery_attempt(self, attempt: DeliveryAttempt) -> None: ...

    def get_open_delivery_attempt(self, intent_id: UUID) -> DeliveryAttempt | None: ...

    def save_delivery_attempt(self, attempt: DeliveryAttempt) -> None: ...

    def get_health(self, now: datetime) -> OrchestrationHealth: ...
