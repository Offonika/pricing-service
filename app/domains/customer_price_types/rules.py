"""Deterministic rules and hashing for customer price-type management."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from .entities import (
    CustomerPriceTypeDecision,
    CustomerPriceTypeFacts,
    LevelRule,
    ManualOverride,
    PriceTypeRuleset,
    RegistryOverride,
)

_COUNTERPARTY_REF_RE = re.compile(r"^0x[0-9a-f]{32}$")


def normalize_counterparty_ref(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _COUNTERPARTY_REF_RE.fullmatch(normalized):
        raise ValueError("counterparty_ref must be a 0x-prefixed 32-character hex reference")
    return normalized


def _month_key(value: date) -> str:
    return value.strftime("%Y-%m")


def _add_months(value: date, months: int) -> date:
    total = value.year * 12 + value.month - 1 + months
    return date(total // 12, total % 12 + 1, 1)


def proven_history_coverage_months(first_activity: date | None, snapshot_month: date) -> int:
    """Return proven full closed months, capped to the 12-month rules window."""
    if first_activity is None:
        return 0
    first_month = first_activity.replace(day=1)
    first_full_month = first_month if first_activity.day == 1 else _add_months(first_month, 1)
    if first_full_month > snapshot_month:
        return 0
    coverage = (
        (snapshot_month.year - first_full_month.year) * 12
        + snapshot_month.month
        - first_full_month.month
        + 1
    )
    return min(12, max(0, coverage))


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def load_price_type_ruleset(path: str | Path) -> PriceTypeRuleset:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    population = payload["population"]
    levels = tuple(
        LevelRule(
            key=key,
            price_type_prefix=str(item["price_type_prefix"]),
            retention_norm_3m=_decimal(item["retention_norm_3m"]),
            hold_last_month=_decimal(item["hold_last_month"]),
            downgrade_to=str(item["downgrade_to"]),
        )
        for key, item in payload["levels"].items()
    )
    overrides = tuple(
        ManualOverride(
            counterparty_code=item.get("counterparty_code"),
            counterparty_ref=(
                str(item["counterparty_ref"]).strip().lower()
                if item.get("counterparty_ref")
                else None
            ),
            override_type=str(item["override_type"]),
            reason=str(item["reason"]),
            action_required=bool(item.get("action_required", False)),
            review_type=item.get("review_type"),
            target_price_type=item.get("target_price_type"),
            through_snapshot_month=item.get("through_snapshot_month"),
        )
        for item in payload.get("manual_overrides", [])
    )
    registry_payload = population.get("registry", {}).get("entries", {})
    registry_overrides = tuple(
        RegistryOverride(
            counterparty_code=str(code).strip() or None,
            counterparty_ref=None,
            registry_class=str(registry_class).strip(),
        )
        for code, registry_class in sorted(registry_payload.items())
    )
    price_types = payload.get("price_types", {})
    sources = payload.get("source_requirements", {})
    return PriceTypeRuleset(
        version=str(payload["ruleset_version"]),
        effective_date=date.fromisoformat(str(payload["effective_date"])),
        retail_prefixes=tuple(price_types.get("retail_prefixes", ["Розница"])),
        key_account_prefixes=tuple(price_types.get("key_account_prefixes", ["Key Account"])),
        variants=tuple(price_types.get("variants", ["бн", "USD"])),
        levels=levels,
        upgrades_frozen=bool(payload["upgrades"]["frozen"]),
        zero_months_to_recovery=int(payload["sleeping"]["zero_months_to_recovery"]),
        dead_after_months=int(payload["sleeping"]["dead_after_months"]),
        mismatch_max_pct=_decimal(payload["economics"]["data_mismatch_max_pct"]),
        excluded_registry_classes=frozenset(population["excluded_registry_classes"]),
        hygiene_registry_classes=frozenset(population["hygiene_registry_classes"]),
        required_sources=tuple(
            sources.get(
                "core",
                ["contracts", "sales_history", "ledger_reconciliation", "master_data"],
            )
        ),
        manual_overrides=overrides,
        registry_overrides=registry_overrides,
    )


def _canonical(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value.quantize(Decimal("0.01")), "f")
    if isinstance(value, date):
        return value.isoformat()
    if hasattr(value, "__dataclass_fields__"):
        return _canonical(asdict(value))
    if isinstance(value, dict):
        return {str(key): _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple, set, frozenset)):
        rendered = [_canonical(item) for item in value]
        return sorted(
            rendered, key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True)
        )
    return value


def canonical_sha256(payload: Any) -> str:
    rendered = json.dumps(
        _canonical(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _raw_facts_payload(facts: CustomerPriceTypeFacts) -> dict[str, Any]:
    payload = asdict(facts)
    return payload


def build_source_fingerprint(
    facts: list[CustomerPriceTypeFacts] | tuple[CustomerPriceTypeFacts, ...],
    *,
    source_statuses: dict[str, str],
) -> str:
    rows = sorted(
        (
            normalize_counterparty_ref(item.counterparty_ref),
            canonical_sha256(_raw_facts_payload(item)),
        )
        for item in facts
    )
    months = sorted({_month_key(item.snapshot_month) for item in facts})
    windows = [
        {
            "snapshot_month": month,
            "window_start": _month_key(_add_months(date.fromisoformat(f"{month}-01"), -2)),
            "window_end_exclusive": _month_key(_add_months(date.fromisoformat(f"{month}-01"), 1)),
        }
        for month in months
    ]
    return canonical_sha256(
        {"windows": windows, "source_statuses": source_statuses, "raw_fact_hashes": rows}
    )


def build_default_run_key(
    *, snapshot_month: date, ruleset_version: str, source_fingerprint: str
) -> str:
    return (
        f"customer-price-types:{_month_key(snapshot_month)}:"
        f"{ruleset_version}:{source_fingerprint}"
    )


class CustomerPriceTypeRulesEngine:
    def __init__(self, ruleset: PriceTypeRuleset) -> None:
        self.ruleset = ruleset

    def _price_type(self, value: str | None) -> tuple[str | None, str | None]:
        raw = " ".join(str(value or "").split())
        lowered = raw.casefold()
        for prefix in self.ruleset.retail_prefixes:
            if lowered.startswith(prefix.casefold()):
                return "retail", self._variant(raw)
        for prefix in self.ruleset.key_account_prefixes:
            if lowered.startswith(prefix.casefold()):
                return "key_account", self._variant(raw)
        for level in self.ruleset.levels:
            if lowered.startswith(level.price_type_prefix.casefold()):
                return level.key, self._variant(raw)
        return None, self._variant(raw)

    def _variant(self, value: str) -> str | None:
        lowered = value.casefold()
        for variant in self.ruleset.variants:
            if variant.casefold() in lowered:
                return variant.casefold()
        return None

    def _canonical_price_type(self, level_key: str) -> str:
        if level_key == "retail":
            return self.ruleset.retail_prefixes[0]
        if level_key == "key_account":
            return self.ruleset.key_account_prefixes[0]
        return next(
            level.price_type_prefix for level in self.ruleset.levels if level.key == level_key
        )

    def _manual_override(self, facts: CustomerPriceTypeFacts) -> ManualOverride | None:
        month = _month_key(facts.snapshot_month)
        normalized_ref = normalize_counterparty_ref(facts.counterparty_ref)
        for item in self.ruleset.manual_overrides:
            if item.counterparty_ref and item.counterparty_ref != normalized_ref:
                continue
            if (
                item.counterparty_code
                and item.counterparty_code.casefold()
                != str(facts.counterparty_code or "").strip().casefold()
            ):
                continue
            if item.through_snapshot_month and month > item.through_snapshot_month:
                continue
            return item
        return None

    def _registry_override(self, facts: CustomerPriceTypeFacts) -> RegistryOverride | None:
        normalized_ref = normalize_counterparty_ref(facts.counterparty_ref)
        code = str(facts.counterparty_code or "").strip().casefold()
        for item in self.ruleset.registry_overrides:
            if item.counterparty_ref and item.counterparty_ref != normalized_ref:
                continue
            if item.counterparty_code and item.counterparty_code.casefold() != code:
                continue
            return item
        return None

    def _window_values(self, facts: CustomerPriceTypeFacts) -> tuple[list[Decimal], Decimal]:
        keys = [_month_key(_add_months(facts.snapshot_month, delta)) for delta in (-2, -1, 0)]
        values = [max(Decimal("0"), _decimal(facts.monthly_sales.get(key, 0))) for key in keys]
        return values, sum(values, Decimal("0"))

    def _consecutive_zero_months(self, facts: CustomerPriceTypeFacts) -> int:
        months = min(facts.history_coverage_months, self.ruleset.dead_after_months)
        count = 0
        for offset in range(months):
            key = _month_key(_add_months(facts.snapshot_month, -offset))
            if max(Decimal("0"), _decimal(facts.monthly_sales.get(key, 0))) > 0:
                break
            count += 1
        return count

    def _source_problem(self, facts: CustomerPriceTypeFacts) -> tuple[str, ...]:
        return tuple(
            f"source_{source}_{facts.source_statuses.get(source, 'missing')}"
            for source in self.ruleset.required_sources
            if facts.source_statuses.get(source, "missing") != "ready"
        )

    def _source_mismatch(self, facts: CustomerPriceTypeFacts) -> bool:
        if facts.direct_onec_total_3m is None or facts.ledger_total_3m is None:
            return False
        left = abs(facts.direct_onec_total_3m)
        right = abs(facts.ledger_total_3m)
        denominator = max(left, right)
        if denominator == 0:
            return False
        return abs(left - right) / denominator * Decimal("100") > self.ruleset.mismatch_max_pct

    def _decision(
        self,
        facts: CustomerPriceTypeFacts,
        *,
        source_status: str,
        current_level: str | None,
        current_price_type: str | None,
        price_type_variant: str | None,
        recommendation: str,
        recommended_price_type: str | None,
        reason: str,
        action_required: bool,
        case_type: str | None,
        review_type: str | None,
        reasons: tuple[str, ...] = (),
        stop_factors: tuple[str, ...] = (),
        excluded: bool = False,
    ) -> CustomerPriceTypeDecision:
        values, total = self._window_values(facts)
        decision = CustomerPriceTypeDecision(
            source_status=source_status,
            current_level=current_level,
            current_price_type=current_price_type,
            price_type_variant=price_type_variant,
            recommendation=recommendation,
            recommended_price_type=recommended_price_type,
            recommendation_reason=reason,
            action_required=action_required,
            case_type=case_type,
            review_type=review_type,
            reasons=reasons or (reason,),
            stop_factors=stop_factors,
            total_3m=total,
            last_month=values[-1],
            consecutive_zero_months=self._consecutive_zero_months(facts),
            registry_class=facts.service_class,
            is_hygiene=str(facts.service_class or "").strip().casefold()
            in {item.casefold() for item in self.ruleset.hygiene_registry_classes},
            excluded=excluded,
        )
        snapshot_hash = canonical_sha256(
            {
                "facts": _raw_facts_payload(facts),
                "result": asdict(decision),
                "ruleset_version": self.ruleset.version,
            }
        )
        return replace(decision, snapshot_hash=snapshot_hash)

    def evaluate(self, facts: CustomerPriceTypeFacts) -> CustomerPriceTypeDecision:
        normalized_ref = normalize_counterparty_ref(facts.counterparty_ref)
        if normalized_ref != facts.counterparty_ref:
            facts = replace(facts, counterparty_ref=normalized_ref)

        registry_override = self._registry_override(facts)
        if registry_override is not None:
            registry_flag = f"registry:{registry_override.registry_class}"
            facts = replace(
                facts,
                service_class=registry_override.registry_class,
                master_data_flags=tuple(sorted({*facts.master_data_flags, registry_flag})),
            )

        service_class = str(facts.service_class or "").strip().casefold()
        if service_class in {item.casefold() for item in self.ruleset.excluded_registry_classes}:
            return self._decision(
                facts,
                source_status="excluded",
                current_level=None,
                current_price_type=None,
                price_type_variant=None,
                recommendation="excluded_service_card",
                recommended_price_type=None,
                reason="Контрагент исключён утверждённым реестром служебных карточек.",
                action_required=False,
                case_type=None,
                review_type=None,
                stop_factors=("service_card",),
                excluded=True,
            )

        contracts = facts.contracts
        if not contracts:
            return self._data_check(facts, "active_contract_missing", None, None, None)
        if any(contract.price_type_missing for contract in contracts):
            return self._data_check(facts, "price_type_missing", None, None, None)
        if any(contract.price_type_marked for contract in contracts):
            return self._data_check(facts, "price_type_marked", None, None, None)

        parsed_types = tuple(
            (contract.price_type_name, *self._price_type(contract.price_type_name))
            for contract in contracts
        )
        if any(level is None for _, level, _ in parsed_types):
            return self._data_check(facts, "unknown_price_type", None, None, None)

        levels = {level for _, level, _ in parsed_types}
        if len(levels) != 1:
            return self._data_check(facts, "conflicting_price_levels", None, None, None)

        current_level = next(iter(levels))
        normalized_types = {
            " ".join(str(raw_type or "").split()).casefold() for raw_type, _, _ in parsed_types
        }
        current_price_type = (
            " ".join(str(parsed_types[0][0] or "").split())
            if len(normalized_types) == 1
            else self._canonical_price_type(current_level)
        )
        variants = {variant for _, _, variant in parsed_types}
        variant = next(iter(variants)) if len(variants) == 1 else None
        if facts.duplicate_flag:
            return self._data_check(
                facts, "duplicate_counterparty", current_level, current_price_type, variant
            )

        override = self._manual_override(facts)
        if override is not None:
            return self._decision(
                facts,
                source_status="ready",
                current_level=current_level,
                current_price_type=current_price_type,
                price_type_variant=variant,
                recommendation=f"manual_override:{override.override_type}",
                recommended_price_type=override.target_price_type,
                reason=override.reason,
                action_required=override.action_required,
                case_type="special_review" if override.action_required else None,
                review_type=override.review_type,
                stop_factors=("manual_override",),
            )

        source_problems = self._source_problem(facts)
        if source_problems:
            return self._data_check(
                facts,
                "partial_source",
                current_level,
                current_price_type,
                variant,
                stop_factors=source_problems,
            )
        if self._source_mismatch(facts):
            return self._data_check(
                facts,
                "source_mismatch",
                current_level,
                current_price_type,
                variant,
                stop_factors=("source_conflict",),
            )
        if facts.history_coverage_months < 3:
            return self._decision(
                facts,
                source_status="ready",
                current_level=current_level,
                current_price_type=current_price_type,
                price_type_variant=variant,
                recommendation="insufficient_history",
                recommended_price_type=current_price_type,
                reason="Недостаточно полных закрытых месяцев для решения.",
                action_required=False,
                case_type=None,
                review_type=None,
                stop_factors=("insufficient_history",),
            )
        if facts.first_activity_date and facts.first_activity_date > facts.snapshot_month:
            return self._decision(
                facts,
                source_status="ready",
                current_level=current_level,
                current_price_type=current_price_type,
                price_type_variant=variant,
                recommendation="new_client",
                recommended_price_type=current_price_type,
                reason="У клиента ещё нет полного календарного месяца поведения.",
                action_required=False,
                case_type=None,
                review_type=None,
                stop_factors=("new_client",),
            )

        values, total = self._window_values(facts)
        zero_months = self._consecutive_zero_months(facts)
        if current_level == "retail":
            upgrade_candidate = total >= self.ruleset.levels[0].retention_norm_3m
            return self._decision(
                facts,
                source_status="ready",
                current_level=current_level,
                current_price_type=current_price_type,
                price_type_variant=variant,
                recommendation=(
                    "informational_upgrade_candidate" if upgrade_candidate else "keep_current"
                ),
                recommended_price_type=current_price_type,
                reason=(
                    "Кандидат на B2B-квалификацию; повышения в v1 заморожены."
                    if upgrade_candidate
                    else "Розничный тип цены сохраняется."
                ),
                action_required=False,
                case_type=None,
                review_type=None,
                stop_factors=(("upgrade_freeze",) if upgrade_candidate else ()),
            )

        if facts.return_review_type == "quality":
            return self._decision(
                facts,
                source_status="ready",
                current_level=current_level,
                current_price_type=current_price_type,
                price_type_variant=variant,
                recommendation="special_review",
                recommended_price_type=current_price_type,
                reason="Сверхнормативный возвратный сигнал требует ручной проверки качества.",
                action_required=True,
                case_type="special_review",
                review_type="quality",
                stop_factors=("returns_advisory_only",),
            )
        if facts.return_review_type == "data_check":
            return self._data_check(
                facts,
                "return_period_mismatch",
                current_level,
                current_price_type,
                variant,
            )
        if current_level == "key_account":
            return self._decision(
                facts,
                source_status="ready",
                current_level=current_level,
                current_price_type=current_price_type,
                price_type_variant=variant,
                recommendation="keep_current",
                recommended_price_type=current_price_type,
                reason="Key Account не имеет числового порога и сохраняется без автоматических действий.",
                action_required=False,
                case_type=None,
                review_type=None,
                stop_factors=("key_account_no_numeric_threshold",),
            )

        level = self.ruleset.levels_by_key[current_level]
        level_index = next(
            index for index, item in enumerate(self.ruleset.levels) if item.key == current_level
        )
        if level_index + 1 < len(self.ruleset.levels):
            next_level = self.ruleset.levels[level_index + 1]
            if total >= next_level.retention_norm_3m:
                return self._decision(
                    facts,
                    source_status="ready",
                    current_level=current_level,
                    current_price_type=current_price_type,
                    price_type_variant=variant,
                    recommendation="informational_upgrade_candidate",
                    recommended_price_type=current_price_type,
                    reason=(
                        f"Оборот соответствует уровню {next_level.price_type_prefix}; "
                        "повышения в v1 заморожены."
                    ),
                    action_required=False,
                    case_type=None,
                    review_type=None,
                    stop_factors=("upgrade_freeze",),
                )
        if total >= level.retention_norm_3m:
            return self._decision(
                facts,
                source_status="ready",
                current_level=current_level,
                current_price_type=current_price_type,
                price_type_variant=variant,
                recommendation="keep_current",
                recommended_price_type=current_price_type,
                reason="Норматив трёх полных месяцев выполнен.",
                action_required=False,
                case_type=None,
                review_type=None,
            )
        if facts.key_account_flag:
            return self._decision(
                facts,
                source_status="ready",
                current_level=current_level,
                current_price_type=current_price_type,
                price_type_variant=variant,
                recommendation="special_review",
                recommended_price_type=current_price_type,
                reason="Key Account пересматривается только личным решением руководителя.",
                action_required=True,
                case_type="special_review",
                review_type="key_account",
                stop_factors=("key_account",),
            )
        if zero_months >= self.ruleset.dead_after_months:
            if facts.economics_status not in {"ok", "ready"}:
                return self._data_check(
                    facts,
                    "economics_missing",
                    current_level,
                    current_price_type,
                    variant,
                    stop_factors=("economics_required",),
                )
            return self._decision(
                facts,
                source_status="ready",
                current_level=current_level,
                current_price_type=current_price_type,
                price_type_variant=variant,
                recommendation="downgrade_to_retail",
                recommended_price_type=self.ruleset.retail_prefixes[0],
                reason="Продаж нет 12 и более месяцев; нужна ручная проверка перед розницей.",
                action_required=True,
                case_type="recovery",
                review_type="dead_soul",
                stop_factors=("human_approval_required",),
            )
        if zero_months >= self.ruleset.zero_months_to_recovery:
            return self._decision(
                facts,
                source_status="ready",
                current_level=current_level,
                current_price_type=current_price_type,
                price_type_variant=variant,
                recommendation="recovery",
                recommended_price_type=current_price_type,
                reason="Три месяца без продаж требуют CRM-реанимации.",
                action_required=True,
                case_type="recovery",
                review_type="history",
                stop_factors=("human_approval_required",),
            )
        if values[-1] >= level.hold_last_month:
            return self._decision(
                facts,
                source_status="ready",
                current_level=current_level,
                current_price_type=current_price_type,
                price_type_variant=variant,
                recommendation="manager_retention",
                recommended_price_type=current_price_type,
                reason="Итог ниже нормы, но последний месяц достиг порога удержания.",
                action_required=True,
                case_type="manager_work",
                review_type="retention",
            )
        if facts.economics_status not in {"ok", "ready"}:
            return self._data_check(
                facts,
                "economics_missing",
                current_level,
                current_price_type,
                variant,
                stop_factors=("economics_required",),
            )
        return self._decision(
            facts,
            source_status="ready",
            current_level=current_level,
            current_price_type=current_price_type,
            price_type_variant=variant,
            recommendation="isolate",
            recommended_price_type=level.downgrade_to,
            reason="Итог и последний месяц ниже порогов; требуется полный месяц изолятора.",
            action_required=True,
            case_type="isolate",
            review_type="economics" if facts.economics else "retention",
            stop_factors=("isolation_required", "human_approval_required"),
        )

    def _data_check(
        self,
        facts: CustomerPriceTypeFacts,
        reason_code: str,
        current_level: str | None,
        current_price_type: str | None,
        variant: str | None,
        *,
        stop_factors: tuple[str, ...] = (),
    ) -> CustomerPriceTypeDecision:
        return self._decision(
            facts,
            source_status="conflict" if reason_code == "source_mismatch" else "partial",
            current_level=current_level,
            current_price_type=current_price_type,
            price_type_variant=variant,
            recommendation="data_check",
            recommended_price_type=current_price_type,
            reason=f"Требуется сверка данных: {reason_code}.",
            action_required=True,
            case_type="data_check",
            review_type="data",
            reasons=(reason_code,),
            stop_factors=stop_factors or (reason_code,),
        )
