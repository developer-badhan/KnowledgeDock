"""Registration, login and password reset.

Each class owns one business rule end to end. They take their collaborators
through the constructor so tests can substitute an in-memory repository and a
hasher with known cost, and so nothing here reaches for a global.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from knowledgedock.application.auth.policies import normalize_email, validate_password
from knowledgedock.domain.errors import (
    AuthenticationFailed,
    Conflict,
    NotFound,
    ValidationFailed,
)
from knowledgedock.domain.users import PasswordResetToken, User, utcnow
from knowledgedock.infrastructure.repositories.user_repository import (
    DuplicateEmailError,
    UserRepository,
)
from knowledgedock.infrastructure.security.passwords import PasswordHasher
from knowledgedock.infrastructure.security.tokens import (
    TokenService,
    generate_reset_token,
    hash_reset_token,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class IssuedSession:
    """What the caller needs to establish a session, and nothing more."""

    token: str
    expires_in_seconds: int
    user: User


@dataclass(frozen=True, slots=True)
class PasswordResetRequest:
    """Outcome of a reset request.

    `delivered_token` is for the **server**, never for the response. KnowledgeDock
    has no mail provider, and inventing one is exactly the kind of infrastructure
    `SKILL.md` §3 warns against. Outside production the plaintext is written to
    the log so the flow is demonstrable end to end; in production it exists only
    inside the delivered message.

    It is deliberately kept out of the HTTP body. Returning it for a known
    address and omitting it for an unknown one would turn the endpoint into an
    enumeration oracle, which `test_request_looks_identical_for_an_unknown_address`
    catches.
    """

    accepted: bool
    delivered_token: str | None = None


class RegisterUser:
    def __init__(
        self, repository: UserRepository, hasher: PasswordHasher, *, minimum_password_length: int
    ) -> None:
        self._repository = repository
        self._hasher = hasher
        self._minimum_length = minimum_password_length

    async def execute(self, email: str, password: str) -> User:
        normalized = normalize_email(email)
        validate_password(password, minimum_length=self._minimum_length)

        # Cheap pre-check. The unique index is still the real guarantee, because
        # two simultaneous registrations can both pass this point.
        if await self._repository.find_by_email(normalized) is not None:
            raise Conflict("An account with that email already exists.")

        now = utcnow()
        user = User(
            id=uuid4(),
            email=normalized,
            password_hash=self._hasher.hash(password),
            created_at=now,
            updated_at=now,
            session_version=1,
            is_active=True,
        )
        try:
            return await self._repository.create(user)
        except DuplicateEmailError as exc:
            raise Conflict("An account with that email already exists.") from exc


class AuthenticateUser:
    def __init__(
        self, repository: UserRepository, hasher: PasswordHasher, tokens: TokenService
    ) -> None:
        self._repository = repository
        self._hasher = hasher
        self._tokens = tokens

    async def execute(self, email: str, password: str) -> IssuedSession:
        normalized = normalize_email(email)
        user = await self._repository.find_by_email(normalized)

        if user is None:
            # Still spend the hashing cost. Returning immediately would make the
            # response time reveal whether the address is registered.
            self._hasher.verify(password, _dummy_hash_value())
            raise AuthenticationFailed()

        if not self._hasher.verify(password, user.password_hash):
            raise AuthenticationFailed(detail=f"wrong password for user {user.id}")

        if not user.is_authenticatable:
            raise AuthenticationFailed(detail=f"user {user.id} is not active")

        if self._hasher.needs_rehash(user.password_hash):
            logger.info("auth.password_rehash_scheduled", extra={"user_id": str(user.id)})

        return IssuedSession(
            token=self._tokens.issue_session(user.id, user.session_version),
            expires_in_seconds=self._tokens.expire_seconds,
            user=user,
        )


class RequestPasswordReset:
    def __init__(
        self,
        repository: UserRepository,
        *,
        expire_minutes: int,
        expose_token: bool,
    ) -> None:
        self._repository = repository
        self._expire_minutes = expire_minutes
        self._expose_token = expose_token

    async def execute(self, email: str) -> PasswordResetRequest:
        """Always report success, whether or not the address is registered.

        A response that differs for unknown addresses is an enumeration oracle.
        The genuine reason goes to the log, not to the client.
        """
        try:
            normalized = normalize_email(email)
        except ValidationFailed:
            # Malformed input is still answered identically.
            return PasswordResetRequest(accepted=True)

        user = await self._repository.find_by_email(normalized)
        if user is None or not user.is_authenticatable:
            logger.info(
                "auth.reset_requested_unknown_email",
                extra={"email_hash": hash_reset_token(normalized)[:16]},
            )
            return PasswordResetRequest(accepted=True)

        plaintext, digest = generate_reset_token()
        await self._repository.store_reset_token(
            PasswordResetToken(
                user_id=user.id,
                token_hash=digest,
                expires_at=utcnow() + timedelta(minutes=self._expire_minutes),
            )
        )
        log = logger.info if self._expose_token else logger.warning
        log(
            "auth.reset_requested",
            extra={
                "user_id": str(user.id),
                "expires_in_minutes": self._expire_minutes,
                # Outside production this is the stand-in for a mail provider.
                # In production it marks the missing delivery so the gap is
                # visible in the logs rather than silently swallowing resets.
                "delivery": "log-only" if self._expose_token else "UNCONFIGURED",
                "reset_token": plaintext if self._expose_token else None,
            },
        )
        return PasswordResetRequest(
            accepted=True,
            delivered_token=plaintext if self._expose_token else None,
        )


class ConfirmPasswordReset:
    def __init__(
        self,
        repository: UserRepository,
        hasher: PasswordHasher,
        *,
        minimum_password_length: int,
    ) -> None:
        self._repository = repository
        self._hasher = hasher
        self._minimum_length = minimum_password_length

    async def execute(self, token: str, new_password: str) -> User:
        validate_password(new_password, minimum_length=self._minimum_length)

        digest = hash_reset_token(token)
        # Consuming is atomic, so a replayed token is already gone by the time a
        # second request looks for it.
        record = await self._repository.consume_reset_token(digest)
        if record is None:
            raise NotFound("That reset link is invalid or has already been used.")

        if record.is_expired():
            raise ValidationFailed("That reset link has expired. Request a new one.")

        new_version = await self._repository.replace_password(
            record.user_id, self._hasher.hash(new_password)
        )
        if new_version is None:
            # The account was deleted between the request and the confirmation.
            raise NotFound("That reset link is invalid or has already been used.")

        logger.info(
            "auth.password_reset_completed",
            extra={"user_id": str(record.user_id), "session_version": new_version},
        )
        user = await self._repository.find_by_id(record.user_id)
        if user is None:
            raise NotFound("That reset link is invalid or has already been used.")
        return user


class ResolveCurrentUser:
    """Turns a session token into a user, or raises `AuthenticationFailed`."""

    def __init__(self, repository: UserRepository, tokens: TokenService) -> None:
        self._repository = repository
        self._tokens = tokens

    async def execute(self, token: str) -> User:
        session = self._tokens.decode_session(token)
        if session is None:
            raise AuthenticationFailed("Your session is invalid or has expired.")

        user = await self._repository.find_by_id(session.user_id)
        if user is None:
            raise AuthenticationFailed("Your session is invalid or has expired.")

        # The revocation check. A password change or reset bumps session_version,
        # so every token minted before it stops validating here — which a purely
        # stateless token cannot do on its own.
        if user.session_version != session.session_version:
            raise AuthenticationFailed(
                detail=f"stale session for user {user.id}",
            )

        if not user.is_authenticatable:
            raise AuthenticationFailed(detail=f"user {user.id} is not active")

        return user


# A genuine argon2id hash of a value nobody can guess. It exists so a login for
# an unregistered address performs the same hashing work as one with a wrong
# password; returning early would make response time reveal whether an address is
# registered. Lazily initialized to avoid import-time Argon2id cost.
_dummy_hash: str | None = None


def _dummy_hash_value() -> str:
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = PasswordHasher().hash("knowledgedock-timing-equaliser")
    return _dummy_hash
