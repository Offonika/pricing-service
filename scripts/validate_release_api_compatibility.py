#!/usr/bin/env python3
"""Block a release that removes production API operations or critical routes."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

HTTP_METHODS = {"delete", "get", "head", "options", "patch", "post", "put", "trace"}
DEFAULT_POLICY = PROJECT_ROOT / "config" / "production_required_routes.json"


def route_inventory_from_openapi(payload: dict[str, Any]) -> set[tuple[str, str]]:
    routes: set[tuple[str, str]] = set()
    for path, path_item in (payload.get("paths") or {}).items():
        if not isinstance(path_item, dict):
            continue
        for method in path_item:
            normalized_method = str(method).lower()
            if normalized_method in HTTP_METHODS:
                routes.add((normalized_method.upper(), str(path)))
    return routes


def load_openapi_routes(path: Path) -> set[tuple[str, str]]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"OpenAPI root is not an object: {path}")
    return route_inventory_from_openapi(payload)


def route_inventory_from_app(app: FastAPI) -> set[tuple[str, str]]:
    """Read the routes actually registered by the candidate application."""
    routes: set[tuple[str, str]] = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None) or set()
        if not path:
            continue
        for method in methods:
            normalized_method = str(method).lower()
            if normalized_method in HTTP_METHODS:
                routes.add((normalized_method.upper(), str(path)))
    return routes


def load_policy(path: Path) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    required = {
        (str(item["method"]).upper(), str(item["path"]))
        for item in payload.get("required_routes") or []
    }
    allowed_removed = {
        (str(item["method"]).upper(), str(item["path"]))
        for item in payload.get("allowed_removed_routes") or []
    }
    return required, allowed_removed


def validate_route_compatibility(
    *,
    candidate_routes: set[tuple[str, str]],
    baseline_routes: set[tuple[str, str]],
    required_routes: set[tuple[str, str]],
    allowed_removed_routes: set[tuple[str, str]],
) -> dict[str, Any]:
    removed = sorted(baseline_routes - candidate_routes - allowed_removed_routes)
    missing_required = sorted(required_routes - candidate_routes)
    return {
        "ok": not removed and not missing_required,
        "candidate_route_count": len(candidate_routes),
        "baseline_route_count": len(baseline_routes),
        "removed_routes": [f"{method} {path}" for method, path in removed],
        "missing_required_routes": [f"{method} {path}" for method, path in missing_required],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-dir", required=True, type=Path)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    args = parser.parse_args()

    baseline_openapi = args.baseline_dir.resolve() / "openapi.yaml"
    if not baseline_openapi.is_file():
        raise SystemExit(f"baseline OpenAPI is missing: {baseline_openapi}")

    from app.main import app

    candidate_routes = route_inventory_from_app(app)
    baseline_routes = load_openapi_routes(baseline_openapi)
    required_routes, allowed_removed_routes = load_policy(args.policy.resolve())
    report = validate_route_compatibility(
        candidate_routes=candidate_routes,
        baseline_routes=baseline_routes,
        required_routes=required_routes,
        allowed_removed_routes=allowed_removed_routes,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
