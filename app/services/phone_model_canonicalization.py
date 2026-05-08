from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import PhoneModel, PhoneModelAlias, Product, ProductPhoneModel
from app.services.device_brands import BrandResolver

BRAND_SYNONYMS = {
    "iphone": "apple",
    "ipad": "apple",
    "ipod": "apple",
    "apple": "apple",
    "microsoft": "microsoft",
    "samsung": "samsung",
    "galaxy": "samsung",
    "xiaomi": "xiaomi",
    "redmi": "xiaomi",
    "poco": "xiaomi",
    "mi": "xiaomi",
    "huawei": "huawei",
    "honor": "honor",
    "highscreen": "highscreen",
    "explay": "explay",
    "wiko": "wiko",
    "bq": "bq",
    "dexp": "dexp",
    "nothing": "nothing",
    "cmf": "nothing",
    "nokia": "nokia",
    "realme": "realme",
    "oppo": "oppo",
    "vivo": "vivo",
    "itel": "itel",
    "iiif150": "iiif150",
    "oneplus": "oneplus",
    "google": "google",
    "pixel": "google",
    "sony": "sony",
    "motorola": "motorola",
    "lenovo": "lenovo",
    "infinix": "infinix",
    "infinx": "infinix",
    "tecno": "tecno",
    "asus": "asus",
    "acer": "acer",
    "alcatel": "alcatel",
    "zte": "zte",
    "meizu": "meizu",
    "blackview": "blackview",
    "doogee": "doogee",
    "oukitel": "oukitel",
    "ulefone": "ulefone",
    "cubot": "cubot",
    "tcl": "tcl",
    "siemens": "siemens",
    "fly": "fly",
    "philips": "philips",
    "lg": "lg",
    "htc": "htc",
}

VARIANT_WORDS = {
    "pro",
    "max",
    "plus",
    "mini",
    "ultra",
    "fe",
    "lite",
    "se",
    "edge",
    "neo",
    "youth",
}

COMPETITOR_BLOCKED_BRANDS = {"generic"}

COMPETITOR_ACCESSORY_KEYWORDS = {
    "гарнитура",
    "кабель",
    "адаптер",
    "adapter",
    "charger",
    "charging",
    "зарядное",
    "зарядка",
    "powerbank",
    "power bank",
    "external battery",
    "внешний акб",
    "акустика",
    "speaker system",
    "аудиокабель",
    "audio adapter",
    "tws",
    "usb кабель",
    "автомобильное зарядное устройство",
    "геймпада",
    "playstation",
}

COMPETITOR_PART_NUMBER_PATTERNS = (
    re.compile(r"\bgh(?:81|82|96|97|98)\s*[- ]?\s*\d{4,}[a-z]?\b", re.IGNORECASE),
    re.compile(r"\bgh\d{6,}[a-z]?\b", re.IGNORECASE),
)

COMPETITOR_MULTI_FAMILY_PATTERNS = {
    "xiaomi": (("redmi", "poco"),),
    "infinix": (("infinix", "tecno"),),
    "tecno": (("tecno", "infinix"),),
}

APPLE_CONNECTIVITY_QUALIFIERS_RE = re.compile(
    r"""
    \(
        \s*
        (?:
            e\s*sim(?:\s+version|\s+версия)?
            |
            sim\s*\+\s*e\s*sim(?:\s+version|\s+версия)?
        )
        \s*
    \)
    """,
    re.IGNORECASE | re.VERBOSE,
)

APPLE_HARDWARE_VARIANT_TOKEN_RE = re.compile(r"^(?:a\d{4,5}|or100)$", re.IGNORECASE)
SAMSUNG_HARDWARE_CODE_RE = re.compile(
    r"\b(?:sm\s*)?((?:s|a|m|g|j|n|f|z)\d{3,4})[a-z]?\b",
    re.IGNORECASE,
)
XIAOMI_CODE_MODEL_OVERRIDES: dict[str, str] = {
    "m2101k6g": "redmi note 10 pro 4g (m2101k6g)",
    "2201116sg": "redmi note 11 pro 5g (2201116sg)",
    "21091116ug": "redmi note 11 pro+ 5g (21091116ug)",
    "2201122g": "12 pro (2201122g)",
    "2201123g": "12 (2201123g)",
    "2203129g": "12 lite (2203129g)",
    "2210132g": "13 pro (2210132g)",
    "22101316g": "redmi note 12 pro 5g (22101316g)",
    "22101316ug": "redmi note 12 pro+ 5g (22101316ug)",
    "2209116ag": "redmi note 12 pro 4g (2209116ag)",
    "2211133g": "13 (2211133g)",
    "2306epn60g": "13t (2306epn60g)",
    "23078pnd5g": "13t pro (23078pnd5g)",
    "23127pn0cg": "14 (23127pn0cg)",
    "2506bpn68g": "15t pro (2506bpn68g)",
    "24117rk2cg": "poco f7 pro (24117rk2cg)",
    "24117rk2cgi": "poco f7 pro (24117rk2cg)",
    "2409fpcc4g": "poco m7 pro 5g (2409fpcc4g)",
    "24069pc21g": "poco f6 (24069pc21g)",
    "23113rkc6g": "poco f6 pro (23113rkc6g)",
    "23122pcd1g": "poco x6 (23122pcd1g)",
    "24095pcadg": "poco x7 (24095pcadg)",
    "24116raccg": "redmi note 14 pro 4g (24116raccg)",
    "22011119uy": "redmi 10 2022 (22011119uy)",
    "m2101k9g": "mi 11 lite 5g (m2101k9g)",
}
HUAWEI_HONOR_CODE_MODEL_OVERRIDES: dict[str, tuple[str, str]] = {
    "lly nx1": ("huawei", "honor 200 lite (lly nx1)"),
    "dny nx9": ("huawei", "honor 400 (dny nx9)"),
    "dnp nx9": ("huawei", "honor 400 pro (dnp nx9)"),
    "abr nx1": ("huawei", "honor 400 lite (abr nx1)"),
    "lly lx1": ("huawei", "honor x8b (lly lx1)"),
    "dco lx9": ("huawei", "mate 50 pro (dco lx9)"),
}
SAMSUNG_CODE_MODEL_OVERRIDES: dict[str, tuple[str, str | None]] = {
    "g780": ("galaxy s20 fe", None),
    "g980": ("galaxy s20", None),
    "g981": ("galaxy s20 5g", None),
    "g985": ("galaxy s20+", None),
    "g986": ("galaxy s20+ 5g", None),
    "g988": ("galaxy s20 ultra", None),
    "g990": ("galaxy s21 fe", None),
    "g991": ("galaxy s21", None),
    "g996": ("galaxy s21+", None),
    "g998": ("galaxy s21 ultra", None),
    "s711": ("galaxy s23", "fe"),
    "s901": ("galaxy s22", None),
    "s906": ("galaxy s22+", None),
    "s908": ("galaxy s22", "ultra"),
    "s911": ("galaxy s23", None),
    "s916": ("galaxy s23+", None),
    "s918": ("galaxy s23", "ultra"),
    "s926": ("galaxy s24+", None),
}

TABLET_KEYWORDS = {
    "ipad",
    "tablet",
    "tab",
    "pad",
    "mipad",
    "matepad",
    "mediapad",
    "galaxy tab",
    "iconia tab",
    "lenovo tab",
}

NON_TARGET_DEVICE_KEYWORDS = {
    "macbook",
    "notebook",
    "laptop",
    "thinkpad",
    "vivobook",
    "zenbook",
    "ideapad",
    "chromebook",
    "aspire",
    "transformer pad tf",
    "n173hce",
    "b156",
    "матрица",
    "monitor",
    "монитор",
    "printer",
    "принтер",
    "watch",
    "smart watch",
}

NOISE_KEYWORDS = {
    *COMPETITOR_ACCESSORY_KEYWORDS,
    "сервис",
    "услуга",
    "ремонт",
    "tool",
    "инструмент",
}

LONG_NO_SPACES_RE = re.compile(r"^[a-z0-9]{18,}$")
PART_NUMBER_ONLY_RE = re.compile(r"^(?:[a-z]{1,4}\d{3,}[a-z0-9]*|[a-z0-9]{12,})$")
MODEL_CODE_SUFFIX_RE = re.compile(r"\s*\([^)]*\)\s*$")

ONEC_NON_PHONE_KINDS = {
    "Ноутбук/ПК — запчасти и узлы",
    "Инструменты и оборудование (ремонт)",
}
ONEC_NON_PHONE_SUBJECTS = {
    "матрица для ноутбука",
    "клавиатура",
    "тачпад",
}
ONEC_PLACEHOLDER_COMPATIBILITIES = {
    "",
    "<>",
    "-",
    "unknown",
    "неизвестно",
}
ONEC_NON_PHONE_TEXT_PATTERNS = (
    re.compile(r"\bapple\s+watch\b", re.IGNORECASE),
    re.compile(r"\bamazfit\b", re.IGNORECASE),
    re.compile(r"\bgarmin\b", re.IGNORECASE),
    re.compile(r"\bgalaxy\s+watch\b", re.IGNORECASE),
    re.compile(r"\bmagic\s*watch\b", re.IGNORECASE),
    re.compile(r"\boculus\b", re.IGNORECASE),
    re.compile(r"\bquest\b", re.IGNORECASE),
    re.compile(r"\b(?:wi[\s-]*fi\s+)?роутер\w*\b", re.IGNORECASE),
    re.compile(r"\brouter\b", re.IGNORECASE),
    re.compile(r"\bvostro\b", re.IGNORECASE),
    re.compile(r"\binspiron\b", re.IGNORECASE),
    re.compile(r"\bideapad\b", re.IGNORECASE),
    re.compile(r"\baspire\b", re.IGNORECASE),
    re.compile(r"\bwatch\b", re.IGNORECASE),
    re.compile(r"\bmacbook\b", re.IGNORECASE),
    re.compile(r"\bipad(?:\s+pro|\s+air|\s+mini)?\b", re.IGNORECASE),
    re.compile(r"\btablet\b", re.IGNORECASE),
    re.compile(r"\bmatepad\b", re.IGNORECASE),
    re.compile(r"\bmediapad\b", re.IGNORECASE),
    re.compile(r"\bgalaxy\s+tab\b", re.IGNORECASE),
)


def _collapse_spaces(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = re.sub(r"\s+", " ", str(value).strip())
    return cleaned or None


def normalize_brand(value: str | None) -> str | None:
    cleaned = _collapse_spaces(value)
    if not cleaned:
        return None
    return BRAND_SYNONYMS.get(cleaned.lower(), cleaned.lower())


def normalize_model_name(value: str | None) -> str | None:
    cleaned = _collapse_spaces(value)
    if not cleaned:
        return None
    normalized = cleaned.replace("_", " ").replace("-", " ")
    normalized = re.sub(r"\s+", " ", normalized).strip().lower()
    return normalized or None


def normalize_variant(value: str | None) -> str | None:
    cleaned = normalize_model_name(value)
    return cleaned or None


def normalize_model_family_name(value: str | None) -> str | None:
    model_norm = normalize_model_name(value)
    if not model_norm:
        return None
    stripped = MODEL_CODE_SUFFIX_RE.sub("", model_norm).strip()
    return stripped or model_norm


def strip_apple_connectivity_qualifiers(value: str | None) -> str | None:
    cleaned = _collapse_spaces(value)
    if not cleaned:
        return None
    stripped = APPLE_CONNECTIVITY_QUALIFIERS_RE.sub(" ", cleaned)
    stripped = re.sub(r"\s+", " ", stripped).strip()
    return stripped or None


def split_model_and_variant(
    model_name: str | None, variant: str | None
) -> tuple[str | None, str | None]:
    model_norm = normalize_model_name(model_name)
    variant_norm = normalize_variant(variant)
    if not model_norm:
        return None, variant_norm
    if variant_norm:
        if model_norm.endswith(f" {variant_norm}"):
            stripped = model_norm[: -len(variant_norm)].strip()
            return stripped or model_norm, variant_norm
        return model_norm, variant_norm

    parts = model_norm.split()
    if len(parts) >= 2 and " ".join(parts[-2:]) == "pro max":
        return " ".join(parts[:-2]) or model_norm, "pro max"
    if parts[-1] in VARIANT_WORDS:
        return " ".join(parts[:-1]) or model_norm, parts[-1]
    return model_norm, None


def is_apple_hardware_variant(value: str | None) -> bool:
    normalized = normalize_variant(value)
    if not normalized:
        return False
    tokens = [token for token in re.split(r"[/\s]+", normalized) if token]
    if not tokens:
        return False
    return all(APPLE_HARDWARE_VARIANT_TOKEN_RE.fullmatch(token) for token in tokens)


def build_normalized_key(
    brand: str | None, model_name: str | None, variant: str | None
) -> str | None:
    brand_norm = normalize_brand(brand)
    model_norm, variant_norm = split_model_and_variant(model_name, variant)
    if not brand_norm or not model_norm:
        return None
    return "|".join(part for part in (brand_norm, model_norm, variant_norm) if part)


def parse_raw_device(raw_value: str | None) -> tuple[str | None, str | None, str | None]:
    cleaned = normalize_model_name(raw_value)
    if not cleaned:
        return None, None, None
    tokens = [tok for tok in cleaned.split() if tok]
    if not tokens:
        return None, None, None
    brand: str | None = None
    brand_idx: int | None = None
    for idx, token in enumerate(tokens[:5]):
        normalized = normalize_brand(token)
        if normalized in BRAND_SYNONYMS.values():
            brand = normalized
            brand_idx = idx
            break
    if brand is None:
        joined = " ".join(tokens)
        for token in sorted(BRAND_SYNONYMS.keys(), key=len, reverse=True):
            if re.search(rf"\b{re.escape(token)}\b", joined):
                brand = BRAND_SYNONYMS[token]
                break
    if brand is None:
        return None, None, None
    if brand_idx is not None:
        preserve_family_token = tokens[brand_idx] in {"iphone", "ipad", "ipod"}
        start_idx = brand_idx if preserve_family_token else brand_idx + 1
        model_tokens = tokens[start_idx:]
    else:
        model_tokens = [tok for tok in tokens if normalize_brand(tok) != brand]
    model = " ".join(model_tokens) if model_tokens else None
    model_norm, variant_norm = split_model_and_variant(model, None)
    return brand, model_norm, variant_norm


def _confidence_to_float(value: float | Decimal | None) -> float | None:
    if value is None:
        return None
    return float(value)


@dataclass
class CanonicalizationResult:
    phone_model: PhoneModel | None
    normalized_key: str | None
    confidence: float | None
    reason: str
    created_new: bool = False
    ambiguous: bool = False
    raw_brand: str | None = None
    raw_model: str | None = None
    raw_variant: str | None = None
    device_type: str = "other"


@dataclass
class ProductCompatibilityScreen:
    eligible_for_phone_canonicalization: bool
    filter_reason: str | None = None


def screen_product_phone_compatibility(
    product: Product,
    raw_value: str | None,
    *,
    source: str,
) -> ProductCompatibilityScreen:
    if source != "onec":
        return ProductCompatibilityScreen(eligible_for_phone_canonicalization=True)

    raw_norm = normalize_model_name(raw_value) or ""
    if raw_norm in ONEC_PLACEHOLDER_COMPATIBILITIES:
        return ProductCompatibilityScreen(
            eligible_for_phone_canonicalization=False,
            filter_reason="placeholder_compatibility",
        )

    kind = _collapse_spaces(product.vid_nomenklatury_1c) or _collapse_spaces(
        product.vid_nomenklatury
    )
    if kind in ONEC_NON_PHONE_KINDS:
        return ProductCompatibilityScreen(
            eligible_for_phone_canonicalization=False,
            filter_reason="non_phone_kind",
        )

    subject = _collapse_spaces(product.subject_1c) or _collapse_spaces(product.subject)
    if subject and subject.lower() in ONEC_NON_PHONE_SUBJECTS:
        return ProductCompatibilityScreen(
            eligible_for_phone_canonicalization=False,
            filter_reason="non_phone_subject",
        )

    rendered = " ".join(
        part
        for part in (
            _collapse_spaces(raw_value),
            _collapse_spaces(product.name),
        )
        if part
    )
    if any(pattern.search(rendered) for pattern in ONEC_NON_PHONE_TEXT_PATTERNS):
        return ProductCompatibilityScreen(
            eligible_for_phone_canonicalization=False,
            filter_reason="non_phone_text",
        )

    return ProductCompatibilityScreen(eligible_for_phone_canonicalization=True)


class PhoneModelCanonicalizer:
    def __init__(self, db: Session):
        self.db = db
        self.settings = get_settings()
        self.brand_resolver = BrandResolver(db)

    def canonicalize(
        self,
        *,
        source: str,
        raw_value: str | None = None,
        brand: str | None = None,
        model_name: str | None = None,
        variant: str | None = None,
        confidence: float | Decimal | None = None,
        is_manual: bool = False,
    ) -> CanonicalizationResult:
        raw_brand = brand
        raw_model = model_name
        raw_variant = variant
        if not brand and not model_name and raw_value:
            brand, model_name, variant = parse_raw_device(raw_value)
        original_model_norm = normalize_model_name(model_name)
        brand_norm = normalize_brand(brand)
        if brand_norm == "apple":
            model_name = strip_apple_connectivity_qualifiers(model_name)
            variant = strip_apple_connectivity_qualifiers(variant)
        model_norm, variant_norm = split_model_and_variant(model_name, variant)
        if source == "competitor_parser":
            brand_norm, model_norm, variant_norm = self._normalize_competitor_identity(
                brand=brand_norm,
                model_name=model_norm,
                variant=variant_norm,
                raw_value=raw_value,
            )
        device_brand = self.brand_resolver.resolve_for_model(
            brand_norm,
            model_norm,
            source=source,
            create=True,
        )
        normalized_key = build_normalized_key(brand_norm, model_norm, variant_norm)
        confidence_value = _confidence_to_float(confidence)
        device_type = self._classify_device_type(
            brand=brand_norm,
            model_name=model_norm,
            variant=variant_norm,
            raw_value=raw_value,
        )

        if source == "onec" and confidence_value is None:
            confidence_value = self._estimate_onec_confidence(
                brand=brand_norm,
                model_name=model_norm,
                variant=variant_norm,
                raw_value=raw_value,
                device_type=device_type,
            )

        blocked_reason: str | None = None
        if source == "competitor_parser":
            blocked_reason = self._blocked_competitor_reason(
                brand=brand_norm,
                model_name=model_norm,
                variant=variant_norm,
                raw_value=raw_value,
                device_type=device_type,
            )
            if (
                blocked_reason is None
                and not model_norm
                and self._is_part_number_noise(original_model_norm)
            ):
                blocked_reason = "blocked_part_number_noise"
        elif source == "onec":
            blocked_reason = self._blocked_onec_reason(
                brand=brand_norm,
                model_name=model_norm,
                variant=variant_norm,
                raw_value=raw_value,
                device_type=device_type,
            )

        if blocked_reason:
            return CanonicalizationResult(
                phone_model=None,
                normalized_key=normalized_key,
                confidence=confidence_value,
                reason=blocked_reason,
                raw_brand=raw_brand or brand_norm,
                raw_model=raw_model or model_norm,
                raw_variant=raw_variant or variant_norm,
                device_type=device_type,
            )

        if not brand_norm or not model_norm or not normalized_key:
            return CanonicalizationResult(
                phone_model=None,
                normalized_key=normalized_key,
                confidence=confidence_value,
                reason="missing_brand_or_model",
                raw_brand=raw_brand or brand_norm,
                raw_model=raw_model or model_norm,
                raw_variant=raw_variant or variant_norm,
                device_type=device_type,
            )

        model = self._find_exact_model(brand_norm, model_norm, variant_norm)
        if model:
            self._sync_device_brand(model, device_brand)
            self._upsert_alias(
                phone_model=model,
                source=source,
                raw_value=raw_value or self._compose_raw_value(brand, model_name, variant),
                raw_brand=raw_brand or brand,
                raw_model=raw_model or model_name,
                raw_variant=raw_variant or variant,
                normalized_key=normalized_key,
                confidence=confidence_value,
                is_manual=is_manual,
                decision_reason="exact_model_match",
                device_type=device_type,
            )
            return CanonicalizationResult(
                phone_model=model,
                normalized_key=normalized_key,
                confidence=confidence_value,
                reason="exact_model_match",
                raw_brand=raw_brand or brand_norm,
                raw_model=raw_model or model_norm,
                raw_variant=raw_variant or variant_norm,
                device_type=device_type,
            )

        family_model = self._find_family_model(
            brand=brand_norm,
            model_name=model_norm,
            variant=variant_norm,
            raw_value=raw_value,
        )
        if family_model:
            self._sync_device_brand(family_model, device_brand)
            self._upsert_alias(
                phone_model=family_model,
                source=source,
                raw_value=raw_value or self._compose_raw_value(brand, model_name, variant),
                raw_brand=raw_brand or brand,
                raw_model=raw_model or model_name,
                raw_variant=raw_variant or variant,
                normalized_key=normalized_key,
                confidence=confidence_value,
                is_manual=is_manual,
                decision_reason="family_model_match",
                device_type=device_type,
            )
            return CanonicalizationResult(
                phone_model=family_model,
                normalized_key=normalized_key,
                confidence=confidence_value,
                reason="family_model_match",
                raw_brand=raw_brand or brand_norm,
                raw_model=raw_model or model_norm,
                raw_variant=raw_variant or variant_norm,
                device_type=device_type,
            )

        alias_models = self._find_models_by_alias(normalized_key)
        if len(alias_models) == 1:
            model = alias_models[0]
            self._sync_device_brand(model, device_brand)
            self._upsert_alias(
                phone_model=model,
                source=source,
                raw_value=raw_value or self._compose_raw_value(brand, model_name, variant),
                raw_brand=raw_brand or brand,
                raw_model=raw_model or model_name,
                raw_variant=raw_variant or variant,
                normalized_key=normalized_key,
                confidence=confidence_value,
                is_manual=is_manual,
                decision_reason="alias_match",
                device_type=device_type,
            )
            return CanonicalizationResult(
                phone_model=model,
                normalized_key=normalized_key,
                confidence=confidence_value,
                reason="alias_match",
                raw_brand=raw_brand or brand_norm,
                raw_model=raw_model or model_norm,
                raw_variant=raw_variant or variant_norm,
                device_type=device_type,
            )

        if len(alias_models) > 1:
            return CanonicalizationResult(
                phone_model=None,
                normalized_key=normalized_key,
                confidence=confidence_value,
                reason="ambiguous_alias_match",
                ambiguous=True,
                raw_brand=raw_brand or brand_norm,
                raw_model=raw_model or model_norm,
                raw_variant=raw_variant or variant_norm,
                device_type=device_type,
            )

        if self._can_create(
            source=source,
            confidence=confidence_value,
            is_manual=is_manual,
            device_type=device_type,
        ):
            model = PhoneModel(
                brand=brand_norm,
                brand_id=device_brand.id if device_brand else None,
                model_name=model_norm,
                variant=variant_norm,
            )
            self.db.add(model)
            self.db.flush()
            self._upsert_alias(
                phone_model=model,
                source=source,
                raw_value=raw_value or self._compose_raw_value(brand, model_name, variant),
                raw_brand=raw_brand or brand,
                raw_model=raw_model or model_name,
                raw_variant=raw_variant or variant,
                normalized_key=normalized_key,
                confidence=confidence_value,
                is_manual=is_manual,
                decision_reason="created_new_model",
                device_type=device_type,
            )
            return CanonicalizationResult(
                phone_model=model,
                normalized_key=normalized_key,
                confidence=confidence_value,
                reason="created_new_model",
                created_new=True,
                raw_brand=raw_brand or brand_norm,
                raw_model=raw_model or model_norm,
                raw_variant=raw_variant or variant_norm,
                device_type=device_type,
            )

        return CanonicalizationResult(
            phone_model=None,
            normalized_key=normalized_key,
            confidence=confidence_value,
            reason="creation_not_allowed",
            raw_brand=raw_brand or brand_norm,
            raw_model=raw_model or model_norm,
            raw_variant=raw_variant or variant_norm,
            device_type=device_type,
        )

    def _sync_device_brand(self, model: PhoneModel, device_brand: object | None) -> None:
        brand_id = getattr(device_brand, "id", None)
        if brand_id is None or model.brand_id == brand_id:
            return
        model.brand_id = brand_id
        self.db.add(model)

    def sync_product_links(
        self,
        *,
        product: Product,
        source: str,
        raw_values: list[str],
    ) -> dict[str, int]:
        existing_links = {
            link.phone_model_id: link for link in product.phone_model_links if link.source == source
        }
        desired_links: dict[int, CanonicalizationResult] = {}
        stats = {
            "resolved": 0,
            "auto_created": 0,
            "ambiguous": 0,
            "unresolved": 0,
            "filtered_non_phone": 0,
        }
        has_filtered_values = False

        for raw_value in raw_values:
            screen = screen_product_phone_compatibility(product, raw_value, source=source)
            if not screen.eligible_for_phone_canonicalization:
                has_filtered_values = True
                stats["filtered_non_phone"] += 1
                continue
            result = self.canonicalize(source=source, raw_value=raw_value, confidence=None)
            if result.phone_model is None:
                if result.ambiguous:
                    stats["ambiguous"] += 1
                else:
                    stats["unresolved"] += 1
                continue
            desired_links[result.phone_model.id] = result
            stats["resolved"] += 1
            if result.created_new:
                stats["auto_created"] += 1

        for phone_model_id, result in desired_links.items():
            existing = existing_links.pop(phone_model_id, None)
            if existing:
                if existing.is_manual:
                    continue
                existing.raw_value = result.raw_model or existing.raw_value
                existing.confidence = result.confidence
                existing.updated_at = datetime.utcnow()
                self.db.add(existing)
                continue
            self.db.add(
                ProductPhoneModel(
                    product=product,
                    phone_model_id=phone_model_id,
                    source=source,
                    raw_value=result.raw_model or result.normalized_key,
                    confidence=result.confidence,
                )
            )

        if not has_filtered_values:
            for stale in existing_links.values():
                if stale.is_manual:
                    continue
                self.db.delete(stale)

        return stats

    def _find_exact_model(
        self, brand: str, model_name: str, variant: str | None
    ) -> PhoneModel | None:
        query = self.db.query(PhoneModel).filter(
            func.lower(PhoneModel.brand) == brand,
            func.lower(PhoneModel.model_name) == model_name,
        )
        if variant is None:
            query = query.filter(PhoneModel.variant.is_(None))
        else:
            query = query.filter(func.lower(PhoneModel.variant) == variant)
        return query.first()

    def _find_family_model(
        self,
        *,
        brand: str,
        model_name: str,
        variant: str | None,
        raw_value: str | None,
    ) -> PhoneModel | None:
        family_name = self._family_identity(model_name, variant)
        if not family_name:
            return None

        rendered = normalize_model_name(raw_value)
        if rendered and any(separator in rendered for separator in ("/", "\\")):
            return None

        candidates = (
            self.db.query(PhoneModel)
            .filter(
                func.lower(PhoneModel.brand) == brand,
                PhoneModel.is_active.is_(True),
            )
            .all()
        )

        family_candidates: list[PhoneModel] = []
        exact_family_candidates: list[PhoneModel] = []
        for candidate in candidates:
            candidate_family = self._family_identity(candidate.model_name, candidate.variant)
            if candidate_family != family_name:
                continue
            family_candidates.append(candidate)
            if (
                normalize_model_name(candidate.model_name) == family_name
                and candidate.variant is None
            ):
                exact_family_candidates.append(candidate)

        if len(exact_family_candidates) == 1:
            return exact_family_candidates[0]
        if len(family_candidates) == 1:
            return family_candidates[0]
        return None

    def _family_identity(self, model_name: str | None, variant: str | None) -> str | None:
        model_norm = normalize_model_name(model_name)
        variant_norm = normalize_variant(variant)
        rendered = model_norm
        if variant_norm and all(token in VARIANT_WORDS for token in variant_norm.split()):
            rendered = " ".join(part for part in (model_norm, variant_norm) if part)
        return normalize_model_family_name(rendered)

    def _find_models_by_alias(self, normalized_key: str) -> list[PhoneModel]:
        return (
            self.db.query(PhoneModel)
            .join(PhoneModelAlias, PhoneModelAlias.phone_model_id == PhoneModel.id)
            .filter(PhoneModelAlias.normalized_key == normalized_key)
            .all()
        )

    def _upsert_alias(
        self,
        *,
        phone_model: PhoneModel,
        source: str,
        raw_value: str | None,
        raw_brand: str | None,
        raw_model: str | None,
        raw_variant: str | None,
        normalized_key: str,
        confidence: float | None,
        is_manual: bool,
        decision_reason: str,
        device_type: str,
    ) -> PhoneModelAlias:
        alias = (
            self.db.query(PhoneModelAlias)
            .filter(
                PhoneModelAlias.phone_model_id == phone_model.id,
                PhoneModelAlias.source == source,
                PhoneModelAlias.normalized_key == normalized_key,
            )
            .first()
        )
        now = datetime.utcnow()
        if alias is None:
            alias = PhoneModelAlias(
                phone_model_id=phone_model.id,
                source=source,
                raw_value=raw_value or normalized_key,
                raw_brand=raw_brand,
                raw_model=raw_model,
                raw_variant=raw_variant,
                normalized_key=normalized_key,
                confidence=confidence,
                is_manual=is_manual,
                decision_reason=decision_reason,
                device_type=device_type,
                first_seen_at=now,
                last_seen_at=now,
            )
        else:
            if not alias.is_manual or is_manual:
                alias.raw_value = raw_value or alias.raw_value
                alias.raw_brand = raw_brand or alias.raw_brand
                alias.raw_model = raw_model or alias.raw_model
                alias.raw_variant = raw_variant or alias.raw_variant
                alias.confidence = confidence if confidence is not None else alias.confidence
                alias.decision_reason = decision_reason or alias.decision_reason
                alias.device_type = device_type or alias.device_type
            alias.is_manual = alias.is_manual or is_manual
            alias.last_seen_at = now
        self.db.add(alias)
        self.db.flush()
        return alias

    def _can_create(
        self,
        *,
        source: str,
        confidence: float | None,
        is_manual: bool,
        device_type: str,
    ) -> bool:
        if is_manual:
            return True
        if source in {"news_agent", "smartphone_release"}:
            return True
        if source == "onec":
            if device_type not in {"smartphone", "tablet"}:
                return False
            if confidence is None:
                return False
            return confidence >= self.settings.phone_model_autocreate_min_confidence_onec
        if source == "competitor_parser":
            if not self.settings.phone_model_autocreate_from_competitor_enabled:
                return False
            if device_type not in {"smartphone", "tablet"}:
                return False
            if confidence is None:
                return False
            return confidence >= self.settings.phone_model_autocreate_min_confidence
        return False

    def _compose_raw_value(
        self, brand: str | None, model_name: str | None, variant: str | None
    ) -> str | None:
        parts = [_collapse_spaces(brand), _collapse_spaces(model_name), _collapse_spaces(variant)]
        rendered = " ".join(part for part in parts if part)
        return rendered or None

    def _blocked_competitor_reason(
        self,
        *,
        brand: str | None,
        model_name: str | None,
        variant: str | None,
        raw_value: str | None,
        device_type: str,
    ) -> str | None:
        if not brand or not model_name:
            return None
        if brand in COMPETITOR_BLOCKED_BRANDS:
            return "blocked_generic_brand"
        if device_type not in {"smartphone", "tablet"}:
            return "blocked_non_target_device_type"

        rendered = self._render_for_checks(
            brand=brand,
            model_name=model_name,
            variant=variant,
            raw_value=raw_value,
        )
        if self._contains_noise_keywords(rendered):
            return "blocked_accessory_noise"
        if self._is_part_number_noise(model_name):
            return "blocked_part_number_noise"
        if self._is_long_no_spaces_noise(model_name):
            return "long_no_spaces"

        family_patterns = COMPETITOR_MULTI_FAMILY_PATTERNS.get(brand, ())
        for family_tokens in family_patterns:
            if all(token in rendered for token in family_tokens):
                return "blocked_multi_family_model"
        return None

    def _normalize_competitor_identity(
        self,
        *,
        brand: str | None,
        model_name: str | None,
        variant: str | None,
        raw_value: str | None,
    ) -> tuple[str | None, str | None, str | None]:
        brand_norm = normalize_brand(brand)
        model_norm = normalize_model_name(model_name)
        variant_norm = normalize_variant(variant)

        if raw_value and brand_norm in {None, "generic"}:
            parsed_brand, parsed_model, parsed_variant = parse_raw_device(raw_value)
            if parsed_brand:
                brand_norm = parsed_brand
                model_norm = parsed_model or model_norm
                variant_norm = parsed_variant or variant_norm

        inferred_brand, stripped_model = self._extract_brand_from_model_name(model_norm)
        if inferred_brand:
            if brand_norm in {None, "generic"} or brand_norm != inferred_brand:
                brand_norm = inferred_brand
            model_norm = stripped_model or model_norm
        elif brand_norm:
            model_norm = self._strip_leading_brand_from_model(model_norm, brand_norm)

        model_norm = self._strip_competitor_service_codes(model_norm)
        if brand_norm == "apple":
            model_norm, variant_norm = self._normalize_apple_competitor_identity(
                model_name=model_norm,
                variant=variant_norm,
            )
        elif brand_norm == "samsung":
            model_norm, variant_norm = self._normalize_samsung_competitor_identity(
                model_name=model_norm,
                variant=variant_norm,
                raw_value=raw_value,
            )
        elif brand_norm == "xiaomi":
            model_norm, variant_norm = self._normalize_xiaomi_competitor_identity(
                model_name=model_norm,
                variant=variant_norm,
                raw_value=raw_value,
            )
        elif brand_norm in {"huawei", "honor"}:
            brand_norm, model_norm, variant_norm = self._normalize_huawei_competitor_identity(
                brand=brand_norm,
                model_name=model_norm,
                variant=variant_norm,
                raw_value=raw_value,
            )
        model_norm, variant_norm = split_model_and_variant(model_norm, variant_norm)
        return brand_norm, model_norm, variant_norm

    def _blocked_onec_reason(
        self,
        *,
        brand: str | None,
        model_name: str | None,
        variant: str | None,
        raw_value: str | None,
        device_type: str,
    ) -> str | None:
        if not brand or not model_name:
            return None
        if device_type not in {"smartphone", "tablet"}:
            return "blocked_non_target_device_type"
        rendered = self._render_for_checks(
            brand=brand,
            model_name=model_name,
            variant=variant,
            raw_value=raw_value,
        )
        if self._contains_noise_keywords(rendered):
            return "blocked_accessory_noise"
        if self._is_long_no_spaces_noise(model_name):
            return "long_no_spaces"
        return None

    def _classify_device_type(
        self,
        *,
        brand: str | None,
        model_name: str | None,
        variant: str | None,
        raw_value: str | None,
    ) -> str:
        rendered = self._render_for_checks(
            brand=brand,
            model_name=model_name,
            variant=variant,
            raw_value=raw_value,
        )
        if any(token in rendered for token in NON_TARGET_DEVICE_KEYWORDS):
            return "other"
        if any(token in rendered for token in TABLET_KEYWORDS):
            return "tablet"
        if brand in {"apple"} and model_name and "ipad" in model_name:
            return "tablet"
        if brand:
            return "smartphone"
        return "other"

    def _estimate_onec_confidence(
        self,
        *,
        brand: str | None,
        model_name: str | None,
        variant: str | None,
        raw_value: str | None,
        device_type: str,
    ) -> float:
        if not brand or not model_name:
            return 0.0
        if device_type not in {"smartphone", "tablet"}:
            return 0.1
        rendered = self._render_for_checks(
            brand=brand,
            model_name=model_name,
            variant=variant,
            raw_value=raw_value,
        )
        if self._contains_noise_keywords(rendered):
            return 0.2
        if self._is_long_no_spaces_noise(model_name):
            return 0.3
        return 0.95

    def _render_for_checks(
        self,
        *,
        brand: str | None,
        model_name: str | None,
        variant: str | None,
        raw_value: str | None,
    ) -> str:
        return " ".join(
            part
            for part in (
                _collapse_spaces(brand),
                _collapse_spaces(model_name),
                _collapse_spaces(variant),
                _collapse_spaces(raw_value),
            )
            if part
        ).lower()

    def _contains_noise_keywords(self, rendered: str) -> bool:
        return any(keyword in rendered for keyword in NOISE_KEYWORDS)

    def _extract_brand_from_model_name(
        self, model_name: str | None
    ) -> tuple[str | None, str | None]:
        if not model_name:
            return None, None
        tokens = [tok for tok in model_name.split() if tok]
        if not tokens:
            return None, None
        candidate_brand = normalize_brand(tokens[0])
        if candidate_brand not in BRAND_SYNONYMS.values() or tokens[0].lower() != candidate_brand:
            return None, None
        stripped = " ".join(tokens[1:]).strip() or None
        return candidate_brand, stripped

    def _strip_leading_brand_from_model(
        self, model_name: str | None, brand: str | None
    ) -> str | None:
        if not model_name or not brand:
            return model_name
        tokens = [tok for tok in model_name.split() if tok]
        if not tokens:
            return model_name
        if normalize_brand(tokens[0]) != brand or tokens[0].lower() != brand:
            return model_name
        stripped = " ".join(tokens[1:]).strip()
        return stripped or model_name

    def _strip_competitor_service_codes(self, model_name: str | None) -> str | None:
        cleaned = normalize_model_name(model_name)
        if not cleaned:
            return None
        stripped = cleaned
        for pattern in COMPETITOR_PART_NUMBER_PATTERNS:
            stripped = pattern.sub(" ", stripped)
        stripped = re.sub(r"\bsm\b", " ", stripped)
        stripped = re.sub(r"\s+", " ", stripped).strip()
        return stripped or None

    def _normalize_apple_competitor_identity(
        self, *, model_name: str | None, variant: str | None
    ) -> tuple[str | None, str | None]:
        model_norm = strip_apple_connectivity_qualifiers(model_name)
        variant_norm = strip_apple_connectivity_qualifiers(variant)
        if not model_norm:
            return normalize_model_name(model_norm), normalize_variant(variant_norm)

        normalized_model = normalize_model_name(model_norm)
        if normalized_model and "iphone" in normalized_model:
            base_model, marketing_variant = split_model_and_variant(normalized_model, None)
            if is_apple_hardware_variant(variant_norm):
                return base_model, marketing_variant
            if marketing_variant and not variant_norm:
                return base_model, marketing_variant
        return normalize_model_name(model_norm), normalize_variant(variant_norm)

    def _normalize_samsung_competitor_identity(
        self,
        *,
        model_name: str | None,
        variant: str | None,
        raw_value: str | None,
    ) -> tuple[str | None, str | None]:
        model_norm = normalize_model_name(model_name)
        variant_norm = normalize_variant(variant)
        if not model_norm:
            return model_norm, variant_norm

        rendered = " ".join(
            part for part in (model_norm, variant_norm, normalize_model_name(raw_value)) if part
        )
        hw_code = self._extract_samsung_hardware_code(rendered)
        if not hw_code:
            return model_norm, variant_norm

        override = SAMSUNG_CODE_MODEL_OVERRIDES.get(hw_code)
        if override:
            family, forced_variant = override
            final_variant = forced_variant or variant_norm
            if final_variant and self._extract_samsung_hardware_code(final_variant):
                final_variant = None
            if final_variant in {"plus", "+"}:
                final_variant = None
            return f"{hw_code} {family}", final_variant

        cleaned = re.sub(r"\b20\d{2}\b", " ", model_norm)
        cleaned = re.sub(r"\b(?:sm|5g)\b", " ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()

        match = re.search(
            r"(?:^|\b)(?:galaxy\s+)?(s|a|m)\s*(\d{1,2})(\+|\s+plus)?(?:\s+(ultra|fe))?\b",
            cleaned,
        )
        if not match:
            return model_norm, variant_norm

        series = match.group(1)
        number = match.group(2)
        plus_marker = match.group(3)
        marketing_variant = match.group(4)

        family = f"galaxy {series}{number}"
        if plus_marker:
            family = f"{family}+"

        if "s20" in family and "5g" in rendered:
            family = f"{family} 5g"

        final_variant = marketing_variant or variant_norm
        if final_variant and self._extract_samsung_hardware_code(final_variant):
            final_variant = None
        if final_variant in {"plus", "+"}:
            final_variant = None

        return f"{hw_code} {family}", final_variant

    def _extract_samsung_hardware_code(self, value: str | None) -> str | None:
        normalized = normalize_model_name(value)
        if not normalized:
            return None
        match = SAMSUNG_HARDWARE_CODE_RE.search(normalized)
        if not match:
            return None
        return match.group(1).lower()

    def _normalize_xiaomi_competitor_identity(
        self,
        *,
        model_name: str | None,
        variant: str | None,
        raw_value: str | None,
    ) -> tuple[str | None, str | None]:
        model_norm = normalize_model_name(model_name)
        variant_norm = normalize_variant(variant)
        rendered = " ".join(
            part for part in (model_norm, variant_norm, normalize_model_name(raw_value)) if part
        )
        override = self._find_known_code_override(rendered, XIAOMI_CODE_MODEL_OVERRIDES)
        if override:
            return override, None
        return model_norm, variant_norm

    def _normalize_huawei_competitor_identity(
        self,
        *,
        brand: str | None,
        model_name: str | None,
        variant: str | None,
        raw_value: str | None,
    ) -> tuple[str | None, str | None, str | None]:
        brand_norm = normalize_brand(brand)
        model_norm = normalize_model_name(model_name)
        variant_norm = normalize_variant(variant)
        rendered = " ".join(
            part
            for part in (brand_norm, model_norm, variant_norm, normalize_model_name(raw_value))
            if part
        )
        override = self._find_known_code_override(rendered, HUAWEI_HONOR_CODE_MODEL_OVERRIDES)
        if override:
            override_brand, override_model = override
            return override_brand, override_model, None
        return brand_norm, model_norm, variant_norm

    def _find_known_code_override(self, rendered: str | None, mapping: dict):
        normalized = normalize_model_name(rendered)
        if not normalized:
            return None
        matches = []
        for key, value in sorted(mapping.items(), key=lambda item: len(item[0]), reverse=True):
            needle = normalize_model_name(key)
            if needle and re.search(rf"\b{re.escape(needle)}\b", normalized):
                matches.append(value)
        if len(matches) == 1:
            return matches[0]
        return None

    def _is_part_number_noise(self, model_name: str | None) -> bool:
        if not model_name:
            return False
        if len(model_name.split()) > 2:
            return any(
                pattern.search(model_name.lower()) for pattern in COMPETITOR_PART_NUMBER_PATTERNS
            )
        normalized = re.sub(r"[^a-z0-9]", "", model_name.lower())
        if not normalized:
            return False
        return any(pattern.search(normalized) for pattern in COMPETITOR_PART_NUMBER_PATTERNS)

    def _is_long_no_spaces_noise(self, model_name: str | None) -> bool:
        if not model_name:
            return False
        if " " in model_name:
            return False
        normalized = re.sub(r"[^a-z0-9]", "", model_name.lower())
        return bool(LONG_NO_SPACES_RE.fullmatch(normalized))


__all__ = [
    "CanonicalizationResult",
    "PhoneModelCanonicalizer",
    "build_normalized_key",
    "is_apple_hardware_variant",
    "normalize_brand",
    "normalize_model_name",
    "normalize_variant",
    "parse_raw_device",
    "split_model_and_variant",
    "strip_apple_connectivity_qualifiers",
]
