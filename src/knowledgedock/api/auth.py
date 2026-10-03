"""Authentication routes.

Every handler here does the same three things: take input, call one use case,
shape the response. No hashing, no queries, no token construction. That is the
`SKILL.md` §4 boundary.

Each business action is exposed twice, deliberately:

  * `/auth/...` returns JSON for API consumers
  * `/ui/...` returns HTML for the HTMX forms

Both call the same use case object, so the two surfaces cannot disagree about what
a valid registration or a successful login is.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from knowledgedock.api.dependencies import (
    SESSION_COOKIE_NAME,
    clear_session_cookie,
    get_current_user,
    set_session_cookie,
)
from knowledgedock.api.schemas import (
    LoginRequest,
    MessageResponse,
    PasswordResetAccepted,
    PasswordResetConfirmBody,
    PasswordResetRequestBody,
    RegisterRequest,
    SessionResponse,
    UserResponse,
)
from knowledgedock.application.auth.use_cases import (
    AuthenticateUser,
    ConfirmPasswordReset,
    IssuedSession,
    RegisterUser,
    RequestPasswordReset,
)
from knowledgedock.core.config import Settings
from knowledgedock.domain.errors import AuthenticationFailed, Conflict, ValidationFailed
from knowledgedock.domain.users import User

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])


# --------------------------------------------------------------------------
# Dependency wiring. Each resolver reads the collaborator the app already built,
# so no route reaches into app.state directly.
# --------------------------------------------------------------------------
def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_register(request: Request) -> RegisterUser:
    return request.app.state.register_user


def get_authenticate(request: Request) -> AuthenticateUser:
    return request.app.state.authenticate_user


def get_reset_request(request: Request) -> RequestPasswordReset:
    return request.app.state.request_password_reset


def get_reset_confirm(request: Request) -> ConfirmPasswordReset:
    return request.app.state.confirm_password_reset


RegisterDep = Annotated[RegisterUser, Depends(get_register)]
AuthenticateDep = Annotated[AuthenticateUser, Depends(get_authenticate)]
ResetRequestDep = Annotated[RequestPasswordReset, Depends(get_reset_request)]
ResetConfirmDep = Annotated[ConfirmPasswordReset, Depends(get_reset_confirm)]
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
CurrentUser = Annotated[User, Depends(get_current_user)]


def to_user_response(user: User) -> UserResponse:
    return UserResponse(
        id=user.id,
        email=user.email,
        created_at=user.created_at.isoformat(),
    )


def _establish_session(response: Response, session: IssuedSession, settings: Settings) -> None:
    set_session_cookie(
        response,
        session.token,
        max_age=session.expires_in_seconds,
        secure=settings.is_production,
    )


# --------------------------------------------------------------------------
# JSON API
# --------------------------------------------------------------------------
@router.post(
    "/auth/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register(
    payload: RegisterRequest, response: Response, use_case: RegisterDep
) -> UserResponse:
    user = await use_case.execute(payload.email, payload.password)
    logger.info("auth.registered", extra={"user_id": str(user.id)})
    return to_user_response(user)


@router.post("/auth/login", response_model=SessionResponse)
async def login(
    payload: LoginRequest, response: Response, use_case: AuthenticateDep, settings: SettingsDep
) -> SessionResponse:
    session = await use_case.execute(payload.email, payload.password)
    _establish_session(response, session, settings)
    logger.info("auth.logged_in", extra={"user_id": str(session.user.id)})
    return SessionResponse(
        user=to_user_response(session.user),
        expires_in_seconds=session.expires_in_seconds,
    )


@router.post("/auth/logout", response_model=MessageResponse)
async def logout(response: Response, settings: SettingsDep) -> MessageResponse:
    # Returns a serialised model, so FastAPI merges the injected response's
    # headers — including Set-Cookie — into the final reply.
    clear_session_cookie(response, secure=settings.is_production)
    return MessageResponse(message="Signed out.")


@router.get("/auth/me", response_model=UserResponse)
async def me(user: CurrentUser) -> UserResponse:
    return to_user_response(user)


@router.post("/auth/password-reset/request", response_model=PasswordResetAccepted)
async def password_reset_request(
    body: PasswordResetRequestBody, use_case: ResetRequestDep
) -> PasswordResetAccepted:
    # The response is byte-identical whether or not the address exists. The reset
    # link goes to the log in development; it must never appear here.
    await use_case.execute(body.email)
    return PasswordResetAccepted(
        accepted=True,
        message="If that address has an account, a reset link has been sent.",
    )


@router.post("/auth/password-reset/confirm", response_model=MessageResponse)
async def password_reset_confirm(
    body: PasswordResetConfirmBody, use_case: ResetConfirmDep
) -> MessageResponse:
    await use_case.execute(body.token, body.new_password)
    return MessageResponse(message="Your password has been reset. Please sign in.")


# --------------------------------------------------------------------------
# HTML form surface for HTMX. Same use cases, different rendering.
# --------------------------------------------------------------------------
@router.get("/login", include_in_schema=False)
async def login_page(request: Request, registered: int = 0) -> Response:
    if request.cookies.get(SESSION_COOKIE_NAME):
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"error": None, "email": "", "registered": bool(registered)},
    )


@router.post("/ui/login", include_in_schema=False)
async def ui_login(
    request: Request,
    settings: SettingsDep,
    use_case: AuthenticateDep,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
) -> Response:
    try:
        session = await use_case.execute(email, password)
    except (AuthenticationFailed, ValidationFailed) as exc:
        return _reauth_page(request, "login.html", exc.message, email=email)
    logger.info("auth.logged_in", extra={"user_id": str(session.user.id), "via": "form"})
    # The cookie must be set on the response that is actually returned. Writing it
    # to FastAPI's injected `response` would be silently discarded, because that
    # object is only merged when the handler returns a serialised model.
    redirect = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    set_session_cookie(
        redirect,
        session.token,
        max_age=session.expires_in_seconds,
        secure=settings.is_production,
    )
    return redirect


@router.get("/register", include_in_schema=False)
async def register_page(request: Request) -> Response:
    return request.app.state.templates.TemplateResponse(
        request=request, name="register.html", context={"error": None, "email": ""}
    )


@router.post("/ui/register", include_in_schema=False)
async def ui_register(
    request: Request,
    use_case: RegisterDep,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
) -> Response:
    try:
        user = await use_case.execute(email, password)
    except (Conflict, ValidationFailed) as exc:
        return _reauth_page(request, "register.html", exc.message, email=email)
    logger.info("auth.registered", extra={"user_id": str(user.id), "via": "form"})
    # Not signed in automatically: the user goes through the login form, which
    # keeps one code path for establishing a session.
    return RedirectResponse("/login?registered=1", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/ui/logout", include_in_schema=False)
async def ui_logout(settings: SettingsDep) -> Response:
    response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    clear_session_cookie(response, secure=settings.is_production)
    return response


def _reauth_page(
    request: Request,
    template: str,
    error: str,
    *,
    email: str = "",
) -> HTMLResponse:
    """Re-render a form carrying the failure, rather than redirecting away.

    The submitted address is echoed back so the user does not retype it. The
    password never is.

    Template variables are passed as keyword arguments. Wrapping them in
    `context={...}` would create a single variable literally named `context`, and
    `{{ error }}` would silently render empty.
    """
    return HTMLResponse(
        content=request.app.state.templates.get_template(template).render(
            request=request,
            error=error,
            email=email,
            registered=False,
            password_min_length=request.app.state.settings.password_min_length,
        ),
        status_code=400,
    )
