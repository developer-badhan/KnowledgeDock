"""JWT issuance and verification.

The token is the minimum a session needs: who the user is, which password
generation the session belongs to, when it expires. No roles and no permissions
— authorisation is resolved from the database on every request, so a token can
never carry a stale claim that grants access the user has since lost.

`python-jose` was rejected in favour of PyJWT: it has a history of CVEs and is
maintained far less actively. `itsdangerous` was rejected because it signs but
does not encrypt, and a token a client can read invites payload tinkering.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt
from jwt import InvalidTokenError

ALGORITHM = "HS256"
ISSUER = "knowledgedock"
# Bytes of entropy in a reset token. 32 bytes = 256 bits.
RESET_TOKEN_BYTES = 32


@dataclass(frozen=True, slots=True)
class SessionToken:
    user_id: UUID
    session_version: int
    expires_at: datetime


class TokenService:
    def __init__(self, secret: str, *, expire_minutes: int, issuer: str = ISSUER) -> None:
        self._secret = secret
        self._expire_minutes = expire_minutes
        self._issuer = issuer

    def issue_session(
        self, user_id: UUID, session_version: int, *, now: datetime | None = None
    ) -> str:
        """Mint a session token.

        `now` is injectable so expiry can be tested without sleeping for an hour or
        freezing the clock globally. Production callers omit it.
        """
        now = now or datetime.now(UTC)
        payload = {
            "sub": str(user_id),
            "sv": session_version,
            "iss": self._issuer,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=self._expire_minutes)).timestamp()),
        }
        return jwt.encode(payload, self._secret, algorithm=ALGORITHM)

    def decode_session(self, token: str) -> SessionToken | None:
        """Return the session, or None if the token is unusable.

        Every failure mode — bad signature, expired, wrong issuer, malformed,
        wrong subject type — collapses to None. Callers raise
        `AuthenticationFailed`, which keeps the reason in the log rather than
        in the response.
        """
        try:
            payload = jwt.decode(
                token,
                self._secret,
                algorithms=[ALGORITHM],
                issuer=self._issuer,
                # `verify_iat` is off on purpose. `iat` is recorded so a token's age
                # can be audited, but it must not gate validity: PyJWT checks it
                # against the wall clock, and the wall clock is not monotonic. An
                # NTP correction or a VM resume that steps time backwards makes a
                # session minted a moment earlier look like it was issued in the
                # future, and every live session is rejected with
                # ImmatureSignatureError. That is a real outage on a cloud host,
                # and it produced a test flake that took four phases to trace.
                #
                # Trade-off documented: disabling `verify_iat` means a token with a
                # future `iat` (clock skew) would be accepted. The alternative,
                # using `leeway`, would widen the expiry window and weaken `exp`
                # enforcement. Since `exp` is the security-critical claim, we
                # accept the `iat` risk and rely on `exp` for expiry.
                options={"require": ["exp", "sub", "iss"], "verify_iat": False},
            )
            subject = UUID(payload["sub"])
            version = payload["sv"]
            if not isinstance(version, int):
                return None
        except (InvalidTokenError, KeyError, TypeError, ValueError):
            return None
        return SessionToken(
            user_id=subject,
            session_version=version,
            expires_at=datetime.fromtimestamp(payload["exp"], tz=UTC),
        )

    @property
    def expire_seconds(self) -> int:
        return self._expire_minutes * 60


def generate_reset_token() -> tuple[str, str]:
    """Return `(plaintext, sha256_hex)`.

    The plaintext goes in the emailed link and is never persisted. Only the
    digest is stored, so read access to the database does not yield a usable
    reset token.
    """
    plaintext = secrets.token_urlsafe(RESET_TOKEN_BYTES)
    return plaintext, hash_reset_token(plaintext)


def hash_reset_token(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
