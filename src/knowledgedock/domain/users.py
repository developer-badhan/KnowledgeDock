"""The User entity.

`session_version` is the mechanism that makes stateless JWTs revocable. Every
issued token embeds the version it was minted with; `get_current_user` rejects a
token whose version no longer matches the user. Bumping the column therefore logs
the user out everywhere at once — which is what a password change or reset must
do, and what a bare stateless token cannot express on its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class User:
    id: UUID
    email: str
    password_hash: str
    created_at: datetime
    updated_at: datetime
    session_version: int = 1
    is_active: bool = True

    @property
    def is_authenticatable(self) -> bool:
        return self.is_active

    def to_document(self) -> dict[str, Any]:
        """Serialise for MongoDB. `_id` is the UUID, never a generated ObjectId."""
        return {
            "_id": self.id,
            "email": self.email,
            "password_hash": self.password_hash,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "session_version": self.session_version,
            "is_active": self.is_active,
        }

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> User:
        return cls(
            id=document["_id"],
            email=document["email"],
            password_hash=document["password_hash"],
            created_at=document["created_at"],
            updated_at=document["updated_at"],
            session_version=document.get("session_version", 1),
            is_active=document.get("is_active", True),
        )


@dataclass(frozen=True, slots=True)
class PasswordResetToken:
    """A single-use password reset token.

    Only the SHA-256 digest is stored. A database leak therefore does not hand an
    attacker usable reset links, and `expires_at` carries a TTL index so MongoDB
    purges spent tokens on its own.
    """

    user_id: UUID
    token_hash: str
    expires_at: datetime
    created_at: datetime = field(default_factory=utcnow)

    def is_expired(self, now: datetime | None = None) -> bool:
        return (now or utcnow()) >= self.expires_at

    def to_document(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "token_hash": self.token_hash,
            "expires_at": self.expires_at,
            "created_at": self.created_at,
        }

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> PasswordResetToken:
        return cls(
            user_id=document["user_id"],
            token_hash=document["token_hash"],
            expires_at=document["expires_at"],
            created_at=document["created_at"],
        )
