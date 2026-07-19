"""Pure domain surface for customer price-type management."""

from .entities import (
    ContractFact,
    CustomerPriceTypeAccessScope,
    CustomerPriceTypeDecision,
    CustomerPriceTypeFacts,
    LevelRule,
    ManualOverride,
    PriceTypeRuleset,
    RegistryOverride,
)
from .rules import (
    CustomerPriceTypeRulesEngine,
    build_default_run_key,
    build_source_fingerprint,
    canonical_sha256,
    load_price_type_ruleset,
    normalize_counterparty_ref,
    proven_history_coverage_months,
)

__all__ = [
    "ContractFact",
    "CustomerPriceTypeAccessScope",
    "CustomerPriceTypeDecision",
    "CustomerPriceTypeFacts",
    "CustomerPriceTypeRulesEngine",
    "LevelRule",
    "ManualOverride",
    "PriceTypeRuleset",
    "RegistryOverride",
    "build_default_run_key",
    "build_source_fingerprint",
    "canonical_sha256",
    "load_price_type_ruleset",
    "normalize_counterparty_ref",
    "proven_history_coverage_months",
]
