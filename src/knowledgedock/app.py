"""FastAPI application factory.

Kept separate from `main.py` so tests can build an isolated app instance
without importing the module-level ASGI object.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from knowledgedock import __version__
from knowledgedock.api.health import router as health_router
from knowledgedock.core.config import Settings, get_settings
from knowledgedock.core.logging import configure_logging
from knowledgedock.infrastructure.mongo import MongoManager

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    mongo_manager: MongoManager | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_format)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        mongo = mongo_manager or MongoManager(settings)
        app.state.mongo = mongo
        app.state.settings = settings
        await mongo.connect()
        try:
            yield
        finally:
            await mongo.disconnect()

    app = FastAPI(
        title="KnowledgeDock",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs" if not settings.is_production else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if not settings.is_production else None,
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = request_id
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "request.failed",
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                },
            )
            raise
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        response.headers["x-request-id"] = request_id
        logger.info(
            "request.completed",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": duration_ms,
            },
        )
        return response

    app.mount("/static", StaticFiles(directory=settings.static_dir), name="static")
    app.include_router(health_router)

    templates = Jinja2Templates(directory=settings.templates_dir)

    @app.get("/", include_in_schema=False)
    async def index(request: Request) -> Response:
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={"app_name": "KnowledgeDock", "version": __version__},
        )

    return app
