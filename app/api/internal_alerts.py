from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Security
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.api.dependencies import get_db
from app.core.config import get_settings
from app.schemas.return_scheme_alerts import (
    ReturnSchemeAlertBatchAckResponse,
    ReturnSchemeAlertBatchList,
)
from app.services.return_scheme import (
    acknowledge_return_scheme_alert_batch,
    get_pending_return_scheme_alert_batches,
    get_return_scheme_alert_batch,
    serialize_return_scheme_alert_batch,
)

router = APIRouter()
security = HTTPBearer(auto_error=False)


def _authorize_internal(
    credentials: HTTPAuthorizationCredentials = Security(security),
) -> str:
    expected = get_settings().return_scheme_internal_api_token
    if not expected:
        raise HTTPException(status_code=401, detail="internal token not configured")
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="unauthorized")
    if credentials.credentials != expected:
        raise HTTPException(status_code=401, detail="unauthorized")
    return credentials.credentials


@router.get("/return-scheme/pending", response_model=ReturnSchemeAlertBatchList)
def list_pending_return_scheme_alerts(
    db: Session = Depends(get_db),
    _: str = Depends(_authorize_internal),
):
    batches = get_pending_return_scheme_alert_batches(db)
    return ReturnSchemeAlertBatchList(
        items=[serialize_return_scheme_alert_batch(db, batch) for batch in batches]
    )


@router.get("/return-scheme/{batch_id}/report")
def download_return_scheme_report(
    batch_id: int,
    db: Session = Depends(get_db),
    _: str = Depends(_authorize_internal),
):
    batch = get_return_scheme_alert_batch(db, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="batch not found")

    report_path = Path(batch.report_path)
    if not report_path.exists() or not report_path.is_file():
        raise HTTPException(status_code=404, detail="report not found")

    return FileResponse(
        path=report_path,
        filename=report_path.name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@router.post("/return-scheme/{batch_id}/ack", response_model=ReturnSchemeAlertBatchAckResponse)
def acknowledge_return_scheme_alert(
    batch_id: int,
    db: Session = Depends(get_db),
    _: str = Depends(_authorize_internal),
):
    batch = get_return_scheme_alert_batch(db, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="batch not found")

    batch = acknowledge_return_scheme_alert_batch(db, batch_id)
    db.commit()
    db.refresh(batch)
    return ReturnSchemeAlertBatchAckResponse(
        id=batch.id,
        status=batch.status,
        delivered_at=batch.delivered_at,
    )
