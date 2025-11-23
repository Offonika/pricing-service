import logging
import time
from typing import Callable

from fastapi import FastAPI, Request, Response

from app.api.health import router as health_router
from app.api.bi import router as bi_router
from app.api.recommendations import router as recommendations_router
from app.api.reports import router as reports_router
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


@app.middleware("http")
async def log_requests(request: Request, call_next: Callable[[Request], Response]) -> Response:
    start_time = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("request failed", extra={"path": request.url.path, "method": request.method})
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
app.include_router(recommendations_router, prefix="/api")
app.include_router(reports_router, prefix="/api/reports")
app.include_router(bi_router, prefix="/api/bi")
app.include_router(telegram_router, prefix="/api/telegram")
