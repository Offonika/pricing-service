"""SQLAlchemy persistence and scoped reads for customer price types."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, case, exists, false, func, or_, select, update
from sqlalchemy.orm import Session, aliased

from app.domains.customer_price_types import (
    CustomerPriceTypeAccessScope,
    CustomerPriceTypeDecision,
    CustomerPriceTypeFacts,
    normalize_counterparty_ref,
)
from app.models.customer_price_type import (
    CustomerPriceTypeCase,
    CustomerPriceTypeCaseEvent,
    CustomerPriceTypeExternalAction,
    CustomerPriceTypeOneCContractAction,
    CustomerPriceTypeProfile,
    CustomerPriceTypeQualitySample,
    CustomerPriceTypeReview,
    CustomerPriceTypeReviewBatch,
    CustomerPriceTypeReviewBatchItem,
    CustomerPriceTypeRun,
    CustomerPriceTypeSnapshot,
)

_BATCH_SIZE = 750
QUALITY_GROUPS = (
    "manager_work",
    "isolate",
    "recovery",
    "data_check",
    "special_review",
    "downgrade_approval",
    "no_action",
)
_BUSINESS_CONFLICT_REASONS = {
    "conflicting_price_levels",
    "conflicting_price_type_variants",
}
_REVIEW_REQUIRED_SOURCES = (
    "contracts",
    "sales_history",
    "ledger_reconciliation",
    "master_data",
)


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


def _actionable_case_predicate() -> Any:
    return func.coalesce(
        or_(
            CustomerPriceTypeSnapshot.action_required.is_(True),
            CustomerPriceTypeCase.onec_export_status == "exported",
            CustomerPriceTypeCase.onec_readback_status.in_(("pending", "mismatch", "error")),
        ),
        false(),
    )


def review_batch_snapshot_status(snapshot: CustomerPriceTypeSnapshot | None) -> str:
    if snapshot is None:
        return "missing_snapshot"
    if any(
        snapshot.source_statuses.get(source, "missing") != "ready"
        for source in _REVIEW_REQUIRED_SOURCES
    ):
        return "technical_incomplete"
    if snapshot.system_recommendation != "data_check":
        return "ready"
    if set(snapshot.reasons or ()) & _BUSINESS_CONFLICT_REASONS:
        return "business_conflict"
    return "technical_incomplete"


def review_batch_item_matches(
    item: CustomerPriceTypeReviewBatchItem,
    snapshot: CustomerPriceTypeSnapshot | None,
) -> bool:
    if snapshot is None:
        return False
    actual_bucket = (
        "working_bronze" if snapshot.current_price_type == "2.Бронзовый" else "review_queue"
    )
    if actual_bucket != item.expected_bucket:
        return False
    status = review_batch_snapshot_status(snapshot)
    if item.expected_price_type:
        return snapshot.current_price_type == item.expected_price_type and status == "ready"
    return snapshot.current_price_type is None and status == "business_conflict"


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
            profile.is_service_card = decision.recommendation == "excluded_service_card"
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
            calculation_refs = set(decision.calculation_contract_refs)
            change_target_refs = set(decision.price_type_change_contract_refs)
            contract_candidates = []
            for contract in fact.contracts:
                contract_ref = str(contract.contract_ref or "")
                used_for_calculation = contract_ref in calculation_refs
                serialized_contract = asdict(contract)
                serialized_contract["sales_amount_12m"] = format(
                    contract.sales_amount_12m.quantize(Decimal("0.01")), "f"
                )
                serialized_contract["last_sale_at"] = (
                    contract.last_sale_at.isoformat() if contract.last_sale_at else None
                )
                candidate = {
                    **serialized_contract,
                    "used_for_calculation": used_for_calculation,
                    "price_type_change_target": contract_ref in change_target_refs,
                    "ignored_reason": None,
                }
                if not used_for_calculation:
                    if contract.price_type_missing:
                        candidate["ignored_reason"] = "price_type_missing"
                    elif contract.price_type_marked:
                        candidate["ignored_reason"] = "price_type_marked"
                    elif not contract.is_working and contract.sale_document_count_12m == 0:
                        candidate["ignored_reason"] = "no_sales_in_working_window"
                    else:
                        candidate["ignored_reason"] = "not_selected_for_price_type"
                contract_candidates.append(candidate)
            snapshot = CustomerPriceTypeSnapshot(
                run_id=run.id,
                profile_id=profile.id,
                counterparty_ref=ref,
                snapshot_month=run.snapshot_month,
                ruleset_version=run.ruleset_version,
                current_price_type=decision.current_price_type,
                current_level=decision.current_level,
                price_type_variant=decision.price_type_variant,
                contract_candidates=contract_candidates,
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
                    stage="NEW_SNAPSHOT",
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
                        after_status="NEW_SNAPSHOT",
                        comment=decision.recommendation_reason,
                        metadata_json={"snapshot_hash": decision.snapshot_hash, "run_id": run.id},
                        idempotency_key=f"case-created:{case.case_key}",
                    )
                )
                continue

            previous_hash = previous_hashes.get(case.current_snapshot_id)
            changed = previous_hash != decision.snapshot_hash
            previous_case_type = case.case_type
            previous_stage = case.stage
            case.current_snapshot_id = snapshot.id
            case.ruleset_version = run.ruleset_version
            case.system_recommendation = decision.recommendation
            case.recommended_price_type = decision.recommended_price_type
            case.owner_ref = fact.owner_ref
            case.owner_name = fact.owner_name
            case.department_ref = fact.department_ref
            case.department_name = fact.department_name
            external_control_active = (
                case.onec_export_status == "exported"
                or case.onec_readback_status in {"pending", "mismatch", "error"}
            )
            if not decision.action_required:
                if external_control_active:
                    profile.open_case_id = case.id
                    continue
                if profile.open_case_id == case.id:
                    profile.open_case_id = None
                if case.stage != "CLOSED_KEEP":
                    before = {
                        "case_type": case.case_type,
                        "stage": case.stage,
                        "approval_status": case.approval_status,
                        "human_final_decision": case.human_final_decision,
                        "snapshot_hash": previous_hash,
                        "version": case.version,
                    }
                    case.stage = "CLOSED_KEEP"
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
                            event_type="case_auto_closed",
                            actor="system",
                            source="calculation",
                            before_status=previous_stage,
                            after_status="CLOSED_KEEP",
                            comment=(
                                "Новый расчёт не требует операционного действия; "
                                "кейс закрыт без изменения типа цены."
                            ),
                            metadata_json={
                                "before": before,
                                "after_snapshot_hash": decision.snapshot_hash,
                                "run_id": run.id,
                            },
                            idempotency_key=f"run:{run.id}:case-auto-closed:{case.id}",
                        )
                    )
                continue

            profile.open_case_id = case.id
            next_case_type = decision.case_type or case.case_type
            reclassified = previous_case_type != next_case_type
            reopened = previous_stage in {"CLOSED_KEEP", "CLOSED_CHANGED"}
            case.case_type = next_case_type
            case.review_type = decision.review_type
            case.reasons = list(decision.reasons)
            if not changed and not reclassified and not reopened:
                continue
            before = {
                "case_type": previous_case_type,
                "stage": previous_stage,
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
            event_type = "snapshot_changed"
            comment = "Исходные факты изменились; требуется повторная проверка."
            if reclassified:
                event_type = "case_reclassified"
                comment = "Новый расчёт изменил операционную очередь кейса."
                case.stage = "NEW_SNAPSHOT"
            elif reopened:
                event_type = "case_reopened"
                comment = "Новый расчёт снова требует операционного действия."
                case.stage = "NEW_SNAPSHOT"
            self.session.add(
                CustomerPriceTypeCaseEvent(
                    case_id=case.id,
                    event_type=event_type,
                    actor="system",
                    source="calculation",
                    before_status=previous_stage,
                    after_status=case.stage,
                    comment=comment,
                    metadata_json={
                        "before": before,
                        "after_case_type": case.case_type,
                        "after_snapshot_hash": decision.snapshot_hash,
                        "run_id": run.id,
                    },
                    idempotency_key=(f"run:{run.id}:{event_type}:{decision.snapshot_hash}"),
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

    def _profile_scope_predicates(
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
        return [false()]

    def search_profiles(
        self,
        *,
        run_id: int,
        access: CustomerPriceTypeAccessScope,
        search: str,
        limit: int,
        offset: int,
    ) -> tuple[list[tuple[Any, ...]], int]:
        pattern = f"%{search.strip()}%"
        base = (
            select(
                CustomerPriceTypeProfile,
                CustomerPriceTypeSnapshot,
                CustomerPriceTypeQualitySample,
            )
            .join(
                CustomerPriceTypeSnapshot,
                (CustomerPriceTypeSnapshot.profile_id == CustomerPriceTypeProfile.id)
                & (CustomerPriceTypeSnapshot.run_id == run_id),
            )
            .outerjoin(
                CustomerPriceTypeQualitySample,
                CustomerPriceTypeQualitySample.snapshot_id == CustomerPriceTypeSnapshot.id,
            )
            .where(
                CustomerPriceTypeProfile.is_service_card.is_(False),
                or_(
                    CustomerPriceTypeProfile.counterparty_ref.ilike(pattern),
                    CustomerPriceTypeProfile.counterparty_code.ilike(pattern),
                    CustomerPriceTypeProfile.counterparty_name.ilike(pattern),
                ),
                *self._profile_scope_predicates(access),
            )
        )
        total = int(
            self.session.scalar(select(func.count()).select_from(base.order_by(None).subquery()))
            or 0
        )
        rows = self.session.execute(
            base.order_by(
                CustomerPriceTypeProfile.counterparty_name.asc(),
                CustomerPriceTypeProfile.id.asc(),
            )
            .offset(offset)
            .limit(limit)
        ).all()
        return [(row[0], row[1], row[2]) for row in rows], total

    def list_data_issues(
        self,
        *,
        run_id: int,
        search: str | None,
    ) -> list[tuple[Any, ...]]:
        profile_filter = []
        if search:
            pattern = f"%{search.strip()}%"
            profile_filter.append(
                or_(
                    CustomerPriceTypeProfile.counterparty_ref.ilike(pattern),
                    CustomerPriceTypeProfile.counterparty_code.ilike(pattern),
                    CustomerPriceTypeProfile.counterparty_name.ilike(pattern),
                )
            )
        automatic = self.session.execute(
            select(
                CustomerPriceTypeProfile,
                CustomerPriceTypeSnapshot,
                CustomerPriceTypeCase,
            )
            .join(
                CustomerPriceTypeSnapshot,
                CustomerPriceTypeSnapshot.profile_id == CustomerPriceTypeProfile.id,
            )
            .outerjoin(
                CustomerPriceTypeCase,
                CustomerPriceTypeCase.current_snapshot_id == CustomerPriceTypeSnapshot.id,
            )
            .where(
                CustomerPriceTypeSnapshot.run_id == run_id,
                CustomerPriceTypeSnapshot.case_type == "data_check",
                CustomerPriceTypeProfile.is_service_card.is_(False),
                *profile_filter,
            )
        ).all()
        expert = self.session.execute(
            select(
                CustomerPriceTypeProfile,
                CustomerPriceTypeSnapshot,
                CustomerPriceTypeQualitySample,
            )
            .join(
                CustomerPriceTypeSnapshot,
                CustomerPriceTypeSnapshot.profile_id == CustomerPriceTypeProfile.id,
            )
            .join(
                CustomerPriceTypeQualitySample,
                CustomerPriceTypeQualitySample.snapshot_id == CustomerPriceTypeSnapshot.id,
            )
            .where(
                CustomerPriceTypeSnapshot.run_id == run_id,
                CustomerPriceTypeQualitySample.status == "reviewed",
                CustomerPriceTypeQualitySample.system_group != "data_check",
                CustomerPriceTypeQualitySample.correct_group == "data_check",
                CustomerPriceTypeProfile.is_service_card.is_(False),
                *profile_filter,
            )
        ).all()
        current_reviews = self.session.execute(
            select(
                CustomerPriceTypeProfile,
                CustomerPriceTypeSnapshot,
                CustomerPriceTypeReview,
            )
            .join(
                CustomerPriceTypeSnapshot,
                CustomerPriceTypeSnapshot.profile_id == CustomerPriceTypeProfile.id,
            )
            .join(
                CustomerPriceTypeReview,
                CustomerPriceTypeReview.snapshot_id == CustomerPriceTypeSnapshot.id,
            )
            .where(
                CustomerPriceTypeSnapshot.run_id == run_id,
                CustomerPriceTypeReview.result == "data_issue",
                CustomerPriceTypeProfile.is_service_card.is_(False),
                *profile_filter,
            )
        ).all()
        result = (
            [("calculation", row[0], row[1], row[2]) for row in automatic]
            + [("expert", row[0], row[1], row[2]) for row in expert]
            + [("expert", row[0], row[1], row[2]) for row in current_reviews]
        )
        return sorted(
            result,
            key=lambda row: (
                str(row[1].counterparty_name or "").casefold(),
                str(row[1].counterparty_ref),
                row[0],
            ),
        )

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
            "actionable_count": sum(
                row.action_required
                and not (access.role == "network_head" and row.case_type == "data_check")
                for row in snapshots
            ),
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
                _actionable_case_predicate(),
                CustomerPriceTypeProfile.is_service_card.is_(False),
            )
            .group_by(CustomerPriceTypeCase.case_type)
        )
        predicates = self._scope_predicates(access)
        if predicates:
            statement = statement.where(*predicates)
        raw = {str(case_type): int(count) for case_type, count in self.session.execute(statement)}
        if access.role == "network_head":
            raw.pop("data_check", None)
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
            _actionable_case_predicate(),
            CustomerPriceTypeProfile.is_service_card.is_(False),
        ]
        if snapshot_month is not None:
            filters.append(CustomerPriceTypeCase.snapshot_month == snapshot_month)
        if access.role == "network_head":
            filters.append(CustomerPriceTypeCase.case_type != "data_check")
        if worklist:
            filters.extend(
                [
                    CustomerPriceTypeCase.case_type == worklist,
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

    def get_review_batch(self, batch_key: str) -> CustomerPriceTypeReviewBatch | None:
        return self.session.scalar(
            select(CustomerPriceTypeReviewBatch).where(
                CustomerPriceTypeReviewBatch.batch_key == batch_key,
                CustomerPriceTypeReviewBatch.status == "ready",
            )
        )

    def list_portfolio(
        self,
        *,
        batch: CustomerPriceTypeReviewBatch,
        access: CustomerPriceTypeAccessScope,
        run_id: int | None,
        bucket: str,
        current_price_type: str | None,
        action_required: bool | None,
        search: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[tuple[Any, ...]], int, dict[str, int], dict[str, int], int]:
        actual_working_bronze = CustomerPriceTypeSnapshot.current_price_type == "2.Бронзовый"
        filters: list[Any] = [CustomerPriceTypeReviewBatchItem.batch_id == batch.id]
        if bucket == "working_bronze":
            filters.append(actual_working_bronze)
        elif bucket == "review_queue":
            filters.append(
                or_(
                    CustomerPriceTypeSnapshot.current_price_type.is_(None),
                    CustomerPriceTypeSnapshot.current_price_type != "2.Бронзовый",
                )
            )
        if current_price_type:
            filters.append(CustomerPriceTypeSnapshot.current_price_type == current_price_type)
        if action_required is not None:
            effective_action = _actionable_case_predicate()
            filters.append(effective_action if action_required else ~effective_action)
        if search:
            pattern = f"%{search.strip()}%"
            filters.append(
                or_(
                    CustomerPriceTypeReviewBatchItem.counterparty_code.ilike(pattern),
                    CustomerPriceTypeProfile.counterparty_name.ilike(pattern),
                    CustomerPriceTypeProfile.counterparty_ref.ilike(pattern),
                )
            )
        filters.extend(self._scope_predicates(access))
        base = (
            select(
                CustomerPriceTypeReviewBatchItem,
                CustomerPriceTypeProfile,
                CustomerPriceTypeSnapshot,
                CustomerPriceTypeCase,
            )
            .join(
                CustomerPriceTypeProfile,
                CustomerPriceTypeProfile.counterparty_ref
                == CustomerPriceTypeReviewBatchItem.counterparty_ref,
            )
            .outerjoin(
                CustomerPriceTypeSnapshot,
                (
                    (CustomerPriceTypeSnapshot.profile_id == CustomerPriceTypeProfile.id)
                    & (CustomerPriceTypeSnapshot.run_id == run_id)
                ),
            )
            .outerjoin(
                CustomerPriceTypeCase,
                CustomerPriceTypeCase.current_snapshot_id == CustomerPriceTypeSnapshot.id,
            )
            .where(*filters)
        )
        total = int(
            self.session.scalar(select(func.count()).select_from(base.order_by(None).subquery()))
            or 0
        )
        rows = self.session.execute(
            base.order_by(
                CustomerPriceTypeReviewBatchItem.expected_bucket.asc(),
                CustomerPriceTypeReviewBatchItem.source_row.asc(),
            )
            .offset(offset)
            .limit(limit)
        ).all()

        scope_filters: list[Any] = [CustomerPriceTypeReviewBatchItem.batch_id == batch.id]
        scope_filters.extend(self._scope_predicates(access))
        all_rows = self.session.execute(
            select(
                CustomerPriceTypeReviewBatchItem,
                CustomerPriceTypeProfile,
                CustomerPriceTypeSnapshot,
                CustomerPriceTypeCase,
            )
            .join(
                CustomerPriceTypeProfile,
                CustomerPriceTypeProfile.counterparty_ref
                == CustomerPriceTypeReviewBatchItem.counterparty_ref,
            )
            .outerjoin(
                CustomerPriceTypeSnapshot,
                (
                    (CustomerPriceTypeSnapshot.profile_id == CustomerPriceTypeProfile.id)
                    & (CustomerPriceTypeSnapshot.run_id == run_id)
                ),
            )
            .outerjoin(
                CustomerPriceTypeCase,
                CustomerPriceTypeCase.current_snapshot_id == CustomerPriceTypeSnapshot.id,
            )
            .where(*scope_filters)
        ).all()
        counts = {"working_bronze": 0, "review_queue": 0, "total": len(all_rows)}
        review_status_counts = {
            "ready": 0,
            "business_conflict": 0,
            "technical_incomplete": 0,
            "missing_snapshot": 0,
        }
        mismatch_count = 0
        for item, _, snapshot, _ in all_rows:
            actual_bucket = (
                "working_bronze"
                if snapshot is not None and snapshot.current_price_type == "2.Бронзовый"
                else "review_queue"
            )
            counts[actual_bucket] += 1
            review_status_counts[review_batch_snapshot_status(snapshot)] += 1
            if not review_batch_item_matches(item, snapshot):
                mismatch_count += 1
        return (
            [tuple(row) for row in rows],
            total,
            counts,
            review_status_counts,
            mismatch_count,
        )

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
        if access.role == "network_head":
            statement = statement.where(CustomerPriceTypeCase.case_type != "data_check")
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

    def profile_cases(
        self,
        profile_id: int,
        *,
        access: CustomerPriceTypeAccessScope,
        limit: int = 24,
    ) -> list[tuple[CustomerPriceTypeCase, CustomerPriceTypeProfile, CustomerPriceTypeSnapshot]]:
        statement = (
            select(
                CustomerPriceTypeCase,
                CustomerPriceTypeProfile,
                CustomerPriceTypeSnapshot,
            )
            .join(
                CustomerPriceTypeProfile,
                CustomerPriceTypeProfile.id == CustomerPriceTypeCase.profile_id,
            )
            .join(
                CustomerPriceTypeSnapshot,
                CustomerPriceTypeSnapshot.id == CustomerPriceTypeCase.current_snapshot_id,
            )
            .where(
                CustomerPriceTypeCase.profile_id == profile_id,
                CustomerPriceTypeProfile.is_service_card.is_(False),
                *self._scope_predicates(access),
            )
            .order_by(
                CustomerPriceTypeCase.snapshot_month.desc(),
                CustomerPriceTypeCase.id.desc(),
            )
            .limit(limit)
        )
        return [tuple(row) for row in self.session.execute(statement).all()]

    def prepare_quality_samples(
        self,
        *,
        run: CustomerPriceTypeRun,
        actor: str,
        per_group: int,
    ) -> tuple[int, int]:
        created = 0
        for group in QUALITY_GROUPS:
            if group == "data_check":
                continue
            existing_count = int(
                self.session.scalar(
                    select(func.count(CustomerPriceTypeQualitySample.id)).where(
                        CustomerPriceTypeQualitySample.run_id == run.id,
                        CustomerPriceTypeQualitySample.system_group == group,
                    )
                )
                or 0
            )
            remaining = max(per_group - existing_count, 0)
            if remaining == 0:
                continue

            already_selected = exists(
                select(CustomerPriceTypeQualitySample.id).where(
                    CustomerPriceTypeQualitySample.snapshot_id == CustomerPriceTypeSnapshot.id
                )
            )
            statement = (
                select(CustomerPriceTypeSnapshot)
                .join(
                    CustomerPriceTypeProfile,
                    CustomerPriceTypeProfile.id == CustomerPriceTypeSnapshot.profile_id,
                )
                .where(
                    CustomerPriceTypeSnapshot.run_id == run.id,
                    CustomerPriceTypeProfile.is_service_card.is_(False),
                    ~already_selected,
                )
            )
            if group == "no_action":
                statement = statement.where(
                    CustomerPriceTypeSnapshot.action_required.is_(False),
                    or_(
                        CustomerPriceTypeSnapshot.case_type.is_(None),
                        CustomerPriceTypeSnapshot.case_type != "data_check",
                    ),
                )
            else:
                statement = statement.where(
                    CustomerPriceTypeSnapshot.action_required.is_(True),
                    CustomerPriceTypeSnapshot.case_type == group,
                )
            snapshots = self.session.scalars(
                statement.order_by(
                    CustomerPriceTypeSnapshot.snapshot_hash.asc(),
                    CustomerPriceTypeSnapshot.id.asc(),
                ).limit(remaining)
            ).all()
            for snapshot in snapshots:
                self.session.add(
                    CustomerPriceTypeQualitySample(
                        run_id=run.id,
                        snapshot_id=snapshot.id,
                        profile_id=snapshot.profile_id,
                        system_group=group,
                        status="pending",
                        selected_by=actor,
                        version=1,
                    )
                )
                created += 1
        self.session.flush()
        total = int(
            self.session.scalar(
                select(func.count(CustomerPriceTypeQualitySample.id)).where(
                    CustomerPriceTypeQualitySample.run_id == run.id,
                    CustomerPriceTypeQualitySample.system_group != "data_check",
                    or_(
                        CustomerPriceTypeQualitySample.correct_group.is_(None),
                        CustomerPriceTypeQualitySample.correct_group != "data_check",
                    ),
                )
            )
            or 0
        )
        return created, total

    def list_quality_samples(
        self,
        *,
        run_id: int,
        access: CustomerPriceTypeAccessScope,
        status: str | None,
        group: str | None,
        limit: int,
        offset: int,
    ) -> tuple[
        list[
            tuple[
                CustomerPriceTypeQualitySample,
                CustomerPriceTypeProfile,
                CustomerPriceTypeSnapshot,
            ]
        ],
        int,
    ]:
        filters: list[Any] = [CustomerPriceTypeQualitySample.run_id == run_id]
        filters.append(CustomerPriceTypeQualitySample.system_group != "data_check")
        filters.append(
            or_(
                CustomerPriceTypeQualitySample.correct_group.is_(None),
                CustomerPriceTypeQualitySample.correct_group != "data_check",
            )
        )
        if status:
            filters.append(CustomerPriceTypeQualitySample.status == status)
        if group:
            filters.append(CustomerPriceTypeQualitySample.system_group == group)
        if access.role == "quality":
            filters.append(CustomerPriceTypeQualitySample.system_group == "special_review")
        base = (
            select(
                CustomerPriceTypeQualitySample,
                CustomerPriceTypeProfile,
                CustomerPriceTypeSnapshot,
            )
            .join(
                CustomerPriceTypeProfile,
                CustomerPriceTypeProfile.id == CustomerPriceTypeQualitySample.profile_id,
            )
            .join(
                CustomerPriceTypeSnapshot,
                CustomerPriceTypeSnapshot.id == CustomerPriceTypeQualitySample.snapshot_id,
            )
            .where(*filters)
        )
        total = int(
            self.session.scalar(select(func.count()).select_from(base.order_by(None).subquery()))
            or 0
        )
        rows = self.session.execute(
            base.order_by(
                CustomerPriceTypeQualitySample.status.asc(),
                CustomerPriceTypeQualitySample.system_group.asc(),
                CustomerPriceTypeProfile.counterparty_name.asc(),
                CustomerPriceTypeQualitySample.id.asc(),
            )
            .offset(offset)
            .limit(limit)
        ).all()
        return [(row[0], row[1], row[2]) for row in rows], total

    def get_quality_sample(
        self,
        sample_id: int,
        access: CustomerPriceTypeAccessScope,
    ) -> (
        tuple[
            CustomerPriceTypeQualitySample,
            CustomerPriceTypeProfile,
            CustomerPriceTypeSnapshot,
        ]
        | None
    ):
        statement = (
            select(
                CustomerPriceTypeQualitySample,
                CustomerPriceTypeProfile,
                CustomerPriceTypeSnapshot,
            )
            .join(
                CustomerPriceTypeProfile,
                CustomerPriceTypeProfile.id == CustomerPriceTypeQualitySample.profile_id,
            )
            .join(
                CustomerPriceTypeSnapshot,
                CustomerPriceTypeSnapshot.id == CustomerPriceTypeQualitySample.snapshot_id,
            )
            .where(CustomerPriceTypeQualitySample.id == sample_id)
        )
        if access.role == "quality":
            statement = statement.where(
                CustomerPriceTypeQualitySample.system_group == "special_review"
            )
        elif access.role == "network_head":
            statement = statement.where(
                CustomerPriceTypeQualitySample.system_group != "data_check",
                or_(
                    CustomerPriceTypeQualitySample.correct_group.is_(None),
                    CustomerPriceTypeQualitySample.correct_group != "data_check",
                ),
            )
        row = self.session.execute(statement).first()
        return (row[0], row[1], row[2]) if row else None

    def update_quality_sample_review(
        self,
        *,
        sample_id: int,
        correct_group: str,
        comment: str | None,
        reviewed_by: str,
        reviewed_at: datetime,
        expected_version: int,
        access: CustomerPriceTypeAccessScope,
    ) -> bool:
        filters: list[Any] = [
            CustomerPriceTypeQualitySample.id == sample_id,
            CustomerPriceTypeQualitySample.version == expected_version,
        ]
        if access.role == "quality":
            filters.append(CustomerPriceTypeQualitySample.system_group == "special_review")
        result = self.session.execute(
            update(CustomerPriceTypeQualitySample)
            .where(*filters)
            .values(
                correct_group=correct_group,
                status="reviewed",
                reviewed_by=reviewed_by,
                reviewed_at=reviewed_at,
                comment=comment,
                version=CustomerPriceTypeQualitySample.version + 1,
                updated_at=reviewed_at,
            )
        )
        return bool(result.rowcount)

    def quality_population_counts(
        self,
        *,
        run_id: int,
        access: CustomerPriceTypeAccessScope,
    ) -> dict[str, int]:
        system_group = case(
            (CustomerPriceTypeSnapshot.action_required.is_(False), "no_action"),
            else_=CustomerPriceTypeSnapshot.case_type,
        )
        statement = (
            select(system_group.label("system_group"), func.count())
            .join(
                CustomerPriceTypeProfile,
                CustomerPriceTypeProfile.id == CustomerPriceTypeSnapshot.profile_id,
            )
            .where(
                CustomerPriceTypeSnapshot.run_id == run_id,
                CustomerPriceTypeProfile.is_service_card.is_(False),
                or_(
                    CustomerPriceTypeSnapshot.case_type.is_(None),
                    CustomerPriceTypeSnapshot.case_type != "data_check",
                ),
            )
            .group_by(system_group)
        )
        if access.role == "quality":
            statement = statement.where(system_group == "special_review")
        return {
            str(group): int(count)
            for group, count in self.session.execute(statement)
            if group in QUALITY_GROUPS
        }

    def quality_samples_for_metrics(
        self,
        *,
        run_id: int,
        access: CustomerPriceTypeAccessScope,
    ) -> list[tuple[CustomerPriceTypeQualitySample, CustomerPriceTypeSnapshot]]:
        statement = (
            select(CustomerPriceTypeQualitySample, CustomerPriceTypeSnapshot)
            .join(
                CustomerPriceTypeSnapshot,
                CustomerPriceTypeSnapshot.id == CustomerPriceTypeQualitySample.snapshot_id,
            )
            .where(CustomerPriceTypeQualitySample.run_id == run_id)
            .where(
                CustomerPriceTypeQualitySample.system_group != "data_check",
                or_(
                    CustomerPriceTypeQualitySample.correct_group.is_(None),
                    CustomerPriceTypeQualitySample.correct_group != "data_check",
                ),
            )
        )
        if access.role == "quality":
            statement = statement.where(
                CustomerPriceTypeQualitySample.system_group == "special_review"
            )
        return [(row[0], row[1]) for row in self.session.execute(statement).all()]

    def list_review_cards(
        self,
        *,
        run_id: int,
        access: CustomerPriceTypeAccessScope,
        search: str | None,
    ) -> list[tuple[Any, ...]]:
        price_review = aliased(CustomerPriceTypeReview)
        action_review = aliased(CustomerPriceTypeReview)
        filters: list[Any] = [
            CustomerPriceTypeSnapshot.run_id == run_id,
            CustomerPriceTypeProfile.is_service_card.is_(False),
            *self._profile_scope_predicates(access),
        ]
        if search:
            pattern = f"%{search.strip()}%"
            filters.append(
                or_(
                    CustomerPriceTypeProfile.counterparty_ref.ilike(pattern),
                    CustomerPriceTypeProfile.counterparty_code.ilike(pattern),
                    CustomerPriceTypeProfile.counterparty_name.ilike(pattern),
                )
            )
        statement = (
            select(
                CustomerPriceTypeProfile,
                CustomerPriceTypeSnapshot,
                CustomerPriceTypeCase,
                price_review,
                action_review,
            )
            .join(
                CustomerPriceTypeSnapshot,
                CustomerPriceTypeSnapshot.profile_id == CustomerPriceTypeProfile.id,
            )
            .outerjoin(
                CustomerPriceTypeCase,
                CustomerPriceTypeCase.current_snapshot_id == CustomerPriceTypeSnapshot.id,
            )
            .outerjoin(
                price_review,
                and_(
                    price_review.snapshot_id == CustomerPriceTypeSnapshot.id,
                    price_review.review_kind == "price_type",
                ),
            )
            .outerjoin(
                action_review,
                and_(
                    action_review.snapshot_id == CustomerPriceTypeSnapshot.id,
                    action_review.review_kind == "client_action",
                ),
            )
            .where(*filters)
            .order_by(
                CustomerPriceTypeProfile.counterparty_name.asc(),
                CustomerPriceTypeProfile.id.asc(),
            )
        )
        return [tuple(row) for row in self.session.execute(statement).all()]

    def get_review_card(
        self,
        *,
        snapshot_id: int,
        access: CustomerPriceTypeAccessScope,
    ) -> tuple[Any, ...] | None:
        rows = self.list_review_cards(
            run_id=(
                self.session.scalar(
                    select(CustomerPriceTypeSnapshot.run_id).where(
                        CustomerPriceTypeSnapshot.id == snapshot_id
                    )
                )
                or -1
            ),
            access=access,
            search=None,
        )
        return next((row for row in rows if row[1].id == snapshot_id), None)

    def get_review(self, *, snapshot_id: int, review_kind: str) -> CustomerPriceTypeReview | None:
        return self.session.scalar(
            select(CustomerPriceTypeReview).where(
                CustomerPriceTypeReview.snapshot_id == snapshot_id,
                CustomerPriceTypeReview.review_kind == review_kind,
            )
        )

    def external_actions_for_review(self, review_id: int) -> list[CustomerPriceTypeExternalAction]:
        return list(
            self.session.scalars(
                select(CustomerPriceTypeExternalAction)
                .where(CustomerPriceTypeExternalAction.review_id == review_id)
                .order_by(CustomerPriceTypeExternalAction.id.asc())
            )
        )

    def onec_contract_actions(
        self, external_action_id: int
    ) -> list[CustomerPriceTypeOneCContractAction]:
        return list(
            self.session.scalars(
                select(CustomerPriceTypeOneCContractAction)
                .where(CustomerPriceTypeOneCContractAction.external_action_id == external_action_id)
                .order_by(CustomerPriceTypeOneCContractAction.id.asc())
            )
        )

    def cancellable_onec_action_for_case(
        self, case_id: int
    ) -> CustomerPriceTypeExternalAction | None:
        return self.session.scalar(
            select(CustomerPriceTypeExternalAction)
            .where(
                CustomerPriceTypeExternalAction.case_id == case_id,
                CustomerPriceTypeExternalAction.action_kind == "onec_change",
                CustomerPriceTypeExternalAction.status.in_(
                    ("held", "pending", "preflight", "ready_to_apply", "applying")
                ),
            )
            .order_by(CustomerPriceTypeExternalAction.id.desc())
        )

    def review_metrics(self, *, run_id: int, review_kind: str) -> dict[str, int | float]:
        reviews = list(
            self.session.scalars(
                select(CustomerPriceTypeReview)
                .join(
                    CustomerPriceTypeSnapshot,
                    CustomerPriceTypeSnapshot.id == CustomerPriceTypeReview.snapshot_id,
                )
                .where(
                    CustomerPriceTypeSnapshot.run_id == run_id,
                    CustomerPriceTypeReview.review_kind == review_kind,
                )
            )
        )
        total = len(reviews)
        corrected = sum(item.result == "correct" for item in reviews)
        data_issues = sum(item.result == "data_issue" for item in reviews)
        return {
            "reviewed_count": total,
            "confirmed_count": sum(item.result == "confirm" for item in reviews),
            "corrected_count": corrected,
            "no_action_count": sum(item.result == "no_action" for item in reviews),
            "data_issue_count": data_issues,
            "correction_rate": round(corrected / total, 4) if total else 0.0,
        }

    def internal_no_change_audit_snapshot_ids(self, *, run_id: int, limit: int = 30) -> set[int]:
        rows = self.session.scalars(
            select(CustomerPriceTypeSnapshot.id)
            .join(
                CustomerPriceTypeProfile,
                CustomerPriceTypeProfile.id == CustomerPriceTypeSnapshot.profile_id,
            )
            .where(
                CustomerPriceTypeSnapshot.run_id == run_id,
                CustomerPriceTypeSnapshot.source_status == "ready",
                CustomerPriceTypeSnapshot.current_price_type.is_not(None),
                CustomerPriceTypeSnapshot.recommended_price_type
                == CustomerPriceTypeSnapshot.current_price_type,
                CustomerPriceTypeSnapshot.system_recommendation.not_in(
                    (
                        "data_check",
                        "insufficient_history",
                        "new_client",
                        "excluded_without_sales_history",
                        "excluded_service_card",
                    )
                ),
                or_(
                    CustomerPriceTypeSnapshot.case_type.is_(None),
                    CustomerPriceTypeSnapshot.case_type != "data_check",
                ),
                CustomerPriceTypeProfile.is_service_card.is_(False),
            )
            .order_by(
                CustomerPriceTypeSnapshot.snapshot_hash.asc(),
                CustomerPriceTypeSnapshot.id.asc(),
            )
            .limit(limit)
        ).all()
        return {int(item) for item in rows}
