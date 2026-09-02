from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.models.procurement_order_formation import (
    ProcurementOrderFormation,
    ProcurementOrderFormationLine,
)
from tasks import sync_procurement_order_product_rows as task


def _linked_order(db_session) -> ProcurementOrderFormation:
    order = ProcurementOrderFormation(
        stable_key="product-row-backfill",
        status="transmitted",
        lifecycle_status="active",
        origin="onec_import",
        version=1,
        bitrix_entity_type_id=1056,
        bitrix_item_id="317",
        supplier_name="1077 MINA",
        contract_name="Основной договор",
        warehouse_name="Склад",
        currency="RMB",
        procurement_contour="cargo",
        route="cargo",
        batch_id="onec-590",
        order_date=date(2026, 9, 1),
        calculation_id="onec:590",
        onec_status="transmitted",
        onec_document_number="РБГУ0000590",
    )
    order.lines = [
        ProcurementOrderFormationLine(
            stable_key="product-row-backfill:1",
            line_number=1,
            bitrix_product_id="2001",
            bitrix_product_xml_id="00000000-0000-0000-0000-000000000001",
            nomenclature_ref="00000000-0000-0000-0000-000000000001",
            nomenclature_name="Товар",
            final_quantity=Decimal("1"),
            purchase_price=Decimal("2.5"),
            amount=Decimal("2.5"),
            currency="RMB",
        )
    ]
    db_session.add(order)
    db_session.commit()
    return order


def test_apply_requires_explicit_scope_and_backup() -> None:
    with pytest.raises(ValueError, match="--all"):
        task.run(task.parse_args(["--apply"]))
    with pytest.raises(ValueError, match="--backup-path"):
        task.run(task.parse_args(["--apply", "--all"]))


def test_apply_exports_all_existing_rows_before_first_write(
    db_session, monkeypatch, tmp_path
) -> None:
    order = _linked_order(db_session)
    factory = sessionmaker(bind=db_session.get_bind())
    monkeypatch.setattr(task, "get_application_session_factory", lambda: factory)
    monkeypatch.setattr(
        task,
        "get_settings",
        lambda: Settings(procurement_bitrix_webhook_url="https://bitrix.example/rest/1/token"),
    )
    sequence: list[str] = []

    def fake_list(**_kwargs):
        sequence.append("backup")
        return [{"ID": "9001", "PRODUCT_ID": "2001"}]

    def fake_sync(_session, synced_order, **_kwargs):
        sequence.append("sync")
        return {"state": "synced", "order_id": synced_order.id}

    monkeypatch.setattr(task, "list_procurement_product_rows", fake_list)
    monkeypatch.setattr(task, "sync_procurement_order_product_rows", fake_sync)
    backup_path = tmp_path / "product-rows-backup.json"

    result = task.run(
        task.parse_args(
            [
                "--apply",
                "--order-id",
                str(order.id),
                "--backup-path",
                str(backup_path),
            ]
        )
    )

    assert result["summary"] == {"synced": 1}
    assert sequence == ["backup", "sync"]
    assert '"item_id": "317"' in backup_path.read_text(encoding="utf-8")
