from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_POLICY_PATH = Path("config/competitor_matching/auto_accept_policy.json")
VALID_MODES = frozenset({"auto", "shadow", "review"})


@dataclass(frozen=True)
class CategoryPolicy:
    mode: str
    min_score: float
    required_evidence: tuple[str, ...]
    validation_examples: int
    minimum_validation_examples: int
    measured_precision: float | None
    target_precision: float

    @property
    def promotable(self) -> bool:
        return (
            self.validation_examples >= self.minimum_validation_examples
            and self.measured_precision is not None
            and self.measured_precision >= self.target_precision
        )

    @property
    def effective_mode(self) -> str:
        if self.mode != "auto":
            return self.mode
        return "auto" if self.promotable else "shadow"


@dataclass(frozen=True)
class AutoAcceptPolicy:
    version: int
    target_precision: float
    exact_code_target_precision: float
    minimum_validation_examples: int
    audit_sample_rate: float
    rollback_error_rate: float
    global_policy: CategoryPolicy
    exact_evidence_policy: CategoryPolicy
    categories: dict[str, CategoryPolicy]
    competitors: dict[str, dict[str, Any]]

    def for_category(self, item_type: str | None, competitor: str | None = None) -> CategoryPolicy:
        key = str(item_type or "unknown").strip().lower() or "unknown"
        base = self.categories.get(key, self.categories.get("unknown", self.global_policy))
        override = self.competitors.get(str(competitor or "").lower(), {}).get(key, {})
        if not override:
            return base
        return _category_policy(
            override,
            defaults=base,
            default_target_precision=self.target_precision,
            default_minimum_validation_examples=self.minimum_validation_examples,
        )


def load_auto_accept_policy(path: str | Path | None = None) -> AutoAcceptPolicy:
    policy_path = Path(path or DEFAULT_POLICY_PATH)
    payload = json.loads(policy_path.read_text(encoding="utf-8"))
    target_precision = float(payload.get("target_precision", 0.95))
    exact_code_target_precision = float(payload.get("exact_code_target_precision", 0.995))
    minimum_validation_examples = int(payload.get("minimum_validation_examples", 50))
    global_policy = _category_policy(
        payload.get("global") or {},
        default_target_precision=target_precision,
        default_minimum_validation_examples=minimum_validation_examples,
    )
    exact_evidence_policy = _category_policy(
        payload.get("exact_evidence") or {},
        default_target_precision=exact_code_target_precision,
        default_minimum_validation_examples=minimum_validation_examples,
    )
    categories = {
        str(key).lower(): _category_policy(
            value,
            default_target_precision=target_precision,
            default_minimum_validation_examples=minimum_validation_examples,
        )
        for key, value in (payload.get("categories") or {}).items()
    }
    policy = AutoAcceptPolicy(
        version=int(payload["version"]),
        target_precision=target_precision,
        exact_code_target_precision=exact_code_target_precision,
        minimum_validation_examples=minimum_validation_examples,
        audit_sample_rate=float(payload.get("audit_sample_rate", 0.10)),
        rollback_error_rate=float(payload.get("rollback_error_rate", 0.05)),
        global_policy=global_policy,
        exact_evidence_policy=exact_evidence_policy,
        categories=categories,
        competitors=dict(payload.get("competitors") or {}),
    )
    _validate(policy)
    return policy


def _category_policy(
    payload: dict[str, Any],
    *,
    defaults: CategoryPolicy | None = None,
    default_target_precision: float,
    default_minimum_validation_examples: int,
) -> CategoryPolicy:
    return CategoryPolicy(
        mode=str(payload.get("mode", defaults.mode if defaults else "review")).lower(),
        min_score=float(payload.get("min_score", defaults.min_score if defaults else 1.0)),
        required_evidence=tuple(
            str(value)
            for value in payload.get(
                "required_evidence",
                defaults.required_evidence if defaults else (),
            )
        ),
        validation_examples=int(
            payload.get(
                "validation_examples",
                defaults.validation_examples if defaults else 0,
            )
        ),
        minimum_validation_examples=int(
            payload.get(
                "minimum_validation_examples",
                (
                    defaults.minimum_validation_examples
                    if defaults
                    else default_minimum_validation_examples
                ),
            )
        ),
        measured_precision=(
            float(payload["measured_precision"])
            if payload.get("measured_precision") is not None
            else (defaults.measured_precision if defaults else None)
        ),
        target_precision=float(
            payload.get(
                "target_precision",
                defaults.target_precision if defaults else default_target_precision,
            )
        ),
    )


def _validate(policy: AutoAcceptPolicy) -> None:
    if policy.version < 1:
        raise ValueError("auto-accept policy version must be positive")
    for name, value in {
        "target_precision": policy.target_precision,
        "exact_code_target_precision": policy.exact_code_target_precision,
        "audit_sample_rate": policy.audit_sample_rate,
        "rollback_error_rate": policy.rollback_error_rate,
    }.items():
        if not 0 <= value <= 1:
            raise ValueError(f"{name} must be between 0 and 1")
    for name, category in {
        "global": policy.global_policy,
        "exact_evidence": policy.exact_evidence_policy,
        **policy.categories,
    }.items():
        if category.mode not in VALID_MODES:
            raise ValueError(f"unsupported mode for {name}: {category.mode}")
        if not 0 <= category.min_score <= 1:
            raise ValueError(f"min_score for {name} must be between 0 and 1")
        if category.validation_examples < 0:
            raise ValueError(f"validation_examples for {name} must not be negative")
        if category.minimum_validation_examples < 1:
            raise ValueError(f"minimum_validation_examples for {name} must be positive")
        if category.measured_precision is not None and not 0 <= category.measured_precision <= 1:
            raise ValueError(f"measured_precision for {name} must be between 0 and 1")
        if not 0 <= category.target_precision <= 1:
            raise ValueError(f"target_precision for {name} must be between 0 and 1")


__all__ = [
    "AutoAcceptPolicy",
    "CategoryPolicy",
    "DEFAULT_POLICY_PATH",
    "load_auto_accept_policy",
]
