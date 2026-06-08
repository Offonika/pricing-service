from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

PaymentDirection = Literal["incoming", "outgoing"]
NormalizeStatus = Literal["ready", "manual_review"]
BankPaymentSberExportMode = Literal["high_confidence"]


class BankPaymentClassifyRequest(BaseModel):
    direction: PaymentDirection | None = None
    document_kind: str | None = None
    payment_date: date | None = None
    number: str | None = None
    amount: float | None = None
    payer_name: str | None = None
    payer_inn: str | None = None
    payer_kpp: str | None = None
    payer_account: str | None = None
    payer_bank: str | None = None
    recipient_name: str | None = None
    recipient_inn: str | None = None
    recipient_kpp: str | None = None
    recipient_account: str | None = None
    recipient_bank: str | None = None
    purpose: str | None = None
    payment_purpose_code: str | None = None
    priority: str | None = None
    payer_account_is_own: bool = False
    recipient_account_is_own: bool = False
    existing_document_found: bool = False


class BankPaymentClassifyResponse(BaseModel):
    scenario: str
    operation_code: str | None = None
    cash_flow_article_name: str | None = None
    contract_code: str | None = None
    physical_person_name: str | None = None
    skip_auto_contract_fill: bool = False
    skip_payment_fill: bool = False
    should_load: bool = True
    existing_document_policy: str = "default"
    confidence: float = Field(ge=0.0, le=1.0)
    reasons: list[str] = Field(default_factory=list)


class BankPaymentNormalizeCounts(BaseModel):
    source_lines: int = 0
    payments: int = 0
    classified: int = 0
    manual_review: int = 0
    exported: int = 0


class BankPaymentNormalizeResponse(BaseModel):
    upload_id: str
    status: NormalizeStatus
    detected_format: str
    counts: BankPaymentNormalizeCounts
    issues: list[str] = Field(default_factory=list)
    download_url: str | None = None
    report_url: str | None = None


class BankPaymentSberExportRequest(BaseModel):
    date_from: date
    date_to: date
    account_numbers: list[str] | None = None
    export_mode: BankPaymentSberExportMode = "high_confidence"


class BankPaymentBitrixSyncResponse(BaseModel):
    processed: int = 0
    ready: int = 0
    errors: int = 0
    skipped: int = 0
    last_error: str | None = None
