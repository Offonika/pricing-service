from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.services.customer_settlement_mapping import CrmClusterSourceRow
from app.services.customer_settlement_source import CustomerSettlementScopeEligibility
from app.services.customer_settlements import (
    SettlementBalanceInput,
    SettlementMappingInput,
    activate_financial_revision,
    activate_mapping_revision,
    active_pilot_site_user_ids,
    onec_ref_to_guid,
    replace_pilot_access_scope,
)
from app.workers import customer_settlements as settlement_workers

ORG = "0x" + "a" * 32
ORG_GUID = onec_ref_to_guid(ORG)
BASE_TIME = datetime(2026, 8, 24, 9, 0, tzinfo=UTC)


def _ref(value: int) -> str:
    return f"0x{value:032x}"


def _crm_row(user_id: str, counterparty_ref: str) -> CrmClusterSourceRow:
    return CrmClusterSourceRow(
        row_id=user_id,
        cluster_id=f"cluster-{user_id}",
        site_user_ids=(user_id,),
        counterparty_refs=(counterparty_ref,),
        source_updated_at=BASE_TIME,
    )


def test_large_scope_requires_explicit_limit(db_session: Session) -> None:
    refs = tuple(_ref(value) for value in range(1, 12))
    entries = tuple(
        SettlementMappingInput(str(index), f"cluster-{index}", ref, "linked")
        for index, ref in enumerate(refs, start=1)
    )

    with pytest.raises(ValueError, match="mapping_revision_scope_limit_exceeded"):
        activate_mapping_revision(
            db_session,
            entries=entries,
            source_checked_at=BASE_TIME,
            organization_ref=ORG,
            organization_guid=ORG_GUID,
        )
    db_session.rollback()

    mapping, activated = activate_mapping_revision(
        db_session,
        entries=entries,
        source_checked_at=BASE_TIME,
        organization_ref=ORG,
        organization_guid=ORG_GUID,
        max_scope_users=20,
    )
    assert activated is True
    assert mapping.loaded_entry_count == 11

    financial, activated = activate_financial_revision(
        db_session,
        organization_ref=ORG,
        organization_guid=ORG_GUID,
        as_of=BASE_TIME,
        source_db_time=BASE_TIME,
        source_mode="synthetic-test",
        expected_counterparty_refs=refs,
        balances=tuple(SettlementBalanceInput(ref, Decimal("0.00")) for ref in refs),
        synced_at=BASE_TIME,
        max_scope_users=20,
    )
    assert activated is True
    assert financial.loaded_row_count == 11


def test_replace_access_scope_revokes_removed_users_atomically(db_session: Session) -> None:
    first = replace_pilot_access_scope(
        db_session,
        site_user_ids=("1", "2", "3"),
        max_scope_users=5,
    )
    assert first == {"scope_users": 3, "created": 3, "enabled": 3, "disabled": 0}
    assert active_pilot_site_user_ids(db_session) == ("1", "2", "3")

    second = replace_pilot_access_scope(
        db_session,
        site_user_ids=("2", "3", "4"),
        max_scope_users=5,
    )
    assert second == {"scope_users": 3, "created": 1, "enabled": 1, "disabled": 1}
    assert active_pilot_site_user_ids(db_session) == ("2", "3", "4")

    with pytest.raises(ValueError, match="pilot_whitelist_limit_exceeded"):
        replace_pilot_access_scope(
            db_session,
            site_user_ids=("1", "2", "3", "4", "5", "6"),
            max_scope_users=5,
        )
    assert active_pilot_site_user_ids(db_session) == ("2", "3", "4")


def test_all_linked_worker_replaces_scope_and_rolls_back_failed_change(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_rows = [_crm_row("101", _ref(1)), _crm_row("102", _ref(2))]
    settings = Settings(
        _env_file=None,
        customer_settlements_shadow_enabled=True,
        customer_settlements_mapping_mode="crm_readonly",
        customer_settlements_access_mode="all_linked",
        customer_settlements_max_scope_users=20,
        customer_settlements_excluded_counterparty_hashes=[
            hashlib.sha256(_ref(1).encode("ascii")).hexdigest()
        ],
        customer_settlements_expected_database_name="synthetic",
        customer_settlements_organization_ref=ORG,
        customer_settlements_organization_guid=ORG_GUID,
        customer_settlements_crm_webhook_url="https://example.test/rest/1/token",
        onec_database_url="mssql+pyodbc://synthetic",
    )
    monkeypatch.setattr(
        settlement_workers,
        "get_application_session_factory",
        lambda: lambda: Session(db_session.get_bind()),
    )
    monkeypatch.setattr(
        settlement_workers,
        "assert_expected_application_database",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        settlement_workers,
        "build_onec_engine",
        lambda *_args, **_kwargs: SimpleNamespace(dispose=lambda: None),
    )
    monkeypatch.setattr(
        settlement_workers,
        "fetch_crm_cluster_rows",
        lambda **_kwargs: tuple(source_rows),
    )
    monkeypatch.setattr(
        settlement_workers,
        "resolve_crm_counterparty_hashes",
        lambda rows, **_kwargs: tuple(rows),
    )
    monkeypatch.setattr(
        settlement_workers,
        "fetch_customer_settlement_scope_eligibility",
        lambda _engine, *, counterparty_refs, **_kwargs: CustomerSettlementScopeEligibility(
            eligible_counterparty_refs=tuple(counterparty_refs),
            total_counterparties=len(counterparty_refs),
            blank_name_counterparties=0,
            non_rub_counterparties=0,
            duration_seconds=0.1,
        ),
    )

    result = settlement_workers.run_customer_settlement_mapping_sync(settings=settings)
    assert result["status"] == "activated"
    assert result["mapping_entries"] == 1
    assert result["access_scope_changes"] == {
        "scope_users": 1,
        "created": 1,
        "enabled": 1,
        "disabled": 0,
    }
    assert result["scope_eligibility"]["eligible_counterparties"] == 2
    assert result["scope_eligibility"]["excluded_counterparties"] == 1
    with Session(db_session.get_bind()) as readback:
        assert active_pilot_site_user_ids(readback) == ("102",)

    source_rows[:] = [_crm_row("102", _ref(2)), _crm_row("103", _ref(3))]
    original_activate = settlement_workers.activate_mapping_revision
    monkeypatch.setattr(
        settlement_workers,
        "activate_mapping_revision",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("synthetic failure")),
    )
    failed = settlement_workers.run_customer_settlement_mapping_sync(settings=settings)
    assert failed == {"status": "error", "reason": "mapping_sync_failed"}
    with Session(db_session.get_bind()) as readback:
        assert active_pilot_site_user_ids(readback) == ("102",)

    monkeypatch.setattr(settlement_workers, "activate_mapping_revision", original_activate)
    recovered = settlement_workers.run_customer_settlement_mapping_sync(settings=settings)
    assert recovered["status"] == "activated"
    with Session(db_session.get_bind()) as readback:
        assert active_pilot_site_user_ids(readback) == ("102", "103")


def test_all_linked_worker_blocks_invalid_exclusion_hashes() -> None:
    settings = Settings(
        _env_file=None,
        customer_settlements_shadow_enabled=True,
        customer_settlements_mapping_mode="crm_readonly",
        customer_settlements_access_mode="all_linked",
        customer_settlements_excluded_counterparty_hashes=["not-a-sha256"],
    )

    assert settlement_workers.run_customer_settlement_mapping_sync(settings=settings) == {
        "status": "blocked",
        "reason": "excluded_counterparty_hashes_invalid",
    }
