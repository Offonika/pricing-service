from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.dependencies import get_db
from app.core.config import get_settings
from app.main import app
from app.models import Base
from app.models.receivable_balance_snapshot import ReceivableBalanceSnapshot
from app.services.counterparty_folder_recommendations import (
    STATUS_MOVE_RECOMMENDED,
    STATUS_NEEDS_REVIEW,
    STATUS_NO_OVERDUE,
    STATUS_OK,
    build_counterparty_folder_recommendations,
)

SNAPSHOT_DATE = date(2026, 5, 29)


def _make_sqlite_engine(path: str | None = None):
    if path is not None:
        return create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    return create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


def _override_db(engine):
    def _override():
        db = Session(engine)
        try:
            yield db
        finally:
            db.close()

    return _override


def _snapshot(
    counterparty_ref: str,
    *,
    counterparty_name: str,
    balance: str,
    document_ref: str | None,
    document_number: str | None,
    document_date: datetime | None,
    credit_depth_days: int | None,
    is_overdue: bool,
    overdue_days: int | None,
) -> ReceivableBalanceSnapshot:
    due_date = (
        document_date + timedelta(days=credit_depth_days)
        if document_date is not None and credit_depth_days is not None
        else None
    )
    return ReceivableBalanceSnapshot(
        snapshot_date=SNAPSHOT_DATE,
        counterparty_ref=counterparty_ref,
        counterparty_name=counterparty_name,
        current_balance=Decimal(balance),
        origin_document_ref=document_ref,
        origin_document_number=document_number,
        origin_document_date=document_date,
        origin_manager_ref="mgr-origin",
        origin_manager_name="Менеджер долга",
        current_manager_ref="mgr-current",
        current_manager_name="Текущий менеджер",
        planned_payment_date=None,
        credit_depth_days=credit_depth_days,
        shipment_ban=False,
        payment_term_source="credit_depth_days" if credit_depth_days is not None else "missing",
        due_date=due_date,
        overdue_days=overdue_days,
        is_overdue=is_overdue,
        aged_bucket="31+",
        activity_segment="active",
    )


def _seed_app_db(engine) -> None:
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                _snapshot(
                    "cp-site",
                    counterparty_name="Контрагент из папки Сайт",
                    balance="12000.00",
                    document_ref="doc-old-spb",
                    document_number="РТУ-1",
                    document_date=datetime(2026, 5, 1, 10, 0),
                    credit_depth_days=7,
                    is_overdue=True,
                    overdue_days=21,
                ),
                _snapshot(
                    "cp-ok",
                    counterparty_name="Контрагент СПБ",
                    balance="9000.00",
                    document_ref="doc-spb-ok",
                    document_number="РТУ-2",
                    document_date=datetime(2026, 5, 10, 10, 0),
                    credit_depth_days=7,
                    is_overdue=True,
                    overdue_days=12,
                ),
                _snapshot(
                    "cp-fresh",
                    counterparty_name="Свежий долг Митино",
                    balance="8000.00",
                    document_ref="doc-mitino",
                    document_number="РТУ-3",
                    document_date=datetime(2026, 5, 27, 10, 0),
                    credit_depth_days=7,
                    is_overdue=False,
                    overdue_days=0,
                ),
                _snapshot(
                    "cp-review-folder",
                    counterparty_name="Нет папки подразделения",
                    balance="7000.00",
                    document_ref="doc-no-folder",
                    document_number="РТУ-4",
                    document_date=datetime(2026, 5, 2, 10, 0),
                    credit_depth_days=7,
                    is_overdue=True,
                    overdue_days=20,
                ),
                _snapshot(
                    "cp-review-document",
                    counterparty_name="Не найден документ",
                    balance="6000.00",
                    document_ref="doc-missing",
                    document_number="РТУ-5",
                    document_date=datetime(2026, 5, 3, 10, 0),
                    credit_depth_days=7,
                    is_overdue=True,
                    overdue_days=19,
                ),
            ]
        )
        session.commit()


def _seed_onec_engine():
    engine = _make_sqlite_engine()
    with engine.begin() as conn:
        conn.execute(text("""
                CREATE TABLE _Reference54 (
                    _IDRRef TEXT PRIMARY KEY,
                    _ParentIDRRef TEXT,
                    _Description TEXT
                )
                """))
        conn.execute(text("""
                CREATE TABLE _Reference68 (
                    _IDRRef TEXT PRIMARY KEY,
                    _Description TEXT,
                    _Fld8927RRef TEXT
                )
                """))
        conn.execute(text("""
                CREATE TABLE _Document203 (
                    _IDRRef TEXT PRIMARY KEY,
                    _Fld4937RRef TEXT
                )
                """))
        conn.execute(text("""
                INSERT INTO _Reference54 (_IDRRef, _ParentIDRRef, _Description)
                VALUES
                    ('folder-site', NULL, '08. Сайт'),
                    ('folder-spb', NULL, '02. СПБ'),
                    ('folder-mitino', NULL, '03. Митино'),
                    ('cp-site', 'folder-site', 'Контрагент из папки Сайт'),
                    ('cp-ok', 'folder-spb', 'Контрагент СПБ'),
                    ('cp-fresh', 'folder-mitino', 'Свежий долг Митино'),
                    ('cp-review-folder', 'folder-site', 'Нет папки подразделения'),
                    ('cp-review-document', 'folder-site', 'Не найден документ')
                """))
        conn.execute(text("""
                INSERT INTO _Reference68 (_IDRRef, _Description, _Fld8927RRef)
                VALUES
                    ('dept-spb', 'СПБ', 'folder-spb'),
                    ('dept-mitino', 'Митино', 'folder-mitino'),
                    ('dept-no-folder', 'Подразделение без папки', NULL)
                """))
        conn.execute(text("""
                INSERT INTO _Document203 (_IDRRef, _Fld4937RRef)
                VALUES
                    ('doc-old-spb', 'dept-spb'),
                    ('doc-spb-ok', 'dept-spb'),
                    ('doc-mitino', 'dept-mitino'),
                    ('doc-no-folder', 'dept-no-folder')
                """))
    return engine


def test_counterparty_folder_recommendations_builds_statuses(tmp_path) -> None:
    app_db_path = tmp_path / "app.db"
    app_engine = _make_sqlite_engine(str(app_db_path))
    onec_engine = _seed_onec_engine()
    _seed_app_db(app_engine)

    with Session(app_engine) as session:
        report = build_counterparty_folder_recommendations(
            session,
            onec_engine=onec_engine,
            snapshot_date=SNAPSHOT_DATE,
        )

    by_ref = {item["counterparty_ref"]: item for item in report["payload"]}
    assert by_ref["cp-site"]["status"] == STATUS_MOVE_RECOMMENDED
    assert by_ref["cp-site"]["current_folder_name"] == "08. Сайт"
    assert by_ref["cp-site"]["recommended_folder_name"] == "02. СПБ"
    assert by_ref["cp-site"]["debt_department_name"] == "СПБ"
    assert by_ref["cp-ok"]["status"] == STATUS_OK
    assert by_ref["cp-fresh"]["status"] == STATUS_NO_OVERDUE
    assert by_ref["cp-review-folder"]["status"] == STATUS_NEEDS_REVIEW
    assert by_ref["cp-review-folder"]["review_reason"] == "department_folder_missing"
    assert by_ref["cp-review-document"]["status"] == STATUS_NEEDS_REVIEW
    assert by_ref["cp-review-document"]["review_reason"] == "origin_document_not_found"
    assert report["summary"]["source_snapshot_count"] == 5
    assert report["summary"]["move_recommended_count"] == 1
    assert report["summary"]["ok_count"] == 1
    assert report["summary"]["no_overdue_count"] == 1
    assert report["summary"]["needs_review_count"] == 2

    app_engine.dispose()
    onec_engine.dispose()


def test_counterparty_folder_recommendations_can_filter_move_recommended(tmp_path) -> None:
    app_db_path = tmp_path / "app.db"
    app_engine = _make_sqlite_engine(str(app_db_path))
    onec_engine = _seed_onec_engine()
    _seed_app_db(app_engine)

    with Session(app_engine) as session:
        report = build_counterparty_folder_recommendations(
            session,
            onec_engine=onec_engine,
            snapshot_date=SNAPSHOT_DATE,
            status=STATUS_MOVE_RECOMMENDED,
            limit=1,
        )

    assert report["summary"]["source_snapshot_count"] == 5
    assert report["summary"]["total_count"] == 1
    assert report["payload"][0]["counterparty_ref"] == "cp-site"
    assert report["payload"][0]["status"] == STATUS_MOVE_RECOMMENDED

    app_engine.dispose()
    onec_engine.dispose()


def test_counterparty_folder_recommendations_api(monkeypatch, tmp_path) -> None:
    app_db_path = tmp_path / "app.db"
    app_engine = _make_sqlite_engine(str(app_db_path))
    onec_engine = _seed_onec_engine()
    _seed_app_db(app_engine)

    monkeypatch.setenv("MANAGEMENT_INTERNAL_API_TOKEN", "secret-token")
    get_settings.cache_clear()
    app.dependency_overrides = {get_db: _override_db(app_engine)}
    monkeypatch.setattr("app.api.management._build_onec_engine", lambda: onec_engine)
    client = TestClient(app)

    response = client.get(
        "/api/management/counterparty-folder-recommendations",
        params={"date": SNAPSHOT_DATE.isoformat(), "status": STATUS_MOVE_RECOMMENDED},
        headers={"Authorization": "Bearer secret-token"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["as_of"] == SNAPSHOT_DATE.isoformat()
    assert payload["source_status"] == "ready"
    assert payload["summary"]["source_snapshot_count"] == 5
    assert payload["summary"]["move_recommended_count"] == 1
    assert [item["counterparty_ref"] for item in payload["payload"]] == ["cp-site"]
    assert payload["payload"][0]["recommended_folder_name"] == "02. СПБ"

    app.dependency_overrides = {}
    get_settings.cache_clear()
    app_engine.dispose()
    onec_engine.dispose()
    if os.path.exists(app_db_path):
        os.remove(app_db_path)
