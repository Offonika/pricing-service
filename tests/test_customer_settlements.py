from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.customer_settlement import (
    CustomerAccountSiteBinding,
    CustomerAccountSourceBinding,
    CustomerSettlementAlertOutbox,
    CustomerSettlementAssertionJti,
    CustomerSettlementBalance,
    CustomerSettlementMappingEntry,
    CustomerSettlementMappingRevision,
    CustomerSettlementPilotAccess,
    CustomerSettlementReconciliationRun,
    CustomerSettlementRevision,
)
from app.services import customer_settlements as settlement_services
from app.services.customer_settlements import (
    SettlementBalanceInput,
    SettlementMappingInput,
    activate_financial_revision,
    activate_mapping_revision,
    active_pilot_counterparty_refs,
    cleanup_customer_settlements,
    customer_settlement_health_metrics,
    ensure_utc,
    get_customer_settlement_eligibility,
    get_customer_settlement_summary,
    mark_financial_revision_failed,
    normalize_money,
    onec_guid_to_ref,
    onec_ref_to_guid,
    set_pilot_access,
    settlement_state,
)

ORG = "0x" + "a" * 32
ORG_GUID = onec_ref_to_guid(ORG)
CP_1 = "0x" + "1" * 32
CP_2 = "0x" + "2" * 32
CP_3 = "0x" + "3" * 32
CP_4 = "0x" + "4" * 32
BASE_TIME = datetime(2026, 7, 29, 9, 0, tzinfo=UTC)


def _balance(ref: str, amount: str) -> SettlementBalanceInput:
    return SettlementBalanceInput(counterparty_ref=ref, signed_balance=Decimal(amount))


def _activate_balances(
    session: Session,
    rows: list[SettlementBalanceInput],
    *,
    as_of: datetime = BASE_TIME,
    synced_at: datetime = BASE_TIME,
) -> CustomerSettlementRevision:
    revision, activated = activate_financial_revision(
        session,
        organization_ref=ORG,
        as_of=as_of,
        source_db_time=as_of,
        source_mode="synthetic-test",
        expected_counterparty_refs=[row.counterparty_ref for row in rows],
        balances=rows,
        synced_at=synced_at,
    )
    assert activated is True
    return revision


def _activate_mapping(
    session: Session,
    entries: list[SettlementMappingInput],
    *,
    checked_at: datetime = BASE_TIME,
    source_name: str = "bitrix_crm_customer_cluster",
) -> CustomerSettlementMappingRevision:
    revision, activated = activate_mapping_revision(
        session,
        entries=entries,
        source_checked_at=checked_at,
        source_name=source_name,
        organization_ref=ORG,
        organization_guid=ORG_GUID,
    )
    assert activated is True
    assert revision.status == "active"
    return revision


def _linked(user_id: str, ref: str, *, cluster: str | None = None) -> SettlementMappingInput:
    return SettlementMappingInput(
        site_user_id=user_id,
        cluster_id=cluster or f"cluster-{user_id}",
        counterparty_ref=ref,
        status="linked",
    )


def test_money_rules_round_half_up_and_never_return_negative_zero() -> None:
    assert normalize_money("-0.004") == Decimal("0.00")
    assert settlement_state(Decimal("0.004")) == ("zero", Decimal("0.00"))
    assert settlement_state(Decimal("12.345")) == ("debt", Decimal("12.35"))
    assert settlement_state(Decimal("-12.345")) == ("advance", Decimal("12.35"))


def test_financial_revision_requires_every_expected_counterparty(db_session: Session) -> None:
    with pytest.raises(ValueError, match="incomplete_financial_revision"):
        activate_financial_revision(
            db_session,
            organization_ref=ORG,
            as_of=BASE_TIME,
            source_db_time=BASE_TIME,
            source_mode="synthetic-test",
            expected_counterparty_refs=[CP_1, CP_2],
            balances=[_balance(CP_1, "1.00")],
        )

    assert db_session.scalar(select(func.count()).select_from(CustomerSettlementRevision)) == 0

    with pytest.raises(ValueError, match="financial_revision_as_of_is_in_the_future"):
        activate_financial_revision(
            db_session,
            organization_ref=ORG,
            as_of=BASE_TIME + timedelta(seconds=1),
            source_db_time=BASE_TIME,
            source_mode="synthetic-test",
            expected_counterparty_refs=[CP_1],
            balances=[_balance(CP_1, "1.00")],
        )


def test_financial_revision_rejects_source_time_ahead_of_sync_clock(
    db_session: Session,
) -> None:
    with pytest.raises(ValueError, match="source_time_is_in_the_future"):
        activate_financial_revision(
            db_session,
            organization_ref=ORG,
            as_of=BASE_TIME,
            source_db_time=BASE_TIME + timedelta(minutes=1),
            source_mode="synthetic-test",
            expected_counterparty_refs=[CP_1],
            balances=[_balance(CP_1, "10.00")],
            synced_at=BASE_TIME,
        )


def test_revision_activation_rejects_timestamps_ahead_of_backend_clock(
    db_session: Session,
) -> None:
    future_time = settlement_services.utc_now() + timedelta(hours=1)
    with pytest.raises(ValueError, match="source_time_is_in_the_future"):
        activate_financial_revision(
            db_session,
            organization_ref=ORG,
            as_of=future_time,
            source_db_time=future_time,
            source_mode="synthetic-test",
            expected_counterparty_refs=[CP_1],
            balances=[_balance(CP_1, "10.00")],
            synced_at=future_time,
        )

    with pytest.raises(ValueError, match="mapping_revision_source_time_is_in_the_future"):
        activate_mapping_revision(
            db_session,
            entries=(_linked("901", CP_1),),
            source_checked_at=future_time,
            organization_ref=ORG,
            organization_guid=ORG_GUID,
        )


def test_empty_revision_scopes_are_rejected(db_session: Session) -> None:
    with pytest.raises(ValueError, match="financial_revision_scope_is_empty"):
        activate_financial_revision(
            db_session,
            organization_ref=ORG,
            as_of=BASE_TIME,
            source_db_time=BASE_TIME,
            source_mode="synthetic-test",
            expected_counterparty_refs=(),
            balances=(),
        )
    db_session.rollback()

    with pytest.raises(ValueError, match="mapping_revision_scope_is_empty"):
        activate_mapping_revision(
            db_session,
            entries=(),
            source_checked_at=BASE_TIME,
        )


def test_revision_scopes_cannot_exceed_pilot_limit(db_session: Session) -> None:
    refs = [f"0x{value:032x}" for value in range(1, 12)]
    with pytest.raises(ValueError, match="financial_revision_scope_limit_exceeded"):
        activate_financial_revision(
            db_session,
            organization_ref=ORG,
            as_of=BASE_TIME,
            source_db_time=BASE_TIME,
            source_mode="synthetic-test",
            expected_counterparty_refs=refs,
            balances=[_balance(ref, "0.00") for ref in refs],
        )

    with pytest.raises(ValueError, match="mapping_revision_scope_limit_exceeded"):
        activate_mapping_revision(
            db_session,
            entries=tuple(_linked(str(index), ref) for index, ref in enumerate(refs, start=1)),
            source_checked_at=BASE_TIME,
            organization_ref=ORG,
            organization_guid=ORG_GUID,
        )


def test_identical_financial_payload_does_not_refresh_corrupted_revision(
    db_session: Session,
) -> None:
    revision = _activate_balances(db_session, [_balance(CP_1, "10.00")])
    db_session.commit()
    stored = db_session.scalar(
        select(CustomerSettlementBalance).where(
            CustomerSettlementBalance.revision_id == revision.id
        )
    )
    assert stored is not None
    db_session.delete(stored)
    db_session.commit()

    with pytest.raises(ValueError, match="financial_revision_payload_mismatch"):
        activate_financial_revision(
            db_session,
            organization_ref=ORG,
            as_of=BASE_TIME,
            source_db_time=BASE_TIME,
            source_mode="synthetic-test",
            expected_counterparty_refs=[CP_1],
            balances=[_balance(CP_1, "10.00")],
            synced_at=BASE_TIME + timedelta(minutes=5),
        )


def test_identical_financial_payload_rejects_corrupted_revision_metadata(
    db_session: Session,
) -> None:
    revision = _activate_balances(db_session, [_balance(CP_1, "10.00")])
    db_session.commit()
    revision.organization_guid = onec_ref_to_guid(CP_2)
    db_session.commit()

    with pytest.raises(ValueError, match="financial_revision_payload_mismatch"):
        activate_financial_revision(
            db_session,
            organization_ref=ORG,
            as_of=BASE_TIME,
            source_db_time=BASE_TIME,
            source_mode="synthetic-test",
            expected_counterparty_refs=[CP_1],
            balances=[_balance(CP_1, "10.00")],
        )


def test_identical_mapping_payload_does_not_refresh_corrupted_revision(
    db_session: Session,
) -> None:
    entry = _linked("100", CP_1)
    revision, activated = activate_mapping_revision(
        db_session,
        entries=[entry],
        source_checked_at=BASE_TIME,
        organization_ref=ORG,
        organization_guid=ORG_GUID,
    )
    assert activated is True
    db_session.commit()
    stored = db_session.scalar(
        select(CustomerSettlementMappingEntry).where(
            CustomerSettlementMappingEntry.revision_id == revision.id
        )
    )
    assert stored is not None
    db_session.delete(stored)
    db_session.commit()

    with pytest.raises(ValueError, match="mapping_revision_payload_mismatch"):
        activate_mapping_revision(
            db_session,
            entries=[entry],
            source_checked_at=BASE_TIME + timedelta(minutes=5),
            organization_ref=ORG,
            organization_guid=ORG_GUID,
        )


def test_zero_balance_is_stored_and_new_revision_atomically_supersedes_old(
    db_session: Session,
) -> None:
    first = _activate_balances(db_session, [_balance(CP_1, "0")])
    db_session.commit()
    stored = db_session.scalar(
        select(CustomerSettlementBalance).where(CustomerSettlementBalance.revision_id == first.id)
    )
    assert stored is not None
    assert stored.signed_balance == Decimal("0.00")
    assert first.zero_row_count == 1

    second = _activate_balances(
        db_session,
        [_balance(CP_1, "15.00")],
        as_of=BASE_TIME + timedelta(hours=1),
        synced_at=BASE_TIME + timedelta(hours=1),
    )
    db_session.commit()
    db_session.refresh(first)
    assert first.status == "superseded"
    assert second.status == "active"

    repeated, activated = activate_financial_revision(
        db_session,
        organization_ref=ORG,
        as_of=BASE_TIME + timedelta(hours=1),
        source_db_time=BASE_TIME + timedelta(hours=1),
        source_mode="synthetic-test",
        expected_counterparty_refs=[CP_1],
        balances=[_balance(CP_1, "15.00")],
        synced_at=BASE_TIME + timedelta(hours=2),
    )
    assert activated is False
    assert repeated.id == second.id
    assert db_session.scalar(select(func.count()).select_from(CustomerSettlementRevision)) == 2


@pytest.mark.parametrize("value", ("NaN", "Infinity", "-Infinity"))
def test_non_finite_financial_amounts_are_rejected(
    db_session: Session,
    value: str,
) -> None:
    with pytest.raises(ValueError, match="money value must be finite"):
        activate_financial_revision(
            db_session,
            organization_ref=ORG,
            as_of=BASE_TIME,
            source_db_time=BASE_TIME,
            source_mode="synthetic-test",
            expected_counterparty_refs=[CP_1],
            balances=[_balance(CP_1, value)],
        )


def test_summary_covers_debt_advance_zero_and_freshness(db_session: Session) -> None:
    _activate_mapping(
        db_session,
        [_linked("101", CP_1), _linked("102", CP_2), _linked("103", CP_3)],
    )
    _activate_balances(
        db_session,
        [_balance(CP_1, "14800"), _balance(CP_2, "-250"), _balance(CP_3, "0")],
    )
    for user_id in ("101", "102", "103"):
        set_pilot_access(db_session, site_user_id=user_id, enabled=True)
    db_session.commit()

    debt = get_customer_settlement_summary(
        db_session,
        site_user_id="101",
        enabled=True,
        stale_after_seconds=7200,
        hide_after_seconds=21600,
        mapping_stale_after_seconds=7200,
        now=BASE_TIME + timedelta(hours=1),
    )
    advance = get_customer_settlement_summary(
        db_session,
        site_user_id="102",
        enabled=True,
        stale_after_seconds=7200,
        hide_after_seconds=21600,
        mapping_stale_after_seconds=7200,
        now=BASE_TIME + timedelta(hours=1),
    )
    zero = get_customer_settlement_summary(
        db_session,
        site_user_id="103",
        enabled=True,
        stale_after_seconds=7200,
        hide_after_seconds=21600,
        mapping_stale_after_seconds=7200,
        now=BASE_TIME + timedelta(hours=1),
    )

    assert (debt.status, debt.state, debt.amount) == (
        "available",
        "debt",
        Decimal("14800.00"),
    )
    assert (advance.state, advance.amount) == ("advance", Decimal("250.00"))
    assert (zero.state, zero.amount) == ("zero", Decimal("0.00"))

    stale = get_customer_settlement_summary(
        db_session,
        site_user_id="101",
        enabled=True,
        stale_after_seconds=7200,
        hide_after_seconds=21600,
        mapping_stale_after_seconds=21600,
        now=BASE_TIME + timedelta(hours=3),
    )
    hidden = get_customer_settlement_summary(
        db_session,
        site_user_id="101",
        enabled=True,
        stale_after_seconds=7200,
        hide_after_seconds=21600,
        mapping_stale_after_seconds=28800,
        now=BASE_TIME + timedelta(hours=7),
    )
    assert stale.status == "stale"
    assert stale.is_stale is True
    assert stale.amount == Decimal("14800.00")
    assert hidden.status == "temporarily_unavailable"
    assert hidden.is_stale is False
    assert hidden.amount is None
    assert hidden.state is None


def test_financial_freshness_boundaries_are_exact(db_session: Session) -> None:
    _activate_mapping(db_session, [_linked("104", CP_1)])
    _activate_balances(db_session, [_balance(CP_1, "10.00")])
    set_pilot_access(db_session, site_user_id="104", enabled=True)
    db_session.commit()

    exact_stale = get_customer_settlement_summary(
        db_session,
        site_user_id="104",
        enabled=True,
        stale_after_seconds=7200,
        hide_after_seconds=21600,
        mapping_stale_after_seconds=21600,
        now=BASE_TIME + timedelta(hours=2),
    )
    exact_hide = get_customer_settlement_summary(
        db_session,
        site_user_id="104",
        enabled=True,
        stale_after_seconds=7200,
        hide_after_seconds=21600,
        mapping_stale_after_seconds=21600,
        now=BASE_TIME + timedelta(hours=6),
    )
    after_hide = get_customer_settlement_summary(
        db_session,
        site_user_id="104",
        enabled=True,
        stale_after_seconds=7200,
        hide_after_seconds=21600,
        mapping_stale_after_seconds=21601,
        now=BASE_TIME + timedelta(hours=6, microseconds=1),
    )
    exact_health = customer_settlement_health_metrics(
        db_session,
        stale_after_seconds=7200,
        hide_after_seconds=21600,
        mapping_stale_after_seconds=21600,
        now=BASE_TIME + timedelta(hours=2),
    )
    after_hide_health = customer_settlement_health_metrics(
        db_session,
        stale_after_seconds=7200,
        hide_after_seconds=21600,
        mapping_stale_after_seconds=21601,
        now=BASE_TIME + timedelta(hours=6, microseconds=1),
    )

    assert exact_stale.status == "stale"
    assert exact_stale.is_stale is True
    assert exact_hide.status == "stale"
    assert after_hide.status == "temporarily_unavailable"
    assert exact_health["freshness_status"] == "warning"
    assert after_hide_health["freshness_status"] == "critical"


def test_eligibility_uses_only_pilot_and_fresh_mapping(db_session: Session) -> None:
    _activate_mapping(db_session, [_linked("111", CP_1)])
    set_pilot_access(db_session, site_user_id="111", enabled=True)
    db_session.commit()

    assert (
        get_customer_settlement_eligibility(
            db_session,
            site_user_id="111",
            enabled=True,
            mapping_stale_after_seconds=7200,
            now=BASE_TIME + timedelta(hours=1),
        )
        == "eligible"
    )
    assert (
        get_customer_settlement_eligibility(
            db_session,
            site_user_id="999",
            enabled=True,
            mapping_stale_after_seconds=7200,
            now=BASE_TIME,
        )
        == "not_eligible"
    )
    assert (
        get_customer_settlement_eligibility(
            db_session,
            site_user_id="111",
            enabled=True,
            mapping_stale_after_seconds=7200,
            now=BASE_TIME + timedelta(hours=3),
        )
        == "temporarily_unavailable"
    )

    _activate_mapping(
        db_session,
        [_linked("111", CP_1)],
        checked_at=BASE_TIME + timedelta(minutes=1),
        source_name="manual_confirmed_pilot",
    )
    db_session.commit()
    assert (
        get_customer_settlement_eligibility(
            db_session,
            site_user_id="111",
            enabled=True,
            mapping_stale_after_seconds=7200,
            now=BASE_TIME + timedelta(days=1),
        )
        == "eligible"
    )


def test_pilot_whitelist_rejects_eleventh_enabled_user(db_session: Session) -> None:
    for user_id in range(1, 11):
        set_pilot_access(db_session, site_user_id=str(user_id), enabled=True)
    with pytest.raises(ValueError, match="pilot_whitelist_limit_exceeded"):
        set_pilot_access(db_session, site_user_id="11", enabled=True)

    assert (
        db_session.scalar(
            select(func.count())
            .select_from(CustomerSettlementPilotAccess)
            .where(CustomerSettlementPilotAccess.enabled.is_(True))
        )
        == 10
    )


def test_onec_reference_guid_round_trip_is_stable() -> None:
    ref = "0xb34a0025901e48ef11e211128227ea80"
    guid = "8227ea80-1112-11e2-b34a-0025901e48ef"
    assert onec_ref_to_guid(ref) == guid
    assert onec_guid_to_ref(guid) == ref


def test_customer_account_survives_guid_remap_and_old_snapshot_is_not_reused(
    db_session: Session,
) -> None:
    _activate_mapping(db_session, [_linked("501", CP_1)])
    _activate_balances(db_session, [_balance(CP_1, "10.00")])
    set_pilot_access(db_session, site_user_id="501", enabled=True)
    db_session.commit()
    first_site_binding = db_session.scalar(
        select(CustomerAccountSiteBinding).where(
            CustomerAccountSiteBinding.site_user_id == "501",
            CustomerAccountSiteBinding.status == "active",
        )
    )
    assert first_site_binding is not None
    account_id = first_site_binding.customer_account_id

    _activate_mapping(
        db_session,
        [_linked("501", CP_2)],
        checked_at=BASE_TIME + timedelta(minutes=30),
    )
    db_session.commit()
    current_site_binding = db_session.scalar(
        select(CustomerAccountSiteBinding).where(
            CustomerAccountSiteBinding.site_user_id == "501",
            CustomerAccountSiteBinding.status == "active",
        )
    )
    assert current_site_binding is not None
    assert current_site_binding.customer_account_id == account_id
    source_bindings = list(
        db_session.scalars(
            select(CustomerAccountSourceBinding)
            .where(CustomerAccountSourceBinding.customer_account_id == account_id)
            .order_by(CustomerAccountSourceBinding.id)
        )
    )
    assert [(item.counterparty_ref, item.status) for item in source_bindings] == [
        (CP_1, "revoked"),
        (CP_2, "active"),
    ]
    summary = get_customer_settlement_summary(
        db_session,
        site_user_id="501",
        enabled=True,
        stale_after_seconds=7200,
        hide_after_seconds=21600,
        mapping_stale_after_seconds=7200,
        now=BASE_TIME + timedelta(hours=1),
    )
    assert summary.status == "temporarily_unavailable"
    assert summary.amount is None


def test_remap_splits_shared_account_without_breaking_other_site_user(
    db_session: Session,
) -> None:
    _activate_mapping(
        db_session,
        [_linked("551", CP_1, cluster="shared"), _linked("552", CP_1, cluster="shared")],
    )
    db_session.commit()
    initial_bindings = list(
        db_session.scalars(
            select(CustomerAccountSiteBinding).where(CustomerAccountSiteBinding.status == "active")
        )
    )
    assert len({item.customer_account_id for item in initial_bindings}) == 1

    _activate_mapping(
        db_session,
        [_linked("551", CP_2, cluster="moved"), _linked("552", CP_1, cluster="shared")],
        checked_at=BASE_TIME + timedelta(minutes=30),
    )
    _activate_balances(
        db_session,
        [_balance(CP_1, "10.00"), _balance(CP_2, "20.00")],
        as_of=BASE_TIME + timedelta(hours=1),
        synced_at=BASE_TIME + timedelta(hours=1),
    )
    for user_id in ("551", "552"):
        set_pilot_access(db_session, site_user_id=user_id, enabled=True)
    db_session.commit()

    current_bindings = list(
        db_session.scalars(
            select(CustomerAccountSiteBinding).where(CustomerAccountSiteBinding.status == "active")
        )
    )
    assert len(current_bindings) == 2
    assert len({item.customer_account_id for item in current_bindings}) == 2
    summaries = {
        user_id: get_customer_settlement_summary(
            db_session,
            site_user_id=user_id,
            enabled=True,
            stale_after_seconds=7200,
            hide_after_seconds=21600,
            mapping_stale_after_seconds=7200,
            now=BASE_TIME + timedelta(hours=1, minutes=5),
        )
        for user_id in ("551", "552")
    }
    assert summaries["551"].amount == Decimal("20.00")
    assert summaries["552"].amount == Decimal("10.00")


def test_remap_moves_all_users_of_shared_account_to_one_new_identity(
    db_session: Session,
) -> None:
    _activate_mapping(
        db_session,
        [
            _linked("561", CP_1, cluster="shared"),
            _linked("562", CP_1, cluster="shared"),
        ],
    )
    db_session.commit()
    original_bindings = list(
        db_session.scalars(
            select(CustomerAccountSiteBinding).where(CustomerAccountSiteBinding.status == "active")
        )
    )
    original_account_id = original_bindings[0].customer_account_id
    assert {item.customer_account_id for item in original_bindings} == {original_account_id}

    _activate_mapping(
        db_session,
        [
            _linked("561", CP_2, cluster="moved"),
            _linked("562", CP_2, cluster="moved"),
        ],
        checked_at=BASE_TIME + timedelta(minutes=30),
    )
    db_session.commit()

    current_bindings = list(
        db_session.scalars(
            select(CustomerAccountSiteBinding).where(CustomerAccountSiteBinding.status == "active")
        )
    )
    assert {item.customer_account_id for item in current_bindings} == {original_account_id}
    current_source = db_session.scalar(
        select(CustomerAccountSourceBinding).where(
            CustomerAccountSourceBinding.customer_account_id == original_account_id,
            CustomerAccountSourceBinding.status == "active",
        )
    )
    assert current_source is not None
    assert current_source.counterparty_ref == CP_2


def test_remap_partitions_shared_account_by_new_identity(db_session: Session) -> None:
    _activate_mapping(
        db_session,
        [
            _linked("571", CP_1, cluster="shared"),
            _linked("572", CP_1, cluster="shared"),
            _linked("573", CP_1, cluster="shared"),
        ],
    )
    db_session.commit()

    _activate_mapping(
        db_session,
        [
            _linked("571", CP_2, cluster="moved-together"),
            _linked("572", CP_2, cluster="moved-together"),
            _linked("573", CP_3, cluster="moved-alone"),
        ],
        checked_at=BASE_TIME + timedelta(minutes=30),
    )
    db_session.commit()

    bindings_by_user = {
        item.site_user_id: item
        for item in db_session.scalars(
            select(CustomerAccountSiteBinding).where(CustomerAccountSiteBinding.status == "active")
        )
    }
    assert (
        bindings_by_user["571"].customer_account_id == bindings_by_user["572"].customer_account_id
    )
    assert (
        bindings_by_user["573"].customer_account_id != bindings_by_user["571"].customer_account_id
    )


def test_mapping_conflict_between_two_durable_accounts_fails_closed(
    db_session: Session,
) -> None:
    _activate_mapping(db_session, [_linked("601", CP_1), _linked("602", CP_2)])
    db_session.commit()
    with pytest.raises(ValueError, match="durable_customer_account_mapping_conflict"):
        _activate_mapping(
            db_session,
            [_linked("601", CP_2)],
            checked_at=BASE_TIME + timedelta(minutes=30),
        )
    db_session.rollback()
    active_bindings = list(
        db_session.scalars(
            select(CustomerAccountSourceBinding).where(
                CustomerAccountSourceBinding.status == "active"
            )
        )
    )
    assert len(active_bindings) == 2


def test_existing_identity_conflict_does_not_depend_on_mapping_order(
    db_session: Session,
) -> None:
    _activate_mapping(db_session, [_linked("601", CP_2), _linked("602", CP_1)])
    db_session.commit()

    with pytest.raises(ValueError, match="durable_customer_account_mapping_conflict"):
        _activate_mapping(
            db_session,
            [_linked("601", CP_2), _linked("602", CP_2)],
            checked_at=BASE_TIME + timedelta(minutes=30),
        )
    db_session.rollback()

    active_site_bindings = list(
        db_session.scalars(
            select(CustomerAccountSiteBinding).where(CustomerAccountSiteBinding.status == "active")
        )
    )
    assert len({item.customer_account_id for item in active_site_bindings}) == 2


def test_revoked_site_binding_cannot_reuse_current_mapping_entry(
    db_session: Session,
) -> None:
    _activate_mapping(db_session, [_linked("650", CP_1)])
    _activate_balances(db_session, [_balance(CP_1, "10.00")])
    set_pilot_access(db_session, site_user_id="650", enabled=True)
    db_session.commit()
    site_binding = db_session.scalar(
        select(CustomerAccountSiteBinding).where(
            CustomerAccountSiteBinding.site_user_id == "650",
            CustomerAccountSiteBinding.status == "active",
        )
    )
    assert site_binding is not None
    site_binding.status = "revoked"
    site_binding.valid_to = BASE_TIME + timedelta(minutes=1)
    db_session.commit()

    summary = get_customer_settlement_summary(
        db_session,
        site_user_id="650",
        enabled=True,
        stale_after_seconds=7200,
        hide_after_seconds=21600,
        mapping_stale_after_seconds=7200,
        now=BASE_TIME + timedelta(minutes=5),
    )
    eligibility = get_customer_settlement_eligibility(
        db_session,
        site_user_id="650",
        enabled=True,
        mapping_stale_after_seconds=7200,
        now=BASE_TIME + timedelta(minutes=5),
    )
    health = customer_settlement_health_metrics(
        db_session,
        stale_after_seconds=7200,
        hide_after_seconds=21600,
        mapping_stale_after_seconds=7200,
        now=BASE_TIME + timedelta(minutes=5),
    )

    assert summary.status == "ambiguous_link"
    assert eligibility == "not_eligible"
    assert active_pilot_counterparty_refs(db_session) == ()
    assert health["mapping_status"] == "critical"


def test_mismatched_source_system_cannot_reuse_current_mapping_entry(
    db_session: Session,
) -> None:
    _activate_mapping(db_session, [_linked("651", CP_1)])
    _activate_balances(db_session, [_balance(CP_1, "10.00")])
    set_pilot_access(db_session, site_user_id="651", enabled=True)
    db_session.commit()
    source_binding = db_session.scalar(
        select(CustomerAccountSourceBinding).where(CustomerAccountSourceBinding.status == "active")
    )
    assert source_binding is not None
    source_binding.source_system = "ka2"
    db_session.commit()

    summary = get_customer_settlement_summary(
        db_session,
        site_user_id="651",
        enabled=True,
        stale_after_seconds=7200,
        hide_after_seconds=21600,
        mapping_stale_after_seconds=7200,
        now=BASE_TIME + timedelta(minutes=5),
    )
    eligibility = get_customer_settlement_eligibility(
        db_session,
        site_user_id="651",
        enabled=True,
        mapping_stale_after_seconds=7200,
        now=BASE_TIME + timedelta(minutes=5),
    )
    health = customer_settlement_health_metrics(
        db_session,
        stale_after_seconds=7200,
        hide_after_seconds=21600,
        mapping_stale_after_seconds=7200,
        now=BASE_TIME + timedelta(minutes=5),
    )

    assert summary.status == "ambiguous_link"
    assert eligibility == "not_eligible"
    assert active_pilot_counterparty_refs(db_session) == ()
    assert health["mapping_status"] == "critical"


def test_stale_source_binding_revision_cannot_reuse_current_mapping_entry(
    db_session: Session,
) -> None:
    _activate_mapping(db_session, [_linked("652", CP_1)])
    _activate_balances(db_session, [_balance(CP_1, "10.00")])
    set_pilot_access(db_session, site_user_id="652", enabled=True)
    db_session.commit()
    source_binding = db_session.scalar(
        select(CustomerAccountSourceBinding).where(CustomerAccountSourceBinding.status == "active")
    )
    assert source_binding is not None
    source_binding.mapping_revision_id = None
    db_session.commit()

    summary = get_customer_settlement_summary(
        db_session,
        site_user_id="652",
        enabled=True,
        stale_after_seconds=7200,
        hide_after_seconds=21600,
        mapping_stale_after_seconds=7200,
        now=BASE_TIME + timedelta(minutes=5),
    )
    eligibility = get_customer_settlement_eligibility(
        db_session,
        site_user_id="652",
        enabled=True,
        mapping_stale_after_seconds=7200,
        now=BASE_TIME + timedelta(minutes=5),
    )
    health = customer_settlement_health_metrics(
        db_session,
        stale_after_seconds=7200,
        hide_after_seconds=21600,
        mapping_stale_after_seconds=7200,
        now=BASE_TIME + timedelta(minutes=5),
    )

    assert summary.status == "ambiguous_link"
    assert eligibility == "not_eligible"
    assert active_pilot_counterparty_refs(db_session) == ()
    assert health["mapping_status"] == "critical"


def test_not_linked_mapping_revokes_previous_site_binding(db_session: Session) -> None:
    _activate_mapping(db_session, [_linked("651", CP_1)])
    db_session.commit()
    _activate_mapping(
        db_session,
        [SettlementMappingInput("651", None, None, "not_linked")],
        checked_at=BASE_TIME + timedelta(minutes=30),
    )
    db_session.commit()

    bindings = list(
        db_session.scalars(
            select(CustomerAccountSiteBinding).where(
                CustomerAccountSiteBinding.site_user_id == "651"
            )
        )
    )
    assert len(bindings) == 1
    assert bindings[0].status == "revoked"
    assert bindings[0].valid_to is not None
    assert ensure_utc(bindings[0].valid_to) == BASE_TIME + timedelta(minutes=30)


def test_manual_confirmed_mapping_does_not_expire_without_explicit_remap(
    db_session: Session,
) -> None:
    revision, activated = activate_mapping_revision(
        db_session,
        entries=[_linked("701", CP_1)],
        source_checked_at=BASE_TIME,
        source_name="manual_confirmed_pilot",
        organization_ref=ORG,
        organization_guid=ORG_GUID,
    )
    assert activated is True
    assert revision.source_name == "manual_confirmed_pilot"
    _activate_balances(
        db_session,
        [_balance(CP_1, "12.00")],
        as_of=BASE_TIME + timedelta(days=1),
        synced_at=BASE_TIME + timedelta(days=1),
    )
    set_pilot_access(db_session, site_user_id="701", enabled=True)
    db_session.commit()

    summary = get_customer_settlement_summary(
        db_session,
        site_user_id="701",
        enabled=True,
        stale_after_seconds=7200,
        hide_after_seconds=21600,
        mapping_stale_after_seconds=7200,
        now=BASE_TIME + timedelta(days=1, minutes=5),
    )
    assert summary.status == "available"
    health = customer_settlement_health_metrics(
        db_session,
        stale_after_seconds=7200,
        hide_after_seconds=21600,
        mapping_stale_after_seconds=7200,
        now=BASE_TIME + timedelta(days=1, minutes=5),
    )
    assert health["mapping_status"] == "ok"


def test_summary_fails_closed_for_mapping_states_and_missing_compatible_balance(
    db_session: Session,
) -> None:
    _activate_balances(db_session, [_balance(CP_1, "10")])
    _activate_mapping(
        db_session,
        [
            _linked("201", CP_4),
            SettlementMappingInput("202", None, None, "not_linked"),
            SettlementMappingInput("203", None, None, "ambiguous"),
        ],
    )
    for user_id in ("201", "202", "203"):
        set_pilot_access(db_session, site_user_id=user_id, enabled=True)
    db_session.commit()

    def summary(user_id: str, *, now: datetime = BASE_TIME + timedelta(minutes=5)):
        return get_customer_settlement_summary(
            db_session,
            site_user_id=user_id,
            enabled=True,
            stale_after_seconds=7200,
            hide_after_seconds=21600,
            mapping_stale_after_seconds=7200,
            now=now,
        )

    assert summary("201").status == "temporarily_unavailable"
    assert summary("202").status == "not_linked"
    assert summary("203").status == "ambiguous_link"
    assert summary("204").status == "pilot_disabled"

    set_pilot_access(db_session, site_user_id="204", enabled=True)
    db_session.commit()
    assert summary("204").status == "temporarily_unavailable"
    assert summary("202").status == "not_linked"
    assert (
        get_customer_settlement_eligibility(
            db_session,
            site_user_id="204",
            enabled=True,
            mapping_stale_after_seconds=7200,
            now=BASE_TIME + timedelta(minutes=5),
        )
        == "temporarily_unavailable"
    )

    _activate_mapping(
        db_session,
        [
            _linked("201", CP_4),
            SettlementMappingInput("202", None, None, "not_linked"),
            SettlementMappingInput("203", None, None, "ambiguous"),
            SettlementMappingInput("204", None, None, "not_linked"),
        ],
        checked_at=BASE_TIME + timedelta(minutes=30),
    )
    db_session.commit()
    assert summary("202", now=BASE_TIME + timedelta(minutes=35)).status == "not_linked"
    assert summary("204", now=BASE_TIME + timedelta(minutes=35)).status == "not_linked"


def test_summary_rejects_stale_mapping_and_non_pilot(db_session: Session) -> None:
    _activate_mapping(db_session, [_linked("301", CP_1)])
    _activate_balances(db_session, [_balance(CP_1, "10")], synced_at=BASE_TIME + timedelta(hours=3))
    set_pilot_access(db_session, site_user_id="301", enabled=True)
    db_session.commit()

    stale_mapping = get_customer_settlement_summary(
        db_session,
        site_user_id="301",
        enabled=True,
        stale_after_seconds=7200,
        hide_after_seconds=21600,
        mapping_stale_after_seconds=7200,
        now=BASE_TIME + timedelta(hours=3),
    )
    disabled = get_customer_settlement_summary(
        db_session,
        site_user_id="999",
        enabled=True,
        stale_after_seconds=7200,
        hide_after_seconds=21600,
        mapping_stale_after_seconds=7200,
        now=BASE_TIME,
    )
    assert stale_mapping.status == "temporarily_unavailable"
    assert disabled.status == "pilot_disabled"


def test_health_metrics_report_warning_and_critical_without_financial_amounts(
    db_session: Session,
) -> None:
    _activate_mapping(db_session, [_linked("351", CP_1)])
    _activate_balances(db_session, [_balance(CP_1, "99999")])
    set_pilot_access(db_session, site_user_id="351", enabled=True)
    db_session.commit()

    warning = customer_settlement_health_metrics(
        db_session,
        stale_after_seconds=7200,
        hide_after_seconds=21600,
        mapping_stale_after_seconds=7200,
        now=BASE_TIME + timedelta(hours=3),
    )
    critical = customer_settlement_health_metrics(
        db_session,
        stale_after_seconds=7200,
        hide_after_seconds=21600,
        mapping_stale_after_seconds=7200,
        now=BASE_TIME + timedelta(hours=7),
    )

    assert warning["freshness_status"] == "warning"
    assert warning["mapping_status"] == "critical"
    assert warning["loaded_rows"] == 1
    assert warning["zero_rows"] == 0
    assert "amount" not in warning
    assert critical["freshness_status"] == "critical"


def test_health_marks_fresh_but_incomplete_pilot_mapping_critical(
    db_session: Session,
) -> None:
    _activate_mapping(
        db_session,
        [SettlementMappingInput("352", None, None, "not_linked")],
    )
    set_pilot_access(db_session, site_user_id="352", enabled=True)
    db_session.commit()

    metrics = customer_settlement_health_metrics(
        db_session,
        stale_after_seconds=7200,
        hide_after_seconds=21600,
        mapping_stale_after_seconds=7200,
        now=BASE_TIME + timedelta(minutes=5),
    )

    assert metrics["mapping_age_seconds"] == 300
    assert metrics["mapping_status"] == "critical"
    assert metrics["enabled_pilots"] == 1
    assert metrics["linked_pilots"] == 0


def test_health_marks_incompatible_financial_scope_critical(db_session: Session) -> None:
    _activate_mapping(db_session, [_linked("353", CP_2)])
    _activate_balances(db_session, [_balance(CP_1, "10.00")])
    set_pilot_access(db_session, site_user_id="353", enabled=True)
    db_session.commit()

    incompatible = customer_settlement_health_metrics(
        db_session,
        stale_after_seconds=7200,
        hide_after_seconds=21600,
        mapping_stale_after_seconds=7200,
        now=BASE_TIME + timedelta(minutes=5),
    )

    assert incompatible["freshness_status"] == "critical"
    assert incompatible["linked_pilots"] == 1
    assert incompatible["compatible_pilots"] == 0
    assert incompatible["mapping_status"] == "critical"

    _activate_balances(
        db_session,
        [_balance(CP_2, "20.00")],
        as_of=BASE_TIME + timedelta(minutes=10),
        synced_at=BASE_TIME + timedelta(minutes=10),
    )
    db_session.commit()
    compatible = customer_settlement_health_metrics(
        db_session,
        stale_after_seconds=7200,
        hide_after_seconds=21600,
        mapping_stale_after_seconds=7200,
        now=BASE_TIME + timedelta(minutes=15),
    )

    assert compatible["compatible_pilots"] == 1
    assert compatible["mapping_status"] == "ok"


def test_runtime_reads_and_health_reject_unexpected_active_context(db_session: Session) -> None:
    _activate_mapping(db_session, [_linked("354", CP_1)])
    _activate_balances(db_session, [_balance(CP_1, "10.00")])
    set_pilot_access(db_session, site_user_id="354", enabled=True)
    db_session.commit()

    health = customer_settlement_health_metrics(
        db_session,
        stale_after_seconds=7200,
        hide_after_seconds=21600,
        mapping_stale_after_seconds=7200,
        expected_source_mode="unexpected-source-mode",
        expected_mapping_source_name="bitrix_crm_customer_cluster",
        expected_source_system="ut103",
        expected_organization_ref=ORG,
        expected_organization_guid=ORG_GUID,
        now=BASE_TIME + timedelta(minutes=5),
    )
    summary = get_customer_settlement_summary(
        db_session,
        site_user_id="354",
        enabled=True,
        stale_after_seconds=7200,
        hide_after_seconds=21600,
        mapping_stale_after_seconds=7200,
        expected_source_mode="unexpected-source-mode",
        expected_mapping_source_name="bitrix_crm_customer_cluster",
        expected_source_system="ut103",
        expected_organization_ref=ORG,
        expected_organization_guid=ORG_GUID,
        now=BASE_TIME + timedelta(minutes=5),
    )
    eligibility = get_customer_settlement_eligibility(
        db_session,
        site_user_id="354",
        enabled=True,
        mapping_stale_after_seconds=7200,
        expected_mapping_source_name="unexpected-mapping-source",
        expected_source_system="ut103",
        expected_organization_ref=ORG,
        expected_organization_guid=ORG_GUID,
        now=BASE_TIME + timedelta(minutes=5),
    )

    assert health["freshness_status"] == "critical"
    assert health["mapping_status"] == "critical"
    assert summary.status == "temporarily_unavailable"
    assert eligibility == "temporarily_unavailable"


def test_health_rejects_financial_revision_with_extra_counterparties(
    db_session: Session,
) -> None:
    _activate_mapping(db_session, [_linked("356", CP_1)])
    _activate_balances(
        db_session,
        [_balance(CP_1, "10.00"), _balance(CP_2, "20.00")],
    )
    set_pilot_access(db_session, site_user_id="356", enabled=True)
    db_session.commit()

    metrics = customer_settlement_health_metrics(
        db_session,
        stale_after_seconds=7200,
        hide_after_seconds=21600,
        mapping_stale_after_seconds=7200,
        now=BASE_TIME + timedelta(minutes=5),
    )

    assert metrics["pilot_counterparties"] == 1
    assert metrics["expected_rows"] == 2
    assert metrics["freshness_status"] == "critical"
    assert metrics["mapping_status"] == "critical"
    summary = get_customer_settlement_summary(
        db_session,
        site_user_id="356",
        enabled=True,
        stale_after_seconds=7200,
        hide_after_seconds=21600,
        mapping_stale_after_seconds=7200,
        now=BASE_TIME + timedelta(minutes=5),
    )
    assert summary.status == "temporarily_unavailable"
    assert summary.amount is None


def test_health_rejects_actual_financial_rows_that_disagree_with_revision_counts(
    db_session: Session,
) -> None:
    _activate_mapping(db_session, [_linked("358", CP_1)])
    revision = _activate_balances(db_session, [_balance(CP_1, "10.00")])
    set_pilot_access(db_session, site_user_id="358", enabled=True)
    db_session.commit()
    db_session.add(
        CustomerSettlementBalance(
            revision_id=revision.id,
            counterparty_ref=CP_2,
            counterparty_guid=onec_ref_to_guid(CP_2),
            signed_balance=Decimal("20.00"),
            currency="RUB",
        )
    )
    db_session.commit()

    metrics = customer_settlement_health_metrics(
        db_session,
        stale_after_seconds=7200,
        hide_after_seconds=21600,
        mapping_stale_after_seconds=7200,
        now=BASE_TIME + timedelta(minutes=5),
    )
    summary = get_customer_settlement_summary(
        db_session,
        site_user_id="358",
        enabled=True,
        stale_after_seconds=7200,
        hide_after_seconds=21600,
        mapping_stale_after_seconds=7200,
        now=BASE_TIME + timedelta(minutes=5),
    )

    assert metrics["expected_rows"] == 1
    assert metrics["loaded_rows"] == 1
    assert metrics["freshness_status"] == "critical"
    assert metrics["mapping_status"] == "critical"
    assert summary.status == "temporarily_unavailable"


def test_future_revision_timestamps_fail_closed(db_session: Session) -> None:
    mapping = _activate_mapping(db_session, [_linked("357", CP_1)])
    financial = _activate_balances(db_session, [_balance(CP_1, "10.00")])
    set_pilot_access(db_session, site_user_id="357", enabled=True)
    financial.synced_at = BASE_TIME + timedelta(minutes=10)
    db_session.commit()

    summary = get_customer_settlement_summary(
        db_session,
        site_user_id="357",
        enabled=True,
        stale_after_seconds=7200,
        hide_after_seconds=21600,
        mapping_stale_after_seconds=7200,
        now=BASE_TIME + timedelta(minutes=5),
    )
    health = customer_settlement_health_metrics(
        db_session,
        stale_after_seconds=7200,
        hide_after_seconds=21600,
        mapping_stale_after_seconds=7200,
        now=BASE_TIME + timedelta(minutes=5),
    )
    assert summary.status == "temporarily_unavailable"
    assert health["freshness_status"] == "critical"

    financial.synced_at = BASE_TIME
    mapping.source_checked_at = BASE_TIME + timedelta(minutes=10)
    db_session.commit()
    assert (
        get_customer_settlement_eligibility(
            db_session,
            site_user_id="357",
            enabled=True,
            mapping_stale_after_seconds=7200,
            now=BASE_TIME + timedelta(minutes=5),
        )
        == "temporarily_unavailable"
    )
    assert (
        get_customer_settlement_summary(
            db_session,
            site_user_id="357",
            enabled=True,
            stale_after_seconds=7200,
            hide_after_seconds=21600,
            mapping_stale_after_seconds=7200,
            now=BASE_TIME + timedelta(minutes=5),
        ).status
        == "temporarily_unavailable"
    )

    mapping.source_checked_at = BASE_TIME
    financial.as_of = BASE_TIME + timedelta(minutes=10)
    financial.source_db_time = BASE_TIME
    db_session.commit()
    assert (
        get_customer_settlement_summary(
            db_session,
            site_user_id="357",
            enabled=True,
            stale_after_seconds=7200,
            hide_after_seconds=21600,
            mapping_stale_after_seconds=7200,
            now=BASE_TIME + timedelta(minutes=15),
        ).status
        == "temporarily_unavailable"
    )
    assert (
        customer_settlement_health_metrics(
            db_session,
            stale_after_seconds=7200,
            hide_after_seconds=21600,
            mapping_stale_after_seconds=7200,
            now=BASE_TIME + timedelta(minutes=15),
        )["freshness_status"]
        == "critical"
    )

    financial.as_of = BASE_TIME + timedelta(minutes=20)
    financial.source_db_time = BASE_TIME + timedelta(minutes=20)
    db_session.commit()
    assert (
        get_customer_settlement_summary(
            db_session,
            site_user_id="357",
            enabled=True,
            stale_after_seconds=7200,
            hide_after_seconds=21600,
            mapping_stale_after_seconds=7200,
            now=BASE_TIME + timedelta(minutes=15),
        ).status
        == "temporarily_unavailable"
    )
    assert (
        customer_settlement_health_metrics(
            db_session,
            stale_after_seconds=7200,
            hide_after_seconds=21600,
            mapping_stale_after_seconds=7200,
            now=BASE_TIME + timedelta(minutes=15),
        )["freshness_status"]
        == "critical"
    )


def test_health_fails_closed_when_context_lock_is_busy(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settlement_services,
        "try_customer_settlement_context_read_lock",
        lambda _session: False,
    )

    metrics = customer_settlement_health_metrics(
        db_session,
        stale_after_seconds=7200,
        hide_after_seconds=21600,
        mapping_stale_after_seconds=7200,
        now=BASE_TIME,
    )

    assert metrics["freshness_status"] == "critical"
    assert metrics["mapping_status"] == "critical"
    assert metrics["compatible_pilots"] == 0
    assert metrics["context_stable"] is False


def test_customer_reads_require_validated_source_gate(db_session: Session) -> None:
    summary = get_customer_settlement_summary(
        db_session,
        site_user_id="355",
        enabled=True,
        source_validated=False,
        stale_after_seconds=7200,
        hide_after_seconds=21600,
        mapping_stale_after_seconds=7200,
        now=BASE_TIME,
    )
    eligibility = get_customer_settlement_eligibility(
        db_session,
        site_user_id="355",
        enabled=True,
        source_validated=False,
        mapping_stale_after_seconds=7200,
        now=BASE_TIME,
    )

    assert summary.status == "temporarily_unavailable"
    assert eligibility == "temporarily_unavailable"


def test_customer_reads_fail_closed_when_context_lock_is_busy(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settlement_services,
        "try_customer_settlement_context_read_lock",
        lambda _session: False,
    )

    summary = get_customer_settlement_summary(
        db_session,
        site_user_id="355",
        enabled=True,
        stale_after_seconds=7200,
        hide_after_seconds=21600,
        mapping_stale_after_seconds=7200,
        now=BASE_TIME,
    )
    eligibility = get_customer_settlement_eligibility(
        db_session,
        site_user_id="355",
        enabled=True,
        mapping_stale_after_seconds=7200,
        now=BASE_TIME,
    )

    assert summary.status == "temporarily_unavailable"
    assert summary.amount is None
    assert eligibility == "temporarily_unavailable"


def test_health_rechecks_active_revision_ids_before_return(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _activate_mapping(db_session, [_linked("354", CP_1)])
    _activate_balances(db_session, [_balance(CP_1, "10.00")])
    set_pilot_access(db_session, site_user_id="354", enabled=True)
    db_session.commit()
    original_scalar = db_session.scalar

    def changed_financial_id(statement, *args, **kwargs):
        value = original_scalar(statement, *args, **kwargs)
        if "SELECT customer_settlement_revision.id" in str(statement) and value is not None:
            return int(value) + 1000
        return value

    monkeypatch.setattr(db_session, "scalar", changed_financial_id)
    metrics = customer_settlement_health_metrics(
        db_session,
        stale_after_seconds=7200,
        hide_after_seconds=21600,
        mapping_stale_after_seconds=7200,
        now=BASE_TIME + timedelta(minutes=5),
    )

    assert metrics["freshness_status"] == "critical"
    assert metrics["mapping_status"] == "critical"
    assert metrics["compatible_pilots"] == 0
    assert metrics["context_stable"] is False


def test_retention_removes_only_old_non_active_revisions_and_expired_jti(
    db_session: Session,
) -> None:
    old_time = BASE_TIME - timedelta(days=40)
    first = _activate_balances(db_session, [_balance(CP_1, "1")], as_of=old_time)
    active = _activate_balances(db_session, [_balance(CP_1, "2")], as_of=BASE_TIME)
    failed = mark_financial_revision_failed(
        db_session,
        organization_ref=ORG,
        as_of=old_time,
        source_mode="synthetic-test",
        error_code="test",
    )
    first.created_at = old_time
    active.created_at = old_time
    failed.created_at = old_time

    old_mapping, _ = activate_mapping_revision(
        db_session,
        entries=[_linked("401", CP_1)],
        source_checked_at=old_time,
    )
    active_mapping, _ = activate_mapping_revision(
        db_session,
        entries=[_linked("402", CP_1)],
        source_checked_at=BASE_TIME,
    )
    old_mapping.created_at = old_time
    active_mapping.created_at = old_time
    old_mapping_id = old_mapping.id
    db_session.add_all(
        [
            CustomerSettlementAssertionJti(
                jti_hash="a" * 64,
                expires_at=BASE_TIME - timedelta(hours=25),
                consumed_at=old_time,
            ),
            CustomerSettlementAssertionJti(
                jti_hash="b" * 64,
                expires_at=BASE_TIME,
                consumed_at=BASE_TIME,
            ),
            CustomerSettlementReconciliationRun(
                report_date=date(2026, 6, 19),
                as_of=old_time,
                report_hash="c" * 64,
                status="matched",
                expected_count=10,
                matched_count=10,
                mismatch_count=0,
                max_abs_difference=Decimal("0.00"),
                created_at=BASE_TIME,
            ),
            CustomerSettlementReconciliationRun(
                report_date=date(2026, 6, 20),
                as_of=old_time,
                report_hash="e" * 64,
                status="mismatched",
                expected_count=1,
                matched_count=0,
                mismatch_count=1,
                max_abs_difference=Decimal("0.02"),
                created_at=old_time,
            ),
            CustomerSettlementAlertOutbox(
                event_key="d" * 64,
                status="sent",
                severity="warning",
                message="synthetic safe alert",
                attempt_count=1,
                next_attempt_at=old_time,
                sent_at=old_time,
                created_at=old_time,
                updated_at=old_time,
            ),
        ]
    )
    db_session.commit()

    result = cleanup_customer_settlements(
        db_session,
        successful_retention_days=30,
        failed_retention_days=7,
        jti_retention_hours=24,
        now=BASE_TIME,
    )
    db_session.commit()

    assert result["financial_revisions"] == 2
    assert result["reconciliation_runs"] == 2
    assert result["alert_outbox"] == 1
    assert db_session.get(CustomerSettlementRevision, active.id) is not None
    assert db_session.get(CustomerSettlementMappingRevision, active_mapping.id) is not None
    assert db_session.get(CustomerSettlementMappingRevision, old_mapping_id) is None
    assert db_session.scalar(select(func.count()).select_from(CustomerSettlementAssertionJti)) == 1
    assert (
        db_session.scalar(select(func.count()).select_from(CustomerSettlementReconciliationRun))
        == 0
    )
    assert db_session.scalar(select(func.count()).select_from(CustomerSettlementAlertOutbox)) == 0


@pytest.mark.parametrize(
    ("successful_days", "failed_days", "jti_hours"),
    ((29, 7, 24), (30, 6, 24), (30, 7, 23), (30, 7, -1)),
)
def test_cleanup_rejects_unsafe_retention_configuration_before_database_access(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    successful_days: int,
    failed_days: int,
    jti_hours: int,
) -> None:
    monkeypatch.setattr(
        settlement_services,
        "_require_customer_settlement_context_lock",
        lambda _session: pytest.fail("invalid retention must fail before database access"),
    )

    with pytest.raises(
        ValueError,
        match="customer_settlement_retention_configuration_invalid",
    ):
        cleanup_customer_settlements(
            db_session,
            successful_retention_days=successful_days,
            failed_retention_days=failed_days,
            jti_retention_hours=jti_hours,
            now=BASE_TIME,
        )
