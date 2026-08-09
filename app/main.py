import logging
import time
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.agents import router as agents_router
from app.api.analytics import router as analytics_router
from app.api.bank_payments import router as bank_payments_router
from app.api.bi import router as bi_router
from app.api.bitrix_executive_dashboard import (
    page_router as bitrix_executive_dashboard_page_router,
)
from app.api.bitrix_executive_dashboard import router as bitrix_executive_dashboard_router
from app.api.bitrix_matching import page_router as bitrix_matching_page_router
from app.api.bitrix_matching import router as bitrix_matching_router
from app.api.bitrix_receivables import page_router as bitrix_receivables_page_router
from app.api.bitrix_receivables import router as bitrix_receivables_router
from app.api.card_balance_reconciliation import router as card_balance_reconciliation_router
from app.api.counterparty_duplicates import router as counterparty_duplicates_router
from app.api.customer_price_types import (
    page_router as customer_price_types_page_router,
)
from app.api.customer_price_types import router as customer_price_types_router
from app.api.expertise import router as expertise_router
from app.api.health import router as health_router
from app.api.internal_alerts import router as internal_alerts_router
from app.api.logistics import router as logistics_router
from app.api.logistics_bot import router as logistics_bot_router
from app.api.logistics_web import page_router as logistics_web_page_router
from app.api.logistics_web import router as logistics_web_router
from app.api.management import router as management_router
from app.api.matching import router as matching_router
from app.api.orchestration import router as orchestration_router
from app.api.order_fulfillment import router as order_fulfillment_router
from app.api.order_payment_control import router as order_payment_control_router
from app.api.procurement_assortment_decisions import (
    page_router as procurement_assortment_page_router,
)
from app.api.procurement_assortment_decisions import router as procurement_assortment_router
from app.api.procurement_labels import page_router as procurement_labels_page_router
from app.api.procurement_labels import router as procurement_labels_router
from app.api.procurement_order_formation import (
    page_router as procurement_order_formation_page_router,
)
from app.api.procurement_order_formation import router as procurement_order_formation_router
from app.api.receivable_workplace import page_router as receivable_workplace_page_router
from app.api.receivable_workplace import router as receivable_workplace_router
from app.api.receivables import router as receivables_router
from app.api.recommendations import router as recommendations_router
from app.api.reports import router as reports_router
from app.api.site_defect_archive import page_router as site_defect_archive_page_router
from app.api.site_defect_archive import router as site_defect_archive_router
from app.api.staffing import router as staffing_router
from app.api.telegram import router as telegram_router
from app.core.config import get_settings
from app.core.logging import configure_logging

configure_logging()
settings = get_settings()
logger = logging.getLogger("app.request")

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    debug=settings.debug,
)

_UI_STATIC_ROOTS = (Path(__file__).resolve().parents[1] / "ui" / "dist",)

for static_root in _UI_STATIC_ROOTS:
    assets_root = static_root / "assets"
    if assets_root.exists():
        app.mount("/assets", StaticFiles(directory=assets_root), name="ui-assets")
        break


@app.get("/vite.svg", include_in_schema=False)
def vite_icon() -> FileResponse:
    for static_root in _UI_STATIC_ROOTS:
        icon = static_root / "vite.svg"
        if icon.exists():
            return FileResponse(icon)
    raise HTTPException(status_code=404, detail="vite icon not found")


if settings.cors_allow_origins:
    origins = [o.strip() for o in settings.cors_allow_origins.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=settings.cors_allow_credentials,
        allow_methods=(
            [m.strip() for m in settings.cors_allow_methods.split(",")]
            if settings.cors_allow_methods
            else ["*"]
        ),
        allow_headers=(
            [h.strip() for h in settings.cors_allow_headers.split(",")]
            if settings.cors_allow_headers
            else ["*"]
        ),
    )


@app.middleware("http")
async def log_requests(request: Request, call_next: Callable[[Request], Response]) -> Response:
    start_time = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception(
            "request failed", extra={"path": request.url.path, "method": request.method}
        )
        raise

    duration_ms = (time.perf_counter() - start_time) * 1000
    logger.info(
        "request handled",
        extra={
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            "duration_ms": round(duration_ms, 2),
        },
    )
    return response


app.include_router(health_router)
app.include_router(bitrix_matching_page_router)
app.include_router(bitrix_executive_dashboard_page_router)
app.include_router(bitrix_receivables_page_router)
app.include_router(logistics_web_page_router)
app.include_router(site_defect_archive_page_router)
app.include_router(receivable_workplace_page_router)
app.include_router(procurement_labels_page_router)
app.include_router(procurement_assortment_page_router)
app.include_router(procurement_order_formation_page_router)
app.include_router(customer_price_types_page_router)
app.include_router(recommendations_router, prefix="/api")
app.include_router(reports_router, prefix="/api/reports")
app.include_router(bi_router, prefix="/api/bi")
app.include_router(telegram_router, prefix="/api/telegram")
app.include_router(agents_router, prefix="/api")
app.include_router(analytics_router, prefix="/api")
app.include_router(bank_payments_router, prefix="/api")
app.include_router(matching_router, prefix="/api")
app.include_router(bitrix_matching_router, prefix="/api")
app.include_router(bitrix_executive_dashboard_router, prefix="/api")
app.include_router(bitrix_receivables_router, prefix="/api")
app.include_router(management_router, prefix="/api/management")
app.include_router(receivables_router, prefix="/api/receivables")
app.include_router(receivable_workplace_router, prefix="/api/receivables")
app.include_router(staffing_router, prefix="/api/staffing")
app.include_router(internal_alerts_router, prefix="/api/internal/alerts")
app.include_router(counterparty_duplicates_router, prefix="/api/internal/counterparty-duplicates")
app.include_router(customer_price_types_router)
app.include_router(expertise_router, prefix="/api/expertise")
app.include_router(site_defect_archive_router, prefix="/api/site-defects")
app.include_router(card_balance_reconciliation_router, prefix="/api/card-balance-reconciliation")
app.include_router(logistics_router, prefix="/api/logistics")
app.include_router(logistics_bot_router, prefix="/api/logistics/bot")
app.include_router(logistics_web_router, prefix="/api/logistics/web")
app.include_router(order_fulfillment_router, prefix="/api/order-fulfillment")
app.include_router(order_payment_control_router, prefix="/api/order-payment-control")
app.include_router(orchestration_router)
app.include_router(procurement_labels_router, prefix="/api")
app.include_router(procurement_assortment_router, prefix="/api")
app.include_router(procurement_order_formation_router, prefix="/api")
