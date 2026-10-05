"""FastAPI application factory.

Kept separate from `main.py` so tests can build an isolated app instance
without importing the module-level ASGI object.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pymongo.errors import PyMongoError

from knowledgedock import __version__
from knowledgedock.api.auth import router as auth_router
from knowledgedock.api.dependencies import SESSION_COOKIE_NAME
from knowledgedock.api.documents import router as documents_router
from knowledgedock.api.health import router as health_router
from knowledgedock.api.query import router as query_router
from knowledgedock.api.search import router as search_router
from knowledgedock.api.ui import router as ui_router
from knowledgedock.api.workspaces import router as workspaces_router
from knowledgedock.application.auth.use_cases import (
    AuthenticateUser,
    ConfirmPasswordReset,
    RegisterUser,
    RequestPasswordReset,
    ResolveCurrentUser,
)
from knowledgedock.application.documents.use_cases import (
    DeleteDocument,
    GetDocument,
    ListDocuments,
    UploadDocument,
)
from knowledgedock.application.ingestion.process_document import ProcessDocument
from knowledgedock.application.rag.answer_question import (
    AskQuestion,
    DeleteConversation,
    GetConversationHistory,
    GetUsage,
    ListConversations,
    StartConversation,
)
from knowledgedock.application.retrieval.search import SemanticSearch
from knowledgedock.application.workspaces.use_cases import (
    AddMember,
    AuthorizeWorkspace,
    CreateWorkspace,
    DeleteWorkspace,
    GetWorkspace,
    ListMembers,
    ListWorkspaces,
    RemoveMember,
    RenameWorkspace,
)
from knowledgedock.core.config import Settings, get_settings
from knowledgedock.core.logging import configure_logging
from knowledgedock.domain.errors import AppError, ErrorCode
from knowledgedock.infrastructure.ai.embedding import (
    GeminiEmbeddingProvider,
    NullEmbeddingProvider,
)
from knowledgedock.infrastructure.ai.llm import GeminiChatProvider, NullLLMProvider
from knowledgedock.infrastructure.ai.usage import (
    UsageRecorder,
    UsageTrackingLLM,
    UsageTrackingProvider,
)
from knowledgedock.infrastructure.mongo import MongoManager
from knowledgedock.infrastructure.rate_limit import RateLimiter
from knowledgedock.infrastructure.repositories.chunk_repository import (
    MongoChunkRepository,
    UnavailableChunkRepository,
)
from knowledgedock.infrastructure.repositories.conversation_repository import (
    MongoConversationRepository,
    UnavailableConversationRepository,
)
from knowledgedock.infrastructure.repositories.document_repository import (
    MongoDocumentRepository,
    UnavailableDocumentRepository,
)
from knowledgedock.infrastructure.repositories.usage_repository import (
    MongoAIUsageRepository,
    UnavailableAIUsageRepository,
)
from knowledgedock.infrastructure.repositories.user_repository import (
    MongoUserRepository,
    UnavailableUserRepository,
)
from knowledgedock.infrastructure.repositories.workspace_repository import (
    MongoWorkspaceRepository,
    UnavailableWorkspaceRepository,
)
from knowledgedock.infrastructure.security.passwords import PasswordHasher
from knowledgedock.infrastructure.security.tokens import TokenService
from knowledgedock.infrastructure.storage import LocalFileStorage
from knowledgedock.infrastructure.vector_index import ensure_vector_index
from knowledgedock.workers.processor import IngestionWorker, build_worker_task

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    mongo_manager: MongoManager | None = None,
    user_repository=None,
    workspace_repository=None,
    document_repository=None,
    chunk_repository=None,
    conversation_repository=None,
    usage_repository=None,
    clock=None,
    storage=None,
    embeddings=None,
    llm=None,
    processing_enabled: bool | None = None,
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
    templates.env.filters["filesizeformat"] = _filesize
    # Aliased because the roadmap and every other layer call it `filesize`.
    templates.env.filters["filesize"] = _filesize
    templates.env.filters["datetimeformat"] = _short_datetime

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

        # Workspaces share the database handle but get their own repository so a
        # test can inject either one independently. Both fall back together: if
        # the handle is unavailable, neither is usable.
        if workspace_repository is not None:
            workspaces_repo = workspace_repository
        elif isinstance(repository, UnavailableUserRepository):
            workspaces_repo = UnavailableWorkspaceRepository(repository.cause)
        else:
            try:
                workspaces_repo = MongoWorkspaceRepository(mongo.database())
            except Exception as exc:  # pragma: no cover - mirrors the user path
                logger.error(
                    "workspaces.repository_unavailable",
                    extra={"error": "could not reach the database at startup"},
                    exc_info=True,
                )
                workspaces_repo = UnavailableWorkspaceRepository(exc)

        if document_repository is not None:
            documents_repo = document_repository
        elif isinstance(repository, UnavailableUserRepository):
            documents_repo = UnavailableDocumentRepository(repository.cause)
        else:
            try:
                documents_repo = MongoDocumentRepository(mongo.database())
            except Exception as exc:  # pragma: no cover - mirrors the other paths
                logger.error(
                    "documents.repository_unavailable",
                    extra={"error": "could not reach the database at startup"},
                    exc_info=True,
                )
                documents_repo = UnavailableDocumentRepository(exc)

        if usage_repository is not None:
            usage_repo = usage_repository
        elif isinstance(repository, UnavailableUserRepository):
            usage_repo = UnavailableAIUsageRepository(repository.cause)
        else:
            try:
                usage_repo = MongoAIUsageRepository(mongo.database())
            except Exception as exc:  # pragma: no cover - mirrors the other paths
                logger.error(
                    "usage.repository_unavailable",
                    extra={"error": "could not reach the database at startup"},
                    exc_info=True,
                )
                usage_repo = UnavailableAIUsageRepository(exc)

        if conversation_repository is not None:
            conversations_repo = conversation_repository
        elif isinstance(repository, UnavailableUserRepository):
            conversations_repo = UnavailableConversationRepository(repository.cause)
        else:
            try:
                conversations_repo = MongoConversationRepository(mongo.database())
            except Exception as exc:  # pragma: no cover - mirrors the other paths
                logger.error(
                    "conversations.repository_unavailable",
                    extra={"error": "could not reach the database at startup"},
                    exc_info=True,
                )
                conversations_repo = UnavailableConversationRepository(exc)

        # Uploads are staged on local disk. On Render that disk is ephemeral,
        # which is fine: this is a staging area for processing, and the durable
        # copy of the extracted text lives in MongoDB.
        file_storage = storage or LocalFileStorage(settings.storage_dir)
        file_storage.ensure_root()

        app.state.upload_document = UploadDocument(
            documents_repo,
            file_storage,
            allowed_content_types=settings.allowed_content_types,
            max_bytes=settings.max_upload_size_mb * 1024 * 1024,
        )
        app.state.list_documents = ListDocuments(documents_repo)
        app.state.get_document = GetDocument(documents_repo)
        app.state.delete_document = DeleteDocument(documents_repo, file_storage)

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

        # Workspace use cases. `authorize_workspace` is exposed on its own because
        # it is the single isolation gate that later phases depend on.
        authorize = AuthorizeWorkspace(workspaces_repo)
        app.state.authorize_workspace = authorize
        app.state.create_workspace = CreateWorkspace(workspaces_repo)
        app.state.list_workspaces = ListWorkspaces(workspaces_repo)
        app.state.get_workspace = GetWorkspace(workspaces_repo, authorize)
        app.state.rename_workspace = RenameWorkspace(workspaces_repo, authorize)
        app.state.delete_workspace = DeleteWorkspace(workspaces_repo, authorize)
        app.state.list_members = ListMembers(workspaces_repo, authorize)
        app.state.add_member = AddMember(workspaces_repo, repository, authorize)
        app.state.remove_member = RemoveMember(workspaces_repo, authorize)

        if chunk_repository is not None:
            chunks_repo = chunk_repository
        elif isinstance(repository, UnavailableUserRepository):
            chunks_repo = UnavailableChunkRepository(repository.cause)
        else:
            try:
                chunks_repo = MongoChunkRepository(mongo.database())
            except Exception as exc:  # pragma: no cover - mirrors the other paths
                logger.error(
                    "chunks.repository_unavailable",
                    extra={"error": "could not reach the database at startup"},
                    exc_info=True,
                )
                chunks_repo = UnavailableChunkRepository(exc)

        # One provider shared by ingestion and retrieval: it owns an httpx client
        # and a token counter, and two instances would double the connections and
        # split the accounting that Phase 05 added for quota safety.
        raw_embeddings = embeddings or _build_embedding_provider(settings)
        raw_llm = llm or _build_llm_provider(settings)
        recorder = UsageRecorder(usage_repo)
        # The null providers report arithmetic on string lengths as tokens, so
        # recording them would fill the quota ledger with noise that looks like
        # real spend.
        embedding_provider = (
            raw_embeddings
            if settings.ai_provider == "null"
            else UsageTrackingProvider(raw_embeddings, recorder)
        )
        answer_provider = (
            raw_llm if settings.ai_provider == "null" else UsageTrackingLLM(raw_llm, recorder)
        )

        process = ProcessDocument(
            documents=documents_repo,
            chunks=chunks_repo,
            storage=file_storage,
            embeddings=embedding_provider,
            chunk_size=settings.chunk_size,
            chunk_overlap=settings.chunk_overlap,
            min_chunk_size=settings.min_chunk_size,
            task_type=settings.gemini_embedding_task_document,
            max_embed_tokens=settings.gemini_embedding_max_input_tokens,
        )
        worker = IngestionWorker(
            documents=documents_repo,
            process=process,
            poll_interval_seconds=settings.processing_poll_interval_seconds,
            max_attempts=settings.processing_max_attempts,
            stale_after_minutes=settings.processing_stale_after_minutes,
        )
        app.state.ingestion_worker = worker
        app.state.chunk_repository = chunks_repo

        app.state.semantic_search = SemanticSearch(
            chunks=chunks_repo,
            embeddings=embedding_provider,
            index_name=settings.vector_index_name,
            task_type=settings.gemini_embedding_task_query,
            max_embed_tokens=settings.gemini_embedding_max_input_tokens,
            top_k=settings.retrieval_top_k,
            min_score=settings.retrieval_min_score,
            context_max_characters=settings.context_max_chars,
        )

        app.state.ask_question = AskQuestion(
            search=app.state.semantic_search,
            llm=answer_provider,
            conversations=conversations_repo,
            top_k=settings.retrieval_top_k,
            min_score=settings.retrieval_min_score,
            history_max_messages=settings.llm_history_max_messages,
            max_question_characters=settings.llm_max_question_characters,
        )
        app.state.start_conversation = StartConversation(conversations_repo)
        app.state.list_conversations = ListConversations(conversations_repo)
        app.state.get_conversation_history = GetConversationHistory(conversations_repo)
        app.state.delete_conversation = DeleteConversation(conversations_repo)
        app.state.get_usage = GetUsage(usage_repo)
        app.state.conversation_repository = conversations_repo
        app.state.usage_repository = usage_repo

        await _ensure_indexes(
            repository,
            workspaces_repo,
            documents_repo,
            chunks_repo,
            conversations_repo,
            usage_repo,
        )

        # Per-user sliding window on the provider-quota endpoints. `clock` is
        # injectable so the limiter can be tested against time passing without
        # the test sleeping.
        app.state.rate_limiter = RateLimiter(
            limit=settings.ai_rate_limit_per_minute,
            window_seconds=60.0,
            clock=clock,
            max_keys=settings.rate_limit_max_keys,
        )

        # The same window over the credential endpoints, keyed by client address
        # instead of by principal because a caller signing in has no principal yet.
        app.state.auth_rate_limiter = RateLimiter(
            limit=settings.auth_rate_limit_per_minute,
            window_seconds=60.0,
            clock=clock,
            max_keys=settings.rate_limit_max_keys,
        )

        # The vector index is created here so a fresh Atlas cluster needs no
        # manual step. `database()` raises when there is no client at all, and
        # index creation can fail for many reasons; neither may stop startup.
        app.state.vector_index_ready = False
        try:
            app.state.vector_index_ready = await ensure_vector_index(
                mongo.database(),
                index_name=settings.vector_index_name,
                dimensions=settings.gemini_embedding_dimensions,
                similarity=settings.vector_similarity,
            )
        except Exception:
            logger.error("vector_index.startup_check_failed", exc_info=True)

        stop = asyncio.Event()
        task: asyncio.Task | None = None
        # Explicit argument wins, so tests can run the worker without a
        # background task racing their assertions.
        run_worker = (
            settings.processing_enabled if processing_enabled is None else processing_enabled
        )
        if run_worker:
            await worker.reclaim_abandoned()
            task = build_worker_task(worker, stop)

        try:
            yield
        finally:
            stop.set()
            if task is not None:
                task.cancel()
                # Shutdown must not raise; the process is going away anyway, and a
                # cancelled worker is the expected outcome, not a failure.
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
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
        # Set here rather than on the raised error: FastAPI runs exception
        # handlers inside this middleware, so the 429 reaches it as a normal
        # response and carries the headers. It also publishes the remaining
        # budget on successful calls, which is how a client paces itself without
        # waiting to be refused.
        decision = getattr(request.state, "rate_limit", None)
        if decision is not None:
            for header, value in decision.headers.items():
                response.headers[header] = value
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
    app.include_router(workspaces_router)
    app.include_router(documents_router)
    app.include_router(search_router)
    app.include_router(query_router)
    app.include_router(ui_router)

    @app.get("/", include_in_schema=False)
    async def index(request: Request) -> Response:
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={"app_name": "KnowledgeDock", "version": __version__},
        )

    return app


async def _ensure_indexes(
    repository,
    workspace_repository=None,
    document_repository=None,
    chunk_repository=None,
    conversation_repository=None,
    usage_repository=None,
) -> None:
    """Create indexes, tolerating failure.

    A missing unique index would allow duplicate accounts, so the failure is
    logged loudly — but it must not stop the process binding `$PORT`. The same
    reasoning as the Mongo connection: report the truth, keep serving.
    """
    try:
        await repository.ensure_indexes()
        if workspace_repository is not None:
            await workspace_repository.ensure_indexes()
        if document_repository is not None:
            await document_repository.ensure_indexes()
        if chunk_repository is not None:
            await chunk_repository.ensure_indexes()
        if usage_repository is not None:
            await usage_repository.ensure_indexes()
        if conversation_repository is not None:
            await conversation_repository.ensure_indexes()
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
        authorization = request.headers.get("authorization", "")
        scheme, _, candidate = authorization.partition(" ")
        if scheme.lower() == "bearer" and candidate:
            token = candidate
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


def _filesize(value: int | None) -> str:
    """Human-readable byte count for the document table.

    Decimal units, because that is how file sizes are labelled everywhere else in
    the industry. A binary reading that disagreed with the number in the OS file
    manager would read as a bug.
    """
    size = float(value or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1000 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1000
    return f"{size:.1f} GB"


def _short_datetime(value) -> str:
    """A compact local timestamp for conversation rows.

    Rendered in UTC rather than converted: the browser already knows the user's
    offset, and this avoids shipping a timezone assumption through to the client.
    """
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%d %b %Y, %H:%M UTC")


def _build_llm_provider(settings: Settings):
    """Choose the generation implementation from configuration.

    `AI_PROVIDER=null` selects the deterministic local provider, so the whole RAG
    path runs without an API key. Same switch as embeddings: one variable decides
    whether this process talks to Gemini or to nothing.
    """
    if settings.ai_provider == "null":
        return NullLLMProvider()
    return GeminiChatProvider(
        api_key=settings.gemini_api_key,
        model=settings.gemini_chat_model,
        temperature=settings.gemini_chat_temperature,
        max_output_tokens=settings.gemini_chat_max_output_tokens,
        timeout_seconds=settings.ai_timeout_seconds,
        max_retries=settings.ai_max_retries,
        backoff_seconds=settings.ai_retry_backoff_seconds,
    )


def _build_embedding_provider(settings: Settings):
    """Choose the embedding implementation from configuration.

    `AI_PROVIDER=null` selects the deterministic local provider, which is what
    makes the whole ingestion pipeline runnable — and testable — without an API
    key or any quota.
    """
    if settings.ai_provider == "null":
        return NullEmbeddingProvider(dimensions=settings.gemini_embedding_dimensions)
    return GeminiEmbeddingProvider(
        api_key=settings.gemini_api_key,
        model=settings.gemini_embedding_model,
        dimensions=settings.gemini_embedding_dimensions,
        timeout_seconds=settings.ai_timeout_seconds,
        max_retries=settings.ai_max_retries,
        backoff_seconds=settings.ai_retry_backoff_seconds,
        requests_per_minute=settings.ai_embedding_requests_per_minute,
        burst=settings.ai_embedding_burst,
    )
