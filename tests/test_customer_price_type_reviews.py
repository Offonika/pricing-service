from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.api.customer_price_types import require_customer_price_type_access
from app.api.dependencies import get_db
from app.core.config import Settings
from app.domains.customer_price_types import (
    ContractFact,
    CustomerPriceTypeAccessScope,
    CustomerPriceTypeFacts,
)
from app.main import app
from app.models import Base
from app.models.customer_price_type import (
    CustomerPriceTypeCase,
    CustomerPriceTypeExternalAction,
    CustomerPriceTypeOneCContractAction,
    CustomerPriceTypeReview,
    CustomerPriceTypeSnapshot,
)
from app.services.customer_price_type_reviews import CustomerPriceTypeReviewService
from app.services.customer_price_types import CustomerPriceTypeRunService


def _ref(value: int) -> str:
    return f"0x{value:032x}"


def _facts(
    value: int,
    *,
    price_type: str = "2.Бронзовый",
    monthly: tuple[str, str, str] = ("1", "1", "1"),
    return_review_type: str | None = None,
    history_months: int = 12,
) -> CustomerPriceTypeFacts:
    total = sum(Decimal(item) for item in monthly)
    return CustomerPriceTypeFacts(
        counterparty_ref=_ref(value),
        counterparty_code=f"РБ{value:06d}",
        counterparty_name=f"Клиент {value}",
        snapshot_month=date(2026, 6, 1),
        contracts=(
            ContractFact(
                contract_ref=_ref(1000 + value),
                contract_name="Основной рабочий договор",
                price_type_name=price_type,
                sale_document_count_12m=3,
                sales_amount_12m=Decimal("50000"),
                is_working=True,
            ),
        ),
        monthly_sales={
            "2026-04": Decimal(monthly[0]),
            "2026-05": Decimal(monthly[1]),
            "2026-06": Decimal(monthly[2]),
        },
        source_statuses={
            "contracts": "ready",
            "sales_history": "ready",
            "ledger_reconciliation": "ready",
            "master_data": "ready",
        },
        owner_ref="manager-1",
        owner_name="Менеджер 1",
        department_ref="department-1",
        department_name="Розничная сеть",
        history_coverage_months=history_months,
        direct_onec_total_3m=total,
        ledger_total_3m=total,
        economics_status="ok",
        economics={"status": "ok"},
        return_review_type=return_review_type,
    )


def _client(factory, access: CustomerPriceTypeAccessScope) -> TestClient:
    def db_dependency():
        with factory() as session:
            yield session

    app.dependency_overrides = {
        get_db: db_dependency,
        require_customer_price_type_access: lambda: access,
    }
    return TestClient(app)


def _upgrade_facts(value: int) -> CustomerPriceTypeFacts:
    return _facts(value, monthly=("120000", "120000", "120000"))


def test_review_cards_separate_price_type_and_client_action(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'reviews.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    run = CustomerPriceTypeRunService(factory).execute(
        [_facts(1, return_review_type="quality"), _facts(2, history_months=2)],
        source_statuses={"contracts": "ready"},
    )
    with Session(engine) as session:
        quality_snapshot = session.scalar(
            select(CustomerPriceTypeSnapshot).where(
                CustomerPriceTypeSnapshot.run_id == run.run_id,
                CustomerPriceTypeSnapshot.counterparty_ref == _ref(1),
            )
        )
    access = CustomerPriceTypeAccessScope(actor="arsen", role="network_head", can_view_money=True)
    client = _client(factory, access)
    try:
        response = client.get(
            "/api/customer-price-types/reviews/cards",
            params={"pending_only": True},
        )
        assert response.status_code == 200
        assert response.json()["total"] == 1
        card = response.json()["payload"][0]
        assert card["snapshot_id"] == quality_snapshot.id
        assert card["price_type"]["can_review"] is False
        assert card["client_action"]["can_review"] is True
        assert card["client_action"]["system_value"] == "quality"

        forbidden_price_review = client.put(
            f"/api/customer-price-types/reviews/cards/{quality_snapshot.id}/price-type",
            json={
                "result": "confirm",
                "corrected_value": None,
                "comment": None,
                "expected_version": 0,
                "snapshot_hash": quality_snapshot.snapshot_hash,
            },
        )
        assert forbidden_price_review.status_code == 422

        saved = client.put(
            f"/api/customer-price-types/reviews/cards/{quality_snapshot.id}/client-action",
            json={
                "result": "confirm",
                "corrected_value": None,
                "comment": None,
                "expected_version": 0,
                "snapshot_hash": quality_snapshot.snapshot_hash,
            },
        )
        assert saved.status_code == 200
        assert saved.json()["card"]["client_action"]["result"] == "confirm"
        assert saved.json()["card"]["client_action"]["decision_mode"] == "test"
        assert saved.json()["card"]["client_action"]["external_state"] == "held"
        assert saved.json()["card"]["price_type"]["review_id"] is None
        with Session(engine) as session:
            action = session.scalar(select(CustomerPriceTypeExternalAction))
            assert action.action_kind == "bitrix_case"
            assert action.execution_allowed_at_decision is False
            assert action.status == "held"
    finally:
        app.dependency_overrides = {}
        engine.dispose()


def test_price_review_validates_hash_and_creates_exact_contract_outbox(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'price-review.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    CustomerPriceTypeRunService(factory).execute(
        [_upgrade_facts(10)], source_statuses={"contracts": "ready"}
    )
    with Session(engine) as session:
        snapshot = session.scalar(select(CustomerPriceTypeSnapshot))
        snapshot_id = snapshot.id
    with Session(engine) as session:
        snapshot = session.get(CustomerPriceTypeSnapshot, snapshot_id)
        snapshot_hash = snapshot.snapshot_hash
    access = CustomerPriceTypeAccessScope(actor="arsen", role="network_head", can_view_money=True)
    client = _client(factory, access)
    try:
        stale = client.put(
            f"/api/customer-price-types/reviews/cards/{snapshot_id}/price-type",
            json={
                "result": "confirm",
                "expected_version": 0,
                "snapshot_hash": "0" * 64,
            },
        )
        assert stale.status_code == 409
        with Session(engine) as session:
            assert session.scalar(select(CustomerPriceTypeReview)) is None

        invalid = client.put(
            f"/api/customer-price-types/reviews/cards/{snapshot_id}/price-type",
            json={
                "result": "correct",
                "corrected_value": "4.Золотой",
                "comment": "Нужен другой уровень",
                "expected_version": 0,
                "snapshot_hash": snapshot_hash,
            },
        )
        assert invalid.status_code == 422

        saved = client.put(
            f"/api/customer-price-types/reviews/cards/{snapshot_id}/price-type",
            json={
                "result": "confirm",
                "expected_version": 0,
                "snapshot_hash": snapshot_hash,
            },
        )
        assert saved.status_code == 200
        assert saved.json()["card"]["price_type"]["external_state"] == "held"
        with Session(engine) as session:
            action = session.scalar(select(CustomerPriceTypeExternalAction))
            line = session.scalar(select(CustomerPriceTypeOneCContractAction))
            assert action.status == "held"
            assert action.execution_allowed_at_decision is False
            assert line.contract_ref == _ref(1010)
            assert line.expected_price_type == "2.Бронзовый"
            assert line.target_price_type == "3.Серебряный"
            action_id = action.id
            action_version = action.version

        cancelled = client.post(
            f"/api/customer-price-types/cases/{saved.json()['card']['case_id']}/cancel-change",
            json={"comment": "Отменяю до запуска", "expected_version": action_version},
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["action_id"] == action_id
        assert cancelled.json()["status"] == "cancelled"
    finally:
        app.dependency_overrides = {}
        engine.dispose()


def test_correcting_to_current_type_does_not_create_onec_action(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'keep-current.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    CustomerPriceTypeRunService(factory).execute(
        [_upgrade_facts(20)], source_statuses={"contracts": "ready"}
    )
    with Session(engine) as session:
        snapshot_id = session.scalar(select(CustomerPriceTypeSnapshot.id))
    with Session(engine) as session:
        snapshot = session.get(CustomerPriceTypeSnapshot, snapshot_id)
        command = {
            "result": "correct",
            "corrected_value": "2.Бронзовый",
            "comment": "Оставить действующий тип",
            "expected_version": 0,
            "snapshot_hash": snapshot.snapshot_hash,
        }
    access = CustomerPriceTypeAccessScope(actor="arsen", role="network_head", can_view_money=True)
    client = _client(factory, access)
    try:
        response = client.put(
            f"/api/customer-price-types/reviews/cards/{snapshot_id}/price-type",
            json=command,
        )
        assert response.status_code == 200
        assert response.json()["card"]["price_type"]["final_value"] == "2.Бронзовый"
        assert response.json()["card"]["price_type"]["external_state"] == "not_created"
        with Session(engine) as session:
            assert session.scalar(select(CustomerPriceTypeExternalAction)) is None
    finally:
        app.dependency_overrides = {}
        engine.dispose()


def test_enabled_direction_is_captured_only_at_decision_time(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'live-gate.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    CustomerPriceTypeRunService(factory).execute(
        [_upgrade_facts(30)], source_statuses={"contracts": "ready"}
    )
    with Session(engine) as session:
        snapshot_id = session.scalar(select(CustomerPriceTypeSnapshot.id))
    settings = Settings(
        customer_price_type_external_actions_enabled=True,
        customer_price_type_onec_actions_enabled=True,
        customer_price_type_onec_enabled_directions=["bronze_to_silver"],
    )
    access = CustomerPriceTypeAccessScope(actor="arsen", role="network_head", can_view_money=True)
    with factory() as session:
        snapshot = session.get(CustomerPriceTypeSnapshot, snapshot_id)
        result = CustomerPriceTypeReviewService(session, settings=settings).save(
            snapshot_id=snapshot_id,
            review_kind="price_type",
            result="confirm",
            corrected_value=None,
            comment=None,
            expected_version=0,
            snapshot_hash=snapshot.snapshot_hash,
            access=access,
        )
        assert result.card["price_type"]["decision_mode"] == "live"
    with Session(engine) as session:
        action = session.scalar(select(CustomerPriceTypeExternalAction))
        assert action.status == "pending"
        assert action.execution_allowed_at_decision is True
    engine.dispose()


def test_real_rules_do_not_offer_downgrade_until_completed_action_and_new_run(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'real-downgrade.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    june = _facts(40)
    service = CustomerPriceTypeRunService(factory)
    service.execute([june], source_statuses={"contracts": "ready"}, run_key="june")
    with Session(engine) as session:
        june_snapshot = session.scalar(select(CustomerPriceTypeSnapshot))
        june_case = session.scalar(select(CustomerPriceTypeCase))
        assert june_snapshot.case_type == "isolate"
        assert june_snapshot.recommended_price_type == "Розница"
        assert june_snapshot.stop_factors
        assert june_case.stage == "ISOLATE_1M"
        june_case.manager_action_completeness = {
            "status": "completed",
            "source": "bitrix_readback",
            "action": "isolate",
            "current_price_type": "2.Бронзовый",
            "snapshot_month": "2026-06-01",
            "snapshot_hash": june_snapshot.snapshot_hash,
            "bitrix_item_id": "777",
            "bitrix_stage_id": "DT1188_77:CLOSED_KEEP",
        }
        session.commit()

    july = replace(
        june,
        snapshot_month=date(2026, 7, 1),
        monthly_sales={
            "2026-05": Decimal("1"),
            "2026-06": Decimal("1"),
            "2026-07": Decimal("1"),
        },
    )
    service.execute([july], source_statuses={"contracts": "ready"}, run_key="july")
    with Session(engine) as session:
        july_snapshot = session.scalar(
            select(CustomerPriceTypeSnapshot).where(
                CustomerPriceTypeSnapshot.snapshot_month == date(2026, 7, 1)
            )
        )
        assert july_snapshot.system_recommendation == "downgrade_proposed"
        assert july_snapshot.case_type == "downgrade_approval"
        assert july_snapshot.recommended_price_type == "Розница"
        assert july_snapshot.stop_factors == []
    engine.dispose()
