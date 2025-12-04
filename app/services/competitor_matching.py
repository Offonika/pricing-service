from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy import Select, and_, select
from sqlalchemy.orm import Session

from app.models import (
    Competitor,
    CompetitorFtpRecord,
    CompetitorPrice,
    Product,
    ProductMatch,
    PhoneModel,
    ProductMatchOverride,
)

logger = logging.getLogger("app.matching.competitor_ftp")


@dataclass
class MatchStats:
    processed: int = 0
    matched: int = 0
    prices_created: int = 0
    matches_created: int = 0
    unmatched: int = 0
    ambiguous: int = 0
    skipped_no_price: int = 0

    def as_dict(self) -> dict:
        return {
            "processed": self.processed,
            "matched": self.matched,
            "prices_created": self.prices_created,
            "matches_created": self.matches_created,
            "unmatched": self.unmatched,
            "ambiguous": self.ambiguous,
            "skipped_no_price": self.skipped_no_price,
        }


def _normalize_sku(value: Optional[str]) -> str:
    if not value:
        return ""
    s = str(value).strip().lower()
    s = s.replace("–", "-").replace("—", "-")
    s = re.sub(r"[\s\t\n\r]+", "", s)
    return s


def _load_products_by_sku(session: Session, subject_whitelist: Optional[set[str]] = None) -> Dict[str, List[Product]]:
    products: Dict[str, List[Product]] = {}
    for product in session.execute(select(Product)).scalars():
        if subject_whitelist and product.subject not in subject_whitelist:
            continue
        key = _normalize_sku(product.sku)
        if not key:
            continue
        products.setdefault(key, []).append(product)
    return products


BRAND_SYNONYMS = {
    "iphone": "apple",
    "apple": "apple",
    "ipad": "apple",
    "ipod": "apple",
    "samsung": "samsung",
    "galaxy": "samsung",
    "xiaomi": "xiaomi",
    "poco": "xiaomi",
    "mi": "xiaomi",
    "redmi": "xiaomi",
    "mipad": "xiaomi",
    "realme": "realme",
    "vivo": "vivo",
    "oppo": "oppo",
    "oneplus": "oneplus",
    "huawei": "huawei",
    "honor": "honor",
    "nokia": "nokia",
    "sony": "sony",
    "zte": "zte",
    "lenovo": "lenovo",
    "motorola": "motorola",
    "meizu": "meizu",
}

STOP_TOKENS = {
    "в",
    "с",
    "сборе",
    "тачскрином",
    "тачскрин",
    "черный",
    "черный-",
    "черная",
    "чёрный",
    "чёрная",
    "белый",
    "белая",
    "золотистый",
    "серый",
    "синий",
    "голубой",
    "красный",
    "розовый",
    "фиолетовый",
    "green",
    "black",
    "white",
    "gold",
    "blue",
    "red",
    "pink",
    "оптима",
    "копия",
    "ориг",
    "оригинал",
    "premium",
    "orig",
    "oem",
    "aaa",
    "oled",
    "lcd",
    "frame",
    "в",
    "без",
    "рамки",
}

VARIANT_TOKENS = {
    "pro",
    "plus",
    "max",
    "ultra",
    "mini",
    "lite",
    "fe",
    "edge",
    "note",
    "se",
}

QUALITY_MAP = {
    "orig": "orig",
    "ориг": "orig",
    "оригинал": "orig",
    "oem": "oem",
    "копия": "copy",
    "copy": "copy",
    "optima": "copy",
    "premium": "premium",
    "aaa": "premium",
}

APPLE_MODEL_SKIP_TOKENS = {"iphone", "ipad", "ipod"}
APPLE_A_CODE_RE = re.compile(r"a\d{4,5}", re.IGNORECASE)


def _normalize_model(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]", "", value.lower())
    return cleaned


def _extract_brand_model(name: Optional[str]) -> Tuple[Optional[str], List[str]]:
    if not name:
        return None, []
    lower = name.lower()
    if "дисплей" not in lower:
        return None, []
    tokens = re.findall(r"[A-Za-z0-9а-яё]+", lower)
    # попытка найти бренд после "для"
    brand = None
    start_idx = None
    for idx, tok in enumerate(tokens):
        if tok == "для" and idx + 1 < len(tokens):
            brand_token = tokens[idx + 1]
            brand = BRAND_SYNONYMS.get(brand_token, brand_token)
            start_idx = idx + 2
            break
    # fallback: взять первый встретившийся бренд/синоним, даже без "для"
    if brand is None:
        for idx, tok in enumerate(tokens):
            candidate = BRAND_SYNONYMS.get(tok)
            if candidate:
                brand = candidate
                start_idx = idx + 1
                break
    if brand is None or start_idx is None:
        return None, []

    models: List[str] = []
    current: List[str] = []

    def flush_current() -> None:
        nonlocal current
        if current:
            norm = _normalize_model(" ".join(current))
            if norm:
                models.append(norm)
            current = []

    for tok in tokens[start_idx:]:
        # новый кандидат модели при повторном упоминании бренда
        if BRAND_SYNONYMS.get(tok) == brand:
            flush_current()
            continue
        if brand == "apple":
            if tok in APPLE_MODEL_SKIP_TOKENS:
                continue
            if APPLE_A_CODE_RE.fullmatch(tok):
                continue
        if not tok or tok in STOP_TOKENS:
            break
        current.append(tok)
    flush_current()
    return brand, models


def _extract_quality(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    lower = name.lower()
    for token, quality in QUALITY_MAP.items():
        if token in lower:
            return quality
    return None


def _normalize_quality_value(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    v = value.strip().lower()
    if v in {"orig", "or", "or100", "orig100", "ориг100", "ориг", "ор"}:
        return "orig"
    if v in {"copy", "копия", "optima", "оптима"}:
        return "copy"
    if v in {"oem"}:
        return "oem"
    if v in {"premium", "премиум"}:
        return "premium"
    return v


def _extract_display_type(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    lower = name.lower()
    if "hard oled" in lower or "hard oled" in lower.replace("-", " "):
        return "hard oled"
    if "soft oled" in lower or "soft oled" in lower.replace("-", " "):
        return "soft oled"
    if "oled" in lower:
        return "oled"
    if "in-cell" in lower or "incell" in lower:
        return "in-cell"
    if "tft" in lower or "lcd" in lower:
        return "lcd"
    return None


def _extract_in_frame(name: Optional[str]) -> Optional[bool]:
    if not name:
        return None
    lower = name.lower()
    if "в рамке" in lower:
        return True
    if "без рамки" in lower or "no frame" in lower:
        return False
    return None


def _extract_variant(model_tokens: List[str]) -> Tuple[List[str], Optional[str]]:
    if not model_tokens:
        return model_tokens, None
    variant = None
    filtered: List[str] = []
    for tok in model_tokens:
        if tok in VARIANT_TOKENS and variant is None:
            variant = tok
            continue
        filtered.append(tok)
    return filtered, variant


def _load_products_by_brand_model(session: Session, subject_whitelist: Optional[set[str]] = None) -> Dict[str, List[Tuple[str, Product]]]:
    brand_map: Dict[str, List[Tuple[str, Product]]] = {}
    for product in session.execute(select(Product)).scalars():
        if subject_whitelist and product.subject not in subject_whitelist:
            continue
        brand, models = _extract_brand_model(product.name)
        if not brand or not models:
            continue
        for model in models:
            brand_map.setdefault(brand, []).append((model, product))
    return brand_map


def _load_overrides(session: Session, sources: Optional[Sequence[str]]) -> Dict[tuple, ProductMatchOverride]:
    query = select(ProductMatchOverride)
    if sources:
        query = query.where(ProductMatchOverride.competitor_source.in_(list(sources)))
    overrides: Dict[tuple, ProductMatchOverride] = {}
    for ov in session.execute(query).scalars():
        key = (ov.competitor_source, _normalize_sku(ov.competitor_sku) if ov.competitor_sku else None)
        overrides[key] = ov
    return overrides


def _ensure_competitor(session: Session, name: str) -> Competitor:
    competitor = (
        session.execute(select(Competitor).where(Competitor.name == name)).scalar_one_or_none()
    )
    if competitor:
        return competitor
    competitor = Competitor(name=name)
    session.add(competitor)
    session.flush()
    return competitor


def _existing_match(session: Session, product_id: int, competitor_id: int) -> Optional[ProductMatch]:
    stmt: Select[tuple[ProductMatch]] = select(ProductMatch).where(
        ProductMatch.product_id == product_id,
        ProductMatch.competitor_id == competitor_id,
    )
    return session.execute(stmt).scalar_one_or_none()


def _existing_price(
    session: Session,
    product_id: int,
    competitor_id: int,
    collected_at,
) -> Optional[CompetitorPrice]:
    stmt: Select[tuple[CompetitorPrice]] = select(CompetitorPrice).where(
        CompetitorPrice.product_id == product_id,
        CompetitorPrice.competitor_id == competitor_id,
        CompetitorPrice.collected_at == collected_at,
    )
    return session.execute(stmt).scalar_one_or_none()


def match_competitor_ftp_records(
    session: Session,
    days_back: int = 3,
    sources: Optional[Sequence[str]] = None,
    max_samples: int = 20,
    subject_whitelist: Optional[Sequence[str]] = None,
) -> dict:
    """
    Сопоставляет FTP-записи конкурентов с товарами по SKU и пишет цены в competitor_price.
    """
    stats = MatchStats()
    since_date = date.today() - timedelta(days=days_back)

    query = select(CompetitorFtpRecord).where(CompetitorFtpRecord.file_date >= since_date)
    if sources:
        query = query.where(CompetitorFtpRecord.source.in_(list(sources)))

    records = list(session.execute(query).scalars())
    if not records:
        return {"skipped": True, "reason": "no_records"}

    subject_whitelist_set = set(subject_whitelist) if subject_whitelist else None
    products_by_sku = _load_products_by_sku(session, subject_whitelist_set)
    products_by_brand_model = _load_products_by_brand_model(session, subject_whitelist_set)
    overrides = _load_overrides(session, sources)
    competitors: Dict[str, Competitor] = {}
    unmatched_samples: List[dict] = []
    ambiguous_samples: List[dict] = []

    for record in records:
        stats.processed += 1
        product: Optional[Product] = None
        phone_model: Optional[PhoneModel] = None
        quality = _extract_quality(record.name)
        is_manual = False

        sku_norm = _normalize_sku(record.sku)
        override = overrides.get((record.source, sku_norm)) or overrides.get((record.source, None))
        if override:
            if override.product_id:
                product = session.get(Product, override.product_id)
            if override.phone_model_id:
                phone_model = session.get(PhoneModel, override.phone_model_id)
            if override.brand and override.model and not phone_model:
                pm = session.execute(
                    select(PhoneModel).where(
                        PhoneModel.brand == override.brand,
                        PhoneModel.model_name == override.model,
                    )
                ).scalar_one_or_none()
                if pm:
                    phone_model = pm
            if override.quality:
                quality = override.quality
            is_manual = True

        if product is None and sku_norm:
            candidates = products_by_sku.get(sku_norm) or []
            if candidates:
                if len(candidates) > 1:
                    stats.ambiguous += 1
                    if len(ambiguous_samples) < max_samples:
                        ambiguous_samples.append({"source": record.source, "sku": record.sku, "name": record.name})
                    continue
                product = candidates[0]

        if product is None:
            brand, models = _extract_brand_model(record.name)
            variant = None
            if brand and models:
                product_candidates = products_by_brand_model.get(brand, [])
                matched_products: List[Product] = []
                seen_ids = set()
                quality_token = _extract_quality(record.name)
                display_type_token = _extract_display_type(record.name)
                in_frame_token = _extract_in_frame(record.name)
                for cand_model, cand_product in product_candidates:
                    if any(
                        cand_model == model or cand_model in model or model in cand_model
                        for model in models
                    ):
                        if cand_product.id not in seen_ids:
                            matched_products.append(cand_product)
                            seen_ids.add(cand_product.id)
                # try to disambiguate by quality/display type/frame
                if len(matched_products) > 1:
                    norm_quality = _normalize_quality_value(quality_token)
                    if norm_quality:
                        filtered = [
                            p
                            for p in matched_products
                            if _normalize_quality_value(getattr(p, "quality", None)) == norm_quality
                        ]
                        if filtered:
                            matched_products = filtered
                if len(matched_products) > 1 and display_type_token:
                    filtered = [
                        p
                        for p in matched_products
                        if (getattr(p, "display_type", None) or "").lower() == display_type_token.lower()
                    ]
                    if filtered:
                        matched_products = filtered
                if len(matched_products) > 1 and in_frame_token is not None:
                    filtered = []
                    for p in matched_products:
                        val = getattr(p, "in_frame", None)
                        if val is None:
                            continue
                        val_norm = str(val).lower()
                        if in_frame_token and val_norm in {"да", "yes", "true", "1"}:
                            filtered.append(p)
                        if in_frame_token is False and val_norm in {"нет", "no", "false", "0"}:
                            filtered.append(p)
                    if filtered:
                        matched_products = filtered
                if len(matched_products) == 1:
                    product = matched_products[0]
                elif len(matched_products) > 1:
                    stats.ambiguous += 1
                    if len(ambiguous_samples) < max_samples:
                        ambiguous_samples.append({"source": record.source, "sku": record.sku, "name": record.name})
                    continue
                # create/find phone model with variant if possible
                model_tokens = re.findall(r"[A-Za-z0-9]+", record.name.lower())
                model_tokens, variant = _extract_variant(model_tokens)
                phone_model_name = models[0] if models else None
                if phone_model_name:
                    phone_model = (
                        session.execute(
                            select(PhoneModel).where(
                                PhoneModel.brand == brand,
                                PhoneModel.model_name == phone_model_name,
                                PhoneModel.variant == variant,
                            )
                        ).scalar_one_or_none()
                    )
                    if phone_model is None:
                        phone_model = PhoneModel(brand=brand, model_name=phone_model_name, variant=variant)
                        session.add(phone_model)
                        session.flush()

        if product is None:
            stats.unmatched += 1
            if len(unmatched_samples) < max_samples:
                unmatched_samples.append({"source": record.source, "sku": record.sku, "name": record.name})
            continue
        competitor = competitors.get(record.source)
        if competitor is None:
            competitor = _ensure_competitor(session, record.source)
            competitors[record.source] = competitor

        price = record.price_roz if record.price_roz is not None else record.price_opt
        if price is None:
            stats.skipped_no_price += 1
            continue

        existing_price = _existing_price(
            session,
            product_id=product.id,
            competitor_id=competitor.id,
            collected_at=record.observed_at,
        )
        if not existing_price:
            cp = CompetitorPrice(
                product_id=product.id,
                competitor_id=competitor.id,
                price=price,
                in_stock=record.in_stock,
                collected_at=record.observed_at,
            )
            session.add(cp)
            stats.prices_created += 1

        pm = _existing_match(session, product_id=product.id, competitor_id=competitor.id)
        if not pm:
            match = ProductMatch(
                product_id=product.id,
                competitor_id=competitor.id,
                competitor_sku=record.sku,
                confidence=1.0,
                is_manual=is_manual,
                phone_model_id=phone_model.id if phone_model else None,
                quality=quality,
            )
            session.add(match)
            stats.matches_created += 1
        else:
            updated = False
            if phone_model and not pm.phone_model_id:
                pm.phone_model_id = phone_model.id
                updated = True
            if quality and not pm.quality:
                pm.quality = quality
                updated = True
            if is_manual and not pm.is_manual:
                pm.is_manual = True
                updated = True
            if updated:
                session.add(pm)

        stats.matched += 1

    session.commit()
    result = {
        "skipped": False,
        **stats.as_dict(),
        "unmatched_samples": unmatched_samples,
        "ambiguous_samples": ambiguous_samples,
    }
    return result


__all__ = ["match_competitor_ftp_records"]
