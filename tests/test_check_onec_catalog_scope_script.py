from __future__ import annotations

import json
from contextlib import contextmanager

import pytest

from scripts import check_onec_catalog_scope


@pytest.mark.parametrize(
    ("active_articles", "expected_status", "expected_exit_code"),
    [
        (["A", "B"], "ok", 0),
        (["A", "B", "OUTSIDE"], "failed", 1),
    ],
)
def test_check_onec_catalog_scope_uses_central_read_only_session(
    active_articles: list[str],
    expected_status: str,
    expected_exit_code: int,
    monkeypatch,
    capsys,
) -> None:
    onec_engine = object()
    engine_calls: list[bool] = []
    scope_calls: list[bool] = []
    folder_calls: list[object] = []
    catalog_calls: list[tuple[object, object]] = []
    product_calls: list[tuple[object, object, list[bytes]]] = []
    scalar_calls: list[object] = []

    class FakeScalarResult:
        def all(self) -> list[str]:
            return active_articles

    class FakeSession:
        def scalars(self, statement: object) -> FakeScalarResult:
            scalar_calls.append(statement)
            return FakeScalarResult()

    session = FakeSession()

    @contextmanager
    def fake_session_scope(*, read_only: bool = False):
        scope_calls.append(read_only)
        yield session

    def fake_get_onec_engine() -> object:
        engine_calls.append(True)
        return onec_engine

    def fake_detect_item_folder_value(current_engine: object) -> bytes:
        folder_calls.append(current_engine)
        return b"folder"

    def fake_fetch_general_catalog_item_ids(
        current_engine: object,
        folder_value: object,
    ) -> set[bytes]:
        catalog_calls.append((current_engine, folder_value))
        return {b"item-b", b"item-a"}

    def fake_fetch_onec_products(
        current_engine: object,
        folder_value: object,
        allowed_ids: list[bytes],
    ) -> list[dict[str, str]]:
        product_calls.append((current_engine, folder_value, allowed_ids))
        return [
            {"article": "A", "name": "Phone"},
            {"article": "B", "name": "Watch"},
        ]

    monkeypatch.setattr(
        check_onec_catalog_scope,
        "get_onec_engine",
        fake_get_onec_engine,
    )
    monkeypatch.setattr(
        check_onec_catalog_scope,
        "session_scope",
        fake_session_scope,
    )
    monkeypatch.setattr(
        check_onec_catalog_scope,
        "detect_item_folder_value",
        fake_detect_item_folder_value,
    )
    monkeypatch.setattr(
        check_onec_catalog_scope,
        "fetch_general_catalog_item_ids",
        fake_fetch_general_catalog_item_ids,
    )
    monkeypatch.setattr(
        check_onec_catalog_scope,
        "fetch_onec_products",
        fake_fetch_onec_products,
    )
    monkeypatch.setattr(
        check_onec_catalog_scope,
        "_clean_str",
        lambda value: str(value or "").strip(),
    )
    monkeypatch.setattr(
        check_onec_catalog_scope,
        "has_duplicate_marker",
        lambda _: False,
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "check_onec_catalog_scope",
            "--baseline-source-count",
            "2",
            "--max-missing",
            "0",
            "--max-outside",
            "0",
            "--max-drop-percent",
            "0",
        ],
    )

    assert check_onec_catalog_scope.main() == expected_exit_code

    assert engine_calls == [True]
    assert folder_calls == [onec_engine]
    assert catalog_calls == [(onec_engine, b"folder")]
    assert product_calls == [(onec_engine, b"folder", [b"item-a", b"item-b"])]
    assert scope_calls == [True]
    assert len(scalar_calls) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == expected_status
    assert output["onec_catalog_scope"] == 2
    assert output["pricing_products_active"] == len(active_articles)
