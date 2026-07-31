from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.services.competitor_matching_replay import (
    build_auto_accept_audit_sample,
    evaluate_snapshot_decisions,
)


def _decision(
    index: int,
    *,
    accepted: bool,
    created_at: datetime | None = None,
    reason_code: str = "confirmed_attributes",
):
    return SimpleNamespace(
        action="accept" if accepted else "reject",
        reason_code=reason_code,
        created_at=created_at,
        snapshot_json={
            "features": {"competitor_item": {"item_type": "camera"}},
            "scores": {"final": 0.9 if accepted else 0.55, "embed_gap": 0.1},
            "guardrail": {"allowed": True},
            "top_k": {"candidates": [{"product_id": index}]},
            "rationale": {},
        },
    )


def test_replay_uses_chronological_holdout_and_does_not_train_on_future(monkeypatch):
    started = datetime(2026, 1, 1, tzinfo=UTC)
    rows = [
        _decision(index, accepted=index % 2 == 0, created_at=started + timedelta(days=index))
        for index in range(10)
    ]
    fitted_labels: list[list[int]] = []

    def capture_fit(train_rows):
        fitted_labels.append([row[3] for row in train_rows])
        from numpy import zeros

        return zeros(6)

    monkeypatch.setattr("app.services.competitor_matching_replay._fit_logistic", capture_fit)
    report = evaluate_snapshot_decisions(rows, minimum_examples=1)

    category = report["categories"]["camera"]
    assert report["split"] == "chronological_80_20"
    assert report["feature_version"] == "competitor_reranker_v1"
    assert category["train_examples"] == 8
    assert category["validation_examples"] == 2
    assert fitted_labels == [[1, 0, 1, 0, 1, 0, 1, 0]]


def test_replay_sorts_missing_timestamps_after_dated_examples():
    dated = _decision(1, accepted=True, created_at=datetime(2026, 1, 1, tzinfo=UTC))
    undated = _decision(2, accepted=False, created_at=None)

    report = evaluate_snapshot_decisions([undated, dated], minimum_examples=1)

    assert report["categories"]["camera"]["train_examples"] == 1
    assert report["categories"]["camera"]["validation_examples"] == 1


def test_replay_reports_legacy_hard_negatives_and_reason_coverage():
    legacy_reject = SimpleNamespace(
        action="reject",
        reason_code="legacy_unspecified",
        created_at=datetime(2025, 1, 1, tzinfo=UTC),
        snapshot_json=None,
    )
    structured_accept = _decision(
        1,
        accepted=True,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    report = evaluate_snapshot_decisions([legacy_reject, structured_accept], minimum_examples=1)

    assert report["historical_examples_without_snapshot"] == 1
    assert report["historical_hard_negatives"] == 1
    assert report["structured_reason_coverage"]["accept"]["rate"] == 1.0
    assert report["structured_reason_coverage"]["reject"]["rate"] == 0.0


def test_replay_recommends_rollback_for_systematic_auto_conflict():
    decision = _decision(
        1,
        accepted=False,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        reason_code="wrong_model",
    )
    decision.snapshot_json["rationale"]["auto_accept_policy"] = {
        "version": 1,
        "category": "camera",
    }

    report = evaluate_snapshot_decisions([decision], minimum_examples=1)

    audit = report["auto_accept_audit"]["camera"]
    assert audit["systematic_conflicts"] == 1
    assert audit["disable_recommended"] is True


def test_auto_accept_audit_sample_is_deterministic():
    match = SimpleNamespace(
        id=10,
        competitor_item_id=20,
        product_id=30,
        final_score=0.94,
        rationale_json={"auto_accept_policy": {"version": 2, "category": "camera"}},
        competitor_item=SimpleNamespace(competitor="moba"),
        product=SimpleNamespace(article="P-30"),
    )

    assert build_auto_accept_audit_sample([match], sample_rate=1.0) == [
        {
            "match_id": 10,
            "policy_version": 2,
            "category": "camera",
            "competitor": "moba",
            "competitor_item_id": 20,
            "product_id": 30,
            "product_article": "P-30",
            "score": 0.94,
        }
    ]
