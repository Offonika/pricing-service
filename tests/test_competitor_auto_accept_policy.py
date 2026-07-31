from __future__ import annotations

import json

from app.services.competitor_auto_accept_policy import load_auto_accept_policy


def test_default_policy_keeps_unvalidated_auto_categories_in_shadow():
    policy = load_auto_accept_policy()

    camera = policy.for_category("camera", "moba")
    assert camera.mode == "auto"
    assert camera.effective_mode == "shadow"
    assert camera.promotable is False
    assert policy.for_category("unknown", "moba").effective_mode == "review"
    assert policy.exact_evidence_policy.target_precision == 0.995


def test_competitor_override_promotes_only_after_minimum_precision(tmp_path):
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(
        json.dumps(
            {
                "version": 2,
                "target_precision": 0.95,
                "exact_code_target_precision": 0.995,
                "minimum_validation_examples": 50,
                "global": {"mode": "review", "min_score": 1.0},
                "exact_evidence": {"mode": "shadow", "min_score": 0.95},
                "categories": {
                    "camera": {
                        "mode": "auto",
                        "min_score": 0.84,
                        "validation_examples": 60,
                        "measured_precision": 0.96,
                        "required_evidence": ["same_model"],
                    },
                    "unknown": {"mode": "review", "min_score": 1.0},
                },
                "competitors": {
                    "liberti": {
                        "camera": {
                            "min_score": 0.88,
                            "validation_examples": 55,
                            "measured_precision": 0.94,
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    policy = load_auto_accept_policy(policy_path)

    assert policy.for_category("camera", "moba").effective_mode == "auto"
    liberti = policy.for_category("camera", "liberti")
    assert liberti.min_score == 0.88
    assert liberti.required_evidence == ("same_model",)
    assert liberti.effective_mode == "shadow"
