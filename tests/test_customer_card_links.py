from __future__ import annotations

import hashlib
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api import receivable_workplace as workplace_api
from app.core.config import Settings
from app.main import app
from app.services.customer_card_links import (
    CustomerCardConflict,
    CustomerCardLink,
    CustomerCardNotFound,
    onec_reference_hash,
    onec_reference_hex_candidates,
    resolve_customer_card_link,
)


class FakeBitrix:
    def __init__(self, matches: dict[str, list[str]]) -> None:
        self.matches = matches
        self.calls: list[tuple[str, list[tuple[str, str]]]] = []

    def call(
        self,
        method: str,
        params: list[tuple[str, str]] | None = None,
        *,
        timeout: int = 60,
    ) -> dict[str, Any]:
        values = list(params or [])
        self.calls.append((method, values))
        ref_hash = next(value for key, value in values if key.startswith("filter["))
        return {"result": [{"ID": item} for item in self.matches.get(ref_hash, [])]}


def _settings() -> Settings:
    return Settings(
        database_url="sqlite://",
        receivable_bitrix_webhook_url="https://crm.example.test/rest/1/secret",
    )


def test_onec_reference_candidates_support_sql_hex_and_uuid_byte_order() -> None:
    assert onec_reference_hex_candidates("0x00112233445566778899AABBCCDDEEFF") == (
        "0x00112233445566778899aabbccddeeff",
    )
    assert onec_reference_hex_candidates("00112233-4455-6677-8899-aabbccddeeff") == (
        "0x00112233445566778899aabbccddeeff",
        "0x33221100554477668899aabbccddeeff",
    )


def test_onec_reference_hash_matches_crm_sync_v2_contract() -> None:
    ref_hex = "0x00112233445566778899aabbccddeeff"
    expected = hashlib.sha256(
        f"bitrix-crm-customer-audit-v1|onec-ref|{ref_hex}".encode()
    ).hexdigest()[:24]
    assert onec_reference_hash(ref_hex) == expected


def test_resolve_customer_card_link_returns_unique_company() -> None:
    guid = "00112233-4455-6677-8899-aabbccddeeff"
    direct_hash = onec_reference_hash("0x00112233445566778899aabbccddeeff")
    fake = FakeBitrix({direct_hash: ["731"]})

    result = resolve_customer_card_link(guid, settings=_settings(), client=fake)

    assert result.company_id == "731"
    assert result.url == "https://crm.example.test/crm/company/details/731/"
    assert all(method == "crm.company.list" for method, _params in fake.calls)


def test_resolve_customer_card_link_reports_missing_company() -> None:
    with pytest.raises(CustomerCardNotFound):
        resolve_customer_card_link(
            "00112233-4455-6677-8899-aabbccddeeff",
            settings=_settings(),
            client=FakeBitrix({}),
        )


def test_resolve_customer_card_link_blocks_duplicate_companies() -> None:
    guid = "00112233-4455-6677-8899-aabbccddeeff"
    candidates = onec_reference_hex_candidates(guid)
    fake = FakeBitrix(
        {
            onec_reference_hash(candidates[0]): ["731"],
            onec_reference_hash(candidates[1]): ["732"],
        }
    )
    with pytest.raises(CustomerCardConflict):
        resolve_customer_card_link(guid, settings=_settings(), client=fake)


def test_customer_card_route_redirects_to_universal_company(monkeypatch) -> None:
    monkeypatch.setattr(
        workplace_api,
        "resolve_customer_card_link",
        lambda _reference, *, settings: CustomerCardLink(
            company_id="731",
            url="https://crm.example.test/crm/company/details/731/",
        ),
    )

    response = TestClient(app).get(
        "/customer-card/00112233-4455-6677-8899-aabbccddeeff",
        follow_redirects=False,
    )

    assert response.status_code == 307
    assert response.headers["location"] == "https://crm.example.test/crm/company/details/731/"


def test_customer_card_route_maps_bitrix_failure_to_503(monkeypatch) -> None:
    def fail(_reference, *, settings):
        raise RuntimeError("webhook detail must not leak")

    monkeypatch.setattr(workplace_api, "resolve_customer_card_link", fail)

    response = TestClient(app).get(
        "/customer-card/00112233-4455-6677-8899-aabbccddeeff",
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "Bitrix временно недоступен"}
