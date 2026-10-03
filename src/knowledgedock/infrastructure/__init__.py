"""Adapters: persistence, security primitives, external providers.

Domain code depends on the Protocols declared here, never on PyMongo, PyJWT or
argon2 directly. That is what lets the test suite run the whole auth flow without
a database, an AI key, or a real clock.
"""

from knowledgedock.infrastructure.repositories.user_repository import (
    InMemoryUserRepository,
    UserRepository,
)
from knowledgedock.infrastructure.security.passwords import PasswordHasher, hash_password
from knowledgedock.infrastructure.security.tokens import TokenService

__all__ = [
    "InMemoryUserRepository",
    "PasswordHasher",
    "TokenService",
    "UserRepository",
    "hash_password",
]
