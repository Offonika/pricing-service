from __future__ import annotations

import importlib.util
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, insert, select

from app.services.onec_stock_availability import (
    COVERAGE_TABLE,
    CURRENT_STOCK_BY_CODE_SQL,
    DAY_DELTA_TABLE,
    INTERVAL_TABLE,
    MOVEMENT_SQL,
    OPENING_BALANCE_SQL,
    SYNC_RUN_TABLE,
    attach_effective_availability_shadow_to_facts,
    build_availability_rows,
    build_current_stock_snapshot,
    enforce_retention,
    metadata,
)


def _opening(quantity: str = "3") -> dict[str, object]:
    return {
        "source_register": "warehouse",
        "product_ref": "0xPRODUCT",
        "product_code": "РБ0001",
        "warehouse_key": "0xSHOP",
        "warehouse_code": "SHOP-1",
        "quantity": quantity,
    }


def _movement(
    business_date: date,
    *,
    receipt: str = "0",
    expense: str = "0",
) -> dict[str, object]:
    return {
        "source_register": "warehouse",
        "product_ref": "0xPRODUCT",
        "product_code": "РБ0001",
        "warehouse_key": "0xSHOP",
        "warehouse_code": "SHOP-1",
        "business_date": business_date,
        "receipt_qty": receipt,
        "expense_qty": expense,
    }


def test_build_availability_counts_sold_out_day_and_restarts_after_receipt() -> None:
    result = build_availability_rows(
        month=date(2026, 7, 1),
        range_start=date(2026, 7, 1),
        range_end=date(2026, 7, 7),
        opening_rows=[_opening()],
        movement_rows=[
            _movement(date(2026, 7, 3), expense="3"),
            _movement(date(2026, 7, 5), receipt="2"),
        ],
    )

    assert [(row["available_from"], row["available_to"]) for row in result.intervals] == [
        (date(2026, 7, 1), date(2026, 7, 3)),
        (date(2026, 7, 5), date(2026, 7, 7)),
    ]
    sold_out = result.day_deltas[0]
    assert sold_out["opening_qty"] == Decimal("3.000")
    assert sold_out["closing_qty"] == Decimal("0.000")
    assert sold_out["available_day"] is True


def test_build_availability_replays_movements_before_partial_range() -> None:
    result = build_availability_rows(
        month=date(2026, 7, 1),
        range_start=date(2026, 7, 3),
        range_end=date(2026, 7, 7),
        opening_rows=[_opening("5")],
        movement_rows=[
            _movement(date(2026, 7, 2), expense="5"),
            _movement(date(2026, 7, 6), receipt="1"),
        ],
    )

    assert [(row["available_from"], row["available_to"]) for row in result.intervals] == [
        (date(2026, 7, 6), date(2026, 7, 7))
    ]
    assert len(result.day_deltas) == 1
    assert result.day_deltas[0]["business_date"] == date(2026, 7, 6)


def test_build_availability_keeps_opening_balance_without_daily_grid() -> None:
    result = build_availability_rows(
        month=date(2026, 7, 1),
        range_start=date(2026, 7, 1),
        range_end=date(2026, 7, 31),
        opening_rows=[_opening("2")],
        movement_rows=[],
    )

    assert result.day_deltas == ()
    assert len(result.intervals) == 1
    assert result.intervals[0]["available_from"] == date(2026, 7, 1)
    assert result.intervals[0]["available_to"] == date(2026, 7, 31)


def test_shadow_metrics_use_only_explicit_physical_sales_points() -> None:
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    try:
        with engine.begin() as connection:
            run_id = connection.execute(
                insert(SYNC_RUN_TABLE).values(
                    run_key="test",
                    range_start=date(2026, 7, 1),
                    range_end=date(2026, 7, 3),
                    status="ready",
                    opening_rows=1,
                    movement_rows=0,
                    day_delta_rows=0,
                    interval_rows=2,
                    summary={},
                    started_at=date(2026, 7, 4),
                    finished_at=date(2026, 7, 4),
                    created_at=date(2026, 7, 4),
                )
            ).inserted_primary_key[0]
            connection.execute(
                insert(COVERAGE_TABLE).values(
                    period_month=date(2026, 7, 1),
                    covered_from=date(2026, 7, 1),
                    covered_to=date(2026, 7, 3),
                    status="ready",
                    last_run_id=run_id,
                    updated_at=date(2026, 7, 4),
                )
            )
            connection.execute(
                insert(INTERVAL_TABLE),
                [
                    {
                        "period_month": date(2026, 7, 1),
                        "source_register": "warehouse",
                        "product_ref": "0xPRODUCT",
                        "product_code": "РБ0001",
                        "warehouse_key": "0xSHOP",
                        "warehouse_code": "SHOP-1",
                        "available_from": date(2026, 7, 1),
                        "available_to": date(2026, 7, 2),
                        "last_run_id": run_id,
                        "updated_at": date(2026, 7, 4),
                    },
                    {
                        "period_month": date(2026, 7, 1),
                        "source_register": "warehouse",
                        "product_ref": "0xPRODUCT",
                        "product_code": "РБ0001",
                        "warehouse_key": "0xONLINE",
                        "warehouse_code": "ONLINE",
                        "available_from": date(2026, 7, 1),
                        "available_to": date(2026, 7, 3),
                        "last_run_id": run_id,
                        "updated_at": date(2026, 7, 4),
                    },
                ],
            )

        facts = attach_effective_availability_shadow_to_facts(
            engine,
            [
                {
                    "product_ref": "0xPRODUCT",
                    "warehouses": [
                        {
                            "warehouse_code": "SHOP-1",
                            "role": "physical_sales_point",
                            "sells_systematically": True,
                        },
                        {
                            "warehouse_code": "ONLINE",
                            "role": "online_site_reserve",
                            "sells_systematically": True,
                        },
                    ],
                }
            ],
            date_to=date(2026, 7, 3),
            history_days=3,
        )

        shadow = facts[0]["effective_availability_shadow"]
        assert shadow["coverage_status"] == "ready"
        assert shadow["physical_sales_point_count"] == 1
        assert shadow["available_point_days"] == 2
        assert shadow["out_of_stock_point_days"] == 1
        assert shadow["points"] == [
            {
                "warehouse_code": "SHOP-1",
                "available_days": 2,
                "out_of_stock_days": 1,
            }
        ]
    finally:
        engine.dispose()


def test_retention_deletes_old_rows_and_trims_overlapping_interval() -> None:
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    try:
        with engine.begin() as connection:
            connection.execute(
                insert(DAY_DELTA_TABLE),
                [
                    {
                        "business_date": date(2026, 1, 1),
                        "period_month": date(2026, 1, 1),
                        "source_register": "warehouse",
                        "product_ref": "old",
                        "product_code": "",
                        "warehouse_key": "shop",
                        "warehouse_code": "SHOP",
                        "opening_qty": 1,
                        "receipt_qty": 0,
                        "expense_qty": 1,
                        "closing_qty": 0,
                        "available_day": True,
                        "updated_at": date(2026, 1, 2),
                    }
                ],
            )
            connection.execute(
                insert(INTERVAL_TABLE),
                [
                    {
                        "period_month": date(2026, 1, 1),
                        "source_register": "warehouse",
                        "product_ref": "overlap",
                        "product_code": "",
                        "warehouse_key": "shop",
                        "warehouse_code": "SHOP",
                        "available_from": date(2026, 1, 1),
                        "available_to": date(2026, 7, 1),
                        "updated_at": date(2026, 7, 2),
                    }
                ],
            )

        assert enforce_retention(engine, cutoff=date(2026, 2, 1)) == 1
        with engine.connect() as connection:
            assert connection.execute(select(DAY_DELTA_TABLE)).first() is None
            interval = connection.execute(select(INTERVAL_TABLE)).mappings().one()
        assert interval["available_from"] == date(2026, 2, 1)
    finally:
        engine.dispose()


def test_sql_reads_monthly_totals_and_active_movements_from_both_registers() -> None:
    opening_sql = str(OPENING_BALANCE_SQL)
    movement_sql = str(MOVEMENT_SQL)

    assert "_AccumRgT7745" in opening_sql
    assert "_AccumRgT7759" in opening_sql
    assert "_AccumRg7735" in movement_sql
    assert "_AccumRg7747" in movement_sql
    assert movement_sql.count("_Active = 0x01") == 2


def test_current_stock_sql_reads_verified_current_totals_by_product_code() -> None:
    current_sql = str(CURRENT_STOCK_BY_CODE_SQL)

    assert "_AccumRgT7745" in current_sql
    assert "_Reference62" in current_sql
    assert "t._Period = :current_totals_period" in current_sql
    assert "positive_quantity" in current_sql
    assert "net_quantity" in current_sql


def test_build_current_stock_snapshot_keeps_positive_and_net_quantities() -> None:
    captured_at = datetime(2026, 8, 16, 9, tzinfo=UTC)
    snapshot = build_current_stock_snapshot(
        [
            {
                "product_code": "РБ0001",
                "source_row_count": 3,
                "positive_row_count": 2,
                "positive_quantity": "5.000",
                "net_quantity": "4.000",
            },
            {
                "product_code": "РБ0002",
                "source_row_count": 1,
                "positive_row_count": 0,
                "positive_quantity": "0",
                "net_quantity": "-1",
            },
        ],
        captured_at=captured_at,
    )

    assert snapshot.source_status == "ready"
    assert snapshot.captured_at == captured_at
    assert snapshot.source_row_count == 4
    assert snapshot.product_code_count == 2
    assert snapshot.positive_row_count == 2
    assert snapshot.positive_product_code_count == 1
    assert snapshot.total_positive_quantity == Decimal("5.000")
    assert snapshot.total_net_quantity == Decimal("3.000")
    assert snapshot.quantities_by_code == {
        "РБ0001": Decimal("5.000"),
        "РБ0002": Decimal("0.000"),
    }


def test_migration_upgrade_and_downgrade(tmp_path: Path) -> None:
    migration_path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/d1a2b3c4e5f7_add_onec_stock_availability.py"
    )
    spec = importlib.util.spec_from_file_location(
        "onec_stock_availability_migration", migration_path
    )
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    try:
        with engine.begin() as connection:
            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()
            table_names = set(connection.dialect.get_table_names(connection))
            assert {
                SYNC_RUN_TABLE.name,
                COVERAGE_TABLE.name,
                DAY_DELTA_TABLE.name,
                INTERVAL_TABLE.name,
            } <= table_names
            migration.downgrade()
            remaining = set(connection.dialect.get_table_names(connection))
            assert INTERVAL_TABLE.name not in remaining
    finally:
        engine.dispose()


def test_cron_uses_active_release_symlink() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    wrapper = (repo_root / "infra/cron/onec_stock_availability.sh").read_text(encoding="utf-8")
    cron = (repo_root / "infra/cron/onec_stock_availability.cron").read_text(encoding="utf-8")

    active_release = "/opt/MM/pricing-service-task43-current"
    assert f'REPO_DIR="${{REPO_DIR:-{active_release}}}"' in wrapper
    assert f"{active_release}/infra/cron/onec_stock_availability.sh nightly" in cron
    assert f"{active_release}/infra/cron/onec_stock_availability.sh weekly" in cron
