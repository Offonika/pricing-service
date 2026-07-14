import os
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.dependencies import get_db
from app.main import app
from app.models import (
    Base,
    Competitor,
    CompetitorItem,
    CompetitorItemCompatibility,
    CompetitorPrice,
    OneCSalesDailyKpi,
    PhoneModel,
    PhoneModelAlias,
    PriceRecommendation,
    PricingStrategyVersion,
    Product,
    ProductCompatibility,
    ProductPhoneModel,
    ProductStock,
    ReceivableBalanceSnapshot,
    ReceivableLedgerEvent,
)
from app.services import bi as bi_service
from tests.test_management_api import seed_management_data

UTC = timezone.utc


def setup_db():
    fd, path = tempfile.mkstemp(prefix="bi_test_", suffix=".db")
    os.close(fd)
    url = f"sqlite:///{path}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    return engine, path


def seed_sample_data(session: Session) -> None:
    product = Product(
        article="SKU-1",
        fact_sku="F5-DSP-IPH15P-OLED-BLK-AAA",
        planned_sku="F5-DSP-IPH15P-OLED-BLK-AAA",
        sku_sync_status="match",
        name="Prod 1",
        brand="BrandA",
        category="CatA",
    )
    product.stock = ProductStock(quantity=5, purchase_price=Decimal("100"))
    competitor = Competitor(name="CompA", website="https://compa.example.com")
    phone_model = PhoneModel(brand="apple", model_name="iphone 15", variant="pro")
    session.add_all([product, competitor, phone_model])
    session.flush()

    session.add(
        PhoneModelAlias(
            phone_model_id=phone_model.id,
            source="news_agent",
            raw_value="Apple iPhone 15 Pro",
            raw_brand="Apple",
            raw_model="iPhone 15",
            raw_variant="Pro",
            normalized_key="apple|iphone 15|pro",
            confidence=Decimal("1.0"),
        )
    )
    session.add(
        ProductPhoneModel(
            product_id=product.id,
            phone_model_id=phone_model.id,
            source="onec",
            raw_value="Apple iPhone 15 Pro",
            confidence=Decimal("1.0"),
        )
    )
    session.add(
        ProductCompatibility(product_id=product.id, value="Apple iPhone 15 Pro", source="onec")
    )

    session.add(
        CompetitorPrice(
            product_id=product.id,
            competitor_id=competitor.id,
            price=Decimal("120"),
            in_stock=True,
            collected_at=datetime.now(UTC),
        )
    )
    strategy = PricingStrategyVersion(
        name="base_v1", description="Base", parameters={"min_margin": 0.1}
    )
    session.add(strategy)
    session.flush()
    session.add(
        PriceRecommendation(
            product_id=product.id,
            strategy_version_id=strategy.id,
            recommended_price=Decimal("120"),
            floor_price=Decimal("110"),
            competitor_min_price=Decimal("120"),
            min_margin_pct=Decimal("0.1"),
            reasons=["market above floor"],
            created_at=datetime.now(UTC) - timedelta(minutes=1),
        )
    )
    competitor_item = CompetitorItem(
        competitor="CompA", external_id="CMP-1", name="Display iPhone 15 Pro"
    )
    session.add(competitor_item)
    session.flush()
    session.add(
        CompetitorItemCompatibility(
            competitor_item_id=competitor_item.id,
            phone_model_id=phone_model.id,
            device_brand="apple",
            device_model="iphone 15",
            device_variant="pro",
            source="parser",
        )
    )
    session.commit()


def override_db(engine):
    def _override():
        db = Session(engine)
        try:
            yield db
        finally:
            db.close()

    return _override


def test_bi_products_and_recommendations() -> None:
    engine, path = setup_db()
    with Session(engine) as session:
        seed_sample_data(session)

    app.dependency_overrides = {get_db: override_db(engine)}
    client = TestClient(app)

    resp = client.get("/api/bi/products")
    assert resp.status_code == 200
    products = resp.json()
    assert len(products) == 1
    assert products[0]["article"] == "SKU-1"
    assert products[0]["fact_sku"] == "F5-DSP-IPH15P-OLED-BLK-AAA"
    assert products[0]["planned_sku"] == "F5-DSP-IPH15P-OLED-BLK-AAA"
    assert products[0]["sku_sync_status"] == "match"
    assert products[0]["purchase_price"] == 100.0

    resp_rec = client.get("/api/bi/recommendations")
    assert resp_rec.status_code == 200
    recs = resp_rec.json()
    assert len(recs) == 1
    assert recs[0]["article"] == "SKU-1"
    assert Decimal(str(recs[0]["recommended_price"])) == Decimal("120")
    assert recs[0]["strategy_name"] == "base_v1"

    resp_prices = client.get("/api/bi/competitor-prices")
    assert resp_prices.status_code == 200
    prices = resp_prices.json()
    assert len(prices) == 1
    assert prices[0]["competitor"] == "CompA"

    resp_links = client.get("/api/bi/phone-model-links")
    assert resp_links.status_code == 200
    links = resp_links.json()
    assert any(link["product_article"] == "SKU-1" for link in links)
    assert any(link["competitor_sku"] == "CMP-1" for link in links)

    resp_summary = client.get("/api/bi/canonicalization-summary")
    assert resp_summary.status_code == 200
    summary = resp_summary.json()
    assert summary["phone_models"] == 1
    assert summary["aliases"] == 1
    assert summary["product_links"] == 1
    assert summary["competitor_links"] == 1
    assert summary["filtered_non_phone_product_compatibilities"] == 0

    resp_unresolved = client.get("/api/bi/compatibility-unresolved")
    assert resp_unresolved.status_code == 200
    assert resp_unresolved.json() == []

    app.dependency_overrides = {}
    engine.dispose()
    if os.path.exists(path):
        os.remove(path)


def test_bi_filters_non_phone_product_compatibilities_from_review() -> None:
    engine, path = setup_db()

    with Session(engine) as session:
        filtered_watch = Product(
            article="SKU-WATCH",
            name="Защитное стекло для Apple Watch 8",
            subject_1c="защитное стекло",
            subject="защитное стекло",
            subject_source="1c",
            vid_nomenklatury_1c="Аксессуары (розничные товары)",
            vid_nomenklatury="Аксессуары (розничные товары)",
            vid_nomenklatury_source="1c",
        )
        filtered_placeholder = Product(
            article="SKU-PLACEHOLDER",
            name="Кабель USB-C",
            subject_1c="кабель",
            subject="кабель",
            subject_source="1c",
            vid_nomenklatury_1c="Питание и зарядка (розница + сервис)",
            vid_nomenklatury="Питание и зарядка (розница + сервис)",
            vid_nomenklatury_source="1c",
        )
        eligible_phone = Product(
            article="SKU-PHONE",
            name="Защитное стекло для Samsung S21 Ultra",
            subject_1c="защитное стекло",
            subject="защитное стекло",
            subject_source="1c",
            vid_nomenklatury_1c="Аксессуары (розничные товары)",
            vid_nomenklatury="Аксессуары (розничные товары)",
            vid_nomenklatury_source="1c",
        )
        session.add_all([filtered_watch, filtered_placeholder, eligible_phone])
        session.flush()

        session.add_all(
            [
                ProductCompatibility(
                    product_id=filtered_watch.id,
                    value="Apple Watch S8 (41 мм)",
                    source="onec",
                ),
                ProductCompatibility(
                    product_id=filtered_placeholder.id,
                    value="<>",
                    source="onec",
                ),
                ProductCompatibility(
                    product_id=eligible_phone.id,
                    value="Samsung G998 Galaxy S21 Ultra",
                    source="onec",
                ),
            ]
        )
        session.commit()

    app.dependency_overrides = {get_db: override_db(engine)}
    client = TestClient(app)

    resp_summary = client.get("/api/bi/canonicalization-summary")
    assert resp_summary.status_code == 200
    summary = resp_summary.json()
    assert summary["unresolved_product_compatibilities"] == 1
    assert summary["filtered_non_phone_product_compatibilities"] == 2

    resp_unresolved = client.get("/api/bi/compatibility-unresolved")
    assert resp_unresolved.status_code == 200
    unresolved = resp_unresolved.json()
    assert len(unresolved) == 1
    assert unresolved[0]["entity_type"] == "product"
    assert unresolved[0]["raw_value"] == "Samsung G998 Galaxy S21 Ultra"

    app.dependency_overrides = {}
    engine.dispose()
    if os.path.exists(path):
        os.remove(path)


def test_bi_receivables_datasets(monkeypatch) -> None:
    engine, path = setup_db()
    seed_management_data(engine)
    monkeypatch.setattr(bi_service, "_buyers_counterparty_refs_from_onec", lambda: None)

    with Session(engine) as session:
        session.add(
            ReceivableLedgerEvent(
                source="onec",
                business_key="return-cp-d-1",
                event_type="return",
                external_document_ref="ret-d1",
                external_document_number="R-301",
                external_document_date=datetime(2026, 3, 20, 10, 30, tzinfo=UTC),
                counterparty_ref="cp-d",
                counterparty_name="Контрагент D",
                contract_ref="contract-d",
                contract_name="Основной договор D",
                contract_kind_ref="kind-buyer",
                contract_kind_name="С покупателем",
                manager_ref="mgr-5",
                manager_name="Менеджер 5",
                store_ref="store-4",
                store_name="Магазин 4",
                source_layer="regular_receivables",
                amount_delta=Decimal("-10"),
            )
        )
        session.add(
            ReceivableLedgerEvent(
                source="onec",
                business_key="sale-cp-rub-1",
                event_type="sale",
                external_document_ref="sale-rub-1",
                external_document_number="S-RUB-1",
                external_document_date=datetime(2026, 3, 20, 11, 0, tzinfo=UTC),
                counterparty_ref="cp-rub",
                counterparty_name="Контрагент Руб",
                contract_ref="contract-rub",
                contract_name="Основной договор, руб",
                contract_kind_ref="kind-supplier",
                contract_kind_name="С поставщиком",
                manager_ref="mgr-rub",
                manager_name="Менеджер Руб",
                store_ref="store-rub",
                store_name="Магазин Руб",
                source_layer="regular_receivables",
                amount_delta=Decimal("55"),
            )
        )
        session.commit()

    app.dependency_overrides = {get_db: override_db(engine)}
    client = TestClient(app)

    resp_current = client.get("/api/bi/receivables-current", params={"date": "2026-03-20"})
    assert resp_current.status_code == 200
    current_payload = resp_current.json()
    assert len(current_payload) == 4
    assert current_payload[0]["counterparty_ref"] == "cp-a"
    assert Decimal(str(current_payload[0]["current_balance"])) == Decimal("80")
    current_cp_d = next(item for item in current_payload if item["counterparty_ref"] == "cp-d")
    assert Decimal(str(current_cp_d["current_balance"])) == Decimal("70")

    resp_cases = client.get(
        "/api/bi/receivable-cases",
        params={"date": "2026-03-20", "segment": "employee"},
    )
    assert resp_cases.status_code == 200
    cases_payload = resp_cases.json()
    assert len(cases_payload) == 1
    assert cases_payload[0]["counterparty_ref"] == "cp-b"
    assert cases_payload[0]["segment"] == "employee"

    resp_manager = client.get(
        "/api/bi/receivables-manager-summary",
        params={"date": "2026-03-20"},
    )
    assert resp_manager.status_code == 200
    manager_payload = resp_manager.json()
    manager_mgr5 = next(item for item in manager_payload if item["manager_ref"] == "mgr-5")
    assert manager_mgr5["snapshot_date"] == "2026-03-20"
    assert manager_mgr5["new_daily_count"] == 1
    assert Decimal(str(manager_mgr5["total_balance"])) == Decimal("70")

    resp_contracts = client.get(
        "/api/bi/receivables-contract-balances",
        params={"date": "2026-03-20"},
    )
    assert resp_contracts.status_code == 200
    contracts_payload = resp_contracts.json()
    contract_cp_a = next(item for item in contracts_payload if item["counterparty_ref"] == "cp-a")
    assert contract_cp_a["contract_name"] == "Основной договор A"
    assert contract_cp_a["contract_kind_name"] == "С покупателем"
    assert contract_cp_a["source_layer"] == "regular_receivables"
    assert Decimal(str(contract_cp_a["current_balance"])) == Decimal("80")

    resp_contracts_buyers_rub = client.get(
        "/api/bi/receivables-contract-balances",
        params={"date": "2026-03-20", "buyers_rub_only": "true"},
    )
    assert resp_contracts_buyers_rub.status_code == 200
    buyers_rub_payload = resp_contracts_buyers_rub.json()
    assert len(buyers_rub_payload) == 4
    buyers_cp_a = next(item for item in buyers_rub_payload if item["counterparty_ref"] == "cp-a")
    assert buyers_cp_a["contract_name"] is None
    assert buyers_cp_a["contract_kind_name"] == "С покупателем"
    assert buyers_cp_a["source_layer"] == "buyers_rub_snapshot"
    assert Decimal(str(buyers_cp_a["current_balance"])) == Decimal("80")

    app.dependency_overrides = {}
    engine.dispose()
    if os.path.exists(path):
        os.remove(path)


def test_bi_receivables_buyers_rub_only_uses_exact_snapshot_only(monkeypatch) -> None:
    engine, path = setup_db()
    with Session(engine) as session:
        session.add_all(
            [
                ReceivableBalanceSnapshot(
                    snapshot_date=datetime(2026, 3, 31, tzinfo=UTC).date(),
                    counterparty_ref="cp-a",
                    counterparty_name="Контрагент A",
                    current_balance=Decimal("100.00"),
                    activity_segment="active",
                    aged_bucket="0-30",
                    is_overdue=False,
                    origin_document_date=datetime(2026, 3, 20, 10, 0, tzinfo=UTC),
                    last_sale_at=datetime(2026, 3, 31, 10, 0, tzinfo=UTC),
                ),
                ReceivableBalanceSnapshot(
                    snapshot_date=datetime(2026, 3, 31, tzinfo=UTC).date(),
                    counterparty_ref="cp-b",
                    counterparty_name="Контрагент B",
                    current_balance=Decimal("50.00"),
                    activity_segment="active",
                    aged_bucket="0-30",
                    is_overdue=False,
                    origin_document_date=datetime(2026, 3, 22, 10, 0, tzinfo=UTC),
                    last_sale_at=datetime(2026, 3, 31, 11, 0, tzinfo=UTC),
                ),
                ReceivableBalanceSnapshot(
                    snapshot_date=datetime(2026, 4, 4, tzinfo=UTC).date(),
                    counterparty_ref="wrong-cp",
                    counterparty_name="Неправильный общий контур",
                    current_balance=Decimal("-200000000.00"),
                    activity_segment="active",
                    aged_bucket="0-30",
                    is_overdue=False,
                    origin_document_date=datetime(2026, 4, 4, 10, 0, tzinfo=UTC),
                    last_sale_at=datetime(2026, 4, 4, 10, 0, tzinfo=UTC),
                ),
                ReceivableLedgerEvent(
                    source="onec",
                    business_key="cp-a-pay-0401",
                    event_type="payment",
                    external_document_ref="pay-a-1",
                    external_document_number="P-A-1",
                    external_document_date=datetime(2026, 4, 1, 11, 0, tzinfo=UTC),
                    counterparty_ref="cp-a",
                    counterparty_name="Контрагент A",
                    contract_ref="contract-a",
                    contract_name="Договор A",
                    contract_kind_ref="kind-buyer",
                    contract_kind_name="С покупателем",
                    manager_ref="mgr-1",
                    manager_name="Менеджер 1",
                    store_ref="store-1",
                    store_name="Магазин 1",
                    source_layer="regular_receivables",
                    amount_delta=Decimal("-20.00"),
                ),
                ReceivableLedgerEvent(
                    source="onec",
                    business_key="cp-a-sale-0403",
                    event_type="sale",
                    external_document_ref="sale-a-2",
                    external_document_number="S-A-2",
                    external_document_date=datetime(2026, 4, 3, 12, 0, tzinfo=UTC),
                    counterparty_ref="cp-a",
                    counterparty_name="Контрагент A",
                    contract_ref="contract-a",
                    contract_name="Договор A",
                    contract_kind_ref="kind-buyer",
                    contract_kind_name="С покупателем",
                    manager_ref="mgr-1",
                    manager_name="Менеджер 1",
                    store_ref="store-1",
                    store_name="Магазин 1",
                    source_layer="regular_receivables",
                    amount_delta=Decimal("10.00"),
                ),
                ReceivableLedgerEvent(
                    source="onec",
                    business_key="cp-c-sale-0404",
                    event_type="sale",
                    external_document_ref="sale-c-1",
                    external_document_number="S-C-1",
                    external_document_date=datetime(2026, 4, 4, 13, 0, tzinfo=UTC),
                    counterparty_ref="cp-c",
                    counterparty_name="Контрагент C",
                    contract_ref="contract-c",
                    contract_name="Договор C",
                    contract_kind_ref="kind-buyer",
                    contract_kind_name="С покупателем",
                    manager_ref="mgr-3",
                    manager_name="Менеджер 3",
                    store_ref="store-3",
                    store_name="Магазин 3",
                    source_layer="regular_receivables",
                    amount_delta=Decimal("30.00"),
                ),
                ReceivableLedgerEvent(
                    source="onec",
                    business_key="cp-x-sale-0402",
                    event_type="sale",
                    external_document_ref="sale-x-1",
                    external_document_number="S-X-1",
                    external_document_date=datetime(2026, 4, 2, 14, 0, tzinfo=UTC),
                    counterparty_ref="cp-x",
                    counterparty_name="Контрагент X",
                    contract_ref="contract-x",
                    contract_name="Договор X",
                    contract_kind_ref="kind-supplier",
                    contract_kind_name="С поставщиком",
                    manager_ref="mgr-x",
                    manager_name="Менеджер X",
                    store_ref="store-x",
                    store_name="Магазин X",
                    source_layer="regular_receivables",
                    amount_delta=Decimal("999.00"),
                ),
            ]
        )
        session.commit()

    monkeypatch.setattr(
        bi_service,
        "_buyers_counterparty_refs_from_onec",
        lambda: ("cp-a", "cp-b", "cp-c"),
    )

    app.dependency_overrides = {get_db: override_db(engine)}
    client = TestClient(app)

    response = client.get(
        "/api/bi/receivables-contract-balances",
        params={"date": "2026-04-04", "buyers_rub_only": "true"},
    )
    assert response.status_code == 200
    payload = response.json()

    assert payload == []

    app.dependency_overrides = {}
    engine.dispose()
    if os.path.exists(path):
        os.remove(path)


def test_bi_receivables_buyers_rub_only_filters_non_buyers_from_snapshot(monkeypatch) -> None:
    engine, path = setup_db()
    with Session(engine) as session:
        session.add_all(
            [
                ReceivableBalanceSnapshot(
                    snapshot_date=datetime(2026, 4, 19, tzinfo=UTC).date(),
                    counterparty_ref="buyer-a",
                    counterparty_name="Покупатель A",
                    current_balance=Decimal("100.00"),
                    activity_segment="active",
                    aged_bucket="unknown",
                    is_overdue=False,
                ),
                ReceivableBalanceSnapshot(
                    snapshot_date=datetime(2026, 4, 19, tzinfo=UTC).date(),
                    counterparty_ref="buyer-b",
                    counterparty_name="Покупатель B",
                    current_balance=Decimal("50.00"),
                    activity_segment="active",
                    aged_bucket="unknown",
                    is_overdue=False,
                ),
                ReceivableBalanceSnapshot(
                    snapshot_date=datetime(2026, 4, 19, tzinfo=UTC).date(),
                    counterparty_ref="buyer-c",
                    counterparty_name="Покупатель C",
                    current_balance=Decimal("-20.00"),
                    activity_segment="active",
                    aged_bucket="unknown",
                    is_overdue=False,
                ),
                ReceivableBalanceSnapshot(
                    snapshot_date=datetime(2026, 4, 19, tzinfo=UTC).date(),
                    counterparty_ref="misc-x",
                    counterparty_name="Не buyer",
                    current_balance=Decimal("999.00"),
                    activity_segment="active",
                    aged_bucket="unknown",
                    is_overdue=False,
                ),
            ]
        )
        session.commit()

    monkeypatch.setattr(
        bi_service,
        "_buyers_counterparty_refs_from_onec",
        lambda: ("buyer-a", "buyer-b", "buyer-c"),
    )

    app.dependency_overrides = {get_db: override_db(engine)}
    client = TestClient(app)

    response = client.get(
        "/api/bi/receivables-contract-balances",
        params={"date": "2026-04-19", "buyers_rub_only": "true"},
    )
    assert response.status_code == 200
    payload = response.json()

    assert [item["counterparty_ref"] for item in payload] == ["buyer-a", "buyer-b", "buyer-c"]
    assert sum(Decimal(str(item["current_balance"])) for item in payload) == Decimal("130.00")

    app.dependency_overrides = {}
    engine.dispose()
    if os.path.exists(path):
        os.remove(path)


def test_buyers_counterparty_refs_from_onec_does_not_keep_process_cache(monkeypatch) -> None:
    class FakeEngine:
        def dispose(self) -> None:
            pass

    calls = iter((("buyer-a",), ("buyer-a", "buyer-b")))

    monkeypatch.setattr(
        bi_service,
        "get_settings",
        lambda: SimpleNamespace(onec_database_url="mssql://onec"),
    )
    monkeypatch.setattr(
        bi_service,
        "build_onec_engine_from_settings",
        lambda: FakeEngine(),
    )
    monkeypatch.setattr(
        bi_service,
        "fetch_counterparty_refs_from_onec_group",
        lambda *_args, **_kwargs: next(calls),
    )

    assert bi_service._buyers_counterparty_refs_from_onec() == ("buyer-a",)
    assert bi_service._buyers_counterparty_refs_from_onec() == ("buyer-a", "buyer-b")


def test_bi_sales_kpi_datasets() -> None:
    engine, path = setup_db()
    with Session(engine) as session:
        session.add_all(
            [
                OneCSalesDailyKpi(
                    sales_date=datetime(2026, 3, 4, tzinfo=UTC).date(),
                    manager_ref="mgr-1",
                    manager_name="Менеджер 1",
                    store_ref="store-1",
                    store_name="Магазин 1",
                    revenue=Decimal("2730305.88"),
                    cost_of_sales=Decimal("2000000.00"),
                    sales_count=Decimal("2907.000"),
                ),
                OneCSalesDailyKpi(
                    sales_date=datetime(2026, 3, 5, tzinfo=UTC).date(),
                    manager_ref="mgr-1",
                    manager_name="Менеджер 1",
                    store_ref="store-1",
                    store_name="Магазин 1",
                    revenue=Decimal("100.00"),
                    cost_of_sales=Decimal("40.00"),
                    sales_count=Decimal("1.000"),
                ),
            ]
        )
        session.commit()

    app.dependency_overrides = {get_db: override_db(engine)}
    client = TestClient(app)

    resp_daily = client.get(
        "/api/bi/sales-daily-kpi",
        params={"date_from": "2026-03-04", "date_to": "2026-03-04"},
    )
    assert resp_daily.status_code == 200
    payload_daily = resp_daily.json()
    assert len(payload_daily) == 1
    assert payload_daily[0]["manager_ref"] == "mgr-1"
    assert Decimal(str(payload_daily[0]["revenue"])) == Decimal("2730305.88")
    assert Decimal(str(payload_daily[0]["sales_count"])) == Decimal("2907.000")
    assert Decimal(str(payload_daily[0]["cost_of_sales"])) == Decimal("2000000.00")
    assert Decimal(str(payload_daily[0]["gross_profit"])) == Decimal("730305.88")
    assert Decimal(str(payload_daily[0]["margin_pct"])) == Decimal("0.2675")
    assert Decimal(str(payload_daily[0]["profitability_pct"])) == Decimal("0.3652")

    resp_weekly = client.get(
        "/api/bi/sales-weekly-kpi",
        params={"date_from": "2026-03-04", "date_to": "2026-03-05"},
    )
    assert resp_weekly.status_code == 200
    payload_weekly = resp_weekly.json()
    assert len(payload_weekly) == 1
    assert Decimal(str(payload_weekly[0]["revenue"])) == Decimal("2730405.88")
    assert Decimal(str(payload_weekly[0]["sales_count"])) == Decimal("2908.000")
    assert Decimal(str(payload_weekly[0]["cost_of_sales"])) == Decimal("2000040.00")
    assert Decimal(str(payload_weekly[0]["gross_profit"])) == Decimal("730365.88")
    assert Decimal(str(payload_weekly[0]["margin_pct"])) == Decimal("0.2675")
    assert Decimal(str(payload_weekly[0]["profitability_pct"])) == Decimal("0.3652")

    app.dependency_overrides = {}
    engine.dispose()
    if os.path.exists(path):
        os.remove(path)
