from __future__ import annotations

import json
from collections.abc import Generator
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api import bitrix_matching as bitrix_matching_api
from app.api import matching as matching_api
from app.api.dependencies import get_db
from app.core.config import Settings
from app.main import app
from app.models import (
    CompatibilityMappingDecision,
    CompetitorItem,
    CompetitorItemMatch,
    CompetitorItemUrlAlias,
    DeviceBrandAlias,
    PhoneModel,
    Product,
    ProductCompatibility,
    ProductLiveCandidateCache,
    ProductPhoneModel,
)
from app.models.competitor_item_compatibility import CompetitorItemCompatibility
from app.models.competitor_item_match import (
    CompetitorItemMatchMethod,
    CompetitorItemMatchStatus,
)
from app.services import bitrix_matching_auth
from tasks.refresh_live_candidate_cache import refresh_live_candidate_cache


@pytest.fixture()
def matching_client(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> Generator[TestClient, None, None]:
    monkeypatch.setattr(matching_api.settings, "api_basic_user", "api")
    monkeypatch.setattr(matching_api.settings, "api_basic_password", "secret")

    def override_db() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def _auth() -> tuple[str, str]:
    return ("api", "secret")


def _bitrix_settings() -> Settings:
    return Settings(
        api_basic_user="api",
        api_basic_password="secret",
        matching_bitrix_enabled=True,
        matching_bitrix_allowed_domains=["crm.master-mobile.ru"],
        matching_bitrix_allowed_member_ids=["member-1"],
        matching_bitrix_allowed_user_ids=["42"],
        matching_bitrix_session_secret="test-matching-session-secret",
        matching_bitrix_session_ttl_seconds=3600,
    )


def test_bitrix_matching_page_inlines_built_assets(
    matching_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    assets_dir = tmp_path / "assets"
    assets_dir.mkdir()
    (assets_dir / "index-test.js").write_text(
        'console.log("matching loaded");',
        encoding="utf-8",
    )
    (assets_dir / "index-test.css").write_text(
        ".matching-root{display:block}",
        encoding="utf-8",
    )
    index_path = tmp_path / "index.html"
    index_path.write_text(
        "\n".join(
            [
                "<!doctype html>",
                "<html><head>",
                '<script type="module" crossorigin src="./assets/index-test.js"></script>',
                '<link rel="stylesheet" crossorigin href="./assets/index-test.css">',
                '</head><body><div id="root"></div></body></html>',
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(bitrix_matching_api, "_INDEX_PATHS", (index_path,))

    response = matching_client.get("/bitrix/matching/")

    assert response.status_code == 200
    assert 'src="./assets/' not in response.text
    assert 'href="./assets/' not in response.text
    assert '<script type="module">console.log("matching loaded");</script>' in response.text
    assert "<style>.matching-root{display:block}</style>" in response.text
    assert "window.__MM_BITRIX_LAUNCH__" in response.text


class _FakeBitrixResponse:
    def __enter__(self) -> _FakeBitrixResponse:
        return self

    def __exit__(self, *args) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps({"result": {"ID": "42", "NAME": "Иван", "LAST_NAME": "Петров"}}).encode()


def _seed(db: Session) -> dict[str, object]:
    p1 = Product(
        article="P-001",
        name="Дисплей для iPhone 11",
        brand="Apple",
        category="Дисплеи",
        subject="Дисплеи для телефонов",
    )
    p2 = Product(
        article="P-002",
        name="Аккумулятор для Samsung A50",
        brand="Samsung",
        category="АКБ",
        subject="Аккумуляторы для телефонов",
    )
    item1 = CompetitorItem(
        competitor="moba",
        external_id="LCD-IPH11-BLK",
        name="Дисплей iPhone 11 черный",
        item_type="display",
        category_group="display",
        item_brand="Apple",
        attrs_model="iPhone 11",
        attrs_quality="Copy",
        attrs_color="черный",
        price_roz=2500,
        availability=True,
    )
    item2 = CompetitorItem(
        competitor="liberti",
        external_id="BAT-SAM-A50",
        name="АКБ Samsung A50",
        item_type="battery",
        category_group="battery",
        item_brand="Samsung",
        attrs_model="A50",
        price_roz=900,
        availability=True,
    )
    item3 = CompetitorItem(
        competitor="moba",
        external_id="LCD-IPH11-WHT",
        name="Дисплей iPhone 11 белый",
        item_type="display",
        category_group="display",
        item_brand="Apple",
        attrs_model="iPhone 11",
        attrs_quality="Copy",
        attrs_color="белый",
        price_roz=2550,
        availability=True,
    )
    db.add_all([p1, p2, item1, item2, item3])
    db.flush()
    apple_model = PhoneModel(brand="apple", model_name="iphone 11")
    samsung_model = PhoneModel(brand="samsung", model_name="galaxy a50")
    db.add_all([apple_model, samsung_model])
    db.flush()
    db.add_all(
        [
            ProductPhoneModel(
                product_id=p1.id,
                phone_model_id=apple_model.id,
                source="test",
                raw_value="Apple iPhone 11",
            ),
            ProductPhoneModel(
                product_id=p2.id,
                phone_model_id=samsung_model.id,
                source="test",
                raw_value="Samsung Galaxy A50",
            ),
        ]
    )
    db.add_all(
        [
            CompetitorItemMatch(
                competitor_item_id=item1.id,
                product_id=p1.id,
                status=CompetitorItemMatchStatus.SUGGESTED,
                method=CompetitorItemMatchMethod.EMBEDDING_AUTO,
                final_score=0.88,
            ),
            CompetitorItemMatch(
                competitor_item_id=item2.id,
                product_id=p2.id,
                status=CompetitorItemMatchStatus.ACCEPTED,
                method=CompetitorItemMatchMethod.MANUAL,
                final_score=1.0,
            ),
        ]
    )
    db.commit()
    return {"p1": p1, "p2": p2, "item1": item1, "item2": item2, "item3": item3}


def test_bitrix_session_endpoint_issues_matching_token(
    matching_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _bitrix_settings()
    monkeypatch.setattr(bitrix_matching_api, "get_settings", lambda: settings)

    def fake_urlopen(request, timeout):  # noqa: ANN001, ANN202
        assert request.full_url == "https://crm.master-mobile.ru/rest/user.current.json"
        assert timeout == settings.matching_bitrix_rest_timeout_seconds
        assert json.loads(request.data.decode()) == {"auth": "bitrix-access-token"}
        return _FakeBitrixResponse()

    monkeypatch.setattr(bitrix_matching_auth.urllib.request, "urlopen", fake_urlopen)

    response = matching_client.post(
        "/api/bitrix/matching/session",
        json={
            "access_token": "bitrix-access-token",
            "domain": "crm.master-mobile.ru",
            "member_id": "member-1",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["session_token"]
    assert body["user"] == {"user_id": "42", "name": "Иван Петров"}


def test_bitrix_session_endpoint_allows_any_user_when_user_allowlist_is_empty(
    matching_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _bitrix_settings()
    settings.matching_bitrix_allowed_user_ids = []
    monkeypatch.setattr(bitrix_matching_api, "get_settings", lambda: settings)

    def fake_urlopen(request, timeout):  # noqa: ANN001, ANN202
        return _FakeBitrixResponse()

    monkeypatch.setattr(bitrix_matching_auth.urllib.request, "urlopen", fake_urlopen)

    response = matching_client.post(
        "/api/bitrix/matching/session",
        json={
            "access_token": "bitrix-access-token",
            "domain": "crm.master-mobile.ru",
            "member_id": "member-1",
        },
    )

    assert response.status_code == 200


def test_bitrix_session_endpoint_rejects_unknown_domain(
    matching_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _bitrix_settings()
    monkeypatch.setattr(bitrix_matching_api, "get_settings", lambda: settings)

    response = matching_client.post(
        "/api/bitrix/matching/session",
        json={
            "access_token": "bitrix-access-token",
            "domain": "other.example",
            "member_id": "member-1",
        },
    )

    assert response.status_code == 403


def test_matching_api_accepts_bitrix_bearer_token(
    matching_client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _bitrix_settings()
    monkeypatch.setattr(matching_api, "settings", settings)
    _seed(db_session)
    token, _ = bitrix_matching_auth.create_matching_session_token(
        domain="crm.master-mobile.ru",
        member_id="member-1",
        user_id="42",
        settings=settings,
    )

    response = matching_client.get(
        "/api/matching/products",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["total"] == 2


def test_product_status_filters_distinguish_candidates_and_matched(
    matching_client: TestClient, db_session: Session
) -> None:
    seeded = _seed(db_session)

    candidates = matching_client.get(
        "/api/matching/products", params={"status": "candidates"}, auth=_auth()
    )
    assert candidates.status_code == 200
    assert candidates.json()["total"] == 1
    assert candidates.json()["items"][0]["id"] == seeded["p1"].id
    assert candidates.json()["items"][0]["status"] == "candidates"

    matched = matching_client.get(
        "/api/matching/products", params={"status": "matched"}, auth=_auth()
    )
    assert matched.status_code == 200
    assert matched.json()["total"] == 1
    assert matched.json()["items"][0]["id"] == seeded["p2"].id
    assert matched.json()["items"][0]["status"] == "manual"


def test_product_with_accepted_and_remaining_candidate_stays_in_candidate_queue(
    matching_client: TestClient, db_session: Session
) -> None:
    product = Product(
        article="P-MULTI-COMP",
        name="Дисплей для Google Pixel 8",
        brand="Google",
        category="Дисплеи",
        subject="Дисплеи для телефонов",
    )
    accepted_item = CompetitorItem(
        competitor="moba",
        external_id="LCD-GGL-PXL8-MOBA",
        name="Дисплей Google Pixel 8",
        item_type="display",
        category_group="display",
        item_brand="Google",
        availability=True,
    )
    pending_item = CompetitorItem(
        competitor="liberti",
        external_id="470001",
        name="LCD дисплей Google Pixel 8",
        item_type="display",
        category_group="display",
        item_brand="Google",
        availability=True,
    )
    db_session.add_all([product, accepted_item, pending_item])
    db_session.flush()
    db_session.add_all(
        [
            CompetitorItemMatch(
                competitor_item_id=accepted_item.id,
                product_id=product.id,
                status=CompetitorItemMatchStatus.ACCEPTED,
                method=CompetitorItemMatchMethod.MANUAL,
                final_score=1.0,
            ),
            CompetitorItemMatch(
                competitor_item_id=pending_item.id,
                product_id=product.id,
                status=CompetitorItemMatchStatus.SUGGESTED,
                method=CompetitorItemMatchMethod.EMBEDDING_AUTO,
                final_score=0.9,
            ),
        ]
    )
    db_session.commit()

    candidates = matching_client.get(
        "/api/matching/products", params={"status": "candidates"}, auth=_auth()
    )
    assert candidates.status_code == 200
    assert candidates.json()["total"] == 1
    candidate_row = candidates.json()["items"][0]
    assert candidate_row["id"] == product.id
    assert candidate_row["status"] == "candidates"
    assert candidate_row["accepted_count"] == 1
    assert candidate_row["suggested_count"] == 1

    matched = matching_client.get(
        "/api/matching/products", params={"status": "matched"}, auth=_auth()
    )
    assert matched.status_code == 200
    assert matched.json()["total"] == 1
    assert matched.json()["items"][0]["id"] == product.id


def test_product_subject_filter_and_facets(
    matching_client: TestClient, db_session: Session
) -> None:
    seeded = _seed(db_session)
    wrong_category = Product(
        article="P-WRONG-CATEGORY",
        name="Аккумулятор для Vivo V29 (V2250) (B-Z7) (Premium)",
        brand="Vivo",
        category="Дисплеи",
        subject="аккумулятор",
        subject_1c="аккумулятор",
    )
    wrong_device_group = Product(
        article="P-WRONG-DEVICE",
        name="Дисплей для Apple iPad Pro 10.5 (2017) + тачскрин (белый) (ORIG)",
        brand="Apple",
        category="Дисплеи для телефонов",
        subject="дисплей",
        subject_1c="дисплей",
    )
    phone_display = Product(
        article="P-PHONE-DISPLAY",
        name="Дисплей для Apple iPhone 12 + тачскрин (черный)",
        brand="Apple",
        category="Дисплеи для телефонов",
        subject="дисплей",
        subject_1c="дисплей",
    )
    db_session.add_all([wrong_category, wrong_device_group, phone_display])
    db_session.commit()

    response = matching_client.get(
        "/api/matching/products",
        params={"subject": "Дисплеи для телефонов"},
        auth=_auth(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == seeded["p1"].id
    assert body["items"][0]["subject"] == "Дисплеи для телефонов"
    assert {item["value"] for item in body["facets"]["subjects"]} >= {
        "Дисплеи для телефонов",
        "Аккумуляторы для телефонов",
    }
    assert {item["value"] for item in body["facets"]["categories"]} >= {"Дисплеи", "АКБ"}
    category_counts = {item["value"]: item["count"] for item in body["facets"]["categories"]}
    assert category_counts["Дисплеи"] == 1
    assert category_counts["Дисплеи для телефонов"] == 1
    assert {item["value"] for item in body["facets"]["compatibility_brands"]} >= {
        "apple",
        "samsung",
    }

    category_response = matching_client.get(
        "/api/matching/products",
        params={"category": "АКБ"},
        auth=_auth(),
    )
    assert category_response.status_code == 200
    category_body = category_response.json()
    assert category_body["total"] == 1
    assert category_body["items"][0]["id"] == seeded["p2"].id
    assert category_body["items"][0]["category"] == "АКБ"

    display_category_response = matching_client.get(
        "/api/matching/products",
        params={"category": "Дисплеи"},
        auth=_auth(),
    )
    assert display_category_response.status_code == 200
    display_category_body = display_category_response.json()
    assert display_category_body["total"] == 1
    assert display_category_body["items"][0]["id"] == seeded["p1"].id

    phone_display_response = matching_client.get(
        "/api/matching/products",
        params={"category": "Дисплеи для телефонов"},
        auth=_auth(),
    )
    assert phone_display_response.status_code == 200
    phone_display_body = phone_display_response.json()
    assert phone_display_body["total"] == 1
    assert phone_display_body["items"][0]["id"] == phone_display.id

    compatibility_response = matching_client.get(
        "/api/matching/products",
        params={"compatibility_brand": "samsung"},
        auth=_auth(),
    )
    assert compatibility_response.status_code == 200
    compatibility_body = compatibility_response.json()
    assert compatibility_body["total"] == 1
    assert compatibility_body["items"][0]["id"] == seeded["p2"].id


def test_products_list_can_sort_by_name_asc(
    matching_client: TestClient, db_session: Session
) -> None:
    seeded = _seed(db_session)

    response = matching_client.get(
        "/api/matching/products",
        params={"sort": "name_asc"},
        auth=_auth(),
    )

    assert response.status_code == 200
    body = response.json()
    assert [item["id"] for item in body["items"]] == [seeded["p2"].id, seeded["p1"].id]


def test_products_list_includes_candidate_previews(
    matching_client: TestClient, db_session: Session
) -> None:
    seeded = _seed(db_session)

    response = matching_client.get(
        "/api/matching/products",
        params={"search": seeded["p1"].article},
        auth=_auth(),
    )

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["candidate_previews"][0]["competitor_item_id"] == seeded["item1"].id
    assert item["candidate_previews"][0]["status"] == "suggested"


def test_products_list_includes_live_candidate_count_without_saved_match(
    matching_client: TestClient, db_session: Session
) -> None:
    product = Product(
        article="P-IPH12",
        name="Дисплей для Apple iPhone 12",
        brand="Apple",
        category="Дисплеи для телефонов",
        subject="дисплей",
    )
    locked_product = Product(
        article="P-LOCKED",
        name="Дисплей для Apple iPhone 12",
        brand="Apple",
        category="Дисплеи для телефонов",
        subject="дисплей",
    )
    live_item = CompetitorItem(
        competitor="moba",
        external_id="LCD-PMI-12-B",
        name="Дисплей для iPhone 12 в сборе с тачскрином Черный",
        item_type="display",
        category_group="display",
        item_brand="Apple",
        parsed_device_model="iphone 12",
        price_roz=5100,
        availability=True,
    )
    locked_item = CompetitorItem(
        competitor="liberti",
        external_id="LCD-LOCKED-12",
        name="LCD дисплей для iPhone 12 locked",
        item_type="display",
        category_group="display",
        item_brand="Apple",
        parsed_device_model="iphone 12",
        price_roz=5200,
        availability=True,
    )
    db_session.add_all([product, locked_product, live_item, locked_item])
    db_session.flush()
    db_session.add(
        CompetitorItemMatch(
            competitor_item_id=locked_item.id,
            product_id=locked_product.id,
            status=CompetitorItemMatchStatus.ACCEPTED,
            method=CompetitorItemMatchMethod.MANUAL,
        )
    )
    db_session.commit()

    response = matching_client.get(
        "/api/matching/products",
        params={"search": product.article},
        auth=_auth(),
    )

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["status"] == "live_candidates"
    assert item["candidate_previews"] == []
    assert item["live_candidate_count"] == 1

    fast_response = matching_client.get(
        "/api/matching/products",
        params={"search": product.article, "include_live_counts": "false"},
        auth=_auth(),
    )
    assert fast_response.status_code == 200
    fast_item = fast_response.json()["items"][0]
    assert fast_item["status"] == "none"
    assert fast_item["live_candidate_count"] == 0

    live_response = matching_client.get(
        "/api/matching/products",
        params={
            "search": product.article,
            "status": "live_candidates",
            "include_live_counts": "false",
        },
        auth=_auth(),
    )
    assert live_response.status_code == 200
    assert live_response.json()["total"] == 0

    exact_live_response = matching_client.get(
        "/api/matching/products",
        params={
            "search": product.article,
            "status": "live_candidates",
            "include_live_counts": "true",
        },
        auth=_auth(),
    )
    assert exact_live_response.status_code == 200
    assert exact_live_response.json()["total"] == 1
    assert exact_live_response.json()["items"][0]["id"] == product.id

    none_response = matching_client.get(
        "/api/matching/products",
        params={"search": product.article, "status": "none"},
        auth=_auth(),
    )
    assert none_response.status_code == 200
    assert none_response.json()["total"] == 0


def test_live_candidate_count_and_search_apply_device_guardrails(
    matching_client: TestClient, db_session: Session
) -> None:
    product = Product(
        article="TAB-P11-FLEX",
        name="Шлейф межплатный для Lenovo Tab P11",
        brand="Lenovo",
        category="Шлейфы",
        subject="Шлейфы для планшетов",
    )
    good_item = CompetitorItem(
        competitor="moba",
        external_id="TAB-P11-FLEX-1",
        name="Шлейф межплатный для Lenovo Tab P11",
        item_type="flex",
        category_group="flex",
        availability=True,
    )
    bad_item = CompetitorItem(
        competitor="liberti",
        external_id="LAPTOP-P11-FLEX",
        name="Шлейф матрицы для ноутбука Lenovo Tab P11",
        item_type="flex",
        category_group="flex",
        availability=True,
    )
    db_session.add_all([product, good_item, bad_item])
    db_session.commit()

    products = matching_client.get(
        "/api/matching/products",
        params={"search": product.article},
        auth=_auth(),
    )
    assert products.status_code == 200
    row = products.json()["items"][0]
    assert row["live_candidate_count"] == 1
    assert row["status"] == "live_candidates"

    search = matching_client.get(
        f"/api/matching/products/{product.id}/candidate-search",
        params={"q": "Lenovo Tab P11", "item_type": "flex"},
        auth=_auth(),
    )
    assert search.status_code == 200
    skus = {candidate["sku"] for candidate in search.json()["items"]}
    assert "TAB-P11-FLEX-1" in skus
    assert "LAPTOP-P11-FLEX" not in skus
    by_sku = {candidate["sku"]: candidate for candidate in search.json()["items"]}
    assert by_sku["TAB-P11-FLEX-1"]["needs_compat_review"] is True
    assert by_sku["TAB-P11-FLEX-1"]["compatibility_hint"]["status"] == "required"


def test_candidate_search_does_not_require_display_quality_words(
    matching_client: TestClient, db_session: Session
) -> None:
    product = Product(
        article="058366",
        name="Дисплей для Apple iPhone Xs Max + тачскрин (черный) (GX) (Hard Oled)",
        category="Дисплеи для телефонов",
        subject="дисплей",
    )
    item = CompetitorItem(
        competitor="moba",
        external_id="LCD-PMIMSX-CP-B-GX",
        name="Дисплей для iPhone Xs Max в сборе Черный GX",
        item_type="display",
        category_group="display",
        item_brand="iPhone",
        parsed_device_brand="apple",
        parsed_device_model="iphone xs max",
        attrs_model="Xs Max",
        availability=False,
    )
    db_session.add_all([product, item])
    db_session.flush()
    db_session.add(
        CompetitorItemCompatibility(
            competitor_item_id=item.id,
            device_brand="apple",
            device_model="iphone xs max",
            source="parser",
        )
    )
    db_session.commit()

    response = matching_client.get(
        f"/api/matching/products/{product.id}/candidate-search",
        auth=_auth(),
    )

    assert response.status_code == 200
    candidates = {candidate["sku"]: candidate for candidate in response.json()["items"]}
    assert "LCD-PMIMSX-CP-B-GX" in candidates
    assert candidates["LCD-PMIMSX-CP-B-GX"]["needs_compat_review"] is False
    assert candidates["LCD-PMIMSX-CP-B-GX"]["compatibility_hint"]["status"] == "existing"
    assert "apple iphone xs max" in [
        value.lower()
        for value in candidates["LCD-PMIMSX-CP-B-GX"]["compatibility_hint"]["matched_values"]
    ]


def test_candidate_search_treats_slash_compatibility_models_as_alternatives(
    matching_client: TestClient, db_session: Session
) -> None:
    product = Product(
        article="051578",
        name=(
            "Шлейф для Tecno Spark 7 (KF6N) / Infinix Hot 10S (X689D) / "
            "Hot 11 (X689F) и др. с комп. (на кнопку включения и кнопки громкости)"
        ),
        category="Шлейфы для телефонов",
        subject="шлейф",
    )
    matching_item = CompetitorItem(
        competitor="moba",
        external_id="FPC-TCN-SPR-7-VOL",
        name="Шлейф для Tecno Spark 7 (KF6N) на кнопки громкости/включения",
        item_type="flex",
        category_group="flex",
        availability=True,
    )
    other_item = CompetitorItem(
        competitor="moba",
        external_id="FPC-TCN-CMN-7-VOL",
        name="Шлейф для Tecno Camon 7 на кнопки громкости/включения",
        item_type="flex",
        category_group="flex",
        availability=True,
    )
    db_session.add_all([product, matching_item, other_item])
    db_session.commit()

    response = matching_client.get(
        f"/api/matching/products/{product.id}/candidate-search",
        auth=_auth(),
    )

    assert response.status_code == 200
    skus = {candidate["sku"] for candidate in response.json()["items"]}
    assert "FPC-TCN-SPR-7-VOL" in skus
    assert "FPC-TCN-CMN-7-VOL" not in skus


def test_candidate_search_allows_untyped_display_and_blocks_oneplus_nord_ce5(
    matching_client: TestClient, db_session: Session
) -> None:
    product = Product(
        article="039882",
        name="Дисплей для OnePlus 5 + тачскрин (черный) (OLED)",
        category="Дисплеи для телефонов",
        subject="дисплей",
    )
    moba_item = CompetitorItem(
        competitor="moba",
        external_id="LCD-OPL-5-CP-B-OR",
        name="Дисплей для OnePlus 5 в сборе с тачскрином Черный - OR",
        url="https://moba.ru/catalog/displei/7779/",
        item_type=None,
        category_group=None,
        availability=True,
    )
    nord_item = CompetitorItem(
        competitor="liberti",
        external_id="472505",
        name="LCD дисплей для OnePlus Nord CE5 в сборе с тачскрином (черный)OR 100%",
        item_type="display",
        category_group="display",
        parsed_device_brand="oneplus",
        parsed_device_model="5",
        attrs_model="5",
        availability=True,
    )
    five_t_item = CompetitorItem(
        competitor="moba",
        external_id="LCD-OPL-5T-CP-B-LED",
        name="Дисплей для OnePlus 5T (A5010) в сборе с тачскрином Черный - (OLED)",
        item_type="display",
        category_group="display",
        parsed_device_brand="oneplus",
        parsed_device_model="5",
        parsed_device_variant="t",
        attrs_model="5T",
        availability=True,
    )
    db_session.add_all([product, moba_item, nord_item, five_t_item])
    db_session.commit()

    response = matching_client.get(
        f"/api/matching/products/{product.id}/candidate-search",
        auth=_auth(),
    )

    assert response.status_code == 200
    skus = {candidate["sku"] for candidate in response.json()["items"]}
    assert "LCD-OPL-5-CP-B-OR" in skus
    assert "472505" not in skus
    assert "LCD-OPL-5T-CP-B-LED" not in skus


def test_candidate_search_full_query_ignores_display_service_words(
    matching_client: TestClient, db_session: Session
) -> None:
    product = Product(
        article="062490",
        name="Дисплей для OPPO A78 4G (CPH2565) + тачскрин (черный) (In-Cell)",
        category="Дисплеи для телефонов",
        subject="дисплей",
    )
    moba_item = CompetitorItem(
        competitor="moba",
        external_id="LCD-OPP-A78-4G-CP-B-IC",
        name="Дисплей для OPPO A78 4G (CPH2565) Черный - In-Cell",
        url="https://poiskzip.ru/redirect/1134761",
        item_type=None,
        category_group=None,
        availability=True,
    )
    wrong_item = CompetitorItem(
        competitor="liberti",
        external_id="460996",
        name="LCD дисплей для Oppo A18/A38 (CPH2591/CPH2579) с тачскрином (черный) 100% OR",
        item_type="display",
        category_group="display",
        availability=True,
    )
    flex_item = CompetitorItem(
        competitor="moba",
        external_id="FPC-OPP-A78-4G-VOL",
        name="Шлейф для OPPO A78 4G (CPH2565) на кнопку включения и кнопки громкости",
        item_type="flex",
        category_group="flex",
        availability=True,
    )
    db_session.add_all([product, moba_item, wrong_item, flex_item])
    db_session.flush()
    db_session.add(
        CompetitorItemUrlAlias(
            competitor_item_id=moba_item.id,
            competitor="moba",
            alias_url="https://moba.ru/catalog/displei/101185/?utm_referrer=poiskzip.ru",
            normalized_url="https://moba.ru/catalog/displei/101185",
            url_kind="resolved",
            catalog_id="101185",
            redirect_id="1134761",
            resolved_from_url="https://poiskzip.ru/redirect/1134761",
        )
    )
    db_session.commit()

    response = matching_client.get(
        f"/api/matching/products/{product.id}/candidate-search",
        params={"q": "дисплей для OPPO A78 4G (CPH2565) в сборе с тачскрином Черный - (In-Cell)"},
        auth=_auth(),
    )

    assert response.status_code == 200
    skus = {candidate["sku"] for candidate in response.json()["items"]}
    assert "LCD-OPP-A78-4G-CP-B-IC" in skus
    assert "460996" not in skus

    url_response = matching_client.get(
        f"/api/matching/products/{product.id}/candidate-search",
        params={"q": "https://moba.ru/catalog/displei/101185/"},
        auth=_auth(),
    )

    assert url_response.status_code == 200
    url_skus = {candidate["sku"] for candidate in url_response.json()["items"]}
    assert "LCD-OPP-A78-4G-CP-B-IC" in url_skus
    assert "FPC-OPP-A78-4G-VOL" not in url_skus

    global_url_response = matching_client.get(
        "/api/matching/candidates",
        params={"q": "https://moba.ru/catalog/displei/101185/"},
        auth=_auth(),
    )

    assert global_url_response.status_code == 200
    assert global_url_response.json()["items"][0]["sku"] == "LCD-OPP-A78-4G-CP-B-IC"


def test_candidate_search_precise_sku_bypasses_inferred_item_type_filter(
    matching_client: TestClient, db_session: Session
) -> None:
    product = Product(
        article="P-DISPLAY-STRICT",
        name="Дисплей для Xiaomi Redmi Note 12S (черный)",
        category="Дисплеи для телефонов",
        subject="дисплей",
    )
    item = CompetitorItem(
        competitor="moba",
        external_id="LCD-XMI-RMN12S-B-OR",
        name="Дисплей для Xiaomi Redmi Note 12S Черный - OR",
        item_type="screen",
        category_group="display",
        availability=True,
    )
    db_session.add_all([product, item])
    db_session.commit()

    response = matching_client.get(
        f"/api/matching/products/{product.id}/candidate-search",
        params={"q": "LCD-XMI-RMN12S-B-OR"},
        auth=_auth(),
    )

    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["sku"] == "LCD-XMI-RMN12S-B-OR"


def test_candidate_search_allows_sim_holder_when_device_code_matches(
    matching_client: TestClient, db_session: Session
) -> None:
    huawei_product = Product(
        article="063512",
        name="Держатель сим-карты для Huawei Nova Y71 (MGA-LX9N) (черный)",
        category="Держатели сим-карт для телефонов",
        subject="держатель сим-карты",
    )
    huawei_item = CompetitorItem(
        competitor="moba",
        external_id="HLD-SIM-HUW-NVA-Y70-B",
        name="Держатель SIM для Huawei Nova Y70/Y70 Plus/Y71 (MGA-LX9N) Черный",
        item_type="other",
        category_group="аксессуары",
        availability=True,
        first_seen_at=datetime(2026, 5, 2, tzinfo=timezone.utc),
    )
    oppo_product = Product(
        article="066991",
        name="Держатель сим-карты для OPPO A3x (CPH2641) (голубой)",
        category="Держатели сим-карт для телефонов",
        subject="держатель сим-карты",
    )
    oppo_item = CompetitorItem(
        competitor="moba",
        external_id="HLD-SIM-OPP-A3X-4G-LHT-BLU",
        name="Держатель SIM для OPPO A3x 4G (CPH2641) Голубой",
        item_type="other",
        category_group="аксессуары",
        availability=True,
        first_seen_at=datetime(2026, 5, 2, tzinfo=timezone.utc),
    )
    db_session.add_all([huawei_product, huawei_item, oppo_product, oppo_item])
    db_session.commit()

    for product, item in (
        (huawei_product, huawei_item),
        (oppo_product, oppo_item),
    ):
        response = matching_client.get(
            f"/api/matching/products/{product.id}/candidate-search",
            params={"q": item.external_id},
            auth=_auth(),
        )

        assert response.status_code == 200
        candidates = {candidate["sku"]: candidate for candidate in response.json()["items"]}
        assert item.external_id in candidates


def test_products_list_uses_cached_live_candidate_count(
    matching_client: TestClient, db_session: Session
) -> None:
    product = Product(
        article="P-CACHED-LIVE",
        name="Дисплей для Apple iPhone 15",
        brand="Apple",
        category="Дисплеи для телефонов",
        subject="дисплей",
    )
    db_session.add(product)
    db_session.flush()
    db_session.add(ProductLiveCandidateCache(product_id=product.id, live_candidate_count=2))
    db_session.commit()

    response = matching_client.get(
        "/api/matching/products",
        params={"search": product.article, "include_live_counts": "false"},
        auth=_auth(),
    )
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["status"] == "live_candidates"
    assert item["live_candidate_count"] == 2

    live_response = matching_client.get(
        "/api/matching/products",
        params={
            "search": product.article,
            "status": "live_candidates",
            "include_live_counts": "false",
        },
        auth=_auth(),
    )
    assert live_response.status_code == 200
    assert live_response.json()["total"] == 1
    assert live_response.json()["items"][0]["id"] == product.id


def test_refresh_live_candidate_cache_creates_counts(db_session: Session) -> None:
    product = Product(
        article="P-REFRESH-LIVE",
        name="Дисплей для Apple iPhone 14",
        brand="Apple",
        category="Дисплеи для телефонов",
        subject="дисплей",
    )
    item = CompetitorItem(
        competitor="moba",
        external_id="LCD-PMI-14-B",
        name="Дисплей для iPhone 14 в сборе с тачскрином Черный",
        item_type="display",
        category_group="display",
        item_brand="Apple",
        parsed_device_model="iphone 14",
        availability=True,
    )
    db_session.add_all([product, item])
    db_session.commit()

    report = refresh_live_candidate_cache(db_session, product_ids=[product.id])

    cache = db_session.query(ProductLiveCandidateCache).filter_by(product_id=product.id).one()
    assert report["processed"] == 1
    assert report["with_live_candidates"] == 1
    assert cache.live_candidate_count == 1


def test_candidate_search_applies_phone_model_compatibility_guardrail(
    matching_client: TestClient, db_session: Session
) -> None:
    product = Product(
        article="IP17-DISP",
        name="Дисплей для Apple iPhone 17",
        brand="Apple",
        category="Дисплеи",
        subject="Дисплеи для телефонов",
    )
    iphone17 = PhoneModel(brand="apple", model_name="iphone 17")
    iphone11 = PhoneModel(brand="apple", model_name="iphone 11")
    item = CompetitorItem(
        competitor="moba",
        external_id="LCD-IP17-WRONG-COMPAT",
        name="Дисплей для iPhone 17",
        item_type="display",
        category_group="display",
        availability=True,
    )
    db_session.add_all([product, iphone17, iphone11, item])
    db_session.flush()
    db_session.add(
        ProductPhoneModel(
            product_id=product.id,
            phone_model_id=iphone17.id,
            source="test",
            raw_value="Apple iPhone 17",
        )
    )
    db_session.add(
        CompetitorItemCompatibility(
            competitor_item_id=item.id,
            phone_model_id=iphone11.id,
            device_brand="apple",
            device_model="iphone 11",
            source="test",
        )
    )
    db_session.commit()

    response = matching_client.get(
        f"/api/matching/products/{product.id}/candidate-search",
        params={"q": "iphone 17", "item_type": "display"},
        auth=_auth(),
    )
    assert response.status_code == 200
    assert response.json()["total"] == 0


def test_candidate_search_uses_model_compatibility_as_default_fallback(
    matching_client: TestClient, db_session: Session
) -> None:
    product = Product(
        article="MODEL-FALLBACK",
        name="Дисплей для Apple iPhone Xs Max + тачскрин (черный) (Premium RevQ)",
        category="Дисплеи для телефонов",
        subject="дисплей",
    )
    phone_model = PhoneModel(brand="apple", model_name="iphone xs max")
    item = CompetitorItem(
        competitor="moba",
        external_id="LCD-PMIMSX-CP-B-GX",
        name="Дисплей для iPhone Xs Max в сборе Черный GX",
        item_type="display",
        category_group="display",
        parsed_device_brand="apple",
        parsed_device_model="iphone xs max",
        availability=True,
    )
    db_session.add_all([product, phone_model, item])
    db_session.flush()
    db_session.add(
        ProductPhoneModel(
            product_id=product.id,
            phone_model_id=phone_model.id,
            source="test",
            raw_value="Apple iPhone Xs Max",
        )
    )
    db_session.add(
        CompetitorItemCompatibility(
            competitor_item_id=item.id,
            phone_model_id=phone_model.id,
            device_brand="apple",
            device_model="iphone xs max",
            source="test",
        )
    )
    db_session.commit()

    response = matching_client.get(
        f"/api/matching/products/{product.id}/candidate-search",
        auth=_auth(),
    )

    assert response.status_code == 200
    skus = {candidate["sku"] for candidate in response.json()["items"]}
    assert "LCD-PMIMSX-CP-B-GX" in skus


def test_candidate_search_prefers_title_model_over_parser_base_compatibility(
    matching_client: TestClient, db_session: Session
) -> None:
    product = Product(
        article="037404",
        name="Дисплей для Apple iPhone 6s + тачскрин (белый) (Medium)",
        category="Дисплеи для телефонов",
        subject="дисплей",
        subject_1c="дисплей",
        display_quality="Copy Medium",
        quality="Copy Medium",
        quality_raw="Optima",
        display_type="In-Cell",
    )
    iphone6 = PhoneModel(brand="apple", model_name="iphone 6")
    item = CompetitorItem(
        competitor="moba",
        external_id="LCD-PMIS600-CP-W",
        name="Дисплей для iPhone 6S в сборе с тачскрином Белый - Оптима",
        normalized_title="Дисплей для iPhone 6S в сборе с тачскрином Белый",
        item_type="display",
        category_group="display",
        parsed_device_brand="apple",
        parsed_device_model="iphone 6",
        attrs_model="iPhone 6S",
        attrs_quality="Original",
        screen_quality_grade="UNKNOWN",
        attrs_color="Белый",
        color="Белый",
        availability=True,
    )
    plus_item = CompetitorItem(
        competitor="moba",
        external_id="LCD-PMISP600-CP-W",
        name="Дисплей для iPhone 6S Plus в сборе с тачскрином Белый - Оптима",
        normalized_title="Дисплей для iPhone 6S Plus в сборе с тачскрином Белый",
        item_type="display",
        category_group="display",
        parsed_device_brand="apple",
        parsed_device_model="iphone 6",
        attrs_model="iPhone 6S Plus",
        availability=True,
    )
    db_session.add_all([product, iphone6, item, plus_item])
    db_session.flush()
    db_session.add_all(
        [
            CompetitorItemCompatibility(
                competitor_item_id=item.id,
                phone_model_id=iphone6.id,
                device_brand="apple",
                device_model="iphone 6",
                source="parser",
            ),
            CompetitorItemCompatibility(
                competitor_item_id=plus_item.id,
                phone_model_id=iphone6.id,
                device_brand="apple",
                device_model="iphone 6",
                source="parser",
            ),
        ]
    )
    db_session.commit()

    response = matching_client.get(
        f"/api/matching/products/{product.id}/candidate-search",
        params={"q": "LCD-PMIS600-CP-W"},
        auth=_auth(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["items"][0]["sku"] == "LCD-PMIS600-CP-W"
    skus = {candidate["sku"] for candidate in body["items"]}
    assert "LCD-PMISP600-CP-W" not in skus

    prefixed_response = matching_client.get(
        f"/api/matching/products/{product.id}/candidate-search",
        params={"q": "Артикул: LCD-PMIS600-CP-W"},
        auth=_auth(),
    )

    assert prefixed_response.status_code == 200
    prefixed_body = prefixed_response.json()
    assert prefixed_body["items"][0]["sku"] == "LCD-PMIS600-CP-W"
    prefixed_skus = {candidate["sku"] for candidate in prefixed_body["items"]}
    assert "LCD-PMISP600-CP-W" not in prefixed_skus


def test_candidate_search_allows_iphone_slash_combo_display(
    matching_client: TestClient, db_session: Session
) -> None:
    product = Product(
        article="064280",
        name=(
            "Дисплей для Apple iPhone 12 / iPhone 12 Pro + тачскрин "
            "(черный) (GX ORIG) (Hard Oled)"
        ),
        category="Дисплеи для телефонов",
        subject="дисплей",
        subject_1c="дисплей",
        display_quality="Copy High",
        quality="Copy High",
        quality_raw="High",
        display_type="OLED",
        display_has_frame=True,
        color="черный",
    )
    item = CompetitorItem(
        competitor="moba",
        external_id="LCD-PMI120-CP-B-GX",
        name=(
            "Дисплей для iPhone 12/12 Pro (A2403/A2407) в сборе с тачскрином "
            "Черный - GX (Hard OLED) (площадка под IC)"
        ),
        normalized_title="Дисплей для iPhone 12/12 Pro в сборе с тачскрином Черный GX",
        item_type="display",
        category_group="display",
        parsed_device_brand="apple",
        parsed_device_model="iphone 12",
        attrs_model="12/12 Pro",
        attrs_quality="GX",
        screen_quality_grade="GX",
        attrs_color="Черный",
        color="Черный",
        availability=True,
    )
    db_session.add_all([product, item])
    db_session.commit()

    response = matching_client.get(
        f"/api/matching/products/{product.id}/candidate-search",
        params={"q": "LCD-PMI120-CP-B-GX"},
        auth=_auth(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["items"][0]["sku"] == "LCD-PMI120-CP-B-GX"

    prefixed_response = matching_client.get(
        f"/api/matching/products/{product.id}/candidate-search",
        params={"q": "Артикул: LCD-PMI120-CP-B-GX"},
        auth=_auth(),
    )

    assert prefixed_response.status_code == 200
    assert prefixed_response.json()["items"][0]["sku"] == "LCD-PMI120-CP-B-GX"


def test_candidate_search_allows_samsung_s20_text_overlap_when_compat_ids_differ(
    matching_client: TestClient, db_session: Session
) -> None:
    product = Product(
        article="061642",
        name=(
            "Дисплей для Samsung G980 Galaxy S20 + тачскрин "
            "(серый) (в рамке) (OLED) (Small Size)"
        ),
        brand="Samsung",
        category="Дисплеи для телефонов",
        subject="дисплей",
    )
    wrong_product_model = PhoneModel(brand="samsung", model_name="galaxy s20 5g")
    competitor_model = PhoneModel(brand="samsung", model_name="galaxy s20")
    item = CompetitorItem(
        competitor="moba",
        external_id="LCD-SSG-G980F-FR-GY-OR-SP",
        name="Дисплей для Samsung Galaxy S20 (G980F) модуль с рамкой Серый - OR Ref. (SP)",
        normalized_title=(
            "Дисплей для Samsung Galaxy S20 (G980F) модуль с рамкой Серый - OR Ref. (SP)"
        ),
        item_type="display",
        category_group="display",
        parsed_device_brand="samsung",
        parsed_device_model="galaxy s20",
        availability=True,
    )
    db_session.add_all([product, wrong_product_model, competitor_model, item])
    db_session.flush()
    db_session.add_all(
        [
            ProductPhoneModel(
                product_id=product.id,
                phone_model_id=wrong_product_model.id,
                source="onec",
                raw_value="g981 galaxy s20 5g",
            ),
            CompetitorItemCompatibility(
                competitor_item_id=item.id,
                phone_model_id=competitor_model.id,
                device_brand="samsung",
                device_model="g980 galaxy s20",
                source="parser",
            ),
        ]
    )
    db_session.commit()

    response = matching_client.get(
        f"/api/matching/products/{product.id}/candidate-search",
        auth=_auth(),
    )

    assert response.status_code == 200
    skus = {candidate["sku"] for candidate in response.json()["items"]}
    assert "LCD-SSG-G980F-FR-GY-OR-SP" in skus


def test_candidate_search_blocks_samsung_s20_base_against_other_s20_variants(
    matching_client: TestClient, db_session: Session
) -> None:
    product = Product(
        article="S20-BASE",
        name="Дисплей для Samsung G980 Galaxy S20 + тачскрин",
        brand="Samsung",
        category="Дисплеи для телефонов",
        subject="дисплей",
    )
    items = [
        CompetitorItem(
            competitor="moba",
            external_id="LCD-S20-PLUS",
            name="Дисплей для Samsung Galaxy S20+ (G985F) модуль с рамкой",
            item_type="display",
            category_group="display",
            availability=True,
        ),
        CompetitorItem(
            competitor="moba",
            external_id="LCD-S20-ULTRA",
            name="Дисплей для Samsung Galaxy S20 Ultra (G988B) модуль с рамкой",
            item_type="display",
            category_group="display",
            availability=True,
        ),
        CompetitorItem(
            competitor="moba",
            external_id="LCD-S20-FE",
            name="Дисплей для Samsung Galaxy S20 FE (G780F) модуль с рамкой",
            item_type="display",
            category_group="display",
            availability=True,
        ),
    ]
    db_session.add(product)
    db_session.add_all(items)
    db_session.commit()

    response = matching_client.get(
        f"/api/matching/products/{product.id}/candidate-search",
        params={"q": "samsung s20", "item_type": "display"},
        auth=_auth(),
    )

    assert response.status_code == 200
    assert response.json()["total"] == 0


def test_candidate_search_blocks_iphone_air_for_base_iphone(
    matching_client: TestClient, db_session: Session
) -> None:
    product = Product(
        article="IP17-BASE",
        name="Дисплей для Apple iPhone 17 + тачскрин",
        brand="Apple",
        category="Дисплеи",
        subject="Дисплеи для телефонов",
    )
    item = CompetitorItem(
        competitor="moba",
        external_id="LCD-PMI-AIR",
        name="Дисплей для iPhone Air (A3517) в сборе с тачскрином Черный",
        item_type="display",
        category_group="display",
        availability=True,
    )
    db_session.add_all([product, item])
    db_session.commit()

    response = matching_client.get(
        f"/api/matching/products/{product.id}/candidate-search",
        params={"q": "iphone", "item_type": "display"},
        auth=_auth(),
    )
    assert response.status_code == 200
    assert response.json()["total"] == 0


def test_multiple_auto_matches_from_distinct_competitors_are_matched(
    matching_client: TestClient, db_session: Session
) -> None:
    product = Product(
        article="P-003",
        name="Дисплей для Google Pixel 8",
        brand="Google",
        category="Дисплеи",
        subject="Дисплеи для телефонов",
    )
    item1 = CompetitorItem(
        competitor="moba",
        external_id="LCD-GGL-PXL8",
        name="Дисплей Google Pixel 8",
        item_type="display",
        category_group="display",
        item_brand="Google",
        price_roz=3000,
        availability=True,
    )
    item2 = CompetitorItem(
        competitor="liberti",
        external_id="470001",
        name="LCD дисплей Google Pixel 8",
        item_type="display",
        category_group="display",
        item_brand="Google",
        price_roz=3100,
        availability=True,
    )
    db_session.add_all([product, item1, item2])
    db_session.flush()
    db_session.add_all(
        [
            CompetitorItemMatch(
                competitor_item_id=item1.id,
                product_id=product.id,
                status=CompetitorItemMatchStatus.ACCEPTED,
                method=CompetitorItemMatchMethod.EMBEDDING_AUTO,
                final_score=0.91,
            ),
            CompetitorItemMatch(
                competitor_item_id=item2.id,
                product_id=product.id,
                status=CompetitorItemMatchStatus.ACCEPTED,
                method=CompetitorItemMatchMethod.EMBEDDING_AUTO,
                final_score=0.9,
            ),
        ]
    )
    db_session.commit()

    response = matching_client.get(
        "/api/matching/products",
        params={"search": product.article},
        auth=_auth(),
    )

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["accepted_count"] == 2
    assert item["status"] == "auto"


def test_candidate_search_accept_revoke_history(
    matching_client: TestClient, db_session: Session
) -> None:
    seeded = _seed(db_session)
    product = seeded["p1"]
    item = seeded["item1"]

    response = matching_client.get(
        f"/api/matching/products/{product.id}/candidate-search",
        params={"q": "iphone 11", "item_type": "display"},
        auth=_auth(),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] >= 1
    assert body["items"][0]["competitor_item_id"] == item.id
    assert body["items"][0]["sku"] == "LCD-IPH11-BLK"
    assert body["facets"]["sources"][0]["value"] == "moba"

    accepted = matching_client.post(
        f"/api/matching/products/{product.id}/matches",
        json={"competitor_item_id": item.id, "reason_code": "confirmed_attributes"},
        auth=_auth(),
    )
    assert accepted.status_code == 200
    assert accepted.json()["competitor_item_id"] == item.id

    products = matching_client.get(
        "/api/matching/products", params={"status": "manual"}, auth=_auth()
    )
    assert products.status_code == 200
    assert products.json()["total"] == 2

    revoked = matching_client.post(
        f"/api/matching/products/{product.id}/revoke",
        json={"competitor_item_id": item.id, "reason_code": "auto_false_positive"},
        auth=_auth(),
    )
    assert revoked.status_code == 200

    history = matching_client.get(f"/api/matching/products/{product.id}/history", auth=_auth())
    assert history.status_code == 200
    history_items = history.json()["items"]
    assert [row["action"] for row in history_items] == ["revoke", "accept"]
    assert history_items[0]["previous_status"] == "accepted"
    assert history_items[1]["previous_status"] == "suggested"
    assert history_items[0]["reason_code"] == "auto_false_positive"
    assert history_items[1]["reason_code"] == "confirmed_attributes"
    assert history_items[1]["snapshot_schema_version"] == 1
    assert history_items[1]["snapshot_top_k_count"] >= 1


def test_accept_new_item_infers_missing_compatibility_from_product_model(
    matching_client: TestClient, db_session: Session
) -> None:
    product = Product(
        article="P-REALME-C85",
        name="Держатель сим-карты для Realme C85 4G (RMX5566) (черный)",
        brand="Realme",
        category="Держатели сим-карт",
        subject="держатель сим-карты",
    )
    item = CompetitorItem(
        competitor="moba",
        external_id="HLD-SIM-REAL-C85-4G-B",
        name="Держатель SIM для Realme C85 4G (RMX5566) Черный",
        item_type="sim_holder",
        category_group="sim_holder",
        availability=True,
        first_seen_at=datetime(2026, 5, 2, tzinfo=timezone.utc),
    )
    db_session.add_all([product, item])
    db_session.flush()
    phone_model = PhoneModel(brand="realme", model_name="c85 4g (rmx5566)")
    db_session.add(phone_model)
    db_session.flush()
    db_session.add(
        ProductPhoneModel(
            product_id=product.id,
            phone_model_id=phone_model.id,
            source="test",
            raw_value="realme c85 4g (rmx5566)",
        )
    )
    db_session.commit()

    search = matching_client.get(
        f"/api/matching/products/{product.id}/candidate-search",
        params={"q": "Realme C85 4G"},
        auth=_auth(),
    )
    assert search.status_code == 200
    candidates = {candidate["sku"]: candidate for candidate in search.json()["items"]}
    assert candidates["HLD-SIM-REAL-C85-4G-B"]["compatibility_hint"]["status"] == "inferred_model"
    assert "realme c85 4g (rmx5566)" in [
        value.lower()
        for value in candidates["HLD-SIM-REAL-C85-4G-B"]["compatibility_hint"]["matched_values"]
    ]

    accepted = matching_client.post(
        f"/api/matching/products/{product.id}/matches",
        json={"competitor_item_id": item.id},
        auth=_auth(),
    )

    assert accepted.status_code == 200
    compat = (
        db_session.query(CompetitorItemCompatibility)
        .filter(CompetitorItemCompatibility.competitor_item_id == item.id)
        .one()
    )
    assert compat.phone_model_id == phone_model.id
    assert compat.source == "manual_accept_inferred"


def test_accept_new_router_battery_infers_compatibility_from_shared_code(
    matching_client: TestClient, db_session: Session
) -> None:
    product = Product(
        article="041567",
        name="Аккумулятор для Huawei Wi-Fi роутера E5573 / E5577 (HB434666RBC)",
        category="Аккумуляторы",
        subject="аккумулятор",
    )
    item = CompetitorItem(
        competitor="moba",
        external_id="BTT-HUW-HB434666RBC",
        name="Аккумулятор для Huawei E5573 Wi-Fi роутер (HB434666RBC)",
        item_type="battery",
        category_group="battery",
        availability=True,
        first_seen_at=datetime(2026, 5, 2, tzinfo=timezone.utc),
    )
    db_session.add_all([product, item])
    db_session.commit()

    search = matching_client.get(
        f"/api/matching/products/{product.id}/candidate-search",
        auth=_auth(),
    )
    assert search.status_code == 200
    candidates = {candidate["sku"]: candidate for candidate in search.json()["items"]}
    assert candidates["BTT-HUW-HB434666RBC"]["needs_compat_review"] is False
    assert candidates["BTT-HUW-HB434666RBC"]["compatibility_hint"]["status"] == "inferred_code"
    assert (
        "HB434666RBC" in candidates["BTT-HUW-HB434666RBC"]["compatibility_hint"]["matched_values"]
    )

    accepted = matching_client.post(
        f"/api/matching/products/{product.id}/matches",
        json={"competitor_item_id": item.id},
        auth=_auth(),
    )

    assert accepted.status_code == 200
    compat = (
        db_session.query(CompetitorItemCompatibility)
        .filter(CompetitorItemCompatibility.competitor_item_id == item.id)
        .one()
    )
    assert compat.phone_model_id is None
    assert compat.device_model == "HB434666RBC"
    assert compat.source == "manual_accept_code_overlap"


def test_accept_new_item_without_model_or_shared_code_still_requires_compatibility(
    matching_client: TestClient, db_session: Session
) -> None:
    product = Product(
        article="047312",
        name="Аккумулятор для OnePlus 8 Pro (BLP759)",
        category="Аккумуляторы",
        subject="аккумулятор",
    )
    item = CompetitorItem(
        competitor="moba",
        external_id="BTT-ONE-NOCODE",
        name="Аккумулятор для OnePlus 8 Pro",
        item_type="battery",
        category_group="battery",
        availability=True,
        first_seen_at=datetime(2026, 5, 2, tzinfo=timezone.utc),
    )
    db_session.add_all([product, item])
    db_session.commit()

    search = matching_client.get(
        f"/api/matching/products/{product.id}/candidate-search",
        params={"q": "OnePlus 8 Pro"},
        auth=_auth(),
    )
    assert search.status_code == 200
    candidates = {candidate["sku"]: candidate for candidate in search.json()["items"]}
    assert candidates["BTT-ONE-NOCODE"]["needs_compat_review"] is True
    assert candidates["BTT-ONE-NOCODE"]["compatibility_hint"]["status"] == "required"

    accepted = matching_client.post(
        f"/api/matching/products/{product.id}/matches",
        json={"competitor_item_id": item.id},
        auth=_auth(),
    )

    assert accepted.status_code == 409
    assert accepted.json()["detail"]["error"] == "compatibility_required"


def test_candidate_search_uses_product_tokens_by_default(
    matching_client: TestClient, db_session: Session
) -> None:
    seeded = _seed(db_session)
    product = seeded["p1"]
    item = seeded["item1"]
    distractor = CompetitorItem(
        competitor="moba",
        external_id="LCD-IPH11-MAX-WRONG",
        name="Дисплей iPhone 11 Pro Max черный",
        item_type="display",
        category_group="display",
        item_brand="Apple",
        attrs_model="iPhone 11 Pro Max",
        price_roz=7700,
        availability=True,
    )
    wrong_subject = CompetitorItem(
        competitor="moba",
        external_id="BAT-IPH11-WRONG",
        name="Аккумулятор iPhone 11",
        item_type="battery",
        category_group="battery",
        item_brand="Apple",
        attrs_model="iPhone 11",
        price_roz=900,
        availability=True,
    )
    wrong_display_type = CompetitorItem(
        competitor="moba",
        external_id="MDL-BGA-IPH11-WRONG",
        name="Модуль BGA для iPhone 11",
        item_type="display",
        category_group="tools",
        item_brand="Apple",
        attrs_model="iPhone 11",
        price_roz=1200,
        availability=True,
    )
    db_session.add_all([distractor, wrong_subject, wrong_display_type])
    db_session.commit()

    response = matching_client.get(
        f"/api/matching/products/{product.id}/candidate-search",
        auth=_auth(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] >= 1
    assert body["items"][0]["competitor_item_id"] == item.id
    assert body["items"][0]["status"] == "suggested"
    assert {row["item_type"] for row in body["items"]} == {"display"}
    assert wrong_display_type.id not in {row["competitor_item_id"] for row in body["items"]}

    battery_response = matching_client.get(
        f"/api/matching/products/{product.id}/candidate-search",
        params={"item_type": "battery", "q": "iPhone 11"},
        auth=_auth(),
    )
    assert battery_response.status_code == 200
    assert any(
        row["competitor_item_id"] == wrong_subject.id for row in battery_response.json()["items"]
    )


def test_candidate_search_filters_by_candidate_status(
    matching_client: TestClient, db_session: Session
) -> None:
    seeded = _seed(db_session)
    product = seeded["p1"]
    locked_item = seeded["item2"]

    locked = matching_client.get(
        f"/api/matching/products/{product.id}/candidate-search",
        params={
            "q": "Samsung A50",
            "candidate_status": "locked",
            "item_type": "battery",
        },
        auth=_auth(),
    )
    assert locked.status_code == 200
    assert locked.json()["total"] == 1
    assert locked.json()["items"][0]["competitor_item_id"] == locked_item.id
    assert locked.json()["items"][0]["status"] == "locked"


def test_candidate_search_default_does_not_require_own_article(
    matching_client: TestClient, db_session: Session
) -> None:
    seeded = _seed(db_session)
    item = seeded["item1"]
    product = Product(
        article="OUR-ARTICLE-ONLY",
        name="Дисплей для iPhone 11",
        brand="Apple",
        category="Дисплеи",
        quality="High Copy",
    )
    db_session.add(product)
    db_session.commit()

    response = matching_client.get(
        f"/api/matching/products/{product.id}/candidate-search",
        auth=_auth(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] >= 1
    assert body["items"][0]["competitor_item_id"] == item.id


def test_candidate_search_default_ignores_iphone_service_tokens(
    matching_client: TestClient, db_session: Session
) -> None:
    product = Product(
        article="P-IPH17-PRO",
        name=(
            "Дисплей для Apple iPhone 17 Pro (SIM + eSIM) / iPhone 17 Pro (eSIM) "
            "+ тачскрин + ALS шлейф (черный) (ORIG100) (Снятый)"
        ),
        brand="Apple",
        category="Дисплеи для телефонов",
        subject="дисплей",
    )
    target = CompetitorItem(
        competitor="moba",
        external_id="LCD-PMI-17-PR-CP-B-OR100",
        name=(
            "Дисплей для iPhone 17 Pro (A3523) в сборе с тачскрином Черный - "
            "OR100 (Снятый, без ремонта)"
        ),
        item_type="display",
        category_group="display",
        parsed_device_model="iphone 17 pro",
        price_roz=40700,
        availability=True,
    )
    wrong_model = CompetitorItem(
        competitor="moba",
        external_id="LCD-PMI-17-CP-B-OR100",
        name="Дисплей для iPhone 17 (A3520) в сборе с тачскрином Черный - OR100",
        item_type="display",
        category_group="display",
        parsed_device_model="iphone 17",
        price_roz=39500,
        availability=True,
    )
    db_session.add_all([product, target, wrong_model])
    db_session.commit()

    response = matching_client.get(
        f"/api/matching/products/{product.id}/candidate-search",
        auth=_auth(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["competitor_item_id"] == target.id


def test_candidate_search_default_does_not_match_model_number_in_sku(
    matching_client: TestClient, db_session: Session
) -> None:
    product = Product(
        article="P-IPH17",
        name="Дисплей для Apple iPhone 17 + тачскрин (черный) (ORIG100)",
        brand="Apple",
        category="Дисплеи для телефонов",
        subject="дисплей",
    )
    target = CompetitorItem(
        competitor="moba",
        external_id="LCD-PMI-17-CP-B-OR100",
        name="Дисплей для iPhone 17 (A3520) в сборе с тачскрином Черный - OR100",
        item_type="display",
        category_group="display",
        parsed_device_model="iphone 17",
        price_roz=39500,
        availability=True,
    )
    sku_false_positive = CompetitorItem(
        competitor="liberti",
        external_id="406217",
        name="LCD дисплей для Apple iPhone 11 (черный) с тачскрином original",
        item_type="display",
        category_group="display",
        parsed_device_model="iphone 11",
        price_roz=3000,
        availability=True,
    )
    db_session.add_all([product, target, sku_false_positive])
    db_session.commit()

    response = matching_client.get(
        f"/api/matching/products/{product.id}/candidate-search",
        auth=_auth(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["competitor_item_id"] == target.id


def test_candidate_search_tokenizes_long_content_query(
    matching_client: TestClient, db_session: Session
) -> None:
    seeded = _seed(db_session)
    product = seeded["p1"]
    item = seeded["item1"]

    response = matching_client.get(
        f"/api/matching/products/{product.id}/candidate-search",
        params={"q": "Дисплей для iPhone 11 черный"},
        auth=_auth(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["competitor_item_id"] == item.id


def test_reject_hides_only_for_current_product(
    matching_client: TestClient, db_session: Session
) -> None:
    seeded = _seed(db_session)
    product = seeded["p1"]
    item = seeded["item3"]

    rejected = matching_client.post(
        f"/api/matching/products/{product.id}/reject",
        json={"competitor_item_id": item.id, "reason": "wrong color"},
        auth=_auth(),
    )
    assert rejected.status_code == 200

    hidden = matching_client.get(
        f"/api/matching/products/{product.id}/candidate-search",
        params={"q": "белый"},
        auth=_auth(),
    )
    assert hidden.status_code == 200
    assert hidden.json()["total"] == 0

    visible = matching_client.get(
        f"/api/matching/products/{product.id}/candidate-search",
        params={"q": "белый", "include_rejected": True},
        auth=_auth(),
    )
    assert visible.status_code == 200
    assert visible.json()["total"] == 1
    assert visible.json()["items"][0]["status"] == "rejected"


def test_bulk_reject_hides_multiple_candidates_and_skips_locked(
    matching_client: TestClient, db_session: Session
) -> None:
    seeded = _seed(db_session)
    product = seeded["p1"]
    item1 = seeded["item1"]
    item2 = seeded["item2"]
    item3 = seeded["item3"]
    missing_id = 999999

    response = matching_client.post(
        f"/api/matching/products/{product.id}/reject-bulk",
        json={
            "competitor_item_ids": [item1.id, item3.id, item2.id, missing_id],
            "reason": "bulk cleanup",
        },
        auth=_auth(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["rejected_count"] == 2
    assert body["skipped_count"] == 2
    reasons = {row["competitor_item_id"]: row["reason"] for row in body["items"]}
    assert reasons[item1.id] == "rejected"
    assert reasons[item3.id] == "rejected"
    assert reasons[item2.id] == "locked"
    assert reasons[missing_id] == "not_found"

    hidden = matching_client.get(
        f"/api/matching/products/{product.id}/candidate-search",
        params={"q": "iphone 11", "item_type": "display"},
        auth=_auth(),
    )
    assert hidden.status_code == 200
    hidden_ids = {row["competitor_item_id"] for row in hidden.json()["items"]}
    assert item1.id not in hidden_ids
    assert item3.id not in hidden_ids

    visible = matching_client.get(
        f"/api/matching/products/{product.id}/candidate-search",
        params={"q": "iphone 11", "item_type": "display", "include_rejected": True},
        auth=_auth(),
    )
    assert visible.status_code == 200
    statuses = {
        row["competitor_item_id"]: row["status"]
        for row in visible.json()["items"]
        if row["competitor_item_id"] in {item1.id, item3.id}
    }
    assert statuses == {item1.id: "rejected", item3.id: "rejected"}


def test_bulk_reject_skips_current_match(matching_client: TestClient, db_session: Session) -> None:
    seeded = _seed(db_session)
    product = seeded["p1"]
    item = seeded["item1"]

    accepted = matching_client.post(
        f"/api/matching/products/{product.id}/matches",
        json={"competitor_item_id": item.id},
        auth=_auth(),
    )
    assert accepted.status_code == 200

    response = matching_client.post(
        f"/api/matching/products/{product.id}/reject-bulk",
        json={"competitor_item_ids": [item.id], "reason": "bulk cleanup"},
        auth=_auth(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["rejected_count"] == 0
    assert body["skipped_count"] == 1
    assert body["items"][0]["reason"] == "current"

    history = matching_client.get(f"/api/matching/products/{product.id}/history", auth=_auth())
    assert history.status_code == 200
    assert [row["action"] for row in history.json()["items"]] == ["accept"]


def test_bulk_reject_is_idempotent_for_already_rejected_candidate(
    matching_client: TestClient, db_session: Session
) -> None:
    seeded = _seed(db_session)
    product = seeded["p1"]
    item = seeded["item3"]

    first = matching_client.post(
        f"/api/matching/products/{product.id}/reject-bulk",
        json={"competitor_item_ids": [item.id], "reason": "bulk cleanup"},
        auth=_auth(),
    )
    assert first.status_code == 200
    assert first.json()["rejected_count"] == 1

    second = matching_client.post(
        f"/api/matching/products/{product.id}/reject-bulk",
        json={"competitor_item_ids": [item.id], "reason": "bulk cleanup"},
        auth=_auth(),
    )
    assert second.status_code == 200
    body = second.json()
    assert body["rejected_count"] == 0
    assert body["skipped_count"] == 1
    assert body["items"][0]["reason"] == "already_rejected"

    history = matching_client.get(f"/api/matching/products/{product.id}/history", auth=_auth())
    assert history.status_code == 200
    assert [row["action"] for row in history.json()["items"]] == ["reject"]


def test_revoke_rejected_candidate_returns_it_to_search(
    matching_client: TestClient, db_session: Session
) -> None:
    seeded = _seed(db_session)
    product = seeded["p1"]
    item = seeded["item3"]

    rejected = matching_client.post(
        f"/api/matching/products/{product.id}/reject-bulk",
        json={"competitor_item_ids": [item.id], "reason": "wrong candidate"},
        auth=_auth(),
    )
    assert rejected.status_code == 200

    hidden = matching_client.get(
        f"/api/matching/products/{product.id}/candidate-search",
        params={"q": "белый"},
        auth=_auth(),
    )
    assert hidden.status_code == 200
    assert hidden.json()["total"] == 0

    revoked = matching_client.post(
        f"/api/matching/products/{product.id}/revoke",
        json={"competitor_item_id": item.id, "reason": "mistaken reject"},
        auth=_auth(),
    )
    assert revoked.status_code == 200

    visible = matching_client.get(
        f"/api/matching/products/{product.id}/candidate-search",
        params={"q": "белый"},
        auth=_auth(),
    )
    assert visible.status_code == 200
    assert visible.json()["total"] == 1
    assert visible.json()["items"][0]["competitor_item_id"] == item.id

    history = matching_client.get(f"/api/matching/products/{product.id}/history", auth=_auth())
    assert history.status_code == 200
    history_items = history.json()["items"]
    assert [row["action"] for row in history_items] == ["revoke", "reject"]
    assert history_items[0]["previous_status"] == "rejected"


def test_foreign_auto_rejection_stays_available_for_current_product(
    matching_client: TestClient, db_session: Session
) -> None:
    seeded = _seed(db_session)
    product = seeded["p1"]
    item = seeded["item1"]
    foreign_product = Product(
        article="P-FOREIGN",
        name="Дисплей для другого товара",
        brand="Apple",
        category="Дисплеи",
        subject="Дисплеи для телефонов",
    )
    db_session.add(foreign_product)
    db_session.flush()

    match = (
        db_session.query(CompetitorItemMatch)
        .filter(CompetitorItemMatch.competitor_item_id == item.id)
        .one()
    )
    match.product_id = foreign_product.id
    match.status = CompetitorItemMatchStatus.REJECTED
    match.method = CompetitorItemMatchMethod.EMBEDDING_AUTO
    match.final_score = 0.99
    db_session.commit()

    response = matching_client.get(
        f"/api/matching/products/{product.id}/candidate-search",
        params={"q": item.external_id, "include_rejected": True},
        auth=_auth(),
    )

    assert response.status_code == 200
    candidate = next(
        row for row in response.json()["items"] if row["competitor_item_id"] == item.id
    )
    assert candidate["status"] == "available"
    assert candidate["confidence"] is None

    revoke = matching_client.post(
        f"/api/matching/products/{product.id}/revoke",
        json={"competitor_item_id": item.id, "reason_code": "auto_false_positive"},
        auth=_auth(),
    )
    assert revoke.status_code == 404
    db_session.refresh(match)
    assert match.product_id == foreign_product.id
    assert match.status == CompetitorItemMatchStatus.REJECTED


def test_revoke_current_auto_rejection_removes_match(
    matching_client: TestClient, db_session: Session
) -> None:
    seeded = _seed(db_session)
    product = seeded["p1"]
    item = seeded["item1"]
    match = (
        db_session.query(CompetitorItemMatch)
        .filter(CompetitorItemMatch.competitor_item_id == item.id)
        .one()
    )
    match.status = CompetitorItemMatchStatus.REJECTED
    match.method = CompetitorItemMatchMethod.EMBEDDING_AUTO
    db_session.commit()

    revoked = matching_client.post(
        f"/api/matching/products/{product.id}/revoke",
        json={"competitor_item_id": item.id, "reason_code": "auto_false_positive"},
        auth=_auth(),
    )

    assert revoked.status_code == 200
    assert db_session.get(CompetitorItemMatch, match.id) is None


def test_revoke_rejected_candidate_restores_previous_suggestion_status(
    matching_client: TestClient, db_session: Session
) -> None:
    seeded = _seed(db_session)
    product = seeded["p1"]
    item = seeded["item1"]

    rejected = matching_client.post(
        f"/api/matching/products/{product.id}/reject-bulk",
        json={"competitor_item_ids": [item.id], "reason": "wrong candidate"},
        auth=_auth(),
    )
    assert rejected.status_code == 200

    rejected_search = matching_client.get(
        f"/api/matching/products/{product.id}/candidate-search",
        params={"q": "черный", "include_rejected": True},
        auth=_auth(),
    )
    assert rejected_search.status_code == 200
    assert rejected_search.json()["items"][0]["status"] == "rejected"

    revoked = matching_client.post(
        f"/api/matching/products/{product.id}/revoke",
        json={"competitor_item_id": item.id, "reason": "mistaken reject"},
        auth=_auth(),
    )
    assert revoked.status_code == 200

    restored = matching_client.get(
        f"/api/matching/products/{product.id}/candidate-search",
        params={"q": "черный"},
        auth=_auth(),
    )
    assert restored.status_code == 200
    assert restored.json()["items"][0]["competitor_item_id"] == item.id
    assert restored.json()["items"][0]["status"] == "suggested"


def test_accept_locked_item_returns_conflict(
    matching_client: TestClient, db_session: Session
) -> None:
    seeded = _seed(db_session)
    product = seeded["p1"]
    locked_item = seeded["item2"]

    response = matching_client.post(
        f"/api/matching/products/{product.id}/matches",
        json={"competitor_item_id": locked_item.id},
        auth=_auth(),
    )
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "already_accepted"


def test_property_mapping_rules_create_and_apply_value_map(
    matching_client: TestClient, db_session: Session
) -> None:
    product = Product(
        article="P-PROP-MAP",
        name="Дисплей для Apple iPhone 11 черный",
        brand="Apple",
        category="Дисплеи",
        subject="Дисплеи для телефонов",
        color="черный",
    )
    item = CompetitorItem(
        competitor="moba",
        external_id="LCD-PROP-MAP",
        name="Дисплей для iPhone 11 nero",
        item_type="display",
        category_group="display",
        parsed_device_model="iphone 11",
        attrs_json={"finish": "nero"},
        availability=True,
    )
    db_session.add_all([product, item])
    db_session.commit()

    profiles = matching_client.get("/api/matching/property-profiles", auth=_auth())
    assert profiles.status_code == 200
    display_profile = next(row for row in profiles.json() if row["code"] == "display")

    rule_response = matching_client.post(
        "/api/matching/property-rules",
        json={
            "profile_id": display_profile["id"],
            "property_key": "finish",
            "label": "Отделка",
            "product_field": "display.color",
            "competitor_field": "attrs.finish",
            "comparison_mode": "mapped_value",
            "severity": "review",
            "sort_order": 500,
        },
        auth=_auth(),
    )
    assert rule_response.status_code == 200
    rule_id = rule_response.json()["id"]

    map_response = matching_client.post(
        "/api/matching/property-value-maps",
        json={
            "rule_id": rule_id,
            "competitor_source": "moba",
            "competitor_value": "nero",
            "mapped_value": "black",
        },
        auth=_auth(),
    )
    assert map_response.status_code == 200

    comparison = matching_client.get(
        f"/api/matching/products/{product.id}/candidates/{item.id}/properties",
        auth=_auth(),
    )
    assert comparison.status_code == 200
    finish = next(row for row in comparison.json()["items"] if row["property_key"] == "finish")
    assert finish["competitor_value"] == "nero"
    assert finish["mapped_value"] == "black"
    assert finish["status"] == "match"


def test_property_mapping_defaults_seed_current_value_maps(
    matching_client: TestClient,
) -> None:
    profiles = matching_client.get("/api/matching/property-profiles", auth=_auth())
    assert profiles.status_code == 200

    value_maps = matching_client.get(
        "/api/matching/property-value-maps",
        params={"profile_code": "display"},
        auth=_auth(),
    )
    assert value_maps.status_code == 200
    rows = value_maps.json()

    assert any(
        row["property_key"] == "quality"
        and row["competitor_source"] == "moba"
        and row["competitor_value"] == "or100"
        and row["mapped_value"] == "Original"
        for row in rows
    )
    assert any(
        row["property_key"] == "color"
        and row["competitor_source"] is None
        and row["competitor_value"] in {"black", "черный", "чёрный"}
        and row["mapped_value"] == "black"
        for row in rows
    )
    assert any(
        row["property_key"] == "type"
        and row["competitor_source"] is None
        and row["competitor_value"] == "AMOLED"
        and row["mapped_value"] == "AMOLED"
        for row in rows
    )

    rules = matching_client.get(
        "/api/matching/property-rules",
        params={"profile_code": "display"},
        auth=_auth(),
    )
    assert rules.status_code == 200
    matrix_type = next(row for row in rules.json() if row["property_key"] == "type")
    construction = next(row for row in rules.json() if row["property_key"] == "construction")
    assert matrix_type["comparison_mode"] == "mapped_value"
    assert construction["comparison_mode"] == "mapped_value"


def test_property_mapping_patch_clears_nullable_fields_and_rejects_duplicates(
    matching_client: TestClient,
) -> None:
    profiles = matching_client.get("/api/matching/property-profiles", auth=_auth())
    display_profile = next(row for row in profiles.json() if row["code"] == "display")
    rule_response = matching_client.post(
        "/api/matching/property-rules",
        json={
            "profile_id": display_profile["id"],
            "property_key": "finish",
            "label": "Отделка",
            "product_field": "display.color",
            "competitor_field": "attrs.finish",
            "comparison_mode": "mapped_value",
            "severity": "review",
            "sort_order": 500,
        },
        auth=_auth(),
    )
    assert rule_response.status_code == 200
    rule_id = rule_response.json()["id"]

    created = matching_client.post(
        "/api/matching/property-value-maps",
        json={
            "rule_id": rule_id,
            "competitor_source": "moba",
            "competitor_value": "nero",
            "mapped_value": "black",
            "notes": "test",
        },
        auth=_auth(),
    )
    assert created.status_code == 200
    value_map_id = created.json()["id"]

    duplicate = matching_client.post(
        "/api/matching/property-value-maps",
        json={
            "rule_id": rule_id,
            "competitor_source": " moba ",
            "competitor_value": " NERO ",
            "mapped_value": "black",
        },
        auth=_auth(),
    )
    assert duplicate.status_code == 409

    patched = matching_client.patch(
        f"/api/matching/property-value-maps/{value_map_id}",
        json={"competitor_source": None, "notes": None},
        auth=_auth(),
    )
    assert patched.status_code == 200
    assert patched.json()["competitor_source"] is None
    assert patched.json()["notes"] is None


def test_property_mapping_source_specific_map_wins_over_global(
    matching_client: TestClient, db_session: Session
) -> None:
    product = Product(
        article="P-PROP-SOURCE-PRIORITY",
        name="Дисплей для Apple iPhone 11 черный",
        brand="Apple",
        category="Дисплеи",
        subject="Дисплеи для телефонов",
        color="черный",
    )
    item = CompetitorItem(
        competitor="moba",
        external_id="LCD-PROP-SOURCE-PRIORITY",
        name="Дисплей для iPhone 11 nero",
        item_type="display",
        category_group="display",
        parsed_device_model="iphone 11",
        attrs_json={"finish": "nero"},
        availability=True,
    )
    db_session.add_all([product, item])
    db_session.commit()

    profiles = matching_client.get("/api/matching/property-profiles", auth=_auth())
    display_profile = next(row for row in profiles.json() if row["code"] == "display")
    rule = matching_client.post(
        "/api/matching/property-rules",
        json={
            "profile_id": display_profile["id"],
            "property_key": "finish",
            "label": "Отделка",
            "product_field": "display.color",
            "competitor_field": "attrs.finish",
            "comparison_mode": "mapped_value",
            "severity": "review",
            "sort_order": 500,
        },
        auth=_auth(),
    )
    rule_id = rule.json()["id"]
    for source, mapped in ((None, "white"), ("moba", "black")):
        response = matching_client.post(
            "/api/matching/property-value-maps",
            json={
                "rule_id": rule_id,
                "competitor_source": source,
                "competitor_value": "nero",
                "mapped_value": mapped,
            },
            auth=_auth(),
        )
        assert response.status_code == 200

    comparison = matching_client.get(
        f"/api/matching/products/{product.id}/candidates/{item.id}/properties",
        auth=_auth(),
    )
    finish = next(row for row in comparison.json()["items"] if row["property_key"] == "finish")
    assert finish["mapped_value"] == "black"
    assert finish["status"] == "match"


def test_property_mapping_boolean_strings_are_normalized(
    matching_client: TestClient, db_session: Session
) -> None:
    product = Product(
        article="P-PROP-BOOL",
        name="Дисплей для Apple iPhone 11",
        brand="Apple",
        category="Дисплеи",
        subject="Дисплеи для телефонов",
        display_has_frame=False,
    )
    item = CompetitorItem(
        competitor="moba",
        external_id="LCD-PROP-BOOL",
        name="Дисплей для iPhone 11",
        item_type="display",
        category_group="display",
        parsed_device_model="iphone 11",
        attrs_json={"frame": "нет"},
        availability=True,
    )
    db_session.add_all([product, item])
    db_session.commit()

    profiles = matching_client.get("/api/matching/property-profiles", auth=_auth())
    display_profile = next(row for row in profiles.json() if row["code"] == "display")
    response = matching_client.post(
        "/api/matching/property-rules",
        json={
            "profile_id": display_profile["id"],
            "property_key": "frame_text",
            "label": "Рамка текстом",
            "product_field": "display.has_frame",
            "competitor_field": "attrs.frame",
            "comparison_mode": "boolean",
            "severity": "review",
            "sort_order": 500,
        },
        auth=_auth(),
    )
    assert response.status_code == 200

    comparison = matching_client.get(
        f"/api/matching/products/{product.id}/candidates/{item.id}/properties",
        auth=_auth(),
    )
    frame = next(row for row in comparison.json()["items"] if row["property_key"] == "frame_text")
    assert frame["status"] == "match"


def test_property_mapping_restore_default_and_suggestions(
    matching_client: TestClient, db_session: Session
) -> None:
    item = CompetitorItem(
        competitor="moba",
        external_id="LCD-PROP-SUGGEST",
        name="Дисплей для iPhone 11 verde",
        item_type="display",
        category_group="display",
        attrs_json={"finish": "verde"},
        availability=True,
    )
    db_session.add(item)
    db_session.commit()

    profiles = matching_client.get("/api/matching/property-profiles", auth=_auth())
    display_profile = next(row for row in profiles.json() if row["code"] == "display")
    rules = matching_client.get(
        "/api/matching/property-rules",
        params={"profile_code": "display"},
        auth=_auth(),
    )
    model_rule = next(row for row in rules.json() if row["property_key"] == "model")

    drifted = matching_client.patch(
        f"/api/matching/property-rules/{model_rule['id']}",
        json={"product_field": "display.quality"},
        auth=_auth(),
    )
    assert drifted.status_code == 200
    assert drifted.json()["has_default_drift"] is True

    restored = matching_client.post(
        f"/api/matching/property-rules/{model_rule['id']}/restore-default",
        auth=_auth(),
    )
    assert restored.status_code == 200
    assert restored.json()["product_field"] == "compatibility.model"
    assert restored.json()["has_default_drift"] is False

    custom_rule = matching_client.post(
        "/api/matching/property-rules",
        json={
            "profile_id": display_profile["id"],
            "property_key": "finish",
            "label": "Отделка",
            "product_field": "display.color",
            "competitor_field": "attrs.finish",
            "comparison_mode": "mapped_value",
            "severity": "review",
            "sort_order": 500,
        },
        auth=_auth(),
    )
    assert custom_rule.status_code == 200

    suggestions = matching_client.get(
        "/api/matching/property-value-suggestions",
        params={"profile_code": "display", "rule_id": custom_rule.json()["id"]},
        auth=_auth(),
    )
    assert suggestions.status_code == 200
    assert any(row["competitor_value"] == "verde" for row in suggestions.json())


def test_property_mapping_accepts_safe_value_suggestions(
    matching_client: TestClient, db_session: Session
) -> None:
    item = CompetitorItem(
        competitor="moba",
        external_id="LCD-PROP-SAFE-SUGGEST",
        name="Дисплей для iPhone 11 black",
        item_type="display",
        category_group="display",
        attrs_json={"finish": "black"},
        availability=True,
    )
    db_session.add(item)
    db_session.commit()

    profiles = matching_client.get("/api/matching/property-profiles", auth=_auth())
    display_profile = next(row for row in profiles.json() if row["code"] == "display")
    custom_rule = matching_client.post(
        "/api/matching/property-rules",
        json={
            "profile_id": display_profile["id"],
            "property_key": "finish",
            "label": "Отделка",
            "product_field": "display.color",
            "competitor_field": "attrs.finish",
            "comparison_mode": "mapped_value",
            "severity": "review",
            "sort_order": 500,
        },
        auth=_auth(),
    )
    assert custom_rule.status_code == 200
    rule_id = custom_rule.json()["id"]

    suggestions = matching_client.get(
        "/api/matching/property-value-suggestions",
        params={"profile_code": "display", "rule_id": rule_id},
        auth=_auth(),
    )
    assert suggestions.status_code == 200
    black = next(row for row in suggestions.json() if row["competitor_value"] == "black")
    assert black["safe_auto"] is True
    assert black["suggested_mapped_value"] == "black"

    accepted = matching_client.post(
        "/api/matching/property-value-suggestions/accept-safe",
        json={"profile_code": "display", "rule_id": rule_id},
        auth=_auth(),
    )
    assert accepted.status_code == 200
    assert accepted.json()["created_count"] == 1

    value_maps = matching_client.get(
        "/api/matching/property-value-maps",
        params={"rule_id": rule_id},
        auth=_auth(),
    )
    assert value_maps.status_code == 200
    assert any(
        row["competitor_source"] == "moba"
        and row["competitor_value"] == "black"
        and row["mapped_value"] == "black"
        for row in value_maps.json()
    )


def test_property_mapping_non_display_profiles_use_model_compatibility(
    matching_client: TestClient, db_session: Session
) -> None:
    phone_model = PhoneModel(brand="apple", model_name="iphone 11")
    product = Product(
        article="P-PROP-BAT-COMPAT",
        name="Аккумулятор для Apple iPhone 11",
        brand="Apple",
        category="АКБ",
        subject="Аккумуляторы для телефонов",
    )
    item = CompetitorItem(
        competitor="moba",
        external_id="BAT-PROP-COMPAT",
        name="АКБ iPhone 11",
        item_type="battery",
        category_group="battery",
        availability=True,
    )
    db_session.add_all([phone_model, product, item])
    db_session.flush()
    db_session.add_all(
        [
            ProductPhoneModel(
                product_id=product.id,
                phone_model_id=phone_model.id,
                source="test",
                raw_value="Apple iPhone 11",
            ),
            CompetitorItemCompatibility(
                competitor_item_id=item.id,
                phone_model_id=phone_model.id,
                device_brand="apple",
                device_model="iphone 11",
                source="test",
            ),
        ]
    )
    db_session.commit()

    rules = matching_client.get(
        "/api/matching/property-rules",
        params={"profile_code": "battery"},
        auth=_auth(),
    )
    assert rules.status_code == 200
    model_rule = next(row for row in rules.json() if row["property_key"] == "model")
    assert model_rule["label"] == "Совместимость модели"
    assert model_rule["product_field"] == "compatibility.model"
    assert model_rule["competitor_field"] == "compatibility.model"
    assert model_rule["comparison_mode"] == "set_overlap"

    comparison = matching_client.get(
        f"/api/matching/products/{product.id}/candidates/{item.id}/properties",
        params={"profile_code": "battery"},
        auth=_auth(),
    )
    assert comparison.status_code == 200
    model = next(row for row in comparison.json()["items"] if row["property_key"] == "model")
    assert model["status"] == "match"

    suggestions = matching_client.get(
        "/api/matching/property-value-suggestions",
        params={"profile_code": "battery", "rule_id": model_rule["id"]},
        auth=_auth(),
    )
    assert suggestions.status_code == 200
    assert any(row["competitor_value"] == "iphone_11" for row in suggestions.json())


def test_property_value_map_rejects_compatibility_model_rule(
    matching_client: TestClient,
) -> None:
    rules = matching_client.get(
        "/api/matching/property-rules",
        params={"profile_code": "display"},
        auth=_auth(),
    )
    assert rules.status_code == 200
    model_rule = next(row for row in rules.json() if row["property_key"] == "model")

    response = matching_client.post(
        "/api/matching/property-value-maps",
        json={
            "rule_id": model_rule["id"],
            "competitor_value": "iphone_11",
            "mapped_value": "iphone_11",
        },
        auth=_auth(),
    )

    assert response.status_code == 400
    assert "compatibility model values" in response.json()["detail"]


def test_compatibility_mapping_applies_product_raw_value_to_multiple_models(
    matching_client: TestClient, db_session: Session
) -> None:
    brands = matching_client.get("/api/matching/compatibility/brands", auth=_auth())
    assert brands.status_code == 200
    apple_id = next(row["id"] for row in brands.json() if row["code"] == "apple")
    created_models = []
    for model_name in ("iphone 11", "iphone 11 pro"):
        response = matching_client.post(
            "/api/matching/compatibility/models",
            json={"brand_id": apple_id, "model_name": model_name},
            auth=_auth(),
        )
        assert response.status_code == 200
        created_models.append(response.json())

    product = Product(
        article="P-COMPAT-MULTI",
        name="Дисплей для Apple iPhone 11 / 11 Pro",
        brand="Apple",
        category="Дисплеи",
        subject="Дисплеи для телефонов",
    )
    db_session.add(product)
    db_session.flush()
    db_session.add(
        ProductCompatibility(
            product_id=product.id,
            value="Apple iPhone 11 / 11 Pro",
            source="onec",
        )
    )
    db_session.commit()

    unresolved = matching_client.get(
        "/api/matching/compatibility/unresolved",
        params={"entity_type": "product", "q": "11 Pro"},
        auth=_auth(),
    )
    assert unresolved.status_code == 200
    row = next(item for item in unresolved.json() if item["entity_id"] == product.id)
    payload = {
        "entity_type": row["entity_type"],
        "source": row["source"],
        "raw_value": row["raw_value"],
        "brand_id": apple_id,
        "target_phone_model_ids": [item["id"] for item in created_models],
    }

    preview = matching_client.post(
        "/api/matching/compatibility/unresolved/preview",
        json=payload,
        auth=_auth(),
    )
    assert preview.status_code == 200
    assert preview.json()["affected_count"] == 1

    applied = matching_client.post(
        "/api/matching/compatibility/unresolved/apply",
        json={**payload, "preview_token": preview.json()["preview_token"], "scope": "previewed"},
        auth=_auth(),
    )
    assert applied.status_code == 200
    assert applied.json()["product_links_created"] == 2

    links = (
        db_session.query(ProductPhoneModel).filter(ProductPhoneModel.product_id == product.id).all()
    )
    assert {link.phone_model_id for link in links} == {item["id"] for item in created_models}
    assert all(link.raw_value == "Apple iPhone 11 / 11 Pro" for link in links)
    assert all(link.is_manual for link in links)


def test_compatibility_unresolved_groups_apply_all_product_rows(
    matching_client: TestClient, db_session: Session
) -> None:
    brands = matching_client.get("/api/matching/compatibility/brands", auth=_auth())
    assert brands.status_code == 200
    apple_id = next(row["id"] for row in brands.json() if row["code"] == "apple")
    model_response = matching_client.post(
        "/api/matching/compatibility/models",
        json={"brand_id": apple_id, "model_name": "iphone 11"},
        auth=_auth(),
    )
    assert model_response.status_code == 200
    model_id = model_response.json()["id"]

    products = [
        Product(
            article=f"P-GROUP-{idx}",
            name=f"Дисплей для Apple iPhone 11 черный {idx}",
            brand="Apple",
            category="Дисплеи",
            subject="Дисплеи для телефонов",
        )
        for idx in range(3)
    ]
    db_session.add_all(products)
    db_session.flush()
    db_session.add_all(
        [
            ProductCompatibility(product_id=product.id, value="Apple iPhone 11", source="onec")
            for product in products
        ]
    )
    db_session.commit()

    groups = matching_client.get(
        "/api/matching/compatibility/unresolved-groups",
        params={"entity_type": "product", "q": "iphone 11"},
        auth=_auth(),
    )
    assert groups.status_code == 200
    group = next(row for row in groups.json() if row["raw_value"] == "Apple iPhone 11")
    assert group["affected_count"] == 3
    assert group["product_count"] == 3
    assert group["examples"]
    assert model_id in {row["id"] for row in group["suggested_phone_models"]}

    preview = matching_client.post(
        "/api/matching/compatibility/unresolved/preview",
        json={
            "group_key": group["group_key"],
            "brand_id": apple_id,
            "target_phone_model_ids": [model_id],
        },
        auth=_auth(),
    )
    assert preview.status_code == 200
    assert preview.json()["affected_count"] == 3
    assert preview.json()["target_phone_models"][0]["id"] == model_id
    assert len(preview.json()["items"]) <= 5

    applied = matching_client.post(
        "/api/matching/compatibility/unresolved/apply",
        json={
            "group_key": group["group_key"],
            "brand_id": apple_id,
            "target_phone_model_ids": [model_id],
            "preview_token": preview.json()["preview_token"],
            "scope": "group",
        },
        auth=_auth(),
    )
    assert applied.status_code == 200
    assert applied.json()["affected_count"] == 3
    assert applied.json()["product_links_created"] == 3

    links = (
        db_session.query(ProductPhoneModel)
        .filter(ProductPhoneModel.product_id.in_([product.id for product in products]))
        .all()
    )
    assert len(links) == 3
    assert {link.phone_model_id for link in links} == {model_id}


def test_compatibility_unresolved_group_ranks_iphone_base_suggestion_safely(
    matching_client: TestClient, db_session: Session
) -> None:
    brands = matching_client.get("/api/matching/compatibility/brands", auth=_auth())
    assert brands.status_code == 200
    apple_id = next(row["id"] for row in brands.json() if row["code"] == "apple")
    created: dict[tuple[str, str | None], int] = {}
    for model_name, variant in (
        ("iphone 11", None),
        ("iphone 11", "pro"),
        ("iphone 11", "pro max"),
        ("iphone 11 a2221", None),
        ("iphone 11 a2221/or100", None),
    ):
        response = matching_client.post(
            "/api/matching/compatibility/models",
            json={"brand_id": apple_id, "model_name": model_name, "variant": variant},
            auth=_auth(),
        )
        assert response.status_code == 200
        created[(model_name, variant)] = response.json()["id"]

    product = Product(
        article="P-IPHONE-11-BASE",
        name="Дисплей для Apple iPhone 11",
        brand="Apple",
        category="Дисплеи",
        subject="Дисплеи для телефонов",
    )
    db_session.add(product)
    db_session.flush()
    db_session.add(
        ProductCompatibility(product_id=product.id, value="Apple iPhone 11", source="onec")
    )
    db_session.commit()

    groups = matching_client.get(
        "/api/matching/compatibility/unresolved-groups",
        params={"entity_type": "product", "q": "iphone 11"},
        auth=_auth(),
    )
    assert groups.status_code == 200
    group = next(row for row in groups.json() if row["raw_value"] == "Apple iPhone 11")
    suggestions = group["suggested_phone_models"]

    assert group["safe_auto_model_id"] == created[("iphone 11", None)]
    assert suggestions[0]["id"] == created[("iphone 11", None)]
    assert suggestions[0]["suggestion_kind"] == "exact_base"
    kinds_by_id = {row["id"]: row["suggestion_kind"] for row in suggestions}
    assert kinds_by_id[created[("iphone 11", "pro")]] == "related_family"
    assert kinds_by_id[created[("iphone 11", "pro max")]] == "related_family"
    assert kinds_by_id[created[("iphone 11 a2221", None)]] == "hardware_variant"
    assert kinds_by_id[created[("iphone 11 a2221/or100", None)]] == "hardware_variant"


def test_compatibility_unresolved_groups_include_noise_and_block_reason(
    matching_client: TestClient, db_session: Session
) -> None:
    products = [
        Product(
            article=f"P-NOISE-{idx}",
            name=f"Дисплей без корректной совместимости {idx}",
            brand="Apple",
            category="Дисплеи",
            subject="Дисплеи для телефонов",
        )
        for idx in range(2)
    ]
    db_session.add_all(products)
    db_session.flush()
    db_session.add_all(
        [
            ProductCompatibility(product_id=product.id, value="<>", source="onec")
            for product in products
        ]
    )
    db_session.commit()

    groups = matching_client.get(
        "/api/matching/compatibility/unresolved-groups",
        params={"entity_type": "product", "without_brand": True},
        auth=_auth(),
    )
    assert groups.status_code == 200
    group = next(row for row in groups.json() if row["raw_value"] == "<>")
    assert group["affected_count"] == 2
    assert group["brand_id"] is None
    assert group["is_noise_candidate"] is True

    blocked = matching_client.post(
        "/api/matching/compatibility/unresolved/block",
        json={"group_key": group["group_key"], "reason": "noise", "notes": "bad onec raw"},
        auth=_auth(),
    )
    assert blocked.status_code == 200
    assert blocked.json()["affected_count"] == 2
    assert blocked.json()["decisions_created"] == 2

    decisions = (
        db_session.query(CompatibilityMappingDecision)
        .filter(CompatibilityMappingDecision.raw_value == "<>")
        .all()
    )
    assert len(decisions) == 2
    assert {decision.action for decision in decisions} == {"block"}
    assert all((decision.notes or "").startswith("reason=noise") for decision in decisions)


def test_compatibility_brand_aliases_can_be_listed_and_disabled(
    matching_client: TestClient, db_session: Session
) -> None:
    brands = matching_client.get("/api/matching/compatibility/brands", auth=_auth())
    assert brands.status_code == 200
    apple_id = next(row["id"] for row in brands.json() if row["code"] == "apple")

    created = matching_client.post(
        "/api/matching/compatibility/brand-aliases",
        json={"brand_id": apple_id, "raw_value": "AAPL", "source": "manual"},
        auth=_auth(),
    )
    assert created.status_code == 200
    alias = (
        db_session.query(DeviceBrandAlias)
        .filter(DeviceBrandAlias.brand_id == apple_id, DeviceBrandAlias.raw_value == "AAPL")
        .one()
    )

    aliases = matching_client.get(
        "/api/matching/compatibility/brand-aliases",
        params={"brand_id": apple_id, "q": "aapl"},
        auth=_auth(),
    )
    assert aliases.status_code == 200
    assert aliases.json()[0]["id"] == alias.id
    assert aliases.json()[0]["is_active"] is True

    patched = matching_client.patch(
        f"/api/matching/compatibility/brand-aliases/{alias.id}",
        json={"is_active": False},
        auth=_auth(),
    )
    assert patched.status_code == 200
    assert patched.json()["is_active"] is False

    active_aliases = matching_client.get(
        "/api/matching/compatibility/brand-aliases",
        params={"brand_id": apple_id, "q": "aapl"},
        auth=_auth(),
    )
    assert active_aliases.status_code == 200
    assert active_aliases.json() == []


def test_compatibility_history_returns_recent_group_actions(
    matching_client: TestClient, db_session: Session
) -> None:
    products = [
        Product(
            article=f"P-HISTORY-{idx}",
            name=f"Дисплей для Apple iPhone 12 {idx}",
            brand="Apple",
            category="Дисплеи",
            subject="Дисплеи для телефонов",
        )
        for idx in range(2)
    ]
    db_session.add_all(products)
    db_session.flush()
    db_session.add_all(
        [
            ProductCompatibility(product_id=product.id, value="Apple iPhone 12", source="onec")
            for product in products
        ]
    )
    db_session.commit()

    group = matching_client.get(
        "/api/matching/compatibility/unresolved-groups",
        params={"entity_type": "product", "q": "iphone 12"},
        auth=_auth(),
    ).json()[0]
    blocked = matching_client.post(
        "/api/matching/compatibility/unresolved/block",
        json={"group_key": group["group_key"], "reason": "not_supported"},
        auth=_auth(),
    )
    assert blocked.status_code == 200

    history = matching_client.get("/api/matching/compatibility/history", auth=_auth())
    assert history.status_code == 200
    first = history.json()[0]
    assert first["action"] == "block"
    assert first["raw_value"] == "Apple iPhone 12"
    assert first["affected_count"] == 2
    assert first["reason"] == "not_supported"


def test_compatibility_mapping_applies_competitor_raw_value_to_multiple_models(
    matching_client: TestClient, db_session: Session
) -> None:
    brands = matching_client.get("/api/matching/compatibility/brands", auth=_auth())
    assert brands.status_code == 200
    apple_id = next(row["id"] for row in brands.json() if row["code"] == "apple")
    model_ids = []
    for model_name in ("iphone 12", "iphone 12 mini"):
        response = matching_client.post(
            "/api/matching/compatibility/models",
            json={"brand_id": apple_id, "model_name": model_name},
            auth=_auth(),
        )
        assert response.status_code == 200
        model_ids.append(response.json()["id"])

    item = CompetitorItem(
        competitor="moba",
        external_id="COMPAT-MULTI",
        name="Дисплей iPhone 12 / 12 mini",
        item_type="display",
        category_group="display",
        availability=True,
    )
    db_session.add(item)
    db_session.flush()
    compat = CompetitorItemCompatibility(
        competitor_item_id=item.id,
        device_brand="apple",
        device_brand_id=apple_id,
        device_model="iphone 12 / 12 mini",
        source="parser",
    )
    db_session.add(compat)
    db_session.commit()

    unresolved = matching_client.get(
        "/api/matching/compatibility/unresolved",
        params={"entity_type": "competitor_item", "q": "12 mini"},
        auth=_auth(),
    )
    assert unresolved.status_code == 200
    row = next(item for item in unresolved.json() if item["entity_id"] == compat.id)
    payload = {
        "entity_type": row["entity_type"],
        "source": row["source"],
        "raw_value": row["raw_value"],
        "raw_brand": row["raw_brand"],
        "raw_model": row["raw_model"],
        "raw_variant": row["raw_variant"],
        "brand_id": apple_id,
        "target_phone_model_ids": model_ids,
    }

    preview = matching_client.post(
        "/api/matching/compatibility/unresolved/preview",
        json=payload,
        auth=_auth(),
    )
    assert preview.status_code == 200
    applied = matching_client.post(
        "/api/matching/compatibility/unresolved/apply",
        json={**payload, "preview_token": preview.json()["preview_token"], "scope": "previewed"},
        auth=_auth(),
    )
    assert applied.status_code == 200
    assert applied.json()["competitor_links_created"] == 2

    linked = (
        db_session.query(CompetitorItemCompatibility)
        .filter(
            CompetitorItemCompatibility.competitor_item_id == item.id,
            CompetitorItemCompatibility.phone_model_id.in_(model_ids),
        )
        .all()
    )
    assert {row.phone_model_id for row in linked} == set(model_ids)


def test_candidate_search_property_summary_is_opt_in(
    matching_client: TestClient, db_session: Session
) -> None:
    product = Product(
        article="P-PROP-SUMMARY",
        name="Дисплей для Apple iPhone 11 черный",
        brand="Apple",
        category="Дисплеи",
        subject="Дисплеи для телефонов",
        color="черный",
    )
    item = CompetitorItem(
        competitor="moba",
        external_id="LCD-PROP-WHITE",
        name="Дисплей для iPhone 11 белый",
        item_type="display",
        category_group="display",
        parsed_device_model="iphone 11",
        attrs_color="белый",
        availability=True,
    )
    db_session.add_all([product, item])
    db_session.commit()

    plain = matching_client.get(
        f"/api/matching/products/{product.id}/candidate-search",
        params={"q": "iphone 11 белый", "item_type": "display"},
        auth=_auth(),
    )
    assert plain.status_code == 200
    assert plain.json()["items"][0]["property_summary"] is None

    enriched = matching_client.get(
        f"/api/matching/products/{product.id}/candidate-search",
        params={
            "q": "iphone 11 белый",
            "item_type": "display",
            "include_property_summary": True,
        },
        auth=_auth(),
    )
    assert enriched.status_code == 200
    summary = enriched.json()["items"][0]["property_summary"]
    assert summary["status"] == "conflict"
    assert "Цвет" in summary["conflicts"]
