from __future__ import annotations

from fastapi import FastAPI

from scripts.validate_release_api_compatibility import (
    route_inventory_from_app,
    validate_route_compatibility,
)


def test_route_inventory_uses_routes_registered_by_application() -> None:
    app = FastAPI()

    @app.get("/actual")
    def actual_route() -> dict[str, bool]:
        return {"ok": True}

    assert ("GET", "/actual") in route_inventory_from_app(app)


def test_release_route_compatibility_rejects_removed_operation() -> None:
    report = validate_route_compatibility(
        candidate_routes={("GET", "/health")},
        baseline_routes={
            ("GET", "/health"),
            ("GET", "/api/management/retail-counterparty-zero-balances"),
        },
        required_routes=set(),
        allowed_removed_routes=set(),
    )

    assert report["ok"] is False
    assert report["removed_routes"] == ["GET /api/management/retail-counterparty-zero-balances"]


def test_release_route_compatibility_enforces_critical_route_without_baseline() -> None:
    report = validate_route_compatibility(
        candidate_routes={("GET", "/health")},
        baseline_routes={("GET", "/health")},
        required_routes={("GET", "/api/management/retail-counterparty-zero-balances")},
        allowed_removed_routes=set(),
    )

    assert report["ok"] is False
    assert report["missing_required_routes"] == [
        "GET /api/management/retail-counterparty-zero-balances"
    ]


def test_release_route_compatibility_allows_explicit_removal() -> None:
    report = validate_route_compatibility(
        candidate_routes={("GET", "/health")},
        baseline_routes={("GET", "/health"), ("GET", "/legacy")},
        required_routes=set(),
        allowed_removed_routes={("GET", "/legacy")},
    )

    assert report["ok"] is True
