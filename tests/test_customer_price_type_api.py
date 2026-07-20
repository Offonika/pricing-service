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
from app.core.config import get_settings
from app.domains.customer_price_types import (
    ContractFact,
    CustomerPriceTypeAccessScope,
    CustomerPriceTypeFacts,
)
from app.main import app
from app.models import Base
from app.models.customer_price_type import CustomerPriceTypeProfile
from app.services.customer_price_types import CustomerPriceTypeRunService


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
        assert detail.json()["events"][0]["event_type"] == "case_created"
        assert profile.status_code == 200
        assert profile.json()["latest_snapshot"]["total_3m"] == "300.00"
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
        assert client.get("/api/customer-price-types/cases").json()["total"] == 3
        assert (
            client.get("/api/customer-price-types/cases", params={"worklist": "data_check"}).json()[
                "total"
            ]
            == 1
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
        assert page["total"] == 3
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
    network = CustomerPriceTypeAccessScope(
        actor="network-expert", role="network_head", can_view_money=True
    )
    app.dependency_overrides = {
        get_db: _override_db(factory),
        require_customer_price_type_access: lambda: network,
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

        reviewed = client.put(
            f"/api/customer-price-types/quality/samples/{isolate['id']}",
            json={
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
                "correct_group": "isolate",
                "expected_version": isolate["version"],
            },
        )
        assert stale.status_code == 409
        assert (
            client.put(
                f"/api/customer-price-types/quality/samples/{no_action['id']}",
                json={
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
        assert body["reviewed_count"] == 2
        assert body["coverage"] == 0.6667
        assert body["override_rate"] == 0.5
        assert body["critical_false_downgrade_count"] == 1
        assert body["groups"]["isolate"]["false_positive"] == 1
        assert body["groups"]["no_action"]["recall"] == 0.5

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
