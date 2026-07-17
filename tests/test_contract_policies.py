from __future__ import annotations

from app.infrastructure.contract_policies import CONTRACT_POLICIES


def test_retail_director_monthly_contract_policy_accepts_matching_month_path() -> None:
    policy = CONTRACT_POLICIES.get(
        "retail-director-monthly/2026-06/retail-director-summary-2026-06.json"
    )

    assert policy is not None
    assert policy.contract_version == "retail-director-monthly-snapshot.v2"


def test_retail_director_monthly_contract_policy_rejects_mismatched_month_path() -> None:
    policy = CONTRACT_POLICIES.get(
        "retail-director-monthly/2026-06/retail-director-summary-2026-05.json"
    )

    assert policy is None
