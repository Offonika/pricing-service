from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class ReturnSchemeAlertIncident(BaseModel):
    id: int
    store_ref: str
    store_name: str | None = None
    product_ref: str
    product_name: str | None = None
    manager_ref: str | None = None
    manager_name: str | None = None
    second_price_type: str | None = None
    matched_qty: float
    amount: float
    first_sale_doc_number: str
    first_sale_doc_datetime: datetime
    return_doc_number: str
    return_doc_datetime: datetime
    second_sale_doc_number: str
    second_sale_doc_datetime: datetime
    repeat_store_product_7d_count: int
    repeat_employee_7d_count: int


class ReturnSchemeAlertSummary(BaseModel):
    message: str


class ReturnSchemeAlertBatchItem(BaseModel):
    id: int
    generated_at: datetime
    window_start: datetime
    window_end: datetime
    new_incidents_count: int
    notification_incidents_count: int
    report_path: str
    status: str
    incident_ids: list[int]
    incidents: list[ReturnSchemeAlertIncident]
    summary: ReturnSchemeAlertSummary


class ReturnSchemeAlertBatchList(BaseModel):
    items: list[ReturnSchemeAlertBatchItem]


class ReturnSchemeAlertBatchAckResponse(BaseModel):
    id: int
    status: str
    delivered_at: datetime | None = None
