from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel


class BIProduct(BaseModel):
    article: str
    fact_sku: str | None = None
    planned_sku: str | None = None
    sku_sync_status: str | None = None
    code_1c: str | None = None
    info_system_code: str | None = None
    name: str
    brand: str | None = None
    category: str | None = None
    is_active: bool
    is_marked_for_deletion: bool
    stock_quantity: int | None = None
    purchase_price: float | None = None


class BIRecommendation(BaseModel):
    article: str
    recommended_price: Decimal
    floor_price: Decimal
    competitor_min_price: Decimal | None = None
    min_margin_pct: Decimal
    strategy_name: str | None = None
    created_at: datetime
    reasons: list[str]


class BICompetitorPrice(BaseModel):
    article: str
    competitor: str
    price: Decimal
    in_stock: bool
    collected_at: datetime


class BIPhoneModelLink(BaseModel):
    phone_model_id: int
    brand: str
    model_name: str
    variant: str | None = None
    product_article: str | None = None
    product_name: str | None = None
    competitor: str | None = None
    competitor_sku: str | None = None
    competitor_name: str | None = None


class BIUnresolvedCompatibility(BaseModel):
    source: str
    entity_type: str
    entity_id: int
    raw_value: str
    brand: str | None = None
    model_name: str | None = None
    variant: str | None = None
    notes: str | None = None


class BICanonicalizationSummary(BaseModel):
    phone_models: int
    aliases: int
    product_links: int
    competitor_links: int
    unresolved_product_compatibilities: int
    filtered_non_phone_product_compatibilities: int | None = None
    unresolved_competitor_compatibilities: int
    valid_canonical_links: int | None = None
    review_candidates: int | None = None
    blocked_noise: int | None = None


class BIReceivableCurrent(BaseModel):
    snapshot_date: date
    counterparty_ref: str
    counterparty_name: str | None = None
    current_balance: Decimal
    aged_bucket: str
    activity_segment: str
    is_overdue: bool
    overdue_days: int | None = None
    due_date: datetime | None = None
    planned_payment_date: datetime | None = None
    credit_depth_days: int | None = None
    payment_term_source: str | None = None
    shipment_ban: bool | None = None
    origin_document_ref: str | None = None
    origin_document_number: str | None = None
    origin_document_date: datetime | None = None
    origin_manager_ref: str | None = None
    origin_manager_name: str | None = None
    current_manager_ref: str | None = None
    current_manager_name: str | None = None
    department_ref: str | None = None
    department_name: str | None = None
    last_sale_at: datetime | None = None
    last_payment_at: datetime | None = None


class BIReceivableCase(BaseModel):
    snapshot_date: date
    segment: str
    owner_type: str
    recommendation: str
    counterparty_ref: str
    counterparty_name: str | None = None
    current_balance: Decimal
    aged_bucket: str
    activity_segment: str
    is_overdue: bool
    overdue_days: int | None = None
    due_date: datetime | None = None
    planned_payment_date: datetime | None = None
    credit_depth_days: int | None = None
    payment_term_source: str | None = None
    shipment_ban: bool | None = None
    origin_document_ref: str | None = None
    origin_document_number: str | None = None
    origin_document_date: datetime | None = None
    origin_manager_ref: str | None = None
    origin_manager_name: str | None = None
    current_manager_ref: str | None = None
    current_manager_name: str | None = None
    department_ref: str | None = None
    department_name: str | None = None


class BIReceivablesManagerSummary(BaseModel):
    snapshot_date: date
    manager_ref: str | None = None
    manager_name: str | None = None
    counterparty_count: int
    total_balance: Decimal
    new_daily_count: int
    inactive_count: int
    employee_count: int
    fired_manager_count: int
    adjustment_candidates_count: int


class BIReceivableContractBalance(BaseModel):
    snapshot_date: date
    counterparty_ref: str
    counterparty_name: str | None = None
    contract_ref: str | None = None
    contract_name: str | None = None
    contract_kind_ref: str | None = None
    contract_kind_name: str | None = None
    source_layer: str
    current_balance: Decimal
    event_count: int
    last_event_at: datetime


class BIDailySalesKPI(BaseModel):
    sales_date: date
    manager_ref: str | None = None
    manager_name: str | None = None
    store_ref: str | None = None
    store_name: str | None = None
    revenue: Decimal
    sales_count: Decimal
    cost_of_sales: Decimal
    gross_profit: Decimal
    margin_pct: Decimal | None = None
    profitability_pct: Decimal | None = None


class BIWeeklySalesKPI(BaseModel):
    week_start: date
    week_end: date
    manager_ref: str | None = None
    manager_name: str | None = None
    store_ref: str | None = None
    store_name: str | None = None
    revenue: Decimal
    sales_count: Decimal
    cost_of_sales: Decimal
    gross_profit: Decimal
    margin_pct: Decimal | None = None
    profitability_pct: Decimal | None = None
