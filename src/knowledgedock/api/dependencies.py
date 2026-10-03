"""Session cookie handling and the authentication dependency.

The session travels in an HttpOnly cookie so HTMX forms need no JavaScript and no
token is ever reachable from `document.cookie`. `Secure` follows the environment:
required over HTTPS on Render, disabled for plain-HTTP local development where a
`Secure` cookie would simply never be sent.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request, Response

from knowledgedock.application.auth.use_cases import ResolveCurrentUser
from knowledgedock.domain.errors import AuthenticationFailed
from knowledgedock.domain.users import User

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
