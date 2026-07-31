from __future__ import annotations

from datetime import date

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.api.customer_price_types as cpt_api
from app.core.config import get_settings
from app.domains.customer_price_types import CustomerPriceTypeAccessScope
from app.main import app
from app.models import Base, CustomerPriceTypeProfile, TelephonyUserLineSnapshot
from app.models.staff_member import StaffMember
from app.services import bitrix_customer_price_types_auth as auth


def _settings(**over):
    base = {
        "customer_price_type_bitrix_enabled": True,
        "customer_price_type_bitrix_allowed_domains": ["portal.bitrix24.ru"],
        "customer_price_type_bitrix_allowed_member_ids": ["member-1"],
        "customer_price_type_bitrix_full_access_user_ids": [],
        "customer_price_type_access_rules_json": None,
        "customer_price_type_bitrix_session_secret": "test-secret",
        "customer_price_type_bitrix_session_ttl_seconds": 3600,
        "customer_price_type_bitrix_rest_timeout_seconds": 6.0,
    }
    base.update(over)
    return get_settings().model_copy(update=base)


def test_session_token_roundtrip_preserves_scope():
    settings = _settings()
    access = CustomerPriceTypeAccessScope(
        actor="bitrix:7", role="manager", owner_ref="0xABC", can_view_money=False
    )
    token, _ = auth.create_customer_price_type_session_token(
        domain="portal.bitrix24.ru",
        member_id="member-1",
        user_id="7",
        user_name="Иван",
        access=access,
        settings=settings,
        now=1000,
    )
    scope = auth.verify_customer_price_type_session_token(token, settings=settings, now=1100)
    assert scope.role == "manager"
    assert scope.owner_ref == "0xabc"
    assert scope.can_view_money is False
    assert scope.is_full is False


def test_expired_session_rejected():
    settings = _settings()
    access = CustomerPriceTypeAccessScope(actor="x", role="network_head", can_view_money=True)
    token, _ = auth.create_customer_price_type_session_token(
        domain="portal.bitrix24.ru",
        member_id="member-1",
        user_id="7",
        user_name=None,
        access=access,
        settings=settings,
        now=1000,
    )
    with pytest.raises(HTTPException) as info:
        auth.verify_customer_price_type_session_token(token, settings=settings, now=1000 + 3601)
    assert info.value.status_code == 401


def test_tampered_signature_rejected():
    settings = _settings()
    access = CustomerPriceTypeAccessScope(actor="x", role="network_head", can_view_money=True)
    token, _ = auth.create_customer_price_type_session_token(
        domain="portal.bitrix24.ru",
        member_id="member-1",
        user_id="7",
        user_name=None,
        access=access,
        settings=settings,
        now=1000,
    )
    tampered = token[:-2] + ("zz" if not token.endswith("zz") else "aa")
    with pytest.raises(HTTPException) as info:
        auth.verify_customer_price_type_session_token(tampered, settings=settings, now=1100)
    assert info.value.status_code == 401


def test_disabled_app_blocks_launch():
    settings = _settings(customer_price_type_bitrix_enabled=False)
    with pytest.raises(HTTPException) as info:
        auth.ensure_bitrix_launch_allowed(
            domain="portal.bitrix24.ru", member_id="member-1", settings=settings
        )
    assert info.value.status_code == 403


def test_launch_domain_not_allowed():
    settings = _settings()
    with pytest.raises(HTTPException) as info:
        auth.ensure_bitrix_launch_allowed(
            domain="evil.bitrix24.ru", member_id="member-1", settings=settings
        )
    assert info.value.status_code == 403


def test_resolve_full_access_user():
    settings = _settings(customer_price_type_bitrix_full_access_user_ids=["7"])
    scope = auth.resolve_customer_price_type_access(bitrix_user_id="7", settings=settings)
    assert scope.role == "network_head"
    assert scope.is_full is True
    assert scope.can_view_money is True


def test_resolve_network_head_by_department():
    rules = '{"roles":[{"role":"network_head","department_ids":["1"]}]}'
    settings = _settings(customer_price_type_access_rules_json=rules)
    scope = auth.resolve_customer_price_type_access(
        bitrix_user_id="42", department_ids=("1",), settings=settings
    )
    assert scope.role == "network_head"
    assert scope.is_full is True
    assert scope.can_view_money is True


def test_resolve_network_head_by_bitrix_headship():
    rules = '{"roles":[{"role":"network_head","headed_department_ids":["3269"]}]}'
    settings = _settings(customer_price_type_access_rules_json=rules)
    scope = auth.resolve_customer_price_type_access(
        bitrix_user_id="130751",
        department_ids=("3227", "3269"),
        headed_department_ids=("3269",),
        settings=settings,
    )
    assert scope.role == "network_head"
    assert scope.is_full is True
    assert scope.can_view_money is True


def test_network_head_rule_does_not_grant_access_to_regular_department_member():
    rules = '{"roles":[{"role":"network_head","headed_department_ids":["3269"]}]}'
    settings = _settings(customer_price_type_access_rules_json=rules)
    with pytest.raises(HTTPException) as info:
        auth.resolve_customer_price_type_access(
            bitrix_user_id="42",
            department_ids=("3269",),
            headed_department_ids=(),
            settings=settings,
        )
    assert info.value.status_code == 403


def test_resolve_executive_by_bitrix_headship():
    rules = '{"roles":[{"role":"executive","headed_department_ids":["3227"]}]}'
    settings = _settings(customer_price_type_access_rules_json=rules)
    scope = auth.resolve_customer_price_type_access(
        bitrix_user_id="4241",
        department_ids=("3227",),
        headed_department_ids=("3227",),
        settings=settings,
    )
    assert scope.role == "executive"
    assert scope.is_full is True
    assert scope.can_view_money is True


def test_resolve_finance_by_department():
    rules = '{"roles":[{"role":"finance","department_ids":["12"]}]}'
    settings = _settings(customer_price_type_access_rules_json=rules)
    scope = auth.resolve_customer_price_type_access(
        bitrix_user_id="9", department_ids=("12",), settings=settings
    )
    assert scope.role == "finance"
    assert scope.can_view_money is True


def test_resolve_manager_by_current_onec_owner_ref():
    settings = _settings()
    scope = auth.resolve_customer_price_type_access(
        bitrix_user_id="77",
        manager_owner_ref="0xABC",
        settings=settings,
    )
    assert scope.role == "manager"
    assert scope.owner_ref == "0xabc"
    assert scope.can_view_money is False


def test_existing_telephony_mapping_resolves_manager_owner_ref():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            session.add(
                CustomerPriceTypeProfile(
                    counterparty_ref="0xcustomer",
                    counterparty_code="C-1",
                    owner_ref="0xabc",
                )
            )
            session.add_all(
                [
                    TelephonyUserLineSnapshot(
                        snapshot_date=date(2026, 7, 30),
                        mapping_source="test",
                        user_ref_hex="0xold",
                        user_name="Менеджер",
                        bitrix_user_id="77",
                        is_marked=False,
                        has_extension=False,
                        has_bitrix=True,
                    ),
                    TelephonyUserLineSnapshot(
                        snapshot_date=date(2026, 7, 31),
                        mapping_source="test",
                        user_ref_hex="0xABC",
                        user_name="Менеджер",
                        bitrix_user_id="77",
                        is_marked=False,
                        has_extension=False,
                        has_bitrix=True,
                    ),
                ]
            )
            session.commit()
            owner_ref = auth.resolve_customer_price_type_manager_owner_ref(
                session,
                bitrix_user_id="77",
            )
        assert owner_ref == "0xabc"
    finally:
        engine.dispose()


def test_ambiguous_telephony_mapping_does_not_grant_manager_scope():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            session.add_all(
                [
                    TelephonyUserLineSnapshot(
                        snapshot_date=date(2026, 7, 31),
                        mapping_source="test",
                        user_ref_hex=owner_ref,
                        user_name="Неоднозначный пользователь",
                        bitrix_user_id="77",
                        is_marked=False,
                        has_extension=False,
                        has_bitrix=True,
                    )
                    for owner_ref in ("0xABC", "0xDEF")
                ]
            )
            session.commit()
            owner_ref = auth.resolve_customer_price_type_manager_owner_ref(
                session,
                bitrix_user_id="77",
            )
        assert owner_ref is None
    finally:
        engine.dispose()


def test_resolve_department_head_by_headship():
    rules = '{"roles":[{"role":"department_head","head_department_refs":{"7":["0xDEAD"]}}]}'
    settings = _settings(customer_price_type_access_rules_json=rules)
    scope = auth.resolve_customer_price_type_access(
        bitrix_user_id="5",
        department_ids=("7",),
        headed_department_ids=("7",),
        settings=settings,
    )
    assert scope.role == "department_head"
    assert scope.department_refs == ("0xdead",)
    assert scope.can_view_money is False
    assert scope.is_full is False


def test_resolve_department_head_reuses_dynamic_department_refs():
    settings = _settings()
    scope = auth.resolve_customer_price_type_access(
        bitrix_user_id="5",
        headed_department_ids=("3278",),
        headed_department_refs=("0xABC", "0xDEF"),
        settings=settings,
    )
    assert scope.role == "department_head"
    assert scope.department_refs == ("0xabc", "0xdef")
    assert scope.can_view_money is False


def test_existing_staff_mapping_resolves_price_type_department_refs():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            session.add(
                StaffMember(
                    source="test",
                    external_ref="employee-1",
                    full_name="Иван Пример",
                    department_ref="0xABC",
                    department_name="Радиорынок «Электромир»",
                    employment_status="active",
                )
            )
            session.commit()
            refs = auth.resolve_customer_price_type_department_refs(
                session,
                department_names={"Радиорынок «Электромир»"},
            )
        assert "0xabc" in refs
    finally:
        engine.dispose()


def test_headed_departments_fall_back_to_read_only_webhook(monkeypatch):
    settings = _settings(bitrix_box_webhook_base="https://hook.example/rest/1/token")
    calls = []

    def fake_load(*, url, payload, timeout):
        calls.append((url, payload, timeout))
        if len(calls) == 1:
            return []
        return [{"ID": "3278", "NAME": "Радиорынок «Электромир»"}]

    monkeypatch.setattr(auth, "_load_headed_department_rows", fake_load)
    departments = auth.load_bitrix_headed_departments(
        domain="portal.bitrix24.ru",
        access_token="launch-token",
        user_id="7",
        settings=settings,
    )
    assert departments == (
        auth.BitrixDepartment(
            department_id="3278",
            name="Радиорынок «Электромир»",
        ),
    )
    assert calls[0][0] == "https://portal.bitrix24.ru/rest/department.get.json"
    assert calls[1][0] == "https://hook.example/rest/1/token/department.get.json"
    assert calls[1][1] == {"FILTER": {"UF_HEAD": "7"}}


def test_resolve_regular_member_denied():
    # member of an unmapped department, heads nothing -> no management position -> 403
    rules = '{"roles":[{"role":"finance","department_ids":["12"]}]}'
    settings = _settings(customer_price_type_access_rules_json=rules)
    with pytest.raises(HTTPException) as info:
        auth.resolve_customer_price_type_access(
            bitrix_user_id="77", department_ids=("99",), settings=settings
        )
    assert info.value.status_code == 403


def test_resolve_no_position_denied():
    settings = _settings()
    with pytest.raises(HTTPException) as info:
        auth.resolve_customer_price_type_access(bitrix_user_id="999", settings=settings)
    assert info.value.status_code == 403


def test_session_endpoint_issues_valid_token(monkeypatch):
    settings = _settings(customer_price_type_bitrix_full_access_user_ids=["7"])
    monkeypatch.setattr(cpt_api, "get_settings", lambda: settings)
    monkeypatch.setattr(
        cpt_api,
        "load_bitrix_current_user",
        lambda **_: auth.BitrixUser(user_id="7", name="Иван Пример", department_ids=("1",)),
    )
    monkeypatch.setattr(cpt_api, "load_bitrix_headed_departments", lambda **_: ())
    monkeypatch.setattr(
        cpt_api,
        "resolve_customer_price_type_department_refs",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(
        cpt_api,
        "resolve_customer_price_type_manager_owner_ref",
        lambda *_args, **_kwargs: None,
    )
    client = TestClient(app)
    response = client.post(
        "/api/customer-price-types/session",
        json={"access_token": "abc", "domain": "portal.bitrix24.ru", "member_id": "member-1"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["user"]["role"] == "network_head"
    assert body["user"]["can_view_money"] is True
    assert body["token_type"] == "Bearer"
    scope = auth.verify_customer_price_type_session_token(body["session_token"], settings=settings)
    assert scope.role == "network_head"


def test_session_endpoint_issues_manager_scope_from_onec_owner(monkeypatch):
    settings = _settings()
    monkeypatch.setattr(cpt_api, "get_settings", lambda: settings)
    monkeypatch.setattr(
        cpt_api,
        "load_bitrix_current_user",
        lambda **_: auth.BitrixUser(user_id="77", name="Менеджер", department_ids=("99",)),
    )
    monkeypatch.setattr(cpt_api, "load_bitrix_headed_departments", lambda **_: ())
    monkeypatch.setattr(
        cpt_api,
        "resolve_customer_price_type_department_refs",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(
        cpt_api,
        "resolve_customer_price_type_manager_owner_ref",
        lambda *_args, **_kwargs: "0xabc",
    )
    client = TestClient(app)
    response = client.post(
        "/api/customer-price-types/session",
        json={"access_token": "abc", "domain": "portal.bitrix24.ru", "member_id": "member-1"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["user"]["role"] == "manager"
    assert body["user"]["can_view_money"] is False
    scope = auth.verify_customer_price_type_session_token(body["session_token"], settings=settings)
    assert scope.role == "manager"
    assert scope.owner_ref == "0xabc"
