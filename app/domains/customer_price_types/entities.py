"""Framework-free entities used by the customer price-type rules engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any


@dataclass(frozen=True, slots=True)
class LevelRule:
    key: str
    price_type_prefix: str
    retention_norm_3m: Decimal
    hold_last_month: Decimal
    downgrade_to: str


@dataclass(frozen=True, slots=True)
class ManualOverride:
    counterparty_code: str | None
    counterparty_ref: str | None
    override_type: str
    reason: str
    action_required: bool
    review_type: str | None = None
    target_price_type: str | None = None
    through_snapshot_month: str | None = None


@dataclass(frozen=True, slots=True)
class RegistryOverride:
    counterparty_code: str | None
    counterparty_ref: str | None
    registry_class: str


@dataclass(frozen=True, slots=True)
class PriceTypeRuleset:
    version: str
    effective_date: date
    retail_prefixes: tuple[str, ...]
    key_account_prefixes: tuple[str, ...]
    variants: tuple[str, ...]
    levels: tuple[LevelRule, ...]
    upgrades_frozen: bool
    zero_months_to_recovery: int
    dead_after_months: int
    mismatch_max_pct: Decimal
    excluded_registry_classes: frozenset[str]
    hygiene_registry_classes: frozenset[str]
    required_sources: tuple[str, ...]
    manual_overrides: tuple[ManualOverride, ...]
    registry_overrides: tuple[RegistryOverride, ...]

    @property
    def levels_by_key(self) -> dict[str, LevelRule]:
        return {level.key: level for level in self.levels}


@dataclass(frozen=True, slots=True)
class ContractFact:
    contract_ref: str | None
    contract_name: str | None
    price_type_name: str | None
    price_type_marked: bool = False
    price_type_missing: bool = False


@dataclass(frozen=True, slots=True)
class CustomerPriceTypeFacts:
    counterparty_ref: str
    counterparty_code: str | None
    counterparty_name: str | None
    snapshot_month: date
    contracts: tuple[ContractFact, ...]
    monthly_sales: dict[str, Decimal]
    source_statuses: dict[str, str] = field(default_factory=dict)
    department_ref: str | None = None
    department_name: str | None = None
    owner_ref: str | None = None
    owner_name: str | None = None
    service_class: str | None = None
    duplicate_flag: bool = False
    key_account_flag: bool = False
    first_activity_date: date | None = None
    history_coverage_months: int = 0
    direct_onec_total_3m: Decimal | None = None
    ledger_total_3m: Decimal | None = None
    economics_status: str | None = None
    economics: dict[str, Any] = field(default_factory=dict)
    payments: dict[str, Any] = field(default_factory=dict)
    returns: dict[str, Any] = field(default_factory=dict)
    return_review_type: str | None = None
    master_data_flags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CustomerPriceTypeDecision:
    source_status: str
    current_level: str | None
    current_price_type: str | None
    price_type_variant: str | None
    recommendation: str
    recommended_price_type: str | None
    recommendation_reason: str
    action_required: bool
    case_type: str | None
    review_type: str | None
    reasons: tuple[str, ...]
    stop_factors: tuple[str, ...]
    total_3m: Decimal
    last_month: Decimal
    consecutive_zero_months: int
    registry_class: str | None = None
    is_hygiene: bool = False
    excluded: bool = False
    snapshot_hash: str = ""


@dataclass(frozen=True, slots=True)
class CustomerPriceTypeAccessScope:
    actor: str
    role: str
    owner_ref: str | None = None
    department_refs: tuple[str, ...] = ()
    can_view_money: bool = False

    @property
    def is_full(self) -> bool:
        return self.role in {"executive", "network_head", "internal"}
