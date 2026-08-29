from __future__ import annotations

from dataclasses import asdict
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.dependencies import get_db, require_order_fulfillment_internal_token
from app.core.config import get_settings
from app.infrastructure.db.engines import DatabaseNotConfiguredError, get_onec_engine
from app.schemas.order_fulfillment import (
    BitrixChatIngestResponse,
    BitrixChatMessageIngestRequest,
    BitrixChatMessageIngestResponse,
    DeliveryMethodReportResponse,
    OrderFulfillmentMentionResponse,
    OrderFulfillmentRecommendationsResponse,
    OrderFulfillmentReviewResponse,
    OrderShipmentsSyncRequest,
    OrderShipmentsSyncResponse,
)
from app.services import site_order_fulfillment as fulfillment
from app.services import site_order_shipments

router = APIRouter(dependencies=[Depends(require_order_fulfillment_internal_token)])


@router.post("/shipments/sync", response_model=OrderShipmentsSyncResponse)
def sync_order_shipments(
    payload: OrderShipmentsSyncRequest,
    db: Session = Depends(get_db),
) -> OrderShipmentsSyncResponse:
    settings = get_settings()
    if not payload.dry_run and not (
        settings.order_fulfillment_shipments_master_enabled
        and settings.order_fulfillment_shipments_ingest_enabled
    ):
        raise HTTPException(
            status_code=409,
            detail="shipment ingest is disabled",
        )
    shipment_snapshots = [item.model_dump(mode="python") for item in payload.shipments]
    if not payload.dry_run and settings.order_fulfillment_shipments_gateway_apply_enabled:
        if payload.bitrix_order_id is None:
            raise HTTPException(status_code=409, detail="bitrix_order_id is required")
        try:
            gateway = site_order_shipments.BitrixSaleShipmentGatewayClient(
                base_url=settings.order_fulfillment_shipments_gateway_url or "",
                token=settings.order_fulfillment_shipments_gateway_token or "",
            )
            shipment_snapshots = site_order_shipments.ensure_missing_bitrix_shipments(
                gateway,
                order_id=payload.bitrix_order_id,
                shipment_snapshots=shipment_snapshots,
            )
        except (ValueError, site_order_shipments.ShipmentGatewayError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    result = site_order_shipments.sync_order_shipments(
        db,
        site_order_number=payload.site_order_number,
        bitrix_deal_id=payload.bitrix_deal_id,
        current_stage=payload.current_stage,
        expected_items=[item.model_dump(mode="python") for item in payload.expected_items],
        rtus=[item.model_dump(mode="python") for item in payload.rtus],
        shipments=shipment_snapshots,
        event_at=payload.event_at,
        persist=not payload.dry_run,
        enqueue_crm_fields=(
            not payload.dry_run and settings.order_fulfillment_shipments_crm_fields_enabled
        ),
        enqueue_notifications=(
            not payload.dry_run and settings.order_fulfillment_shipments_notifications_enabled
        ),
        email_enabled=settings.order_fulfillment_shipments_email_enabled,
        sms_enabled=settings.order_fulfillment_shipments_sms_enabled,
    )
    if not payload.dry_run:
        db.commit()
    return OrderShipmentsSyncResponse(**asdict(result))


@router.post("/bitrix/messages", response_model=BitrixChatMessageIngestResponse)
def ingest_bitrix_message(
    payload: BitrixChatMessageIngestRequest,
    db: Session = Depends(get_db),
) -> BitrixChatMessageIngestResponse:
    chat_code = _normalize_chat_code(payload.chat_code)
    settings = get_settings()
    if payload.dry_run:
        mentions = fulfillment.parse_bitrix_message(
            chat_code=chat_code,
            text_value=payload.text,
            ocr_payloads=payload.ocr_payloads,
        )
        return BitrixChatMessageIngestResponse(
            message_id=payload.message_id,
            parse_status="parsed" if mentions else "no_mentions",
            mentions=[
                OrderFulfillmentMentionResponse(
                    site_order_number=mention.site_order_number,
                    event_type=mention.event_type,
                    confidence=mention.confidence,
                    evidence_text=mention.evidence_text,
                    payload=mention.payload,
                )
                for mention in mentions
            ],
            events_created=0,
        )

    result = fulfillment.ingest_bitrix_message(
        db,
        chat_code=chat_code,
        dialog_id=payload.dialog_id,
        chat_id=payload.chat_id,
        message_id=payload.message_id,
        message_at=payload.message_at,
        author_id=payload.author_id,
        text_value=payload.text,
        payload=payload.payload,
        ocr_payloads=payload.ocr_payloads,
        create_execution_events=(
            chat_code == fulfillment.CHAT_COURIER_SPB
            or (
                chat_code == fulfillment.CHAT_SITE_MASTER_MOBILE
                and not settings.order_fulfillment_bot_enabled
            )
        ),
    )
    return BitrixChatMessageIngestResponse(
        message_id=result.message.message_id,
        parse_status=result.message.parse_status,
        duplicate_message=result.duplicate_message,
        mentions=[
            OrderFulfillmentMentionResponse(
                site_order_number=mention.site_order_number,
                event_type=mention.event_type,
                confidence=mention.confidence,
                evidence_text=mention.evidence_text,
                payload=mention.payload,
            )
            for mention in result.mentions
        ],
        events_created=len(result.events),
    )


@router.post("/bitrix/chats/ingest", response_model=BitrixChatIngestResponse)
def ingest_bitrix_chat(
    chat_code: str = Query(default=fulfillment.CHAT_SITE_MASTER_MOBILE),
    limit: int = Query(default=50, ge=1, le=200),
    run_ocr: bool = Query(default=True),
    db: Session = Depends(get_db),
) -> BitrixChatIngestResponse:
    settings = get_settings()
    if not settings.order_fulfillment_bitrix_webhook_url:
        raise HTTPException(
            status_code=400,
            detail="ORDER_FULFILLMENT_BITRIX_WEBHOOK_URL is not configured",
        )
    chat_code = _normalize_chat_code(chat_code)
    dialog_id = _dialog_id_for_chat_code(chat_code)
    client = fulfillment.BitrixChatClient(settings.order_fulfillment_bitrix_webhook_url)
    stats = fulfillment.ingest_bitrix_chat(
        db,
        client=client,
        chat_code=chat_code,
        dialog_id=dialog_id,
        limit=limit,
        run_ocr=bool(run_ocr and settings.order_fulfillment_ocr_enabled),
        settings=settings,
    )
    return BitrixChatIngestResponse(chat_code=chat_code, dialog_id=dialog_id, **stats)


@router.get("/cases/recommendations", response_model=OrderFulfillmentRecommendationsResponse)
def list_recommendations(
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> OrderFulfillmentRecommendationsResponse:
    return OrderFulfillmentRecommendationsResponse(
        items=fulfillment.build_recommendations(db, limit=limit, status=status)
    )


@router.get("/cases/review", response_model=OrderFulfillmentReviewResponse)
def list_review_rows(
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> OrderFulfillmentReviewResponse:
    settings = get_settings()
    bitrix_client = (
        fulfillment.BitrixChatClient(settings.order_fulfillment_bitrix_webhook_url)
        if settings.order_fulfillment_bitrix_webhook_url
        else None
    )
    try:
        onec_engine = get_onec_engine()
    except DatabaseNotConfiguredError:
        onec_engine = None
    rows = fulfillment.build_review_rows(
        db,
        limit=limit,
        status=status,
        bitrix_client=bitrix_client,
        onec_engine=onec_engine,
        settings=settings,
    )
    return OrderFulfillmentReviewResponse(items=fulfillment.review_rows_to_dicts(rows))


@router.get("/delivery-methods/unknown", response_model=DeliveryMethodReportResponse)
def list_unknown_delivery_methods(
    date_from: date | None = Query(default=None),
) -> DeliveryMethodReportResponse:
    try:
        rows = fulfillment.query_unknown_delivery_methods(get_settings(), date_from=date_from)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return DeliveryMethodReportResponse(
        items=[
            {
                "raw_delivery_method": row.raw_delivery_method,
                "count": row.count,
                "status": row.status,
                "note": row.note,
            }
            for row in rows
        ]
    )


def _dialog_id_for_chat_code(chat_code: str) -> str:
    settings = get_settings()
    if chat_code == fulfillment.CHAT_SITE_MASTER_MOBILE:
        return settings.order_fulfillment_site_chat_dialog_id
    if chat_code == fulfillment.CHAT_COURIER_SPB:
        return settings.order_fulfillment_spb_courier_chat_dialog_id
    raise HTTPException(status_code=400, detail=f"unknown chat_code: {chat_code}")


def _normalize_chat_code(chat_code: str) -> str:
    if chat_code == "spb_courier_report":
        return fulfillment.CHAT_COURIER_SPB
    return chat_code
