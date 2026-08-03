"""FastAPI application factory."""

from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.v1 import admin, auth, delivery, governance, ops, projects
from app.config import get_settings
from app.core.errors import FynixError
from app.core.logging import clear_context, configure, get_logger, set_correlation_id
from app.notifications import service as notifications
from app.portal import routes as portal_routes
from app.telegram import webhook as telegram_webhook

log = get_logger("fynix.api")

DESCRIPTION = """
FYNIX AI Pipeline — Control Plane для конвейера цифровых проектов.

Платформа управляет процессом: бриф → blueprint → задачи → PR → gates →
evidence → релиз → деплой. AI-агенты выполняют работу внутри изолированных
runner'ов и не имеют безусловного доступа к production, секретам и
клиентской инфраструктуре.
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.assert_bootable()
    configure(settings.log_level, json_output=settings.is_production)
    notifications.register()
    log.info("api.started", env=settings.env, base_url=settings.base_url)
    yield
    log.info("api.stopping")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="FYNIX AI Pipeline",
        description=DESCRIPTION,
        version="1.0.0",
        docs_url="/docs",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def correlation_middleware(request: Request, call_next):
        """Attach a correlation id to every request and log its outcome."""
        correlation_id = request.headers.get("X-Correlation-Id") or uuid.uuid4().hex
        set_correlation_id(correlation_id)
        request.state.correlation_id = correlation_id
        started = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            clear_context()
        duration_ms = int((time.perf_counter() - started) * 1000)
        response.headers["X-Correlation-Id"] = correlation_id
        if not request.url.path.startswith(("/static", "/metrics", "/health")):
            log.info(
                "http.request",
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                duration_ms=duration_ms,
            )
        return response

    @app.exception_handler(FynixError)
    async def fynix_error_handler(request: Request, exc: FynixError):
        # Domain errors carry a stable code; they are answers, not crashes.
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.to_dict(),
            headers={"X-Correlation-Id": getattr(request.state, "correlation_id", "")},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "request body failed validation",
                    "details": {"errors": exc.errors()},
                }
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception):
        # Never leak internals to the client; the correlation id links the
        # response to the full stack trace in the logs.
        correlation_id = getattr(request.state, "correlation_id", "")
        log.exception("http.unhandled_error", path=request.url.path, error=str(exc)[:500])
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "internal error",
                    "details": {"correlation_id": correlation_id},
                }
            },
        )

    app.include_router(auth.router)
    app.include_router(projects.router)
    app.include_router(governance.router)
    app.include_router(delivery.router)
    app.include_router(admin.router)
    app.include_router(ops.router)
    app.include_router(telegram_webhook.router)
    app.include_router(portal_routes.router)

    _ = settings
    return app


app = create_app()
