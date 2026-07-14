from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import DeviceBrand, DeviceBrandAlias

DEVICE_BRAND_SEEDS = {
    "apple": ("apple", "Apple", "apple"),
    "samsung": ("samsung", "Samsung", "samsung"),
    "xiaomi": ("xiaomi", "Xiaomi", "xiaomi"),
    "redmi": ("redmi", "Redmi", "xiaomi"),
    "poco": ("poco", "POCO", "xiaomi"),
    "huawei": ("huawei", "Huawei", "huawei_honor"),
    "honor": ("honor", "Honor", "huawei_honor"),
    "realme": ("realme", "Realme", "realme"),
    "oppo": ("oppo", "OPPO", "oppo"),
    "vivo": ("vivo", "Vivo", "vivo"),
    "oneplus": ("oneplus", "OnePlus", "oneplus"),
    "tecno": ("tecno", "Tecno", "tecno"),
    "infinix": ("infinix", "Infinix", "infinix"),
    "itel": ("itel", "Itel", "itel"),
    "nokia": ("nokia", "Nokia", "nokia"),
    "sony": ("sony", "Sony", "sony"),
    "motorola": ("motorola", "Motorola", "motorola"),
    "google": ("google", "Google", "google"),
    "lenovo": ("lenovo", "Lenovo", "lenovo"),
    "asus": ("asus", "Asus", "asus"),
    "zte": ("zte", "ZTE", "zte"),
    "meizu": ("meizu", "Meizu", "meizu"),
    "alcatel": ("alcatel", "Alcatel", "alcatel"),
    "nothing": ("nothing", "Nothing", "nothing"),
    "bq": ("bq", "BQ", "bq"),
    "doogee": ("doogee", "Doogee", "doogee"),
    "oukitel": ("oukitel", "Oukitel", "oukitel"),
    "blackview": ("blackview", "Blackview", "blackview"),
    "ulefone": ("ulefone", "Ulefone", "ulefone"),
    "cubot": ("cubot", "Cubot", "cubot"),
    "fly": ("fly", "Fly", "fly"),
    "philips": ("philips", "Philips", "philips"),
    "lg": ("lg", "LG", "lg"),
    "htc": ("htc", "HTC", "htc"),
}

DEVICE_BRAND_SYNONYMS = {
    "iphone": "apple",
    "ipad": "apple",
    "ipod": "apple",
    "apple": "apple",
    "samsung": "samsung",
    "galaxy": "samsung",
    "xiaomi": "xiaomi",
    "mi": "xiaomi",
    "mipad": "xiaomi",
    "redmi": "redmi",
    "poco": "poco",
    "huawei": "huawei",
    "honor": "honor",
    "realme": "realme",
    "oppo": "oppo",
    "vivo": "vivo",
    "oneplus": "oneplus",
    "one plus": "oneplus",
    "tecno": "tecno",
    "infinix": "infinix",
    "infinx": "infinix",
    "itel": "itel",
    "nokia": "nokia",
    "sony": "sony",
    "motorola": "motorola",
    "google": "google",
    "pixel": "google",
    "lenovo": "lenovo",
    "xiaoxin": "lenovo",
    "asus": "asus",
    "zte": "zte",
    "meizu": "meizu",
    "alcatel": "alcatel",
    "nothing": "nothing",
    "cmf": "nothing",
    "bq": "bq",
    "doogee": "doogee",
    "oukitel": "oukitel",
    "blackview": "blackview",
    "ulefone": "ulefone",
    "cubot": "cubot",
    "fly": "fly",
    "philips": "philips",
    "lg": "lg",
    "htc": "htc",
}

BRAND_TOKEN_PRIORITY = (
    "redmi",
    "poco",
)


def normalize_brand_key(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower().replace("ё", "е")
    normalized = re.sub(r"[_\-]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized or None


def brand_code_from_text(value: str | None) -> str | None:
    normalized = normalize_brand_key(value)
    if not normalized:
        return None

    code = DEVICE_BRAND_SYNONYMS.get(normalized)
    if code:
        return code

    compact = normalized.replace(" ", "")
    code = DEVICE_BRAND_SYNONYMS.get(compact)
    if code:
        return code

    tokens = re.findall(r"[a-z0-9а-я]+", normalized)
    for token in BRAND_TOKEN_PRIORITY:
        if token in tokens:
            return DEVICE_BRAND_SYNONYMS[token]
    for token in tokens:
        code = DEVICE_BRAND_SYNONYMS.get(token)
        if code:
            return code

    code = normalized
    return re.sub(r"[^a-z0-9]+", "_", code).strip("_") or None


def brand_group_code(code: str | None) -> str | None:
    if not code:
        return None
    if code in {"xiaomi", "redmi", "poco"}:
        return "xiaomi"
    if code in {"huawei", "honor"}:
        return "huawei_honor"
    return code


def display_name_for_brand(code: str) -> str:
    seed = DEVICE_BRAND_SEEDS.get(code)
    if seed:
        return seed[1]
    return code.replace("_", " ").title()


def brand_code_for_model(brand: str | None, model_name: str | None = None) -> str | None:
    normalized_brand = brand_code_from_text(brand)
    normalized_model = normalize_brand_key(model_name) or ""
    if normalized_brand == "xiaomi":
        if normalized_model.startswith("redmi"):
            return "redmi"
        if normalized_model.startswith("poco"):
            return "poco"
    return normalized_brand


@dataclass(frozen=True)
class ResolvedBrand:
    brand: DeviceBrand
    normalized_key: str
    matched_by: str


class BrandResolver:
    def __init__(self, db: Session):
        self.db = db

    def ensure_seed_brands(self) -> None:
        for code, (name, display_name, group_code) in DEVICE_BRAND_SEEDS.items():
            self._get_or_create_brand(
                code=code,
                name=name,
                display_name=display_name,
                group_code=group_code,
            )

    def list_brands(self, *, q: str | None = None, limit: int = 100) -> list[DeviceBrand]:
        self.ensure_seed_brands()
        query = self.db.query(DeviceBrand).filter(DeviceBrand.is_active.is_(True))
        search = normalize_brand_key(q)
        if search:
            pattern = f"%{search}%"
            query = query.outerjoin(DeviceBrandAlias).filter(
                or_(
                    func.lower(DeviceBrand.code).like(pattern),
                    func.lower(DeviceBrand.name).like(pattern),
                    func.lower(DeviceBrand.display_name).like(pattern),
                    DeviceBrandAlias.normalized_key.like(pattern),
                )
            )
        return (
            query.order_by(DeviceBrand.display_name.asc(), DeviceBrand.code.asc())
            .distinct()
            .limit(limit)
            .all()
        )

    def create_brand(
        self,
        *,
        code: str,
        name: str | None = None,
        display_name: str | None = None,
        group_code: str | None = None,
    ) -> DeviceBrand:
        normalized_code = brand_code_from_text(code)
        if not normalized_code:
            raise ValueError("brand code is required")
        return self._get_or_create_brand(
            code=normalized_code,
            name=name or normalized_code,
            display_name=display_name or display_name_for_brand(normalized_code),
            group_code=group_code or brand_group_code(normalized_code),
        )

    def resolve(
        self,
        value: str | None,
        *,
        source: str = "manual",
        create: bool = True,
        is_manual: bool = False,
        confidence: float | Decimal | None = None,
    ) -> ResolvedBrand | None:
        normalized_key = normalize_brand_key(value)
        if not normalized_key:
            return None

        alias = (
            self.db.query(DeviceBrandAlias)
            .join(DeviceBrand)
            .filter(
                DeviceBrandAlias.normalized_key == normalized_key,
                DeviceBrandAlias.is_active.is_(True),
                DeviceBrand.is_active.is_(True),
            )
            .order_by(DeviceBrandAlias.is_manual.desc(), DeviceBrandAlias.id.asc())
            .first()
        )
        if alias:
            alias.last_seen_at = datetime.utcnow()
            self.db.add(alias)
            return ResolvedBrand(alias.brand, normalized_key, "alias")

        code = brand_code_from_text(value)
        if not code:
            return None
        brand = (
            self.db.query(DeviceBrand)
            .filter(DeviceBrand.code == code, DeviceBrand.is_active.is_(True))
            .first()
        )
        if not brand and create:
            brand = self.create_brand(code=code)
        if not brand:
            return None

        self.upsert_alias(
            brand=brand,
            raw_value=value or code,
            source=source,
            is_manual=is_manual,
            confidence=confidence,
            decision_reason="resolved_brand",
        )
        return ResolvedBrand(brand, normalized_key, "code")

    def resolve_for_model(
        self,
        brand: str | None,
        model_name: str | None = None,
        *,
        source: str = "phone_model",
        create: bool = True,
    ) -> DeviceBrand | None:
        code = brand_code_for_model(brand, model_name)
        if not code:
            return None
        resolved = self.resolve(code, source=source, create=create)
        if resolved and brand:
            self.upsert_alias(
                brand=resolved.brand,
                raw_value=brand,
                source=source,
                decision_reason="model_brand",
            )
        return resolved.brand if resolved else None

    def upsert_alias(
        self,
        *,
        brand: DeviceBrand,
        raw_value: str,
        source: str = "manual",
        is_manual: bool = False,
        confidence: float | Decimal | None = None,
        decision_reason: str | None = None,
    ) -> DeviceBrandAlias:
        normalized_key = normalize_brand_key(raw_value)
        if not normalized_key:
            raise ValueError("brand alias raw value is required")
        alias = (
            self.db.query(DeviceBrandAlias)
            .filter(
                DeviceBrandAlias.brand_id == brand.id,
                DeviceBrandAlias.source == source,
                DeviceBrandAlias.normalized_key == normalized_key,
            )
            .first()
        )
        if alias:
            alias.raw_value = raw_value
            alias.last_seen_at = datetime.utcnow()
            alias.is_active = True
            alias.is_manual = alias.is_manual or is_manual
            if confidence is not None:
                alias.confidence = float(confidence)
            if decision_reason:
                alias.decision_reason = decision_reason
            self.db.add(alias)
            return alias
        alias = DeviceBrandAlias(
            brand=brand,
            source=source,
            raw_value=raw_value,
            normalized_key=normalized_key,
            confidence=float(confidence) if confidence is not None else None,
            is_manual=is_manual,
            decision_reason=decision_reason,
        )
        self.db.add(alias)
        try:
            self.db.flush()
        except IntegrityError:
            self.db.rollback()
            alias = (
                self.db.query(DeviceBrandAlias)
                .filter(
                    DeviceBrandAlias.brand_id == brand.id,
                    DeviceBrandAlias.source == source,
                    DeviceBrandAlias.normalized_key == normalized_key,
                )
                .one()
            )
        return alias

    def _get_or_create_brand(
        self,
        *,
        code: str,
        name: str,
        display_name: str,
        group_code: str | None,
    ) -> DeviceBrand:
        brand = self.db.execute(
            select(DeviceBrand).where(DeviceBrand.code == code)
        ).scalar_one_or_none()
        if brand:
            changed = False
            if not brand.display_name and display_name:
                brand.display_name = display_name
                changed = True
            if not brand.group_code and group_code:
                brand.group_code = group_code
                changed = True
            if changed:
                self.db.add(brand)
                self.db.flush()
            return brand
        brand = DeviceBrand(
            code=code,
            name=name,
            display_name=display_name,
            group_code=group_code or brand_group_code(code),
            is_active=True,
        )
        self.db.add(brand)
        try:
            self.db.flush()
        except IntegrityError:
            self.db.rollback()
            brand = self.db.execute(
                select(DeviceBrand).where(DeviceBrand.code == code)
            ).scalar_one()
        return brand
