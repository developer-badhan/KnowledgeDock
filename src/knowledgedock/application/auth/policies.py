"""Email normalisation and password policy.

These are domain rules, not HTTP concerns, so they live here rather than in a
route or a Pydantic validator. Both the API and the form handlers call them, which
is what keeps a JSON registration and an HTML form registration from drifting
apart.
"""

from __future__ import annotations

import re

from knowledgedock.domain.errors import ValidationFailed

# Deliberately permissive. The goal is to catch typos, not to adjudicate RFC 5322.
_EMAIL_SHAPE = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")

# Render's free tier gives 512 MB and Argon2 costs real CPU. These bound the work
# an unauthenticated caller can force the server to do per request.
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128


def normalize_email(email: str) -> str:
    """Lowercase and trim so `A@B.com` and `a@b.com` are one account."""
    candidate = email.strip()
    if not candidate:
        raise ValidationFailed("Email is required.", detail="empty email")
    if len(candidate) > 254:
        raise ValidationFailed("That email address is too long.")
    if not _EMAIL_SHAPE.match(candidate):
        raise ValidationFailed("That does not look like a valid email address.")
    return candidate.lower()


def validate_password(password: str, *, minimum_length: int = MIN_PASSWORD_LENGTH) -> str:
    if len(password) < minimum_length:
        raise ValidationFailed(f"Password must be at least {minimum_length} characters.")
    if len(password) > MAX_PASSWORD_LENGTH:
        # Argon2 accepts any length, but an unbounded input is a free CPU denial
        # of service against the hasher.
        raise ValidationFailed(f"Password must be at most {MAX_PASSWORD_LENGTH} characters.")
    return password
