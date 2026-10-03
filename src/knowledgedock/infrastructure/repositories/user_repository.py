"""User persistence.

Indexes are created from actual query patterns, not from the shape of the
document (`SKILL.md` §20). Two queries exist today:

  * find by email               -> unique index on `email`
  * find reset token by digest  -> unique index on `token_hash`, plus a TTL index
    on `expires_at` so MongoDB deletes spent tokens without a sweeper job

`session_version` and `is_active` are read on every authenticated request, but
always alongside `_id` via an email lookup during login, so indexing them would
add write cost for no query benefit.
"""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

from knowledgedock.domain.users import PasswordResetToken, User, utcnow


class DuplicateEmailError(Exception):
    """Raised when the unique email index rejects an insert."""


class UserRepository(Protocol):
    async def ensure_indexes(self) -> None: ...

    async def find_by_email(self, email: str) -> User | None: ...

    async def find_by_id(self, user_id: UUID) -> User | None: ...

    async def create(self, user: User) -> User: ...

    async def replace_password(self, user_id: UUID, password_hash: str) -> int | None:
        """Set a new password and invalidate every existing session.

        Returns the new `session_version`, or None if the user does not exist.
        """

    async def store_reset_token(self, token: PasswordResetToken) -> None: ...

    async def consume_reset_token(self, token_hash: str) -> PasswordResetToken | None: ...


class MongoUserRepository:
    def __init__(self, database: Any) -> None:
        self._users = database["users"]
        self._resets = database["password_reset_tokens"]

    async def ensure_indexes(self) -> None:
        await self._users.create_index(
            [("email", ASCENDING)], unique=True, name="users_email_unique"
        )
        await self._resets.create_index(
            [("token_hash", ASCENDING)], unique=True, name="resets_token_hash_unique"
        )
        # MongoDB removes documents once expires_at passes. No cleanup job.
        await self._resets.create_index(
            [("expires_at", ASCENDING)], expireAfterSeconds=0, name="resets_expires_ttl"
        )
        # Supports discarding a user's previous link when a new reset is requested.
        await self._resets.create_index(
            [("user_id", ASCENDING), ("created_at", DESCENDING)], name="resets_user_recent"
        )

    async def find_by_email(self, email: str) -> User | None:
        document = await self._users.find_one({"email": email})
        return self._as_user(document)

    async def find_by_id(self, user_id: UUID) -> User | None:
        document = await self._users.find_one({"_id": user_id})
        return self._as_user(document)

    async def create(self, user: User) -> User:
        try:
            await self._users.insert_one(user.to_document())
        except DuplicateKeyError as exc:
            # The unique index is the authority. Two concurrent registrations of
            # the same address race here and the loser must not overwrite the
            # winner; the pre-check in the use case is an optimisation, not the
            # guarantee. `SKILL.md` §5 requires duplicate detection.
            raise DuplicateEmailError from exc
        return user

    async def replace_password(self, user_id: UUID, password_hash: str) -> int | None:
        # $inc rather than $set: the new version is computed by the database, so
        # two concurrent resets cannot both set the same value and leave a
        # stale token valid.
        result = await self._users.find_one_and_update(
            {"_id": user_id},
            {
                "$set": {"password_hash": password_hash, "updated_at": utcnow()},
                "$inc": {"session_version": 1},
            },
            return_document=ReturnDocument.AFTER,
        )
        return result["session_version"] if result else None

    async def store_reset_token(self, token: PasswordResetToken) -> None:
        # One live reset per user: re-requesting replaces the previous link.
        await self._resets.delete_many({"user_id": token.user_id})
        await self._resets.insert_one(token.to_document())

    async def consume_reset_token(self, token_hash: str) -> PasswordResetToken | None:
        """Atomically read and delete a reset token.

        `find_one_and_delete` is the whole point: a token must be spendable
        exactly once even if two requests arrive together.
        """
        document = await self._resets.find_one_and_delete({"token_hash": token_hash})
        return PasswordResetToken.from_document(document) if document else None

    @staticmethod
    def _as_user(document: dict[str, Any] | None) -> User | None:
        """Plain function, not a coroutine.

        PyMongo's async collection methods return awaitables. Passing one into
        another coroutine yields a coroutine where a document is expected, which
        fails as a confusing subscript error rather than a missing `await`.
        """
        return User.from_document(document) if document else None


class InMemoryUserRepository:
    """Test double with the same semantics as `MongoUserRepository`.

    It reproduces only the behaviours the use cases actually depend on: the
    unique email constraint, single-use reset tokens, and the session-version
    bump. That keeps the auth suite in milliseconds without a database.
    """

    def __init__(self) -> None:
        self.users: dict[UUID, User] = {}
        self.resets: dict[str, PasswordResetToken] = {}

    async def ensure_indexes(self) -> None:
        return None

    async def find_by_email(self, email: str) -> User | None:
        return next((u for u in self.users.values() if u.email == email), None)

    async def find_by_id(self, user_id: UUID) -> User | None:
        return self.users.get(user_id)

    async def create(self, user: User) -> User:
        if await self.find_by_email(user.email) is not None:
            raise DuplicateEmailError
        self.users[user.id] = user
        return user

    async def replace_password(self, user_id: UUID, password_hash: str) -> int | None:
        existing = self.users.get(user_id)
        if existing is None:
            return None
        version = existing.session_version + 1
        self.users[user_id] = User(
            id=existing.id,
            email=existing.email,
            password_hash=password_hash,
            created_at=existing.created_at,
            updated_at=utcnow(),
            session_version=version,
            is_active=existing.is_active,
        )
        return version

    async def store_reset_token(self, token: PasswordResetToken) -> None:
        for digest, existing in list(self.resets.items()):
            if existing.user_id == token.user_id:
                del self.resets[digest]
        self.resets[token.token_hash] = token

    async def consume_reset_token(self, token_hash: str) -> PasswordResetToken | None:
        return self.resets.pop(token_hash, None)


class UnavailableUserRepository:
    """Stands in when the database could not be reached at startup.

    `MongoManager.connect()` deliberately does not raise, so the process still
    binds `$PORT` and `/health` stays green (Phase 01 decision). The consequence
    is that authentication may have no working repository. Rather than crash the
    process or pretend the user does not exist — which would turn an outage into
    "wrong email or password" — every call raises `ServiceUnavailable`, so the
    client is told the truth and the logs carry the reason.
    """

    _message = "The user store is unavailable. The database connection failed at startup."

    def __init__(self, cause: Exception | None = None) -> None:
        self.cause = cause

    async def ensure_indexes(self) -> None:
        return None

    async def find_by_email(self, email: str) -> User | None:
        raise self._error()

    async def find_by_id(self, user_id: UUID) -> User | None:
        raise self._error()

    async def create(self, user: User) -> User:
        raise self._error()

    async def replace_password(self, user_id: UUID, password_hash: str) -> int | None:
        raise self._error()

    async def store_reset_token(self, token: PasswordResetToken) -> None:
        raise self._error()

    async def consume_reset_token(self, token_hash: str) -> PasswordResetToken | None:
        raise self._error()

    def _error(self) -> Exception:
        from knowledgedock.domain.errors import ServiceUnavailable

        return ServiceUnavailable(self._message, detail=f"cause={self.cause!r}")
