from __future__ import annotations

from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base
from app.services.management_rules import (
    RULE_RECEIVABLE_OVERDUE,
    RULE_STAFFING_DEFICIT,
    build_management_task_payloads,
)
from app.services.receivables import OneCReceivableLedgerExtractor, sync_receivable_ledger
from app.services.staffing import sync_staffing_data
from tests.test_receivables import NORMALIZED_SQL, _setup_onec_source
from tests.test_staffing import _fact_rows, _plan_rows, _staff_rows


def test_build_management_task_payloads_covers_receivables_and_staffing() -> None:
    app_engine = create_engine("sqlite:///:memory:")
    onec_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(app_engine)
    _setup_onec_source(onec_engine)

    extractor = OneCReceivableLedgerExtractor(onec_engine, operations_sql=NORMALIZED_SQL)
    events = extractor.fetch_receivable_events()

    with Session(app_engine) as session:
        sync_receivable_ledger(
            session,
            events,
            snapshot_date=date(2026, 3, 20),
            employee_counterparty_refs=["cp-b"],
            fired_manager_refs=["mgr-4"],
        )
        sync_staffing_data(
            session,
            staff_members=_staff_rows(),
            shift_plans=_plan_rows(),
            shift_facts=_fact_rows(),
            snapshot_dates=[date(2026, 3, 20)],
        )
        session.commit()

        payloads = build_management_task_payloads(session, as_of=date(2026, 3, 20))

        rule_codes = {item["rule_code"] for item in payloads}
        assert RULE_RECEIVABLE_OVERDUE in rule_codes
        assert RULE_STAFFING_DEFICIT in rule_codes

        assert "receivable_new_daily" not in rule_codes
        assert "receivable_adjustment_candidate" not in rule_codes

        overdue_refs = {
            item["entity_ref"] for item in payloads if item["rule_code"] == RULE_RECEIVABLE_OVERDUE
        }
        assert overdue_refs == {"cp-b", "cp-c"}
        overdue_b = next(
            item
            for item in payloads
            if item["rule_code"] == RULE_RECEIVABLE_OVERDUE and item["entity_ref"] == "cp-b"
        )
        assert overdue_b["metrics"]["payment_term_source"] == "planned_payment_date"
        assert overdue_b["metrics"]["overdue_days"] == 38

        staffing = next(item for item in payloads if item["rule_code"] == RULE_STAFFING_DEFICIT)
        assert staffing["entity_ref"] == "store-1"
        assert staffing["owner_code"] == "retail_supervisor"
        assert staffing["metrics"]["deficit_count"] == 1
