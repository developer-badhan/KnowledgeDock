"""The HTML surface: dashboard, documents, ask, conversation history.

Deliberately a *rendering* layer only. Every action here calls the same use cases
the JSON API calls — `upload_document`, `ask_question`, `list_documents` — so
there is exactly one implementation of every rule. The upload form goes through
`UploadDocument`, which means a form upload and an API upload are the same
operation with the same validation, not a looser second path.

Routes live under `/ui` and are excluded from the schema. That keeps the JSON API
documented as the product surface, matching Phase 01 decision 9.

Workspace selection is a path segment rather than a cookie or a query parameter.
Phase 03 made the workspace the only access boundary, and a path segment is the
one form the dependency already validates. A cookie would need its own validation
and its own way to be wrong.

Fragments are served as `text/html` partials for HTMX. No JSON is rendered into a
page and parsed by script; the server decides what the fragment contains, so
there is no second template for the same view.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse

from knowledgedock import __version__
from knowledgedock.api.dependencies import (
    enforce_rate_limit,
    get_current_user,
    require_configured_ai,
    require_workspace_access,
)
from knowledgedock.application.documents.use_cases import (
    DeleteDocument,
    ListDocuments,
    UploadDocument,
)
from knowledgedock.application.rag.answer_question import (
    AskQuestion,
    DeleteConversation,
    GetConversationHistory,
    ListConversations,
    StartConversation,
)
from knowledgedock.application.workspaces.use_cases import ListWorkspaces
from knowledgedock.domain.conversation import Answer, Message
from knowledgedock.domain.documents import Document
from knowledgedock.domain.errors import AppError, NotFound, ValidationFailed
from knowledgedock.domain.users import User
from knowledgedock.domain.workspaces import Workspace, WorkspaceAccess
from knowledgedock.infrastructure.security.errors import AiProviderError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ui"], include_in_schema=False)

Access = Annotated[WorkspaceAccess, Depends(require_workspace_access)]
CurrentUser = Annotated[User, Depends(get_current_user)]


# Each dependency is a named function with an annotated `request: Request`.
# A bare `lambda r: r.app.state.x` cannot carry that annotation, so FastAPI
# cannot tell `r` is the ASGI request and instead demands it from the caller --
# which surfaces as a 422 asking for a phantom field.
def get_list_workspaces(request: Request) -> ListWorkspaces:
    return request.app.state.list_workspaces


async def get_active_workspace(
    request: Request,
    access: Access,
    user: CurrentUser,
) -> Workspace:
    """Resolve the path workspace through the same authorisation as the API.

    Returning the `Workspace` rather than the use case keeps the routes below
    reading like templates rather than plumbing. Authorisation still happens
    exactly once, here, through `GetWorkspace`.
    """
    return await request.app.state.get_workspace.execute(access.workspace_id, user)


def get_documents(request: Request) -> ListDocuments:
    return request.app.state.list_documents


def get_delete_document_use_case(request: Request) -> DeleteDocument:
    return request.app.state.delete_document


def get_upload(request: Request) -> UploadDocument:
    return request.app.state.upload_document


def get_delete(request: Request) -> DeleteDocument:
    return request.app.state.delete_document


def get_ask_question(request: Request) -> AskQuestion:
    return request.app.state.ask_question


def get_start_conversation(request: Request) -> StartConversation:
    return request.app.state.start_conversation


def get_list_conversations(request: Request) -> ListConversations:
    return request.app.state.list_conversations


def get_history(request: Request) -> GetConversationHistory:
    return request.app.state.get_conversation_history


def get_delete_conversation(request: Request) -> DeleteConversation:
    return request.app.state.delete_conversation


ListWorkspacesDep = Annotated[ListWorkspaces, Depends(get_list_workspaces)]
UploadDep = Annotated[UploadDocument, Depends(get_upload)]
DeleteDep = Annotated[DeleteDocument, Depends(get_delete_document_use_case)]
WorkspaceDep = Annotated[Workspace, Depends(get_active_workspace)]
AskDep = Annotated[AskQuestion, Depends(get_ask_question)]
ListConvDep = Annotated[ListConversations, Depends(get_list_conversations)]
HistoryDep = Annotated[GetConversationHistory, Depends(get_history)]
DeleteConvDep = Annotated[DeleteConversation, Depends(get_delete_conversation)]


#: Matches the marketing page, so the navbar, footer and titles agree.
APP_NAME = "KnowledgeDock"


def _base_context(request: Request, **extra: Any) -> dict[str, Any]:
    settings = request.app.state.settings
    context = {
        "request": request,
        "app_name": APP_NAME,
        "version": __version__,
        "is_production": settings.is_production,
        # base.html gates the nav "Ask" link on this. It is derived from the path
        # so any /w/{workspace_id}/... page lights it up, and callers holding an
        # active workspace override it through the kwargs below.
        "workspace_id": request.path_params.get("workspace_id"),
    }
    context.update(extra)
    return context


async def _require_user(request: Request) -> User | None:
    return getattr(request.state, "user", None)


def _render(request: Request, name: str, status_code: int = 200, **context: Any) -> Response:
    return request.app.state.templates.TemplateResponse(
        request=request,
        name=name,
        context=_base_context(request, **context),
        status_code=status_code,
    )


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------
@router.get("/app")
async def dashboard(
    request: Request,
    user: CurrentUser,
    list_workspaces: ListWorkspacesDep,
) -> Response:
    workspaces = await list_workspaces.execute(user)
    return await _dashboard(request, user, workspaces)


@router.post("/ui/workspaces", include_in_schema=False)
async def ui_create_workspace(
    request: Request,
    user: CurrentUser,
    list_workspaces: ListWorkspacesDep,
    # Defaulted rather than required: Starlette reads an empty form value as an
    # absent one, so a required field answers a blank submit with a bare JSON 422.
    # Letting it through puts the blank case in front of the use case, which
    # refuses it in the user's own words and re-renders this page.
    name: Annotated[str, Form()] = "",
) -> Response:
    """Create a workspace from the form.

    `POST /workspaces` was the only way to make one, so a new account landed on a
    page reading "Create a workspace to start" with nothing on it to comply with.
    Plain form POST, no HTMX: the empty state is the first thing anyone sees and it
    has to work with JavaScript still loading.
    """
    try:
        workspace = await request.app.state.create_workspace.execute(user, name)
    except ValidationFailed as exc:
        workspaces = await list_workspaces.execute(user)
        return await _dashboard(
            request,
            user,
            workspaces,
            workspace_error=exc.message,
            workspace_name=name,
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    logger.info(
        "workspace.created",
        extra={"workspace_id": str(workspace.id), "via": "form"},
    )
    # Straight into the new workspace, which is the whole point of making it.
    return RedirectResponse(f"/w/{workspace.id}", status_code=status.HTTP_303_SEE_OTHER)


async def _dashboard(
    request: Request,
    user: User,
    workspaces: list[Workspace],
    *,
    workspace_error: str | None = None,
    workspace_name: str = "",
    status_code: int = 200,
) -> Response:
    if not workspaces:
        return _render(
            request,
            "dashboard.html",
            workspaces=[],
            active=None,
            documents=None,
            workspace_error=workspace_error,
            workspace_name=workspace_name,
            status_code=status_code,
        )
    requested = request.query_params.get("workspace")
    active = next((w for w in workspaces if str(w.id) == requested), workspaces[0])
    return await _workspace_view(
        request,
        user,
        active,
        workspaces,
        workspace_error=workspace_error,
        workspace_name=workspace_name,
        status_code=status_code,
    )


@router.get("/w/{workspace_id}")
async def workspace_dashboard(
    request: Request,
    user: CurrentUser,
    access: Access,
    active: WorkspaceDep,
    list_workspaces: ListWorkspacesDep,
) -> Response:
    """A workspace by id, rather than a remembered 'current one'.

    Explicit and bookmarkable: a shared link opens the same workspace for a
    colleague, and a reload never lands somewhere unexpected.
    """
    return await _workspace_view(request, user, active, await list_workspaces.execute(user))


async def _workspace_view(
    request: Request,
    user: User,
    active: Workspace,
    workspaces: list[Workspace],
    *,
    workspace_error: str | None = None,
    workspace_name: str = "",
    status_code: int = 200,
) -> Response:
    page = await request.app.state.list_documents.execute(active.id, limit=50)
    return _render(
        request,
        "dashboard.html",
        workspaces=workspaces,
        active=active,
        documents=page.items,
        total=page.total,
        # base.html builds the nav "Ask" link from this.
        workspace_id=str(active.id),
        workspace_error=workspace_error,
        workspace_name=workspace_name,
        status_code=status_code,
    )


# --------------------------------------------------------------------------
# Documents
# --------------------------------------------------------------------------
def _document_row(request: Request, document: Document, active: Workspace) -> str:
    return request.app.state.templates.get_template("_document_row.html").render(
        **_base_context(request, document=document, active=active)
    )


@router.get("/ui/workspaces/{workspace_id}/documents", response_class=HTMLResponse)
async def document_rows(
    request: Request,
    access: Access,
    workspace: WorkspaceDep,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
) -> HTMLResponse:
    """The list as a fragment, for filtering without a full page reload."""
    page = await request.app.state.list_documents.execute(access.workspace_id, limit=100)
    documents = page.items
    if status_filter:
        documents = [d for d in documents if d.status.value == status_filter]
    return HTMLResponse("\n".join(_document_row(request, d, workspace) for d in documents))


@router.post("/ui/workspaces/{workspace_id}/documents")
async def ui_upload(
    request: Request,
    access: Access,
    user: CurrentUser,
    upload: UploadDep,
    file: Annotated[UploadFile, File()],
) -> Response:
    """Upload through the same use case the JSON API uses.

    Redirect rather than swap: after an upload the user wants to see the document
    list with the new row in it, which is a different fragment from the upload
    form. Returning the row alone would leave the form showing a stale state.
    """
    try:
        await upload.execute(
            access,
            user,
            filename=file.filename,
            content_type=file.content_type,
            source=file.file,
        )
    except AppError as exc:
        logger.info("ui.upload_rejected", extra={"code": exc.code.value})
        return _render(
            request,
            "_upload_error.html",
            active=None,
            message=exc.message,
            status_code=exc.status_code,
        )
    if request.headers.get("HX-Request"):
        # A 303 here is followed by htmx inside the XHR, and what comes back is the
        # whole dashboard document -- which then gets swapped into #uploadFeedback,
        # a one-line div. The user saw a blank dialog and no confirmation.
        # HX-Redirect is htmx's own instruction to navigate the browser for real,
        # so they land on the dashboard with the new row actually in the table.
        response = HTMLResponse("", status_code=status.HTTP_200_OK)
        response.headers["HX-Redirect"] = f"/w/{access.workspace_id}"
        return response
    # A plain form post, with JavaScript unavailable or not yet loaded.
    return RedirectResponse(f"/w/{access.workspace_id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/ui/workspaces/{workspace_id}/documents/{document_id}/delete")
async def ui_delete_document(
    request: Request, access: Access, delete: DeleteDep, document_id: UUID
) -> Response:
    # Indistinguishable from a document in another workspace, as everywhere else.
    # A form cannot render "not found" differently without leaking, so both cases
    # simply return to the list.
    with contextlib.suppress(NotFound):
        await delete.execute(access.workspace_id, document_id)
    return RedirectResponse(f"/w/{access.workspace_id}", status_code=status.HTTP_303_SEE_OTHER)


# --------------------------------------------------------------------------
# Ask
# --------------------------------------------------------------------------
def get_ai_guard(request: Request) -> None:
    require_configured_ai(request)
    enforce_rate_limit(request, route="ui-ask")


AIGuard = Annotated[None, Depends(get_ai_guard)]


@router.get("/w/{workspace_id}/ask")
async def ask_page(
    request: Request,
    user: CurrentUser,
    access: Access,
    workspace: WorkspaceDep,
    list_workspaces: ListWorkspacesDep,
    conversation_id: UUID | None = None,
) -> Response:
    turns: list[Message] = []
    if conversation_id is not None:
        try:
            turns = await get_history(request).execute(access.workspace_id, conversation_id)
        except NotFound:
            conversation_id = None
    return _render(
        request,
        "ask.html",
        workspaces=await list_workspaces.execute(user),
        active=workspace,
        turns=turns,
        conversation_id=conversation_id,
        error=None,
    )


def _answer_fragment(request: Request, answer: Answer, workspace_id: UUID) -> str:
    # The serialised form, not the dataclass: one description of an answer's
    # shape for the template and the JSON response, rather than two that can
    # drift apart.
    return request.app.state.templates.get_template("_answer.html").render(
        **_base_context(request, answer=answer.to_dict(), active_id=workspace_id)
    )


@router.post("/ui/workspaces/{workspace_id}/ask", response_class=HTMLResponse)
async def ui_ask(
    request: Request,
    access: Access,
    user: CurrentUser,
    guard: AIGuard,
    use_case: AskDep,
    question: Annotated[str, Form()],
    conversation_id: Annotated[str | None, Form()] = None,
) -> Response:
    """Ask a question, returning the answer as a fragment.

    Both the main form and the follow-up form post through HTMX with the answer
    region as the swap target. The main form appends (``beforeend``) rather than
    replaces so a conversation accumulates; the client also appends an animated
    "thinking" turn for the tens of seconds a slow free-tier provider can take,
    then removes it when this fragment lands.
    """
    try:
        parsed = UUID(conversation_id) if conversation_id else None
    except ValueError:
        parsed = None
    try:
        answer = await use_case.execute(
            access.workspace_id, question, conversation_id=parsed, user_id=user.id
        )
    except AiProviderError:
        # An external AI outage is not the client's fault. The use case logs the
        # full failure; here it becomes a readable rejection the ask UI can swap
        # in, using the same fragment as every other ask failure. htmx 2 does not
        # swap 4xx/5xx by default, so app.js forces the swap for this target.
        logger.error(
            "ui.ask.provider_failed",
            extra={"workspace_id": str(access.workspace_id), "user_id": str(user.id)},
        )
        return HTMLResponse(
            _ask_error(
                request,
                "The AI provider is unavailable right now. Please try again in a moment.",
            ),
            status_code=status.HTTP_502_BAD_GATEWAY,
        )
    except (AppError, ValueError) as exc:
        # The JSON API rejects a blank question at the schema; a form post has no
        # schema, so it reaches the use case instead. Catching ValueError here is
        # what keeps a stray space from surfacing as a 500.
        message = exc.message if isinstance(exc, AppError) else str(exc)
        code = (
            exc.status_code if isinstance(exc, AppError) else status.HTTP_422_UNPROCESSABLE_CONTENT
        )
        return HTMLResponse(_ask_error(request, message), status_code=code)
    return HTMLResponse(_answer_fragment(request, answer, access.workspace_id))


def _ask_error(request: Request, message: str) -> str:
    return request.app.state.templates.get_template("_ask_error.html").render(
        **_base_context(request, message=message)
    )


@router.get("/ui/workspaces/{workspace_id}/conversations", response_class=HTMLResponse)
async def ui_list_conversations(
    request: Request, access: Access, use_case: ListConvDep
) -> HTMLResponse:
    items, total = await use_case.execute(access.workspace_id, limit=50)
    return HTMLResponse(
        request.app.state.templates.get_template("_conversation_list.html").render(
            **_base_context(
                request, conversations=items, total=total, active_workspace_id=access.workspace_id
            )
        )
    )


@router.post("/ui/workspaces/{workspace_id}/conversations/{conversation_id}/delete")
async def ui_delete_conversation(
    request: Request, access: Access, use_case: DeleteConvDep, conversation_id: UUID
) -> Response:
    with contextlib.suppress(NotFound):
        await use_case.execute(access.workspace_id, conversation_id)
    return RedirectResponse(f"/w/{access.workspace_id}/ask", status_code=status.HTTP_303_SEE_OTHER)
