"""Argon2id password hashing.

Memory cost is pinned rather than read from the environment. Argon2's cost is
only meaningful relative to the hardware it runs on, and KnowledgeDock has exactly
one deployment target: Render's free tier with 512 MB of RAM. 19 MiB per hash
satisfies the OWASP minimum for argon2id and leaves comfortable headroom for
concurrent requests, while the default 64 MiB would have been a quarter of the
whole container's budget for a single login.

Tuning this is a deployment decision, not a business one, so it lives here as a
named constant with its reasoning attached rather than as another env var.
"""

from __future__ import annotations

from argon2 import PasswordHasher as _Argon2Hasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type

# OWASP minimum for argon2id, sized for a 512 MB container.
MEMORY_COST_KIB = 19456
TIME_COST = 3
PARALLELISM = 2
HASH_LENGTH = 32
SALT_LENGTH = 16


class PasswordHasher:
    """Hash and verify passwords. Never compare raw passwords."""

    def __init__(self) -> None:
        self._hasher = _Argon2Hasher(
            time_cost=TIME_COST,
            memory_cost=MEMORY_COST_KIB,
            parallelism=PARALLELISM,
            hash_len=HASH_LENGTH,
            salt_len=SALT_LENGTH,
            type=Type.ID,
        )

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify(self, password: str, password_hash: str) -> bool:
        """Return whether the password matches.

        A malformed or missing hash is a failure, not an exception: a corrupt
        row must deny access rather than 500 the login endpoint.
        """
        try:
            return self._hasher.verify(password_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    def needs_rehash(self, password_hash: str) -> bool:
        """True when a stored hash was made with weaker parameters."""
        try:
            return self._hasher.check_needs_rehash(password_hash)
        except InvalidHashError:
            return True


def hash_password(password: str) -> str:
    return PasswordHasher().hash(password)
