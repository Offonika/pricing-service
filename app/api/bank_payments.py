from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from app.api.dependencies import require_bank_payments_internal_token
from app.core.config import get_settings
from app.schemas.bank_payments import (
    BankPaymentBitrixSyncResponse,
    BankPaymentClassifyRequest,
    BankPaymentClassifyResponse,
    BankPaymentNormalizeResponse,
    BankPaymentSberExportRequest,
)
from app.services import bank_payments as bank_payment_service
from app.workers import bank_payments as bank_payment_worker

router = APIRouter(
    prefix="/v1/bank-payments",
    tags=["bank-payments"],
    dependencies=[Depends(require_bank_payments_internal_token)],
)


@router.post("/normalize", response_model=BankPaymentNormalizeResponse)
async def normalize_bank_payment_file(
    file: UploadFile = File(...),
    source_bank: str = Query(default="auto"),
) -> BankPaymentNormalizeResponse:
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="empty upload")
    return bank_payment_service.normalize_upload(
        content,
        filename=file.filename or "bank-statement",
        source_bank=source_bank,
        settings=get_settings(),
    )


@router.get("/normalize/{upload_id}/download")
def download_normalized_bank_payment_file(upload_id: str) -> FileResponse:
    try:
        path = bank_payment_service.get_normalized_file_path(upload_id, settings=get_settings())
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="normalized file not found") from exc
    return FileResponse(
        path,
        filename=f"bank-payments-{upload_id}.txt",
        media_type="text/plain; charset=windows-1251",
    )


@router.get("/normalize/{upload_id}/report")
def download_bank_payment_report(upload_id: str) -> FileResponse:
    try:
        path = bank_payment_service.get_report_file_path(upload_id, settings=get_settings())
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="report not found") from exc
    return FileResponse(
        path,
        filename=f"bank-payments-{upload_id}-report.txt",
        media_type="text/plain; charset=utf-8",
    )


@router.post("/classify", response_model=BankPaymentClassifyResponse)
def classify_bank_payment_line(
    payload: BankPaymentClassifyRequest,
) -> BankPaymentClassifyResponse:
    return bank_payment_service.classify_request(payload, settings=get_settings())


@router.post("/sber/export", response_model=BankPaymentNormalizeResponse)
def export_sber_bank_payments(
    payload: BankPaymentSberExportRequest,
) -> BankPaymentNormalizeResponse:
    try:
        return bank_payment_service.export_sber_raw_statement(
            date_from=payload.date_from,
            date_to=payload.date_to,
            account_numbers=payload.account_numbers,
            settings=get_settings(),
        )
    except bank_payment_service.BankPaymentsSourceConfigurationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/bitrix/sync", response_model=BankPaymentBitrixSyncResponse)
def sync_bank_payment_files_from_bitrix_disk() -> BankPaymentBitrixSyncResponse:
    try:
        result = bank_payment_worker.run_bank_payments_bitrix_disk_sync()
    except bank_payment_worker.BankPaymentsBitrixConfigurationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return BankPaymentBitrixSyncResponse(**result)
