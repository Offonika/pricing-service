from __future__ import annotations

from collections.abc import Generator

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.api.dependencies import get_db, require_orchestration_internal_token
from app.main import app
from app.models import Base
from app.models.orchestration import (
    OrchestrationDeliveryAttempt,
    OrchestrationDeliveryIntent,
    OrchestrationJobRun,
)


def _run_payload(*, input_sha256: str = "a" * 64) -> dict[str, object]:
    return {
        "job_id": "weekly-manager-sales",
        "run_key": "2026-07-16T09:00:00+03:00",
        "scheduled_for": "2026-07-16T09:00:00+03:00",
        "input_sha256": input_sha256,
        "mode": "shadow",
        "worker_id": "test-worker",
        "lease_seconds": 300,
    }


def _delivery_payload(run_id: str) -> dict[str, object]:
    return {
        "run_id": run_id,
        "job_id": "weekly-manager-sales",
        "channel": "telegram",
        "dedupe_key": "weekly-manager-sales:2026-07-16:chat-a",
        "target_fingerprint": "b" * 64,
        "payload_sha256": "c" * 64,
        "artifact_refs": ["weekly-manager-sales/2026-07-16.xlsx"],
        "worker_id": "test-worker",
        "lease_seconds": 300,
        "max_attempts": 3,
    }


def test_orchestration_run_and_delivery_are_durable_and_idempotent(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'orchestration.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def override_db() -> Generator[Session, None, None]:
        with factory() as session:
            yield session

    app.dependency_overrides = {
        get_db: override_db,
        require_orchestration_internal_token: lambda: "test-token",
    }
    try:
        client = TestClient(app)
        run_headers = {"Idempotency-Key": "run-claim-20260716-001"}
        claimed = client.post(
            "/api/management/internal/orchestration/runs/claim",
            headers=run_headers,
            json=_run_payload(),
        )
        assert claimed.status_code == 200
        assert claimed.json()["should_execute"] is True
        assert claimed.json()["status"] == "claimed"
        run_id = claimed.json()["run_id"]

        replayed_claim = client.post(
            "/api/management/internal/orchestration/runs/claim",
            headers=run_headers,
            json=_run_payload(),
        )
        assert replayed_claim.status_code == 200
        assert replayed_claim.json()["idempotency_replayed"] is True
        assert replayed_claim.json()["should_execute"] is False

        resource_replay = client.post(
            "/api/management/internal/orchestration/runs/claim",
            headers={"Idempotency-Key": "run-claim-20260716-002"},
            json=_run_payload(),
        )
        assert resource_replay.status_code == 200
        assert resource_replay.json()["resource_replayed"] is True
        assert resource_replay.json()["should_execute"] is False

        conflicting_key = client.post(
            "/api/management/internal/orchestration/runs/claim",
            headers=run_headers,
            json=_run_payload(input_sha256="d" * 64),
        )
        assert conflicting_key.status_code == 409

        heartbeat = client.post(
            f"/api/management/internal/orchestration/runs/{run_id}/heartbeat",
            headers={"Idempotency-Key": "run-heartbeat-20260716-001"},
            json={"worker_id": "test-worker", "lease_seconds": 300},
        )
        assert heartbeat.status_code == 200
        assert heartbeat.json()["status"] == "running"

        delivery_headers = {"Idempotency-Key": "delivery-claim-20260716-001"}
        delivery = client.post(
            "/api/management/internal/orchestration/delivery-intents/claim",
            headers=delivery_headers,
            json=_delivery_payload(run_id),
        )
        assert delivery.status_code == 200
        assert delivery.json()["should_send"] is True
        assert delivery.json()["attempts_count"] == 1
        intent_id = delivery.json()["intent_id"]

        delivery_replay = client.post(
            "/api/management/internal/orchestration/delivery-intents/claim",
            headers=delivery_headers,
            json=_delivery_payload(run_id),
        )
        assert delivery_replay.status_code == 200
        assert delivery_replay.json()["should_send"] is False
        assert delivery_replay.json()["idempotency_replayed"] is True

        unknown = client.post(
            f"/api/management/internal/orchestration/delivery-intents/{intent_id}/finish",
            headers={"Idempotency-Key": "delivery-finish-20260716-001"},
            json={
                "worker_id": "test-worker",
                "outcome": "unknown",
                "error_code": "transport_timeout",
                "error_detail": "timeout after request; response was not observed",
            },
        )
        assert unknown.status_code == 200
        assert unknown.json()["manual_review_required"] is True

        no_automatic_resend = client.post(
            "/api/management/internal/orchestration/delivery-intents/claim",
            headers={"Idempotency-Key": "delivery-claim-20260716-002"},
            json=_delivery_payload(run_id),
        )
        assert no_automatic_resend.status_code == 200
        assert no_automatic_resend.json()["status"] == "unknown"
        assert no_automatic_resend.json()["should_send"] is False

        finished = client.post(
            f"/api/management/internal/orchestration/runs/{run_id}/finish",
            headers={"Idempotency-Key": "run-finish-20260716-001"},
            json={
                "worker_id": "test-worker",
                "status": "partial",
                "result": {"delivery_status": "unknown"},
                "error_code": "delivery_manual_review",
                "error_detail": "delivery requires manual review",
            },
        )
        assert finished.status_code == 200
        assert finished.json()["status"] == "partial"

        health = client.get("/api/management/internal/orchestration/health")
        assert health.status_code == 200
        assert health.json()["status"] == "degraded"
        assert health.json()["manual_review_count"] == 1

        with factory() as session:
            assert len(session.scalars(select(OrchestrationJobRun)).all()) == 1
            intents = session.scalars(select(OrchestrationDeliveryIntent)).all()
            attempts = session.scalars(select(OrchestrationDeliveryAttempt)).all()
            assert len(intents) == 1
            assert intents[0].status == "unknown"
            assert len(attempts) == 1
            assert attempts[0].outcome == "unknown"
    finally:
        app.dependency_overrides = {}
        engine.dispose()


def test_orchestration_uses_a_dedicated_token(monkeypatch) -> None:
    monkeypatch.setenv("MANAGEMENT_INTERNAL_API_TOKEN", "legacy-management-token")
    monkeypatch.delenv("ORCHESTRATION_INTERNAL_API_TOKEN", raising=False)

    from app.core.config import get_settings

    get_settings.cache_clear()
    try:
        response = TestClient(app).get(
            "/api/management/internal/orchestration/health",
            headers={"Authorization": "Bearer legacy-management-token"},
        )
        assert response.status_code == 401
        assert response.json()["detail"] == "orchestration internal token not configured"
    finally:
        get_settings.cache_clear()
