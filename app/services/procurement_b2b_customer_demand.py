from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

PROFILE_DATE_RE = re.compile(r"(20\d{2}-\d{2}-\d{2})(?=\D*$)")


@dataclass(frozen=True)
class B2BCustomerDemandComponent:
    counterparty_ref: str
    activity_status: str
    expected_purchase_date: date | None
    daily_rate: Decimal

    @property
    def active(self) -> bool:
        return self.activity_status == "Активный"


@dataclass(frozen=True)
class B2BSkuDemandProfile:
    nomenclature_code: str
    profile_as_of_exclusive: date
    managed_sales_qty_180: Decimal
    managed_sales_qty_270: Decimal
    dependency_class: str
    active_high_tier_share_pct: Decimal
    components: tuple[B2BCustomerDemandComponent, ...]

    @property
    def active_customer_count(self) -> int:
        return len(
            {component.counterparty_ref for component in self.components if component.active}
        )

    @property
    def passive_customer_count(self) -> int:
        return len(
            {
                component.counterparty_ref
                for component in self.components
                if component.activity_status == "Пассивный"
            }
        )

    def due_components(
        self,
        *,
        as_of: date,
        horizon_days: int,
    ) -> tuple[B2BCustomerDemandComponent, ...]:
        horizon_end = as_of + timedelta(days=max(0, horizon_days))
        return tuple(
            component
            for component in self.components
            if component.active
            and (
                component.expected_purchase_date is None
                or component.expected_purchase_date <= horizon_end
            )
        )

    def due_customer_count(self, *, as_of: date, horizon_days: int) -> int:
        return len(
            {
                component.counterparty_ref
                for component in self.due_components(
                    as_of=as_of,
                    horizon_days=horizon_days,
                )
            }
        )

    def active_daily_rate_due(self, *, as_of: date, horizon_days: int) -> Decimal:
        return sum(
            (
                component.daily_rate
                for component in self.due_components(
                    as_of=as_of,
                    horizon_days=horizon_days,
                )
            ),
            Decimal("0"),
        )


def load_b2b_customer_demand_profiles(
    path: Path,
    *,
    profile_as_of_exclusive: date | None = None,
) -> dict[str, B2BSkuDemandProfile]:
    if profile_as_of_exclusive is None:
        profile_as_of_exclusive = infer_profile_as_of_exclusive(path)
    grouped: dict[str, list[dict[str, str]]] = {}
    with path.open(encoding="utf-8-sig", newline="") as source:
        for row in csv.DictReader(source):
            code = _clean(row.get("sku"))
            if not code:
                continue
            grouped.setdefault(code, []).append(dict(row))

    profiles: dict[str, B2BSkuDemandProfile] = {}
    for code, rows in grouped.items():
        components = tuple(
            B2BCustomerDemandComponent(
                counterparty_ref=_clean(row.get("counterparty_ref")),
                activity_status=_clean(row.get("activity_status")),
                expected_purchase_date=_date_value(row.get("expected_customer_purchase_date")),
                daily_rate=max(
                    Decimal("0"),
                    _decimal(row.get("recency_weighted_daily_rate")),
                ),
            )
            for row in rows
            if _clean(row.get("counterparty_ref"))
        )
        profiles[code] = B2BSkuDemandProfile(
            nomenclature_code=code,
            profile_as_of_exclusive=profile_as_of_exclusive,
            managed_sales_qty_180=sum(
                (
                    _decimal(row.get("units_recent_90")) + _decimal(row.get("units_previous_90"))
                    for row in rows
                    if _clean(row.get("activity_status")) in {"Активный", "Пассивный"}
                ),
                Decimal("0"),
            ),
            managed_sales_qty_270=sum(
                (
                    _decimal(row.get("units_270"))
                    for row in rows
                    if _clean(row.get("activity_status")) in {"Активный", "Пассивный"}
                ),
                Decimal("0"),
            ),
            dependency_class=_first_nonempty(row.get("dependency_reading") for row in rows),
            active_high_tier_share_pct=max(
                (_decimal(row.get("active_high_tier_share_pct")) for row in rows),
                default=Decimal("0"),
            ),
            components=components,
        )
    return profiles


def infer_profile_as_of_exclusive(path: Path) -> date:
    match = PROFILE_DATE_RE.search(path.stem)
    if match is None:
        raise ValueError(
            "B2B customer demand profile date is missing; pass profile_as_of_exclusive"
        )
    return date.fromisoformat(match.group(1))


def _first_nonempty(values: Iterable[Any]) -> str:
    for value in values:
        cleaned = _clean(value)
        if cleaned:
            return cleaned
    return ""


def _decimal(value: Any) -> Decimal:
    raw = _clean(value).replace(" ", "").replace(",", ".")
    if not raw:
        return Decimal("0")
    try:
        return Decimal(raw)
    except InvalidOperation:
        return Decimal("0")


def _date_value(value: Any) -> date | None:
    raw = _clean(value)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).date()
    except ValueError:
        try:
            return date.fromisoformat(raw)
        except ValueError:
            return None


def _clean(value: Any) -> str:
    return str(value or "").strip()
