"""Application orchestration for customer price-type calculation and reads."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path
from threading import Lock
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.domains.customer_price_types import (
    CustomerPriceTypeAccessScope,
    CustomerPriceTypeFacts,
    CustomerPriceTypeRulesEngine,
    build_default_run_key,
    build_source_fingerprint,
    load_price_type_ruleset,
    normalize_counterparty_ref,
)
from app.infrastructure.customer_price_types import (
    CustomerPriceTypePersistenceConflict,
    SqlAlchemyCustomerPriceTypeRepository,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RULESET_PATH = REPO_ROOT / "config/price_types/ruleset.yaml"
_PERSISTENCE_LOCKS_GUARD = Lock()
_PERSISTENCE_LOCKS: dict[date, Lock] = {}


@dataclass(frozen=True, slots=True)
class CustomerPriceTypeRunResult:
    run_id: int
    run_key: str
    status: str
    source_fingerprint: str
    created: bool


def _add_months(value: date, months: int) -> date:
    total = value.year * 12 + value.month - 1 + months
    return date(total // 12, total % 12 + 1, 1)


def _month_end(value: date) -> date:
    return _add_months(value, 1) - timedelta(days=1)


def _persistence_lock(snapshot_month: date) -> Lock:
    with _PERSISTENCE_LOCKS_GUARD:
        return _PERSISTENCE_LOCKS.setdefault(snapshot_month, Lock())


def internal_customer_price_type_scope(actor: str = "internal") -> CustomerPriceTypeAccessScope:
    return CustomerPriceTypeAccessScope(
        actor=actor,
        role="internal",
        can_view_money=True,
    )


class CustomerPriceTypeRunService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        ruleset_path: Path = DEFAULT_RULESET_PATH,
    ) -> None:
        self.session_factory = session_factory
        self.ruleset = load_price_type_ruleset(ruleset_path)
        self.engine = CustomerPriceTypeRulesEngine(self.ruleset)

    def execute(
        self,
        facts: list[CustomerPriceTypeFacts],
        *,
        source_statuses: dict[str, str],
        run_key: str | None = None,
    ) -> CustomerPriceTypeRunResult:
        if not facts:
            raise ValueError("at least one customer fact is required")
        normalized = sorted(
            (
                replace(item, counterparty_ref=normalize_counterparty_ref(item.counterparty_ref))
                for item in facts
            ),
            key=lambda item: item.counterparty_ref,
        )
        months = {item.snapshot_month for item in normalized}
        if len(months) != 1:
            raise ValueError("all facts in a run must use the same snapshot_month")
        snapshot_month = next(iter(months))
        fingerprint = build_source_fingerprint(normalized, source_statuses=source_statuses)
        resolved_run_key = run_key or build_default_run_key(
            snapshot_month=snapshot_month,
            ruleset_version=self.ruleset.version,
            source_fingerprint=fingerprint,
        )
        created_run = False

        with self.session_factory() as session:
            repository = SqlAlchemyCustomerPriceTypeRepository(session)
            existing = repository.get_run_by_key(resolved_run_key)
            if existing is not None:
                if existing.source_fingerprint != fingerprint:
                    raise CustomerPriceTypePersistenceConflict(
                        "run_key is already bound to a different source fingerprint"
                    )
                if existing.status != "started":
                    return CustomerPriceTypeRunResult(
                        run_id=existing.id,
                        run_key=existing.run_key,
                        status=existing.status,
                        source_fingerprint=existing.source_fingerprint,
                        created=False,
                    )
                run_id = existing.id
            else:
                try:
                    row = repository.create_run(
                        run_key=resolved_run_key,
                        snapshot_month=snapshot_month,
                        ruleset_version=self.ruleset.version,
                        as_of=_month_end(snapshot_month),
                        window_start=_add_months(snapshot_month, -2),
                        window_end=_add_months(snapshot_month, 1),
                        source_statuses=source_statuses,
                        source_fingerprint=fingerprint,
                        input_count=len(normalized),
                    )
                    run_id = row.id
                    session.commit()
                    created_run = True
                except IntegrityError as exc:
                    session.rollback()
                    existing = repository.get_run_by_key(resolved_run_key)
                    if existing is None:
                        raise
                    if existing.source_fingerprint != fingerprint:
                        raise CustomerPriceTypePersistenceConflict(
                            "run_key is already bound to a different source fingerprint"
                        ) from exc
                    if existing.status != "started":
                        return CustomerPriceTypeRunResult(
                            run_id=existing.id,
                            run_key=existing.run_key,
                            status=existing.status,
                            source_fingerprint=existing.source_fingerprint,
                            created=False,
                        )
                    run_id = existing.id

        try:
            decisions = [self.engine.evaluate(item) for item in normalized]
            with _persistence_lock(snapshot_month):
                with self.session_factory() as session:
                    repository = SqlAlchemyCustomerPriceTypeRepository(session)
                    row = repository.get_run(run_id)
                    if row is None:
                        raise RuntimeError("created calculation run disappeared")
                    if row.status != "started":
                        return CustomerPriceTypeRunResult(
                            run_id=row.id,
                            run_key=row.run_key,
                            status=row.status,
                            source_fingerprint=row.source_fingerprint,
                            created=False,
                        )
                    repository.persist_results(run=row, facts=normalized, decisions=decisions)
                    session.commit()
                    return CustomerPriceTypeRunResult(
                        run_id=row.id,
                        run_key=row.run_key,
                        status=row.status,
                        source_fingerprint=row.source_fingerprint,
                        created=created_run,
                    )
        except Exception as exc:
            with self.session_factory() as session:
                repository = SqlAlchemyCustomerPriceTypeRepository(session)
                repository.mark_failed(run_id, str(exc))
                session.commit()
            raise


class CustomerPriceTypeReadService:
    def __init__(self, session: Session) -> None:
        self.repository = SqlAlchemyCustomerPriceTypeRepository(session)

    def summary(
        self,
        *,
        snapshot_month: date | None,
        access: CustomerPriceTypeAccessScope,
    ) -> dict[str, Any]:
        run = self.repository.latest_run(snapshot_month)
        if run is None:
            return self._missing(snapshot_month)
        return {
            **self._run_envelope(run),
            "summary": self.repository.summary(run=run, access=access),
        }

    def worklists(
        self,
        *,
        snapshot_month: date | None,
        access: CustomerPriceTypeAccessScope,
    ) -> dict[str, Any]:
        run = self.repository.latest_run(snapshot_month)
        if run is None:
            return {**self._missing(snapshot_month), "worklists": {}}
        return {
            **self._run_envelope(run),
            "worklists": self.repository.worklists(
                run=run,
                access=access,
            ),
        }

    @staticmethod
    def _run_envelope(run: Any) -> dict[str, Any]:
        return {
            "run_id": run.id,
            "snapshot_month": run.snapshot_month,
            "ruleset_version": run.ruleset_version,
            "source_status": run.status,
        }

    @staticmethod
    def _missing(snapshot_month: date | None) -> dict[str, Any]:
        return {
            "run_id": None,
            "snapshot_month": snapshot_month,
            "ruleset_version": None,
            "source_status": "missing",
            "summary": {
                "profile_count": 0,
                "actionable_count": 0,
                "levels": {},
                "recommendations": {},
                "source_statuses": {},
                "review_types": {},
                "departments": {},
            },
        }
