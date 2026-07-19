"""Ports exposed by the customer price-type domain."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Protocol

from .entities import CustomerPriceTypeDecision, CustomerPriceTypeFacts


class CustomerPriceTypeFactSource(Protocol):
    def collect(self, *, snapshot_month: date) -> Sequence[CustomerPriceTypeFacts]: ...


class CustomerPriceTypeDecisionSink(Protocol):
    def persist(
        self,
        *,
        facts: Sequence[CustomerPriceTypeFacts],
        decisions: Sequence[CustomerPriceTypeDecision],
        run_key: str,
        source_fingerprint: str,
    ) -> int: ...
