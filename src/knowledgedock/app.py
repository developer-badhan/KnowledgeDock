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
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pymongo.errors import PyMongoError

from knowledgedock import __version__
from knowledgedock.api.auth import router as auth_router
from knowledgedock.api.dependencies import SESSION_COOKIE_NAME
from knowledgedock.api.health import router as health_router
from knowledgedock.application.auth.use_cases import (
    AuthenticateUser,
    ConfirmPasswordReset,
    RegisterUser,
    RequestPasswordReset,
    ResolveCurrentUser,
)
from knowledgedock.core.config import Settings, get_settings
from knowledgedock.core.logging import configure_logging
from knowledgedock.domain.errors import AppError, ErrorCode
from knowledgedock.infrastructure.mongo import MongoManager
from knowledgedock.infrastructure.repositories.user_repository import (
    MongoUserRepository,
    UnavailableUserRepository,
)
from knowledgedock.infrastructure.security.passwords import PasswordHasher
from knowledgedock.infrastructure.security.tokens import TokenService

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    mongo_manager: MongoManager | None = None,
    user_repository=None,
    hasher: PasswordHasher | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    # Templates are built once, before the lifespan runs, so route handlers can
    # reach them via app.state without waiting for startup. Globals let every
    # template know whether the API docs exist at all.
    templates = Jinja2Templates(directory=settings.templates_dir)
    templates.env.globals["is_production"] = settings.is_production
    templates.env.globals["app_version"] = __version__

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        mongo = mongo_manager or MongoManager(settings)
        app.state.mongo = mongo

        password_hasher = hasher or PasswordHasher()
        tokens = TokenService(settings.secret_key, expire_minutes=settings.jwt_expire_minutes)

        # Connect BEFORE building the repository. `MongoManager.database()`
        # refuses to hand out a database before the client exists, so building
        # the repository first raises on every real startup — a failure the
        # in-memory test double hides completely.
        await mongo.connect()

        if user_repository is not None:
            repository = user_repository
        else:
            try:
                repository = MongoUserRepository(mongo.database())
            except Exception as exc:
                logger.error(
                    "auth.repository_unavailable",
                    extra={"error": "could not reach the database at startup"},
                    exc_info=True,
                )
                repository = UnavailableUserRepository(exc)

        app.state.register_user = RegisterUser(
            repository,
            password_hasher,
            minimum_password_length=settings.password_min_length,
        )
        app.state.authenticate_user = AuthenticateUser(repository, password_hasher, tokens)
        app.state.resolve_current_user = ResolveCurrentUser(repository, tokens)
        app.state.request_password_reset = RequestPasswordReset(
            repository,
            expire_minutes=settings.password_reset_expire_minutes,
            # No mail provider exists, so outside production the link is written
            # to the log. In production it is withheld.
            expose_token=not settings.is_production,
        )
        app.state.confirm_password_reset = ConfirmPasswordReset(
            repository,
            password_hasher,
            minimum_password_length=settings.password_min_length,
        )

        await _ensure_indexes(repository)

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
    app.state.settings = settings
    app.state.templates = templates

    @app.middleware("http")
    async def request_context(request: Request, call_next) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = request_id
        # Best-effort, for template rendering only. Authorisation is enforced by
        # the dependency on protected routes; this just lets the nav show the
        # right links. It must never turn a page into a 500.
        request.state.user = await _optional_current_user(request)
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

    _register_error_handlers(app)

    app.mount("/static", StaticFiles(directory=settings.static_dir), name="static")
    app.include_router(health_router)
    app.include_router(auth_router)

    @app.get("/", include_in_schema=False)
    async def index(request: Request) -> Response:
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={"app_name": "KnowledgeDock", "version": __version__},
        )

    return app


async def _ensure_indexes(repository) -> None:
    """Create indexes, tolerating failure.

    A missing unique index would allow duplicate accounts, so the failure is
    logged loudly — but it must not stop the process binding `$PORT`. The same
    reasoning as the Mongo connection: report the truth, keep serving.
    """
    try:
        await repository.ensure_indexes()
    except Exception:
        logger.error(
            "mongodb.index_creation_failed",
            extra={"error": "could not create user indexes"},
            exc_info=True,
        )


async def _optional_current_user(request: Request):
    """Resolve the caller for template rendering. Returns None on any problem."""
    resolver = getattr(request.app.state, "resolve_current_user", None)
    if resolver is None:
        return None
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None
    try:
        return await resolver.execute(token)
    except AppError:
        # Expired or revoked: the nav renders as signed-out, which is correct.
        return None
    except Exception:
        logger.warning("nav.session_lookup_failed", exc_info=True)
        return None


def _register_error_handlers(app: FastAPI) -> None:
    """Turn `AppError` into a controlled response.

    `SKILL.md` §23: clients get a stable code and a safe message; the diagnostic
    detail stays in the log. An unhandled exception also becomes a 500 with no
    traceback in the body.
    """

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> Response:
        log = logger.warning if exc.status_code < 500 else logger.error
        log(
            "request.rejected",
            extra={
                "request_id": getattr(request.state, "request_id", None),
                "code": str(exc.code),
                "status": exc.status_code,
                "path": request.url.path,
                "detail": exc.detail,
            },
        )
        return _error_response(
            request, exc.status_code, str(exc.code), exc.message, templates=app.state.templates
        )

    @app.exception_handler(PyMongoError)
    async def handle_database_error(request: Request, exc: PyMongoError) -> Response:
        """A database failure is ours, not the client's fault.

        Without this, an unreachable Atlas surfaces as a 500 from a route that
        never had a chance to fail cleanly. `connect()` deliberately does not
        raise, so the process stays up and every query then times out on its own;
        each one needs translating here.
        """
        logger.error(
            "database.request_failed",
            extra={
                "request_id": getattr(request.state, "request_id", None),
                "path": request.url.path,
                "error": type(exc).__name__,
            },
        )
        return _error_response(
            request,
            503,
            str(ErrorCode.SERVICE_UNAVAILABLE),
            "The service is temporarily unavailable. Please try again.",
            templates=app.state.templates,
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> Response:
        logger.exception(
            "request.unhandled",
            extra={
                "request_id": getattr(request.state, "request_id", None),
                "path": request.url.path,
                "error": type(exc).__name__,
            },
        )
        return _error_response(
            request,
            500,
            str(ErrorCode.INTERNAL_ERROR),
            "Something went wrong. Please try again.",
            templates=app.state.templates,
        )


def _error_response(
    request: Request, status_code: int, code: str, message: str, *, templates
) -> Response:
    wants_html = "text/html" in request.headers.get("accept", "")
    if wants_html and templates is not None:
        try:
            # Keyword arguments, not context={...}: wrapping them would create a
            # variable named `context` and render the page blank.
            return HTMLResponse(
                content=templates.get_template("error.html").render(
                    request=request, code=code, message=message, status=status_code
                ),
                status_code=status_code,
            )
        except Exception:  # pragma: no cover - the error page must never 500
            logger.warning("error_page_render_failed", exc_info=True)
    return JSONResponse(
        status_code=status_code, content={"error": {"code": code, "message": message}}
    )
