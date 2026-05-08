from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    CompatibilityMappingDecision,
    CompetitorItem,
    CompetitorItemCompatibility,
    DeviceBrand,
    DeviceBrandAlias,
    PhoneModel,
    PhoneModelAlias,
    Product,
    ProductCompatibility,
    ProductPhoneModel,
)
from app.services.device_brands import BrandResolver, brand_code_from_text, normalize_brand_key
from app.services.phone_model_canonicalization import (
    build_normalized_key,
    normalize_brand,
    normalize_model_name,
    parse_raw_device,
    screen_product_phone_compatibility,
)

DECISION_ACTION_MAP = "map"
DECISION_ACTION_BLOCK = "block"
ENTITY_PRODUCT = "product"
ENTITY_COMPETITOR_ITEM = "competitor_item"
GROUP_EXAMPLE_LIMIT = 5
HISTORY_LOOKBACK_LIMIT = 300
VALID_BLOCK_REASONS = {"noise", "not_phone", "bad_1c_value", "not_supported", "other"}
NOISE_VALUES = {"", "<>", "-", "--", "—", "n/a", "na", "none", "null", "нет", "не указано"}


def normalize_compatibility_text(value: str | None) -> str:
    normalized = str(value or "").strip().lower().replace("ё", "е")
    normalized = re.sub(r"[_\-]+", " ", normalized)
    normalized = re.sub(r"[^a-z0-9а-я\s]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def compatibility_identity_key(
    *,
    entity_type: str,
    source: str | None,
    raw_value: str,
    raw_brand: str | None = None,
    raw_model: str | None = None,
    raw_variant: str | None = None,
) -> str:
    parts = [
        entity_type,
        normalize_compatibility_text(source),
        normalize_compatibility_text(raw_value),
        normalize_compatibility_text(raw_brand),
        normalize_compatibility_text(raw_model),
        normalize_compatibility_text(raw_variant),
    ]
    return "|".join(parts)


def is_noise_compatibility_value(value: str | None) -> bool:
    raw = str(value or "").strip().lower()
    if raw in NOISE_VALUES:
        return True
    if not any(char.isalnum() for char in raw):
        return True
    return not normalize_compatibility_text(raw)


def group_key_for(
    *,
    entity_type: str,
    source: str | None,
    raw_value: str,
    raw_brand: str | None = None,
    raw_model: str | None = None,
    raw_variant: str | None = None,
) -> str:
    payload = {
        "entity_type": entity_type,
        "source": source,
        "raw_value": raw_value,
        "raw_brand": raw_brand,
        "raw_model": raw_model,
        "raw_variant": raw_variant,
        "normalized_key": compatibility_identity_key(
            entity_type=entity_type,
            source=source,
            raw_value=raw_value,
            raw_brand=raw_brand,
            raw_model=raw_model,
            raw_variant=raw_variant,
        ),
    }
    raw_payload = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw_payload).decode("ascii").rstrip("=")


def group_payload_from_key(group_key: str) -> dict[str, str | None]:
    try:
        padding = "=" * ((4 - len(group_key) % 4) % 4)
        raw_payload = base64.urlsafe_b64decode(f"{group_key}{padding}".encode("ascii"))
        payload = json.loads(raw_payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid compatibility group key") from exc
    required = {"entity_type", "raw_value", "normalized_key"}
    if not isinstance(payload, dict) or not required.issubset(payload):
        raise ValueError("invalid compatibility group key")
    expected_key = compatibility_identity_key(
        entity_type=str(payload.get("entity_type") or ""),
        source=payload.get("source"),
        raw_value=str(payload.get("raw_value") or ""),
        raw_brand=payload.get("raw_brand"),
        raw_model=payload.get("raw_model"),
        raw_variant=payload.get("raw_variant"),
    )
    if payload.get("normalized_key") != expected_key:
        raise ValueError("invalid compatibility group key")
    return {
        "entity_type": str(payload.get("entity_type") or ""),
        "source": payload.get("source"),
        "raw_value": str(payload.get("raw_value") or ""),
        "raw_brand": payload.get("raw_brand"),
        "raw_model": payload.get("raw_model"),
        "raw_variant": payload.get("raw_variant"),
        "normalized_key": expected_key,
    }


def preview_token_for(
    *,
    entity_type: str,
    source: str | None,
    raw_value: str,
    raw_brand: str | None,
    raw_model: str | None,
    raw_variant: str | None,
    brand_id: int | None,
    target_phone_model_ids: Iterable[int],
) -> str:
    payload = "|".join(
        [
            compatibility_identity_key(
                entity_type=entity_type,
                source=source,
                raw_value=raw_value,
                raw_brand=raw_brand,
                raw_model=raw_model,
                raw_variant=raw_variant,
            ),
            str(brand_id or ""),
            ",".join(str(item) for item in sorted(set(target_phone_model_ids))),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True)
class CompatibilitySummary:
    brands: int
    brand_aliases: int
    phone_models: int
    product_links: int
    competitor_links: int
    unresolved_product_values: int
    unresolved_competitor_values: int
    blocked_values: int


@dataclass(frozen=True)
class CompatibilityBrandRow:
    id: int
    code: str
    name: str
    display_name: str
    group_code: str | None
    is_active: bool
    models_count: int = 0
    unresolved_count: int = 0


@dataclass(frozen=True)
class CompatibilityPhoneModelRow:
    id: int
    brand_id: int | None
    brand_code: str | None
    brand_display_name: str | None
    brand: str
    model_name: str
    variant: str | None
    is_active: bool
    aliases_count: int = 0
    product_links_count: int = 0
    competitor_links_count: int = 0
    suggestion_kind: str | None = None


@dataclass(frozen=True)
class CompatibilityBrandAliasRow:
    id: int
    brand_id: int
    brand_display_name: str | None
    source: str
    raw_value: str
    normalized_key: str
    confidence: float | None
    is_manual: bool
    is_active: bool
    decision_reason: str | None
    first_seen_at: datetime
    last_seen_at: datetime


@dataclass(frozen=True)
class CompatibilityUnresolvedRow:
    entity_type: str
    entity_id: int
    source: str | None
    raw_value: str
    raw_brand: str | None = None
    raw_model: str | None = None
    raw_variant: str | None = None
    normalized_key: str = ""
    brand_id: int | None = None
    brand_display_name: str | None = None
    sample_name: str | None = None
    current_phone_model_ids: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class CompatibilityUnresolvedGroup:
    group_key: str
    entity_type: str
    source: str | None
    raw_value: str
    raw_brand: str | None
    raw_model: str | None
    raw_variant: str | None
    normalized_key: str
    brand_id: int | None
    brand_display_name: str | None
    affected_count: int
    product_count: int
    competitor_count: int
    examples: list[CompatibilityUnresolvedRow]
    suggested_phone_models: list[CompatibilityPhoneModelRow]
    safe_auto_model_id: int | None
    is_noise_candidate: bool


@dataclass(frozen=True)
class CompatibilityPreview:
    preview_token: str
    affected_count: int
    affected_product_count: int
    affected_competitor_count: int
    target_phone_model_ids: list[int]
    target_phone_models: list[CompatibilityPhoneModelRow]
    warnings: list[str]
    items: list[CompatibilityUnresolvedRow]


@dataclass(frozen=True)
class CompatibilityApplyResult:
    preview_token: str
    affected_count: int
    product_links_created: int
    competitor_links_created: int
    decisions_created: int


@dataclass(frozen=True)
class CompatibilityHistoryRow:
    action: str
    source: str | None
    raw_value: str
    normalized_key: str
    brand_id: int | None
    brand_display_name: str | None
    phone_model_ids: list[int]
    phone_model_labels: list[str]
    actor: str | None
    notes: str | None
    reason: str | None
    affected_count: int
    created_at: datetime


class CompatibilityMappingService:
    def __init__(self, db: Session):
        self.db = db
        self.brand_resolver = BrandResolver(db)

    def summary(self) -> CompatibilitySummary:
        self.brand_resolver.ensure_seed_brands()
        return CompatibilitySummary(
            brands=self.db.query(DeviceBrand).count(),
            brand_aliases=self.db.query(DeviceBrandAlias).count(),
            phone_models=self.db.query(PhoneModel).count(),
            product_links=self.db.query(ProductPhoneModel).count(),
            competitor_links=self.db.query(CompetitorItemCompatibility)
            .filter(CompetitorItemCompatibility.phone_model_id.isnot(None))
            .count(),
            unresolved_product_values=self._summary_unresolved_product_count(),
            unresolved_competitor_values=self._summary_unresolved_competitor_count(),
            blocked_values=self.db.query(CompatibilityMappingDecision)
            .filter(CompatibilityMappingDecision.action == DECISION_ACTION_BLOCK)
            .count(),
        )

    def list_brands(self, *, q: str | None = None, limit: int = 100) -> list[CompatibilityBrandRow]:
        brands = self.brand_resolver.list_brands(q=q, limit=limit)
        if not brands:
            return []
        brand_ids = [brand.id for brand in brands]
        model_counts = dict(
            self.db.execute(
                select(PhoneModel.brand_id, func.count(PhoneModel.id))
                .where(PhoneModel.brand_id.in_(brand_ids))
                .group_by(PhoneModel.brand_id)
            ).all()
        )
        unresolved_counts: dict[int, int] = {}
        for group in self.list_unresolved_groups(limit=10000, include_suggestions=False):
            if group.brand_id is not None:
                unresolved_counts[group.brand_id] = (
                    unresolved_counts.get(group.brand_id, 0) + group.affected_count
                )
        return [
            CompatibilityBrandRow(
                id=brand.id,
                code=brand.code,
                name=brand.name,
                display_name=brand.display_name,
                group_code=brand.group_code,
                is_active=brand.is_active,
                models_count=int(model_counts.get(brand.id, 0)),
                unresolved_count=unresolved_counts.get(brand.id, 0),
            )
            for brand in brands
        ]

    def _summary_unresolved_product_count(self) -> int:
        linked = and_(
            ProductPhoneModel.product_id == ProductCompatibility.product_id,
            ProductPhoneModel.source == ProductCompatibility.source,
            ProductPhoneModel.raw_value == ProductCompatibility.value,
        )
        decided = and_(
            CompatibilityMappingDecision.entity_type == ENTITY_PRODUCT,
            CompatibilityMappingDecision.entity_id == ProductCompatibility.product_id,
            CompatibilityMappingDecision.source == ProductCompatibility.source,
            CompatibilityMappingDecision.raw_value == ProductCompatibility.value,
            CompatibilityMappingDecision.action.in_([DECISION_ACTION_MAP, DECISION_ACTION_BLOCK]),
        )
        return int(
            self.db.query(func.count(ProductCompatibility.id))
            .join(Product, ProductCompatibility.product_id == Product.id)
            .outerjoin(ProductPhoneModel, linked)
            .outerjoin(CompatibilityMappingDecision, decided)
            .filter(
                ProductPhoneModel.id.is_(None),
                CompatibilityMappingDecision.id.is_(None),
            )
            .scalar()
            or 0
        )

    def _summary_unresolved_competitor_count(self) -> int:
        decided = and_(
            CompatibilityMappingDecision.entity_type == ENTITY_COMPETITOR_ITEM,
            CompatibilityMappingDecision.entity_id == CompetitorItemCompatibility.id,
            CompatibilityMappingDecision.source == CompetitorItemCompatibility.source,
            CompatibilityMappingDecision.action.in_([DECISION_ACTION_MAP, DECISION_ACTION_BLOCK]),
        )
        return int(
            self.db.query(func.count(CompetitorItemCompatibility.id))
            .outerjoin(CompatibilityMappingDecision, decided)
            .filter(
                CompetitorItemCompatibility.phone_model_id.is_(None),
                CompatibilityMappingDecision.id.is_(None),
            )
            .scalar()
            or 0
        )

    def create_brand(
        self,
        *,
        code: str,
        name: str | None,
        display_name: str | None,
        group_code: str | None,
    ) -> CompatibilityBrandRow:
        brand = self.brand_resolver.create_brand(
            code=code,
            name=name,
            display_name=display_name,
            group_code=group_code,
        )
        self.db.commit()
        return self.list_brands(q=brand.code, limit=1)[0]

    def create_brand_alias(
        self,
        *,
        brand_id: int,
        raw_value: str,
        source: str = "manual",
        actor: str | None = None,
    ) -> CompatibilityBrandRow:
        brand = self.db.get(DeviceBrand, brand_id)
        if brand is None:
            raise ValueError("brand not found")
        self.brand_resolver.upsert_alias(
            brand=brand,
            raw_value=raw_value,
            source=source,
            is_manual=True,
            confidence=1.0,
            decision_reason=f"manual:{actor}" if actor else "manual",
        )
        self.db.commit()
        return self.list_brands(q=brand.code, limit=1)[0]

    def list_brand_aliases(
        self,
        *,
        brand_id: int | None = None,
        q: str | None = None,
        include_inactive: bool = False,
        limit: int = 100,
    ) -> list[CompatibilityBrandAliasRow]:
        query = self.db.query(DeviceBrandAlias).join(DeviceBrand)
        if brand_id is not None:
            query = query.filter(DeviceBrandAlias.brand_id == brand_id)
        if not include_inactive:
            query = query.filter(DeviceBrandAlias.is_active.is_(True))
        search = normalize_compatibility_text(q)
        if search:
            pattern = f"%{search}%"
            query = query.filter(
                or_(
                    func.lower(DeviceBrandAlias.raw_value).like(pattern),
                    DeviceBrandAlias.normalized_key.like(pattern),
                    func.lower(DeviceBrandAlias.source).like(pattern),
                )
            )
        aliases = (
            query.options(selectinload(DeviceBrandAlias.brand))
            .order_by(DeviceBrandAlias.is_manual.desc(), DeviceBrandAlias.last_seen_at.desc())
            .limit(limit)
            .all()
        )
        return [
            CompatibilityBrandAliasRow(
                id=alias.id,
                brand_id=alias.brand_id,
                brand_display_name=alias.brand.display_name if alias.brand else None,
                source=alias.source,
                raw_value=alias.raw_value,
                normalized_key=alias.normalized_key,
                confidence=float(alias.confidence) if alias.confidence is not None else None,
                is_manual=alias.is_manual,
                is_active=alias.is_active,
                decision_reason=alias.decision_reason,
                first_seen_at=alias.first_seen_at,
                last_seen_at=alias.last_seen_at,
            )
            for alias in aliases
        ]

    def set_brand_alias_active(
        self, *, alias_id: int, is_active: bool
    ) -> CompatibilityBrandAliasRow:
        alias = self.db.get(DeviceBrandAlias, alias_id)
        if alias is None:
            raise ValueError("brand alias not found")
        alias.is_active = is_active
        alias.last_seen_at = datetime.utcnow()
        self.db.add(alias)
        self.db.commit()
        return self.list_brand_aliases(
            brand_id=alias.brand_id,
            q=alias.normalized_key,
            include_inactive=True,
            limit=1,
        )[0]

    def list_models(
        self,
        *,
        brand_id: int | None = None,
        q: str | None = None,
        limit: int = 100,
    ) -> list[CompatibilityPhoneModelRow]:
        query = (
            self.db.query(PhoneModel)
            .options(selectinload(PhoneModel.device_brand))
            .filter(PhoneModel.is_active.is_(True))
        )
        if brand_id is not None:
            brand = self.db.get(DeviceBrand, brand_id)
            if brand is None:
                return []
            group_codes = [brand.code]
            if brand.group_code:
                group_codes.extend(
                    code for code, _name, _display in self._brand_codes_for_group(brand.group_code)
                )
            query = query.filter(
                or_(PhoneModel.brand_id == brand_id, PhoneModel.brand.in_(group_codes))
            )
        search = normalize_compatibility_text(q)
        if search:
            pattern = f"%{search}%"
            query = query.filter(
                or_(
                    func.lower(PhoneModel.brand).like(pattern),
                    func.lower(PhoneModel.model_name).like(pattern),
                    func.lower(func.coalesce(PhoneModel.variant, "")).like(pattern),
                )
            )
        models = (
            query.order_by(PhoneModel.brand.asc(), PhoneModel.model_name.asc()).limit(limit).all()
        )
        if not models:
            return []
        model_ids = [model.id for model in models]
        alias_counts = dict(
            self.db.execute(
                select(PhoneModelAlias.phone_model_id, func.count(PhoneModelAlias.id))
                .where(PhoneModelAlias.phone_model_id.in_(model_ids))
                .group_by(PhoneModelAlias.phone_model_id)
            ).all()
        )
        product_counts = dict(
            self.db.execute(
                select(ProductPhoneModel.phone_model_id, func.count(ProductPhoneModel.id))
                .where(ProductPhoneModel.phone_model_id.in_(model_ids))
                .group_by(ProductPhoneModel.phone_model_id)
            ).all()
        )
        competitor_counts = dict(
            self.db.execute(
                select(
                    CompetitorItemCompatibility.phone_model_id,
                    func.count(CompetitorItemCompatibility.id),
                )
                .where(CompetitorItemCompatibility.phone_model_id.in_(model_ids))
                .group_by(CompetitorItemCompatibility.phone_model_id)
            ).all()
        )
        _parsed_brand, parsed_model, parsed_variant = parse_raw_device(q or "")
        rows = [
            self._model_row(
                model,
                aliases_count=int(alias_counts.get(model.id, 0)),
                product_links_count=int(product_counts.get(model.id, 0)),
                competitor_links_count=int(competitor_counts.get(model.id, 0)),
                suggestion_kind=(
                    self._suggestion_kind(
                        model,
                        parsed_model=parsed_model,
                        parsed_variant=parsed_variant,
                    )
                    if search
                    else None
                ),
            )
            for model in models
        ]
        if search:
            rows.sort(key=self._suggestion_sort_key)
        return rows

    def create_model(
        self,
        *,
        brand_id: int,
        model_name: str,
        variant: str | None = None,
    ) -> CompatibilityPhoneModelRow:
        brand = self.db.get(DeviceBrand, brand_id)
        if brand is None:
            raise ValueError("brand not found")
        model_name_norm = normalize_model_name(model_name)
        variant_norm = normalize_model_name(variant)
        if not model_name_norm:
            raise ValueError("model_name is required")
        existing = (
            self.db.query(PhoneModel)
            .filter(
                PhoneModel.brand == brand.code,
                PhoneModel.model_name == model_name_norm,
                (
                    PhoneModel.variant.is_(None)
                    if variant_norm is None
                    else PhoneModel.variant == variant_norm
                ),
            )
            .first()
        )
        if existing is None:
            existing = PhoneModel(
                brand=brand.code,
                brand_id=brand.id,
                model_name=model_name_norm,
                variant=variant_norm,
            )
            self.db.add(existing)
        else:
            existing.brand_id = brand.id
            self.db.add(existing)
        self.db.commit()
        return self._model_row(existing)

    def list_unresolved(
        self,
        *,
        entity_type: str | None = None,
        brand_id: int | None = None,
        source: str | None = None,
        q: str | None = None,
        limit: int = 100,
    ) -> list[CompatibilityUnresolvedRow]:
        rows = self._all_unresolved_rows(
            entity_type=entity_type,
            source=source,
            q=q,
            include_noise=True,
        )
        if brand_id is not None:
            rows = [row for row in rows if row.brand_id == brand_id]
        return rows[:limit]

    def list_unresolved_groups(
        self,
        *,
        entity_type: str | None = None,
        brand_id: int | None = None,
        without_brand: bool = False,
        source: str | None = None,
        q: str | None = None,
        limit: int = 100,
        include_suggestions: bool = True,
    ) -> list[CompatibilityUnresolvedGroup]:
        groups: list[CompatibilityUnresolvedGroup] = []
        if entity_type in {None, ENTITY_PRODUCT}:
            groups.extend(
                self._product_unresolved_groups(
                    source=source,
                    q=q,
                    brand_id=brand_id,
                    without_brand=without_brand,
                    limit=limit,
                    include_suggestions=include_suggestions,
                )
            )
        if entity_type in {None, ENTITY_COMPETITOR_ITEM}:
            groups.extend(
                self._competitor_unresolved_groups(
                    source=source,
                    q=q,
                    include_suggestions=include_suggestions,
                )
            )
        if brand_id is not None:
            groups = [group for group in groups if group.brand_id == brand_id]
        if without_brand:
            groups = [group for group in groups if group.brand_id is None]
        groups.sort(
            key=lambda group: (
                -group.affected_count,
                1 if group.brand_id is None else 0,
                group.raw_value,
            )
        )
        return groups[:limit]

    def preview(
        self,
        *,
        group_key: str | None = None,
        entity_type: str | None = None,
        source: str | None = None,
        raw_value: str | None = None,
        raw_brand: str | None = None,
        raw_model: str | None = None,
        raw_variant: str | None = None,
        brand_id: int | None = None,
        target_phone_model_ids: list[int] | None = None,
    ) -> CompatibilityPreview:
        payload = self._resolve_payload(
            group_key=group_key,
            entity_type=entity_type,
            source=source,
            raw_value=raw_value,
            raw_brand=raw_brand,
            raw_model=raw_model,
            raw_variant=raw_variant,
        )
        target_models = self._target_models(target_phone_model_ids or [])
        all_items = self._matching_unresolved_rows(limit=None, **payload)
        warnings = self._preview_warnings(
            brand_id=brand_id,
            target_models=target_models,
            items=all_items,
        )
        token = preview_token_for(
            entity_type=payload["entity_type"],
            source=payload["source"],
            raw_value=payload["raw_value"],
            raw_brand=payload["raw_brand"],
            raw_model=payload["raw_model"],
            raw_variant=payload["raw_variant"],
            brand_id=brand_id,
            target_phone_model_ids=target_phone_model_ids or [],
        )
        return CompatibilityPreview(
            preview_token=token,
            affected_count=len(all_items),
            affected_product_count=sum(
                1 for item in all_items if item.entity_type == ENTITY_PRODUCT
            ),
            affected_competitor_count=sum(
                1 for item in all_items if item.entity_type == ENTITY_COMPETITOR_ITEM
            ),
            target_phone_model_ids=[model.id for model in target_models],
            target_phone_models=[self._model_row(model) for model in target_models],
            warnings=warnings,
            items=all_items[:GROUP_EXAMPLE_LIMIT],
        )

    def apply(
        self,
        *,
        group_key: str | None = None,
        entity_type: str | None = None,
        source: str | None = None,
        raw_value: str | None = None,
        raw_brand: str | None = None,
        raw_model: str | None = None,
        raw_variant: str | None = None,
        brand_id: int | None = None,
        target_phone_model_ids: list[int] | None = None,
        preview_token: str | None = None,
        actor: str | None = None,
        notes: str | None = None,
    ) -> CompatibilityApplyResult:
        payload = self._resolve_payload(
            group_key=group_key,
            entity_type=entity_type,
            source=source,
            raw_value=raw_value,
            raw_brand=raw_brand,
            raw_model=raw_model,
            raw_variant=raw_variant,
        )
        preview = self.preview(
            group_key=group_key,
            entity_type=payload["entity_type"],
            source=payload["source"],
            raw_value=payload["raw_value"],
            raw_brand=payload["raw_brand"],
            raw_model=payload["raw_model"],
            raw_variant=payload["raw_variant"],
            brand_id=brand_id,
            target_phone_model_ids=target_phone_model_ids or [],
        )
        if preview_token and preview.preview_token != preview_token:
            raise ValueError("preview token does not match current payload")
        target_models = self._target_models(target_phone_model_ids or [])
        brand = self.db.get(DeviceBrand, brand_id) if brand_id else None
        if brand is None:
            raw_brand_missing = not payload["raw_brand"] and not any(
                item.brand_id for item in preview.items
            )
            if raw_brand_missing:
                raise ValueError(
                    "brand_id is required for unresolved values without recognized brand"
                )
        if brand and payload["raw_brand"]:
            self.brand_resolver.upsert_alias(
                brand=brand,
                raw_value=payload["raw_brand"] or "",
                source=payload["source"] or "manual",
                is_manual=True,
                confidence=1.0,
                decision_reason="manual_compatibility_mapping",
            )
        all_items = self._matching_unresolved_rows(limit=None, **payload)
        product_created = 0
        competitor_created = 0
        decisions_created = 0
        for item in all_items:
            if item.entity_type == ENTITY_PRODUCT:
                product_created += self._apply_product_item(item, target_models)
            elif item.entity_type == ENTITY_COMPETITOR_ITEM:
                competitor_created += self._apply_competitor_item(item, target_models, brand)
            decisions_created += self._record_decision(
                item=item,
                action=DECISION_ACTION_MAP,
                brand_id=brand_id,
                target_phone_model_ids=[model.id for model in target_models],
                actor=actor,
                notes=notes,
            )
            for model in target_models:
                self._upsert_phone_model_alias(
                    model=model,
                    source=payload["source"] or "manual",
                    raw_value=item.raw_value,
                    raw_brand=item.raw_brand or payload["raw_brand"],
                    raw_model=item.raw_model or payload["raw_model"] or item.raw_value,
                    raw_variant=item.raw_variant or payload["raw_variant"],
                )
        self.db.commit()
        return CompatibilityApplyResult(
            preview_token=preview.preview_token,
            affected_count=preview.affected_count,
            product_links_created=product_created,
            competitor_links_created=competitor_created,
            decisions_created=decisions_created,
        )

    def block(
        self,
        *,
        group_key: str | None = None,
        entity_type: str | None = None,
        source: str | None = None,
        raw_value: str | None = None,
        raw_brand: str | None = None,
        raw_model: str | None = None,
        raw_variant: str | None = None,
        reason: str | None = None,
        actor: str | None = None,
        notes: str | None = None,
    ) -> CompatibilityApplyResult:
        if reason and reason not in VALID_BLOCK_REASONS:
            raise ValueError("invalid block reason")
        payload = self._resolve_payload(
            group_key=group_key,
            entity_type=entity_type,
            source=source,
            raw_value=raw_value,
            raw_brand=raw_brand,
            raw_model=raw_model,
            raw_variant=raw_variant,
        )
        items = self._matching_unresolved_rows(
            limit=None,
            **payload,
        )
        decision_notes = self._format_decision_notes(reason=reason, notes=notes)
        decisions_created = 0
        for item in items:
            decisions_created += self._record_decision(
                item=item,
                action=DECISION_ACTION_BLOCK,
                brand_id=item.brand_id,
                target_phone_model_ids=[],
                actor=actor,
                notes=decision_notes,
            )
        token = preview_token_for(
            entity_type=payload["entity_type"],
            source=payload["source"],
            raw_value=payload["raw_value"],
            raw_brand=payload["raw_brand"],
            raw_model=payload["raw_model"],
            raw_variant=payload["raw_variant"],
            brand_id=None,
            target_phone_model_ids=[],
        )
        self.db.commit()
        return CompatibilityApplyResult(
            preview_token=token,
            affected_count=len(items),
            product_links_created=0,
            competitor_links_created=0,
            decisions_created=decisions_created,
        )

    def list_history(self, *, limit: int = 50) -> list[CompatibilityHistoryRow]:
        decisions = (
            self.db.query(CompatibilityMappingDecision)
            .options(selectinload(CompatibilityMappingDecision.brand))
            .order_by(
                CompatibilityMappingDecision.created_at.desc(),
                CompatibilityMappingDecision.id.desc(),
            )
            .limit(HISTORY_LOOKBACK_LIMIT)
            .all()
        )
        grouped: dict[tuple[object, ...], dict[str, object]] = {}
        for decision in decisions:
            model_ids = [int(item) for item in (decision.phone_model_ids_json or [])]
            key = (
                decision.action,
                decision.source,
                decision.raw_value,
                decision.normalized_key,
                decision.brand_id,
                tuple(model_ids),
                decision.actor,
                decision.notes,
                decision.created_at.replace(microsecond=0),
            )
            current = grouped.setdefault(
                key,
                {
                    "decision": decision,
                    "phone_model_ids": model_ids,
                    "affected_count": 0,
                },
            )
            current["affected_count"] = int(current["affected_count"]) + 1

        rows: list[CompatibilityHistoryRow] = []
        for data in grouped.values():
            decision = data["decision"]
            if not isinstance(decision, CompatibilityMappingDecision):
                continue
            model_ids = data["phone_model_ids"]
            phone_model_labels = (
                self._phone_model_labels(model_ids) if isinstance(model_ids, list) else []
            )
            rows.append(
                CompatibilityHistoryRow(
                    action=decision.action,
                    source=decision.source,
                    raw_value=decision.raw_value,
                    normalized_key=decision.normalized_key,
                    brand_id=decision.brand_id,
                    brand_display_name=decision.brand.display_name if decision.brand else None,
                    phone_model_ids=model_ids if isinstance(model_ids, list) else [],
                    phone_model_labels=phone_model_labels,
                    actor=decision.actor,
                    notes=decision.notes,
                    reason=self._reason_from_notes(decision.notes),
                    affected_count=int(data["affected_count"]),
                    created_at=decision.created_at,
                )
            )
        return sorted(rows, key=lambda row: row.created_at, reverse=True)[:limit]

    def _resolve_payload(
        self,
        *,
        group_key: str | None,
        entity_type: str | None,
        source: str | None = None,
        raw_value: str | None = None,
        raw_brand: str | None = None,
        raw_model: str | None = None,
        raw_variant: str | None = None,
    ) -> dict[str, str | None]:
        if group_key:
            return group_payload_from_key(group_key)
        if entity_type not in {ENTITY_PRODUCT, ENTITY_COMPETITOR_ITEM}:
            raise ValueError("entity_type is required")
        if not raw_value:
            raise ValueError("raw_value is required")
        return {
            "entity_type": entity_type,
            "source": source,
            "raw_value": raw_value,
            "raw_brand": raw_brand,
            "raw_model": raw_model,
            "raw_variant": raw_variant,
            "normalized_key": compatibility_identity_key(
                entity_type=entity_type,
                source=source,
                raw_value=raw_value,
                raw_brand=raw_brand,
                raw_model=raw_model,
                raw_variant=raw_variant,
            ),
        }

    @staticmethod
    def _format_decision_notes(*, reason: str | None, notes: str | None) -> str | None:
        if not reason:
            return notes
        suffix = f": {notes}" if notes else ""
        return f"reason={reason}{suffix}"[:255]

    @staticmethod
    def _reason_from_notes(notes: str | None) -> str | None:
        if not notes or not notes.startswith("reason="):
            return None
        return notes.split(":", 1)[0].removeprefix("reason=") or None

    def _product_unresolved_groups(
        self,
        *,
        source: str | None,
        q: str | None,
        brand_id: int | None,
        without_brand: bool,
        limit: int,
        include_suggestions: bool,
    ) -> list[CompatibilityUnresolvedGroup]:
        linked = self._product_linked_condition()
        decided = self._product_decided_condition()
        query = (
            self.db.query(
                ProductCompatibility.source.label("source"),
                ProductCompatibility.value.label("raw_value"),
                func.count(ProductCompatibility.id).label("affected_count"),
                func.max(ProductCompatibility.id).label("latest_id"),
            )
            .join(Product, ProductCompatibility.product_id == Product.id)
            .outerjoin(ProductPhoneModel, linked)
            .outerjoin(CompatibilityMappingDecision, decided)
            .filter(
                ProductPhoneModel.id.is_(None),
                CompatibilityMappingDecision.id.is_(None),
            )
            .group_by(ProductCompatibility.source, ProductCompatibility.value)
        )
        if source:
            query = query.filter(ProductCompatibility.source == source)
        if q:
            pattern = f"%{q}%"
            query = query.filter(
                or_(
                    ProductCompatibility.value.ilike(pattern),
                    Product.name.ilike(pattern),
                    Product.article.ilike(pattern),
                )
            )
        rows = query.order_by(
            func.count(ProductCompatibility.id).desc(), func.max(ProductCompatibility.id).desc()
        ).all()
        groups: list[CompatibilityUnresolvedGroup] = []
        for row in rows:
            brand = self._lookup_brand(row.raw_value, None)
            if brand_id is not None and (brand is None or brand.id != brand_id):
                continue
            if without_brand and brand is not None:
                continue
            normalized_key = compatibility_identity_key(
                entity_type=ENTITY_PRODUCT,
                source=row.source,
                raw_value=row.raw_value,
            )
            first = CompatibilityUnresolvedRow(
                entity_type=ENTITY_PRODUCT,
                entity_id=0,
                source=row.source,
                raw_value=row.raw_value,
                normalized_key=normalized_key,
                brand_id=brand.id if brand else None,
                brand_display_name=brand.display_name if brand else None,
            )
            examples = (
                self._product_group_examples(
                    source=row.source,
                    raw_value=row.raw_value,
                    limit=GROUP_EXAMPLE_LIMIT,
                )
                if include_suggestions
                else []
            )
            if examples:
                first = examples[0]
            if include_suggestions and not examples:
                continue
            group_key = group_key_for(
                entity_type=ENTITY_PRODUCT,
                source=row.source,
                raw_value=row.raw_value,
            )
            suggested = self._suggest_phone_models(first, limit=5) if include_suggestions else []
            groups.append(
                CompatibilityUnresolvedGroup(
                    group_key=group_key,
                    entity_type=ENTITY_PRODUCT,
                    source=row.source,
                    raw_value=row.raw_value,
                    raw_brand=None,
                    raw_model=None,
                    raw_variant=None,
                    normalized_key=first.normalized_key,
                    brand_id=first.brand_id,
                    brand_display_name=first.brand_display_name,
                    affected_count=int(row.affected_count),
                    product_count=int(row.affected_count),
                    competitor_count=0,
                    examples=examples,
                    suggested_phone_models=suggested,
                    safe_auto_model_id=self._safe_auto_model_id(suggested),
                    is_noise_candidate=is_noise_compatibility_value(row.raw_value),
                )
            )
            if len(groups) >= limit:
                break
        return groups

    def _competitor_unresolved_groups(
        self,
        *,
        source: str | None,
        q: str | None,
        include_suggestions: bool,
    ) -> list[CompatibilityUnresolvedGroup]:
        grouped: dict[str, dict[str, object]] = {}
        for row in self._competitor_unresolved(source=source, brand_id=None, q=q, limit=None):
            group_key = group_key_for(
                entity_type=row.entity_type,
                source=row.source,
                raw_value=row.raw_value,
                raw_brand=row.raw_brand,
                raw_model=row.raw_model,
                raw_variant=row.raw_variant,
            )
            current = grouped.setdefault(
                group_key,
                {
                    "row": row,
                    "affected_count": 0,
                    "examples": [],
                },
            )
            current["affected_count"] = int(current["affected_count"]) + 1
            examples = current["examples"]
            if isinstance(examples, list) and len(examples) < GROUP_EXAMPLE_LIMIT:
                examples.append(row)
        groups: list[CompatibilityUnresolvedGroup] = []
        for group_key, data in grouped.items():
            row = data["row"]
            if not isinstance(row, CompatibilityUnresolvedRow):
                continue
            suggested = self._suggest_phone_models(row, limit=5) if include_suggestions else []
            groups.append(
                CompatibilityUnresolvedGroup(
                    group_key=group_key,
                    entity_type=row.entity_type,
                    source=row.source,
                    raw_value=row.raw_value,
                    raw_brand=row.raw_brand,
                    raw_model=row.raw_model,
                    raw_variant=row.raw_variant,
                    normalized_key=row.normalized_key,
                    brand_id=row.brand_id,
                    brand_display_name=row.brand_display_name,
                    affected_count=int(data["affected_count"]),
                    product_count=0,
                    competitor_count=int(data["affected_count"]),
                    examples=list(data["examples"])[:GROUP_EXAMPLE_LIMIT],
                    suggested_phone_models=suggested,
                    safe_auto_model_id=self._safe_auto_model_id(suggested),
                    is_noise_candidate=is_noise_compatibility_value(row.raw_value),
                )
            )
        return groups

    def _product_group_examples(
        self,
        *,
        source: str | None,
        raw_value: str,
        limit: int | None,
    ) -> list[CompatibilityUnresolvedRow]:
        linked = self._product_linked_condition()
        decided = self._product_decided_condition()
        query = (
            self.db.query(ProductCompatibility, Product)
            .join(Product, ProductCompatibility.product_id == Product.id)
            .outerjoin(ProductPhoneModel, linked)
            .outerjoin(CompatibilityMappingDecision, decided)
            .filter(
                ProductCompatibility.value == raw_value,
                ProductPhoneModel.id.is_(None),
                CompatibilityMappingDecision.id.is_(None),
            )
        )
        if source is None:
            query = query.filter(ProductCompatibility.source.is_(None))
        else:
            query = query.filter(ProductCompatibility.source == source)
        query = query.order_by(ProductCompatibility.id.desc())
        if limit is not None:
            query = query.limit(limit)
        return [self._product_row(compat, product) for compat, product in query.all()]

    def _product_linked_condition(self):
        return and_(
            ProductPhoneModel.product_id == ProductCompatibility.product_id,
            ProductPhoneModel.source == ProductCompatibility.source,
            ProductPhoneModel.raw_value == ProductCompatibility.value,
        )

    def _product_decided_condition(self):
        return and_(
            CompatibilityMappingDecision.entity_type == ENTITY_PRODUCT,
            CompatibilityMappingDecision.entity_id == ProductCompatibility.product_id,
            CompatibilityMappingDecision.source == ProductCompatibility.source,
            CompatibilityMappingDecision.raw_value == ProductCompatibility.value,
            CompatibilityMappingDecision.action.in_([DECISION_ACTION_MAP, DECISION_ACTION_BLOCK]),
        )

    def _all_unresolved_rows(
        self,
        *,
        entity_type: str | None,
        source: str | None,
        q: str | None,
        include_noise: bool,
    ) -> list[CompatibilityUnresolvedRow]:
        rows: list[CompatibilityUnresolvedRow] = []
        if entity_type in {None, ENTITY_PRODUCT}:
            rows.extend(
                self._product_unresolved(
                    source=source,
                    brand_id=None,
                    q=q,
                    limit=None,
                    include_noise=include_noise,
                )
            )
        if entity_type in {None, ENTITY_COMPETITOR_ITEM}:
            rows.extend(
                self._competitor_unresolved(
                    source=source,
                    brand_id=None,
                    q=q,
                    limit=None,
                )
            )
        return rows

    def _product_unresolved(
        self,
        *,
        source: str | None,
        brand_id: int | None,
        q: str | None,
        limit: int | None,
        include_noise: bool = False,
    ) -> list[CompatibilityUnresolvedRow]:
        query = (
            self.db.query(ProductCompatibility, Product)
            .join(Product, ProductCompatibility.product_id == Product.id)
            .order_by(ProductCompatibility.id.desc())
        )
        if source:
            query = query.filter(ProductCompatibility.source == source)
        if limit is not None:
            query = query.limit(limit * 3)
        rows: list[CompatibilityUnresolvedRow] = []
        for compat, product in query.all():
            screen = screen_product_phone_compatibility(product, compat.value, source=compat.source)
            is_noise = is_noise_compatibility_value(compat.value)
            if not screen.eligible_for_phone_canonicalization and not (include_noise and is_noise):
                continue
            normalized_key = compatibility_identity_key(
                entity_type=ENTITY_PRODUCT,
                source=compat.source,
                raw_value=compat.value,
            )
            if self._decision_exists(
                ENTITY_PRODUCT, product.id, compat.source, compat.value, normalized_key
            ):
                continue
            current_ids = [
                link.phone_model_id
                for link in getattr(product, "phone_model_links", []) or []
                if link.source == compat.source and link.raw_value == compat.value
            ]
            if current_ids:
                continue
            row = self._product_row(compat, product)
            if brand_id is not None and row.brand_id != brand_id:
                continue
            if q and normalize_compatibility_text(q) not in normalize_compatibility_text(
                f"{compat.value} {product.name} {product.article}"
            ):
                continue
            rows.append(row)
            if limit is not None and len(rows) >= limit:
                break
        return rows

    def _product_row(
        self,
        compat: ProductCompatibility,
        product: Product,
    ) -> CompatibilityUnresolvedRow:
        normalized_key = compatibility_identity_key(
            entity_type=ENTITY_PRODUCT,
            source=compat.source,
            raw_value=compat.value,
        )
        brand = self._lookup_brand(compat.value, None)
        return CompatibilityUnresolvedRow(
            entity_type=ENTITY_PRODUCT,
            entity_id=product.id,
            source=compat.source,
            raw_value=compat.value,
            normalized_key=normalized_key,
            brand_id=brand.id if brand else None,
            brand_display_name=brand.display_name if brand else None,
            sample_name=f"{product.article} {product.name}".strip(),
            current_phone_model_ids=[],
        )

    def _competitor_unresolved(
        self,
        *,
        source: str | None,
        brand_id: int | None,
        q: str | None,
        limit: int | None,
    ) -> list[CompatibilityUnresolvedRow]:
        query = (
            self.db.query(CompetitorItemCompatibility, CompetitorItem)
            .join(
                CompetitorItem, CompetitorItemCompatibility.competitor_item_id == CompetitorItem.id
            )
            .filter(CompetitorItemCompatibility.phone_model_id.is_(None))
            .order_by(CompetitorItemCompatibility.id.desc())
        )
        if source:
            query = query.filter(CompetitorItemCompatibility.source == source)
        if limit is not None:
            query = query.limit(limit * 3)
        rows: list[CompatibilityUnresolvedRow] = []
        for compat, item in query.all():
            raw_value = self._competitor_raw_value(compat)
            normalized_key = compatibility_identity_key(
                entity_type=ENTITY_COMPETITOR_ITEM,
                source=compat.source,
                raw_value=raw_value,
                raw_brand=compat.device_brand,
                raw_model=compat.device_model,
                raw_variant=compat.device_variant,
            )
            if self._decision_exists(
                ENTITY_COMPETITOR_ITEM,
                compat.id,
                compat.source,
                raw_value,
                normalized_key,
            ):
                continue
            brand = compat.device_brand or None
            device_brand = compat.device_brand if compat.device_brand_id is None else None
            resolved_brand = compat.device_brand
            brand_model = (
                self.db.get(DeviceBrand, compat.device_brand_id) if compat.device_brand_id else None
            )
            if brand_model is None:
                brand_model = self._lookup_brand(device_brand or brand, compat.device_model)
            if brand_id is not None and (brand_model is None or brand_model.id != brand_id):
                continue
            if q and normalize_compatibility_text(q) not in normalize_compatibility_text(
                f"{raw_value} {item.name} {item.external_id}"
            ):
                continue
            rows.append(
                CompatibilityUnresolvedRow(
                    entity_type=ENTITY_COMPETITOR_ITEM,
                    entity_id=compat.id,
                    source=compat.source,
                    raw_value=raw_value,
                    raw_brand=resolved_brand,
                    raw_model=compat.device_model,
                    raw_variant=compat.device_variant,
                    normalized_key=normalized_key,
                    brand_id=brand_model.id if brand_model else None,
                    brand_display_name=brand_model.display_name if brand_model else None,
                    sample_name=f"{item.competitor} {item.external_id} {item.name or ''}".strip(),
                    current_phone_model_ids=[],
                )
            )
            if limit is not None and len(rows) >= limit:
                break
        return rows

    def _matching_unresolved_rows(
        self,
        *,
        entity_type: str,
        source: str | None,
        raw_value: str,
        raw_brand: str | None,
        raw_model: str | None,
        raw_variant: str | None,
        normalized_key: str | None = None,
        limit: int | None = None,
    ) -> list[CompatibilityUnresolvedRow]:
        target_key = normalized_key or compatibility_identity_key(
            entity_type=entity_type,
            source=source,
            raw_value=raw_value,
            raw_brand=raw_brand,
            raw_model=raw_model,
            raw_variant=raw_variant,
        )
        if entity_type == ENTITY_PRODUCT:
            rows = self._product_group_examples(
                source=source,
                raw_value=raw_value,
                limit=limit,
            )
            return [row for row in rows if row.normalized_key == target_key]
        rows = self._all_unresolved_rows(
            entity_type=entity_type,
            source=source,
            q=None,
            include_noise=True,
        )
        matching = [row for row in rows if row.normalized_key == target_key]
        return matching if limit is None else matching[:limit]

    def _decision_exists(
        self,
        entity_type: str,
        entity_id: int,
        source: str | None,
        raw_value: str,
        normalized_key: str,
    ) -> bool:
        return (
            self.db.execute(
                select(CompatibilityMappingDecision.id).where(
                    CompatibilityMappingDecision.entity_type == entity_type,
                    CompatibilityMappingDecision.entity_id == entity_id,
                    CompatibilityMappingDecision.normalized_key == normalized_key,
                    CompatibilityMappingDecision.raw_value == raw_value,
                    (
                        CompatibilityMappingDecision.source.is_(None)
                        if source is None
                        else CompatibilityMappingDecision.source == source
                    ),
                    CompatibilityMappingDecision.action.in_(
                        [DECISION_ACTION_MAP, DECISION_ACTION_BLOCK]
                    ),
                )
            ).first()
            is not None
        )

    def _record_decision(
        self,
        *,
        item: CompatibilityUnresolvedRow,
        action: str,
        brand_id: int | None,
        target_phone_model_ids: list[int],
        actor: str | None,
        notes: str | None,
    ) -> int:
        if self._decision_exists(
            item.entity_type,
            item.entity_id,
            item.source,
            item.raw_value,
            item.normalized_key,
        ):
            return 0
        self.db.add(
            CompatibilityMappingDecision(
                entity_type=item.entity_type,
                entity_id=item.entity_id,
                source=item.source,
                raw_value=item.raw_value,
                normalized_key=item.normalized_key,
                action=action,
                brand_id=brand_id,
                phone_model_ids_json=target_phone_model_ids,
                actor=actor,
                notes=notes,
            )
        )
        return 1

    def _apply_product_item(
        self,
        item: CompatibilityUnresolvedRow,
        target_models: list[PhoneModel],
    ) -> int:
        product = self.db.get(Product, item.entity_id)
        if product is None:
            return 0
        created = 0
        for model in target_models:
            existing = (
                self.db.query(ProductPhoneModel)
                .filter(
                    ProductPhoneModel.product_id == product.id,
                    ProductPhoneModel.phone_model_id == model.id,
                    ProductPhoneModel.source == (item.source or "manual"),
                )
                .first()
            )
            if existing:
                existing.raw_value = item.raw_value
                existing.is_manual = True
                existing.confidence = 1.0
                existing.updated_at = datetime.utcnow()
                self.db.add(existing)
                continue
            self.db.add(
                ProductPhoneModel(
                    product_id=product.id,
                    phone_model_id=model.id,
                    source=item.source or "manual",
                    raw_value=item.raw_value,
                    confidence=1.0,
                    is_manual=True,
                )
            )
            created += 1
        return created

    def _apply_competitor_item(
        self,
        item: CompatibilityUnresolvedRow,
        target_models: list[PhoneModel],
        brand: DeviceBrand | None,
    ) -> int:
        source_compat = self.db.get(CompetitorItemCompatibility, item.entity_id)
        if source_compat is None:
            return 0
        created = 0
        for model in target_models:
            device_brand = model.device_brand or brand
            existing = (
                self.db.query(CompetitorItemCompatibility)
                .filter(
                    CompetitorItemCompatibility.competitor_item_id
                    == source_compat.competitor_item_id,
                    CompetitorItemCompatibility.device_brand == model.brand,
                    CompetitorItemCompatibility.device_model == model.model_name,
                    (
                        CompetitorItemCompatibility.device_variant.is_(None)
                        if model.variant is None
                        else CompetitorItemCompatibility.device_variant == model.variant
                    ),
                )
                .first()
            )
            if existing:
                existing.phone_model_id = model.id
                existing.device_brand_id = device_brand.id if device_brand else model.brand_id
                existing.source = existing.source or item.source or "manual"
                self.db.add(existing)
                continue
            self.db.add(
                CompetitorItemCompatibility(
                    competitor_item_id=source_compat.competitor_item_id,
                    phone_model_id=model.id,
                    device_brand_id=device_brand.id if device_brand else model.brand_id,
                    device_brand=model.brand,
                    device_model=model.model_name,
                    device_variant=model.variant,
                    source=item.source or "manual",
                    notes=f"manual mapping from {item.raw_value}"[:255],
                )
            )
            created += 1
        return created

    def _upsert_phone_model_alias(
        self,
        *,
        model: PhoneModel,
        source: str,
        raw_value: str,
        raw_brand: str | None,
        raw_model: str | None,
        raw_variant: str | None,
    ) -> None:
        brand_norm = normalize_brand(raw_brand or model.brand)
        model_norm = normalize_model_name(raw_model or model.model_name)
        variant_norm = normalize_model_name(raw_variant)
        normalized_key = build_normalized_key(brand_norm, model_norm, variant_norm)
        if not normalized_key:
            normalized_key = normalize_compatibility_text(raw_value)
        existing = (
            self.db.query(PhoneModelAlias)
            .filter(
                PhoneModelAlias.phone_model_id == model.id,
                PhoneModelAlias.source == source,
                PhoneModelAlias.normalized_key == normalized_key,
            )
            .first()
        )
        if existing:
            existing.raw_value = raw_value
            existing.raw_brand = raw_brand
            existing.raw_model = raw_model
            existing.raw_variant = raw_variant
            existing.is_manual = True
            existing.confidence = 1.0
            existing.decision_reason = "manual_compatibility_mapping"
            existing.last_seen_at = datetime.utcnow()
            self.db.add(existing)
            return
        self.db.add(
            PhoneModelAlias(
                phone_model_id=model.id,
                source=source,
                raw_value=raw_value,
                raw_brand=raw_brand,
                raw_model=raw_model,
                raw_variant=raw_variant,
                normalized_key=normalized_key,
                confidence=1.0,
                is_manual=True,
                decision_reason="manual_compatibility_mapping",
                device_type="phone",
            )
        )

    def _target_models(self, target_phone_model_ids: list[int]) -> list[PhoneModel]:
        unique_ids = list(dict.fromkeys(target_phone_model_ids))
        if not unique_ids:
            raise ValueError("target_phone_model_ids is required")
        models = (
            self.db.query(PhoneModel)
            .options(selectinload(PhoneModel.device_brand))
            .filter(PhoneModel.id.in_(unique_ids))
            .all()
        )
        if len(models) != len(unique_ids):
            raise ValueError("target phone model not found")
        return sorted(models, key=lambda model: unique_ids.index(model.id))

    def _suggest_phone_models(
        self,
        row: CompatibilityUnresolvedRow,
        *,
        limit: int,
    ) -> list[CompatibilityPhoneModelRow]:
        parsed_brand, parsed_model, parsed_variant = parse_raw_device(
            " ".join(value for value in (row.raw_brand, row.raw_model, row.raw_variant) if value)
            or row.raw_value
        )
        brand_id = row.brand_id
        if brand_id is None and parsed_brand:
            brand = self._lookup_brand(parsed_brand, parsed_model)
            brand_id = brand.id if brand else None
        query = (
            self.db.query(PhoneModel)
            .options(selectinload(PhoneModel.device_brand))
            .filter(PhoneModel.is_active.is_(True))
        )
        if brand_id is not None:
            query = query.filter(PhoneModel.brand_id == brand_id)
        candidates: list[PhoneModel] = []
        if parsed_model:
            exact_query = query.filter(PhoneModel.model_name == parsed_model)
            if parsed_variant:
                exact_query = exact_query.filter(PhoneModel.variant == parsed_variant)
            candidates.extend(exact_query.order_by(PhoneModel.variant.asc()).limit(limit * 2).all())
        if len(candidates) < limit:
            search = normalize_compatibility_text(parsed_model or row.raw_model or row.raw_value)
            if search:
                pattern = f"%{search}%"
                existing_ids = {model.id for model in candidates}
                fuzzy = (
                    query.filter(
                        or_(
                            func.lower(PhoneModel.model_name).like(pattern),
                            func.lower(func.coalesce(PhoneModel.variant, "")).like(pattern),
                        )
                    )
                    .order_by(PhoneModel.model_name.asc(), PhoneModel.variant.asc())
                    .limit(limit * 2)
                    .all()
                )
                candidates.extend(model for model in fuzzy if model.id not in existing_ids)
        rows = [
            self._model_row(
                model,
                suggestion_kind=self._suggestion_kind(
                    model,
                    parsed_model=parsed_model,
                    parsed_variant=parsed_variant,
                ),
            )
            for model in candidates
        ]
        rows.sort(key=self._suggestion_sort_key)
        return rows[:limit]

    @staticmethod
    def _safe_auto_model_id(rows: list[CompatibilityPhoneModelRow]) -> int | None:
        safe_rows = [row for row in rows if row.suggestion_kind in {"exact_base", "exact_variant"}]
        if len(safe_rows) != 1:
            return None
        return safe_rows[0].id

    @staticmethod
    def _suggestion_sort_key(row: CompatibilityPhoneModelRow) -> tuple[int, str, str, int]:
        order = {
            "exact_base": 0,
            "exact_variant": 1,
            "hardware_variant": 2,
            "related_family": 3,
        }
        return (
            order.get(row.suggestion_kind or "related_family", 99),
            row.model_name,
            row.variant or "",
            row.id,
        )

    @staticmethod
    def _suggestion_kind(
        model: PhoneModel,
        *,
        parsed_model: str | None,
        parsed_variant: str | None,
    ) -> str:
        model_name = normalize_model_name(model.model_name) or ""
        model_variant = normalize_model_name(model.variant)
        parsed_name = normalize_model_name(parsed_model) or ""
        parsed_variant_norm = normalize_model_name(parsed_variant)
        model_label = normalize_compatibility_text(
            " ".join(value for value in (model_name, model_variant) if value)
        )
        parsed_label = normalize_compatibility_text(
            " ".join(value for value in (parsed_name, parsed_variant_norm) if value)
        )

        if (
            parsed_name
            and model_name == parsed_name
            and not parsed_variant_norm
            and not model_variant
            and not CompatibilityMappingService._has_hardware_variant_token(model_label)
        ):
            return "exact_base"
        if (
            parsed_name
            and model_name == parsed_name
            and parsed_variant_norm
            and model_variant == parsed_variant_norm
            and not CompatibilityMappingService._has_hardware_variant_token(model_variant)
        ):
            return "exact_variant"
        if CompatibilityMappingService._has_hardware_variant_token(model_label):
            return "hardware_variant"
        if parsed_label and CompatibilityMappingService._has_hardware_variant_token(parsed_label):
            return "hardware_variant"
        return "related_family"

    @staticmethod
    def _has_hardware_variant_token(value: str | None) -> bool:
        normalized = normalize_compatibility_text(value)
        if not normalized:
            return False
        return bool(
            re.search(r"\ba\d{4}\b", normalized)
            or re.search(r"\bor(?:ig)?\s*\d{2,3}\b", normalized)
            or re.search(r"\b(?:hard|soft)\s+oled\b", normalized)
        )

    def _phone_model_labels(self, model_ids: list[int]) -> list[str]:
        if not model_ids:
            return []
        models = (
            self.db.query(PhoneModel)
            .options(selectinload(PhoneModel.device_brand))
            .filter(PhoneModel.id.in_(model_ids))
            .all()
        )
        by_id = {model.id: model for model in models}
        return [self._model_label(by_id[model_id]) for model_id in model_ids if model_id in by_id]

    @staticmethod
    def _model_label(model: PhoneModel) -> str:
        brand = model.device_brand.display_name if model.device_brand else model.brand
        return " ".join(value for value in (brand, model.model_name, model.variant) if value)

    def _preview_warnings(
        self,
        *,
        brand_id: int | None,
        target_models: list[PhoneModel],
        items: list[CompatibilityUnresolvedRow],
    ) -> list[str]:
        warnings: list[str] = []
        if brand_id is None and any(item.brand_id is None for item in items):
            warnings.append("brand is required for values without recognized brand")
        target_brand_ids = {model.brand_id for model in target_models if model.brand_id is not None}
        if brand_id and target_brand_ids and brand_id not in target_brand_ids:
            warnings.append("selected brand differs from one or more target models")
        groups = {
            (model.device_brand.group_code if model.device_brand else model.brand)
            for model in target_models
            if model.device_brand or model.brand
        }
        if len(groups) > 1:
            warnings.append("target models belong to different brand groups")
        return warnings

    def _lookup_brand(self, raw_brand: str | None, model_name: str | None) -> DeviceBrand | None:
        code_value = raw_brand
        if raw_brand and normalize_brand_key(raw_brand) == "xiaomi":
            model_text = normalize_brand_key(model_name) or ""
            if model_text.startswith("redmi"):
                code_value = "redmi"
            elif model_text.startswith("poco"):
                code_value = "poco"
        resolved_key = normalize_brand_key(code_value)
        if not resolved_key:
            return None
        alias = (
            self.db.query(DeviceBrandAlias)
            .join(DeviceBrand)
            .filter(
                DeviceBrandAlias.normalized_key == resolved_key,
                DeviceBrandAlias.is_active.is_(True),
                DeviceBrand.is_active.is_(True),
            )
            .order_by(DeviceBrandAlias.is_manual.desc(), DeviceBrandAlias.id.asc())
            .first()
        )
        if alias:
            return alias.brand
        code = brand_code_from_text(code_value)
        if not code:
            return None
        return (
            self.db.query(DeviceBrand)
            .filter(DeviceBrand.code == code, DeviceBrand.is_active.is_(True))
            .first()
        )

    def _model_row(
        self,
        model: PhoneModel,
        *,
        aliases_count: int = 0,
        product_links_count: int = 0,
        competitor_links_count: int = 0,
        suggestion_kind: str | None = None,
    ) -> CompatibilityPhoneModelRow:
        brand = model.device_brand
        return CompatibilityPhoneModelRow(
            id=model.id,
            brand_id=model.brand_id,
            brand_code=brand.code if brand else None,
            brand_display_name=brand.display_name if brand else None,
            brand=model.brand,
            model_name=model.model_name,
            variant=model.variant,
            is_active=model.is_active,
            aliases_count=aliases_count,
            product_links_count=product_links_count,
            competitor_links_count=competitor_links_count,
            suggestion_kind=suggestion_kind,
        )

    def _brand_codes_for_group(self, group_code: str) -> list[tuple[str, str, str]]:
        brands = self.db.query(DeviceBrand).filter(DeviceBrand.group_code == group_code).all()
        return [(brand.code, brand.name, brand.display_name) for brand in brands]

    @staticmethod
    def _competitor_raw_value(compat: CompetitorItemCompatibility) -> str:
        return " ".join(
            value
            for value in (compat.device_brand, compat.device_model, compat.device_variant)
            if value
        ).strip()
