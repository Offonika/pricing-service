from __future__ import annotations

import os
import tempfile
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.dependencies import get_db
from app.core.config import get_settings
from app.main import app
from app.models import Base, ReceivableLedgerEvent
from app.services.retail_customer_price_types import (
    ACTION_DATA_CHECK,
    ACTION_KEEP,
    ACTION_MANAGER_RETENTION,
    BUYERS_CONTRACT_KIND_NAME,
    REGULAR_RECEIVABLES_LAYER,
    build_retail_customer_price_type_recommendations,
)


def _setup_engine():
    fd, path = tempfile.mkstemp(prefix="retail_price_types_", suffix=".db")
    os.close(fd)
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    return engine, path


def _override_db(engine):
    def _override():
        db = Session(engine)
        try:
            yield db
        finally:
            db.close()

    return _override


def _event(
    *,
    idx: int,
    counterparty_ref: str,
    counterparty_name: str,
    contract_name: str,
    amount: str,
    event_type: str = "sale",
    at: datetime = datetime(2026, 3, 10, 12, 0, 0),
) -> ReceivableLedgerEvent:
    return ReceivableLedgerEvent(
        source="test",
        business_key=f"event-{idx}",
        event_type=event_type,
        external_document_ref=f"doc-{idx}",
        external_document_number=f"{idx}",
        external_document_date=at,
        counterparty_ref=counterparty_ref,
        counterparty_name=counterparty_name,
        contract_ref=f"contract-{counterparty_ref}",
        contract_name=contract_name,
        contract_kind_ref="kind-buyer",
        contract_kind_name=BUYERS_CONTRACT_KIND_NAME,
        manager_ref="mgr",
        manager_name="Менеджер",
        store_ref="store",
        store_name="Точка",
        source_layer=REGULAR_RECEIVABLES_LAYER,
        amount_delta=Decimal(amount),
    )


def _seed(session: Session) -> None:
    rows = [
        _event(
            idx=1,
            counterparty_ref="cp-bronze-to-silver",
            counterparty_name="Бронзовый клиент",
            contract_name="2.Бронзовый",
            amount="500000.00",
        ),
        _event(
            idx=2,
            counterparty_ref="cp-silver-to-gold",
            counterparty_name="Серебряный клиент",
            contract_name="3.Серебряный",
            amount="1300000.00",
        ),
        _event(
            idx=3,
            counterparty_ref="cp-gold-to-silver",
            counterparty_name="Золотой клиент с просадкой",
            contract_name="4.Золотой",
            amount="550000.00",
        ),
        _event(
            idx=4,
            counterparty_ref="cp-gold-to-bronze",
            counterparty_name="Золотой клиент ниже порога",
            contract_name="4.Золотой",
            amount="100000.00",
        ),
        _event(
            idx=5,
            counterparty_ref="cp-silver-keep",
            counterparty_name="Серебро без изменений",
            contract_name="3.Серебряный",
            amount="450000.00",
        ),
        _event(
            idx=6,
            counterparty_ref="cp-prior-gold-zero",
            counterparty_name="Золото без продаж",
            contract_name="4.Золотой",
            amount="10.00",
            at=datetime(2026, 2, 15, 12, 0, 0),
        ),
        _event(
            idx=7,
            counterparty_ref="cp-bronze-return",
            counterparty_name="Бронза с возвратом",
            contract_name="2.Бронзовый",
            amount="350000.00",
        ),
        _event(
            idx=8,
            counterparty_ref="cp-bronze-return",
            counterparty_name="Бронза с возвратом",
            contract_name="2.Бронзовый",
            amount="-100000.00",
            event_type="return",
            at=datetime(2026, 3, 20, 12, 0, 0),
        ),
        _event(
            idx=9,
            counterparty_ref="cp-bronze-to-silver",
            counterparty_name="Бронзовый клиент",
            contract_name="2.Бронзовый",
            amount="200000.00",
            at=datetime(2026, 2, 10, 12, 0, 0),
        ),
        _event(
            idx=10,
            counterparty_ref="cp-silver-to-gold",
            counterparty_name="Серебряный клиент",
            contract_name="3.Серебряный",
            amount="1000000.00",
            at=datetime(2026, 2, 10, 12, 0, 0),
        ),
    ]
    history_contracts = {
        "cp-bronze-to-silver": "2.Бронзовый",
        "cp-silver-to-gold": "3.Серебряный",
        "cp-gold-to-silver": "4.Золотой",
        "cp-gold-to-bronze": "4.Золотой",
        "cp-silver-keep": "3.Серебряный",
        "cp-prior-gold-zero": "4.Золотой",
        "cp-bronze-return": "2.Бронзовый",
    }
    rows.extend(
        _event(
            idx=100 + index,
            counterparty_ref=ref,
            counterparty_name=f"История {ref}",
            contract_name=contract_name,
            amount="1.00",
            at=datetime(2025, 1, 10, 12, 0, 0),
        )
        for index, (ref, contract_name) in enumerate(history_contracts.items())
    )
    session.add_all(rows)
    session.commit()


def test_build_retail_customer_price_type_recommendations() -> None:
    engine, path = _setup_engine()
    try:
        with Session(engine) as session:
            _seed(session)
            report = build_retail_customer_price_type_recommendations(
                session,
                month="2026-03",
                actionable_only=True,
            )

        by_ref = {item["counterparty_ref"]: item for item in report["payload"]}
        assert "cp-bronze-to-silver" not in by_ref
        assert "cp-silver-to-gold" not in by_ref
        assert by_ref["cp-gold-to-silver"]["action"] == ACTION_MANAGER_RETENTION
        assert by_ref["cp-gold-to-silver"]["purchase_amount"] == Decimal("550000.00")
        assert by_ref["cp-gold-to-bronze"]["action"] == ACTION_DATA_CHECK
        assert by_ref["cp-prior-gold-zero"]["action"] == ACTION_DATA_CHECK
        assert "cp-silver-keep" not in by_ref
        assert "cp-bronze-return" not in by_ref
        assert report["summary"]["set_silver_count"] == 0
        assert report["summary"]["set_gold_count"] == 0
        assert report["summary"]["manager_work_count"] == 1
        assert report["summary"]["data_check_count"] == 2
        assert report["summary"]["keep_count"] == 2
        assert report["summary"]["review_current_type_count"] == 2
        assert report["previous_month"] == "2026-02"

        code_mapping = {
            "cp-bronze-to-silver": "РБ000001",
            "cp-silver-to-gold": "РБ000002",
            "cp-gold-to-silver": "РБ000003",
            "cp-gold-to-bronze": "РБ000004",
        }
        with Session(engine) as session:
            grouped_report = build_retail_customer_price_type_recommendations(
                session,
                month="2026-03",
                actionable_only=True,
                allowed_counterparty_refs=set(code_mapping),
                counterparty_codes_by_ref=code_mapping,
            )
        grouped_by_ref = {item["counterparty_ref"]: item for item in grouped_report["payload"]}
        assert grouped_by_ref["cp-gold-to-silver"]["counterparty_code"] == "РБ000003"
        assert "cp-prior-gold-zero" not in grouped_by_ref
        assert grouped_report["summary"]["buyer_group_counterparty_count"] == 4

        with Session(engine) as session:
            full_report = build_retail_customer_price_type_recommendations(
                session,
                month="2026-03",
                actionable_only=False,
            )
        full_by_ref = {item["counterparty_ref"]: item for item in full_report["payload"]}
        assert full_by_ref["cp-silver-keep"]["action"] == ACTION_KEEP
        assert full_by_ref["cp-bronze-return"]["purchase_amount"] == Decimal("250000.00")
    finally:
        engine.dispose()
        Path(path).unlink(missing_ok=True)


def test_build_retail_customer_price_type_recommendations_uses_contract_price_type() -> None:
    engine, path = _setup_engine()
    try:
        with Session(engine) as session:
            session.add(
                _event(
                    idx=20,
                    counterparty_ref="cp-contract-requisite",
                    counterparty_name="Клиент с типом цен в договоре",
                    contract_name="Основной договор",
                    amount="100000.00",
                )
            )
            session.add(
                _event(
                    idx=21,
                    counterparty_ref="cp-contract-requisite",
                    counterparty_name="Клиент с типом цен в договоре",
                    contract_name="Основной договор",
                    amount="1.00",
                    at=datetime(2025, 1, 10, 12, 0, 0),
                )
            )
            session.commit()

            report = build_retail_customer_price_type_recommendations(
                session,
                month="2026-03",
                actionable_only=True,
                contract_price_type_loader=lambda _refs: {
                    "contract-cp-contract-requisite": "4.Золотой"
                },
            )

        by_ref = {item["counterparty_ref"]: item for item in report["payload"]}
        item = by_ref["cp-contract-requisite"]
        assert item["current_price_type"] == "4.Золотой"
        assert item["action"] == ACTION_DATA_CHECK
    finally:
        engine.dispose()
        Path(path).unlink(missing_ok=True)


def test_management_endpoint_returns_price_type_recommendations(monkeypatch) -> None:
    engine, path = _setup_engine()
    try:
        with Session(engine) as session:
            _seed(session)
        monkeypatch.setenv("MANAGEMENT_INTERNAL_API_TOKEN", "secret-token")
        get_settings.cache_clear()
        app.dependency_overrides = {get_db: _override_db(engine)}
        client = TestClient(app)

        response = client.get(
            "/api/management/retail-customer-price-type-recommendations",
            params={"month": "2026-03", "buyers_group_only": "false"},
            headers={"Authorization": "Bearer secret-token"},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["source_status"] == "ready"
        assert payload["summary"]["actionable_count"] == 3
        assert payload["summary"]["set_silver_count"] == 0
        assert payload["summary"]["set_gold_count"] == 0
        assert {item["action"] for item in payload["payload"]} == {
            ACTION_DATA_CHECK,
            ACTION_MANAGER_RETENTION,
        }
        assert all(
            item["recommended_price_type"] is None
            for item in payload["payload"]
            if item["action"] == ACTION_DATA_CHECK
        )
    finally:
        app.dependency_overrides = {}
        get_settings.cache_clear()
        engine.dispose()
        Path(path).unlink(missing_ok=True)
