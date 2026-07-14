#!/usr/bin/env python3
from __future__ import annotations

import json
import re
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from app.core.config import get_settings
from app.infrastructure.contracts import ContractIntegrityError, read_json_contract
from app.infrastructure.db.engines import get_application_engine
from app.main import app
from app.services.executive_dashboard import (
    _resolve_cashflow_period_cache_path,
    _resolve_sales_plan_snapshot_path,
    _resolve_snapshot_path,
    _resolve_warehouse_snapshot_path,
)

REQUIRED_ROUTES = {
    ("GET", "/bitrix/executive-dashboard/"),
    ("POST", "/api/bitrix/executive-dashboard/session"),
    ("GET", "/api/management/executive-dashboard"),
    ("GET", "/api/management/executive-dashboard/actions"),
    ("GET", "/api/management/executive-dashboard/cashflow-period"),
    ("GET", "/api/management/executive-dashboard/profit-loss-period"),
    ("GET", "/api/management/executive-dashboard/sales-period"),
    ("GET", "/api/management/executive-dashboard/management-balance"),
    ("POST", "/api/management/executive-dashboard/management-balance/{month}/close"),
    ("GET", "/api/management/executive-dashboard/service-accruals"),
}
ASSET_RE = re.compile(r"(?:src|href)=[\"'](?:\./|/)?assets/([^\"']+)[\"']")


def main() -> None:
    root = Path.cwd().resolve()
    index_path = root / "ui" / "dist" / "index.html"
    errors: list[str] = []

    if not index_path.is_file():
        errors.append(f"release UI is missing: {index_path}")
        assets: list[str] = []
    else:
        index_html = index_path.read_text(encoding="utf-8")
        assets = sorted(set(ASSET_RE.findall(index_html)))
        if not assets:
            errors.append("release UI index does not reference any built assets")
        for asset in assets:
            asset_path = index_path.parent / "assets" / asset
            if not asset_path.is_file():
                errors.append(f"referenced UI asset is missing: {asset_path}")

    actual_routes = {
        (method, route.path)
        for route in app.routes
        for method in (getattr(route, "methods", None) or set())
    }
    missing_routes = sorted(REQUIRED_ROUTES - actual_routes)
    if missing_routes:
        errors.append(f"required routes are missing: {missing_routes}")

    settings = get_settings()
    if not settings.executive_dashboard_bitrix_enabled:
        errors.append("EXECUTIVE_DASHBOARD_BITRIX_ENABLED is false")
    if not settings.executive_dashboard_bitrix_session_secret:
        errors.append("EXECUTIVE_DASHBOARD_BITRIX_SESSION_SECRET is missing")
    for source_name, source_path in (
        ("finance snapshot", _resolve_snapshot_path()),
        ("cashflow cache", _resolve_cashflow_period_cache_path()),
        ("warehouse snapshot", _resolve_warehouse_snapshot_path()),
        ("frozen sales plan snapshot", _resolve_sales_plan_snapshot_path()),
        (
            "employee payroll balance snapshot",
            Path(settings.executive_management_balance_payroll_snapshot_path),
        ),
    ):
        if not source_path.is_file():
            errors.append(f"{source_name} is missing: {source_path}")
            continue
        try:
            payload = json.loads(source_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{source_name} is unreadable: {type(exc).__name__}: {exc}")
            continue
        if not isinstance(payload, dict):
            errors.append(f"{source_name} root is not an object: {source_path}")

    sales_plan_path = _resolve_sales_plan_snapshot_path()
    if sales_plan_path.is_file():
        try:
            sales_plan = read_json_contract(sales_plan_path)
        except (OSError, json.JSONDecodeError, ContractIntegrityError) as exc:
            errors.append(
                f"frozen sales plan contract integrity check failed: {type(exc).__name__}: {exc}"
            )
        else:
            if sales_plan.get("schema_version") != 1:
                errors.append("frozen sales plan schema_version must be 1")
            if not isinstance(sales_plan.get("months"), list):
                errors.append("frozen sales plan months must be an array")

    alembic_config = Config(str(root / "alembic.ini"))
    script = ScriptDirectory.from_config(alembic_config)
    code_head = script.get_current_head()
    database_head = None
    try:
        engine = get_application_engine()
        with engine.connect() as connection:
            database_head = MigrationContext.configure(connection).get_current_revision()
    except Exception as exc:  # pragma: no cover - operational diagnostic
        errors.append(f"database migration check failed: {type(exc).__name__}: {exc}")
    if database_head is not None and database_head != code_head:
        errors.append(f"database revision {database_head} does not match code head {code_head}")

    result = {
        "status": "ok" if not errors else "failed",
        "release_root": str(root),
        "asset_count": len(assets),
        "required_route_count": len(REQUIRED_ROUTES),
        "code_migration_head": code_head,
        "database_migration_head": database_head,
        "errors": errors,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
