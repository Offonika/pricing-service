from __future__ import annotations

import re
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models import (
    CardBalanceCashbox,
    CardBalanceReconciliation,
    StaffMember,
    StoreShiftFact,
    StoreShiftPlan,
)
from app.services import card_balance_bitrix
from app.services import card_balance_reconciliation as reconciliation_service
from app.services.card_balance_onec import OneCCardBalanceExtractor, clean_string
from app.services.staffing import (
    ATTENDANCE_ABSENT,
    ATTENDANCE_ASSIGNED,
    ATTENDANCE_CONFIRMED,
)

CARD_EMPLOYEE_STOPWORDS = {"карта", "сбер", "т", "тбанк", "т-банк", "банк"}
WORKDAY_ATTENDANCE_STATUSES = {
    ATTENDANCE_ASSIGNED,
    ATTENDANCE_CONFIRMED,
    ATTENDANCE_ABSENT,
}
_BITRIX_USER_SEARCH_CACHE: dict[str, list[dict[str, Any]]] = {}
_BITRIX_USER_BY_ID_CACHE: dict[str, dict[str, Any] | None] = {}


def _get_app_engine():
    return create_engine(get_settings().database_url)


def _get_onec_engine():
    settings = get_settings()
    if not settings.onec_database_url:
        raise RuntimeError("ONEC_DATABASE_URL is not configured")
    return create_engine(
        settings.onec_database_url,
        connect_args={
            "timeout": settings.onec_query_timeout_seconds,
            "login_timeout": settings.onec_login_timeout_seconds,
        },
    )


def run_card_balance_onec_cashbox_sync(
    *,
    extractor: OneCCardBalanceExtractor | None = None,
) -> dict[str, int]:
    extractor = extractor or OneCCardBalanceExtractor(_get_onec_engine())
    rows = extractor.fetch_cashbox_registry()
    with Session(_get_app_engine()) as session:
        return reconciliation_service.upsert_cashboxes(session, rows)


def run_card_balance_bitrix_sync(
    *,
    limit: int = 50,
    business_date: date | None = None,
    auto_create_daily: bool | None = None,
    extractor: OneCCardBalanceExtractor | None = None,
) -> dict[str, int | str]:
    settings = get_settings()
    should_auto_create = (
        settings.card_balance_auto_create_daily if auto_create_daily is None else auto_create_daily
    )
    extractor = extractor or OneCCardBalanceExtractor(_get_onec_engine())
    target_business_date = business_date or reconciliation_service.utcnow().date()
    result: dict[str, int | str] = {
        "processed": 0,
        "matched": 0,
        "exceptions": 0,
        "errors": 0,
        "skipped_not_in_pilot": 0,
        "skipped_no_workday_data": 0,
        "skipped_unmapped_bitrix_item": 0,
        "ocr_errors": 0,
    }

    with Session(_get_app_engine()) as session:
        ensure_stats = {
            "created": 0,
            "skipped_existing": 0,
            "skipped_manual_review": 0,
            "skipped_missing_data": 0,
            "skipped_not_in_pilot": 0,
            "skipped_no_workday_data": 0,
            "create_errors": 0,
            "eligible_cashboxes": 0,
        }
        if should_auto_create:
            ensure_stats = _ensure_daily_bitrix_items(
                session,
                business_date=target_business_date,
                settings=settings,
            )
            result["daily_created"] = ensure_stats["created"]
            result["daily_skipped_existing"] = ensure_stats["skipped_existing"]
            result["daily_skipped_manual_review"] = ensure_stats["skipped_manual_review"]
            result["daily_skipped_missing_data"] = ensure_stats["skipped_missing_data"]
            result["daily_skipped_not_in_pilot"] = ensure_stats["skipped_not_in_pilot"]
            result["daily_skipped_no_workday_data"] = ensure_stats["skipped_no_workday_data"]
            result["skipped_not_in_pilot"] = ensure_stats["skipped_not_in_pilot"]
            result["skipped_no_workday_data"] = ensure_stats["skipped_no_workday_data"]
            result["daily_create_errors"] = ensure_stats["create_errors"]

        if should_auto_create or business_date is not None:
            list_limit = max(limit, int(ensure_stats["eligible_cashboxes"]) + 20)
            items = card_balance_bitrix.list_bitrix_items_by_business_date(
                target_business_date,
                settings=settings,
                limit=list_limit,
            )
            result["business_date"] = target_business_date.isoformat()
        else:
            items = card_balance_bitrix.list_bitrix_items(settings=settings, limit=limit)

        decoded_payloads = [
            _enrich_payload_with_cashbox(
                session, card_balance_bitrix.decode_bitrix_item(item, settings=settings)
            )
            for item in items
        ]
        onec_balances = _fetch_balances_for_payloads(extractor, decoded_payloads)
        for item, payload in zip(items, decoded_payloads, strict=False):
            try:
                code = clean_string(payload.get("onec_cashbox_code"))
                business_date = date.fromisoformat(str(payload.get("business_date"))[:10])
                row = card_balance_bitrix.sync_bitrix_item(
                    session,
                    item=item,
                    decoded_payload=payload,
                    onec_balances={code: onec_balances.get((business_date, code))} if code else {},
                    settings=settings,
                )
                result["processed"] += 1
                if row.status == reconciliation_service.STATUS_MATCHED:
                    result["matched"] += 1
                elif row.status in reconciliation_service.EXCEPTION_STATUSES:
                    result["exceptions"] += 1
                if row.status == reconciliation_service.STATUS_UNMAPPED_CARD:
                    result["skipped_unmapped_bitrix_item"] += 1
                if isinstance(row.payload, dict) and clean_string(row.payload.get("ocr_error")):
                    result["ocr_errors"] += 1
            except Exception:
                session.rollback()
                result["errors"] += 1
    return result


def recalculate_reconciliation(
    session: Session,
    *,
    reconciliation_id: int,
    extractor: OneCCardBalanceExtractor | None = None,
) -> CardBalanceReconciliation:
    row = session.scalar(
        select(CardBalanceReconciliation).where(CardBalanceReconciliation.id == reconciliation_id)
    )
    if row is None:
        raise RuntimeError("card balance reconciliation not found")
    extractor = extractor or OneCCardBalanceExtractor(_get_onec_engine())
    onec_balance = None
    if row.onec_cashbox_code:
        balances = extractor.fetch_balance_by_cashbox_codes(
            business_date=row.business_date,
            cashbox_codes=[row.onec_cashbox_code],
        )
        onec_balance = balances.get(row.onec_cashbox_code)
    settings = get_settings()
    if (
        row.bitrix_item_id
        and settings.card_balance_bitrix_webhook_url
        and settings.card_balance_bitrix_entity_type_id
    ):
        item = card_balance_bitrix.get_bitrix_item(row.bitrix_item_id, settings=settings)
        payload = _enrich_payload_with_cashbox(
            session, card_balance_bitrix.decode_bitrix_item(item, settings=settings)
        )
        code = clean_string(payload.get("onec_cashbox_code"))
        return card_balance_bitrix.sync_bitrix_item(
            session,
            item=item,
            decoded_payload=payload,
            onec_balances={code: onec_balance} if code else {},
            settings=settings,
        )
    payload = reconciliation_service.serialize_reconciliation(row)
    updated = reconciliation_service.upsert_reconciliation_from_payload(
        session,
        payload=payload,
        onec_balance=onec_balance,
    )
    return updated


def _enrich_payload_with_cashbox(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    cashbox = None
    code = clean_string(payload.get("onec_cashbox_code"))
    if code:
        cashbox = session.scalar(
            select(CardBalanceCashbox).where(
                CardBalanceCashbox.onec_cashbox_code == code,
                CardBalanceCashbox.is_active.is_(True),
            )
        )
    if cashbox is None:
        cashbox, _ = reconciliation_service.resolve_cashbox(
            session,
            onec_cashbox_code=None,
            card_last4=clean_string(payload.get("card_last4")),
            employee_last_name=clean_string(payload.get("employee_last_name")),
        )
    if cashbox is None:
        return payload
    settings = get_settings()
    employee = _resolve_cashbox_bitrix_employee(session, cashbox, settings=settings)
    enriched = dict(payload)
    enriched["onec_cashbox_code"] = cashbox.onec_cashbox_code
    enriched["onec_cashbox_name"] = cashbox.onec_cashbox_name
    if not clean_string(enriched.get("card_last4")):
        enriched["card_last4"] = clean_string(cashbox.card_last4)
    if not clean_string(enriched.get("employee_last_name")):
        enriched["employee_last_name"] = clean_string(cashbox.employee_last_name)
    if not clean_string(enriched.get("employee_name")):
        enriched["employee_name"] = (
            employee.get("full_name") if employee else clean_string(cashbox.employee_last_name)
        )
    if employee:
        enriched["employee_id"] = employee.get("bitrix_user_id")
    else:
        current_employee_id = clean_string(enriched.get("employee_id"))
        keep_current = False
        if current_employee_id and current_employee_id.isdigit():
            current_user = _load_bitrix_user_by_id(settings=settings, user_id=current_employee_id)
            if current_user and _is_plausible_cashbox_employee_match(cashbox, current_user):
                keep_current = True
        if not keep_current:
            enriched["employee_id"] = None
    return enriched


def _fetch_balances_for_payloads(
    extractor: OneCCardBalanceExtractor,
    payloads: list[dict[str, Any]],
) -> dict[tuple[date, str], Decimal | None]:
    grouped: dict[date, set[str]] = defaultdict(set)
    for payload in payloads:
        code = clean_string(payload.get("onec_cashbox_code"))
        business_date_value = payload.get("business_date")
        if not code or not business_date_value:
            continue
        grouped[date.fromisoformat(str(business_date_value)[:10])].add(code)

    balances: dict[tuple[date, str], Decimal | None] = {}
    for business_date, codes in grouped.items():
        day_balances = extractor.fetch_balance_by_cashbox_codes(
            business_date=business_date,
            cashbox_codes=sorted(codes),
        )
        for code in codes:
            balances[(business_date, code)] = day_balances.get(code)
    return balances


def _ensure_daily_bitrix_items(
    session: Session,
    *,
    business_date: date,
    settings: Settings,
) -> dict[str, int]:
    existing_codes = card_balance_bitrix.list_existing_cashbox_codes_for_business_date(
        business_date,
        settings=settings,
        limit=5000,
    )
    stats = {
        "created": 0,
        "skipped_existing": 0,
        "skipped_manual_review": 0,
        "skipped_missing_data": 0,
        "skipped_not_in_pilot": 0,
        "skipped_no_workday_data": 0,
        "create_errors": 0,
        "eligible_cashboxes": 0,
    }
    pilot_codes = _pilot_cashbox_codes(settings)
    cashboxes = session.scalars(
        select(CardBalanceCashbox)
        .where(CardBalanceCashbox.is_active.is_(True))
        .order_by(CardBalanceCashbox.onec_cashbox_code.asc())
    ).all()
    for cashbox in cashboxes:
        if cashbox.needs_manual_review:
            stats["skipped_manual_review"] += 1
            continue
        code = clean_string(cashbox.onec_cashbox_code)
        name = clean_string(cashbox.onec_cashbox_name)
        if not code or not name:
            stats["skipped_missing_data"] += 1
            continue
        if pilot_codes and code not in pilot_codes:
            stats["skipped_not_in_pilot"] += 1
            continue
        employee = _resolve_cashbox_bitrix_employee(session, cashbox, settings=settings)
        if settings.card_balance_require_workday and not _cashbox_has_workday(
            session,
            cashbox=cashbox,
            employee=employee,
            business_date=business_date,
        ):
            stats["skipped_no_workday_data"] += 1
            continue
        stats["eligible_cashboxes"] += 1
        if code in existing_codes:
            stats["skipped_existing"] += 1
            continue
        employee_id = employee.get("bitrix_user_id") if employee else None
        employee_name = (
            employee.get("full_name")
            if employee and employee.get("full_name")
            else clean_string(cashbox.employee_last_name)
        )
        employee_last_name = clean_string(cashbox.employee_last_name)
        fields = card_balance_bitrix.build_bitrix_daily_item_fields(
            business_date=business_date,
            onec_cashbox_code=code,
            onec_cashbox_name=name,
            card_last4=clean_string(cashbox.card_last4),
            employee_id=employee_id,
            employee_name=employee_name,
            employee_last_name=employee_last_name,
            assigned_by_id=employee_id,
            settings=settings,
        )
        try:
            card_balance_bitrix.create_bitrix_item(fields=fields, settings=settings)
        except Exception:
            stats["create_errors"] += 1
            continue
        existing_codes.add(code)
        stats["created"] += 1
    return stats


def _pilot_cashbox_codes(settings: Settings) -> set[str]:
    return {
        code
        for code in (clean_string(item) for item in settings.card_balance_pilot_cashbox_codes)
        if code
    }


def _cashbox_has_workday(
    session: Session,
    *,
    cashbox: CardBalanceCashbox,
    employee: dict[str, str] | None,
    business_date: date,
) -> bool:
    staff_refs, staff_names = _cashbox_staff_match_keys(session, cashbox, employee)
    if not staff_refs and not staff_names:
        return False

    facts = session.scalars(
        select(StoreShiftFact).where(StoreShiftFact.shift_date == business_date)
    ).all()
    for fact in facts:
        if fact.attendance_status not in WORKDAY_ATTENDANCE_STATUSES:
            continue
        if _shift_staff_matches(
            staff_ref=fact.staff_ref,
            staff_name=fact.staff_name,
            staff_refs=staff_refs,
            staff_names=staff_names,
        ):
            return True

    plans = session.scalars(
        select(StoreShiftPlan).where(StoreShiftPlan.shift_date == business_date)
    ).all()
    return any(
        _shift_staff_matches(
            staff_ref=plan.staff_ref,
            staff_name=plan.staff_name,
            staff_refs=staff_refs,
            staff_names=staff_names,
        )
        for plan in plans
    )


def _cashbox_staff_match_keys(
    session: Session,
    cashbox: CardBalanceCashbox,
    employee: dict[str, str] | None,
) -> tuple[set[str], set[str]]:
    staff_refs = {
        ref
        for ref in {
            clean_string(cashbox.employee_id),
            clean_string(employee.get("bitrix_user_id") if employee else None),
        }
        if ref
    }
    staff_names = {
        name
        for name in {
            clean_string(cashbox.employee_last_name),
            clean_string(employee.get("full_name") if employee else None),
        }
        if name
    }
    if staff_refs:
        members = session.scalars(
            select(StaffMember).where(StaffMember.external_ref.in_(sorted(staff_refs)))
        ).all()
        staff_names.update(clean_string(member.full_name) for member in members if member.full_name)
    return staff_refs, {name for name in staff_names if name}


def _shift_staff_matches(
    *,
    staff_ref: str | None,
    staff_name: str | None,
    staff_refs: set[str],
    staff_names: set[str],
) -> bool:
    normalized_ref = clean_string(staff_ref)
    if normalized_ref and normalized_ref in staff_refs:
        return True
    normalized_shift_name = _normalize_text(staff_name)
    if not normalized_shift_name:
        return False
    for candidate in staff_names:
        normalized_candidate = _normalize_text(candidate)
        if not normalized_candidate:
            continue
        if normalized_candidate == normalized_shift_name:
            return True
        candidate_tokens = _employee_tokens(candidate)
        if candidate_tokens and all(
            f" {token} " in f" {normalized_shift_name} " for token in candidate_tokens
        ):
            return True
    return False


def _resolve_cashbox_bitrix_employee(
    session: Session,
    cashbox: CardBalanceCashbox,
    *,
    settings: Settings,
) -> dict[str, str] | None:
    if not settings.card_balance_bitrix_webhook_url:
        return None
    override_user_id = _resolve_employee_override_user_id(cashbox, settings=settings)
    if override_user_id:
        override_user = _load_bitrix_user_by_id(settings=settings, user_id=override_user_id)
        if override_user:
            candidate = _build_employee_candidate(override_user)
            cashbox.employee_id = clean_string(candidate.get("bitrix_user_id"))
            return candidate

    saved_user_id = clean_string(cashbox.employee_id)
    if saved_user_id and saved_user_id.isdigit():
        saved_user = _load_bitrix_user_by_id(settings=settings, user_id=saved_user_id)
        if saved_user and _is_plausible_cashbox_employee_match(cashbox, saved_user):
            return _build_employee_candidate(saved_user)

    historical_user_id = session.scalar(
        select(CardBalanceReconciliation.employee_id)
        .where(
            CardBalanceReconciliation.cashbox_id == cashbox.id,
            CardBalanceReconciliation.employee_id.is_not(None),
            CardBalanceReconciliation.employee_id != "",
        )
        .order_by(CardBalanceReconciliation.updated_at.desc(), CardBalanceReconciliation.id.desc())
        .limit(1)
    )
    historical_user_id = clean_string(historical_user_id)
    if historical_user_id and historical_user_id.isdigit():
        historical_user = _load_bitrix_user_by_id(settings=settings, user_id=historical_user_id)
        if historical_user and _is_plausible_cashbox_employee_match(cashbox, historical_user):
            return _build_employee_candidate(historical_user)

    employee_tokens = _employee_tokens(cashbox.employee_last_name)
    if not employee_tokens:
        return None
    queries = _employee_queries(cashbox.employee_last_name)
    if not queries:
        return None
    best: tuple[int, dict[str, str]] | None = None
    for query in queries:
        for user in _search_bitrix_users(settings=settings, query=query):
            user_id = clean_string(user.get("ID") or user.get("Id") or user.get("id"))
            if not user_id or not user_id.isdigit():
                continue
            score = _score_employee_match(employee_tokens, user)
            if score <= 0:
                continue
            candidate = _build_employee_candidate(user)
            if best is None or score > best[0]:
                best = (score, candidate)
    if best is None:
        return None
    cashbox.employee_id = clean_string(best[1].get("bitrix_user_id"))
    return best[1]


def _resolve_employee_override_user_id(
    cashbox: CardBalanceCashbox,
    *,
    settings: Settings,
) -> str | None:
    raw = settings.card_balance_bitrix_employee_overrides
    if not raw:
        return None
    normalized: dict[str, str] = {}
    for key, value in raw.items():
        lookup_key = _override_lookup_key(key)
        lookup_value = clean_string(value)
        if lookup_key and lookup_value:
            normalized[lookup_key] = lookup_value
    if not normalized:
        return None
    code = clean_string(cashbox.onec_cashbox_code)
    card_last4 = clean_string(cashbox.card_last4)
    employee_last_name = clean_string(cashbox.employee_last_name)
    store_name = clean_string(cashbox.store_name)
    tokens = _employee_tokens(employee_last_name)
    keys: list[str] = []
    if code:
        keys.extend([code, f"cashbox:{code}"])
    if employee_last_name:
        keys.extend([employee_last_name, f"employee:{employee_last_name}"])
    for token in tokens:
        keys.extend([token, f"employee:{token}"])
        if card_last4:
            keys.extend(
                [
                    f"{card_last4}:{token}",
                    f"card_employee:{card_last4}:{token}",
                ]
            )
        if store_name:
            keys.extend(
                [
                    f"{store_name}:{token}",
                    f"store_employee:{store_name}:{token}",
                ]
            )
    if card_last4:
        keys.extend([card_last4, f"card:{card_last4}", f"last4:{card_last4}"])
    if store_name:
        keys.extend([store_name, f"store:{store_name}"])
    seen: set[str] = set()
    for key in keys:
        normalized_key = _override_lookup_key(key)
        if not normalized_key or normalized_key in seen:
            continue
        seen.add(normalized_key)
        value = normalized.get(normalized_key)
        if value:
            return value
    return None


def _override_lookup_key(value: str | None) -> str | None:
    normalized = clean_string(value)
    if not normalized:
        return None
    return normalized.lower().replace("ё", "е")


def _load_bitrix_user_by_id(*, settings: Settings, user_id: str) -> dict[str, Any] | None:
    cache_key = clean_string(user_id) or ""
    if not cache_key:
        return None
    if cache_key in _BITRIX_USER_BY_ID_CACHE:
        return _BITRIX_USER_BY_ID_CACHE[cache_key]
    try:
        response = card_balance_bitrix.bitrix_call(
            settings.card_balance_bitrix_webhook_url or "",
            "user.get",
            {"ID": cache_key},
        )
    except Exception:
        _BITRIX_USER_BY_ID_CACHE[cache_key] = None
        return None
    result = response.get("result") or []
    for item in result:
        item_id = clean_string(item.get("ID") or item.get("Id") or item.get("id"))
        if item_id != cache_key:
            continue
        if str(item.get("ACTIVE")).lower() in {"false", "n", "0"}:
            continue
        _BITRIX_USER_BY_ID_CACHE[cache_key] = item
        return item
    _BITRIX_USER_BY_ID_CACHE[cache_key] = None
    return None


def _build_employee_candidate(user: dict[str, Any]) -> dict[str, str]:
    user_id = clean_string(user.get("ID") or user.get("Id") or user.get("id")) or ""
    full_name = clean_string(
        f"{clean_string(user.get('NAME')) or ''} {clean_string(user.get('LAST_NAME')) or ''}"
    )
    return {
        "bitrix_user_id": user_id,
        "full_name": full_name or "",
    }


def _is_plausible_cashbox_employee_match(cashbox: CardBalanceCashbox, user: dict[str, Any]) -> bool:
    tokens = _employee_tokens(cashbox.employee_last_name)
    if not tokens:
        return True
    return _score_employee_match(tokens, user) > 0


def _employee_queries(employee_last_name: str | None) -> list[str]:
    raw = clean_string(employee_last_name)
    tokens = _employee_tokens(employee_last_name)
    queries: list[str] = []
    if raw:
        queries.append(raw)
    if len(tokens) >= 2:
        queries.append(" ".join(tokens))
    queries.extend(tokens)
    return list(dict.fromkeys(item for item in queries if item))


def _search_bitrix_users(*, settings: Settings, query: str) -> list[dict[str, Any]]:
    cached = _BITRIX_USER_SEARCH_CACHE.get(query)
    if cached is not None:
        return cached
    try:
        response = card_balance_bitrix.bitrix_call(
            settings.card_balance_bitrix_webhook_url or "",
            "user.search",
            {"FILTER": {"FIND": query}, "SORT": "ID", "ORDER": "ASC"},
        )
    except Exception:
        _BITRIX_USER_SEARCH_CACHE[query] = []
        return []
    result = response.get("result") or []
    users = [item for item in result if str(item.get("ACTIVE")).lower() not in {"false", "n", "0"}]
    _BITRIX_USER_SEARCH_CACHE[query] = users
    return users


def _score_employee_match(employee_tokens: list[str], user: dict[str, Any]) -> int:
    if not employee_tokens:
        return 0
    name_norm = _normalize_text(clean_string(user.get("NAME")))
    last_name_norm = _normalize_text(clean_string(user.get("LAST_NAME")))
    full_norm = f" {last_name_norm} {name_norm} "
    score = 0
    last_name_hits = 0
    matched_tokens = 0
    for token in employee_tokens:
        word = f" {token} "
        if f" {last_name_norm} " == word:
            score += 100
            last_name_hits += 1
            matched_tokens += 1
            continue
        if word in f" {last_name_norm} ":
            score += 50
            last_name_hits += 1
            matched_tokens += 1
            continue
        if f" {name_norm} " == word:
            score += 40
            matched_tokens += 1
            continue
        if word in f" {name_norm} ":
            score += 20
            matched_tokens += 1
            continue
        if word in full_norm:
            score += 10
            matched_tokens += 1
    if len(employee_tokens) > 1 and all(f" {token} " in full_norm for token in employee_tokens):
        score += 30
    if len(employee_tokens) > 1 and matched_tokens < 2:
        return 0
    if len(employee_tokens) > 1 and last_name_hits == 0:
        return 0
    return score


def _normalize_text(value: str | None) -> str:
    normalized = clean_string(value)
    if not normalized:
        return ""
    lowered = normalized.lower().replace("ё", "е")
    return re.sub(r"[^a-zа-я0-9]+", " ", lowered).strip()


def _employee_tokens(value: str | None) -> list[str]:
    parts = [part for part in _normalize_text(value).split() if len(part) >= 2]
    return [part for part in parts if part not in CARD_EMPLOYEE_STOPWORDS]
