from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Iterator

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.models.assortment_lifecycle_signal import AssortmentLifecycleSignal
from app.services.assortment_lifecycle_signals import (
    AssortmentLifecycleSignalConflict,
    AssortmentLifecycleSignalError,
    AssortmentLifecycleSignalInput,
    append_assortment_lifecycle_signal,
    list_assortment_lifecycle_signals_as_of,
)


@pytest.fixture()
def signal_session() -> Iterator[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    AssortmentLifecycleSignal.__table__.create(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    engine.dispose()


def _signal(**overrides: object) -> AssortmentLifecycleSignalInput:
    values: dict[str, object] = {
        "signal_type": "site_order",
        "source": "site",
        "source_event_id": "order-1001",
        "occurred_at": datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
        "available_at": datetime(2026, 8, 17, 9, 1, tzinfo=UTC),
        "reliability": Decimal("0.9500"),
        "reliability_reason": "validated_site_event",
        "nomenclature_code": "SKU-17PM-001",
        "quantity": Decimal("1"),
        "payload": {"customer_key": "customer-1"},
    }
    values.update(overrides)
    return AssortmentLifecycleSignalInput(**values)  # type: ignore[arg-type]


def test_append_is_idempotent_for_the_same_signal(signal_session: Session) -> None:
    first = append_assortment_lifecycle_signal(signal_session, _signal())
    second = append_assortment_lifecycle_signal(signal_session, _signal())

    assert first.created is True
    assert second.created is False
    assert second.signal.id == first.signal.id
    assert signal_session.scalar(select(func.count()).select_from(AssortmentLifecycleSignal)) == 1


def test_same_identity_with_different_content_fails_closed(
    signal_session: Session,
) -> None:
    append_assortment_lifecycle_signal(signal_session, _signal())

    with pytest.raises(
        AssortmentLifecycleSignalConflict,
        match="signal_identity_exists_with_different_payload",
    ):
        append_assortment_lifecycle_signal(
            signal_session,
            _signal(payload={"customer_key": "customer-2"}),
        )

    with pytest.raises(
        AssortmentLifecycleSignalConflict,
        match="signal_identity_exists_with_different_payload",
    ):
        append_assortment_lifecycle_signal(
            signal_session,
            _signal(
                occurred_at=datetime(2026, 8, 17, 9, 2, tzinfo=UTC),
                available_at=datetime(2026, 8, 17, 9, 3, tzinfo=UTC),
            ),
        )


def test_late_signal_is_hidden_until_its_available_at_cutoff(
    signal_session: Session,
) -> None:
    append_assortment_lifecycle_signal(
        signal_session,
        _signal(
            occurred_at=datetime(2026, 8, 15, 12, 0, tzinfo=UTC),
            available_at=datetime(2026, 8, 17, 12, 0, tzinfo=UTC),
        ),
    )

    before_availability = list_assortment_lifecycle_signals_as_of(
        signal_session,
        datetime(2026, 8, 17, 11, 59, tzinfo=UTC),
    )
    at_availability = list_assortment_lifecycle_signals_as_of(
        signal_session,
        datetime(2026, 8, 17, 12, 0, tzinfo=UTC),
    )

    assert before_availability == []
    assert [signal.source_event_id for signal in at_availability] == ["order-1001"]


def test_site_order_and_cart_are_distinct_signal_types(
    signal_session: Session,
) -> None:
    order = append_assortment_lifecycle_signal(
        signal_session,
        _signal(signal_type="site_order"),
    )
    cart = append_assortment_lifecycle_signal(
        signal_session,
        _signal(signal_type="site_cart"),
    )

    assert order.created is True
    assert cart.created is True
    assert order.signal.signal_key != cart.signal.signal_key
    stored_types = set(signal_session.scalars(select(AssortmentLifecycleSignal.signal_type)).all())
    assert stored_types == {"site_order", "site_cart"}


def test_wordstat_cannot_carry_quantity(signal_session: Session) -> None:
    with pytest.raises(AssortmentLifecycleSignalError, match="wordstat_quantity_forbidden"):
        append_assortment_lifecycle_signal(
            signal_session,
            _signal(
                signal_type="wordstat_direction",
                source="wordstat",
                direction="up",
                quantity=Decimal("1"),
            ),
        )


def test_signal_requires_sku_or_versioned_family_link(signal_session: Session) -> None:
    with pytest.raises(AssortmentLifecycleSignalError, match="sku_or_family_link_required"):
        append_assortment_lifecycle_signal(
            signal_session,
            _signal(nomenclature_code=None),
        )

    with pytest.raises(
        AssortmentLifecycleSignalError,
        match="display_family_registry_version_required",
    ):
        append_assortment_lifecycle_signal(
            signal_session,
            _signal(nomenclature_code=None, display_family_key="iphone-17-pro-max"),
        )


def test_naive_signal_datetime_is_rejected(signal_session: Session) -> None:
    with pytest.raises(
        AssortmentLifecycleSignalError,
        match="occurred_at_must_be_timezone_aware",
    ):
        append_assortment_lifecycle_signal(
            signal_session,
            _signal(occurred_at=datetime(2026, 8, 17, 9, 0)),
        )


def test_orm_update_and_delete_are_rejected(signal_session: Session) -> None:
    stored = append_assortment_lifecycle_signal(signal_session, _signal()).signal
    signal_session.commit()
    stored_id = stored.id

    stored.reliability_reason = "changed"
    with pytest.raises(RuntimeError, match="assortment_lifecycle_signal_is_append_only"):
        signal_session.flush()
    signal_session.rollback()

    stored = signal_session.get(AssortmentLifecycleSignal, stored_id)
    assert stored is not None
    signal_session.delete(stored)
    with pytest.raises(RuntimeError, match="assortment_lifecycle_signal_is_append_only"):
        signal_session.flush()
    signal_session.rollback()

    assert signal_session.get(AssortmentLifecycleSignal, stored_id) is not None
