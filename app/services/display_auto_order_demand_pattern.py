"""Look-ahead-free demand-pattern classification for display auto-order analysis."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from statistics import pvariance
from typing import Mapping, Sequence

ZERO = Decimal("0")
WEEK_DAYS = 7
DEMAND_HISTORY_WEEKS = 52
ADI_THRESHOLD = Decimal("1.32")
CV2_THRESHOLD = Decimal("0.49")


@dataclass(frozen=True)
class DemandPattern:
    name: str
    adi: Decimal | None
    cv2: Decimal | None
    positive_weeks: int
    mean_weekly_demand: Decimal


def sum_dates(
    sales_by_day: Mapping[date, Decimal],
    start: date,
    end_exclusive: date,
) -> Decimal:
    return sum(
        (
            max(ZERO, quantity)
            for business_date, quantity in sales_by_day.items()
            if start <= business_date < end_exclusive
        ),
        ZERO,
    )


def completed_weekly_demand(
    sales_by_day: Mapping[date, Decimal],
    *,
    as_of: date,
    weeks: int = DEMAND_HISTORY_WEEKS,
) -> list[Decimal]:
    """Return only completed weekly buckets ending before ``as_of``."""

    start = as_of - timedelta(days=weeks * WEEK_DAYS)
    return [
        sum_dates(
            sales_by_day,
            start + timedelta(days=index * WEEK_DAYS),
            start + timedelta(days=(index + 1) * WEEK_DAYS),
        )
        for index in range(weeks)
    ]


def classify_demand_pattern(weekly_demand: Sequence[Decimal]) -> DemandPattern:
    values = [max(ZERO, Decimal(value)) for value in weekly_demand]
    positive = [value for value in values if value > ZERO]
    mean_weekly = sum(values, ZERO) / Decimal(len(values)) if values else ZERO
    if not positive:
        return DemandPattern("no_history", None, None, 0, mean_weekly)
    adi = Decimal(len(values)) / Decimal(len(positive))
    if len(positive) < 2:
        return DemandPattern("insufficient_history", adi, None, len(positive), mean_weekly)
    positive_mean = sum(positive, ZERO) / Decimal(len(positive))
    cv2 = (
        Decimal(str(pvariance([float(value) for value in positive])))
        / (positive_mean * positive_mean)
        if positive_mean > ZERO
        else ZERO
    )
    if adi < ADI_THRESHOLD and cv2 < CV2_THRESHOLD:
        name = "smooth"
    elif adi >= ADI_THRESHOLD and cv2 < CV2_THRESHOLD:
        name = "intermittent"
    elif adi < ADI_THRESHOLD and cv2 >= CV2_THRESHOLD:
        name = "erratic"
    else:
        name = "lumpy"
    return DemandPattern(name, adi, cv2, len(positive), mean_weekly)
