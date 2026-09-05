from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import date

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.api.dependencies import get_db, require_order_fulfillment_internal_token
from app.core.config import get_settings
from app.infrastructure.db.engines import DatabaseNotConfiguredError, get_onec_engine
from app.schemas.order_fulfillment import (
    BitrixChatIngestResponse,
    BitrixChatMessageIngestRequest,
    BitrixChatMessageIngestResponse,
    DeliveryMethodReportResponse,
    OnecExecutionEventIngestRequest,
    OnecExecutionEventIngestResponse,
    OrderFulfillmentMentionResponse,
    OrderFulfillmentRecommendationsResponse,
    OrderFulfillmentReviewResponse,
    OrderShipmentsSyncRequest,
    OrderShipmentsSyncResponse,
    ShipmentNotificationStatusRequest,
    ShipmentNotificationStatusResponse,
    SiteCrmSignalIngestRequest,
)
from app.services import order_assembly_queue as assembly_queue
from app.services import site_order_execution_ingest, site_order_shipments
from app.services import site_order_fulfillment as fulfillment

router = APIRouter(dependencies=[Depends(require_order_fulfillment_internal_token)])
logger = logging.getLogger(__name__)


def _reconcile_direct_execution_event(site_order_number: str, *, apply: bool) -> None:
    try:
        from tasks.reconcile_onec_assembly_to_crm import reconcile_service_db_orders

        reconcile_service_db_orders([site_order_number], apply=apply)
    except Exception:
        logger.exception(
            "Narrow execution reconciliation failed for site order %s",
            site_order_number,
        )


def _process_direct_site_signal(site_order_number: str, *, apply: bool) -> None:
    try:
        from app.infrastructure.db.session import session_scope
        from app.services import site_order_stage_outbox as stage_outbox_service

        settings = get_settings()
        if not settings.order_fulfillment_bitrix_webhook_url:
            raise RuntimeError("ORDER_FULFILLMENT_BITRIX_WEBHOOK_URL is not configured")
        client = fulfillment.BitrixChatClient(settings.order_fulfillment_bitrix_webhook_url)
        with session_scope() as session:
            stage_outbox_service.process_stage_outbox(
                session,
                client=client,
                apply=apply,
                settings=settings,
                limit=10,
                site_order_numbers=[site_order_number],
            )
    except Exception:
        logger.exception("Narrow site CRM signal processing failed for %s", site_order_number)


@router.post(
    "/execution/events",
    response_model=OnecExecutionEventIngestResponse,
)
def ingest_onec_execution_event(
    payload: OnecExecutionEventIngestRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> OnecExecutionEventIngestResponse:
    settings = get_settings()
    fact = site_order_execution_ingest.OnecExecutionFact(
        signal=payload.signal,
        event_at=payload.event_at,
        site_order_number=payload.site_order_number,
        onec_order_number=payload.onec_order_number,
        rtu_external_id=payload.rtu_external_id,
        rtu_number=payload.rtu_number,
        rtu_date=payload.rtu_date,
        is_posted=payload.is_posted,
        document_amount=payload.document_amount,
    )
    source_ref = site_order_execution_ingest.canonical_onec_execution_source_ref(fact)
    if payload.dry_run:
        return OnecExecutionEventIngestResponse(
            accepted=True,
            duplicate=False,
            event_id=None,
            source_ref=source_ref,
            reconciliation_queued=False,
        )
    if not (
        settings.order_fulfillment_execution_master_enabled
        and settings.order_fulfillment_execution_ingest_enabled
    ):
        raise HTTPException(status_code=409, detail="execution ingest is disabled")
    result = site_order_execution_ingest.ingest_onec_execution_fact(db, fact)
    db.commit()
    reconciliation_queued = bool(
        not result.duplicate and settings.order_fulfillment_execution_reconciliation_enabled
    )
    if reconciliation_queued:
        background_tasks.add_task(
            _reconcile_direct_execution_event,
            payload.site_order_number,
            apply=settings.order_fulfillment_execution_stage_apply_enabled,
        )
    return OnecExecutionEventIngestResponse(
        accepted=True,
        duplicate=result.duplicate,
        event_id=result.event_id,
        source_ref=result.source_ref,
        reconciliation_queued=reconciliation_queued,
    )


@router.post(
    "/site/events",
    response_model=OnecExecutionEventIngestResponse,
)
def ingest_site_crm_signal(
    payload: SiteCrmSignalIngestRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> OnecExecutionEventIngestResponse:
    settings = get_settings()
    fact = site_order_execution_ingest.SiteCrmSignalFact(
        signal=payload.signal,
        event_at=payload.event_at,
        site_order_number=payload.site_order_number,
        bitrix_deal_id=payload.bitrix_deal_id,
        source_revision=payload.source_revision,
        current_stage=payload.current_stage,
    )
    source_ref = site_order_execution_ingest.canonical_site_crm_signal_source_ref(fact)
    if payload.dry_run:
        return OnecExecutionEventIngestResponse(
            accepted=True,
            duplicate=False,
            event_id=None,
            source_ref=source_ref,
            reconciliation_queued=False,
        )
    if not (
        settings.order_fulfillment_execution_master_enabled
        and settings.order_fulfillment_site_signal_ingest_enabled
    ):
        raise HTTPException(status_code=409, detail="site CRM signal ingest is disabled")
    result = site_order_execution_ingest.ingest_site_crm_signal_fact(db, fact)
    db.commit()
    processing_queued = not result.duplicate
    if processing_queued:
        background_tasks.add_task(
            _process_direct_site_signal,
            payload.site_order_number,
            apply=settings.order_fulfillment_site_signal_stage_apply_enabled,
        )
    return OnecExecutionEventIngestResponse(
        accepted=True,
        duplicate=result.duplicate,
        event_id=result.event_id,
        source_ref=result.source_ref,
        reconciliation_queued=processing_queued,
    )


@router.get(
    "/assembly-queue",
    response_class=Response,
    responses={
        200: {
            "description": "Fresh CRM assembly queue",
            "content": {"application/xml": {"schema": {"type": "string"}}},
        },
        503: {
            "description": "CRM queue is unavailable; no stale rows are returned",
            "content": {"application/xml": {"schema": {"type": "string"}}},
        },
    },
)
def get_assembly_queue(
    format: str = Query(default="xml", pattern="^xml$"),
    limit: int = Query(default=1000, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> Response:
    del format
    settings = get_settings()
    if not settings.order_fulfillment_bitrix_webhook_url:
        state = assembly_queue.get_sync_state(db)
        return Response(
            content=assembly_queue.render_error_xml(
                code="bitrix_not_configured",
                last_success_at=state.last_success_at if state is not None else None,
            ),
            status_code=503,
            media_type="application/xml",
        )

    client = fulfillment.BitrixChatClient(settings.order_fulfillment_bitrix_webhook_url)
    try:
        snapshot = assembly_queue.sync_assembly_queue(
            db,
            client=client,
            limit=limit,
        )
        db.commit()
    except assembly_queue.AssemblyQueueError as exc:
        db.rollback()
        state = assembly_queue.record_sync_failure(db, error_code=exc.code)
        db.commit()
        return Response(
            content=assembly_queue.render_error_xml(
                code=exc.code,
                last_success_at=state.last_success_at,
            ),
            status_code=503,
            media_type="application/xml",
        )

    return Response(
        content=assembly_queue.render_queue_xml(snapshot),
        media_type="application/xml",
    )


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
    try:
        result = site_order_shipments.sync_order_shipments(
            db,
            snapshot_id=payload.snapshot_id,
            site_order_number=payload.site_order_number,
            bitrix_deal_id=payload.bitrix_deal_id,
            current_stage=payload.current_stage,
            delivery_kind=payload.delivery_kind,
            expected_items=[item.model_dump(mode="python") for item in payload.expected_items],
            rtus=[item.model_dump(mode="python") for item in payload.rtus],
            shipments=shipment_snapshots,
            event_at=payload.event_at,
            observed_at=payload.observed_at,
            source_revisions=payload.source_revisions,
            bitrix_order_id=payload.bitrix_order_id,
            enqueue_gateway=(
                not payload.dry_run and settings.order_fulfillment_shipments_gateway_apply_enabled
            ),
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
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not payload.dry_run:
        db.commit()
    return OrderShipmentsSyncResponse(**asdict(result))


@router.post(
    "/shipments/notifications/status",
    response_model=ShipmentNotificationStatusResponse,
)
def update_shipment_notification_status(
    payload: ShipmentNotificationStatusRequest,
    db: Session = Depends(get_db),
) -> ShipmentNotificationStatusResponse:
    try:
        result = site_order_shipments.update_notification_status(
            db,
            idempotency_key=payload.idempotency_key,
            status=payload.status,
            occurred_at=payload.occurred_at,
            external_ref=payload.external_ref,
            error=payload.error,
        )
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    db.commit()
    return ShipmentNotificationStatusResponse(**asdict(result))


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
