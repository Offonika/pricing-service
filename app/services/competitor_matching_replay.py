from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Iterable

import numpy as np

FEATURE_NAMES = (
    "bias",
    "final_score",
    "embed_gap",
    "guardrail_allowed",
    "exact_evidence",
    "has_top_k",
)


def evaluate_snapshot_decisions(
    decisions: Iterable[Any],
    *,
    target_precision: float = 0.95,
    minimum_examples: int = 50,
    audit_sample_rate: float = 0.10,
    rollback_error_rate: float = 0.05,
) -> dict[str, Any]:
    decisions = list(decisions)
    grouped: dict[str, list[tuple[Any, ...]]] = defaultdict(list)
    historical_examples = 0
    historical_hard_negatives = 0
    action_counts: dict[str, int] = defaultdict(int)
    structured_reason_counts: dict[str, int] = defaultdict(int)
    auto_audit: dict[str, dict[str, int]] = defaultdict(
        lambda: {"checked": 0, "errors": 0, "systematic_conflicts": 0}
    )
    for decision in decisions:
        action = str(getattr(decision, "action", "")).lower()
        reason_code = str(getattr(decision, "reason_code", "legacy_unspecified") or "")
        if action in {"accept", "reject", "revoke"}:
            action_counts[action] += 1
            if reason_code and reason_code != "legacy_unspecified":
                structured_reason_counts[action] += 1
        snapshot = getattr(decision, "snapshot_json", None)
        rationale = snapshot.get("rationale") if isinstance(snapshot, dict) else None
        policy_data = rationale.get("auto_accept_policy") if isinstance(rationale, dict) else None
        if isinstance(policy_data, dict):
            audit_category = str(policy_data.get("category") or "unknown")
            auto_audit[audit_category]["checked"] += 1
            if reason_code == "auto_false_positive" or action == "revoke":
                auto_audit[audit_category]["errors"] += 1
            if reason_code in {"wrong_model", "wrong_item_type"}:
                auto_audit[audit_category]["systematic_conflicts"] += 1
        row = _training_row(decision)
        if row is not None:
            grouped[row[0]].append(row)
        elif action in {"accept", "reject", "revoke"}:
            historical_examples += 1
            if action == "reject" or reason_code == "auto_false_positive":
                historical_hard_negatives += 1

    categories: dict[str, Any] = {}
    for category, rows in sorted(grouped.items()):
        rows.sort(key=lambda row: _chronological_key(row[1]))
        split = max(1, int(len(rows) * 0.8))
        train_rows = rows[:split]
        validation_rows = rows[split:]
        if not validation_rows and len(rows) > 1:
            train_rows = rows[:-1]
            validation_rows = rows[-1:]
        coefficients = _fit_logistic(train_rows)
        validation = _evaluate_thresholds(
            validation_rows,
            coefficients,
            target_precision=target_precision,
        )
        exact_labels = [row[3] for row in validation_rows if row[4]]
        categories[category] = {
            "examples": len(rows),
            "train_examples": len(train_rows),
            "validation_examples": len(validation_rows),
            "feature_names": list(FEATURE_NAMES),
            "coefficients": coefficients.tolist(),
            "exact_evidence_examples": len(exact_labels),
            "exact_evidence_precision": (
                round(sum(exact_labels) / len(exact_labels), 4) if exact_labels else None
            ),
            **validation,
            "promotable": len(rows) >= minimum_examples
            and validation["precision"] >= target_precision,
        }
    return {
        "schema_version": 2,
        "feature_version": "competitor_reranker_v1",
        "split": "chronological_80_20",
        "target_precision": target_precision,
        "minimum_examples": minimum_examples,
        "rollout_controls": {
            "audit_sample_rate": audit_sample_rate,
            "rollback_error_rate": rollback_error_rate,
        },
        "snapshot_examples": sum(len(rows) for rows in grouped.values()),
        "historical_examples_without_snapshot": historical_examples,
        "historical_hard_negatives": historical_hard_negatives,
        "structured_reason_coverage": {
            action: {
                "total": action_counts[action],
                "structured": structured_reason_counts[action],
                "rate": (
                    round(structured_reason_counts[action] / action_counts[action], 4)
                    if action_counts[action]
                    else 0.0
                ),
            }
            for action in ("accept", "reject", "revoke")
        },
        "auto_accept_audit": {
            category: {
                **counts,
                "error_rate": (
                    round(counts["errors"] / counts["checked"], 4) if counts["checked"] else 0.0
                ),
                "disable_recommended": (
                    counts["systematic_conflicts"] > 0
                    or (
                        counts["checked"] > 0
                        and counts["errors"] / counts["checked"] > rollback_error_rate
                    )
                ),
            }
            for category, counts in sorted(auto_audit.items())
        },
        "categories": categories,
    }


def build_auto_accept_audit_sample(
    matches: Iterable[Any],
    *,
    sample_rate: float = 0.10,
) -> list[dict[str, Any]]:
    if not 0 <= sample_rate <= 1:
        raise ValueError("sample_rate must be between 0 and 1")
    rows: list[dict[str, Any]] = []
    for match in matches:
        rationale = getattr(match, "rationale_json", None)
        policy_data = rationale.get("auto_accept_policy") if isinstance(rationale, dict) else None
        if not isinstance(policy_data, dict):
            continue
        match_id = int(getattr(match, "id", 0) or 0)
        version = int(policy_data.get("version") or 0)
        fraction = (
            int.from_bytes(
                sha256(f"{version}:{match_id}".encode()).digest()[:8],
                "big",
            )
            / 2**64
        )
        if fraction >= sample_rate:
            continue
        item = getattr(match, "competitor_item", None)
        product = getattr(match, "product", None)
        rows.append(
            {
                "match_id": match_id,
                "policy_version": version,
                "category": policy_data.get("category"),
                "competitor": getattr(item, "competitor", None),
                "competitor_item_id": getattr(match, "competitor_item_id", None),
                "product_id": getattr(match, "product_id", None),
                "product_article": getattr(product, "article", None),
                "score": _float(getattr(match, "final_score", None)),
            }
        )
    return sorted(rows, key=lambda row: (str(row["category"]), int(row["match_id"])))


def _training_row(decision: Any) -> tuple[str, Any, np.ndarray, int, bool] | None:
    snapshot = getattr(decision, "snapshot_json", None)
    action = str(getattr(decision, "action", "")).lower()
    if not isinstance(snapshot, dict) or action not in {"accept", "reject"}:
        return None
    features = snapshot.get("features") or {}
    item_features = features.get("competitor_item") or {}
    category = str(item_features.get("item_type") or "unknown").lower()
    scores = snapshot.get("scores") or {}
    guardrail = snapshot.get("guardrail") or {}
    rationale = snapshot.get("rationale") or {}
    exact_evidence = any(
        key in rationale
        for key in (
            "auto_accept_explicit_model_code_overlap",
            "auto_accept_battery_part_code",
            "auto_accept_battery_original_part_code",
        )
    )
    top_k = snapshot.get("top_k") or {}
    vector = np.array(
        [
            1.0,
            _float(scores.get("final")),
            _float(scores.get("embed_gap")),
            1.0 if guardrail.get("allowed") else 0.0,
            1.0 if exact_evidence else 0.0,
            1.0 if top_k.get("candidates") else 0.0,
        ],
        dtype=np.float64,
    )
    return (
        category,
        getattr(decision, "created_at", None),
        vector,
        1 if action == "accept" else 0,
        exact_evidence,
    )


def _fit_logistic(rows: list[tuple[Any, ...]]) -> np.ndarray:
    if not rows:
        return np.zeros(len(FEATURE_NAMES), dtype=np.float64)
    matrix = np.stack([row[2] for row in rows])
    labels = np.array([row[3] for row in rows], dtype=np.float64)
    coefficients = np.zeros(matrix.shape[1], dtype=np.float64)
    for _ in range(600):
        probabilities = _sigmoid(matrix @ coefficients)
        gradient = matrix.T @ (probabilities - labels) / len(labels)
        coefficients -= 0.15 * gradient
    return coefficients


def _evaluate_thresholds(
    rows: list[tuple[Any, ...]],
    coefficients: np.ndarray,
    *,
    target_precision: float,
) -> dict[str, Any]:
    if not rows:
        return {"threshold": 1.0, "precision": 0.0, "coverage": 0.0, "accepted": 0}
    matrix = np.stack([row[2] for row in rows])
    labels = np.array([row[3] for row in rows], dtype=np.int64)
    probabilities = _sigmoid(matrix @ coefficients)
    best = {"threshold": 1.0, "precision": 0.0, "coverage": 0.0, "accepted": 0}
    for threshold in np.arange(0.50, 1.0, 0.01):
        predicted = probabilities >= threshold
        accepted = int(predicted.sum())
        if not accepted:
            continue
        precision = float(labels[predicted].mean())
        candidate = {
            "threshold": round(float(threshold), 2),
            "precision": round(precision, 4),
            "coverage": round(accepted / len(rows), 4),
            "accepted": accepted,
        }
        if precision >= target_precision and accepted > best["accepted"]:
            best = candidate
    return best


def _sigmoid(value: np.ndarray) -> np.ndarray:
    clipped = np.clip(value, -30, 30)
    return 1.0 / (1.0 + np.exp(-clipped))


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _chronological_key(value: Any) -> tuple[int, str]:
    if isinstance(value, datetime):
        normalized = value if value.tzinfo else value.replace(tzinfo=UTC)
        return (0, normalized.astimezone(UTC).isoformat())
    if value is None:
        return (1, "")
    return (0, str(value))


__all__ = [
    "FEATURE_NAMES",
    "build_auto_accept_audit_sample",
    "evaluate_snapshot_decisions",
]
