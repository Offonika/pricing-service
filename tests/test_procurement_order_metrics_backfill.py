from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.models.procurement_order_formation import (
    ProcurementOrderFormation,
    ProcurementOrderFormationLine,
    ProcurementSupplierProfile,
)
from app.services import procurement_order_metrics_backfill as backfill


def _order(db_session) -> ProcurementOrderFormation:
    order = ProcurementOrderFormation(
        stable_key="metrics-order",
        status="draft",
        version=1,
        supplier_ref="0xSUPPLIER",
        supplier_code="SUP-1",
        supplier_name="Поставщик",
        contract_ref="0xCONTRACT",
        contract_code="CON-1",
        contract_name="Договор",
        warehouse_ref="0xWAREHOUSE",
        warehouse_code="WH-1",
        warehouse_name="Склад",
        currency="RUB",
        procurement_contour="ordinary",
        route="ordinary",
        batch_id="2026-08-01",
        order_date=date(2026, 8, 1),
        calculation_id="metrics",
        onec_status="not_sent",
        payload={},
    )
    order.lines.append(
        ProcurementOrderFormationLine(
            stable_key="metrics-order:SKU-1",
            line_number=1,
            version=1,
            bitrix_product_id="1",
            bitrix_product_xml_id="11111111-2222-3333-4444-555555555555",
            nomenclature_ref="11111111-2222-3333-4444-555555555555",
            nomenclature_code="SKU-1",
            nomenclature_name="Дисплей для Phone 1 (OLED)",
            recommended_quantity=Decimal("2"),
            final_quantity=Decimal("2"),
            purchase_price=Decimal("610"),
            amount=Decimal("1220"),
            currency="RUB",
            source_kind="automatic",
            explicit_demand=False,
            risk_codes=[],
            blockers=[],
            payload={
                "product_card_url": "https://master-mobile.ru/catalog/a/1/",
                "photos": [{"original": "https://master-mobile.ru/upload/1.webp"}],
            },
            removed=False,
        )
    )
    db_session.add(order)
    db_session.commit()
    return order


def _stub_sources(monkeypatch) -> None:
    monkeypatch.setattr(
        backfill,
        "fetch_procurement_line_metrics_from_onec",
        lambda *_args, **_kwargs: {
            ("SKU-1", "0xsupplier", "RUB"): {
                "metrics_as_of": "2026-08-01",
                "metrics_window_days": 180,
                "profitability_pct": "25.00",
                "profitability_status": "ready",
                "product_defect_pct": "4.00",
                "product_defect_history_units": 50,
                "product_defect_confidence": "warning",
                "supplier_defect_attribution": "unconfirmed",
                "supplier_defect_source_status": "not_traceable",
                "price_change_pct": "11.00",
                "price_history_count": 2,
            }
        },
    )
    monkeypatch.setattr(
        backfill,
        "fetch_supplier_order_counts",
        lambda *_args, **_kwargs: {"0xsupplier": 7},
    )
    monkeypatch.setattr(
        backfill,
        "fetch_supplier_contract_terms",
        lambda *_args, **_kwargs: {
            ("0xsupplier", "0xcontract"): {
                "payment_terms": None,
                "credit_days": None,
                "credit_limit": None,
                "terms_source": "onec_contract",
                "terms_status": "missing",
                "contract_ref": "0xcontract",
                "contract_code": "CON-1",
                "contract_name": "Договор",
                "contract_source_status": "exact_contract_verified",
            }
        },
    )


def test_metrics_backfill_dry_run_apply_repeat_and_rollback(db_session, monkeypatch) -> None:
    order = _order(db_session)
    line = order.lines[0]
    commercial_before = (
        line.final_quantity,
        line.purchase_price,
        line.amount,
        order.supplier_ref,
        order.status,
    )
    _stub_sources(monkeypatch)
    lead_rows = [
        {
            "nomenclature_code": "SKU-1",
            "display_group_key": "phone 1",
            "recommended_supplier_prepare_days": "5",
            "recommended_logistics_days": "7",
            "lead_time_confidence": "high",
            "order_line_count": "3",
        }
    ]

    plan = backfill.build_metrics_backfill_plan(
        db_session,
        object(),
        lead_time_rows=lead_rows,
        as_of=date(2026, 8, 1),
        run_id="metrics-run",
    )
    assert plan["mode"] == "dry_run"
    assert plan["summary"]["lines_changed"] == 1
    assert line.payload.get("profitability_pct") is None

    manifest = backfill.apply_metrics_backfill(db_session, plan)
    db_session.commit()
    db_session.refresh(order)
    supplier_profile = db_session.query(ProcurementSupplierProfile).one()
    assert supplier_profile.version == 1
    assert line.payload["profitability_pct"] == "25.00"
    assert line.payload["supplier_prepare_days"] == 5
    assert line.payload["logistics_days"] == 7
    assert line.payload["lead_time_days"] == 12
    assert commercial_before == (
        line.final_quantity,
        line.purchase_price,
        line.amount,
        order.supplier_ref,
        order.status,
    )

    repeated = backfill.build_metrics_backfill_plan(
        db_session,
        object(),
        lead_time_rows=lead_rows,
        as_of=date(2026, 8, 1),
        run_id="metrics-repeat",
    )
    assert repeated["summary"]["lines_changed"] == 0
    assert repeated["summary"]["supplier_profiles_changed"] == 0

    result = backfill.rollback_metrics_backfill(db_session, manifest)
    db_session.commit()
    db_session.refresh(order)
    assert result["summary"]["rolled_back_lines"] == 1
    assert "profitability_pct" not in line.payload
    assert db_session.query(ProcurementSupplierProfile).count() == 0
    assert commercial_before == (
        line.final_quantity,
        line.purchase_price,
        line.amount,
        order.supplier_ref,
        order.status,
    )

    repeated_rollback = backfill.rollback_metrics_backfill(db_session, manifest)
    db_session.commit()
    assert repeated_rollback["summary"] == {
        "rolled_back_lines": 0,
        "rolled_back_orders": 0,
        "rolled_back_supplier_profiles": 0,
    }
