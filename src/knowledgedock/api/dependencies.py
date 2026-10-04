"""Session cookie handling and the authentication and authorization dependencies.

The session travels in an HttpOnly cookie so HTMX forms need no JavaScript and no
token is ever reachable from `document.cookie`. `Secure` follows the environment:
required over HTTPS on Render, disabled for plain-HTTP local development where a
`Secure` cookie would simply never be sent.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import Depends, Path, Request, Response

from knowledgedock.application.auth.use_cases import ResolveCurrentUser
from knowledgedock.application.workspaces.use_cases import AuthorizeWorkspace
from knowledgedock.domain.errors import (
    AuthenticationFailed,
    PermissionDenied,
    RateLimited,
    ServiceUnavailable,
)
from knowledgedock.domain.users import User
from knowledgedock.domain.workspaces import WorkspaceAccess
from knowledgedock.infrastructure.rate_limit import principal_key

SESSION_COOKIE_NAME = "kd_session"
# Cross-site POSTs are exactly what login and logout are, so every cookie is
# SameSite=Lax. `None` would require Secure and buys nothing here.
SESSION_COOKIE_SAMESITE = "lax"


def set_session_cookie(response: Response, token: str, *, max_age: int, secure: bool) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=max_age,
        httponly=True,
        secure=secure,
        samesite=SESSION_COOKIE_SAMESITE,
        path="/",
    )


def clear_session_cookie(response: Response, *, secure: bool) -> None:
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        httponly=True,
        secure=secure,
        samesite=SESSION_COOKIE_SAMESITE,
        path="/",
    )


def get_resolver(request: Request) -> ResolveCurrentUser:
    return request.app.state.resolve_current_user


async def get_current_user(
    request: Request,
    resolver: Annotated[ResolveCurrentUser, Depends(get_resolver)],
) -> User:
    """Resolve the caller, or raise `AuthenticationFailed`.

    Reads the cookie first. The `Authorization: Bearer` header is accepted as a
    second path so API consumers are not forced through a browser cookie jar;
    the cookie takes precedence because that is what the HTMX UI sends.
    """
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        authorization = request.headers.get("authorization", "")
        scheme, _, candidate = authorization.partition(" ")
        if scheme.lower() == "bearer" and candidate:
            token = candidate

    if not token:
        raise AuthenticationFailed("You must be signed in to do that.")

    return await resolver.execute(token)


# --------------------------------------------------------------------------
# Workspace authorization
# --------------------------------------------------------------------------
def require_configured_ai(request: Request) -> None:
    """Refuse retrieval and generation when `AI_PROVIDER=null`.

    The null providers are deterministic *stand-ins*: `NullEmbeddingProvider`
    derives vectors from a hash of the text, and `NullLLMProvider` quotes the
    first line of the context it was handed. That makes the whole pipeline
    runnable and testable without an API key, which is their purpose.

    It also means their output carries no meaning. A search would rank documents
    by hash similarity and a query would return an answer assembled from
    whichever chunk hashed closest -- confident, well-formed, and arbitrary. That
    is worse than an error, because nothing in the response distinguishes it from
    a real answer.

    So the stand-ins stay wired up for ingestion, where they exercise the
    document lifecycle, and the two endpoints that would *answer* a person refuse
    with 503 instead. Refusing is the honest outcome: the configuration is
    incomplete, not temporarily broken, and the message says so.
    """
    settings = getattr(request.app.state, "settings", None)
    if settings is not None and settings.ai_provider == "null":
        # The remedy goes in `message`, not `detail`: `detail` carries internal
        # context and is logged rather than returned, so a hint parked there would
        # never reach the person who needs it.
        raise ServiceUnavailable(
            "Answering is not configured. AI_PROVIDER is 'null', which supplies "
            "placeholder results rather than real ones. Set AI_PROVIDER=gemini to "
            "enable /search and /query."
        )


def get_authorize(request: Request) -> AuthorizeWorkspace:
    return request.app.state.authorize_workspace


async def require_workspace_access(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    authorize: Annotated[AuthorizeWorkspace, Depends(get_authorize)],
    workspace_id: Annotated[UUID, Path()],
) -> WorkspaceAccess:
    """Prove the caller may act inside this workspace.

    This is the isolation boundary for every workspace-scoped resource. Documents
    and conversations will depend on this rather than re-deriving access, so there
    is exactly one place where "does this user belong here" is answered.

    Depends on `get_current_user`, so an anonymous request is rejected with 401
    before the workspace is even looked up — membership is never consulted on
    behalf of someone who is not signed in.
    """
    access = await authorize.execute(workspace_id, user)
    # Stash it so handlers and templates do not have to re-query.
    request.state.workspace_access = access
    return access


async def require_workspace_owner(
    access: Annotated[WorkspaceAccess, Depends(require_workspace_access)],
) -> WorkspaceAccess:
    """Narrow `require_workspace_access` to owners only."""
    if not access.is_owner:
        raise PermissionDenied("Only the workspace owner can do that.")
    return access


def enforce_rate_limit(request: Request, route: str) -> None:
    """Count this request against the caller's quota and refuse when over.

    Only the endpoints that spend provider quota are limited. Applying it
    everywhere would rate-limit the frontend's asset requests and the health
    checks Render uses to decide the service is alive, which would turn a
    deliberate limiter into an outage.

    The refusal carries `Retry-After`, so a well-behaved client backs off instead
    of retrying into the same wall.
    """
    limiter = getattr(request.app.state, "rate_limiter", None)
    if limiter is None or not limiter.limit:
        return
    user = getattr(request.state, "user", None)
    workspace_id = None
    path_id = request.path_params.get("workspace_id")
    if path_id:
        try:
            workspace_id = UUID(str(path_id))
        except ValueError:
            workspace_id = None
    key = principal_key(
        user_id=user.id if user is not None else None,
        workspace_id=workspace_id,
        route=route,
    )
    decision = limiter.check(key)
    request.state.rate_limit = decision
    if not decision.allowed:
        raise RateLimited(
            "Too many requests for this workspace. The AI provider's free tier is "
            "rate limited, so the limit protects everyone's quota.",
            detail=f"retry_after={decision.retry_after}s",
        )
