from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.customer_settlement import (
    CustomerSettlementAssertionJti,
    CustomerSettlementBalance,
    CustomerSettlementMappingRevision,
    CustomerSettlementRevision,
)
from app.services.customer_settlements import (
    SettlementBalanceInput,
    SettlementMappingInput,
    activate_financial_revision,
    activate_mapping_revision,
    cleanup_customer_settlements,
    get_customer_settlement_summary,
    mark_financial_revision_failed,
    normalize_money,
    set_pilot_access,
    settlement_state,
)

ORG = "0x" + "a" * 32
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
) -> None:
    revision, activated = activate_mapping_revision(
        session,
        entries=entries,
        source_checked_at=checked_at,
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


def test_zero_balance_is_stored_and_new_revision_atomically_supersedes_old(
    db_session: Session,
) -> None:
    first = _activate_balances(db_session, [_balance(CP_1, "0")])
    db_session.commit()
    stored = db_session.scalar(
        select(CustomerSettlementBalance).where(
            CustomerSettlementBalance.revision_id == first.id
        )
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
    assert (
        db_session.scalar(
            select(func.count()).select_from(CustomerSettlementRevision)
        )
        == 2
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
    assert hidden.amount is None
    assert hidden.state is None


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
    assert db_session.get(CustomerSettlementRevision, active.id) is not None
    assert db_session.get(CustomerSettlementMappingRevision, active_mapping.id) is not None
    assert db_session.get(CustomerSettlementMappingRevision, old_mapping.id) is None
    assert (
        db_session.scalar(
            select(func.count()).select_from(CustomerSettlementAssertionJti)
        )
        == 1
    )
