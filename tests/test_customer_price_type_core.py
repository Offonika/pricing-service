from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.domains.customer_price_types import (
    ContractFact,
    CustomerPriceTypeAccessScope,
    CustomerPriceTypeFacts,
    CustomerPriceTypeRulesEngine,
    build_source_fingerprint,
    canonical_sha256,
    load_price_type_ruleset,
)
from app.infrastructure.customer_price_types import (
    CustomerPriceTypePersistenceConflict,
    SqlAlchemyCustomerPriceTypeRepository,
)
from app.models import Base
from app.models.customer_price_type import (
    CustomerPriceTypeCase,
    CustomerPriceTypeCaseEvent,
    CustomerPriceTypeProfile,
    CustomerPriceTypeRun,
    CustomerPriceTypeSnapshot,
)
from app.services.customer_price_types import (
    CustomerPriceTypeReadService,
    CustomerPriceTypeRunService,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
RULESET = load_price_type_ruleset(REPO_ROOT / "config/price_types/ruleset.yaml")
ENGINE = CustomerPriceTypeRulesEngine(RULESET)


def _ref(value: int) -> str:
    return f"0x{value:032x}"


def _facts(
    *,
    counterparty_ref: str = _ref(1),
    counterparty_code: str = "РБ000001",
    price_type: str = "2.Бронзовый",
    monthly: tuple[str, str, str] = ("3000", "3000", "3000"),
    history_months: int = 12,
    economics_status: str = "ok",
) -> CustomerPriceTypeFacts:
    sales = {
        "2026-04": Decimal(monthly[0]),
        "2026-05": Decimal(monthly[1]),
        "2026-06": Decimal(monthly[2]),
    }
    for month in (
        "2025-07",
        "2025-08",
        "2025-09",
        "2025-10",
        "2025-11",
        "2025-12",
        "2026-01",
        "2026-02",
        "2026-03",
    ):
        sales.setdefault(month, Decimal("1"))
    total = sum((sales[key] for key in ("2026-04", "2026-05", "2026-06")), Decimal())
    return CustomerPriceTypeFacts(
        counterparty_ref=counterparty_ref,
        counterparty_code=counterparty_code,
        counterparty_name=f"Клиент {counterparty_code}",
        snapshot_month=date(2026, 6, 1),
        contracts=(
            ContractFact(
                contract_ref=_ref(100),
                contract_name="Основной",
                price_type_name=price_type,
            ),
        ),
        monthly_sales=sales,
        source_statuses={
            "contracts": "ready",
            "sales_history": "ready",
            "ledger_reconciliation": "ready",
            "master_data": "ready",
            "economics": "ready",
            "return_quality": "ready",
        },
        history_coverage_months=history_months,
        direct_onec_total_3m=total,
        ledger_total_3m=total,
        economics_status=economics_status,
        economics={"status": economics_status},
        owner_ref="manager-1",
        department_ref="department-1",
    )


@pytest.mark.parametrize("level", RULESET.levels)
def test_all_level_boundaries(level) -> None:
    norm = level.retention_norm_3m
    hold = level.hold_last_month
    keep = ENGINE.evaluate(
        _facts(
            price_type=level.price_type_prefix,
            monthly=("0", "0", str(norm)),
        )
    )
    retention = ENGINE.evaluate(
        _facts(
            price_type=level.price_type_prefix,
            monthly=("0", "0", str(hold)),
        )
    )
    isolate = ENGINE.evaluate(
        _facts(
            price_type=level.price_type_prefix,
            monthly=("1", "1", str(hold - 1)),
        )
    )

    assert keep.recommendation == "keep_current"
    assert keep.action_required is False
    assert retention.recommendation == "manager_retention"
    assert retention.action_required is True
    assert isolate.recommendation == "isolate"
    assert isolate.recommended_price_type == level.downgrade_to


def test_retail_and_variants_never_create_upgrade_case() -> None:
    retail = ENGINE.evaluate(_facts(price_type="Розница", monthly=("200000",) * 3))
    bronze = ENGINE.evaluate(_facts(price_type="2.Бронзовый", monthly=("120000",) * 3))
    cashless = ENGINE.evaluate(_facts(price_type="3.Серебряный бн", monthly=("120000",) * 3))
    usd = ENGINE.evaluate(_facts(price_type="4.Золотой USD", monthly=("400000",) * 3))
    key_account = ENGINE.evaluate(_facts(price_type="Key Account", monthly=("1",) * 3))

    assert retail.recommendation == "informational_upgrade_candidate"
    assert retail.action_required is False
    assert "upgrade_freeze" in retail.stop_factors
    assert bronze.recommendation == "informational_upgrade_candidate"
    assert bronze.action_required is False
    assert cashless.current_level == "silver"
    assert cashless.price_type_variant == "бн"
    assert usd.current_level == "gold"
    assert usd.price_type_variant == "usd"
    assert key_account.current_level == "key_account"
    assert key_account.recommendation == "keep_current"
    assert key_account.action_required is False


def test_same_level_contracts_are_combined_and_conflicting_levels_need_data_check() -> None:
    base = _facts()
    same_level = ENGINE.evaluate(
        replace(
            base,
            contracts=base.contracts
            + (
                ContractFact(
                    contract_ref=_ref(101),
                    contract_name="Второй",
                    price_type_name="2.Бронзовый бн",
                ),
            ),
        )
    )
    duplicate_type = ENGINE.evaluate(
        replace(
            base,
            contracts=base.contracts
            + (
                ContractFact(
                    contract_ref=_ref(105),
                    contract_name="Ещё один бронзовый",
                    price_type_name="2.Бронзовый",
                ),
            ),
        )
    )
    different_levels = ENGINE.evaluate(
        replace(
            base,
            contracts=base.contracts
            + (
                ContractFact(
                    contract_ref=_ref(102),
                    contract_name="Другой уровень",
                    price_type_name="3.Серебряный",
                ),
            ),
        )
    )

    assert same_level.reasons == ("conflicting_price_type_variants",)
    assert duplicate_type.current_level == "bronze"
    assert duplicate_type.current_price_type == "2.Бронзовый"
    assert duplicate_type.total_3m == Decimal("9000")
    assert different_levels.reasons == ("conflicting_price_levels",)


def test_working_contracts_determine_exact_price_type_and_ignore_unused_levels() -> None:
    base = _facts()
    bronze_ref = _ref(110)
    unused_retail_ref = _ref(111)
    resolved = ENGINE.evaluate(
        replace(
            base,
            contracts=(
                ContractFact(
                    contract_ref=unused_retail_ref,
                    contract_name="Старый розничный",
                    price_type_name="Розница",
                ),
                ContractFact(
                    contract_ref=bronze_ref,
                    contract_name="Основной",
                    price_type_name="2.Бронзовый",
                    sale_document_count_12m=3,
                    sales_amount_12m=Decimal("2760"),
                    last_sale_at=date(2026, 3, 5),
                    is_working=True,
                ),
            ),
        )
    )
    variant_conflict = ENGINE.evaluate(
        replace(
            base,
            contracts=(
                ContractFact(
                    contract_ref=_ref(112),
                    contract_name="Наличный",
                    price_type_name="2.Бронзовый",
                    sale_document_count_12m=1,
                    is_working=True,
                ),
                ContractFact(
                    contract_ref=_ref(113),
                    contract_name="Безналичный",
                    price_type_name="2.Бронзовый бн",
                    sale_document_count_12m=1,
                    is_working=True,
                ),
            ),
        )
    )

    assert resolved.current_price_type == "2.Бронзовый"
    assert resolved.calculation_contract_refs == (bronze_ref,)
    assert unused_retail_ref not in resolved.calculation_contract_refs
    assert variant_conflict.reasons == ("conflicting_price_type_variants",)


def test_invalid_contract_price_types_and_partial_sources_are_data_checks() -> None:
    base = _facts()
    unknown = ENGINE.evaluate(
        replace(
            base,
            contracts=(
                ContractFact(
                    contract_ref=_ref(100),
                    contract_name="Основной",
                    price_type_name="Неизвестный",
                ),
            ),
        )
    )
    missing = ENGINE.evaluate(
        replace(
            base,
            contracts=(
                ContractFact(
                    contract_ref=_ref(103),
                    contract_name="Без типа",
                    price_type_name=None,
                    price_type_missing=True,
                ),
            ),
        )
    )
    marked = ENGINE.evaluate(
        replace(
            base,
            contracts=(
                ContractFact(
                    contract_ref=_ref(104),
                    contract_name="Помеченный тип",
                    price_type_name="2.Бронзовый",
                    price_type_marked=True,
                ),
            ),
        )
    )
    partial = ENGINE.evaluate(
        replace(base, source_statuses={**base.source_statuses, "master_data": "missing"})
    )
    omitted = ENGINE.evaluate(replace(base, source_statuses={"contracts": "ready"}))

    assert unknown.reasons == ("unknown_price_type",)
    assert missing.reasons == ("price_type_missing",)
    assert marked.reasons == ("price_type_marked",)
    assert partial.recommendation == "data_check"
    assert partial.source_status == "partial"
    assert omitted.recommendation == "data_check"
    assert "source_sales_history_missing" in omitted.stop_factors


def test_working_contract_has_priority_over_incomplete_unused_contracts() -> None:
    base = _facts()
    usable_ref = _ref(106)
    mixed = ENGINE.evaluate(
        replace(
            base,
            contracts=(
                ContractFact(
                    contract_ref=_ref(103),
                    contract_name="Пустой договор",
                    price_type_name=None,
                    price_type_missing=True,
                ),
                ContractFact(
                    contract_ref=usable_ref,
                    contract_name="Договор с покупателем",
                    price_type_name="2.Бронзовый",
                    sale_document_count_12m=1,
                    is_working=True,
                ),
                ContractFact(
                    contract_ref=_ref(107),
                    contract_name="Неизвестный тип",
                    price_type_name="Старый тип",
                ),
            ),
        )
    )
    conflict = ENGINE.evaluate(
        replace(
            base,
            contracts=(
                ContractFact(
                    contract_ref=_ref(103),
                    contract_name="Пустой договор",
                    price_type_name=None,
                    price_type_missing=True,
                ),
                ContractFact(
                    contract_ref=usable_ref,
                    contract_name="Бронзовый",
                    price_type_name="2.Бронзовый",
                    sale_document_count_12m=1,
                    is_working=True,
                ),
                ContractFact(
                    contract_ref=_ref(108),
                    contract_name="Серебряный",
                    price_type_name="3.Серебряный",
                    sale_document_count_12m=1,
                    is_working=True,
                ),
            ),
        )
    )

    assert mixed.current_level == "bronze"
    assert mixed.current_price_type == "2.Бронзовый"
    assert mixed.recommendation == "isolate"
    assert mixed.calculation_contract_refs == (usable_ref,)
    assert mixed.price_type_change_contract_refs == (usable_ref,)
    assert mixed.reasons != ("price_type_missing",)
    assert conflict.reasons == ("conflicting_price_levels",)
    assert set(conflict.calculation_contract_refs) == {usable_ref, _ref(108)}
    assert conflict.price_type_change_contract_refs == ()


def test_never_purchased_card_is_excluded_before_missing_price_type() -> None:
    missing_contract = ContractFact(
        contract_ref=_ref(103),
        contract_name="Без типа",
        price_type_name=None,
        price_type_missing=True,
    )
    never_purchased = ENGINE.evaluate(
        replace(
            _facts(),
            contracts=(missing_contract,),
            monthly_sales={},
            first_activity_date=None,
            history_coverage_months=0,
            direct_onec_total_3m=Decimal("0"),
            ledger_total_3m=Decimal("0"),
        )
    )
    sales_without_first_activity = ENGINE.evaluate(
        replace(
            _facts(),
            contracts=(missing_contract,),
            first_activity_date=None,
        )
    )
    history_without_window_sales = ENGINE.evaluate(
        replace(
            _facts(),
            contracts=(missing_contract,),
            monthly_sales={},
            first_activity_date=date(2025, 1, 15),
            history_coverage_months=12,
            direct_onec_total_3m=Decimal("0"),
            ledger_total_3m=Decimal("0"),
        )
    )

    assert never_purchased.excluded is True
    assert never_purchased.action_required is False
    assert never_purchased.recommendation == "excluded_without_sales_history"
    assert never_purchased.stop_factors == ("no_sales_history",)
    assert sales_without_first_activity.reasons == ("price_type_missing",)
    assert history_without_window_sales.reasons == ("price_type_missing",)


def test_history_coverage_blocks_dead_soul_and_full_history_enables_it() -> None:
    monthly = {f"2025-{month:02d}": Decimal("0") for month in range(7, 13)}
    monthly.update({f"2026-{month:02d}": Decimal("0") for month in range(1, 7)})
    base = replace(
        _facts(), monthly_sales=monthly, direct_onec_total_3m=Decimal(), ledger_total_3m=Decimal()
    )

    insufficient = ENGINE.evaluate(replace(base, history_coverage_months=2))
    dead = ENGINE.evaluate(replace(base, history_coverage_months=12))

    assert insufficient.recommendation == "insufficient_history"
    assert dead.recommendation == "recovery"
    assert dead.recommended_price_type == "2.Бронзовый"
    assert dead.action_required is True


def test_economics_and_source_mismatch_block_isolation() -> None:
    base = _facts(monthly=("1", "1", "1"))
    missing = ENGINE.evaluate(replace(base, economics_status="missing", economics={}))
    conflict = ENGINE.evaluate(
        replace(
            base,
            direct_onec_total_3m=Decimal("100"),
            ledger_total_3m=Decimal("80"),
        )
    )

    assert missing.reasons == ("economics_missing",)
    assert conflict.source_status == "conflict"
    assert conflict.reasons == ("source_mismatch",)
    assert conflict.current_price_type == "2.Бронзовый"
    assert conflict.recommended_price_type is None


def test_manual_overrides_and_key_account_are_human_only() -> None:
    quality = ENGINE.evaluate(_facts(counterparty_code="РБ035953", monthly=("10000",) * 3))
    moratorium = ENGINE.evaluate(_facts(counterparty_code="РБ034611", monthly=("1", "1", "1")))
    key_account = ENGINE.evaluate(replace(_facts(monthly=("1", "1", "1")), key_account_flag=True))
    do_not_touch = ENGINE.evaluate(_facts(counterparty_code="РБ008821"))
    expired = ENGINE.evaluate(
        replace(
            _facts(counterparty_code="РБ034611", monthly=("1", "1", "1")),
            snapshot_month=date(2026, 7, 1),
        )
    )

    assert quality.recommendation == "manual_override:quality_manual_review"
    assert quality.action_required is True
    assert moratorium.action_required is False
    assert key_account.review_type == "key_account"
    assert do_not_touch.action_required is False
    assert expired.recommendation != "manual_override:moratorium"


def test_registry_hygiene_returns_and_dead_soul_economics() -> None:
    excluded = ENGINE.evaluate(_facts(counterparty_code="РБ005290"))
    hygiene = ENGINE.evaluate(_facts(counterparty_code="РБ032342"))
    advisory = ENGINE.evaluate(
        replace(
            _facts(monthly=("5000", "5000", "5000")),
            returns={"return_rate_pct": "99.00"},
        )
    )
    quality = ENGINE.evaluate(replace(_facts(), return_review_type="quality"))
    zeros = {f"2025-{month:02d}": Decimal("0") for month in range(7, 13)}
    zeros.update({f"2026-{month:02d}": Decimal("0") for month in range(1, 7)})
    dead_without_economics = ENGINE.evaluate(
        replace(
            _facts(),
            monthly_sales=zeros,
            direct_onec_total_3m=Decimal("0"),
            ledger_total_3m=Decimal("0"),
            economics_status="missing",
            economics={},
        )
    )

    assert excluded.excluded is True
    assert excluded.registry_class == "служебный инструмент"
    assert hygiene.excluded is False
    assert hygiene.is_hygiene is True
    assert advisory.recommendation == "keep_current"
    assert quality.review_type == "quality"
    assert quality.action_required is True
    assert dead_without_economics.recommendation == "recovery"
    assert dead_without_economics.recommended_price_type == "2.Бронзовый"


def _session_factory(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'customer_price_types.db'}")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def test_idempotent_persistence_and_case_dedupe(tmp_path: Path) -> None:
    engine, factory = _session_factory(tmp_path)
    try:
        service = CustomerPriceTypeRunService(factory)
        fact = _facts(monthly=("1", "1", "1"))
        statuses = {"contracts": "ready", "sales_history": "ready"}

        first = service.execute([fact], source_statuses=statuses)
        repeated = service.execute([fact], source_statuses=statuses)
        replay = service.execute([fact], source_statuses=statuses, run_key="technical-replay-1")

        assert first.created is True
        assert repeated.created is False
        assert repeated.run_id == first.run_id
        assert replay.run_id != first.run_id
        with Session(engine) as session:
            assert session.scalar(select(func_count(CustomerPriceTypeRun))) == 2
            assert session.scalar(select(func_count(CustomerPriceTypeProfile))) == 1
            assert session.scalar(select(func_count(CustomerPriceTypeSnapshot))) == 2
            assert session.scalar(select(func_count(CustomerPriceTypeCase))) == 1
            assert session.scalar(select(func_count(CustomerPriceTypeCaseEvent))) == 1
    finally:
        engine.dispose()


def func_count(model):
    from sqlalchemy import func

    return func.count(model.id)


def test_never_purchased_card_is_removed_from_open_cases_on_recalculation(
    tmp_path: Path,
) -> None:
    engine, factory = _session_factory(tmp_path)
    try:
        service = CustomerPriceTypeRunService(factory)
        missing_contract = ContractFact(
            contract_ref=_ref(103),
            contract_name="Без типа",
            price_type_name=None,
            price_type_missing=True,
        )
        active = replace(_facts(), contracts=(missing_contract,))
        statuses = {
            "contracts": "ready",
            "sales_history": "ready",
            "ledger_reconciliation": "ready",
            "master_data": "ready",
        }
        service.execute([active], source_statuses=statuses)
        inactive = replace(
            active,
            monthly_sales={},
            first_activity_date=None,
            history_coverage_months=0,
            direct_onec_total_3m=Decimal("0"),
            ledger_total_3m=Decimal("0"),
        )
        excluded_run = service.execute(
            [inactive],
            source_statuses=statuses,
            run_key="exclude-never-purchased",
        )

        with Session(engine) as session:
            profile = session.scalar(select(CustomerPriceTypeProfile))
            case = session.scalar(select(CustomerPriceTypeCase))
            run = session.get(CustomerPriceTypeRun, excluded_run.run_id)
            events = session.scalars(
                select(CustomerPriceTypeCaseEvent).order_by(CustomerPriceTypeCaseEvent.id)
            ).all()

            assert profile.open_case_id is None
            assert profile.is_service_card is False
            assert case is not None
            assert run.excluded_count == 1
            assert run.actionable_count == 0
            assert [event.event_type for event in events] == [
                "case_created",
                "profile_excluded",
            ]
    finally:
        engine.dispose()


def test_changed_snapshot_resets_approval_once(tmp_path: Path) -> None:
    engine, factory = _session_factory(tmp_path)
    try:
        service = CustomerPriceTypeRunService(factory)
        base = _facts(monthly=("1", "1", "1"))
        statuses = {"contracts": "ready"}
        first = service.execute([base], source_statuses=statuses)
        with Session(engine) as session:
            case = session.scalar(select(CustomerPriceTypeCase))
            case.approval_status = "approved"
            case.human_final_decision = "downgrade"
            case.approved_snapshot_hash = session.get(
                CustomerPriceTypeSnapshot, case.current_snapshot_id
            ).snapshot_hash
            session.commit()

        changed = replace(
            base,
            monthly_sales={**base.monthly_sales, "2026-06": Decimal("2")},
            direct_onec_total_3m=Decimal("4"),
            ledger_total_3m=Decimal("4"),
        )
        second = service.execute([changed], source_statuses=statuses, run_key="changed-run")
        repeated = service.execute([changed], source_statuses=statuses, run_key="changed-run")

        assert first.run_id != second.run_id
        assert repeated.created is False
        with Session(engine) as session:
            case = session.scalar(select(CustomerPriceTypeCase))
            events = session.scalars(
                select(CustomerPriceTypeCaseEvent).order_by(CustomerPriceTypeCaseEvent.id)
            ).all()
            assert case.approval_status == "not_requested"
            assert case.human_final_decision is None
            assert case.version == 2
            assert [event.event_type for event in events] == ["case_created", "snapshot_changed"]
    finally:
        engine.dispose()


def test_resolved_data_check_is_closed_or_reclassified_without_duplicate_case(
    tmp_path: Path,
) -> None:
    engine, factory = _session_factory(tmp_path)
    try:
        service = CustomerPriceTypeRunService(factory)
        base = _facts(monthly=("4000", "4000", "4000"))
        conflicting = replace(
            base,
            contracts=(
                ContractFact(
                    _ref(120),
                    "Бронзовый",
                    "2.Бронзовый",
                    sale_document_count_12m=2,
                    is_working=True,
                ),
                ContractFact(
                    _ref(121),
                    "Серебряный",
                    "3.Серебряный",
                    sale_document_count_12m=1,
                    is_working=True,
                ),
            ),
        )
        statuses = {"contracts": "ready"}
        service.execute([conflicting], source_statuses=statuses, run_key="conflict-first")

        resolved = replace(
            base,
            contracts=(
                ContractFact(
                    _ref(120),
                    "Бронзовый",
                    "2.Бронзовый",
                    sale_document_count_12m=2,
                    is_working=True,
                ),
                ContractFact(_ref(121), "Серебряный", "3.Серебряный"),
            ),
        )
        service.execute([resolved], source_statuses=statuses, run_key="resolved-keep")

        with Session(engine) as session:
            profile = session.scalar(select(CustomerPriceTypeProfile))
            case = session.scalar(select(CustomerPriceTypeCase))
            events = session.scalars(
                select(CustomerPriceTypeCaseEvent).order_by(CustomerPriceTypeCaseEvent.id)
            ).all()
            assert profile.open_case_id is None
            assert case.stage == "CLOSED_KEEP"
            assert session.scalar(select(func_count(CustomerPriceTypeCase))) == 1
            assert [event.event_type for event in events] == [
                "case_created",
                "case_auto_closed",
            ]

        actionable = replace(
            resolved,
            monthly_sales={
                "2026-04": Decimal("1"),
                "2026-05": Decimal("1"),
                "2026-06": Decimal("1"),
            },
            direct_onec_total_3m=Decimal("3"),
            ledger_total_3m=Decimal("3"),
        )
        service.execute([actionable], source_statuses=statuses, run_key="resolved-actionable")
        with Session(engine) as session:
            profile = session.scalar(select(CustomerPriceTypeProfile))
            case = session.scalar(select(CustomerPriceTypeCase))
            events = session.scalars(
                select(CustomerPriceTypeCaseEvent).order_by(CustomerPriceTypeCaseEvent.id)
            ).all()
            assert profile.open_case_id == case.id
            assert case.case_type == "isolate"
            assert case.stage == "NEW"
            assert session.scalar(select(func_count(CustomerPriceTypeCase))) == 1
            assert events[-1].event_type == "case_reclassified"
    finally:
        engine.dispose()


def test_exported_case_is_not_auto_closed_before_readback(tmp_path: Path) -> None:
    engine, factory = _session_factory(tmp_path)
    try:
        service = CustomerPriceTypeRunService(factory)
        active = _facts(monthly=("1", "1", "1"))
        service.execute([active], source_statuses={"contracts": "ready"}, run_key="active")
        with Session(engine) as session:
            case = session.scalar(select(CustomerPriceTypeCase))
            case.onec_export_status = "exported"
            case.onec_readback_status = "pending"
            session.commit()

        keep = replace(
            active,
            monthly_sales={
                "2026-04": Decimal("4000"),
                "2026-05": Decimal("4000"),
                "2026-06": Decimal("4000"),
            },
            direct_onec_total_3m=Decimal("12000"),
            ledger_total_3m=Decimal("12000"),
        )
        service.execute([keep], source_statuses={"contracts": "ready"}, run_key="keep")
        with Session(engine) as session:
            profile = session.scalar(select(CustomerPriceTypeProfile))
            case = session.scalar(select(CustomerPriceTypeCase))
            assert profile.open_case_id == case.id
            assert case.stage != "CLOSED_KEEP"
            repository = SqlAlchemyCustomerPriceTypeRepository(session)
            latest_run = repository.latest_run()
            _, total = repository.list_cases(
                access=CustomerPriceTypeAccessScope(
                    actor="test", role="internal", can_view_money=True
                ),
                run_id=latest_run.id,
                snapshot_month=latest_run.snapshot_month,
                worklist=None,
                stage=None,
                review_type=None,
                source_status=None,
                department_ref=None,
                search=None,
                limit=100,
                offset=0,
            )
            assert total == 1
    finally:
        engine.dispose()


def test_run_key_conflict(tmp_path: Path) -> None:
    engine, factory = _session_factory(tmp_path)
    try:
        service = CustomerPriceTypeRunService(factory)
        service.execute([_facts()], source_statuses={}, run_key="fixed-key")
        with pytest.raises(CustomerPriceTypePersistenceConflict):
            service.execute(
                [_facts(monthly=("4", "4", "4"))],
                source_statuses={},
                run_key="fixed-key",
            )
    finally:
        engine.dispose()


def test_source_fingerprint_is_order_independent() -> None:
    first = _facts(counterparty_ref=_ref(1))
    second = _facts(counterparty_ref=_ref(2), counterparty_code="РБ000002")
    statuses = {"contracts": "ready"}

    assert build_source_fingerprint(
        [first, second], source_statuses=statuses
    ) == build_source_fingerprint([second, first], source_statuses=statuses)


def test_canonical_hash_normalizes_money_dates_and_collection_order() -> None:
    left = {
        "amount": Decimal("1"),
        "month": date(2026, 6, 1),
        "items": [{"b": 2}, {"a": 1}],
    }
    right = {
        "items": [{"a": 1}, {"b": 2}],
        "month": "2026-06-01",
        "amount": "1.00",
    }

    assert canonical_sha256(left) == canonical_sha256(right)


def test_partial_failed_and_concurrent_technical_runs(tmp_path: Path, monkeypatch) -> None:
    engine, factory = _session_factory(tmp_path)
    try:
        service = CustomerPriceTypeRunService(factory)
        partial_fact = replace(
            _facts(counterparty_ref=_ref(10)),
            source_statuses={**_facts().source_statuses, "master_data": "missing"},
        )
        partial = service.execute(
            [partial_fact],
            source_statuses={"master_data": "missing"},
            run_key="partial-run",
        )
        assert partial.status == "partial"

        fact = _facts(counterparty_ref=_ref(20), monthly=("1", "1", "1"))
        with ThreadPoolExecutor(max_workers=2) as pool:
            completed = list(
                pool.map(
                    lambda key: service.execute(
                        [fact], source_statuses={"contracts": "ready"}, run_key=key
                    ),
                    ("concurrent-a", "concurrent-b"),
                )
            )
        assert {item.status for item in completed} == {"completed"}
        with Session(engine) as session:
            assert (
                session.scalar(
                    select(func_count(CustomerPriceTypeCase)).where(
                        CustomerPriceTypeCase.profile_id
                        == session.scalar(
                            select(CustomerPriceTypeProfile.id).where(
                                CustomerPriceTypeProfile.counterparty_ref == _ref(20)
                            )
                        )
                    )
                )
                == 1
            )

        original = SqlAlchemyCustomerPriceTypeRepository.persist_results

        def fail_persistence(*args, **kwargs):
            raise RuntimeError("forced persistence failure")

        monkeypatch.setattr(
            SqlAlchemyCustomerPriceTypeRepository,
            "persist_results",
            fail_persistence,
        )
        with pytest.raises(RuntimeError, match="forced persistence failure"):
            service.execute(
                [_facts(counterparty_ref=_ref(30))],
                source_statuses={"contracts": "ready"},
                run_key="failed-run",
            )
        monkeypatch.setattr(
            SqlAlchemyCustomerPriceTypeRepository,
            "persist_results",
            original,
        )
        with Session(engine) as session:
            failed = session.scalar(
                select(CustomerPriceTypeRun).where(CustomerPriceTypeRun.run_key == "failed-run")
            )
            assert failed.status == "failed"
            assert "forced persistence failure" in failed.error_summary

            session.add(CustomerPriceTypeProfile(counterparty_ref=_ref(40).upper()))
            with pytest.raises(IntegrityError):
                session.commit()
    finally:
        engine.dispose()


def test_engine_failure_is_persisted_and_excluded_profile_leaves_worklists(
    tmp_path: Path,
) -> None:
    engine, factory = _session_factory(tmp_path)
    statuses = {
        "contracts": "ready",
        "sales_history": "ready",
        "ledger_reconciliation": "ready",
        "master_data": "ready",
    }
    try:
        service = CustomerPriceTypeRunService(factory)
        base = _facts(monthly=("1", "1", "1"))
        service.execute([base], source_statuses=statuses, run_key="before-exclusion")
        service.execute(
            [replace(base, service_class="служебный инструмент")],
            source_statuses=statuses,
            run_key="excluded-profile",
        )
        scope = CustomerPriceTypeAccessScope(
            actor="test",
            role="internal",
            can_view_money=True,
        )
        with Session(engine) as session:
            profile = session.scalar(select(CustomerPriceTypeProfile))
            events = session.scalars(
                select(CustomerPriceTypeCaseEvent).order_by(CustomerPriceTypeCaseEvent.id)
            ).all()
            worklists = CustomerPriceTypeReadService(session).worklists(
                snapshot_month=date(2026, 6, 1),
                access=scope,
            )
            assert profile.is_service_card is True
            assert profile.open_case_id is None
            assert worklists["worklists"]["isolate"] == 0
            assert [event.event_type for event in events] == [
                "case_created",
                "profile_excluded",
            ]

        invalid = replace(base, monthly_sales={"2026-06": "not-a-decimal"})
        with pytest.raises(InvalidOperation):
            service.execute([invalid], source_statuses=statuses, run_key="invalid-facts")
        with Session(engine) as session:
            failed = session.scalar(
                select(CustomerPriceTypeRun).where(CustomerPriceTypeRun.run_key == "invalid-facts")
            )
            assert failed is not None
            assert failed.status == "failed"
    finally:
        engine.dispose()


def test_started_run_is_resumed_idempotently(tmp_path: Path) -> None:
    engine, factory = _session_factory(tmp_path)
    facts = [_facts(monthly=("1", "1", "1"))]
    statuses = {"contracts": "ready"}
    fingerprint = build_source_fingerprint(facts, source_statuses=statuses)
    try:
        with factory() as session:
            repository = SqlAlchemyCustomerPriceTypeRepository(session)
            started = repository.create_run(
                run_key="interrupted-run",
                snapshot_month=date(2026, 6, 1),
                ruleset_version=RULESET.version,
                as_of=date(2026, 6, 30),
                window_start=date(2026, 4, 1),
                window_end=date(2026, 7, 1),
                source_statuses=statuses,
                source_fingerprint=fingerprint,
                input_count=1,
            )
            run_id = started.id
            session.commit()

        result = CustomerPriceTypeRunService(factory).execute(
            facts,
            source_statuses=statuses,
            run_key="interrupted-run",
        )

        assert result.run_id == run_id
        assert result.status == "completed"
        assert result.created is False
        with Session(engine) as session:
            assert session.scalar(select(func_count(CustomerPriceTypeSnapshot))) == 1
            assert session.scalar(select(func_count(CustomerPriceTypeCase))) == 1
    finally:
        engine.dispose()
