from __future__ import annotations

import re
import secrets
from collections import Counter
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from typing import Iterable

from fastapi import APIRouter, Depends, HTTPException, Query, Security
from fastapi.security import (
    HTTPAuthorizationCredentials,
    HTTPBasic,
    HTTPBasicCredentials,
    HTTPBearer,
)
from sqlalchemy import and_, case, delete, func, or_, select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.api.dependencies import get_db
from app.core.config import get_settings
from app.models import (
    Competitor,
    CompetitorItem,
    CompetitorItemCompatibility,
    CompetitorItemMatch,
    CompetitorItemUrlAlias,
    DeviceBrand,
    PhoneModel,
    Product,
    ProductCompatibility,
    ProductCompetitorItemDecision,
    ProductLiveCandidateCache,
    ProductMatch,
    ProductPhoneModel,
)
from app.models.competitor_item_match import (
    CompetitorItemMatchMethod,
    CompetitorItemMatchStatus,
)
from app.schemas.matching import (
    AcceptSafePropertyValueSuggestionsRequest,
    AcceptSafePropertyValueSuggestionsResponse,
    BulkRejectItemResult,
    BulkRejectRequest,
    BulkRejectResponse,
    Candidate,
    CandidateFacetOption,
    CandidateFacets,
    CompatibilityApplyRequest,
    CompatibilityApplyResponse,
    CompatibilityBlockRequest,
    CompatibilityBrand,
    CompatibilityBrandAlias,
    CompatibilityBrandAliasPatch,
    CompatibilityBrandAliasRequest,
    CompatibilityBrandRequest,
    CompatibilityHint,
    CompatibilityHistoryItem,
    CompatibilityPhoneModel,
    CompatibilityPhoneModelRequest,
    CompatibilityPreviewRequest,
    CompatibilityPreviewResponse,
    CompatibilityUnresolvedGroup,
    CompatibilityUnresolvedItem,
    CurrentMatch,
    DecisionHistoryItem,
    DecisionHistoryResponse,
    DisplayFamilyDetailSchema,
    DisplayFamilyRegistrySummarySchema,
    DisplayFamilyRegistryVersionSchema,
    MatchingActionResponse,
    MatchRequest,
    MatchStatus,
    PaginatedCandidates,
    PaginatedDisplayFamiliesSchema,
    PaginatedProducts,
    ProductFacets,
    ProductRow,
    ProductSort,
    PropertyComparisonItem,
    PropertyComparisonResponse,
    PropertyProfile,
    PropertyRule,
    PropertyRulePatch,
    PropertyRuleRequest,
    PropertySummary,
    PropertyValueMap,
    PropertyValueMapPatch,
    PropertyValueMapRequest,
    PropertyValueSuggestion,
    RejectRequest,
    RevokeRequest,
)
from app.schemas.matching import (
    CompatibilitySummary as CompatibilitySummarySchema,
)
from app.services.bitrix_matching_auth import verify_matching_session_token
from app.services.compatibility_mapping import CompatibilityMappingService
from app.services.competitor_url_aliases import normalize_competitor_url, parse_competitor_url
from app.services.display_family_registry import (
    display_family_registry_summary,
    get_active_display_family_detail,
    list_active_display_families,
    list_display_family_registry_versions,
)
from app.services.manual_matching_decisions import (
    build_decision_snapshot,
    normalize_reason_code,
    snapshot_summary,
)
from app.services.matching_guardrails import (
    basic_candidate_guardrails,
    competitor_item_requires_compatibility,
    device_group,
)
from app.services.matching_property_mapping import (
    DuplicateValueMapError,
    PropertyComparisonResult,
    accept_safe_value_suggestions,
    create_rule,
    create_value_map,
    default_rule_spec,
    evaluate_property_comparison,
    list_profiles,
    list_rules,
    list_value_maps,
    list_value_suggestions,
    restore_default_rule,
    rule_has_default_drift,
    update_rule,
    update_value_map,
)
from app.services.matching_property_mapping import (
    PropertyComparisonItem as ServicePropertyComparisonItem,
)

router = APIRouter()
basic_scheme = HTTPBasic(auto_error=False)
bearer_scheme = HTTPBearer(auto_error=False)
settings = get_settings()
UNSAFE_ACCEPT_CUTOFF = date(2026, 5, 1)

_SEARCH_STOPWORDS = {
    "for",
    "with",
    "and",
    "the",
    "артикул",
    "для",
    "без",
    "под",
    "или",
    "при",
    "плюс",
    "модуль",
    "дисплей",
    "экран",
    "тачскрин",
    "стекло",
    "аккумулятор",
    "камера",
    "крышка",
    "шлейф",
    "рамка",
    "плата",
    "кабель",
    "разъем",
    "разъём",
    "сборе",
    "комп",
    "комплект",
    "тачскрином",
    "cell",
    "incell",
    "кнопка",
    "кнопку",
    "кнопки",
    "включения",
    "громкости",
    "galaxy",
}
_CANDIDATE_QUERY_PREFIX_RE = re.compile(
    r"^\s*(?:артикул|sku|скю|код)\s*[:：#№-]?\s*",
    flags=re.IGNORECASE,
)


def _clean_candidate_query(value: str | None) -> str:
    if not value:
        return ""
    cleaned = value.strip()
    while True:
        stripped = _CANDIDATE_QUERY_PREFIX_RE.sub("", cleaned, count=1).strip()
        if stripped == cleaned:
            return cleaned
        cleaned = stripped


_DEFAULT_OPTIONAL_TOKENS = {
    "apple",
    "black",
    "white",
    "gold",
    "silver",
    "blue",
    "green",
    "red",
    "pink",
    "черный",
    "черная",
    "черное",
    "чёрный",
    "чёрная",
    "чёрное",
    "белый",
    "белая",
    "белое",
    "золото",
    "золотой",
    "синий",
    "зеленый",
    "зелёный",
    "красный",
    "lcd",
    "oled",
    "hard",
    "soft",
    "amoled",
    "gx",
    "hd",
    "fhd",
    "full",
    "small",
    "size",
    "tft",
    "incell",
    "cell",
    "copy",
    "original",
    "orig",
    "orig100",
    "or100",
    "sim",
    "esim",
    "als",
    "fpc",
    "оригинал",
    "ориг",
}


def _authorize(
    credentials: HTTPBasicCredentials | None = Security(basic_scheme),
    bearer_credentials: HTTPAuthorizationCredentials | None = Security(bearer_scheme),
) -> str:
    if credentials is not None:
        if not settings.api_basic_user or not settings.api_basic_password:
            raise HTTPException(status_code=401, detail="basic auth not configured")
        if secrets.compare_digest(
            credentials.username, settings.api_basic_user
        ) and secrets.compare_digest(credentials.password, settings.api_basic_password):
            return credentials.username
        raise HTTPException(status_code=401, detail="unauthorized")

    if bearer_credentials is not None and bearer_credentials.scheme.lower() == "bearer":
        return verify_matching_session_token(
            bearer_credentials.credentials,
            settings=settings,
        ).actor

    if not settings.api_basic_user or not settings.api_basic_password:
        raise HTTPException(status_code=401, detail="basic auth not configured")
    raise HTTPException(status_code=401, detail="unauthorized")


def _float(value: Decimal | float | int | None) -> float | None:
    if value is None:
        return None
    return float(value)


def _match_status_value(status: CompetitorItemMatchStatus | str | None) -> str | None:
    if status is None:
        return None
    return status.value if hasattr(status, "value") else str(status)


def _item_price(item: CompetitorItem) -> float | None:
    return _float(item.price_roz if item.price_roz is not None else item.price_opt)


def _current_match(match: CompetitorItemMatch | None) -> CurrentMatch | None:
    if not match or not match.competitor_item:
        return None
    item = match.competitor_item
    return CurrentMatch(
        competitor_item_id=item.id,
        competitor_name=item.competitor,
        sku=item.external_id,
        name=item.name,
        url=item.url,
        price=_item_price(item),
        confidence=_float(match.final_score or match.score_llm or match.score_embed_best),
        status=_match_status_value(match.status),
        mode=match.method.value if hasattr(match.method, "value") else str(match.method),
    )


def _product_subject(product: Product) -> str | None:
    return product.subject or product.subject_1c or product.subject_generated


def _product_name_sort_value(product: Product) -> tuple[str, str, int]:
    return ((product.name or "").casefold(), (product.article or "").casefold(), product.id)


def _candidate_from_item(
    item: CompetitorItem,
    *,
    match: CompetitorItemMatch | None = None,
    product: Product | None = None,
    product_id: int | None = None,
    rejected_ids: set[int] | None = None,
    property_summary: PropertySummary | None = None,
) -> Candidate:
    rejected_ids = rejected_ids or set()
    status = "available"
    reason = None
    score = None
    if item.id in rejected_ids:
        status = "rejected"
        reason = "Отклонено для этого товара"
    elif match:
        match_status = _match_status_value(match.status)
        if match.product_id == product_id:
            status = (
                "current"
                if match_status == CompetitorItemMatchStatus.ACCEPTED.value
                else match_status or "suggested"
            )
            score = _float(match.final_score or match.score_llm or match.score_embed_best)
        elif match_status == CompetitorItemMatchStatus.ACCEPTED.value:
            status = "locked"
            reason = f"Уже принят к товару #{match.product_id}"
            score = _float(match.final_score or match.score_llm or match.score_embed_best)
    compatibility_hint = _candidate_compatibility_hint(product, item)
    needs_compat_review = compatibility_hint.status == "required"

    return Candidate(
        competitor_item_id=item.id,
        competitor_name=item.competitor,
        sku=item.external_id,
        name=item.name,
        url=item.url,
        price=_item_price(item),
        in_stock=item.availability,
        confidence=_float(item.llm_confidence),
        status=status,
        item_type=item.item_type,
        category_group=item.category_group,
        brand=item.item_brand or item.parsed_device_brand,
        model=item.attrs_model or item.parsed_device_model,
        quality=item.attrs_quality or item.screen_quality_grade,
        color=item.attrs_color or item.color,
        score=score,
        reason=reason,
        needs_compat_review=needs_compat_review,
        compatibility_hint=compatibility_hint,
        last_seen_at=item.last_seen_at or item.scraped_at,
        attrs=item.attrs_json,
        property_summary=property_summary,
    )


def _candidate_guardrail_allowed(
    item: CompetitorItem,
    product: Product,
    match: CompetitorItemMatch | None,
    *,
    include_locked_conflicts: bool = False,
) -> bool:
    if match and (
        (match.product_id == product.id or include_locked_conflicts)
        and (
            match.status == CompetitorItemMatchStatus.ACCEPTED
            or match.method == CompetitorItemMatchMethod.MANUAL
        )
    ):
        return True
    return basic_candidate_guardrails(item, product).allowed


def _record_decision(
    db: Session,
    *,
    product_id: int,
    competitor_item_id: int,
    action: str,
    user: str,
    reason: str | None = None,
    reason_code: str | None = None,
    previous_product_id: int | None = None,
    previous_status: str | None = None,
    product: Product | None = None,
    item: CompetitorItem | None = None,
    match: CompetitorItemMatch | None = None,
) -> None:
    product = product or db.get(Product, product_id)
    item = item or db.get(CompetitorItem, competitor_item_id)
    normalized_reason_code = normalize_reason_code(reason_code)
    snapshot = (
        build_decision_snapshot(
            product=product,
            item=item,
            match=match,
            reason_code=normalized_reason_code,
        )
        if product is not None and item is not None
        else None
    )
    db.add(
        ProductCompetitorItemDecision(
            product_id=product_id,
            competitor_item_id=competitor_item_id,
            action=action,
            reason=reason,
            reason_code=normalized_reason_code,
            snapshot_json=snapshot,
            created_by=user,
            previous_product_id=previous_product_id,
            previous_status=previous_status,
        )
    )


def _search_tokens(value: str | None) -> list[str]:
    value = _clean_candidate_query(value)
    if not value:
        return []
    seen: set[str] = set()
    tokens: list[str] = []
    for raw in re.findall(r"[0-9A-Za-zА-Яа-яЁё]+", value.lower()):
        if raw in _SEARCH_STOPWORDS:
            continue
        if len(raw) < 3 and not (raw.isdigit() and len(raw) >= 2):
            continue
        if raw not in seen:
            tokens.append(raw)
            seen.add(raw)
    return tokens


def _candidate_term_condition(term: str, *, include_external_id: bool = True):
    patterns = [f"%{term}%"]
    if any("а" <= char <= "я" or char == "ё" for char in term):
        patterns.append(f"%{term.capitalize()}%")
    conditions = [
        field.ilike(pattern)
        for field in (
            CompetitorItem.name,
            CompetitorItem.normalized_title,
            CompetitorItem.attrs_model,
            CompetitorItem.item_brand,
            CompetitorItem.parsed_device_model,
            CompetitorItem.parsed_device_brand,
            CompetitorItem.attrs_quality,
            CompetitorItem.attrs_color,
            CompetitorItem.color,
        )
        for pattern in patterns
    ]
    if include_external_id:
        conditions.extend(CompetitorItem.external_id.ilike(pattern) for pattern in patterns)
    return or_(*conditions)


def _candidate_terms_condition(terms: list[str], *, include_external_id: bool = True):
    if not terms:
        return None
    return and_(
        *[
            _candidate_term_condition(term, include_external_id=include_external_id)
            for term in terms
        ]
    )


def _candidate_exact_search_condition(value: str | None):
    cleaned = _clean_candidate_query(value)
    if not cleaned:
        return None
    conditions = [
        CompetitorItem.external_id.ilike(cleaned),
        CompetitorItem.external_id.ilike(f"%{cleaned}%"),
        CompetitorItem.url.ilike(cleaned),
        CompetitorItem.url.ilike(f"%{cleaned}%"),
    ]
    normalized_url = None
    url_parts = None
    if "://" in cleaned or "/" in cleaned or "." in cleaned:
        normalized_url = normalize_competitor_url(cleaned)
        url_parts = parse_competitor_url(cleaned)
    alias_conditions = []
    if normalized_url:
        alias_conditions.append(CompetitorItemUrlAlias.normalized_url == normalized_url)
    if url_parts and url_parts.catalog_id:
        alias_conditions.append(CompetitorItemUrlAlias.catalog_id == url_parts.catalog_id)
    if url_parts and url_parts.redirect_id:
        alias_conditions.append(CompetitorItemUrlAlias.redirect_id == url_parts.redirect_id)
    if alias_conditions:
        conditions.append(
            select(CompetitorItemUrlAlias.id)
            .where(
                CompetitorItemUrlAlias.competitor_item_id == CompetitorItem.id,
                or_(*alias_conditions),
            )
            .exists()
        )
    url_match = re.search(r"/catalog/[^\s?#]+/(\d+)/?", cleaned, flags=re.IGNORECASE)
    if url_match:
        conditions.extend(
            (
                CompetitorItem.url.ilike(f"%/{url_match.group(1)}/%"),
                CompetitorItem.url.ilike(f"%/{url_match.group(1)}"),
                CompetitorItem.external_id.ilike(url_match.group(1)),
            )
        )
    return or_(*conditions)


def _is_moba_catalog_url(value: str | None) -> bool:
    cleaned = _clean_candidate_query(value).lower()
    if not cleaned:
        return False
    return bool(
        re.search(
            r"(?:https?://)?(?:www\.)?moba\.ru/catalog/[^\s?#]+/\d+/?",
            cleaned,
        )
    )


def _is_precise_candidate_query(value: str | None) -> bool:
    cleaned = _clean_candidate_query(value)
    if not cleaned:
        return False
    lowered = cleaned.lower()
    if "://" in lowered or "/catalog/" in lowered:
        return True
    if re.fullmatch(r"\d{5,}", cleaned):
        return True
    if re.fullmatch(r"[a-z0-9][a-z0-9._/-]{3,}", lowered, flags=re.IGNORECASE):
        return any(char.isdigit() for char in lowered) and any(
            char in lowered for char in ("-", "_", "/")
        )
    return False


def _candidate_search_score(terms: list[str], *, include_external_id: bool = True):
    score = None
    for term in terms:
        piece = case(
            (_candidate_term_condition(term, include_external_id=include_external_id), 1),
            else_=0,
        )
        score = piece if score is None else score + piece
    return score


def _looks_like_model_or_brand_token(token: str) -> bool:
    if token in _DEFAULT_OPTIONAL_TOKENS:
        return False
    return any(char.isdigit() for char in token) or bool(re.search(r"[a-z]", token))


def _product_default_terms(product: Product) -> tuple[list[str], list[str]]:
    required_source_tokens = _search_tokens(
        " ".join(
            str(value or "")
            for value in (
                product.name,
                product.brand,
                product.category,
                product.subject,
            )
        )
    )
    optional_tokens = _search_tokens(
        " ".join(
            str(value or "")
            for value in (
                product.quality,
                product.display_quality,
                product.color,
            )
        )
    )
    required = [
        token for token in required_source_tokens if _looks_like_model_or_brand_token(token)
    ]
    if not required:
        required = [
            token for token in required_source_tokens if token not in _DEFAULT_OPTIONAL_TOKENS
        ]
    tokens = required_source_tokens + [
        token for token in optional_tokens if token not in required_source_tokens
    ]
    rank_terms = required + [token for token in tokens if token not in required]
    return required[:5], rank_terms[:12]


def _product_compatibility_segment_conditions(product: Product) -> list[object]:
    if not product.name or "/" not in product.name:
        return []

    seen: set[tuple[str, ...]] = set()
    conditions: list[object] = []
    for raw_segment in re.split(r"\s*/\s*", product.name):
        segment = re.split(r"\bи\s+др\b|etc", raw_segment, maxsplit=1, flags=re.IGNORECASE)[0]
        tokens = [
            token for token in _search_tokens(segment) if _looks_like_model_or_brand_token(token)
        ]
        if len(tokens) < 2:
            continue
        key = tuple(tokens[:5])
        if key in seen:
            continue
        seen.add(key)
        condition = _candidate_terms_condition(list(key), include_external_id=False)
        if condition is not None:
            conditions.append(condition)
    return conditions


def _infer_product_item_type(product: Product) -> str | None:
    text = " ".join(
        str(value or "").lower() for value in (product.name, product.category, product.subject)
    )
    if any(word in text for word in ("аккумулятор", "акб", "battery")):
        return "battery"
    if any(word in text for word in ("дисплей", "тачскрин", "lcd", "oled", "экран")):
        return "display"
    if "камера" in text:
        return "camera"
    if "шлейф" in text:
        return "flex"
    if any(word in text for word in ("разъем", "разъём", "коннектор")):
        return "connector"
    if "плата" in text:
        return "board"
    if any(word in text for word in ("крышка", "корпус", "рамка")):
        return "housing"
    if any(word in text for word in ("кабель", "шнур", "провод")):
        return "cable"
    return None


def _category_expected_product_item_type(category: str | None) -> str | None:
    text = str(category or "").strip().lower()
    if not text:
        return None
    if any(word in text for word in ("диспле", "экран", "lcd", "oled")):
        return "display"
    if any(word in text for word in ("аккумулятор", "акб", "battery")):
        return "battery"
    if "камер" in text:
        return "camera"
    if "шлейф" in text:
        return "flex"
    if any(word in text for word in ("разъем", "разъём", "коннектор")):
        return "connector"
    if "плат" in text:
        return "board"
    if any(word in text for word in ("крыш", "корпус", "рамк")):
        return "housing"
    if any(word in text for word in ("кабел", "шнур", "провод")):
        return "cable"
    return None


def _category_expected_device_group(category: str | None) -> str | None:
    text = str(category or "").strip().lower()
    if not text:
        return None
    if "для телефонов" in text or "для iphone" in text:
        return "phone"
    if "для планшетов" in text or "для ipad" in text:
        return "tablet"
    if "смарт-час" in text or "для часов" in text:
        return "watch"
    if "для ноутбуков" in text:
        return "notebook"
    return None


def _infer_product_device_group(product: Product) -> str | None:
    return device_group(
        " ".join(
            str(value or "")
            for value in (
                product.name,
                product.subject,
                product.subject_1c,
                product.subject_generated,
            )
        )
    )


def _product_category_matches(product: Product, category: str | None) -> bool:
    if not category:
        return True
    if product.category != category:
        return False
    expected_type = _category_expected_product_item_type(category)
    if expected_type is None:
        return True
    inferred_type = _infer_product_item_type(product)
    if inferred_type is not None and inferred_type != expected_type:
        return False
    expected_device_group = _category_expected_device_group(category)
    if expected_device_group is None:
        return True
    inferred_device_group = _infer_product_device_group(product)
    return inferred_device_group is None or inferred_device_group == expected_device_group


def _product_category_is_consistent(product: Product) -> bool:
    if not product.category:
        return False
    return _product_category_matches(product, product.category)


def _candidate_item_type_content_condition(item_type: str):
    if item_type != "display":
        return None
    display_markers = (
        "дисплей",
        "тачскрин",
        "lcd",
        "oled",
        "amoled",
        "экран",
        "screen",
        "touchscreen",
        "touch screen",
        "in-cell",
        "incell",
    )
    fields = (
        CompetitorItem.name,
        CompetitorItem.normalized_title,
        CompetitorItem.external_id,
    )
    return or_(*(field.ilike(f"%{marker}%") for field in fields for marker in display_markers))


def _candidate_item_type_condition(item_type: str | None):
    if not item_type:
        return None
    item_type_content_condition = _candidate_item_type_content_condition(item_type)
    if item_type == "display" and item_type_content_condition is not None:
        return and_(
            item_type_content_condition,
            or_(
                CompetitorItem.item_type == item_type,
                CompetitorItem.item_type.is_(None),
                CompetitorItem.item_type == "",
            ),
        )
    if item_type_content_condition is not None:
        return and_(CompetitorItem.item_type == item_type, item_type_content_condition)
    return CompetitorItem.item_type == item_type


def _apply_candidate_item_type_filter(query, item_type: str | None):
    condition = _candidate_item_type_condition(item_type)
    if condition is None:
        return query
    return query.where(condition)


def _default_candidate_condition(product: Product) -> tuple[object, list[str]]:
    required_terms, rank_terms = _product_default_terms(product)
    fallback_pattern = f"%{product.article}%"
    fallback_conditions = [
        CompetitorItem.external_id.ilike(fallback_pattern),
        CompetitorItemMatch.product_id == product.id,
    ]
    phone_model_ids = [
        link.phone_model_id
        for link in getattr(product, "phone_model_links", []) or []
        if link.phone_model_id is not None
    ]
    if phone_model_ids:
        fallback_conditions.append(
            select(CompetitorItemCompatibility.id)
            .where(
                CompetitorItemCompatibility.competitor_item_id == CompetitorItem.id,
                CompetitorItemCompatibility.phone_model_id.in_(phone_model_ids),
            )
            .exists()
        )
    compatibility_segment_conditions = _product_compatibility_segment_conditions(product)
    if compatibility_segment_conditions:
        fallback_conditions.append(or_(*compatibility_segment_conditions))
    product_condition = _candidate_terms_condition(required_terms, include_external_id=False)
    if product_condition is not None:
        fallback_conditions.append(product_condition)
    elif product.name:
        fallback_conditions.append(CompetitorItem.name.ilike(f"%{product.name[:80]}%"))
    return or_(*fallback_conditions), rank_terms


def _apply_default_candidate_filter(query, product: Product) -> tuple[object, list[str]]:
    condition, rank_terms = _default_candidate_condition(product)
    return query.where(condition), rank_terms


def _live_candidate_count_for_product(db: Session, product: Product) -> int:
    rejected_ids = _rejected_item_ids_for_product(db, product.id)
    query = (
        select(CompetitorItem)
        .options(
            selectinload(CompetitorItem.compatibilities).selectinload(
                CompetitorItemCompatibility.phone_model
            ),
            selectinload(CompetitorItem.compatibilities).selectinload(
                CompetitorItemCompatibility.device_brand_ref
            ),
        )
        .outerjoin(CompetitorItemMatch, CompetitorItemMatch.competitor_item_id == CompetitorItem.id)
        .where(CompetitorItem.is_active.is_(True))
    )
    query = _apply_candidate_item_type_filter(query, _infer_product_item_type(product))
    query, _ = _apply_default_candidate_filter(query, product)
    if rejected_ids:
        query = query.where(CompetitorItem.id.notin_(rejected_ids))
    query = query.where(
        or_(
            CompetitorItemMatch.id.is_(None),
            CompetitorItemMatch.product_id == product.id,
            CompetitorItemMatch.status != CompetitorItemMatchStatus.ACCEPTED,
        )
    )
    items = db.execute(query.distinct()).scalars().all()
    return sum(1 for item in items if basic_candidate_guardrails(item, product).allowed)


def _live_candidate_cache_by_product(db: Session, product_ids: Iterable[int]) -> dict[int, int]:
    ids = list(dict.fromkeys(product_ids))
    if not ids:
        return {}
    rows = db.execute(
        select(
            ProductLiveCandidateCache.product_id,
            ProductLiveCandidateCache.live_candidate_count,
        ).where(ProductLiveCandidateCache.product_id.in_(ids))
    ).all()
    return {product_id: int(count or 0) for product_id, count in rows}


def _invalidate_live_candidate_cache(db: Session, *product_ids: int | None) -> None:
    ids = [product_id for product_id in dict.fromkeys(product_ids) if product_id is not None]
    if not ids:
        return
    db.execute(
        delete(ProductLiveCandidateCache).where(ProductLiveCandidateCache.product_id.in_(ids))
    )


def _as_date(value: object | None) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _competitor_item_has_compatibility(db: Session, item_id: int) -> bool:
    return bool(
        db.scalar(
            select(
                select(CompetitorItemCompatibility.id)
                .where(CompetitorItemCompatibility.competitor_item_id == item_id)
                .exists()
            )
        )
    )


_MODEL_TOKEN_RE = re.compile(r"[a-zа-яё0-9]+", re.IGNORECASE)
_ACCEPT_CODE_RE = re.compile(
    r"\b(?:[A-Z]{1,6}[- ]?\d{2,8}[A-Z0-9]{0,8}|\d{2,8}[A-Z]{1,8})\b",
    re.IGNORECASE,
)
_MODEL_TOKEN_STOPWORDS = {
    "apple",
    "samsung",
    "xiaomi",
    "redmi",
    "poco",
    "realme",
    "huawei",
    "honor",
    "oppo",
    "vivo",
    "tecno",
    "infinix",
    "nokia",
    "google",
    "motorola",
    "lenovo",
    "asus",
    "sony",
    "galaxy",
    "iphone",
    "для",
}
_ACCEPT_CODE_STOPWORDS = {
    "3G",
    "4G",
    "5G",
    "LTE",
    "LCD",
    "OLED",
    "TFT",
    "USB",
    "TYPEC",
    "OR",
    "OR100",
    "ORG100",
    "ORIG100",
    "OEM100",
}


def _model_tokens_for_accept(*values: object | None) -> set[str]:
    text = " ".join(str(value or "") for value in values).casefold().replace("ё", "е")
    tokens = set(_MODEL_TOKEN_RE.findall(text))
    return {
        token
        for token in tokens
        if token not in _MODEL_TOKEN_STOPWORDS
        and (len(token) >= 3 or any(char.isdigit() for char in token))
    }


def _accept_model_tokens_match(model_tokens: set[str], item_tokens: set[str]) -> bool:
    if not model_tokens:
        return False
    overlap = model_tokens & item_tokens
    if len(model_tokens) == 1:
        return bool(overlap)
    return len(overlap) >= 2 and len(overlap) / len(model_tokens) >= 0.6


def _accept_code_tokens(*values: object | None) -> set[str]:
    text = " ".join(str(value or "") for value in values).upper()
    text = text.replace("Ё", "Е").replace("–", "-").replace("—", "-")
    tokens: set[str] = set()
    for match in _ACCEPT_CODE_RE.finditer(text):
        token = re.sub(r"[-\s]+", "", match.group(0).upper())
        if len(token) < 5:
            continue
        if token in _ACCEPT_CODE_STOPWORDS:
            continue
        if not re.search(r"[A-Z]", token) or not re.search(r"\d", token):
            continue
        if re.fullmatch(r"\d+(?:G|GB|TB|MAH|WH|W|V|A|HZ)", token):
            continue
        if re.fullmatch(r"(?:OR|ORG|ORIG|OEM|COPY)\d{2,3}", token):
            continue
        tokens.add(token)
    return tokens


def _shared_accept_codes(product: Product, item: CompetitorItem) -> set[str]:
    product_codes = _accept_code_tokens(
        product.name, product.category, product.subject, product.subject_1c
    )
    item_codes = _accept_code_tokens(
        item.name,
        item.normalized_title,
        item.external_id,
        item.attrs_model,
        item.parsed_device_model,
    )
    return product_codes & item_codes


def _dedupe_hint_values(values: Iterable[object | None]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = str(raw or "").strip()
        if not value:
            continue
        key = value.casefold().replace("ё", "е")
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _phone_model_hint_value(phone_model: PhoneModel | None, raw_value: object | None = None) -> str:
    if phone_model is not None:
        value = " ".join(
            part
            for part in (
                phone_model.brand,
                phone_model.model_name,
                phone_model.variant,
            )
            if part
        )
        if value:
            return value
    return str(raw_value or "").strip()


def _compatibility_row_hint_value(compatibility: CompetitorItemCompatibility) -> str:
    phone_model = getattr(compatibility, "phone_model", None)
    value = _phone_model_hint_value(phone_model)
    if value:
        return value
    return " ".join(
        part
        for part in (
            compatibility.device_brand,
            compatibility.device_model,
            compatibility.device_variant,
        )
        if part
    )


def _existing_compatibility_hint(
    compatibilities: Iterable[CompetitorItemCompatibility],
) -> CompatibilityHint:
    values = _dedupe_hint_values(
        _compatibility_row_hint_value(compatibility) for compatibility in compatibilities
    )
    return CompatibilityHint(
        status="existing",
        label="Совм. есть",
        detail="У товара конкурента уже заведена совместимость.",
        matched_values=values,
    )


def _matched_product_model_values(product: Product, item: CompetitorItem) -> list[str]:
    item_tokens = _model_tokens_for_accept(
        item.name,
        item.normalized_title,
        item.external_id,
        item.attrs_model,
        item.parsed_device_model,
    )
    values: list[str] = []
    for link in getattr(product, "phone_model_links", []) or []:
        phone_model = getattr(link, "phone_model", None)
        if phone_model is None:
            continue
        model_tokens = _model_tokens_for_accept(
            phone_model.brand,
            phone_model.model_name,
            phone_model.variant,
            link.raw_value,
        )
        if _accept_model_tokens_match(model_tokens, item_tokens):
            values.append(_phone_model_hint_value(phone_model, link.raw_value))
    return _dedupe_hint_values(values)


def _inferred_accept_compatibility_hint(
    product: Product,
    item: CompetitorItem,
) -> CompatibilityHint | None:
    model_values = _matched_product_model_values(product, item)
    if model_values:
        return CompatibilityHint(
            status="inferred_model",
            label="Модель",
            detail=(
                "Модель совпадает с совместимостью нашего товара. "
                "При принятии совместимость конкурента будет создана автоматически."
            ),
            matched_values=model_values,
        )
    shared_codes = sorted(
        _shared_accept_codes(product, item), key=lambda value: (-len(value), value)
    )
    if shared_codes:
        return CompatibilityHint(
            status="inferred_code",
            label="Код",
            detail=(
                "Код из названия или SKU совпадает с нашим товаром. "
                "При принятии совместимость конкурента будет создана автоматически."
            ),
            matched_values=shared_codes,
        )
    return None


def _candidate_compatibility_hint(
    product: Product | None,
    item: CompetitorItem,
) -> CompatibilityHint:
    compatibilities = list(getattr(item, "compatibilities", []) or [])
    if compatibilities:
        return _existing_compatibility_hint(compatibilities)
    if product is not None:
        inferred = _inferred_accept_compatibility_hint(product, item)
        if inferred is not None:
            return inferred
    target = competitor_item_requires_compatibility(item)
    if target.requires_compatibility:
        return CompatibilityHint(
            status="required",
            label="Совм. нужна",
            detail="У товара конкурента нет совместимости, общую модель или код не нашли.",
            matched_values=[],
        )
    return CompatibilityHint(
        status="not_required",
        label="Не требуется",
        detail="Для этого кандидата совместимость не требуется.",
        matched_values=[],
    )


def _ensure_accept_compatibility_from_shared_code(
    db: Session,
    product: Product,
    item: CompetitorItem,
) -> bool:
    shared_codes = _shared_accept_codes(product, item)
    if not shared_codes:
        return False
    code = sorted(shared_codes, key=lambda value: (-len(value), value))[0]
    db.add(
        CompetitorItemCompatibility(
            competitor_item_id=item.id,
            device_brand=item.parsed_device_brand or item.item_brand or product.brand or "unknown",
            device_model=code,
            source="manual_accept_code_overlap",
            notes=f"Создано при ручном принятии: общий код {code}",
        )
    )
    db.flush()
    return True


def _ensure_accept_compatibility_from_product(
    db: Session,
    product: Product,
    item: CompetitorItem,
) -> bool:
    if _competitor_item_has_compatibility(db, item.id):
        return True
    item_tokens = _model_tokens_for_accept(
        item.name,
        item.normalized_title,
        item.external_id,
        item.attrs_model,
        item.parsed_device_model,
    )
    links = (
        db.execute(
            select(ProductPhoneModel)
            .options(selectinload(ProductPhoneModel.phone_model))
            .where(ProductPhoneModel.product_id == product.id)
        )
        .scalars()
        .all()
    )
    for link in links:
        phone_model = link.phone_model
        if phone_model is None:
            continue
        model_tokens = _model_tokens_for_accept(
            phone_model.brand,
            phone_model.model_name,
            phone_model.variant,
            link.raw_value,
        )
        if not _accept_model_tokens_match(model_tokens, item_tokens):
            continue
        db.add(
            CompetitorItemCompatibility(
                competitor_item_id=item.id,
                phone_model_id=phone_model.id,
                device_brand_id=phone_model.brand_id,
                device_brand=phone_model.brand or product.brand or "",
                device_model=phone_model.model_name,
                device_variant=phone_model.variant,
                source="manual_accept_inferred",
                notes="Создано при ручном принятии в Bitrix Matching",
            )
        )
        db.flush()
        return True
    return _ensure_accept_compatibility_from_shared_code(db, product, item)


def _rejected_item_ids_for_product(db: Session, product_id: int) -> set[int]:
    decisions = (
        db.execute(
            select(ProductCompetitorItemDecision)
            .where(ProductCompetitorItemDecision.product_id == product_id)
            .order_by(ProductCompetitorItemDecision.id.asc())
        )
        .scalars()
        .all()
    )
    rejected: set[int] = set()
    for decision in decisions:
        if decision.action == "reject":
            rejected.add(decision.competitor_item_id)
        elif decision.action in {"accept", "revoke"}:
            rejected.discard(decision.competitor_item_id)
    return rejected


def _dedupe_item_ids(item_ids: Iterable[int]) -> list[int]:
    seen: set[int] = set()
    deduped: list[int] = []
    for item_id in item_ids:
        if item_id in seen:
            continue
        seen.add(item_id)
        deduped.append(item_id)
    return deduped


def _latest_reject_decision_for_product_item(
    db: Session,
    product_id: int,
    competitor_item_id: int,
) -> ProductCompetitorItemDecision | None:
    return (
        db.execute(
            select(ProductCompetitorItemDecision)
            .where(
                ProductCompetitorItemDecision.product_id == product_id,
                ProductCompetitorItemDecision.competitor_item_id == competitor_item_id,
                ProductCompetitorItemDecision.action == "reject",
            )
            .order_by(ProductCompetitorItemDecision.id.desc())
        )
        .scalars()
        .first()
    )


def _restorable_rejected_match_status(value: str | None) -> CompetitorItemMatchStatus | None:
    if value not in {
        CompetitorItemMatchStatus.SUGGESTED.value,
        CompetitorItemMatchStatus.NEEDS_REVIEW.value,
        CompetitorItemMatchStatus.AMBIGUOUS.value,
    }:
        return None
    return CompetitorItemMatchStatus(value)


def _product_status(
    *,
    accepted_count: int,
    accepted_competitor_count: int,
    manual_count: int,
    suggested_count: int,
    review_count: int,
    ambiguous_count: int,
) -> MatchStatus:
    if ambiguous_count:
        return MatchStatus.ambiguous
    if review_count:
        return MatchStatus.uncertain
    if suggested_count:
        return MatchStatus.candidates
    if accepted_count > 0:
        if accepted_competitor_count and accepted_count > accepted_competitor_count:
            return MatchStatus.multiple
        return MatchStatus.manual if manual_count else MatchStatus.auto
    return MatchStatus.none


def _status_filter_allowed(
    status: MatchStatus,
    *,
    accepted_count: int,
    accepted_competitor_count: int,
    manual_count: int,
    suggested_count: int,
    review_count: int,
    ambiguous_count: int,
) -> bool:
    if status == MatchStatus.matched:
        return accepted_count > 0
    if status == MatchStatus.live_candidates:
        return (
            _product_status(
                accepted_count=accepted_count,
                accepted_competitor_count=accepted_competitor_count,
                manual_count=manual_count,
                suggested_count=suggested_count,
                review_count=review_count,
                ambiguous_count=ambiguous_count,
            )
            == MatchStatus.none
        )
    return (
        _product_status(
            accepted_count=accepted_count,
            accepted_competitor_count=accepted_competitor_count,
            manual_count=manual_count,
            suggested_count=suggested_count,
            review_count=review_count,
            ambiguous_count=ambiguous_count,
        )
        == status
    )


def _build_facets(candidates: Iterable[Candidate]) -> CandidateFacets:
    counters = {
        "sources": Counter(),
        "item_types": Counter(),
        "category_groups": Counter(),
        "brands": Counter(),
        "qualities": Counter(),
        "colors": Counter(),
    }
    for candidate in candidates:
        if candidate.competitor_name:
            counters["sources"][candidate.competitor_name] += 1
        if candidate.item_type:
            counters["item_types"][candidate.item_type] += 1
        if candidate.category_group:
            counters["category_groups"][candidate.category_group] += 1
        if candidate.brand:
            counters["brands"][candidate.brand] += 1
        if candidate.quality:
            counters["qualities"][candidate.quality] += 1
        if candidate.color:
            counters["colors"][candidate.color] += 1

    def opts(counter: Counter[str]) -> list[CandidateFacetOption]:
        return [
            CandidateFacetOption(value=value, label=value, count=count)
            for value, count in counter.most_common(50)
            if value
        ]

    return CandidateFacets(
        sources=opts(counters["sources"]),
        item_types=opts(counters["item_types"]),
        category_groups=opts(counters["category_groups"]),
        brands=opts(counters["brands"]),
        qualities=opts(counters["qualities"]),
        colors=opts(counters["colors"]),
    )


_COMPATIBILITY_BRAND_LABELS = {
    "apple": "Apple",
    "samsung": "Samsung",
    "xiaomi": "Xiaomi",
    "huawei": "Huawei",
    "honor": "Honor",
    "tecno": "Tecno",
    "realme": "Realme",
    "infinix": "Infinix",
    "vivo": "Vivo",
    "oppo": "OPPO",
    "oneplus": "OnePlus",
    "zte": "ZTE",
    "google": "Google",
    "doogee": "Doogee",
    "asus": "Asus",
    "meizu": "Meizu",
    "sony": "Sony",
    "itel": "Itel",
    "nokia": "Nokia",
    "nothing": "Nothing",
    "lg": "LG",
}


def _normalize_compatibility_brand(value: str | None) -> str | None:
    normalized = re.sub(r"\s+", " ", str(value or "").strip()).lower()
    return normalized or None


def _compatibility_brand_label(value: str, labels: dict[str, str] | None = None) -> str:
    if labels and value in labels:
        return labels[value]
    return _COMPATIBILITY_BRAND_LABELS.get(value, value)


def _compatibility_brands_by_product(
    db: Session, product_ids: Iterable[int]
) -> tuple[dict[int, set[str]], dict[str, str]]:
    ids = list(dict.fromkeys(product_ids))
    if not ids:
        return {}, {}
    rows = db.execute(
        select(
            ProductPhoneModel.product_id,
            PhoneModel.brand,
            DeviceBrand.code,
            DeviceBrand.display_name,
        )
        .join(PhoneModel, PhoneModel.id == ProductPhoneModel.phone_model_id)
        .outerjoin(DeviceBrand, DeviceBrand.id == PhoneModel.brand_id)
        .where(ProductPhoneModel.product_id.in_(ids), PhoneModel.brand.isnot(None))
    ).all()
    result: dict[int, set[str]] = {}
    labels: dict[str, str] = {}
    for product_id, brand, brand_code, display_name in rows:
        normalized = _normalize_compatibility_brand(brand_code or brand)
        if normalized:
            result.setdefault(product_id, set()).add(normalized)
            if display_name:
                labels[normalized] = display_name
    return result, labels


def _compatibility_models_by_product(
    db: Session,
    product_ids: Iterable[int],
    *,
    limit_per_product: int = 6,
) -> dict[int, list[str]]:
    ids = list(dict.fromkeys(product_ids))
    if not ids:
        return {}
    rows = db.execute(
        select(ProductCompatibility.product_id, ProductCompatibility.value)
        .where(
            ProductCompatibility.product_id.in_(ids),
            ProductCompatibility.source == "onec",
        )
        .order_by(ProductCompatibility.product_id, ProductCompatibility.value)
    ).all()
    result: dict[int, list[str]] = {}
    seen: dict[int, set[str]] = {}
    for product_id, value in rows:
        cleaned = str(value or "").strip()
        if not cleaned:
            continue
        key = cleaned.lower().replace("ё", "е")
        product_seen = seen.setdefault(product_id, set())
        if key in product_seen:
            continue
        product_seen.add(key)
        values = result.setdefault(product_id, [])
        if len(values) < limit_per_product:
            values.append(cleaned)
    return result


def _build_product_facets(
    rows: Iterable[tuple[Product, dict[str, int], MatchStatus]],
    compatibility_brands_by_product: dict[int, set[str]],
    compatibility_brand_labels: dict[str, str] | None = None,
) -> ProductFacets:
    counters = {
        "subjects": Counter(),
        "brands": Counter(),
        "categories": Counter(),
        "compatibility_brands": Counter(),
    }
    for product, _, _ in rows:
        if subject := _product_subject(product):
            counters["subjects"][subject] += 1
        if product.brand:
            counters["brands"][product.brand] += 1
        if _product_category_is_consistent(product):
            counters["categories"][product.category] += 1
        for brand in compatibility_brands_by_product.get(product.id, set()):
            counters["compatibility_brands"][brand] += 1

    def opts(
        counter: Counter[str],
        *,
        labeler: Callable[[str], str] = lambda value: value,
    ) -> list[CandidateFacetOption]:
        return [
            CandidateFacetOption(value=value, label=labeler(value), count=count)
            for value, count in counter.most_common(100)
            if value
        ]

    return ProductFacets(
        subjects=opts(counters["subjects"]),
        brands=opts(counters["brands"]),
        categories=opts(counters["categories"]),
        compatibility_brands=opts(
            counters["compatibility_brands"],
            labeler=lambda value: _compatibility_brand_label(value, compatibility_brand_labels),
        ),
    )


def _candidate_status_filter_allowed(candidate: Candidate, status: str | None) -> bool:
    if not status:
        return True
    normalized = status.strip().lower()
    candidate_status = str(candidate.status or "available").lower()
    if normalized == "free":
        return candidate_status in {
            "available",
            "suggested",
            "needs_review",
            "ambiguous",
        }
    if normalized == "linked":
        return candidate_status in {"current", "locked", "accepted"}
    return candidate_status == normalized


def _property_summary_schema(result: PropertyComparisonResult | None) -> PropertySummary | None:
    if result is None:
        return None
    summary = result.summary
    return PropertySummary(
        total=summary.total,
        matched=summary.matched,
        missing=summary.missing,
        conflict=summary.conflict,
        unmapped=summary.unmapped,
        status=summary.status,
        label=summary.label,
        conflicts=summary.conflicts,
        block_conflict=summary.block_conflict,
        review_conflict=summary.review_conflict,
        hint_conflict=summary.hint_conflict,
    )


def _property_item_schema(item: ServicePropertyComparisonItem) -> PropertyComparisonItem:
    return PropertyComparisonItem(
        property_key=item.property_key,
        label=item.label,
        product_value=item.product_value,
        competitor_value=item.competitor_value,
        mapped_value=item.mapped_value,
        status=item.status,
        severity=item.severity,
        comparison_mode=item.comparison_mode,
    )


def _property_comparison_schema(
    result: PropertyComparisonResult,
) -> PropertyComparisonResponse:
    summary = _property_summary_schema(result)
    assert summary is not None
    return PropertyComparisonResponse(
        profile_id=result.profile_id,
        profile_code=result.profile_code,
        profile_name=result.profile_name,
        summary=summary,
        items=[_property_item_schema(item) for item in result.items],
    )


def _profile_schema(profile) -> PropertyProfile:
    return PropertyProfile(
        id=profile.id,
        code=profile.code,
        name=profile.name,
        item_type=profile.item_type,
        sort_order=profile.sort_order,
        is_active=profile.is_active,
    )


def _rule_schema(rule) -> PropertyRule:
    spec = default_rule_spec(rule)
    return PropertyRule(
        id=rule.id,
        profile_id=rule.profile_id,
        profile_code=rule.profile.code if getattr(rule, "profile", None) else None,
        property_key=rule.property_key,
        label=rule.label,
        product_field=rule.product_field,
        competitor_field=rule.competitor_field,
        comparison_mode=rule.comparison_mode,
        severity=rule.severity,
        config_json=rule.config_json,
        sort_order=rule.sort_order,
        is_active=rule.is_active,
        default_label=spec.label if spec else None,
        default_product_field=spec.product_field if spec else None,
        default_competitor_field=spec.competitor_field if spec else None,
        default_comparison_mode=spec.comparison_mode if spec else None,
        default_severity=spec.severity if spec else None,
        default_config_json=spec.config_json if spec else None,
        default_sort_order=spec.sort_order if spec else None,
        has_default_drift=rule_has_default_drift(rule),
    )


def _value_map_schema(value_map) -> PropertyValueMap:
    rule = getattr(value_map, "rule", None)
    profile = getattr(rule, "profile", None) if rule else None
    return PropertyValueMap(
        id=value_map.id,
        rule_id=value_map.rule_id,
        profile_code=profile.code if profile else None,
        property_key=rule.property_key if rule else None,
        competitor_source=value_map.competitor_source,
        competitor_value=value_map.competitor_value,
        mapped_value=value_map.mapped_value,
        notes=value_map.notes,
        is_active=value_map.is_active,
    )


def _compatibility_summary_schema(summary) -> CompatibilitySummarySchema:
    return CompatibilitySummarySchema(**summary.__dict__)


def _compatibility_brand_schema(row) -> CompatibilityBrand:
    return CompatibilityBrand(**row.__dict__)


def _compatibility_brand_alias_schema(row) -> CompatibilityBrandAlias:
    return CompatibilityBrandAlias(**row.__dict__)


def _compatibility_model_schema(row) -> CompatibilityPhoneModel:
    return CompatibilityPhoneModel(**row.__dict__)


def _compatibility_unresolved_schema(row) -> CompatibilityUnresolvedItem:
    return CompatibilityUnresolvedItem(**row.__dict__)


def _compatibility_group_schema(row) -> CompatibilityUnresolvedGroup:
    return CompatibilityUnresolvedGroup(
        group_key=row.group_key,
        entity_type=row.entity_type,
        source=row.source,
        raw_value=row.raw_value,
        raw_brand=row.raw_brand,
        raw_model=row.raw_model,
        raw_variant=row.raw_variant,
        normalized_key=row.normalized_key,
        brand_id=row.brand_id,
        brand_display_name=row.brand_display_name,
        affected_count=row.affected_count,
        product_count=row.product_count,
        competitor_count=row.competitor_count,
        examples=[_compatibility_unresolved_schema(item) for item in row.examples],
        suggested_phone_models=[
            _compatibility_model_schema(model) for model in row.suggested_phone_models
        ],
        safe_auto_model_id=row.safe_auto_model_id,
        is_noise_candidate=row.is_noise_candidate,
    )


def _compatibility_preview_schema(preview) -> CompatibilityPreviewResponse:
    return CompatibilityPreviewResponse(
        preview_token=preview.preview_token,
        affected_count=preview.affected_count,
        affected_product_count=preview.affected_product_count,
        affected_competitor_count=preview.affected_competitor_count,
        target_phone_model_ids=preview.target_phone_model_ids,
        target_phone_models=[
            _compatibility_model_schema(model) for model in preview.target_phone_models
        ],
        warnings=preview.warnings,
        items=[_compatibility_unresolved_schema(item) for item in preview.items],
    )


def _compatibility_apply_schema(result) -> CompatibilityApplyResponse:
    return CompatibilityApplyResponse(**result.__dict__)


def _compatibility_history_schema(row) -> CompatibilityHistoryItem:
    return CompatibilityHistoryItem(**row.__dict__)


@router.get("/matching/property-profiles", response_model=list[PropertyProfile])
def get_property_profiles(
    db: Session = Depends(get_db),
    _: str = Depends(_authorize),
) -> list[PropertyProfile]:
    return [_profile_schema(profile) for profile in list_profiles(db)]


@router.get("/matching/property-rules", response_model=list[PropertyRule])
def get_property_rules(
    profile_id: int | None = Query(None),
    profile_code: str | None = Query(None),
    db: Session = Depends(get_db),
    _: str = Depends(_authorize),
) -> list[PropertyRule]:
    return [
        _rule_schema(rule)
        for rule in list_rules(db, profile_id=profile_id, profile_code=profile_code)
    ]


@router.post("/matching/property-rules", response_model=PropertyRule)
def create_property_rule(
    payload: PropertyRuleRequest,
    db: Session = Depends(get_db),
    user: str = Depends(_authorize),
) -> PropertyRule:
    try:
        rule = create_rule(
            db,
            profile_id=payload.profile_id,
            profile_code=payload.profile_code,
            property_key=payload.property_key,
            label=payload.label,
            product_field=payload.product_field,
            competitor_field=payload.competitor_field,
            comparison_mode=payload.comparison_mode,
            severity=payload.severity,
            config_json=payload.config_json,
            sort_order=payload.sort_order,
            is_active=payload.is_active,
            actor=user,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _rule_schema(rule)


@router.patch("/matching/property-rules/{rule_id}", response_model=PropertyRule)
def patch_property_rule(
    rule_id: int,
    payload: PropertyRulePatch,
    db: Session = Depends(get_db),
    user: str = Depends(_authorize),
) -> PropertyRule:
    rule = update_rule(
        db,
        rule_id,
        actor=user,
        **payload.model_dump(exclude_unset=True),
    )
    if rule is None:
        raise HTTPException(status_code=404, detail="property rule not found")
    return _rule_schema(rule)


@router.get("/matching/property-value-maps", response_model=list[PropertyValueMap])
def get_property_value_maps(
    rule_id: int | None = Query(None),
    profile_code: str | None = Query(None),
    db: Session = Depends(get_db),
    _: str = Depends(_authorize),
) -> list[PropertyValueMap]:
    return [
        _value_map_schema(value_map)
        for value_map in list_value_maps(db, rule_id=rule_id, profile_code=profile_code)
    ]


@router.post("/matching/property-value-maps", response_model=PropertyValueMap)
def create_property_value_map(
    payload: PropertyValueMapRequest,
    db: Session = Depends(get_db),
    _: str = Depends(_authorize),
) -> PropertyValueMap:
    try:
        value_map = create_value_map(
            db,
            rule_id=payload.rule_id,
            competitor_source=payload.competitor_source,
            competitor_value=payload.competitor_value,
            mapped_value=payload.mapped_value,
            notes=payload.notes,
            is_active=payload.is_active,
        )
    except DuplicateValueMapError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        if "compatibility model values" in str(exc):
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _value_map_schema(value_map)


@router.patch("/matching/property-value-maps/{value_map_id}", response_model=PropertyValueMap)
def patch_property_value_map(
    value_map_id: int,
    payload: PropertyValueMapPatch,
    db: Session = Depends(get_db),
    _: str = Depends(_authorize),
) -> PropertyValueMap:
    try:
        value_map = update_value_map(
            db,
            value_map_id,
            **payload.model_dump(exclude_unset=True),
        )
    except DuplicateValueMapError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if value_map is None:
        raise HTTPException(status_code=404, detail="property value map not found")
    return _value_map_schema(value_map)


@router.post("/matching/property-rules/{rule_id}/restore-default", response_model=PropertyRule)
def restore_property_rule_default(
    rule_id: int,
    db: Session = Depends(get_db),
    user: str = Depends(_authorize),
) -> PropertyRule:
    try:
        rule = restore_default_rule(db, rule_id, actor=user)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if rule is None:
        raise HTTPException(status_code=404, detail="property rule not found")
    return _rule_schema(rule)


@router.get("/matching/property-value-suggestions", response_model=list[PropertyValueSuggestion])
def get_property_value_suggestions(
    profile_code: str = Query(...),
    rule_id: int | None = Query(None),
    source: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    _: str = Depends(_authorize),
) -> list[PropertyValueSuggestion]:
    try:
        return [
            PropertyValueSuggestion(
                rule_id=item.rule_id,
                property_key=item.property_key,
                competitor_source=item.competitor_source,
                competitor_value=item.competitor_value,
                count=item.count,
                sample_competitor_item_id=item.sample_competitor_item_id,
                sample_name=item.sample_name,
                suggested_mapped_value=item.suggested_mapped_value,
                safe_auto=item.safe_auto,
                safe_reason=item.safe_reason,
            )
            for item in list_value_suggestions(
                db,
                profile_code=profile_code,
                rule_id=rule_id,
                source=source,
                limit=limit,
            )
        ]
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/matching/property-value-suggestions/accept-safe",
    response_model=AcceptSafePropertyValueSuggestionsResponse,
)
def accept_safe_property_value_suggestions(
    payload: AcceptSafePropertyValueSuggestionsRequest,
    db: Session = Depends(get_db),
    _: str = Depends(_authorize),
) -> AcceptSafePropertyValueSuggestionsResponse:
    try:
        result = accept_safe_value_suggestions(
            db,
            profile_code=payload.profile_code,
            rule_id=payload.rule_id,
            source=payload.source,
            limit=payload.limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return AcceptSafePropertyValueSuggestionsResponse(
        created_count=result.created_count,
        skipped_count=result.skipped_count,
        created=[
            PropertyValueSuggestion(
                rule_id=item.rule_id,
                property_key=item.property_key,
                competitor_source=item.competitor_source,
                competitor_value=item.competitor_value,
                count=item.count,
                sample_competitor_item_id=item.sample_competitor_item_id,
                sample_name=item.sample_name,
                suggested_mapped_value=item.suggested_mapped_value,
                safe_auto=item.safe_auto,
                safe_reason=item.safe_reason,
            )
            for item in result.created
        ],
    )


@router.get("/matching/compatibility/summary", response_model=CompatibilitySummarySchema)
def get_compatibility_summary(
    db: Session = Depends(get_db),
    _: str = Depends(_authorize),
) -> CompatibilitySummarySchema:
    return _compatibility_summary_schema(CompatibilityMappingService(db).summary())


@router.get("/matching/compatibility/brands", response_model=list[CompatibilityBrand])
def get_compatibility_brands(
    q: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    _: str = Depends(_authorize),
) -> list[CompatibilityBrand]:
    return [
        _compatibility_brand_schema(row)
        for row in CompatibilityMappingService(db).list_brands(q=q, limit=limit)
    ]


@router.post("/matching/compatibility/brands", response_model=CompatibilityBrand)
def create_compatibility_brand(
    payload: CompatibilityBrandRequest,
    db: Session = Depends(get_db),
    _: str = Depends(_authorize),
) -> CompatibilityBrand:
    try:
        row = CompatibilityMappingService(db).create_brand(
            code=payload.code,
            name=payload.name,
            display_name=payload.display_name,
            group_code=payload.group_code,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _compatibility_brand_schema(row)


@router.post("/matching/compatibility/brand-aliases", response_model=CompatibilityBrand)
def create_compatibility_brand_alias(
    payload: CompatibilityBrandAliasRequest,
    db: Session = Depends(get_db),
    user: str = Depends(_authorize),
) -> CompatibilityBrand:
    try:
        row = CompatibilityMappingService(db).create_brand_alias(
            brand_id=payload.brand_id,
            raw_value=payload.raw_value,
            source=payload.source,
            actor=user,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _compatibility_brand_schema(row)


@router.get("/matching/compatibility/brand-aliases", response_model=list[CompatibilityBrandAlias])
def get_compatibility_brand_aliases(
    brand_id: int | None = Query(None),
    q: str | None = Query(None),
    include_inactive: bool = Query(False),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    _: str = Depends(_authorize),
) -> list[CompatibilityBrandAlias]:
    return [
        _compatibility_brand_alias_schema(row)
        for row in CompatibilityMappingService(db).list_brand_aliases(
            brand_id=brand_id,
            q=q,
            include_inactive=include_inactive,
            limit=limit,
        )
    ]


@router.patch(
    "/matching/compatibility/brand-aliases/{alias_id}",
    response_model=CompatibilityBrandAlias,
)
def patch_compatibility_brand_alias(
    alias_id: int,
    payload: CompatibilityBrandAliasPatch,
    db: Session = Depends(get_db),
    _: str = Depends(_authorize),
) -> CompatibilityBrandAlias:
    if payload.is_active is None:
        raise HTTPException(status_code=400, detail="is_active is required")
    try:
        row = CompatibilityMappingService(db).set_brand_alias_active(
            alias_id=alias_id,
            is_active=payload.is_active,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _compatibility_brand_alias_schema(row)


@router.get("/matching/compatibility/models", response_model=list[CompatibilityPhoneModel])
def get_compatibility_models(
    brand_id: int | None = Query(None),
    q: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    _: str = Depends(_authorize),
) -> list[CompatibilityPhoneModel]:
    return [
        _compatibility_model_schema(row)
        for row in CompatibilityMappingService(db).list_models(
            brand_id=brand_id,
            q=q,
            limit=limit,
        )
    ]


@router.post("/matching/compatibility/models", response_model=CompatibilityPhoneModel)
def create_compatibility_model(
    payload: CompatibilityPhoneModelRequest,
    db: Session = Depends(get_db),
    _: str = Depends(_authorize),
) -> CompatibilityPhoneModel:
    try:
        row = CompatibilityMappingService(db).create_model(
            brand_id=payload.brand_id,
            model_name=payload.model_name,
            variant=payload.variant,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _compatibility_model_schema(row)


@router.get(
    "/matching/compatibility/unresolved-groups",
    response_model=list[CompatibilityUnresolvedGroup],
)
def get_compatibility_unresolved_groups(
    entity_type: str | None = Query(None),
    brand_id: int | None = Query(None),
    without_brand: bool = Query(False),
    source: str | None = Query(None),
    q: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    _: str = Depends(_authorize),
) -> list[CompatibilityUnresolvedGroup]:
    return [
        _compatibility_group_schema(row)
        for row in CompatibilityMappingService(db).list_unresolved_groups(
            entity_type=entity_type,
            brand_id=brand_id,
            without_brand=without_brand,
            source=source,
            q=q,
            limit=limit,
        )
    ]


@router.get("/matching/compatibility/unresolved", response_model=list[CompatibilityUnresolvedItem])
def get_compatibility_unresolved(
    entity_type: str | None = Query(None),
    brand_id: int | None = Query(None),
    source: str | None = Query(None),
    q: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    _: str = Depends(_authorize),
) -> list[CompatibilityUnresolvedItem]:
    return [
        _compatibility_unresolved_schema(row)
        for row in CompatibilityMappingService(db).list_unresolved(
            entity_type=entity_type,
            brand_id=brand_id,
            source=source,
            q=q,
            limit=limit,
        )
    ]


@router.post(
    "/matching/compatibility/unresolved/preview", response_model=CompatibilityPreviewResponse
)
def preview_compatibility_mapping(
    payload: CompatibilityPreviewRequest,
    db: Session = Depends(get_db),
    _: str = Depends(_authorize),
) -> CompatibilityPreviewResponse:
    try:
        preview = CompatibilityMappingService(db).preview(
            group_key=payload.group_key,
            entity_type=payload.entity_type,
            source=payload.source,
            raw_value=payload.raw_value,
            raw_brand=payload.raw_brand,
            raw_model=payload.raw_model,
            raw_variant=payload.raw_variant,
            brand_id=payload.brand_id,
            target_phone_model_ids=payload.target_phone_model_ids,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _compatibility_preview_schema(preview)


@router.post("/matching/compatibility/unresolved/apply", response_model=CompatibilityApplyResponse)
def apply_compatibility_mapping(
    payload: CompatibilityApplyRequest,
    db: Session = Depends(get_db),
    user: str = Depends(_authorize),
) -> CompatibilityApplyResponse:
    if payload.scope not in {"previewed", "group"}:
        raise HTTPException(status_code=400, detail="scope must be previewed or group")
    try:
        result = CompatibilityMappingService(db).apply(
            group_key=payload.group_key,
            entity_type=payload.entity_type,
            source=payload.source,
            raw_value=payload.raw_value,
            raw_brand=payload.raw_brand,
            raw_model=payload.raw_model,
            raw_variant=payload.raw_variant,
            brand_id=payload.brand_id,
            target_phone_model_ids=payload.target_phone_model_ids,
            preview_token=payload.preview_token,
            actor=user,
            notes=payload.notes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _compatibility_apply_schema(result)


@router.post("/matching/compatibility/unresolved/block", response_model=CompatibilityApplyResponse)
def block_compatibility_mapping(
    payload: CompatibilityBlockRequest,
    db: Session = Depends(get_db),
    user: str = Depends(_authorize),
) -> CompatibilityApplyResponse:
    try:
        result = CompatibilityMappingService(db).block(
            group_key=payload.group_key,
            entity_type=payload.entity_type,
            source=payload.source,
            raw_value=payload.raw_value,
            raw_brand=payload.raw_brand,
            raw_model=payload.raw_model,
            raw_variant=payload.raw_variant,
            reason=payload.reason,
            actor=user,
            notes=payload.notes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _compatibility_apply_schema(result)


@router.get("/matching/compatibility/history", response_model=list[CompatibilityHistoryItem])
def get_compatibility_history(
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    _: str = Depends(_authorize),
) -> list[CompatibilityHistoryItem]:
    return [
        _compatibility_history_schema(row)
        for row in CompatibilityMappingService(db).list_history(limit=limit)
    ]


@router.get(
    "/matching/compatibility/display-families/summary",
    response_model=DisplayFamilyRegistrySummarySchema,
)
def get_display_family_registry_summary(
    db: Session = Depends(get_db),
    _: str = Depends(_authorize),
) -> DisplayFamilyRegistrySummarySchema:
    return DisplayFamilyRegistrySummarySchema.model_validate(display_family_registry_summary(db))


@router.get(
    "/matching/compatibility/display-families/versions",
    response_model=list[DisplayFamilyRegistryVersionSchema],
)
def get_display_family_registry_versions(
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _: str = Depends(_authorize),
) -> list[DisplayFamilyRegistryVersionSchema]:
    return [
        DisplayFamilyRegistryVersionSchema.model_validate(item)
        for item in list_display_family_registry_versions(db, limit=limit)
    ]


@router.get(
    "/matching/compatibility/display-families",
    response_model=PaginatedDisplayFamiliesSchema,
)
def get_display_families(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    search: str | None = Query(None),
    singleton: bool | None = Query(None),
    has_warnings: bool | None = Query(None),
    needs_review: bool | None = Query(None),
    matching_review: bool | None = Query(None),
    quality_unknown: bool | None = Query(None),
    db: Session = Depends(get_db),
    _: str = Depends(_authorize),
) -> PaginatedDisplayFamiliesSchema:
    return PaginatedDisplayFamiliesSchema.model_validate(
        list_active_display_families(
            db,
            page=page,
            page_size=page_size,
            search=search,
            singleton=singleton,
            has_warnings=has_warnings,
            needs_review=needs_review,
            matching_review=matching_review,
            quality_unknown=quality_unknown,
        )
    )


@router.get(
    "/matching/compatibility/display-families/{family_id}",
    response_model=DisplayFamilyDetailSchema,
)
def get_display_family(
    family_id: int,
    db: Session = Depends(get_db),
    _: str = Depends(_authorize),
) -> DisplayFamilyDetailSchema:
    payload = get_active_display_family_detail(db, family_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="display family not found in active version")
    return DisplayFamilyDetailSchema.model_validate(payload)


@router.get("/matching/products", response_model=PaginatedProducts)
def list_products(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    status: list[MatchStatus] | None = Query(None, description="Filter by status"),
    brand: str | None = None,
    category: str | None = None,
    compatibility_brand: str | None = Query(None, description="Filter by compatible device brand"),
    subject: str | None = Query(None, description="Filter by product subject"),
    search: str | None = Query(None, description="search in name or article"),
    sort: ProductSort = Query(
        ProductSort.default,
        description="Product list sorting mode. Use name_asc for nomenclature A-Z.",
    ),
    include_live_counts: bool = Query(
        True,
        description="Compute live candidate counts. Disable for faster product filtering when live status is not needed.",
    ),
    db: Session = Depends(get_db),
    _: str = Depends(_authorize),
) -> PaginatedProducts:
    counts_sq = (
        select(
            CompetitorItemMatch.product_id.label("product_id"),
            func.sum(
                case((CompetitorItemMatch.status == CompetitorItemMatchStatus.ACCEPTED, 1), else_=0)
            ).label("accepted_count"),
            func.count(
                func.distinct(
                    case(
                        (
                            CompetitorItemMatch.status == CompetitorItemMatchStatus.ACCEPTED,
                            CompetitorItem.competitor,
                        )
                    )
                )
            ).label("accepted_competitor_count"),
            func.sum(
                case(
                    (
                        and_(
                            CompetitorItemMatch.status == CompetitorItemMatchStatus.ACCEPTED,
                            CompetitorItemMatch.method == CompetitorItemMatchMethod.MANUAL,
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("manual_count"),
            func.sum(
                case(
                    (CompetitorItemMatch.status == CompetitorItemMatchStatus.SUGGESTED, 1), else_=0
                )
            ).label("suggested_count"),
            func.sum(
                case(
                    (CompetitorItemMatch.status == CompetitorItemMatchStatus.NEEDS_REVIEW, 1),
                    else_=0,
                )
            ).label("review_count"),
            func.sum(
                case(
                    (CompetitorItemMatch.status == CompetitorItemMatchStatus.AMBIGUOUS, 1), else_=0
                )
            ).label("ambiguous_count"),
        )
        .join(CompetitorItem, CompetitorItem.id == CompetitorItemMatch.competitor_item_id)
        .group_by(CompetitorItemMatch.product_id)
        .subquery()
    )

    query = select(Product, counts_sq).outerjoin(counts_sq, counts_sq.c.product_id == Product.id)
    query = query.where(Product.is_active.is_(True))
    if search:
        pattern = f"%{search}%"
        query = query.where(
            or_(
                Product.name.ilike(pattern),
                Product.article.ilike(pattern),
                Product.fact_sku.ilike(pattern),
                Product.planned_sku.ilike(pattern),
                Product.code_1c.ilike(pattern),
            )
        )

    rows_for_filter = db.execute(query).all()
    status_filtered: list[tuple[Product, dict[str, int], MatchStatus]] = []
    requested_statuses = set(status or [])
    live_sensitive_statuses = {MatchStatus.none, MatchStatus.live_candidates}
    needs_live_status_filter = bool(requested_statuses & live_sensitive_statuses)
    should_compute_live_counts = include_live_counts
    live_candidate_counts_by_product: dict[int, int] = {}
    for row in rows_for_filter:
        product = row[0]
        counts = {
            "accepted_count": int(row.accepted_count or 0),
            "accepted_competitor_count": int(row.accepted_competitor_count or 0),
            "manual_count": int(row.manual_count or 0),
            "suggested_count": int(row.suggested_count or 0),
            "review_count": int(row.review_count or 0),
            "ambiguous_count": int(row.ambiguous_count or 0),
        }
        computed = _product_status(**counts)
        if status and not any(_status_filter_allowed(s, **counts) for s in status):
            continue
        status_filtered.append((product, counts, computed))

    if needs_live_status_filter:
        live_candidate_counts_by_product.update(
            _live_candidate_cache_by_product(
                db,
                [
                    product.id
                    for product, _, computed in status_filtered
                    if computed == MatchStatus.none
                ],
            )
        )
        live_filtered: list[tuple[Product, dict[str, int], MatchStatus]] = []
        non_live_statuses = requested_statuses - live_sensitive_statuses
        for product, counts, computed in status_filtered:
            include_non_live = bool(non_live_statuses) and any(
                _status_filter_allowed(s, **counts) for s in non_live_statuses
            )
            include_live = False
            include_empty = False
            if computed == MatchStatus.none:
                live_count = live_candidate_counts_by_product.get(product.id)
                if live_count is None and should_compute_live_counts:
                    live_count = _live_candidate_count_for_product(db, product)
                    live_candidate_counts_by_product[product.id] = live_count
                if live_count is None:
                    live_count = 0
                live_candidate_counts_by_product[product.id] = live_count
                include_live = MatchStatus.live_candidates in requested_statuses and live_count > 0
                include_empty = MatchStatus.none in requested_statuses and live_count == 0
            if include_non_live or include_live or include_empty:
                live_filtered.append((product, counts, computed))
        status_filtered = live_filtered

    compatibility_brand_filter = _normalize_compatibility_brand(compatibility_brand)
    compatibility_brands_by_product, compatibility_brand_labels = _compatibility_brands_by_product(
        db, [product.id for product, _, _ in status_filtered]
    )
    facets = _build_product_facets(
        status_filtered,
        compatibility_brands_by_product,
        compatibility_brand_labels,
    )
    filtered = [
        (product, counts, computed)
        for product, counts, computed in status_filtered
        if (not brand or product.brand == brand)
        and _product_category_matches(product, category)
        and (
            not compatibility_brand_filter
            or compatibility_brand_filter in compatibility_brands_by_product.get(product.id, set())
        )
        and (not subject or _product_subject(product) == subject)
    ]
    if sort == ProductSort.name_asc:
        filtered.sort(key=lambda row: _product_name_sort_value(row[0]))

    total = len(filtered)
    page_rows = filtered[(page - 1) * page_size : page * page_size]
    product_ids = [product.id for product, _, _ in page_rows]
    matches_by_product: dict[int, CompetitorItemMatch] = {}
    candidate_previews_by_product: dict[int, list[CurrentMatch]] = {}
    compatibility_models_by_product: dict[int, list[str]] = {}
    if product_ids:
        compatibility_models_by_product = _compatibility_models_by_product(db, product_ids)
        live_candidate_counts_by_product.update(_live_candidate_cache_by_product(db, product_ids))
        current_matches = (
            db.execute(
                select(CompetitorItemMatch)
                .options(joinedload(CompetitorItemMatch.competitor_item))
                .where(
                    CompetitorItemMatch.product_id.in_(product_ids),
                    CompetitorItemMatch.status == CompetitorItemMatchStatus.ACCEPTED,
                )
                .order_by(CompetitorItemMatch.updated_at.desc())
            )
            .scalars()
            .all()
        )
        for match in current_matches:
            matches_by_product.setdefault(match.product_id, match)
        candidate_matches = (
            db.execute(
                select(CompetitorItemMatch)
                .options(joinedload(CompetitorItemMatch.competitor_item))
                .where(
                    CompetitorItemMatch.product_id.in_(product_ids),
                    CompetitorItemMatch.status.in_(
                        [
                            CompetitorItemMatchStatus.SUGGESTED,
                            CompetitorItemMatchStatus.NEEDS_REVIEW,
                            CompetitorItemMatchStatus.AMBIGUOUS,
                        ]
                    ),
                )
                .order_by(
                    CompetitorItemMatch.product_id,
                    case(
                        (CompetitorItemMatch.status == CompetitorItemMatchStatus.SUGGESTED, 0),
                        (CompetitorItemMatch.status == CompetitorItemMatchStatus.NEEDS_REVIEW, 1),
                        (CompetitorItemMatch.status == CompetitorItemMatchStatus.AMBIGUOUS, 2),
                        else_=3,
                    ),
                    CompetitorItemMatch.final_score.desc().nullslast(),
                    CompetitorItemMatch.updated_at.desc(),
                )
            )
            .scalars()
            .all()
        )
        for match in candidate_matches:
            preview = _current_match(match)
            if preview is None:
                continue
            previews = candidate_previews_by_product.setdefault(match.product_id, [])
            if len(previews) < 3:
                previews.append(preview)
        for product, _, computed in page_rows:
            if (
                should_compute_live_counts
                and computed == MatchStatus.none
                and product.id not in live_candidate_counts_by_product
            ):
                live_candidate_counts_by_product[product.id] = _live_candidate_count_for_product(
                    db, product
                )

    items = [
        ProductRow(
            id=product.id,
            name=product.name,
            article=product.article,
            brand=product.brand,
            category=product.category,
            subject=_product_subject(product),
            status=(
                MatchStatus.live_candidates
                if computed == MatchStatus.none
                and live_candidate_counts_by_product.get(product.id, 0) > 0
                else computed
            ),
            current_match=_current_match(matches_by_product.get(product.id)),
            candidate_previews=candidate_previews_by_product.get(product.id, []),
            accepted_count=counts["accepted_count"],
            suggested_count=counts["suggested_count"],
            review_count=counts["review_count"] + counts["ambiguous_count"],
            live_candidate_count=live_candidate_counts_by_product.get(product.id, 0),
            compatibility_models=compatibility_models_by_product.get(product.id, []),
        )
        for product, counts, computed in page_rows
    ]
    return PaginatedProducts(
        items=items,
        page=page,
        page_size=page_size,
        total=total,
        facets=facets,
    )


@router.get("/matching/products/{product_id}/candidate-search", response_model=PaginatedCandidates)
def search_product_candidates(
    product_id: int,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=200),
    q: str | None = Query(None, description="search in competitor item content"),
    source: str | None = None,
    include_rejected: bool = False,
    in_stock: bool | None = Query(None),
    category_group: str | None = None,
    item_type: str | None = None,
    brand: str | None = None,
    model: str | None = None,
    quality: str | None = None,
    color: str | None = None,
    candidate_status: str | None = Query(
        None,
        description="Filter competitor items by matching status: free/current/locked/suggested/needs_review/ambiguous/rejected",
    ),
    price_min: float | None = None,
    price_max: float | None = None,
    include_property_summary: bool = Query(
        False,
        description="Include compact property mapping summary for visible page items.",
    ),
    db: Session = Depends(get_db),
    _: str = Depends(_authorize),
) -> PaginatedCandidates:
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(status_code=404, detail="product not found")

    rejected_ids = _rejected_item_ids_for_product(db, product_id)
    query = (
        select(CompetitorItem, CompetitorItemMatch)
        .options(
            selectinload(CompetitorItem.compatibilities).selectinload(
                CompetitorItemCompatibility.phone_model
            ),
            selectinload(CompetitorItem.compatibilities).selectinload(
                CompetitorItemCompatibility.device_brand_ref
            ),
        )
        .outerjoin(CompetitorItemMatch, CompetitorItemMatch.competitor_item_id == CompetitorItem.id)
        .where(CompetitorItem.is_active.is_(True))
    )
    if source:
        query = query.where(CompetitorItem.competitor == source)
    if in_stock is True:
        query = query.where(CompetitorItem.availability.is_(True))
    if category_group:
        query = query.where(CompetitorItem.category_group == category_group)
    precise_query = _is_precise_candidate_query(q)
    if item_type:
        query = _apply_candidate_item_type_filter(query, item_type)
    elif not precise_query:
        query = _apply_candidate_item_type_filter(query, _infer_product_item_type(product))
    if brand:
        query = query.where(
            or_(
                CompetitorItem.item_brand.ilike(f"%{brand}%"),
                CompetitorItem.parsed_device_brand.ilike(f"%{brand}%"),
            )
        )
    if model:
        query = query.where(
            or_(
                CompetitorItem.attrs_model.ilike(f"%{model}%"),
                CompetitorItem.parsed_device_model.ilike(f"%{model}%"),
                CompetitorItem.name.ilike(f"%{model}%"),
            )
        )
    if quality:
        query = query.where(
            or_(
                CompetitorItem.attrs_quality.ilike(f"%{quality}%"),
                CompetitorItem.screen_quality_grade.ilike(f"%{quality}%"),
            )
        )
    if color:
        query = query.where(
            or_(
                CompetitorItem.attrs_color.ilike(f"%{color}%"),
                CompetitorItem.color.ilike(f"%{color}%"),
            )
        )
    price_expr = func.coalesce(CompetitorItem.price_roz, CompetitorItem.price_opt)
    if price_min is not None:
        query = query.where(price_expr >= price_min)
    if price_max is not None:
        query = query.where(price_expr <= price_max)
    rank_terms: list[str] = []
    if q:
        terms = _search_tokens(q)
        exact_condition = _candidate_exact_search_condition(q)
        moba_url_fallback_condition = None
        moba_url_rank_terms: list[str] = []
        if _is_moba_catalog_url(q):
            moba_url_fallback_condition, moba_url_rank_terms = _default_candidate_condition(product)
            item_type_condition = _candidate_item_type_condition(_infer_product_item_type(product))
            if item_type_condition is not None:
                moba_url_fallback_condition = and_(
                    moba_url_fallback_condition,
                    item_type_condition,
                )
        if terms:
            terms_condition = _candidate_terms_condition(terms)
            search_conditions = [
                condition
                for condition in (
                    exact_condition,
                    terms_condition,
                    moba_url_fallback_condition,
                )
                if condition is not None
            ]
            if search_conditions:
                query = query.where(
                    search_conditions[0] if len(search_conditions) == 1 else or_(*search_conditions)
                )
            rank_terms = moba_url_rank_terms if moba_url_rank_terms else terms
        else:
            pattern = f"%{q}%"
            fallback_condition = or_(
                CompetitorItem.name.ilike(pattern),
                CompetitorItem.external_id.ilike(pattern),
                CompetitorItem.competitor.ilike(pattern),
                CompetitorItem.normalized_title.ilike(pattern),
            )
            search_conditions = [
                condition
                for condition in (
                    exact_condition,
                    fallback_condition,
                    moba_url_fallback_condition,
                )
                if condition is not None
            ]
            query = query.where(
                search_conditions[0] if len(search_conditions) == 1 else or_(*search_conditions)
            )
            rank_terms = moba_url_rank_terms
    else:
        query, rank_terms = _apply_default_candidate_filter(query, product)

    order_by = [
        case((CompetitorItemMatch.product_id == product_id, 1), else_=0).desc(),
    ]
    if q:
        cleaned_q = _clean_candidate_query(q)
        if cleaned_q:
            order_by.append(case((CompetitorItem.external_id.ilike(cleaned_q), 1), else_=0).desc())
    item_type_hint = _infer_product_item_type(product)
    if item_type_hint:
        order_by.append(case((CompetitorItem.item_type == item_type_hint, 1), else_=0).desc())
    score_expr = _candidate_search_score(rank_terms, include_external_id=bool(q))
    if score_expr is not None:
        order_by.append(score_expr.desc())
    order_by.extend(
        [
            CompetitorItemMatch.final_score.desc().nullslast(),
            CompetitorItem.availability.desc(),
            CompetitorItem.last_seen_at.desc().nullslast(),
            CompetitorItem.id.desc(),
        ]
    )

    all_rows = db.execute(query.order_by(*order_by)).all()
    all_candidates = [
        _candidate_from_item(
            item,
            match=match,
            product=product,
            product_id=product_id,
            rejected_ids=rejected_ids,
        )
        for item, match in all_rows
        if (include_rejected or item.id not in rejected_ids)
        and _candidate_guardrail_allowed(
            item,
            product,
            match,
            include_locked_conflicts=(candidate_status == "locked"),
        )
    ]
    all_candidates = [
        candidate
        for candidate in all_candidates
        if _candidate_status_filter_allowed(candidate, candidate_status)
    ]
    total = len(all_candidates)
    page_items = all_candidates[offset : offset + limit]
    if include_property_summary:
        item_by_id = {item.id: item for item, _ in all_rows}
        for candidate in page_items:
            if candidate.competitor_item_id is None:
                continue
            item = item_by_id.get(candidate.competitor_item_id)
            if item is None:
                continue
            candidate.property_summary = _property_summary_schema(
                evaluate_property_comparison(db, product, item)
            )
    return PaginatedCandidates(items=page_items, total=total, facets=_build_facets(all_candidates))


@router.get(
    "/matching/products/{product_id}/candidates/{competitor_item_id}/properties",
    response_model=PropertyComparisonResponse,
)
def get_candidate_property_comparison(
    product_id: int,
    competitor_item_id: int,
    profile_code: str | None = Query(None),
    db: Session = Depends(get_db),
    _: str = Depends(_authorize),
) -> PropertyComparisonResponse:
    product = db.get(Product, product_id)
    item = db.get(CompetitorItem, competitor_item_id)
    if not product or not item:
        raise HTTPException(status_code=404, detail="product or competitor item not found")
    result = evaluate_property_comparison(db, product, item, profile_code=profile_code)
    if result is None:
        raise HTTPException(status_code=404, detail="property profile not found")
    return _property_comparison_schema(result)


@router.get("/matching/products/{product_id}/candidates", response_model=PaginatedCandidates)
def list_candidates(
    product_id: int,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=200),
    source: str | None = Query(None),
    include_rejected: bool = False,
    q: str | None = Query(None),
    in_stock: bool | None = Query(None),
    candidate_status: str | None = Query(None),
    db: Session = Depends(get_db),
    _: str = Depends(_authorize),
) -> PaginatedCandidates:
    return search_product_candidates(
        product_id=product_id,
        offset=offset,
        limit=limit,
        q=q,
        source=source,
        include_rejected=include_rejected,
        in_stock=in_stock,
        candidate_status=candidate_status,
        include_property_summary=False,
        db=db,
        _=_,
    )


@router.get("/matching/candidates", response_model=PaginatedCandidates)
def search_candidates(
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=200),
    q: str | None = Query(None),
    in_stock: bool | None = Query(None),
    db: Session = Depends(get_db),
    _: str = Depends(_authorize),
) -> PaginatedCandidates:
    query = (
        select(CompetitorItem)
        .options(
            selectinload(CompetitorItem.compatibilities).selectinload(
                CompetitorItemCompatibility.phone_model
            ),
            selectinload(CompetitorItem.compatibilities).selectinload(
                CompetitorItemCompatibility.device_brand_ref
            ),
        )
        .where(CompetitorItem.is_active.is_(True))
    )
    if in_stock is True:
        query = query.where(CompetitorItem.availability.is_(True))
    if q:
        pattern = f"%{q}%"
        fallback_condition = or_(
            CompetitorItem.name.ilike(pattern),
            CompetitorItem.external_id.ilike(pattern),
            CompetitorItem.competitor.ilike(pattern),
            CompetitorItem.normalized_title.ilike(pattern),
        )
        exact_condition = _candidate_exact_search_condition(q)
        if exact_condition is not None:
            query = query.where(or_(exact_condition, fallback_condition))
        else:
            query = query.where(fallback_condition)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    items = (
        db.execute(
            query.order_by(CompetitorItem.last_seen_at.desc().nullslast(), CompetitorItem.id.desc())
            .offset(offset)
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return PaginatedCandidates(items=[_candidate_from_item(item) for item in items], total=total)


@router.post("/matching/products/{product_id}/matches", response_model=CurrentMatch)
def accept_item_match(
    product_id: int,
    payload: MatchRequest,
    db: Session = Depends(get_db),
    user: str = Depends(_authorize),
) -> CurrentMatch:
    if payload.competitor_item_id is None:
        raise HTTPException(status_code=422, detail="competitor_item_id is required")
    product = db.get(Product, product_id)
    item = db.get(CompetitorItem, payload.competitor_item_id)
    if not product or not item:
        raise HTTPException(status_code=404, detail="product or competitor item not found")
    existing = (
        db.execute(
            select(CompetitorItemMatch)
            .options(joinedload(CompetitorItemMatch.competitor_item))
            .where(CompetitorItemMatch.competitor_item_id == item.id)
        )
        .scalars()
        .one_or_none()
    )
    if (
        existing
        and existing.product_id != product_id
        and existing.status == CompetitorItemMatchStatus.ACCEPTED
        and existing.method == CompetitorItemMatchMethod.MANUAL
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "error": "already_accepted",
                "product_id": existing.product_id,
                "competitor_item_id": item.id,
            },
        )
    guardrail = basic_candidate_guardrails(item, product)
    if not guardrail.allowed:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "candidate_guardrail_blocked",
                "reason": guardrail.reason,
                "competitor_item_id": item.id,
            },
        )
    first_seen_date = _as_date(item.first_seen_at)
    if (
        first_seen_date is not None
        and first_seen_date >= UNSAFE_ACCEPT_CUTOFF
        and not _ensure_accept_compatibility_from_product(db, product, item)
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "error": "compatibility_required",
                "reason": "new competitor item has no compatibility",
                "competitor_item_id": item.id,
            },
        )

    previous_product_id = existing.product_id if existing else None
    previous_status = _match_status_value(existing.status) if existing else None
    match = existing or CompetitorItemMatch(competitor_item_id=item.id, product_id=product_id)
    match.product_id = product_id
    match.status = CompetitorItemMatchStatus.ACCEPTED
    match.method = CompetitorItemMatchMethod.MANUAL
    match.final_score = payload.confidence if payload.confidence is not None else match.final_score
    match.competitor_item = item
    db.add(match)
    _record_decision(
        db,
        product_id=product_id,
        competitor_item_id=item.id,
        action="accept",
        user=user,
        reason=payload.reason,
        reason_code=payload.reason_code,
        previous_product_id=previous_product_id,
        previous_status=previous_status,
        product=product,
        item=item,
        match=match,
    )
    _invalidate_live_candidate_cache(db, product_id, previous_product_id)
    db.commit()
    db.refresh(match)
    return _current_match(match) or CurrentMatch(competitor_item_id=item.id, mode="manual")


@router.post("/matching/products/{product_id}/reject", response_model=MatchingActionResponse)
def reject_candidate(
    product_id: int,
    payload: RejectRequest,
    db: Session = Depends(get_db),
    user: str = Depends(_authorize),
) -> MatchingActionResponse:
    if payload.competitor_item_id is None:
        raise HTTPException(status_code=422, detail="competitor_item_id is required")
    product = db.get(Product, product_id)
    item = db.get(CompetitorItem, payload.competitor_item_id)
    if not product or not item:
        raise HTTPException(status_code=404, detail="product or competitor item not found")

    existing = (
        db.execute(
            select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id == item.id)
        )
        .scalars()
        .one_or_none()
    )
    previous_product_id = existing.product_id if existing else None
    previous_status = _match_status_value(existing.status) if existing else None
    if (
        existing
        and existing.product_id == product_id
        and existing.status == CompetitorItemMatchStatus.ACCEPTED
    ):
        raise HTTPException(status_code=409, detail="accepted match must be revoked before reject")
    if existing and existing.product_id == product_id:
        existing.status = CompetitorItemMatchStatus.REJECTED
        existing.method = CompetitorItemMatchMethod.MANUAL
        db.add(existing)
    _record_decision(
        db,
        product_id=product_id,
        competitor_item_id=item.id,
        action="reject",
        user=user,
        reason=payload.reason,
        reason_code=payload.reason_code,
        previous_product_id=previous_product_id,
        previous_status=previous_status,
        product=product,
        item=item,
        match=existing,
    )
    _invalidate_live_candidate_cache(db, product_id, previous_product_id)
    db.commit()
    return MatchingActionResponse(ok=True)


@router.post(
    "/matching/products/{product_id}/reject-bulk",
    response_model=BulkRejectResponse,
)
def reject_candidates_bulk(
    product_id: int,
    payload: BulkRejectRequest,
    db: Session = Depends(get_db),
    user: str = Depends(_authorize),
) -> BulkRejectResponse:
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(status_code=404, detail="product not found")

    item_ids = _dedupe_item_ids(payload.competitor_item_ids)
    if not item_ids:
        return BulkRejectResponse(ok=True)

    items_by_id = {
        item.id: item
        for item in db.execute(select(CompetitorItem).where(CompetitorItem.id.in_(item_ids)))
        .scalars()
        .all()
    }
    matches_by_item_id = {
        match.competitor_item_id: match
        for match in db.execute(
            select(CompetitorItemMatch).where(CompetitorItemMatch.competitor_item_id.in_(item_ids))
        )
        .scalars()
        .all()
    }
    rejected_ids = _rejected_item_ids_for_product(db, product_id)
    changed_product_ids: set[int | None] = {product_id}
    results: list[BulkRejectItemResult] = []
    rejected_count = 0
    skipped_count = 0

    for item_id in item_ids:
        item = items_by_id.get(item_id)
        if item is None:
            skipped_count += 1
            results.append(
                BulkRejectItemResult(
                    competitor_item_id=item_id,
                    status="skipped",
                    reason="not_found",
                )
            )
            continue

        existing = matches_by_item_id.get(item_id)
        previous_product_id = existing.product_id if existing else None
        previous_status = _match_status_value(existing.status) if existing else None
        if item_id in rejected_ids or (
            existing
            and existing.product_id == product_id
            and existing.status == CompetitorItemMatchStatus.REJECTED
        ):
            skipped_count += 1
            results.append(
                BulkRejectItemResult(
                    competitor_item_id=item_id,
                    status="skipped",
                    reason="already_rejected",
                )
            )
            continue
        if (
            existing
            and existing.product_id == product_id
            and existing.status == CompetitorItemMatchStatus.ACCEPTED
        ):
            skipped_count += 1
            results.append(
                BulkRejectItemResult(
                    competitor_item_id=item_id,
                    status="skipped",
                    reason="current",
                )
            )
            continue
        if (
            existing
            and existing.product_id != product_id
            and existing.status == CompetitorItemMatchStatus.ACCEPTED
        ):
            skipped_count += 1
            results.append(
                BulkRejectItemResult(
                    competitor_item_id=item_id,
                    status="skipped",
                    reason="locked",
                )
            )
            continue

        if existing and existing.product_id == product_id:
            existing.status = CompetitorItemMatchStatus.REJECTED
            existing.method = CompetitorItemMatchMethod.MANUAL
            db.add(existing)
        _record_decision(
            db,
            product_id=product_id,
            competitor_item_id=item_id,
            action="reject",
            user=user,
            reason=payload.reason,
            reason_code=payload.reason_code,
            previous_product_id=previous_product_id,
            previous_status=previous_status,
            product=product,
            item=item,
            match=existing,
        )
        changed_product_ids.add(previous_product_id)
        rejected_ids.add(item_id)
        rejected_count += 1
        results.append(
            BulkRejectItemResult(
                competitor_item_id=item_id,
                status="rejected",
                reason="rejected",
            )
        )

    _invalidate_live_candidate_cache(db, *changed_product_ids)
    db.commit()
    return BulkRejectResponse(
        ok=True,
        rejected_count=rejected_count,
        skipped_count=skipped_count,
        items=results,
    )


@router.post("/matching/products/{product_id}/revoke", response_model=MatchingActionResponse)
def revoke_item_match(
    product_id: int,
    payload: RevokeRequest,
    db: Session = Depends(get_db),
    user: str = Depends(_authorize),
) -> MatchingActionResponse:
    product = db.get(Product, product_id)
    item = db.get(CompetitorItem, payload.competitor_item_id)
    if not product or not item:
        raise HTTPException(status_code=404, detail="product or competitor item not found")
    match = (
        db.execute(
            select(CompetitorItemMatch).where(
                CompetitorItemMatch.product_id == product_id,
                CompetitorItemMatch.competitor_item_id == item.id,
                CompetitorItemMatch.status == CompetitorItemMatchStatus.ACCEPTED,
            )
        )
        .scalars()
        .one_or_none()
    )
    rejected_match = None
    latest_reject_decision = None
    if match is None:
        rejected_match = (
            db.execute(
                select(CompetitorItemMatch).where(
                    CompetitorItemMatch.product_id == product_id,
                    CompetitorItemMatch.competitor_item_id == item.id,
                    CompetitorItemMatch.status == CompetitorItemMatchStatus.REJECTED,
                )
            )
            .scalars()
            .one_or_none()
        )
        latest_reject_decision = _latest_reject_decision_for_product_item(db, product_id, item.id)
        if item.id not in _rejected_item_ids_for_product(db, product_id) and rejected_match is None:
            raise HTTPException(status_code=404, detail="match or rejected decision not found")

    previous_product_id = match.product_id if match is not None else product_id
    previous_status = (
        _match_status_value(match.status)
        if match is not None
        else (
            _match_status_value(rejected_match.status)
            if rejected_match is not None
            else CompetitorItemMatchStatus.REJECTED.value
        )
    )
    _record_decision(
        db,
        product_id=product_id,
        competitor_item_id=item.id,
        action="revoke",
        user=user,
        reason=payload.reason,
        reason_code=payload.reason_code,
        previous_product_id=previous_product_id,
        previous_status=previous_status,
        product=product,
        item=item,
        match=match or rejected_match,
    )
    _invalidate_live_candidate_cache(db, product_id)
    if match is not None:
        db.delete(match)
    elif rejected_match is not None:
        restored_status = _restorable_rejected_match_status(
            latest_reject_decision.previous_status if latest_reject_decision else None
        )
        if restored_status is None:
            db.delete(rejected_match)
        else:
            rejected_match.status = restored_status
            if rejected_match.method == CompetitorItemMatchMethod.MANUAL:
                rejected_match.method = CompetitorItemMatchMethod.EMBEDDING_AUTO
            db.add(rejected_match)
    db.commit()
    return MatchingActionResponse(ok=True)


@router.get("/matching/products/{product_id}/history", response_model=DecisionHistoryResponse)
def get_match_history(
    product_id: int,
    db: Session = Depends(get_db),
    _: str = Depends(_authorize),
) -> DecisionHistoryResponse:
    rows = (
        db.execute(
            select(ProductCompetitorItemDecision)
            .options(joinedload(ProductCompetitorItemDecision.competitor_item))
            .where(ProductCompetitorItemDecision.product_id == product_id)
            .order_by(ProductCompetitorItemDecision.id.desc())
        )
        .scalars()
        .all()
    )
    return DecisionHistoryResponse(
        items=[
            DecisionHistoryItem(
                id=row.id,
                product_id=row.product_id,
                competitor_item_id=row.competitor_item_id,
                competitor_name=row.competitor_item.competitor if row.competitor_item else None,
                sku=row.competitor_item.external_id if row.competitor_item else None,
                name=row.competitor_item.name if row.competitor_item else None,
                action=row.action,
                reason=row.reason,
                reason_code=row.reason_code,
                created_by=row.created_by,
                created_at=row.created_at,
                previous_product_id=row.previous_product_id,
                previous_status=row.previous_status,
                **snapshot_summary(row.snapshot_json),
            )
            for row in rows
        ]
    )


@router.post("/matching/products/{product_id}", response_model=CurrentMatch)
def accept_match_legacy(
    product_id: int,
    payload: MatchRequest,
    db: Session = Depends(get_db),
    user: str = Depends(_authorize),
) -> CurrentMatch:
    if payload.competitor_item_id is not None:
        return accept_item_match(product_id=product_id, payload=payload, db=db, user=user)
    if payload.competitor_id is None:
        raise HTTPException(
            status_code=422, detail="competitor_id or competitor_item_id is required"
        )

    product = db.get(Product, product_id)
    competitor = db.get(Competitor, payload.competitor_id)
    if not product or not competitor:
        raise HTTPException(status_code=404, detail="product or competitor not found")
    existing = (
        db.query(ProductMatch)
        .filter_by(product_id=product_id, competitor_id=payload.competitor_id)
        .one_or_none()
    )
    match = existing or ProductMatch(product_id=product_id, competitor_id=payload.competitor_id)
    match.is_manual = True
    match.confidence = payload.confidence if payload.confidence is not None else match.confidence
    db.add(match)
    db.commit()
    return CurrentMatch(
        competitor_id=match.competitor_id,
        competitor_name=competitor.name,
        sku=match.competitor_sku,
        confidence=match.confidence,
        mode="manual",
    )


@router.delete("/matching/products/{product_id}/{competitor_id}")
def revoke_match_legacy(
    product_id: int,
    competitor_id: int,
    db: Session = Depends(get_db),
    _: str = Depends(_authorize),
) -> dict[str, bool]:
    match = (
        db.query(ProductMatch)
        .filter_by(product_id=product_id, competitor_id=competitor_id)
        .one_or_none()
    )
    if not match:
        raise HTTPException(status_code=404, detail="match not found")
    db.delete(match)
    db.commit()
    return {"ok": True}


@router.get("/matching/competitors/search", response_model=PaginatedCandidates)
def search_competitors(
    q: str,
    brand: str | None = None,
    source: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    _: str = Depends(_authorize),
) -> PaginatedCandidates:
    return search_candidates(
        offset=(page - 1) * page_size,
        limit=page_size,
        q=f"{q} {brand}".strip() if brand else q,
        in_stock=None,
        db=db,
        _=_,
    )
