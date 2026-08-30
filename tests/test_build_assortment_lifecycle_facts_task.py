from __future__ import annotations

import json
import subprocess
import sys
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine, text

from tasks import build_assortment_lifecycle_facts as task
from tasks.build_assortment_lifecycle_facts import _default_history_months, _default_limit

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_build_assortment_lifecycle_facts_task_reads_history_months_from_env(
    monkeypatch,
) -> None:
    monkeypatch.setenv("ASSORTMENT_LIFECYCLE_HISTORY_MONTHS", "48")

    assert _default_history_months() == 48


def test_build_assortment_lifecycle_facts_task_uses_safe_default_limit(
    monkeypatch,
) -> None:
    monkeypatch.delenv("ASSORTMENT_LIFECYCLE_LIMIT", raising=False)
    assert _default_limit() == 3000

    monkeypatch.setenv("ASSORTMENT_LIFECYCLE_LIMIT", "1200")
    assert _default_limit() == 1200


def test_build_assortment_lifecycle_facts_uses_role_specific_read_only_db_access(
    monkeypatch,
    tmp_path: Path,
) -> None:
    onec_engine_calls: list[tuple[str, int, int]] = []
    scope_calls: list[bool] = []
    application_engines: list[object] = []

    class FakeOnecEngine:
        disposed = False

        def dispose(self) -> None:
            self.disposed = True

    class FakeSession:
        def get_bind(self) -> object:
            engine = object()
            application_engines.append(engine)
            return engine

    onec_engine = FakeOnecEngine()

    def fake_build_onec_engine(
        database_url: str,
        *,
        query_timeout_seconds: int,
        login_timeout_seconds: int,
    ) -> FakeOnecEngine:
        onec_engine_calls.append((database_url, query_timeout_seconds, login_timeout_seconds))
        return onec_engine

    @contextmanager
    def fake_session_scope(*, read_only: bool = False):
        scope_calls.append(read_only)
        yield FakeSession()

    args = SimpleNamespace(
        folder="дисплеи",
        history_months=36,
        today=date(2026, 8, 30),
        limit=100,
        onec_database_url="",
        input_json=None,
        warehouse_policy_json=tmp_path / "warehouse-policy.json",
        supplier_order_mapping_json=tmp_path / "supplier-mapping.json",
        receipt_mapping_json=tmp_path / "receipt-mapping.json",
        manual_overrides_json=None,
        manager_signals_json=None,
        output_json=None,
        json=False,
    )
    nomenclature_rows = [{"nomenclature_code": "РБ0001"}]
    facts = [{"nomenclature_code": "РБ0001", "product_ref": "0xA"}]

    monkeypatch.setattr(task, "load_ut103_env_file", lambda: None)
    monkeypatch.setattr(task, "_parse_args", lambda: args)
    monkeypatch.setattr(
        task,
        "get_settings",
        lambda: SimpleNamespace(
            database_url="postgresql://application-snapshot",
            onec_database_url="mssql+pyodbc://onec-snapshot",
            onec_query_timeout_seconds=45,
            onec_login_timeout_seconds=7,
        ),
    )
    monkeypatch.setattr(task, "validate_warehouse_policy", lambda _payload: object())
    monkeypatch.setattr(task, "_load_json_object", lambda _path: {})
    monkeypatch.setattr(task, "_load_document_line_mapping", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(task, "build_onec_engine", fake_build_onec_engine)
    monkeypatch.setattr(task, "session_scope", fake_session_scope)
    monkeypatch.setattr(
        task,
        "fetch_onec_lifecycle_source_rows",
        lambda engine, **_kwargs: ((nomenclature_rows, [], []) if engine is onec_engine else None),
    )
    monkeypatch.setattr(task, "fetch_first_sale_dates", lambda engine, **_kwargs: {})
    monkeypatch.setattr(task, "fetch_sales_window_totals", lambda engine, **_kwargs: {})
    monkeypatch.setattr(
        task,
        "enrich_nomenclature_rows_with_product_snapshot",
        lambda engine, rows: list(rows),
    )
    monkeypatch.setattr(task, "physical_sales_point_codes", lambda _policy: ())
    monkeypatch.setattr(task, "fetch_days_in_sale_by_code", lambda engine, **_kwargs: {})
    monkeypatch.setattr(task, "fetch_previous_statuses", lambda engine: {})
    monkeypatch.setattr(
        task,
        "build_assortment_lifecycle_fact_records",
        lambda **_kwargs: (facts, {"ready": 1}),
    )
    monkeypatch.setattr(
        task,
        "attach_effective_availability_shadow_to_facts",
        lambda engine, rows, **_kwargs: list(rows),
    )

    assert task.main() == 0

    assert onec_engine_calls == [("mssql+pyodbc://onec-snapshot", 45, 7)]
    assert onec_engine.disposed is True
    assert scope_calls == [True, True]
    assert len(application_engines) == 2


def test_build_assortment_lifecycle_facts_task_feeds_updates_task(tmp_path: Path) -> None:
    raw_path = tmp_path / "source-rows.json"
    warehouse_path = tmp_path / "warehouse-policy.json"
    signals_path = tmp_path / "manager-signals.json"
    facts_path = tmp_path / "assortment-lifecycle-facts.json"
    updates_path = tmp_path / "nomenclature-property-updates.json"

    raw_path.write_text(
        json.dumps(
            {
                "nomenclature_rows": [
                    {
                        "nomenclature_ref": "0xA",
                        "nomenclature_code": "РБ0001",
                        "name": "Дисплей тестовый A",
                        "folder_path": "ОБЩИЙ КАТАЛОГ / дисплеи",
                        "item_value": "300",
                    },
                    {
                        "nomenclature_ref": "0xB",
                        "nomenclature_code": "РБ0002",
                        "name": "Дисплей тестовый B",
                        "folder_path": "ОБЩИЙ КАТАЛОГ / дисплеи",
                        "item_value": "100",
                    },
                ],
                "supplier_order_rows": [
                    {
                        "nomenclature_ref": "0xA",
                        "order_date": "2026-01-01",
                        "cargo_handoff_date": "2026-01-05",
                        "line_price": "300",
                    },
                    {
                        "nomenclature_ref": "0xA",
                        "order_date": "2026-02-01",
                        "cargo_handoff_date": "2026-02-05",
                        "line_price": "320",
                    },
                ],
                "receipt_rows": [
                    {"nomenclature_ref": "0xA", "receipt_date": "2026-01-10"},
                    {"nomenclature_ref": "0xA", "receipt_date": "2026-02-10"},
                    {"nomenclature_ref": "0xA", "receipt_date": "2026-03-10"},
                    {"nomenclature_ref": "0xA", "receipt_date": "2026-04-10"},
                    {"nomenclature_ref": "0xA", "receipt_date": "2026-05-10"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    warehouse_path.write_text(
        json.dumps(
            {
                "warehouses": [
                    {"warehouse_code": "shop-1", "sells_systematically": True},
                    {"warehouse_code": "central", "is_central": True},
                    {"warehouse_code": "defect", "is_defect_warehouse": True},
                    {"warehouse_code": "transit", "is_transit": True},
                    {"warehouse_code": "rare", "is_non_systematic_sale": True},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    signals_path.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "nomenclature_code": "РБ0001",
                        "manager_id": "manager-1",
                        "quantity": 1,
                        "source": "offline_call",
                        "signal_date": "2026-01-03",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    facts_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tasks.build_assortment_lifecycle_facts",
            "--input-json",
            str(raw_path),
            "--warehouse-policy-json",
            str(warehouse_path),
            "--manager-signals-json",
            str(signals_path),
            "--today",
            "2026-06-25",
            "--output-json",
            str(facts_path),
            "--json",
        ],
        check=True,
        capture_output=True,
        cwd=PROJECT_ROOT,
        text=True,
    )

    facts_summary = json.loads(facts_result.stdout)
    assert facts_summary["status"] == "ready"
    assert facts_summary["items"] == 2
    assert facts_path.exists()
    facts_payload = json.loads(facts_path.read_text(encoding="utf-8"))
    assert facts_payload["meta"]["schema"] == "assortment_lifecycle_facts.v1"
    assert facts_payload["items"][0]["receipt_dates"][-1] == "2026-05-10"

    updates_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tasks.build_assortment_lifecycle_updates",
            "--input-json",
            str(facts_path),
            "--folder",
            "дисплеи",
            "--changed-at",
            "2026-06-25",
            "--output-json",
            str(updates_path),
            "--json",
        ],
        check=True,
        capture_output=True,
        cwd=PROJECT_ROOT,
        text=True,
    )

    updates_summary = json.loads(updates_result.stdout)
    item = updates_summary["items"][0]
    # 2026-08-02: 5 поступлений за 180 дней дают Рабочий сразу, без
    # подтверждения ответственного (решение 2026-07-20 доведено до кода).
    assert item["status"] == "working"
    assert item["recommended_status"] is None
    assert item["blockers"] == []
    assert item["sales_point_warehouse_codes"] == ["shop-1"]
    assert item["expensive_profile"] == "fast_expensive"
    assert updates_summary["rows"] > 0


def test_build_assortment_lifecycle_facts_task_blocks_without_receipt_mapping(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "onec.db"
    warehouse_path = tmp_path / "warehouse-policy.json"
    supplier_mapping_path = tmp_path / "supplier-mapping.json"
    receipt_mapping_path = tmp_path / "receipt-mapping.json"

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE supplier_doc "
                "(_IDRRef TEXT, _Date_Time TEXT, _Posted TEXT, _Marked TEXT)"
            )
        )
        conn.execute(
            text("CREATE TABLE supplier_lines " "(_DocumentRRef TEXT, _NomenclatureRRef TEXT)")
        )

    warehouse_path.write_text(
        json.dumps({"warehouses": [{"warehouse_code": "shop-1"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    supplier_mapping_path.write_text(
        json.dumps(
            {
                "document_table": "supplier_doc",
                "line_table": "supplier_lines",
                "line_document_column": "_DocumentRRef",
                "line_nomenclature_column": "_NomenclatureRRef",
            }
        ),
        encoding="utf-8",
    )
    receipt_mapping_path.write_text(
        json.dumps(
            {
                "document_table": "receipt_doc",
                "line_table": "receipt_lines",
                "line_document_column": "_DocumentRRef",
                "line_nomenclature_column": "_NomenclatureRRef",
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tasks.build_assortment_lifecycle_facts",
            "--onec-database-url",
            f"sqlite:///{db_path}",
            "--warehouse-policy-json",
            str(warehouse_path),
            "--supplier-order-mapping-json",
            str(supplier_mapping_path),
            "--receipt-mapping-json",
            str(receipt_mapping_path),
            "--json",
        ],
        capture_output=True,
        cwd=PROJECT_ROOT,
        text=True,
    )

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "blocked"
    assert payload["error"].startswith("receipt_mapping_unresolved:")
    assert "table_missing:receipt_doc" in payload["error"]
