"""SQLAlchemy persistence and scoped reads for customer price types."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import false, func, or_, select
from sqlalchemy.orm import Session

from app.domains.customer_price_types import (
    CustomerPriceTypeAccessScope,
    CustomerPriceTypeDecision,
    CustomerPriceTypeFacts,
    normalize_counterparty_ref,
)
from app.models.customer_price_type import (
    CustomerPriceTypeCase,
    CustomerPriceTypeCaseEvent,
    CustomerPriceTypeProfile,
    CustomerPriceTypeRun,
    CustomerPriceTypeSnapshot,
)

_BATCH_SIZE = 750


class CustomerPriceTypePersistenceConflict(RuntimeError):
    """Raised when an idempotency key is reused for different source facts."""


def _chunks(values: Sequence[str], size: int = _BATCH_SIZE) -> list[Sequence[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _money_map(values: dict[str, Decimal]) -> dict[str, str]:
    return {
        key: format(Decimal(str(value)).quantize(Decimal("0.01")), "f")
        for key, value in values.items()
    }


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class SqlAlchemyCustomerPriceTypeRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_run_by_key(self, run_key: str) -> CustomerPriceTypeRun | None:
        return self.session.scalar(
            select(CustomerPriceTypeRun).where(CustomerPriceTypeRun.run_key == run_key)
        )

    def get_run(self, run_id: int) -> CustomerPriceTypeRun | None:
        return self.session.get(CustomerPriceTypeRun, run_id)

    def create_run(
        self,
        *,
        run_key: str,
        snapshot_month: date,
        ruleset_version: str,
        as_of: date,
        window_start: date,
        window_end: date,
        source_statuses: dict[str, str],
        source_fingerprint: str,
        input_count: int,
    ) -> CustomerPriceTypeRun:
        existing = self.get_run_by_key(run_key)
        if existing is not None:
            if existing.source_fingerprint != source_fingerprint:
                raise CustomerPriceTypePersistenceConflict(
                    "run_key is already bound to a different source fingerprint"
                )
            return existing
        row = CustomerPriceTypeRun(
            run_key=run_key,
            snapshot_month=snapshot_month,
            ruleset_version=ruleset_version,
            as_of=as_of,
            window_start=window_start,
            window_end=window_end,
            source_statuses=dict(source_statuses),
            source_fingerprint=source_fingerprint,
            input_count=input_count,
            excluded_count=0,
            calculated_count=0,
            conflict_count=0,
            actionable_count=0,
            status="started",
        )
        self.session.add(row)
        self.session.flush()
        return row

    def mark_failed(self, run_id: int, error: str) -> None:
        row = self.get_run(run_id)
        if row is None or row.status != "started":
            return
        row.status = "failed"
        row.error_summary = error[:4000]
        row.completed_at = _utcnow()

    def persist_results(
        self,
        *,
        run: CustomerPriceTypeRun,
        facts: Sequence[CustomerPriceTypeFacts],
        decisions: Sequence[CustomerPriceTypeDecision],
    ) -> None:
        if len(facts) != len(decisions):
            raise ValueError("facts and decisions must have the same length")

        self._lock_snapshot_month(run.snapshot_month)
        evaluated = list(zip(facts, decisions, strict=True))
        included = [(fact, decision) for fact, decision in evaluated if not decision.excluded]
        refs = sorted({normalize_counterparty_ref(fact.counterparty_ref) for fact, _ in evaluated})
        profiles: dict[str, CustomerPriceTypeProfile] = {}
        for chunk in _chunks(refs):
            rows = self.session.scalars(
                select(CustomerPriceTypeProfile).where(
                    CustomerPriceTypeProfile.counterparty_ref.in_(chunk)
                )
            ).all()
            profiles.update({row.counterparty_ref: row for row in rows})

        for index, (fact, decision) in enumerate(evaluated, start=1):
            ref = normalize_counterparty_ref(fact.counterparty_ref)
            profile = profiles.get(ref)
            if profile is None:
                profile = CustomerPriceTypeProfile(
                    counterparty_ref=ref,
                    counterparty_code=fact.counterparty_code,
                    counterparty_name=fact.counterparty_name,
                    is_service_card=False,
                    is_hygiene=False,
                    master_data_flags=[],
                )
                self.session.add(profile)
                profiles[ref] = profile
            profile.counterparty_code = fact.counterparty_code
            profile.counterparty_name = fact.counterparty_name
            profile.department_ref = fact.department_ref
            profile.department_name = fact.department_name
            profile.owner_ref = fact.owner_ref
            profile.owner_name = fact.owner_name
            profile.is_service_card = decision.excluded
            profile.is_hygiene = decision.is_hygiene
            master_data_flags = set(fact.master_data_flags)
            if decision.registry_class:
                master_data_flags.add(f"registry:{decision.registry_class}")
            profile.master_data_flags = sorted(master_data_flags)
            if index % _BATCH_SIZE == 0:
                self.session.flush()
        self.session.flush()

        profile_ids = [profile.id for profile in profiles.values()]
        existing_cases: dict[int, CustomerPriceTypeCase] = {}
        for index in range(0, len(profile_ids), _BATCH_SIZE):
            chunk = profile_ids[index : index + _BATCH_SIZE]
            rows = self.session.scalars(
                select(CustomerPriceTypeCase).where(
                    CustomerPriceTypeCase.profile_id.in_(chunk),
                    CustomerPriceTypeCase.snapshot_month == run.snapshot_month,
                )
            ).all()
            existing_cases.update({row.profile_id: row for row in rows})

        previous_snapshot_ids = [row.current_snapshot_id for row in existing_cases.values()]
        previous_hashes: dict[int, str] = {}
        for index in range(0, len(previous_snapshot_ids), _BATCH_SIZE):
            chunk = previous_snapshot_ids[index : index + _BATCH_SIZE]
            rows = self.session.execute(
                select(CustomerPriceTypeSnapshot.id, CustomerPriceTypeSnapshot.snapshot_hash).where(
                    CustomerPriceTypeSnapshot.id.in_(chunk)
                )
            ).all()
            previous_hashes.update({row.id: row.snapshot_hash for row in rows})

        for fact, decision in evaluated:
            if not decision.excluded:
                continue
            profile = profiles[normalize_counterparty_ref(fact.counterparty_ref)]
            case = existing_cases.get(profile.id)
            if case is None and profile.open_case_id is not None:
                case = self.session.get(CustomerPriceTypeCase, profile.open_case_id)
            profile.open_case_id = None
            if case is None:
                continue
            idempotency_key = f"profile-excluded:{run.snapshot_month:%Y-%m}:{run.ruleset_version}"
            existing_event = self.session.scalar(
                select(CustomerPriceTypeCaseEvent.id).where(
                    CustomerPriceTypeCaseEvent.case_id == case.id,
                    CustomerPriceTypeCaseEvent.idempotency_key == idempotency_key,
                )
            )
            if existing_event is None:
                self.session.add(
                    CustomerPriceTypeCaseEvent(
                        case_id=case.id,
                        event_type="profile_excluded",
                        actor="system",
                        source="calculation",
                        before_status=case.stage,
                        after_status=case.stage,
                        comment=decision.recommendation_reason,
                        metadata_json={
                            "registry_class": decision.registry_class,
                            "run_id": run.id,
                        },
                        idempotency_key=idempotency_key,
                    )
                )

        snapshot_rows: list[
            tuple[CustomerPriceTypeFacts, CustomerPriceTypeDecision, CustomerPriceTypeSnapshot]
        ] = []
        for index, (fact, decision) in enumerate(included, start=1):
            ref = normalize_counterparty_ref(fact.counterparty_ref)
            profile = profiles[ref]
            snapshot = CustomerPriceTypeSnapshot(
                run_id=run.id,
                profile_id=profile.id,
                counterparty_ref=ref,
                snapshot_month=run.snapshot_month,
                ruleset_version=run.ruleset_version,
                current_price_type=decision.current_price_type,
                current_level=decision.current_level,
                price_type_variant=decision.price_type_variant,
                contract_candidates=[asdict(item) for item in fact.contracts],
                monthly_sales=_money_map(fact.monthly_sales),
                total_3m=decision.total_3m,
                last_month=decision.last_month,
                economics=dict(fact.economics),
                payments=dict(fact.payments),
                returns=dict(fact.returns),
                history={
                    "coverage_months": fact.history_coverage_months,
                    "first_activity_date": (
                        fact.first_activity_date.isoformat() if fact.first_activity_date else None
                    ),
                    "consecutive_zero_months": decision.consecutive_zero_months,
                },
                source_status=decision.source_status,
                source_statuses=dict(fact.source_statuses),
                conflicts=[item for item in decision.stop_factors if "conflict" in item],
                stop_factors=list(decision.stop_factors),
                system_recommendation=decision.recommendation,
                recommended_price_type=decision.recommended_price_type,
                recommendation_reason=decision.recommendation_reason,
                action_required=decision.action_required,
                case_type=decision.case_type,
                review_type=decision.review_type,
                reasons=list(decision.reasons),
                snapshot_hash=decision.snapshot_hash,
            )
            self.session.add(snapshot)
            snapshot_rows.append((fact, decision, snapshot))
            if index % _BATCH_SIZE == 0:
                self.session.flush()
        self.session.flush()

        for fact, decision, snapshot in snapshot_rows:
            profile = profiles[normalize_counterparty_ref(fact.counterparty_ref)]
            profile.latest_snapshot_id = snapshot.id
            case = existing_cases.get(profile.id)
            if case is None and not decision.action_required:
                continue
            if case is None:
                case_key = f"{profile.counterparty_ref}:{run.snapshot_month:%Y-%m}"
                case = CustomerPriceTypeCase(
                    case_key=case_key,
                    profile_id=profile.id,
                    current_snapshot_id=snapshot.id,
                    snapshot_month=run.snapshot_month,
                    ruleset_version=run.ruleset_version,
                    case_type=decision.case_type or "special_review",
                    review_type=decision.review_type,
                    reasons=list(decision.reasons),
                    stage="NEW",
                    owner_ref=fact.owner_ref,
                    owner_name=fact.owner_name,
                    department_ref=fact.department_ref,
                    department_name=fact.department_name,
                    manager_action_completeness={},
                    system_recommendation=decision.recommendation,
                    recommended_price_type=decision.recommended_price_type,
                    approval_status="not_requested",
                    onec_export_status="not_ready",
                    onec_readback_status="not_requested",
                    version=1,
                )
                self.session.add(case)
                self.session.flush()
                profile.open_case_id = case.id
                existing_cases[profile.id] = case
                self.session.add(
                    CustomerPriceTypeCaseEvent(
                        case_id=case.id,
                        event_type="case_created",
                        actor="system",
                        source="calculation",
                        after_status="NEW",
                        comment=decision.recommendation_reason,
                        metadata_json={"snapshot_hash": decision.snapshot_hash, "run_id": run.id},
                        idempotency_key=f"case-created:{case.case_key}",
                    )
                )
                continue

            previous_hash = previous_hashes.get(case.current_snapshot_id)
            changed = previous_hash != decision.snapshot_hash
            case.current_snapshot_id = snapshot.id
            case.ruleset_version = run.ruleset_version
            case.system_recommendation = decision.recommendation
            case.recommended_price_type = decision.recommended_price_type
            case.owner_ref = fact.owner_ref
            case.owner_name = fact.owner_name
            case.department_ref = fact.department_ref
            case.department_name = fact.department_name
            if decision.action_required:
                profile.open_case_id = case.id
                case.case_type = decision.case_type or case.case_type
                case.review_type = decision.review_type
                case.reasons = list(decision.reasons)
            if not changed:
                continue
            before = {
                "approval_status": case.approval_status,
                "human_final_decision": case.human_final_decision,
                "snapshot_hash": previous_hash,
                "version": case.version,
            }
            case.version += 1
            case.approval_status = "not_requested"
            case.approver_ref = None
            case.approver_name = None
            case.approved_at = None
            case.approved_snapshot_hash = None
            case.human_final_decision = None
            self.session.add(
                CustomerPriceTypeCaseEvent(
                    case_id=case.id,
                    event_type="snapshot_changed",
                    actor="system",
                    source="calculation",
                    before_status=case.stage,
                    after_status=case.stage,
                    comment="Исходные факты изменились; требуется повторная проверка.",
                    metadata_json={
                        "before": before,
                        "after_snapshot_hash": decision.snapshot_hash,
                        "run_id": run.id,
                    },
                    idempotency_key=f"run:{run.id}:snapshot:{decision.snapshot_hash}",
                )
            )

        excluded_count = sum(decision.excluded for decision in decisions)
        run.excluded_count = excluded_count
        run.calculated_count = len(decisions) - excluded_count
        run.conflict_count = sum(
            decision.source_status in {"partial", "conflict"} for decision in decisions
        )
        run.actionable_count = sum(
            decision.action_required and not decision.excluded for decision in decisions
        )
        source_partial = any(status != "ready" for status in run.source_statuses.values())
        run.status = "partial" if run.conflict_count or source_partial else "completed"
        run.completed_at = _utcnow()

    def _lock_snapshot_month(self, snapshot_month: date) -> None:
        bind = self.session.get_bind()
        if bind.dialect.name != "postgresql":
            return
        self.session.execute(select(func.pg_advisory_xact_lock(func.hashtext(str(snapshot_month)))))

    def latest_run(self, snapshot_month: date | None = None) -> CustomerPriceTypeRun | None:
        statement = select(CustomerPriceTypeRun).where(
            CustomerPriceTypeRun.status.in_(("completed", "partial"))
        )
        if snapshot_month is not None:
            statement = statement.where(CustomerPriceTypeRun.snapshot_month == snapshot_month)
        return self.session.scalar(
            statement.order_by(
                CustomerPriceTypeRun.snapshot_month.desc(),
                CustomerPriceTypeRun.completed_at.desc(),
                CustomerPriceTypeRun.id.desc(),
            )
        )

    def _scope_predicates(
        self,
        access: CustomerPriceTypeAccessScope,
    ) -> list[Any]:
        if access.is_full:
            return []
        if access.role == "manager":
            return [
                (
                    CustomerPriceTypeProfile.owner_ref == access.owner_ref
                    if access.owner_ref
                    else false()
                )
            ]
        if access.role == "department_head":
            return [
                (
                    CustomerPriceTypeProfile.department_ref.in_(access.department_refs)
                    if access.department_refs
                    else false()
                )
            ]
        if access.role == "master_data":
            return [CustomerPriceTypeCase.case_type == "data_check"]
        if access.role == "quality":
            return [CustomerPriceTypeCase.review_type == "quality"]
        if access.role == "finance":
            return [CustomerPriceTypeCase.review_type.in_(("credit", "economics"))]
        return [false()]

    def summary(
        self,
        *,
        run: CustomerPriceTypeRun,
        access: CustomerPriceTypeAccessScope,
    ) -> dict[str, dict[str, int] | int]:
        statement = (
            select(CustomerPriceTypeSnapshot)
            .join(
                CustomerPriceTypeProfile,
                CustomerPriceTypeProfile.id == CustomerPriceTypeSnapshot.profile_id,
            )
            .outerjoin(
                CustomerPriceTypeCase,
                CustomerPriceTypeCase.current_snapshot_id == CustomerPriceTypeSnapshot.id,
            )
            .where(CustomerPriceTypeSnapshot.run_id == run.id)
        )
        predicates = self._scope_predicates(access)
        if predicates:
            statement = statement.where(*predicates)
        rows = self.session.execute(
            statement.with_only_columns(
                CustomerPriceTypeSnapshot,
                CustomerPriceTypeProfile,
                maintain_column_froms=True,
            )
        ).all()

        snapshots = [row[0] for row in rows]

        def counts(attribute: str) -> dict[str, int]:
            result: dict[str, int] = {}
            for row in snapshots:
                key = str(getattr(row, attribute) or "unknown")
                result[key] = result.get(key, 0) + 1
            return dict(sorted(result.items()))

        departments: dict[str, int] = {}
        for _, profile in rows:
            key = str(profile.department_ref or "unknown")
            departments[key] = departments.get(key, 0) + 1

        return {
            "profile_count": len(rows),
            "actionable_count": sum(row.action_required for row in snapshots),
            "levels": counts("current_level"),
            "recommendations": counts("system_recommendation"),
            "source_statuses": counts("source_status"),
            "review_types": counts("review_type"),
            "departments": dict(sorted(departments.items())),
        }

    def worklists(
        self,
        *,
        run: CustomerPriceTypeRun,
        access: CustomerPriceTypeAccessScope,
    ) -> dict[str, int]:
        statement = (
            select(CustomerPriceTypeCase.case_type, func.count(CustomerPriceTypeCase.id))
            .join(
                CustomerPriceTypeProfile,
                CustomerPriceTypeProfile.id == CustomerPriceTypeCase.profile_id,
            )
            .join(
                CustomerPriceTypeSnapshot,
                CustomerPriceTypeSnapshot.id == CustomerPriceTypeCase.current_snapshot_id,
            )
            .where(
                CustomerPriceTypeCase.snapshot_month == run.snapshot_month,
                CustomerPriceTypeSnapshot.run_id == run.id,
                CustomerPriceTypeSnapshot.action_required.is_(True),
                CustomerPriceTypeProfile.is_service_card.is_(False),
            )
            .group_by(CustomerPriceTypeCase.case_type)
        )
        predicates = self._scope_predicates(access)
        if predicates:
            statement = statement.where(*predicates)
        raw = {str(case_type): int(count) for case_type, count in self.session.execute(statement)}
        return {
            key: raw.get(key, 0)
            for key in (
                "manager_work",
                "isolate",
                "recovery",
                "data_check",
                "special_review",
                "downgrade_approval",
            )
        }

    def list_cases(
        self,
        *,
        access: CustomerPriceTypeAccessScope,
        run_id: int,
        snapshot_month: date | None,
        worklist: str | None,
        stage: str | None,
        review_type: str | None,
        source_status: str | None,
        department_ref: str | None,
        search: str | None,
        limit: int,
        offset: int,
    ) -> tuple[
        list[tuple[CustomerPriceTypeCase, CustomerPriceTypeProfile, CustomerPriceTypeSnapshot]], int
    ]:
        filters: list[Any] = [
            CustomerPriceTypeSnapshot.run_id == run_id,
            CustomerPriceTypeProfile.is_service_card.is_(False),
        ]
        if snapshot_month is not None:
            filters.append(CustomerPriceTypeCase.snapshot_month == snapshot_month)
        if worklist:
            filters.extend(
                [
                    CustomerPriceTypeCase.case_type == worklist,
                    CustomerPriceTypeSnapshot.action_required.is_(True),
                ]
            )
        if stage:
            filters.append(CustomerPriceTypeCase.stage == stage)
        if review_type:
            filters.append(CustomerPriceTypeCase.review_type == review_type)
        if source_status:
            filters.append(CustomerPriceTypeSnapshot.source_status == source_status)
        if department_ref:
            filters.append(CustomerPriceTypeProfile.department_ref == department_ref)
        if search:
            pattern = f"%{search.strip()}%"
            filters.append(
                or_(
                    CustomerPriceTypeProfile.counterparty_ref.ilike(pattern),
                    CustomerPriceTypeProfile.counterparty_code.ilike(pattern),
                    CustomerPriceTypeProfile.counterparty_name.ilike(pattern),
                )
            )
        filters.extend(self._scope_predicates(access))
        base = (
            select(CustomerPriceTypeCase, CustomerPriceTypeProfile, CustomerPriceTypeSnapshot)
            .join(
                CustomerPriceTypeProfile,
                CustomerPriceTypeProfile.id == CustomerPriceTypeCase.profile_id,
            )
            .join(
                CustomerPriceTypeSnapshot,
                CustomerPriceTypeSnapshot.id == CustomerPriceTypeCase.current_snapshot_id,
            )
            .where(*filters)
        )
        total = (
            self.session.scalar(select(func.count()).select_from(base.order_by(None).subquery()))
            or 0
        )
        rows = self.session.execute(
            base.order_by(
                CustomerPriceTypeCase.due_at.asc().nullslast(),
                CustomerPriceTypeProfile.counterparty_name.asc(),
                CustomerPriceTypeCase.id.asc(),
            )
            .offset(offset)
            .limit(limit)
        ).all()
        return [(row[0], row[1], row[2]) for row in rows], int(total)

    def get_case_scoped(
        self, case_id: int, access: CustomerPriceTypeAccessScope
    ) -> tuple[CustomerPriceTypeCase, CustomerPriceTypeProfile, CustomerPriceTypeSnapshot] | None:
        statement = (
            select(CustomerPriceTypeCase, CustomerPriceTypeProfile, CustomerPriceTypeSnapshot)
            .join(
                CustomerPriceTypeProfile,
                CustomerPriceTypeProfile.id == CustomerPriceTypeCase.profile_id,
            )
            .join(
                CustomerPriceTypeSnapshot,
                CustomerPriceTypeSnapshot.id == CustomerPriceTypeCase.current_snapshot_id,
            )
            .where(
                CustomerPriceTypeCase.id == case_id,
                CustomerPriceTypeProfile.is_service_card.is_(False),
                *self._scope_predicates(access),
            )
        )
        row = self.session.execute(statement).first()
        return (row[0], row[1], row[2]) if row else None

    def list_case_events(self, case_id: int) -> list[CustomerPriceTypeCaseEvent]:
        return list(
            self.session.scalars(
                select(CustomerPriceTypeCaseEvent)
                .where(CustomerPriceTypeCaseEvent.case_id == case_id)
                .order_by(
                    CustomerPriceTypeCaseEvent.event_at.asc(), CustomerPriceTypeCaseEvent.id.asc()
                )
            )
        )

    def get_profile_scoped(
        self, counterparty_ref: str, access: CustomerPriceTypeAccessScope
    ) -> CustomerPriceTypeProfile | None:
        ref = normalize_counterparty_ref(counterparty_ref)
        profile = self.session.scalar(
            select(CustomerPriceTypeProfile).where(CustomerPriceTypeProfile.counterparty_ref == ref)
        )
        if profile is None or access.is_full:
            return profile
        if access.role == "manager":
            return profile if access.owner_ref and profile.owner_ref == access.owner_ref else None
        if access.role == "department_head":
            return profile if profile.department_ref in access.department_refs else None
        visible_case = self.session.scalar(
            select(CustomerPriceTypeCase.id).where(
                CustomerPriceTypeCase.profile_id == profile.id,
                *self._scope_predicates(access),
            )
        )
        return profile if visible_case is not None else None

    def profile_snapshots(
        self,
        profile_id: int,
        *,
        access: CustomerPriceTypeAccessScope,
        limit: int = 24,
    ) -> list[CustomerPriceTypeSnapshot]:
        filters: list[Any] = [CustomerPriceTypeSnapshot.profile_id == profile_id]
        if access.role == "master_data":
            filters.append(CustomerPriceTypeSnapshot.case_type == "data_check")
        elif access.role == "quality":
            filters.append(CustomerPriceTypeSnapshot.review_type == "quality")
        elif access.role == "finance":
            filters.append(CustomerPriceTypeSnapshot.review_type.in_(("credit", "economics")))
        elif access.role == "integration_operator":
            filters.append(false())
        return list(
            self.session.scalars(
                select(CustomerPriceTypeSnapshot)
                .where(*filters)
                .order_by(
                    CustomerPriceTypeSnapshot.snapshot_month.desc(),
                    CustomerPriceTypeSnapshot.id.desc(),
                )
                .limit(limit)
            )
        )
