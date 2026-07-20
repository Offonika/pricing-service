"""Application orchestration for customer price-type calculation and reads."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
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


class CustomerPriceTypeQualityConflict(RuntimeError):
    """Raised when an expert review is based on a stale sample version."""


class CustomerPriceTypeQualityService:
    GROUPS = (
        "manager_work",
        "isolate",
        "recovery",
        "data_check",
        "special_review",
        "downgrade_approval",
        "no_action",
    )
    READ_ROLES = {"internal", "network_head", "quality"}
    PREPARE_ROLES = {"internal", "network_head"}

    def __init__(self, session: Session) -> None:
        self.session = session
        self.repository = SqlAlchemyCustomerPriceTypeRepository(session)

    def _require_read(self, access: CustomerPriceTypeAccessScope) -> None:
        if access.role not in self.READ_ROLES:
            raise PermissionError("quality review access denied")

    def resolve_run(self, snapshot_month: date | None) -> Any:
        return self.repository.latest_run(snapshot_month)

    def prepare(
        self,
        *,
        snapshot_month: date | None,
        per_group: int,
        access: CustomerPriceTypeAccessScope,
    ) -> dict[str, Any]:
        if access.role not in self.PREPARE_ROLES:
            raise PermissionError("quality sample preparation access denied")
        run = self.resolve_run(snapshot_month)
        if run is None:
            raise LookupError("customer price-type run not found")
        created, total = self.repository.prepare_quality_samples(
            run=run,
            actor=access.actor,
            per_group=per_group,
        )
        self.session.commit()
        return {
            **CustomerPriceTypeReadService._run_envelope(run),
            "created": created,
            "total": total,
            "per_group": per_group,
        }

    def review(
        self,
        *,
        sample_id: int,
        correct_group: str,
        comment: str | None,
        expected_version: int,
        access: CustomerPriceTypeAccessScope,
    ) -> Any:
        self._require_read(access)
        reviewed_at = datetime.now(UTC).replace(tzinfo=None)
        updated = self.repository.update_quality_sample_review(
            sample_id=sample_id,
            correct_group=correct_group,
            comment=comment.strip() if comment and comment.strip() else None,
            reviewed_by=access.actor,
            reviewed_at=reviewed_at,
            expected_version=expected_version,
            access=access,
        )
        if not updated:
            row = self.repository.get_quality_sample(sample_id, access)
            if row is None:
                raise LookupError("quality sample not found")
            raise CustomerPriceTypeQualityConflict("quality sample version is stale")
        self.session.commit()
        return self.repository.get_quality_sample(sample_id, access)

    def metrics(
        self,
        *,
        snapshot_month: date | None,
        access: CustomerPriceTypeAccessScope,
    ) -> dict[str, Any]:
        self._require_read(access)
        metrics_scope = "special_review_only" if access.role == "quality" else "portfolio"
        run = self.resolve_run(snapshot_month)
        if run is None:
            return {
                "run_id": None,
                "snapshot_month": snapshot_month,
                "ruleset_version": None,
                "source_status": "missing",
                "metrics_scope": metrics_scope,
                "metrics_ready": False,
                "population_count": 0,
                "selected_count": 0,
                "reviewed_count": 0,
                "coverage": 0.0,
                "override_rate": 0.0,
                "critical_false_downgrade_count": 0,
                "groups": {},
                "matrix": {},
            }
        rows = self.repository.quality_samples_for_metrics(run_id=run.id, access=access)
        population_counts = self.repository.quality_population_counts(run_id=run.id, access=access)
        selected_count = len(rows)
        reviewed = [(sample, snapshot) for sample, snapshot in rows if sample.status == "reviewed"]
        reviewed_count = len(reviewed)
        selected_by_group = {
            group: sum(sample.system_group == group for sample, _ in rows) for group in self.GROUPS
        }
        metrics_ready = (
            selected_count > 0
            and reviewed_count == selected_count
            and all(
                population_counts.get(group, 0) == 0 or selected_by_group[group] > 0
                for group in self.GROUPS
            )
        )

        def sample_weight(system_group: str) -> float:
            sampled = selected_by_group.get(system_group, 0)
            return population_counts.get(system_group, 0) / sampled if sampled else 0.0

        matrix: dict[str, dict[str, int]] = {}
        for sample, _ in reviewed:
            truth = str(sample.correct_group)
            predicted = sample.system_group
            bucket = matrix.setdefault(predicted, {})
            bucket[truth] = bucket.get(truth, 0) + 1

        groups: dict[str, dict[str, int | float]] = {}
        for group in self.GROUPS:
            true_positive = sum(
                sample.system_group == group and sample.correct_group == group
                for sample, _ in reviewed
            )
            false_positive = sum(
                sample.system_group == group and sample.correct_group != group
                for sample, _ in reviewed
            )
            false_negative = sum(
                sample.system_group != group and sample.correct_group == group
                for sample, _ in reviewed
            )
            predicted_count = true_positive + false_positive
            weighted_true_positive = sum(
                sample_weight(sample.system_group)
                for sample, _ in reviewed
                if sample.system_group == group and sample.correct_group == group
            )
            weighted_false_negative = sum(
                sample_weight(sample.system_group)
                for sample, _ in reviewed
                if sample.system_group != group and sample.correct_group == group
            )
            weighted_actual_count = weighted_true_positive + weighted_false_negative
            groups[group] = {
                "population_count": population_counts.get(group, 0),
                "selected_count": selected_by_group[group],
                "reviewed_count": sum(sample.system_group == group for sample, _ in reviewed),
                "true_positive": true_positive,
                "false_positive": false_positive,
                "false_negative": false_negative,
                "precision": round(true_positive / predicted_count, 4) if predicted_count else None,
                "recall": (
                    round(weighted_true_positive / weighted_actual_count, 4)
                    if metrics_scope == "portfolio" and weighted_actual_count
                    else None
                ),
            }
        critical_false_downgrades = sum(
            1
            for sample, snapshot in reviewed
            if snapshot.recommended_price_type is not None
            and snapshot.recommended_price_type != snapshot.current_price_type
            and sample.correct_group == "no_action"
        )
        overrides = sum(sample.correct_group != sample.system_group for sample, _ in reviewed)
        return {
            **CustomerPriceTypeReadService._run_envelope(run),
            "metrics_scope": metrics_scope,
            "metrics_ready": metrics_ready,
            "population_count": sum(population_counts.values()),
            "selected_count": selected_count,
            "reviewed_count": reviewed_count,
            "coverage": round(reviewed_count / selected_count, 4) if selected_count else 0.0,
            "override_rate": round(overrides / reviewed_count, 4) if reviewed_count else 0.0,
            "critical_false_downgrade_count": critical_false_downgrades,
            "groups": groups,
            "matrix": matrix,
        }
