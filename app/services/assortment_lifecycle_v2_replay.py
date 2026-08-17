from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Sequence

from app.services.assortment_lifecycle import (
    AssortmentLifecycleInput,
    decide_target_assortment_status,
)
from app.services.assortment_lifecycle_v2_policy import (
    DEFAULT_DEMAND_STATE_POLICY,
    DemandStatePolicy,
)

V2_REPLAY_MODEL_VERSION = "assortment-lifecycle-v2-daily.v2"
ZERO = Decimal("0")


@dataclass(frozen=True)
class HistoricalSaleObservation:
    business_date: date
    quantity: Decimal
    document_id: str = ""
    customer_id: str = ""
    sales_point_id: str = ""


@dataclass(frozen=True)
class HistoricalSupplierOrder:
    created_at: date
    cargo_handoff_at: date | None = None


@dataclass(frozen=True)
class HistoricalReceipt:
    received_at: date


class _SalesHistory:
    def __init__(self, observations: Sequence[HistoricalSaleObservation]) -> None:
        self.observations = tuple(
            sorted(
                (row for row in observations if row.quantity > ZERO),
                key=lambda row: (
                    row.business_date,
                    row.document_id,
                    row.customer_id,
                    row.sales_point_id,
                ),
            )
        )
        self.dates = tuple(row.business_date for row in self.observations)
        prefix = [ZERO]
        for row in self.observations:
            prefix.append(prefix[-1] + row.quantity)
        self.prefix = tuple(prefix)

    def window(self, *, as_of: date, days: int) -> tuple[HistoricalSaleObservation, ...]:
        start = as_of - timedelta(days=days - 1)
        left = bisect_left(self.dates, start)
        right = bisect_right(self.dates, as_of)
        return self.observations[left:right]

    def quantity(self, *, as_of: date, days: int) -> Decimal:
        start = as_of - timedelta(days=days - 1)
        left = bisect_left(self.dates, start)
        right = bisect_right(self.dates, as_of)
        return self.prefix[right] - self.prefix[left]

    def first_sale_at(self, *, as_of: date) -> date | None:
        right = bisect_right(self.dates, as_of)
        return self.dates[0] if right else None

    def last_sale_at(self, *, as_of: date) -> date | None:
        right = bisect_right(self.dates, as_of)
        return self.dates[right - 1] if right else None


class _AvailabilityHistory:
    def __init__(self, dates: Iterable[date]) -> None:
        self.dates = tuple(sorted(set(dates)))

    def days(self, *, as_of: date, window_days: int) -> int:
        start = as_of - timedelta(days=window_days - 1)
        return bisect_right(self.dates, as_of) - bisect_left(self.dates, start)


def build_assortment_lifecycle_v2_trajectory(
    *,
    items: Sequence[Mapping[str, Any]],
    sales_observations_by_code: Mapping[str, Sequence[HistoricalSaleObservation]],
    availability_by_code: Mapping[str, Iterable[date]],
    supplier_orders_by_code: Mapping[str, Sequence[HistoricalSupplierOrder]],
    receipts_by_code: Mapping[str, Sequence[HistoricalReceipt]],
    history_start: date,
    date_from: date,
    date_to: date,
    demand_policy: DemandStatePolicy = DEFAULT_DEMAND_STATE_POLICY,
) -> list[dict[str, Any]]:
    """Rebuild the target daily trajectory without looking past each date.

    The returned rows are suitable for the immutable replay store.  Product
    attributes may come from the current classification snapshot, but every
    event and every sales/availability window is cut at ``business_date``.
    """

    if history_start > date_from or date_from > date_to:
        raise ValueError("assortment_lifecycle_v2_replay_period_invalid")
    item_by_code = {code: item for item in items if (code := _clean(item.get("nomenclature_code")))}
    sales = {code: _SalesHistory(sales_observations_by_code.get(code, ())) for code in item_by_code}
    availability = {
        code: _AvailabilityHistory(availability_by_code.get(code, ())) for code in item_by_code
    }
    orders = {
        code: tuple(sorted(supplier_orders_by_code.get(code, ()), key=lambda row: row.created_at))
        for code in item_by_code
    }
    receipts = {
        code: tuple(sorted(receipts_by_code.get(code, ()), key=lambda row: row.received_at))
        for code in item_by_code
    }
    previous_status: dict[str, str] = {}
    previous_demand_state: dict[str, str] = {}
    demand_state_since: dict[str, date] = {}
    previous_demand_state_at: dict[str, date] = {}
    rows: list[dict[str, Any]] = []

    for business_date in _daterange(history_start, date_to):
        emit = business_date >= date_from
        for code in sorted(item_by_code):
            item = item_by_code[code]
            if not _item_active_as_of(item, as_of=business_date):
                continue
            lifecycle, evidence = historical_v2_lifecycle_decision(
                item=item,
                sales=sales[code],
                availability=availability[code],
                supplier_orders=orders[code],
                receipts=receipts[code],
                as_of=business_date,
                previous_status=previous_status.get(code),
                previous_demand_state=previous_demand_state.get(code),
                demand_state_since=demand_state_since.get(code),
                previous_demand_state_at=previous_demand_state_at.get(code),
                demand_policy=demand_policy,
            )
            old_status = previous_status.get(code, "")
            previous_status[code] = lifecycle.status.value
            if lifecycle.demand_state is not None:
                state = lifecycle.demand_state.value
                prior_state = previous_demand_state.get(code)
                state_since = (
                    lifecycle.demand_state_since
                    or (demand_state_since.get(code) if prior_state == state else None)
                    or business_date
                )
                previous_demand_state[code] = state
                demand_state_since[code] = state_since
                previous_demand_state_at[code] = business_date
            if not emit:
                continue
            rows.append(
                {
                    "business_date": business_date.isoformat(),
                    "nomenclature_code": code,
                    "name": _clean(item.get("name")),
                    "previous_status": old_status,
                    "status": lifecycle.status.value,
                    "status_label": lifecycle.status_label,
                    "reason_codes": list(lifecycle.reason_codes),
                    "reason_text": lifecycle.reason_text,
                    "auto_order_allowed": lifecycle.auto_order_allowed,
                    "manual_review_required": lifecycle.manual_review_required,
                    "blockers": list(lifecycle.blockers),
                    "recommended_status": (
                        lifecycle.recommended_status.value
                        if lifecycle.recommended_status is not None
                        else None
                    ),
                    "demand_state": (
                        lifecycle.demand_state.value if lifecycle.demand_state is not None else None
                    ),
                    "demand_state_label": lifecycle.demand_state_label,
                    "demand_reason_codes": list(lifecycle.demand_reason_codes),
                    "demand_reason_text": lifecycle.demand_reason_text,
                    "demand_state_since": lifecycle.demand_state_since,
                    **evidence,
                }
            )
    return rows


def historical_v2_lifecycle_decision(
    *,
    item: Mapping[str, Any],
    sales: _SalesHistory,
    availability: _AvailabilityHistory,
    supplier_orders: Sequence[HistoricalSupplierOrder],
    receipts: Sequence[HistoricalReceipt],
    as_of: date,
    previous_status: str | None,
    previous_demand_state: str | None,
    demand_state_since: date | None,
    previous_demand_state_at: date | None,
    demand_policy: DemandStatePolicy,
):
    source = _source_record(item)
    order_dates = [row.created_at for row in supplier_orders if row.created_at <= as_of]
    source_order_at = _date(source.get("first_supplier_order_at"))
    if source_order_at is not None and source_order_at <= as_of:
        order_dates.append(source_order_at)
    cargo_dates = [
        row.cargo_handoff_at
        for row in supplier_orders
        if row.cargo_handoff_at is not None and row.cargo_handoff_at <= as_of
    ]
    cargo_dates.extend(_dated_values(source.get("supplier_order_cargo_handoff_dates"), as_of))
    receipt_dates = [row.received_at for row in receipts if row.received_at <= as_of]
    receipt_dates.extend(_dated_values(source.get("receipt_dates"), as_of))
    source_first_receipt = _date(source.get("first_receipt_at"))
    if source_first_receipt is not None and source_first_receipt <= as_of:
        receipt_dates.append(source_first_receipt)
    source_last_receipt = _date(source.get("last_receipt_at"))
    if source_last_receipt is not None and source_last_receipt <= as_of:
        receipt_dates.append(source_last_receipt)
    source_first_stock_inflow = _date(source.get("first_stock_inflow_at"))
    source_last_stock_inflow = _date(source.get("last_stock_inflow_at"))
    stock_inflow_dates = list(receipt_dates)
    if source_first_stock_inflow is not None and source_first_stock_inflow <= as_of:
        stock_inflow_dates.append(source_first_stock_inflow)
    if source_last_stock_inflow is not None and source_last_stock_inflow <= as_of:
        stock_inflow_dates.append(source_last_stock_inflow)

    first_sale_at = sales.first_sale_at(as_of=as_of)
    last_sale_at = sales.last_sale_at(as_of=as_of)
    source_first_sale = _date(source.get("first_sale_at"))
    if source_first_sale is not None and source_first_sale <= as_of:
        first_sale_at = min(filter(None, (first_sale_at, source_first_sale)), default=None)
    source_last_sale = _date(source.get("last_sale_at"))
    if source_last_sale is not None and source_last_sale <= as_of:
        last_sale_at = max(filter(None, (last_sale_at, source_last_sale)), default=None)

    short_observations = sales.window(as_of=as_of, days=30)
    quantity_by_day: dict[date, Decimal] = {}
    for row in short_observations:
        quantity_by_day[row.business_date] = (
            quantity_by_day.get(row.business_date, ZERO) + row.quantity
        )
    short_total = sum(quantity_by_day.values(), ZERO)
    peak = max(quantity_by_day.values(), default=ZERO)
    distribution_complete = all(
        row.document_id and row.customer_id and row.sales_point_id for row in short_observations
    )
    distribution = {
        "sales_active_days_short": len(quantity_by_day) if distribution_complete else None,
        "sales_document_count_short": (
            len({row.document_id for row in short_observations}) if distribution_complete else None
        ),
        "sales_customer_count_short": (
            len({row.customer_id for row in short_observations}) if distribution_complete else None
        ),
        "sales_point_count_short": (
            len({row.sales_point_id for row in short_observations})
            if distribution_complete
            else None
        ),
        "sales_max_day_share_short": (
            peak / short_total if distribution_complete and short_total > ZERO else None
        ),
    }
    manual_changed_at = _date(source.get("manual_changed_at"))
    manual_status = (
        source.get("manual_status")
        if manual_changed_at is not None and manual_changed_at <= as_of
        else None
    )
    lifecycle = decide_target_assortment_status(
        AssortmentLifecycleInput(
            nomenclature_code=_clean(item.get("nomenclature_code")),
            created_at=_item_created_at(item),
            first_supplier_order_at=min(order_dates) if order_dates else None,
            supplier_order_cargo_handoff_dates=tuple(sorted(set(cargo_dates))),
            receipt_dates=tuple(sorted(set(receipt_dates))),
            first_receipt_at=min(receipt_dates) if receipt_dates else None,
            last_receipt_at=max(receipt_dates) if receipt_dates else None,
            first_stock_inflow_at=min(stock_inflow_dates) if stock_inflow_dates else None,
            last_stock_inflow_at=max(stock_inflow_dates) if stock_inflow_dates else None,
            first_sale_at=first_sale_at,
            last_sale_at=last_sale_at,
            as_of=as_of,
            sales_qty_short=sales.quantity(as_of=as_of, days=30),
            sales_qty_medium=sales.quantity(as_of=as_of, days=90),
            sales_qty_long=sales.quantity(as_of=as_of, days=180),
            days_in_sale_short=availability.days(as_of=as_of, window_days=30),
            days_in_sale_medium=availability.days(as_of=as_of, window_days=90),
            days_in_sale_long=availability.days(as_of=as_of, window_days=180),
            previous_status=previous_status,
            previous_demand_state=previous_demand_state,
            demand_state_since=demand_state_since,
            previous_demand_state_at=previous_demand_state_at,
            manual_status=manual_status,
            manual_reason=_clean(source.get("manual_reason")) if manual_status else "",
            manual_approved_by=(_clean(source.get("manual_approved_by")) if manual_status else ""),
            manual_changed_at=manual_changed_at if manual_status else None,
            **distribution,
        ),
        demand_policy=demand_policy,
    )
    evidence = {
        "sales_30": str(sales.quantity(as_of=as_of, days=30)),
        "sales_90": str(sales.quantity(as_of=as_of, days=90)),
        "sales_180": str(sales.quantity(as_of=as_of, days=180)),
        "available_days_30": availability.days(as_of=as_of, window_days=30),
        "available_days_90": availability.days(as_of=as_of, window_days=90),
        "available_days_180": availability.days(as_of=as_of, window_days=180),
        "sales_active_days_30": distribution["sales_active_days_short"],
        "sales_document_count_30": distribution["sales_document_count_short"],
        "sales_customer_count_30": distribution["sales_customer_count_short"],
        "sales_point_count_30": distribution["sales_point_count_short"],
        "sales_max_day_share_30": distribution["sales_max_day_share_short"],
        "first_sale_at": first_sale_at,
        "last_sale_at": last_sale_at,
        "first_supplier_order_at": min(order_dates) if order_dates else None,
        "first_cargo_at": min(cargo_dates) if cargo_dates else None,
        "first_receipt_at": min(receipt_dates) if receipt_dates else None,
        "last_receipt_at": max(receipt_dates) if receipt_dates else None,
        "first_stock_inflow_at": min(stock_inflow_dates) if stock_inflow_dates else None,
        "last_stock_inflow_at": max(stock_inflow_dates) if stock_inflow_dates else None,
        "history_age_days": ((as_of - min(receipt_dates)).days if receipt_dates else None),
        "historical_manual_status_replayed": bool(manual_status),
    }
    return lifecycle, evidence


def sales_observations_from_facts(
    facts: Sequence[Mapping[str, Any]],
) -> dict[str, list[HistoricalSaleObservation]]:
    codes_with_detail = {
        _clean(fact.get("nomenclature_code"))
        for fact in facts
        if _clean(fact.get("fact_type")) == "sale_observation"
        and _clean(fact.get("nomenclature_code"))
    }
    result: dict[str, list[HistoricalSaleObservation]] = {}
    for fact in facts:
        fact_type = _clean(fact.get("fact_type"))
        if fact_type not in {"sale", "sale_observation"}:
            continue
        code = _clean(fact.get("nomenclature_code"))
        if fact_type == "sale" and code in codes_with_detail:
            continue
        business_date = _date(fact.get("business_date"))
        payload = fact.get("payload")
        if not code or business_date is None or not isinstance(payload, Mapping):
            continue
        quantity = _decimal(payload.get("quantity"))
        if quantity <= ZERO:
            continue
        result.setdefault(code, []).append(
            HistoricalSaleObservation(
                business_date=business_date,
                quantity=quantity,
                document_id=_clean(payload.get("document_id")),
                customer_id=_clean(payload.get("customer_id")),
                sales_point_id=_clean(payload.get("sales_point_id")),
            )
        )
    return result


def _daterange(date_from: date, date_to: date):
    cursor = date_from
    while cursor <= date_to:
        yield cursor
        cursor += timedelta(days=1)


def _source_record(item: Mapping[str, Any]) -> Mapping[str, Any]:
    value = item.get("source_record")
    if isinstance(value, Mapping):
        return value
    return item


def _item_created_at(item: Mapping[str, Any]) -> date | None:
    source = _source_record(item)
    return min(
        filter(
            None,
            (
                _date(source.get("first_supplier_order_at")),
                _date(source.get("created_at")),
                _date(source.get("card_created_at")),
                _date(source.get("onec_novelty_date")),
                _date(item.get("created_at")),
            ),
        ),
        default=None,
    )


def _item_active_as_of(item: Mapping[str, Any], *, as_of: date) -> bool:
    created_at = _item_created_at(item)
    return created_at is None or created_at <= as_of


def _dated_values(value: Any, as_of: date) -> list[date]:
    values = value if isinstance(value, (list, tuple, set)) else (value,)
    return [
        parsed
        for raw in values
        if (parsed := _date(raw)) is not None and date(2000, 1, 1) <= parsed <= as_of
    ]


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    rendered = _clean(value)
    if not rendered:
        return None
    try:
        return date.fromisoformat(rendered[:10])
    except ValueError:
        return None


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(_clean(value) or "0")
    except (InvalidOperation, ValueError):
        return ZERO
