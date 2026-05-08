from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, CounterpartyDuplicateCase
from app.services.counterparty_duplicates import (
    DELIVERY_ACKED,
    STATUS_CLOSED,
    CounterpartySnapshotRecord,
    acknowledge_counterparty_duplicate_case,
    detect_counterparty_duplicate_cases,
    normalize_email,
    normalize_phone,
    normalize_tax_id,
    upsert_counterparty_duplicate_cases,
)


def _record(
    ref: str,
    *,
    phone: str | None = None,
    email: str | None = None,
    tax_id: str | None = None,
    name: str | None = None,
) -> CounterpartySnapshotRecord:
    return CounterpartySnapshotRecord.from_mapping(
        {
            "counterparty_ref": ref,
            "counterparty_name": name or ref,
            "phone": phone,
            "email": email,
            "tax_id": tax_id,
            "responsible_code": "finance",
            "updated_at": "2026-03-24T10:00:00",
        }
    )


def test_normalization_helpers() -> None:
    assert normalize_phone("+7 (777) 123-45-67") == "+77771234567"
    assert normalize_phone("8 (777) 123-45-67") == "+77771234567"
    assert normalize_email(" Foo@Bar.Com ") == "foo@bar.com"
    assert normalize_tax_id("12-345 678 901") == "12345678901"


def test_detect_counterparty_duplicate_cases_by_phone_and_email() -> None:
    records = [
        _record("cp-1", phone="+7 777 1234567", email="A@x.test"),
        _record("cp-2", phone="8 777 123 45 67", email="b@x.test"),
        _record("cp-3", email="A@x.test"),
    ]

    detected = detect_counterparty_duplicate_cases(records)

    assert len(detected) == 1
    assert detected[0].risk_level == "P1"
    assert detected[0].reason_codes == ["email", "phone"]
    assert len(detected[0].candidate_records) == 3


def test_upsert_counterparty_duplicate_cases_keeps_acked_case_without_reopen() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    detected_at = datetime(2026, 3, 24, 12, 0, 0)
    payload = detect_counterparty_duplicate_cases(
        [
            _record("cp-1", phone="+7 777 1234567"),
            _record("cp-2", phone="+7 777 1234567"),
        ]
    )

    with Session(engine) as session:
        first = upsert_counterparty_duplicate_cases(
            session,
            payload,
            detected_at=detected_at,
            anti_duplicate_window_hours=24,
        )
        session.commit()
        assert len(first["new"]) == 1
        row = session.query(CounterpartyDuplicateCase).one()

        acknowledge_counterparty_duplicate_case(
            session,
            case_id=row.id,
            external_case_id="sp-1",
            external_status="Закрыт",
            status=STATUS_CLOSED,
        )
        session.commit()

        second = upsert_counterparty_duplicate_cases(
            session,
            payload,
            detected_at=detected_at + timedelta(hours=1),
            anti_duplicate_window_hours=24,
        )
        session.commit()

        assert second["pending"] == []
        session.refresh(row)
        assert row.delivery_state == DELIVERY_ACKED
        assert row.status == STATUS_CLOSED


def test_upsert_counterparty_duplicate_cases_reopens_when_source_changes() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        first = detect_counterparty_duplicate_cases(
            [
                _record("cp-1", phone="+7 777 1234567"),
                _record("cp-2", phone="+7 777 1234567"),
            ]
        )
        result = upsert_counterparty_duplicate_cases(
            session,
            first,
            detected_at=datetime(2026, 3, 24, 12, 0, 0),
            anti_duplicate_window_hours=24,
        )
        row = result["new"][0]
        acknowledge_counterparty_duplicate_case(
            session,
            case_id=row.id,
            external_case_id="sp-1",
            status=STATUS_CLOSED,
        )
        session.commit()

        changed = detect_counterparty_duplicate_cases(
            [
                _record("cp-1", phone="+7 777 1234567"),
                _record("cp-2", phone="+7 777 1234567"),
                _record("cp-3", phone="+7 777 1234567"),
            ]
        )
        result2 = upsert_counterparty_duplicate_cases(
            session,
            changed,
            detected_at=datetime(2026, 3, 24, 15, 0, 0),
            anti_duplicate_window_hours=24,
        )
        session.commit()

        assert len(result2["pending"]) == 1
