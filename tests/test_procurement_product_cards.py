from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import select

import app.services.bitrix_order_formation as bitrix_order_service
import app.services.procurement_product_cards as product_card_service
from app.core.config import Settings
from app.models.procurement_order_formation import (
    ProcurementOrderFormation,
    ProcurementOrderFormationEvent,
    ProcurementOrderFormationLine,
    ProcurementProductCardSyncState,
)
from app.services.assortment_lifecycle_classification_store import (
    ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE,
    ASSORTMENT_LIFECYCLE_METADATA,
)
from app.services.bitrix_order_formation import resolve_catalog_product_by_id
from app.services.procurement_order_formation_workspace import (
    serialize_event,
    serialize_order_list_item,
)
from app.services.procurement_product_cards import (
    PRODUCT_CARD_FIELD_SPECS,
    bitrix_product_path,
    build_product_card_snapshot,
    product_card_native_fields,
    sync_product_cards,
)
from scripts.ensure_procurement_product_card_fields import (
    build_mapping,
    ensure_placement,
    ensure_product_fields,
    field_code,
)

PRODUCT_GUID = "2685293e-967c-11e1-bdb9-0025901e48ef"
CLASSIFICATION_ONLY_GUID = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"


@pytest.fixture()
def product_card_classification_db(sqlite_engine, db_session):
    ASSORTMENT_LIFECYCLE_METADATA.create_all(sqlite_engine)
    try:
        yield db_session
    finally:
        db_session.rollback()
        ASSORTMENT_LIFECYCLE_METADATA.drop_all(sqlite_engine)


def _settings(*, apply: bool = False) -> Settings:
    return Settings(
        procurement_product_card_apply_enabled=apply,
        procurement_product_card_catalog_id=17,
        procurement_product_card_stale_hours=100_000,
    )


def _seed_order(
    db_session,
    *,
    suffix: str = "1",
    product_id: str = "1646",
    product_guid: str = PRODUCT_GUID,
    product_code: str = "РБ000006737",
    product_name: str = "Дисплей тест",
) -> ProcurementOrderFormation:
    order = ProcurementOrderFormation(
        stable_key=f"product-card:order:{suffix}",
        status="draft",
        version=1,
        bitrix_item_url="https://crm.example.test/crm/type/1200/details/7001/",
        supplier_name="Поставщик тест",
        contract_name="Основной договор",
        warehouse_name="Центральный склад",
        currency="RUB",
        procurement_contour="ordinary",
        route="ordinary",
        batch_id="2026-09-02",
        order_date=date(2026, 9, 2),
        calculation_id=f"display-auto-order-2026-09-02:{suffix}",
    )
    order.lines = [
        ProcurementOrderFormationLine(
            stable_key=f"product-card:line:{suffix}",
            line_number=1,
            bitrix_product_id=product_id,
            bitrix_product_xml_id=product_guid,
            nomenclature_ref=product_guid,
            nomenclature_code=product_code,
            nomenclature_name=product_name,
            recommended_quantity=Decimal("7"),
            final_quantity=Decimal("5"),
            purchase_price=Decimal("115"),
            amount=Decimal("575"),
            currency="RUB",
            lifecycle_status="Растим",
            assortment_status="Продажа",
            blockers=["batch_error_suspected"],
            recommendation_reason="Проверить причины возвратов",
            payload={
                "metrics_as_of": "2026-09-02",
                "sales_qty_window": "180",
                "sales_qty_window_medium": "90",
                "sales_qty_window_short": "30",
                "sellable_stock_qty": "4",
                "active_customer_order_qty": "2",
                "incoming_qty": "3",
                "target_stock_qty": "9",
                "return_qty_window": "8",
                "batch_error_return_qty": "5",
                "batch_error_share_pct": "40",
                "defect_return_qty": "2",
                "defect_share_pct": "3.5",
                "profitability_pct": "21.4",
                "lead_time_days": 26,
                "product_card_url": "https://shop.example.test/catalog/1/",
                "photos": [{"thumbnail": "https://shop.example.test/photo.webp"}],
                "display_family_recommendation": {
                    "family_id": "family-a16",
                    "family_label": "Samsung A16",
                    "registry_member_count": 4,
                },
            },
        )
    ]
    db_session.add(order)
    db_session.commit()
    return order


def _mapping() -> dict:
    return {
        "catalog_id": 17,
        "fields": {
            spec["key"]: f"PROPERTY_{index}"
            for index, spec in enumerate(PRODUCT_CARD_FIELD_SPECS, start=900)
        },
    }


def test_native_product_path_is_stable_and_rejects_invalid_id() -> None:
    assert bitrix_product_path("1646", catalog_id=17) == "/crm/catalog/17/product/1646/"
    assert bitrix_product_path("../1646", catalog_id=17) is None


def test_product_card_snapshot_combines_metrics_blockers_and_orders(db_session) -> None:
    _seed_order(db_session)

    snapshot = build_product_card_snapshot(
        db_session,
        product_id="1646",
        settings=_settings(),
    )

    assert snapshot["identity"]["xml_id"] == PRODUCT_GUID
    assert snapshot["identity"]["bitrix_url"] == "/crm/catalog/17/product/1646/"
    assert snapshot["demand"] == {
        "sales_30": Decimal("30"),
        "sales_90": Decimal("90"),
        "sales_180": Decimal("180"),
        "rate_30": Decimal("1.000"),
        "rate_90": Decimal("1.000"),
        "rate_180": Decimal("1.000"),
        "sellable_stock": Decimal("4"),
        "customer_orders": Decimal("2"),
        "incoming": Decimal("3"),
        "target_stock": Decimal("9"),
        "recommended_order": Decimal("7"),
        "current_order": Decimal("5"),
    }
    assert snapshot["blockers"][0]["code"] == "batch_error_suspected"
    assert snapshot["orders"][0]["bitrix_process_url"].endswith("/7001/")
    assert snapshot["source"]["state"] == "ready"

    fields = product_card_native_fields(snapshot)
    assert set(fields) == {spec["key"] for spec in PRODUCT_CARD_FIELD_SPECS}
    assert len(fields) == 8
    assert fields["blocker_count"] == 1
    assert Decimal(str(fields["recommended_order"])) == Decimal("7")
    assert "sales_30" not in fields
    assert "sellable_stock" not in fields


def test_product_card_snapshot_uses_persisted_product_binding_without_order_line(
    product_card_classification_db,
) -> None:
    db_session = product_card_classification_db
    db_session.execute(
        ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE.insert().values(
            nomenclature_code="РБ000006739",
            name="Дисплей из классификации",
            folder="Дисплеи",
            status="working",
            status_label="Рабочий",
            product_ref=CLASSIFICATION_ONLY_GUID,
            source_record={"sales_qty_window_short": "12"},
            source_hash="b" * 64,
            source="test",
            classified_at=datetime(2026, 9, 2, 9, 0, 0),
        )
    )
    db_session.add(
        ProcurementProductCardSyncState(
            product_xml_id=CLASSIFICATION_ONLY_GUID,
            bitrix_product_id="1648",
            snapshot_hash="a" * 64,
            desired_fields={},
            readback_fields={},
            status="synced",
        )
    )
    db_session.commit()

    snapshot = build_product_card_snapshot(
        db_session,
        product_id="1648",
        settings=_settings(),
        product_loader=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("persisted exact binding should avoid a Bitrix lookup")
        ),
    )

    assert snapshot["identity"] == {
        "bitrix_product_id": "1648",
        "xml_id": CLASSIFICATION_ONLY_GUID,
        "nomenclature_code": "РБ000006739",
        "name": "Дисплей из классификации",
        "article": "",
        "photo_url": None,
        "website_url": None,
        "bitrix_url": "/crm/catalog/17/product/1648/",
    }
    assert snapshot["demand"]["sales_30"] == Decimal("12")


def test_registry_and_event_expose_native_product_links(db_session) -> None:
    order = _seed_order(db_session)
    list_item = serialize_order_list_item(order)

    assert list_item["blocked_products"][0]["bitrix_product_id"] == "1646"
    assert list_item["blocked_products"][0]["bitrix_url"].endswith("/product/1646/")

    event = ProcurementOrderFormationEvent(
        order_id=order.id,
        entity_type="order_line",
        entity_id=str(order.lines[0].id),
        event_type="order_line_changed",
        actor="test",
        before={},
        after={
            "lines": [
                {
                    "id": order.lines[0].id,
                    "bitrix_product_id": "1646",
                    "bitrix_product_xml_id": PRODUCT_GUID,
                    "nomenclature_code": "РБ000006737",
                    "nomenclature_name": "Дисплей тест",
                }
            ]
        },
        payload={},
    )
    db_session.add(event)
    db_session.flush()

    assert serialize_event(event)["product"]["bitrix_url"].endswith("/product/1646/")


def test_sync_is_dry_run_by_default_and_apply_requires_flag(db_session) -> None:
    _seed_order(db_session)

    result = sync_product_cards(
        db_session,
        scope="displays",
        settings=_settings(),
        mapping=_mapping(),
        resolver=lambda *_args, **_kwargs: {},
    )

    assert result["mode"] == "dry_run"
    assert result["items"][0]["status"] == "would_update"
    assert db_session.scalar(select(ProcurementProductCardSyncState)) is None

    try:
        sync_product_cards(
            db_session,
            scope="displays",
            apply=True,
            settings=_settings(apply=False),
            mapping=_mapping(),
            resolver=lambda *_args, **_kwargs: {},
        )
    except PermissionError as exc:
        assert "disabled" in str(exc)
    else:
        raise AssertionError("apply without the server flag must fail")


def test_dry_run_reports_missing_mapping_but_apply_stays_blocked(db_session) -> None:
    _seed_order(db_session)
    missing_mapping = {"catalog_id": 17, "fields": {}}

    result = sync_product_cards(
        db_session,
        scope="displays",
        settings=_settings(),
        mapping=missing_mapping,
        resolver=lambda *_args, **_kwargs: {},
    )

    assert result["mode"] == "dry_run"
    assert result["items"][0]["status"] == "would_update"
    assert result["missing_mapping_fields"] == [spec["key"] for spec in PRODUCT_CARD_FIELD_SPECS]

    with pytest.raises(RuntimeError, match="mapping is incomplete"):
        sync_product_cards(
            db_session,
            scope="displays",
            apply=True,
            settings=_settings(apply=True),
            mapping=missing_mapping,
            resolver=lambda *_args, **_kwargs: {},
        )


def test_dry_run_tolerates_missing_sync_state_table_but_apply_stays_blocked(
    db_session,
    monkeypatch,
) -> None:
    _seed_order(db_session)
    monkeypatch.setattr(
        product_card_service,
        "_product_card_sync_state_table_exists",
        lambda _db: False,
    )

    result = sync_product_cards(
        db_session,
        scope="displays",
        settings=_settings(),
        mapping=_mapping(),
        resolver=lambda *_args, **_kwargs: {},
    )

    assert result["mode"] == "dry_run"
    assert result["items"][0]["status"] == "would_update"

    with pytest.raises(RuntimeError, match="sync state table is missing"):
        sync_product_cards(
            db_session,
            scope="displays",
            apply=True,
            settings=_settings(apply=True),
            mapping=_mapping(),
            resolver=lambda *_args, **_kwargs: {},
        )


def test_apply_updates_reads_back_and_second_run_is_idempotent(db_session) -> None:
    _seed_order(db_session)
    second_guid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    _seed_order(
        db_session,
        suffix="2",
        product_id="1647",
        product_guid=second_guid,
        product_code="РБ000006738",
        product_name="Дисплей второй",
    )
    stored: dict[str, dict[str, object]] = {}
    calls: list[str] = []

    def caller(method: str, params: dict, **_kwargs):
        calls.append(method)
        assert method == "batch"
        results: dict[str, object] = {}
        for alias, command in params["cmd"].items():
            query = parse_qs(urlsplit(command).query)
            product_id = query["id"][0]
            if command.startswith("crm.product.update?"):
                stored[product_id] = {
                    key.removeprefix("fields[").removesuffix("]"): values[0]
                    for key, values in query.items()
                    if key.startswith("fields[")
                }
                results[alias] = True
            elif command.startswith("crm.product.get?"):
                results[alias] = {"ID": product_id, **stored[product_id]}
            else:
                raise AssertionError(command)
        return {"result": {"result": results, "result_error": {}}}

    first = sync_product_cards(
        db_session,
        scope="displays",
        apply=True,
        settings=_settings(apply=True),
        mapping=_mapping(),
        caller=caller,
        resolver=lambda *_args, **_kwargs: {},
    )
    second = sync_product_cards(
        db_session,
        scope="displays",
        apply=True,
        settings=_settings(apply=True),
        mapping=_mapping(),
        caller=caller,
        resolver=lambda *_args, **_kwargs: {},
    )

    assert first["updated"] == 2
    assert first["blocked"] == 0, first
    assert second["unchanged"] == 2
    assert calls == ["batch", "batch"]
    states = list(db_session.scalars(select(ProcurementProductCardSyncState)).all())
    assert len(states) == 2
    assert all(state.status == "synced" for state in states)


def test_batch_sync_isolates_one_product_failure(db_session) -> None:
    _seed_order(db_session)
    _seed_order(
        db_session,
        suffix="2",
        product_id="1647",
        product_guid="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        product_code="РБ000006738",
        product_name="Дисплей второй",
    )
    stored: dict[str, dict[str, object]] = {}

    def caller(method: str, params: dict, **_kwargs):
        assert method == "batch"
        if "update_0" in params["cmd"]:
            for _alias, command in params["cmd"].items():
                query = parse_qs(urlsplit(command).query)
                product_id = query["id"][0]
                stored[product_id] = {
                    key.removeprefix("fields[").removesuffix("]"): values[0]
                    for key, values in query.items()
                    if key.startswith("fields[")
                }
            return {
                "result": {
                    "result": {"update_0": True},
                    "result_error": {
                        "update_1": {
                            "error": "ERROR_CORE",
                            "error_description": "test failure",
                        }
                    },
                }
            }
        return {
            "result": {
                "result": {"read_0": {"ID": "1646", **stored["1646"]}},
                "result_error": {},
            }
        }

    result = sync_product_cards(
        db_session,
        scope="displays",
        apply=True,
        settings=_settings(apply=True),
        mapping=_mapping(),
        caller=caller,
        resolver=lambda *_args, **_kwargs: {},
    )

    assert result["updated"] == 1
    assert result["blocked"] == 1
    assert {item["status"] for item in result["items"]} == {"synced", "failed"}
    states = {
        state.bitrix_product_id: state
        for state in db_session.scalars(select(ProcurementProductCardSyncState)).all()
    }
    assert states["1646"].status == "synced"
    assert states["1647"].status == "failed"
    assert "test failure" in str(states["1647"].last_error)


def test_native_product_lookup_uses_exact_bitrix_id(monkeypatch) -> None:
    def fake_call(method: str, params: dict, **_kwargs):
        assert method == "crm.product.get"
        assert params == {"id": 1646}
        return {
            "result": {
                "ID": "1646",
                "NAME": "Дисплей тест",
                "XML_ID": PRODUCT_GUID,
                "PREVIEW_PICTURE": {"showUrl": "/upload/display.webp"},
            }
        }

    monkeypatch.setattr(bitrix_order_service, "bitrix_call", fake_call)
    product = resolve_catalog_product_by_id(
        "1646",
        settings=_settings(),
        mapping={
            "catalog": {
                "product_id": "ID",
                "name": "NAME",
                "xml_id": "XML_ID",
            }
        },
    )

    assert product is not None
    assert product.product_id == "1646"
    assert product.xml_id == PRODUCT_GUID
    assert product.photo_thumbnail_url == "/upload/display.webp"


def test_field_provisioning_discovers_existing_and_creates_only_missing() -> None:
    created: list[str] = []

    def caller(method: str, params: dict, **_kwargs):
        if method == "crm.product.fields":
            result = {
                "PROPERTY_900": {
                    "title": "Переименованное существующее поле",
                    "CODE": field_code(PRODUCT_CARD_FIELD_SPECS[0]["key"]),
                },
                "PROPERTY_901": {
                    "title": "Ещё одно существующее поле",
                    "XML_ID": field_code(PRODUCT_CARD_FIELD_SPECS[1]["key"]),
                },
                "PROPERTY_902": {"title": PRODUCT_CARD_FIELD_SPECS[2]["title"]},
            }
            result.update(
                {f"PROPERTY_{903 + index}": {"title": title} for index, title in enumerate(created)}
            )
            return {"result": result}
        if method == "crm.product.property.add":
            assert params["fields"]["IBLOCK_ID"] == 17
            created.append(params["fields"]["NAME"])
            return {"result": 902 + len(created)}
        raise AssertionError(method)

    fields = ensure_product_fields(caller=caller, settings=_settings())
    mapping = build_mapping(fields, catalog_id=17, placement="CRM_PRODUCT_DETAIL_TAB")

    assert len(created) == len(PRODUCT_CARD_FIELD_SPECS) - 3
    assert mapping["missing_fields"] == []
    assert set(mapping["fields"]) == {spec["key"] for spec in PRODUCT_CARD_FIELD_SPECS}
    assert mapping["fields"][PRODUCT_CARD_FIELD_SPECS[0]["key"]] == "PROPERTY_900"
    assert mapping["fields"][PRODUCT_CARD_FIELD_SPECS[1]["key"]] == "PROPERTY_901"


def test_standard_cron_keeps_product_card_sync_behind_two_flags() -> None:
    script = Path("infra/cron/display_auto_order_sync.sh").read_text(encoding="utf-8")

    assert "DISPLAY_AUTO_ORDER_PRODUCT_CARD_SYNC_ENABLED" in script
    assert "PROCUREMENT_PRODUCT_CARD_APPLY_ENABLED" in script
    assert "-m tasks.sync_procurement_product_cards" in script
    assert "product_card_cmd+=(--apply)" in script


def test_product_insights_placement_is_bound_only_when_missing() -> None:
    calls: list[tuple[str, dict]] = []

    def caller(method: str, params: dict, **_kwargs):
        calls.append((method, params))
        if method == "placement.get":
            return {"result": []}
        if method == "placement.bind":
            return {"result": True}
        raise AssertionError(method)

    changed = ensure_placement(
        placement="CRM_PRODUCT_DETAIL_TAB",
        handler_url=(
            "https://pricing.example.test/bitrix/" "procurement-order-formation/product-insights"
        ),
        caller=caller,
        settings=_settings(),
    )

    assert changed is True
    assert [method for method, _params in calls] == ["placement.get", "placement.bind"]
    assert calls[1][1]["TITLE"] == "Показатели товара"
