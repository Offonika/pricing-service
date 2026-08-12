from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from threading import Barrier

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.api.customer_price_types import require_customer_price_type_access
from app.api.dependencies import get_db
from app.core.config import get_settings
from app.domains.customer_price_types import (
    ContractFact,
    CustomerPriceTypeAccessScope,
    CustomerPriceTypeFacts,
)
from app.main import app
from app.models import Base
from app.models.customer_price_type import (
    CustomerPriceTypeProfile,
    CustomerPriceTypeReviewBatch,
    CustomerPriceTypeReviewBatchItem,
    CustomerPriceTypeSnapshot,
)
from app.services.customer_price_types import (
    CustomerPriceTypeQualityConflict,
    CustomerPriceTypeQualityService,
    CustomerPriceTypeRunService,
)


def _ref(value: int) -> str:
    return f"0x{value:032x}"


def _facts(value: int, *, owner: str, department: str) -> CustomerPriceTypeFacts:
    return CustomerPriceTypeFacts(
        counterparty_ref=_ref(value),
        counterparty_code=f"РБ{value:06d}",
        counterparty_name=f"Клиент {value}",
        snapshot_month=date(2026, 6, 1),
        contracts=(
            ContractFact(
                contract_ref=_ref(1000 + value),
                contract_name="Основной",
                price_type_name="2.Бронзовый",
            ),
        ),
        monthly_sales={
            "2025-07": Decimal("10"),
            "2025-08": Decimal("10"),
            "2025-09": Decimal("10"),
            "2025-10": Decimal("10"),
            "2025-11": Decimal("10"),
            "2025-12": Decimal("10"),
            "2026-01": Decimal("10"),
            "2026-02": Decimal("10"),
            "2026-03": Decimal("10"),
            "2026-04": Decimal("100"),
            "2026-05": Decimal("100"),
            "2026-06": Decimal("100"),
        },
        source_statuses={
            "contracts": "ready",
            "sales_history": "ready",
            "ledger_reconciliation": "ready",
            "master_data": "ready",
        },
        owner_ref=owner,
        owner_name=owner,
        department_ref=department,
        department_name=department,
        history_coverage_months=12,
        direct_onec_total_3m=Decimal("300"),
        ledger_total_3m=Decimal("300"),
        economics_status="ok",
        economics={"status": "ok", "profit": "1000.00"},
        payments={"overdue": "0.00"},
        returns={"return_amount": "10.00", "return_rate_pct": "5.00"},
    )


def _override_db(factory):
    def dependency():
        with factory() as session:
            yield session

    return dependency


def test_read_only_api_and_scopes(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'api.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    run = CustomerPriceTypeRunService(factory).execute(
        [
            _facts(1, owner="manager-1", department="department-1"),
            _facts(2, owner="manager-2", department="department-2"),
        ],
        source_statuses={"contracts": "ready"},
    )
    full = CustomerPriceTypeAccessScope(
        actor="test",
        role="internal",
        can_view_money=True,
    )
    app.dependency_overrides = {
        get_db: _override_db(factory),
        require_customer_price_type_access: lambda: full,
    }
    try:
        client = TestClient(app)
        summary = client.get("/api/customer-price-types/summary")
        worklists = client.get("/api/customer-price-types/worklists")
        cases = client.get("/api/customer-price-types/cases")

        assert summary.status_code == 200
        assert summary.json()["summary"]["profile_count"] == 2
        assert summary.json()["summary"]["actionable_count"] == 2
        assert summary.json()["summary"]["departments"] == {
            "department-1": 1,
            "department-2": 1,
        }
        assert worklists.json()["worklists"]["isolate"] == 2
        assert cases.status_code == 200
        assert cases.json()["total"] == 2
        case_id = cases.json()["payload"][0]["id"]
        counterparty_ref = cases.json()["payload"][0]["counterparty_ref"]

        detail = client.get(f"/api/customer-price-types/cases/{case_id}")
        profile = client.get(f"/api/customer-price-types/profiles/{counterparty_ref}")
        run_response = client.get(f"/api/customer-price-types/runs/{run.run_id}")

        assert detail.status_code == 200
        assert detail.json()["snapshot"]["money_visible"] is True
        assert detail.json()["guidance"] is None
        assert detail.json()["events"][0]["event_type"] == "case_created"
        assert profile.status_code == 200
        assert profile.json()["latest_snapshot"]["total_3m"] == "300.00"
        assert profile.json()["case_history"][0]["id"] == case_id
        assert run_response.status_code == 200
        assert run_response.json()["status"] == "completed"
        assert client.post("/api/customer-price-types/cases").status_code == 405

        manager = CustomerPriceTypeAccessScope(
            actor="manager-1",
            role="manager",
            owner_ref="manager-1",
            can_view_money=False,
        )
        app.dependency_overrides[require_customer_price_type_access] = lambda: manager
        manager_cases = client.get("/api/customer-price-types/cases")
        manager_summary = client.get("/api/customer-price-types/summary")
        manager_case_id = manager_cases.json()["payload"][0]["id"]
        manager_detail = client.get(f"/api/customer-price-types/cases/{manager_case_id}")

        assert manager_cases.json()["total"] == 1
        assert manager_summary.json()["summary"]["profile_count"] == 1
        assert manager_detail.json()["snapshot"]["money_visible"] is False
        assert manager_detail.json()["snapshot"]["total_3m"] is None
        assert manager_detail.json()["snapshot"]["economics"] is None
        assert "return_amount" not in manager_detail.json()["snapshot"]["returns"]
        assert manager_detail.json()["snapshot"]["returns"]["return_rate_pct"] == "5.00"
        assert client.get(f"/api/customer-price-types/runs/{run.run_id}").status_code == 403

        with Session(engine) as session:
            other_ref = session.scalar(
                select(CustomerPriceTypeProfile.counterparty_ref).where(
                    CustomerPriceTypeProfile.owner_ref == "manager-2"
                )
            )
        assert client.get(f"/api/customer-price-types/profiles/{other_ref}").status_code == 404
    finally:
        app.dependency_overrides = {}
        engine.dispose()


def test_conflicting_price_levels_detail_explains_manager_action(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'conflicting-levels.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    fact = _facts(20, owner="manager-20", department="department-20")
    fact = replace(
        fact,
        contracts=(
            ContractFact(
                contract_ref=_ref(1020),
                contract_name="Основной",
                price_type_name="2.Бронзовый",
            ),
            ContractFact(
                contract_ref=_ref(2020),
                contract_name="Дополнительный",
                price_type_name="3.Серебряный",
            ),
        ),
    )
    CustomerPriceTypeRunService(factory).execute(
        [fact],
        source_statuses={"contracts": "ready"},
    )
    full = CustomerPriceTypeAccessScope(
        actor="test",
        role="internal",
        can_view_money=True,
    )
    app.dependency_overrides = {
        get_db: _override_db(factory),
        require_customer_price_type_access: lambda: full,
    }
    try:
        client = TestClient(app)
        cases = client.get("/api/customer-price-types/cases").json()["payload"]
        assert len(cases) == 1

        detail = client.get(f"/api/customer-price-types/cases/{cases[0]['id']}")
        assert detail.status_code == 200
        payload = detail.json()
        assert payload["case"]["case_type"] == "data_check"
        assert payload["snapshot"]["reasons"] == ["conflicting_price_levels"]
        assert {item["price_type_name"] for item in payload["snapshot"]["contract_candidates"]} == {
            "2.Бронзовый",
            "3.Серебряный",
        }
        assert "наши правила запрещают выбирать" in payload["guidance"]["rules"]
        assert "согласовать единый уровень" in payload["guidance"]["recommended_action"]
        assert "не выбирается автоматически" in payload["guidance"]["expected_price_type"]
        assert len(payload["guidance"]["manager_attention"]) == 6
    finally:
        app.dependency_overrides = {}
        engine.dispose()


def test_detail_marks_usable_contract_and_price_type_change_target(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'usable-contract.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    fact = replace(
        _facts(21, owner="manager-21", department="department-21"),
        contracts=(
            ContractFact(
                contract_ref=_ref(1021),
                contract_name="Основной договор",
                price_type_name=None,
                price_type_missing=True,
            ),
            ContractFact(
                contract_ref=_ref(2021),
                contract_name="Договор с покупателем",
                price_type_name="2.Бронзовый",
                sale_document_count_12m=1,
                is_working=True,
            ),
        ),
    )
    CustomerPriceTypeRunService(factory).execute(
        [fact],
        source_statuses={"contracts": "ready"},
    )
    full = CustomerPriceTypeAccessScope(
        actor="test",
        role="internal",
        can_view_money=True,
    )
    app.dependency_overrides = {
        get_db: _override_db(factory),
        require_customer_price_type_access: lambda: full,
    }
    try:
        client = TestClient(app)
        cases = client.get("/api/customer-price-types/cases").json()["payload"]
        assert len(cases) == 1
        assert cases[0]["case_type"] == "isolate"

        detail = client.get(f"/api/customer-price-types/cases/{cases[0]['id']}").json()
        contracts = {
            item["contract_name"]: item for item in detail["snapshot"]["contract_candidates"]
        }

        assert detail["snapshot"]["current_price_type"] == "2.Бронзовый"
        assert detail["snapshot"]["recommended_price_type"] == "Розница"
        assert contracts["Основной договор"]["used_for_calculation"] is False
        assert contracts["Основной договор"]["ignored_reason"] == "price_type_missing"
        assert contracts["Договор с покупателем"]["used_for_calculation"] is True
        assert contracts["Договор с покупателем"]["price_type_change_target"] is True
    finally:
        app.dependency_overrides = {}
        engine.dispose()


def test_closed_case_is_hidden_from_cases_and_kept_in_profile_history(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'closed-case-history.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    service = CustomerPriceTypeRunService(factory)
    base = _facts(22, owner="manager-22", department="department-22")
    conflicting = replace(
        base,
        contracts=(
            ContractFact(
                contract_ref=_ref(1022),
                contract_name="Бронзовый",
                price_type_name="2.Бронзовый",
                sale_document_count_12m=2,
                is_working=True,
            ),
            ContractFact(
                contract_ref=_ref(2022),
                contract_name="Розничный",
                price_type_name="Розница",
                sale_document_count_12m=1,
                is_working=True,
            ),
        ),
    )
    service.execute([conflicting], source_statuses={"contracts": "ready"}, run_key="conflict")
    resolved = replace(
        base,
        contracts=(
            ContractFact(
                contract_ref=_ref(1022),
                contract_name="Бронзовый",
                price_type_name="2.Бронзовый",
                sale_document_count_12m=2,
                is_working=True,
            ),
        ),
        monthly_sales={
            **base.monthly_sales,
            "2026-04": Decimal("4000"),
            "2026-05": Decimal("4000"),
            "2026-06": Decimal("4000"),
        },
        direct_onec_total_3m=Decimal("12000"),
        ledger_total_3m=Decimal("12000"),
    )
    service.execute([resolved], source_statuses={"contracts": "ready"}, run_key="resolved")

    full = CustomerPriceTypeAccessScope(actor="test", role="internal", can_view_money=True)
    app.dependency_overrides = {
        get_db: _override_db(factory),
        require_customer_price_type_access: lambda: full,
    }
    try:
        client = TestClient(app)
        assert client.get("/api/customer-price-types/cases").json()["total"] == 0
        profile = client.get(f"/api/customer-price-types/profiles/{_ref(22)}").json()
        assert profile["open_case"] is None
        assert len(profile["case_history"]) == 1
        assert profile["case_history"][0]["stage"] == "CLOSED_KEEP"
    finally:
        app.dependency_overrides = {}
        engine.dispose()


def test_role_scopes_filters_and_pagination(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'scopes.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    isolate = _facts(10, owner="manager-1", department="department-1")
    data_check_base = _facts(20, owner="manager-2", department="department-2")
    data_check = replace(
        data_check_base,
        source_statuses={**data_check_base.source_statuses, "master_data": "missing"},
    )
    quality = replace(
        _facts(30, owner="manager-3", department="department-1"),
        return_review_type="quality",
    )
    run = CustomerPriceTypeRunService(factory).execute(
        [isolate, data_check, quality],
        source_statuses={"contracts": "ready"},
        run_key="role-scope-run",
    )
    full = CustomerPriceTypeAccessScope(
        actor="network",
        role="network_head",
        can_view_money=True,
    )
    app.dependency_overrides = {
        get_db: _override_db(factory),
        require_customer_price_type_access: lambda: full,
    }
    try:
        client = TestClient(app)
        assert client.get("/api/customer-price-types/cases").json()["total"] == 2
        assert (
            client.get("/api/customer-price-types/cases", params={"worklist": "data_check"}).json()[
                "total"
            ]
            == 0
        )
        assert (
            client.get(
                "/api/customer-price-types/cases",
                params={"department_ref": "department-1"},
            ).json()["total"]
            == 2
        )
        assert (
            client.get("/api/customer-price-types/cases", params={"search": "РБ000030"}).json()[
                "total"
            ]
            == 1
        )
        page = client.get(
            "/api/customer-price-types/cases", params={"limit": 1, "offset": 1}
        ).json()
        assert page["total"] == 2
        assert len(page["payload"]) == 1
        assert (
            client.get(
                "/api/customer-price-types/cases", params={"worklist": "unknown"}
            ).status_code
            == 422
        )
        assert (
            client.get("/api/customer-price-types/cases", params={"limit": 501}).status_code == 422
        )

        scopes = {
            "department": (
                CustomerPriceTypeAccessScope(
                    actor="head",
                    role="department_head",
                    department_refs=("department-1",),
                    can_view_money=True,
                ),
                2,
            ),
            "master_data": (
                CustomerPriceTypeAccessScope(actor="mdm", role="master_data"),
                1,
            ),
            "quality": (
                CustomerPriceTypeAccessScope(actor="quality", role="quality"),
                1,
            ),
            "finance": (
                CustomerPriceTypeAccessScope(actor="finance", role="finance", can_view_money=True),
                1,
            ),
        }
        for scope, expected in scopes.values():
            app.dependency_overrides[require_customer_price_type_access] = lambda scope=scope: scope
            assert client.get("/api/customer-price-types/cases").json()["total"] == expected

        manager = CustomerPriceTypeAccessScope(
            actor="manager-1",
            role="manager",
            owner_ref="manager-1",
            can_view_money=False,
        )
        app.dependency_overrides[require_customer_price_type_access] = lambda: manager
        assert (
            client.get(
                "/api/customer-price-types/cases",
                params={"department_ref": "department-2"},
            ).json()["total"]
            == 0
        )

        operator = CustomerPriceTypeAccessScope(
            actor="operator",
            role="integration_operator",
            can_view_money=False,
        )
        app.dependency_overrides[require_customer_price_type_access] = lambda: operator
        assert client.get("/api/customer-price-types/cases").json()["total"] == 0
        assert (
            client.get("/api/customer-price-types/summary").json()["summary"]["profile_count"] == 0
        )
        assert client.get(f"/api/customer-price-types/runs/{run.run_id}").status_code == 200
        assert client.get(f"/api/customer-price-types/profiles/{_ref(10)}").status_code == 404
    finally:
        app.dependency_overrides = {}
        engine.dispose()


def test_reviewed_portfolio_reproduces_50_and_32_without_overriding_engine(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'portfolio.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def priced(value: int, price_type: str) -> CustomerPriceTypeFacts:
        fact = _facts(value, owner="manager-1", department="department-1")
        return replace(
            fact,
            contracts=(
                ContractFact(
                    contract_ref=_ref(1000 + value),
                    contract_name="Основной",
                    price_type_name=price_type,
                    sale_document_count_12m=2,
                    sales_amount_12m=Decimal("2500"),
                    last_sale_at=date(2026, 5, 20),
                    is_working=True,
                ),
            ),
        )

    facts = [priced(value, "2.Бронзовый") for value in range(1, 51)]
    facts.extend(priced(value, "Розница") for value in range(51, 69))
    facts.extend(priced(value, "2.Бронзовый бн") for value in range(69, 72))
    facts.append(priced(72, "3.Серебряный"))
    facts.append(priced(73, "4.Золотой"))
    for value in range(74, 83):
        fact = priced(value, "2.Бронзовый")
        facts.append(
            replace(
                fact,
                contracts=(
                    *fact.contracts,
                    ContractFact(
                        contract_ref=_ref(2000 + value),
                        contract_name="Другой рабочий",
                        price_type_name="Розница",
                        sale_document_count_12m=1,
                        sales_amount_12m=Decimal("1000"),
                        last_sale_at=date(2026, 4, 15),
                        is_working=True,
                    ),
                ),
            )
        )
    run = CustomerPriceTypeRunService(factory).execute(
        facts,
        source_statuses={
            "contracts": "ready",
            "sales_history": "ready",
            "ledger_reconciliation": "ready",
            "master_data": "ready",
        },
    )
    assert run.status == "partial"

    expected_types = {
        **{value: "2.Бронзовый" for value in range(1, 51)},
        **{value: "Розница" for value in range(51, 69)},
        **{value: "2.Бронзовый бн" for value in range(69, 72)},
        72: "3.Серебряный",
        73: "4.Золотой",
    }
    with Session(engine) as session:
        batch = CustomerPriceTypeReviewBatch(
            batch_key="reviewed-working-contracts-2026-07",
            label="Пакет 82",
            source_sha256="a" * 64,
            source_files=["working.csv", "review.csv"],
            expected_counts={"working_bronze": 50, "review_queue": 32},
            status="ready",
        )
        session.add(batch)
        session.flush()
        session.add_all(
            [
                CustomerPriceTypeReviewBatchItem(
                    batch_id=batch.id,
                    counterparty_ref=_ref(value),
                    counterparty_code=f"РБ{value:06d}",
                    expected_bucket=("working_bronze" if value <= 50 else "review_queue"),
                    expected_price_type=expected_types.get(value),
                    source_name="working.csv" if value <= 50 else "review.csv",
                    source_row=value + 1,
                )
                for value in range(1, 83)
            ]
        )
        session.commit()

    full = CustomerPriceTypeAccessScope(actor="test", role="internal", can_view_money=True)
    app.dependency_overrides = {
        get_db: _override_db(factory),
        require_customer_price_type_access: lambda: full,
    }
    try:
        client = TestClient(app)
        working = client.get(
            "/api/customer-price-types/portfolio",
            params={"bucket": "working_bronze", "limit": 100},
        )
        review = client.get(
            "/api/customer-price-types/portfolio",
            params={"bucket": "review_queue", "limit": 100},
        )
        assert working.status_code == 200
        assert working.json()["total"] == 50
        assert working.json()["counts"] == {
            "working_bronze": 50,
            "review_queue": 32,
            "total": 82,
        }
        assert working.json()["mismatch_count"] == 0
        assert working.json()["review_status_counts"] == {
            "ready": 73,
            "business_conflict": 9,
            "technical_incomplete": 0,
            "missing_snapshot": 0,
        }
        assert len(working.json()["payload"][0]["working_contracts"]) == 1
        assert working.json()["payload"][0]["working_contracts"][0]["sale_document_count_12m"] == 2
        assert review.status_code == 200
        assert review.json()["total"] == 32
        review_rows = review.json()["payload"]
        assert sum(row["current_price_type"] == "Розница" for row in review_rows) == 18
        assert sum(row["current_price_type"] == "2.Бронзовый бн" for row in review_rows) == 3
        assert sum(row["current_price_type"] == "3.Серебряный" for row in review_rows) == 1
        assert sum(row["current_price_type"] == "4.Золотой" for row in review_rows) == 1
        assert sum(row["current_price_type"] is None for row in review_rows) == 9
        assert all(row["reconciliation_status"] == "match" for row in review_rows)

        with Session(engine) as session:
            incomplete = session.scalar(
                select(CustomerPriceTypeSnapshot).where(
                    CustomerPriceTypeSnapshot.counterparty_ref == _ref(1)
                )
            )
            assert incomplete is not None
            incomplete.source_status = "partial"
            incomplete.system_recommendation = "data_check"
            incomplete.reasons = ["partial_source"]
            incomplete.stop_factors = ["source_contracts_missing"]
            business_conflict_with_missing_source = session.scalar(
                select(CustomerPriceTypeSnapshot).where(
                    CustomerPriceTypeSnapshot.counterparty_ref == _ref(74)
                )
            )
            assert business_conflict_with_missing_source is not None
            business_conflict_with_missing_source.source_statuses = {
                **business_conflict_with_missing_source.source_statuses,
                "master_data": "missing",
            }
            session.commit()
        incomplete_response = client.get(
            "/api/customer-price-types/portfolio",
            params={"bucket": "working_bronze", "limit": 100},
        ).json()
        assert incomplete_response["counts"]["working_bronze"] == 50
        assert incomplete_response["mismatch_count"] == 2
        assert incomplete_response["review_status_counts"]["technical_incomplete"] == 2
        assert incomplete_response["review_status_counts"]["business_conflict"] == 8
        incomplete_row = next(
            row for row in incomplete_response["payload"] if row["counterparty_ref"] == _ref(1)
        )
        assert incomplete_row["reconciliation_status"] == "mismatch"
        assert incomplete_row["review_status"] == "technical_incomplete"
    finally:
        app.dependency_overrides = {}
        engine.dispose()


def test_empty_collections_and_authentication(tmp_path: Path, monkeypatch) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    app.dependency_overrides = {get_db: _override_db(factory)}
    monkeypatch.setenv("MANAGEMENT_INTERNAL_API_TOKEN", "test-token")
    get_settings.cache_clear()
    try:
        client = TestClient(app)
        assert client.get("/api/customer-price-types/summary").status_code == 401
        response = client.get(
            "/api/customer-price-types/summary",
            headers={"Authorization": "Bearer test-token"},
        )
        assert response.status_code == 200
        assert response.json()["source_status"] == "missing"
        assert response.json()["summary"]["profile_count"] == 0
        cases = client.get(
            "/api/customer-price-types/cases",
            headers={"Authorization": "Bearer test-token"},
        )
        assert cases.status_code == 200
        assert cases.json()["source_status"] == "missing"
        assert cases.json()["payload"] == []
        assert (
            client.get(
                "/api/customer-price-types/cases/999",
                headers={"Authorization": "Bearer test-token"},
            ).status_code
            == 404
        )
        assert (
            client.get(
                "/api/customer-price-types/summary",
                params={"snapshot_month": "2026-13"},
                headers={"Authorization": "Bearer test-token"},
            ).status_code
            == 422
        )
    finally:
        app.dependency_overrides = {}
        get_settings.cache_clear()
        engine.dispose()


def test_storage_failures_and_openapi_read_only_contract(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'unavailable.db'}")
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    full = CustomerPriceTypeAccessScope(actor="internal", role="internal", can_view_money=True)
    app.dependency_overrides = {
        get_db: _override_db(factory),
        require_customer_price_type_access: lambda: full,
    }
    try:
        client = TestClient(app)
        assert client.get("/api/customer-price-types/summary").status_code == 503
        assert client.get("/api/customer-price-types/portfolio").status_code == 503
        assert client.get("/api/customer-price-types/cases").status_code == 503
        assert client.get("/api/customer-price-types/runs/1").status_code == 503

        schema = app.openapi()
        legacy = schema["paths"]["/api/management/retail-customer-price-type-recommendations"][
            "get"
        ]
        assert legacy["deprecated"] is True
        for path in (
            "/api/customer-price-types/summary",
            "/api/customer-price-types/worklists",
            "/api/customer-price-types/portfolio",
            "/api/customer-price-types/cases",
            "/api/customer-price-types/cases/{case_id}",
            "/api/customer-price-types/profiles/{counterparty_ref}",
            "/api/customer-price-types/runs/{run_id}",
        ):
            assert set(schema["paths"][path]) == {"get"}
    finally:
        app.dependency_overrides = {}
        engine.dispose()


def test_latest_run_envelope_and_collections_use_the_same_snapshots(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'run-consistency.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    service = CustomerPriceTypeRunService(factory)
    service.execute(
        [
            _facts(101, owner="manager-1", department="department-1"),
            _facts(102, owner="manager-2", department="department-2"),
        ],
        source_statuses={"contracts": "ready"},
        run_key="full-portfolio",
    )
    latest = service.execute(
        [_facts(101, owner="manager-1", department="department-1")],
        source_statuses={"contracts": "ready"},
        run_key="partial-portfolio-replay",
    )
    full = CustomerPriceTypeAccessScope(
        actor="network",
        role="network_head",
        can_view_money=True,
    )
    app.dependency_overrides = {
        get_db: _override_db(factory),
        require_customer_price_type_access: lambda: full,
    }
    try:
        client = TestClient(app)
        summary = client.get("/api/customer-price-types/summary").json()
        worklists = client.get("/api/customer-price-types/worklists").json()
        cases = client.get("/api/customer-price-types/cases").json()

        assert summary["run_id"] == latest.run_id
        assert worklists["run_id"] == latest.run_id
        assert cases["run_id"] == latest.run_id
        assert summary["summary"]["profile_count"] == 1
        assert sum(worklists["worklists"].values()) == 1
        assert cases["total"] == 1
        assert cases["payload"][0]["counterparty_ref"] == _ref(101)
    finally:
        app.dependency_overrides = {}
        engine.dispose()


def test_specialized_profile_scope_filters_snapshot_history(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'profile-history-scope.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    service = CustomerPriceTypeRunService(factory)
    june = replace(
        _facts(201, owner="manager-1", department="department-1"),
        return_review_type="quality",
    )
    july = replace(
        _facts(201, owner="manager-1", department="department-1"),
        snapshot_month=date(2026, 7, 1),
        monthly_sales={
            **june.monthly_sales,
            "2026-05": Decimal("100"),
            "2026-06": Decimal("100"),
            "2026-07": Decimal("100"),
        },
    )
    service.execute([june], source_statuses={"contracts": "ready"}, run_key="quality-june")
    service.execute([july], source_statuses={"contracts": "ready"}, run_key="finance-july")
    app.dependency_overrides = {get_db: _override_db(factory)}
    try:
        client = TestClient(app)
        quality = CustomerPriceTypeAccessScope(actor="quality", role="quality")
        app.dependency_overrides[require_customer_price_type_access] = lambda: quality
        quality_response = client.get(f"/api/customer-price-types/profiles/{_ref(201)}")
        assert quality_response.status_code == 200
        assert [item["review_type"] for item in quality_response.json()["history"]] == ["quality"]

        finance = CustomerPriceTypeAccessScope(
            actor="finance",
            role="finance",
            can_view_money=True,
        )
        app.dependency_overrides[require_customer_price_type_access] = lambda: finance
        finance_response = client.get(f"/api/customer-price-types/profiles/{_ref(201)}")
        assert finance_response.status_code == 200
        assert [item["review_type"] for item in finance_response.json()["history"]] == ["economics"]
    finally:
        app.dependency_overrides = {}
        engine.dispose()


def test_quality_sample_review_metrics_idempotency_and_permissions(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'quality-api.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    keep = replace(
        _facts(302, owner="manager-2", department="department-1"),
        monthly_sales={
            **_facts(302, owner="manager-2", department="department-1").monthly_sales,
            "2026-04": Decimal("1000000"),
            "2026-05": Decimal("1000000"),
            "2026-06": Decimal("1000000"),
        },
        direct_onec_total_3m=Decimal("3000000"),
        ledger_total_3m=Decimal("3000000"),
    )
    quality_case = replace(
        _facts(303, owner="manager-3", department="department-1"),
        return_review_type="quality",
    )
    CustomerPriceTypeRunService(factory).execute(
        [
            _facts(301, owner="manager-1", department="department-1"),
            keep,
            quality_case,
        ],
        source_statuses={"contracts": "ready"},
        run_key="quality-review-run",
    )
    internal = CustomerPriceTypeAccessScope(
        actor="internal-expert", role="internal", can_view_money=True
    )
    app.dependency_overrides = {
        get_db: _override_db(factory),
        require_customer_price_type_access: lambda: internal,
    }
    try:
        client = TestClient(app)
        prepared = client.post(
            "/api/customer-price-types/quality/samples/prepare",
            json={"per_group": 2},
        )
        assert prepared.status_code == 200, prepared.text
        assert prepared.json()["created"] == 3
        assert prepared.json()["total"] == 3
        repeated = client.post(
            "/api/customer-price-types/quality/samples/prepare",
            json={"per_group": 2},
        )
        assert repeated.json()["created"] == 0
        assert repeated.json()["total"] == 3

        samples = client.get("/api/customer-price-types/quality/samples").json()
        assert samples["total"] == 3
        isolate = next(item for item in samples["payload"] if item["system_group"] == "isolate")
        no_action = next(item for item in samples["payload"] if item["system_group"] == "no_action")
        detail = client.get(f"/api/customer-price-types/quality/samples/{isolate['id']}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["snapshot"]["money_visible"] is True
        assert detail.json()["snapshot"]["total_3m"] == "300.00"
        assert detail.json()["profile"]["owner_name"] == "manager-1"

        reviewed = client.put(
            f"/api/customer-price-types/quality/samples/{isolate['id']}",
            json={
                "review_result": "incorrect",
                "correct_group": "no_action",
                "comment": "Понижение не требуется",
                "expected_version": isolate["version"],
            },
        )
        assert reviewed.status_code == 200, reviewed.text
        assert reviewed.json()["status"] == "reviewed"
        stale = client.put(
            f"/api/customer-price-types/quality/samples/{isolate['id']}",
            json={
                "review_result": "correct",
                "correct_group": "isolate",
                "expected_version": isolate["version"],
            },
        )
        assert stale.status_code == 409
        assert (
            client.put(
                f"/api/customer-price-types/quality/samples/{no_action['id']}",
                json={
                    "review_result": "correct",
                    "correct_group": "no_action",
                    "expected_version": no_action["version"],
                },
            ).status_code
            == 200
        )

        metrics = client.get("/api/customer-price-types/quality/metrics")
        assert metrics.status_code == 200
        body = metrics.json()
        assert body["selected_count"] == 3
        assert body["population_count"] == 3
        assert body["metrics_scope"] == "portfolio"
        assert body["metrics_ready"] is False
        assert body["reviewed_count"] == 2
        assert body["coverage"] == 0.6667
        assert body["override_rate"] == 0.5
        assert body["critical_false_downgrade_count"] == 1
        assert body["groups"]["isolate"]["false_positive"] == 1
        assert body["groups"]["no_action"]["recall"] == 0.5

        barrier = Barrier(2)

        def concurrent_review(actor: str) -> int:
            access = CustomerPriceTypeAccessScope(actor=actor, role="internal", can_view_money=True)
            with factory() as session:
                barrier.wait()
                try:
                    CustomerPriceTypeQualityService(session).review(
                        sample_id=isolate["id"],
                        review_result="incorrect",
                        correct_group="no_action",
                        comment=actor,
                        expected_version=reviewed.json()["version"],
                        access=access,
                    )
                except CustomerPriceTypeQualityConflict:
                    return 409
                return 200

        with ThreadPoolExecutor(max_workers=2) as executor:
            concurrent_results = list(executor.map(concurrent_review, ("expert-a", "expert-b")))
        assert sorted(concurrent_results) == [200, 409]
        refreshed = client.get(f"/api/customer-price-types/quality/samples/{isolate['id']}").json()[
            "sample"
        ]
        assert refreshed["version"] == reviewed.json()["version"] + 1

        quality = CustomerPriceTypeAccessScope(actor="quality-expert", role="quality")
        app.dependency_overrides[require_customer_price_type_access] = lambda: quality
        quality_samples = client.get("/api/customer-price-types/quality/samples").json()
        assert quality_samples["total"] == 1
        special_review = quality_samples["payload"][0]
        quality_detail = client.get(
            f"/api/customer-price-types/quality/samples/{special_review['id']}"
        )
        assert quality_detail.status_code == 200, quality_detail.text
        assert quality_detail.json()["snapshot"]["money_visible"] is False
        assert quality_detail.json()["snapshot"]["total_3m"] is None
        assert "return_amount" not in quality_detail.json()["snapshot"]["returns"]
        quality_metrics = client.get("/api/customer-price-types/quality/metrics").json()
        assert quality_metrics["metrics_scope"] == "special_review_only"
        assert quality_metrics["groups"]["special_review"]["recall"] is None

        manager = CustomerPriceTypeAccessScope(
            actor="manager-1", role="manager", owner_ref="manager-1"
        )
        app.dependency_overrides[require_customer_price_type_access] = lambda: manager
        assert client.get("/api/customer-price-types/quality/metrics").status_code == 403
        assert client.get("/api/customer-price-types/quality/samples").status_code == 403
        assert (
            client.post(
                "/api/customer-price-types/quality/samples/prepare", json={"per_group": 2}
            ).status_code
            == 403
        )
    finally:
        app.dependency_overrides = {}
        engine.dispose()


def test_data_check_is_excluded_from_quality_sample_and_metrics(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'quality-null-target.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    missing_contract = replace(
        _facts(401, owner="manager-1", department="department-1"), contracts=()
    )
    CustomerPriceTypeRunService(factory).execute(
        [missing_contract],
        source_statuses={"contracts": "ready"},
        run_key="quality-null-target-run",
    )
    internal = CustomerPriceTypeAccessScope(
        actor="internal-expert", role="internal", can_view_money=True
    )
    app.dependency_overrides = {
        get_db: _override_db(factory),
        require_customer_price_type_access: lambda: internal,
    }
    try:
        client = TestClient(app)
        assert (
            client.post(
                "/api/customer-price-types/quality/samples/prepare", json={"per_group": 1}
            ).status_code
            == 200
        )
        samples = client.get("/api/customer-price-types/quality/samples").json()
        assert samples["total"] == 0
        metrics = client.get("/api/customer-price-types/quality/metrics")
        assert metrics.status_code == 200, metrics.text
        assert metrics.json()["population_count"] == 0
        assert metrics.json()["selected_count"] == 0
        assert metrics.json()["critical_false_downgrade_count"] == 0
    finally:
        app.dependency_overrides = {}
        engine.dispose()


def test_profile_search_and_internal_data_issue_queue(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'data-issue-workspace.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    clean = _facts(601, owner="manager-1", department="department-1")
    conflict = replace(
        _facts(602, owner="manager-2", department="department-1"),
        ledger_total_3m=Decimal("1300"),
    )
    CustomerPriceTypeRunService(factory).execute(
        [clean, conflict],
        source_statuses={"contracts": "ready"},
        run_key="data-issue-workspace-run",
    )
    internal = CustomerPriceTypeAccessScope(
        actor="internal-expert", role="internal", can_view_money=True
    )
    app.dependency_overrides = {
        get_db: _override_db(factory),
        require_customer_price_type_access: lambda: internal,
    }
    try:
        client = TestClient(app)
        prepared = client.post(
            "/api/customer-price-types/quality/samples/prepare", json={"per_group": 1}
        )
        assert prepared.status_code == 200, prepared.text
        assert prepared.json()["created"] == 1

        network = CustomerPriceTypeAccessScope(
            actor="network-expert", role="network_head", can_view_money=True
        )
        app.dependency_overrides[require_customer_price_type_access] = lambda: network
        search = client.get("/api/customer-price-types/profiles", params={"search": "Клиент"})
        assert search.status_code == 200, search.text
        by_code = {item["counterparty_code"]: item for item in search.json()["payload"]}
        assert by_code["РБ000601"]["result_state"] == "change_proposed"
        assert by_code["РБ000601"]["can_review"] is True
        assert by_code["РБ000602"]["result_state"] == "data_issue"
        assert by_code["РБ000602"]["result_label"] == "Данные проверяет техническая команда"
        assert by_code["РБ000602"]["recommended_price_type"] is None
        assert by_code["РБ000602"]["can_review"] is False
        assert client.get(f"/api/customer-price-types/profiles/{_ref(602)}").status_code == 404
        with Session(engine) as session:
            conflict_case = session.scalar(
                select(CustomerPriceTypeProfile.open_case_id).where(
                    CustomerPriceTypeProfile.counterparty_ref == _ref(602)
                )
            )
        assert conflict_case is not None
        assert client.get(f"/api/customer-price-types/cases/{conflict_case}").status_code == 404
        assert client.get("/api/customer-price-types/data-issues").status_code == 403
        assert (
            client.get("/api/customer-price-types/cases", params={"worklist": "data_check"}).json()[
                "total"
            ]
            == 0
        )

        app.dependency_overrides[require_customer_price_type_access] = lambda: internal
        sample = client.get("/api/customer-price-types/quality/samples").json()["payload"][0]
        missing_comment = client.put(
            f"/api/customer-price-types/quality/samples/{sample['id']}",
            json={
                "review_result": "data_issue",
                "expected_version": sample["version"],
            },
        )
        assert missing_comment.status_code == 422
        missing_incorrect_result = client.put(
            f"/api/customer-price-types/quality/samples/{sample['id']}",
            json={
                "review_result": "incorrect",
                "comment": "Нужен другой результат",
                "expected_version": sample["version"],
            },
        )
        assert missing_incorrect_result.status_code == 422
        reviewed = client.put(
            f"/api/customer-price-types/quality/samples/{sample['id']}",
            json={
                "review_result": "data_issue",
                "comment": "Сумма за июнь выглядит неверно",
                "expected_version": sample["version"],
            },
        )
        assert reviewed.status_code == 200, reviewed.text
        assert reviewed.json()["review_result"] == "data_issue"
        assert reviewed.json()["correct_group"] == "data_check"

        issues = client.get("/api/customer-price-types/data-issues")
        assert issues.status_code == 200, issues.text
        assert issues.json()["total"] == 2
        by_source = {item["issue_source"]: item for item in issues.json()["payload"]}
        assert by_source["calculation"]["issue_text"] == "Данные источников расходятся."
        assert by_source["calculation"]["current_price_type"] == "2.Бронзовый"
        assert by_source["expert"]["comment"] == "Сумма за июнь выглядит неверно"
        metrics = client.get("/api/customer-price-types/quality/metrics").json()
        assert metrics["selected_count"] == 0
        assert metrics["reviewed_count"] == 0
    finally:
        app.dependency_overrides = {}
        engine.dispose()


def test_quality_recall_is_weighted_by_population_group_size(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'quality-weighted.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def keep(value: int) -> CustomerPriceTypeFacts:
        facts = _facts(value, owner=f"manager-{value}", department="department-1")
        return replace(
            facts,
            monthly_sales={
                **facts.monthly_sales,
                "2026-04": Decimal("1000000"),
                "2026-05": Decimal("1000000"),
                "2026-06": Decimal("1000000"),
            },
            direct_onec_total_3m=Decimal("3000000"),
            ledger_total_3m=Decimal("3000000"),
        )

    CustomerPriceTypeRunService(factory).execute(
        [
            _facts(501, owner="manager-1", department="department-1"),
            _facts(502, owner="manager-2", department="department-1"),
            keep(503),
            keep(504),
            keep(505),
            keep(506),
        ],
        source_statuses={"contracts": "ready"},
        run_key="quality-weighted-run",
    )
    internal = CustomerPriceTypeAccessScope(
        actor="internal-expert", role="internal", can_view_money=True
    )
    app.dependency_overrides = {
        get_db: _override_db(factory),
        require_customer_price_type_access: lambda: internal,
    }
    try:
        client = TestClient(app)
        prepared = client.post(
            "/api/customer-price-types/quality/samples/prepare", json={"per_group": 2}
        )
        assert prepared.status_code == 200, prepared.text
        samples = client.get("/api/customer-price-types/quality/samples").json()["payload"]
        isolate_samples = [item for item in samples if item["system_group"] == "isolate"]
        no_action_samples = [item for item in samples if item["system_group"] == "no_action"]
        assert len(isolate_samples) == 2
        assert len(no_action_samples) == 2
        reviews = [
            (isolate_samples[0], "no_action"),
            (isolate_samples[1], "isolate"),
            *((item, "no_action") for item in no_action_samples),
        ]
        for sample, correct_group in reviews:
            response = client.put(
                f"/api/customer-price-types/quality/samples/{sample['id']}",
                json={
                    "review_result": (
                        "correct" if correct_group == sample["system_group"] else "incorrect"
                    ),
                    "correct_group": correct_group,
                    **(
                        {"comment": "Исправление экспертной оценки"}
                        if correct_group != sample["system_group"]
                        else {}
                    ),
                    "expected_version": sample["version"],
                },
            )
            assert response.status_code == 200, response.text
        metrics = client.get("/api/customer-price-types/quality/metrics").json()
        assert metrics["metrics_ready"] is True
        assert metrics["groups"]["no_action"]["false_negative"] == 1
        assert metrics["groups"]["no_action"]["recall"] == 0.8
    finally:
        app.dependency_overrides = {}
        engine.dispose()
