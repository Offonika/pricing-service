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
) -> None:
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
    for user_id in ("201", "202", "203", "204"):
        set_pilot_access(db_session, site_user_id=user_id, enabled=True)
    db_session.commit()

    def summary(user_id: str):
        return get_customer_settlement_summary(
            db_session,
            site_user_id=user_id,
            enabled=True,
            stale_after_seconds=7200,
            hide_after_seconds=21600,
            mapping_stale_after_seconds=7200,
            now=BASE_TIME + timedelta(minutes=5),
        )

    assert summary("201").status == "temporarily_unavailable"
    assert summary("202").status == "not_linked"
    assert summary("203").status == "ambiguous_link"
    assert summary("204").status == "not_linked"

    _activate_mapping(db_session, [], checked_at=BASE_TIME + timedelta(minutes=30))
    db_session.commit()
    assert summary("202").status == "not_linked"


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
    assert result["reconciliation_runs"] == 1
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
