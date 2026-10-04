"""Phase 02 — authentication.

Focus is business behaviour, per `SKILL.md` §26:

  * registration, including duplicate detection and input validation
  * login, including invalid credentials
  * what the session cookie actually does and does not allow

Two properties get dedicated treatment because they are the ones that quietly
fail in production:

  * **enumeration** — an attacker must not be able to learn whether an address is
    registered, from the status code, the message, or the response time
  * **revocation** — changing a password must invalidate tokens that already exist
"""

from __future__ import annotations

import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from knowledgedock.application.auth.policies import normalize_email, validate_password
from knowledgedock.application.auth.use_cases import AuthenticateUser
from knowledgedock.domain.errors import AuthenticationFailed, ValidationFailed
from knowledgedock.infrastructure.repositories.user_repository import (
    InMemoryUserRepository,
)
from knowledgedock.infrastructure.security.tokens import TokenService

PASSWORD = "correct-horse-battery"


def register(client: TestClient, email: str = "ada@example.com", password: str = PASSWORD):
    return client.post("/auth/register", json={"email": email, "password": password})


def login(client: TestClient, email: str = "ada@example.com", password: str = PASSWORD):
    return client.post("/auth/login", json={"email": email, "password": password})


async def _reset_token(client: TestClient, email: str = "ada@example.com") -> str:
    """Fetch a reset token from the use case.

    The HTTP response is deliberately identical for known and unknown addresses,
    so the token cannot come from there. Outside production it exists only in the
    log, and the log is not the system under test.
    """
    result = await client.app.state.request_password_reset.execute(email)
    assert result.delivered_token, "development must surface the link somewhere"
    return result.delivered_token


class TestRegistration:
    def test_creates_an_account(self, client: TestClient) -> None:
        response = register(client)

        assert response.status_code == 201
        body = response.json()
        assert body["email"] == "ada@example.com"
        assert uuid4().__class__ and body["id"]

    def test_response_never_contains_the_hash(self, client: TestClient) -> None:
        body = register(client).json()

        assert "password_hash" not in body
        assert "password" not in body
        assert "session_version" not in body

    def test_password_is_stored_hashed_not_plaintext(
        self, client: TestClient, users: InMemoryUserRepository
    ) -> None:
        register(client)

        stored = next(iter(users.users.values()))
        assert stored.password_hash != PASSWORD
        assert stored.password_hash.startswith("$argon2id$")

    def test_email_is_normalised_so_case_cannot_split_one_account(self, client: TestClient) -> None:
        register(client, email="Ada@Example.COM")

        response = register(client, email="ada@example.com")
        assert response.status_code == 409

    def test_duplicate_registration_is_rejected(self, client: TestClient) -> None:
        register(client)

        response = register(client)

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "conflict"

    @pytest.mark.parametrize("email", ["", "not-an-email", "no@tld", "a b@c.com", "@x.com"])
    def test_invalid_email_is_rejected(self, client: TestClient, email: str) -> None:
        response = register(client, email=email)

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_failed"

    def test_short_password_is_rejected(self, client: TestClient) -> None:
        response = register(client, password="short")

        assert response.status_code == 422

    def test_overlong_password_is_rejected(self, client: TestClient) -> None:
        # Bounds the CPU an anonymous caller can force the hasher to spend.
        response = register(client, password="x" * 129)

        assert response.status_code == 422

    def test_missing_fields_are_validation_errors(self, client: TestClient) -> None:
        assert client.post("/auth/register", json={"email": "a@b.com"}).status_code == 422
        assert client.post("/auth/register", json={}).status_code == 422


class TestLogin:
    def test_returns_a_session(self, client: TestClient) -> None:
        register(client)

        response = login(client)

        assert response.status_code == 200
        assert response.json()["user"]["email"] == "ada@example.com"
        assert response.json()["expires_in_seconds"] > 0

    def test_sets_an_httponly_session_cookie(self, client: TestClient) -> None:
        register(client)

        response = login(client)

        cookie = response.headers["set-cookie"]
        assert "kd_session=" in cookie
        assert "HttpOnly" in cookie
        assert "Path=/" in cookie

    def test_cookie_is_not_readable_from_javascript(self, client: TestClient) -> None:
        # httponly is the whole reason the UI needs no token handling in JS.
        register(client)

        assert "HttpOnly" in login(client).headers["set-cookie"]

    def test_login_is_case_insensitive_on_email(self, client: TestClient) -> None:
        register(client, email="ada@example.com")

        assert login(client, email="ADA@EXAMPLE.COM").status_code == 200

    def test_wrong_password_is_rejected(self, client: TestClient) -> None:
        register(client)

        response = login(client, password="wrong-password-entirely")

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "authentication_failed"

    def test_unknown_account_is_rejected(self, client: TestClient) -> None:
        response = login(client, email="nobody@example.com")

        assert response.status_code == 401

    def test_failed_login_sets_no_cookie(self, client: TestClient) -> None:
        register(client)

        response = login(client, password="wrong-password-entirely")

        assert "set-cookie" not in response.headers


class TestNoAccountEnumeration:
    """A login endpoint must not reveal which addresses are registered."""

    def test_status_and_message_match_for_unknown_and_wrong_password(
        self, client: TestClient
    ) -> None:
        register(client)

        unknown = login(client, email="nobody@example.com")
        wrong = login(client, password="wrong-password-entirely")

        assert unknown.status_code == wrong.status_code == 401
        assert unknown.json() == wrong.json()

    async def test_unknown_account_still_pays_the_hashing_cost(self) -> None:
        """An immediate return would make response time an enumeration oracle.

        Measured rather than assumed: a miss must not be dramatically faster
        than a hit with a wrong password.
        """
        from knowledgedock.domain.users import User, utcnow
        from knowledgedock.infrastructure.security.passwords import PasswordHasher

        repository = InMemoryUserRepository()
        hasher = PasswordHasher()
        use_case = AuthenticateUser(repository, hasher, TokenService("x" * 40, expire_minutes=60))

        async def timed(email: str, password: str) -> float:
            start = time.perf_counter()
            with pytest.raises(AuthenticationFailed):
                await use_case.execute(email, password)
            return time.perf_counter() - start

        unknown = await timed("nobody@example.com", PASSWORD)

        await repository.create(
            User(
                id=uuid4(),
                email="ada@example.com",
                password_hash=hasher.hash(PASSWORD),
                created_at=utcnow(),
                updated_at=utcnow(),
            )
        )
        wrong = await timed("ada@example.com", "not-the-password")

        assert unknown > wrong / 4, f"unknown={unknown:.4f}s wrong={wrong:.4f}s"


class TestCurrentUser:
    def test_me_requires_a_session(self, client: TestClient) -> None:
        assert client.get("/auth/me").status_code == 401

    def test_me_returns_the_signed_in_user(self, client: TestClient) -> None:
        register(client)
        login(client)

        response = client.get("/auth/me")

        assert response.status_code == 200
        assert response.json()["email"] == "ada@example.com"

    def test_me_works_with_a_bearer_token(self, client: TestClient) -> None:
        register(client)
        login(client)
        token = _token_from_cookie(client)

        response = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})

        assert response.status_code == 200
        assert response.json()["email"] == "ada@example.com"

    def test_malformed_authorization_header_is_ignored(self, client: TestClient) -> None:
        register(client)

        response = client.get("/auth/me", headers={"Authorization": "Basic abc"})

        assert response.status_code == 401

    def test_logout_clears_the_cookie(self, client: TestClient) -> None:
        register(client)
        login(client)

        response = client.post("/auth/logout")

        assert response.status_code == 200
        cookie = response.headers["set-cookie"]
        assert "kd_session=" in cookie
        assert "Max-Age=0" in cookie  # instructs the browser to drop it

    def test_session_stops_working_after_logout(self, client: TestClient) -> None:
        register(client)
        login(client)
        client.post("/auth/logout")

        assert client.get("/auth/me").status_code == 401


class TestSessionRevocation:
    async def test_password_reset_invalidates_existing_sessions(self, client: TestClient) -> None:
        register(client)
        login(client)
        assert client.get("/auth/me").status_code == 200

        token = await _reset_token(client)
        client.post(
            "/auth/password-reset/confirm",
            json={"token": token, "new_password": "a-brand-new-password"},
        )

        # The stateless token is still cryptographically valid; only the
        # session_version comparison can reject it.
        assert client.get("/auth/me").status_code == 401

    async def test_revoked_token_is_rejected_by_a_bearer_request_too(
        self, client: TestClient
    ) -> None:
        register(client)
        login(client)
        token = _token_from_cookie(client)

        token = await _reset_token(client)
        client.post(
            "/auth/password-reset/confirm",
            json={"token": token, "new_password": "a-brand-new-password"},
        )

        response = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401

    async def test_new_login_after_reset_works(self, client: TestClient) -> None:
        register(client)
        token = await _reset_token(client)
        client.post(
            "/auth/password-reset/confirm",
            json={"token": token, "new_password": "a-brand-new-password"},
        )

        assert login(client, password="a-brand-new-password").status_code == 200


class TestPasswordReset:
    def test_request_is_accepted_for_a_known_address(self, client: TestClient) -> None:
        register(client)

        response = client.post("/auth/password-reset/request", json={"email": "ada@example.com"})

        assert response.status_code == 200
        assert response.json()["accepted"] is True

    def test_request_looks_identical_for_an_unknown_address(self, client: TestClient) -> None:
        register(client)

        known = client.post("/auth/password-reset/request", json={"email": "ada@example.com"})
        unknown = client.post("/auth/password-reset/request", json={"email": "nobody@example.com"})

        assert known.status_code == unknown.status_code
        assert known.json() == unknown.json()

    def test_request_looks_identical_for_a_malformed_address(self, client: TestClient) -> None:
        response = client.post("/auth/password-reset/request", json={"email": "garbage"})

        assert response.status_code == 200
        assert response.json()["accepted"] is True

    def test_token_is_returned_outside_production_only(self, client: TestClient) -> None:
        register(client)

        body = client.post("/auth/password-reset/request", json={"email": "ada@example.com"}).json()

        # Never in the body: that would distinguish known from unknown addresses.
        assert "debug_token" not in body
        assert set(body) == {"accepted", "message"}

    async def test_confirm_changes_the_password(self, client: TestClient) -> None:
        register(client)
        token = await _reset_token(client)

        response = client.post(
            "/auth/password-reset/confirm",
            json={"token": token, "new_password": "a-brand-new-password"},
        )

        assert response.status_code == 200
        assert login(client, password="a-brand-new-password").status_code == 200

    async def test_token_is_single_use(self, client: TestClient) -> None:
        register(client)
        token = await _reset_token(client)
        client.post(
            "/auth/password-reset/confirm",
            json={"token": token, "new_password": "a-brand-new-password"},
        )

        replay = client.post(
            "/auth/password-reset/confirm",
            json={"token": token, "new_password": "yet-another-password"},
        )

        assert replay.status_code == 404

    async def test_requesting_again_invalidates_the_previous_token(
        self, client: TestClient
    ) -> None:
        register(client)
        first = await _reset_token(client)
        client.post("/auth/password-reset/request", json={"email": "ada@example.com"})

        response = client.post(
            "/auth/password-reset/confirm",
            json={"token": first, "new_password": "a-brand-new-password"},
        )

        assert response.status_code == 404

    def test_unknown_token_is_rejected(self, client: TestClient) -> None:
        response = client.post(
            "/auth/password-reset/confirm",
            json={"token": "not-a-real-token", "new_password": "a-brand-new-password"},
        )

        assert response.status_code == 404

    async def test_confirm_enforces_the_password_policy(self, client: TestClient) -> None:
        register(client)
        token = await _reset_token(client)

        response = client.post(
            "/auth/password-reset/confirm", json={"token": token, "new_password": "short"}
        )

        assert response.status_code == 422


class TestFormSurface:
    """The HTMX path. Same use cases, HTML rendering."""

    def test_login_page_renders(self, client: TestClient) -> None:
        response = client.get("/login")

        assert response.status_code == 200
        assert "Sign in" in response.text

    def test_register_page_renders(self, client: TestClient) -> None:
        assert client.get("/register").status_code == 200

    def test_form_registration_redirects_to_login(self, client: TestClient) -> None:
        response = client.post(
            "/ui/register",
            data={"email": "ada@example.com", "password": PASSWORD},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/login?registered=1"

    def test_form_registration_does_not_sign_the_user_in(self, client: TestClient) -> None:
        client.post(
            "/ui/register",
            data={"email": "ada@example.com", "password": PASSWORD},
            follow_redirects=False,
        )

        assert client.get("/auth/me").status_code == 401

    def test_form_registration_failure_rerenders_with_the_message(self, client: TestClient) -> None:
        response = client.post(
            "/ui/register", data={"email": "bad", "password": PASSWORD}, follow_redirects=False
        )

        assert response.status_code == 400
        assert "valid email" in response.text

    def test_form_registration_echoes_the_email_but_never_the_password(
        self, client: TestClient
    ) -> None:
        response = client.post(
            "/ui/register",
            data={"email": "bad", "password": "super-secret-guess"},
            follow_redirects=False,
        )

        assert "bad" in response.text
        assert "super-secret-guess" not in response.text

    def test_form_login_sets_the_cookie_and_redirects(self, client: TestClient) -> None:
        client.post(
            "/ui/register",
            data={"email": "ada@example.com", "password": PASSWORD},
            follow_redirects=False,
        )

        response = client.post(
            "/ui/login",
            data={"email": "ada@example.com", "password": PASSWORD},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert "kd_session=" in response.headers["set-cookie"]
        assert client.get("/auth/me").status_code == 200

    def test_form_login_failure_rerenders_and_sets_no_cookie(self, client: TestClient) -> None:
        client.post(
            "/ui/register",
            data={"email": "ada@example.com", "password": PASSWORD},
            follow_redirects=False,
        )

        response = client.post(
            "/ui/login",
            data={"email": "ada@example.com", "password": "wrong-one"},
            follow_redirects=False,
        )

        assert response.status_code == 400
        assert "Incorrect email or password." in response.text
        assert "set-cookie" not in response.headers

    def test_login_page_redirects_when_already_signed_in(self, client: TestClient) -> None:
        client.post(
            "/ui/register",
            data={"email": "ada@example.com", "password": PASSWORD},
            follow_redirects=False,
        )
        client.post(
            "/ui/login",
            data={"email": "ada@example.com", "password": PASSWORD},
            follow_redirects=False,
        )

        response = client.get("/login", follow_redirects=False)

        assert response.status_code == 303

    def test_form_logout_clears_the_session(self, client: TestClient) -> None:
        client.post(
            "/ui/register",
            data={"email": "ada@example.com", "password": PASSWORD},
            follow_redirects=False,
        )
        client.post(
            "/ui/login",
            data={"email": "ada@example.com", "password": PASSWORD},
            follow_redirects=False,
        )

        response = client.post("/ui/logout", follow_redirects=False)

        assert response.status_code == 303
        assert client.get("/auth/me").status_code == 401


class TestErrorContract:
    def test_errors_use_a_stable_shape(self, client: TestClient) -> None:
        body = client.post("/auth/login", json={"email": "x", "password": "y"}).json()

        assert set(body) == {"error"}
        assert set(body["error"]) == {"code", "message"}

    def test_no_stack_trace_reaches_the_client(self, client: TestClient) -> None:
        response = client.post("/auth/login", json={"email": "x", "password": "y"})

        assert "Traceback" not in response.text
        assert 'File "' not in response.text

    def test_html_client_gets_an_html_error_page(self, client: TestClient) -> None:
        response = client.get("/auth/me", headers={"Accept": "text/html"})

        assert response.status_code == 401
        assert "text/html" in response.headers["content-type"]
        assert "Traceback" not in response.text


class TestPolicies:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("  Ada@Example.COM ", "ada@example.com"),
            ("a.b+c@sub.domain.co.uk", "a.b+c@sub.domain.co.uk"),
        ],
    )
    def test_normalize_email(self, raw: str, expected: str) -> None:
        assert normalize_email(raw) == expected

    @pytest.mark.parametrize("raw", ["", "plain", "a@b", "a b@c.d"])
    def test_normalize_email_rejects_junk(self, raw: str) -> None:
        with pytest.raises(ValidationFailed):
            normalize_email(raw)

    def test_validate_password_enforces_minimum(self) -> None:
        with pytest.raises(ValidationFailed):
            validate_password("short")

    def test_validate_password_accepts_the_minimum(self) -> None:
        assert validate_password("12345678") == "12345678"

    def test_validate_password_respects_a_custom_minimum(self) -> None:
        assert validate_password("123456", minimum_length=6) == "123456"
        with pytest.raises(ValidationFailed):
            validate_password("123456", minimum_length=12)


def _token_from_cookie(client: TestClient) -> str:
    value = client.cookies.get("kd_session")
    assert value, "expected a session cookie"
    return value


class TestNavigationSessionState:
    """The nav must reflect the session without becoming an authorisation gate."""

    def test_signed_out_nav_offers_sign_in(self, client: TestClient) -> None:
        html = client.get("/").text

        assert 'href="/login"' in html
        assert "Sign out" not in html

    def test_signed_in_nav_shows_the_email_and_sign_out(self, client: TestClient) -> None:
        register(client)
        client.post(
            "/ui/login",
            data={"email": "ada@example.com", "password": PASSWORD},
            follow_redirects=False,
        )

        html = client.get("/").text

        assert "ada@example.com" in html
        assert "Sign out" in html
        assert 'href="/login"' not in html

    async def test_revoked_session_renders_as_signed_out(self, client: TestClient) -> None:
        register(client)
        client.post(
            "/ui/login",
            data={"email": "ada@example.com", "password": PASSWORD},
            follow_redirects=False,
        )
        token = await _reset_token(client)
        client.post(
            "/auth/password-reset/confirm",
            json={"token": token, "new_password": "a-brand-new-password"},
        )

        # A stale cookie must not raise a 500 on a public page.
        response = client.get("/")

        assert response.status_code == 200
        assert 'href="/login"' in response.text

    def test_garbage_cookie_does_not_break_a_page(self, client: TestClient) -> None:
        client.cookies.set("kd_session", "not-a-jwt")

        assert client.get("/").status_code == 200

    def test_production_hides_the_api_docs_link(self, production_client: TestClient) -> None:
        assert "/api/docs" not in production_client.get("/").text


#: 40 bytes. Config validation requires at least 32 in production, and PyJWT warns
#: below that, so the fixtures use a realistic strength rather than a token string.
TEST_SECRET = "test-session-signing-key-0123456789abcdef"
OTHER_SECRET = "a-different-session-key-9876543210fedcba"


class TestSessionIssuedAtIsNotValidated:
    """`iat` must not gate validity, because the wall clock is not monotonic.

    PyJWT validates `iat` against the current time. If the clock steps backwards --
    an NTP correction, a VM resume, a container being throttled and unfrozen -- a
    session minted a moment earlier appears to come from the future and is
    rejected with `ImmatureSignatureError`. The user is signed out, with no way to
    explain why.

    This produced an intermittent failure across five phases of the test suite that
    was traced to exactly this, so it is pinned here rather than left to chance.
    """

    def _service(self) -> TokenService:
        return TokenService(TEST_SECRET, expire_minutes=60)

    def test_a_token_survives_the_clock_stepping_backwards(self, monkeypatch):
        import time as time_module
        from datetime import UTC, datetime

        service = self._service()
        user_id = uuid4()

        # PyJWT reads the clock through `time.time`; freeze it so `iat` is minted
        # against a known "now".
        frozen = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        monkeypatch.setattr(time_module, "time", lambda: frozen.timestamp())
        token = service.issue_session(user_id, 1)

        # The clock jumps back five seconds, as NTP or a host resume would.
        stepped_back = frozen.timestamp() - 5
        monkeypatch.setattr(time_module, "time", lambda: stepped_back)

        session = service.decode_session(token)
        assert session is not None, "a session was invalidated by a backwards clock step"
        assert session.user_id == user_id

    def test_expiry_is_still_enforced(self, monkeypatch):
        """The claim that carries meaning must keep working.

        Turning off `iat` validation must not weaken `exp`, which is what actually
        limits a session's life.
        """
        from datetime import UTC, datetime, timedelta

        service = TokenService(TEST_SECRET, expire_minutes=1)
        issued = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        token = service.issue_session(uuid4(), 1, now=issued)

        later = issued + timedelta(minutes=30)
        import time as time_module

        monkeypatch.setattr(time_module, "time", lambda: later.timestamp())
        assert service.decode_session(token) is None

    def test_iat_is_still_present_for_auditing(self):
        import jwt as pyjwt

        from knowledgedock.infrastructure.security.tokens import ALGORITHM

        token = self._service().issue_session(uuid4(), 1)
        payload = pyjwt.decode(
            token,
            "secret-for-this-test",
            algorithms=[ALGORITHM],
            options={"verify_signature": False},
        )
        # Recorded, not enforced.
        assert "iat" in payload

    def test_a_token_signed_with_another_secret_is_still_refused(self):
        other = TokenService(OTHER_SECRET, expire_minutes=60)
        token = other.issue_session(uuid4(), 1)
        assert self._service().decode_session(token) is None

    def test_a_token_from_another_issuer_is_still_refused(self):
        from knowledgedock.infrastructure.security.tokens import TokenService

        token = TokenService(TEST_SECRET, expire_minutes=60, issuer="somebody-else").issue_session(
            uuid4(), 1
        )
        assert self._service().decode_session(token) is None


class TestAuthRateLimit:
    """The credential limiter, which exists for abuse rather than quota.

    `enforce_rate_limit` covers provider quota and says so in its refusal. These
    tests pin the separate concern: that guessing a password, or signing up a
    million accounts, or flooding a mailbox with reset mail, is bounded per caller
    -- and, just as importantly, that the limiter did not spread to the routes
    decision 60 deliberately left alone.
    """

    LIMIT = 3

    @pytest.fixture
    def limited(self, settings):
        import dataclasses

        from knowledgedock.infrastructure.repositories.user_repository import (
            InMemoryUserRepository,
        )
        from tests.conftest import _build_app

        app = _build_app(
            dataclasses.replace(settings, auth_rate_limit_per_minute=self.LIMIT),
            InMemoryUserRepository(),
            workspaces=None,
        )
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client

    def _attempts(self, client: TestClient, count: int) -> list[int]:
        return [
            client.post(
                "/ui/login",
                data={"email": "ada@example.com", "password": "wrong-password"},
                follow_redirects=False,
            ).status_code
            for _ in range(count)
        ]

    def test_attempts_past_the_limit_are_refused(self, limited: TestClient) -> None:
        statuses = self._attempts(limited, self.LIMIT + 1)

        assert statuses[: self.LIMIT] == [400] * self.LIMIT
        assert statuses[self.LIMIT] == 429

    def test_the_refusal_advertises_retry_after(self, limited: TestClient) -> None:
        self._attempts(limited, self.LIMIT)

        response = limited.post("/ui/login", data={"email": "ada@example.com", "password": "wrong"})

        assert int(response.headers["retry-after"]) >= 1

    def test_the_refusal_is_not_the_provider_quota_message(self, limited: TestClient) -> None:
        # Telling someone who mistyped a password that Gemini's free tier is rate
        # limited would be both false and baffling.
        self._attempts(limited, self.LIMIT)

        body = limited.post(
            "/ui/login", data={"email": "ada@example.com", "password": "wrong"}
        ).text

        assert "Gemini" not in body
        assert "free tier" not in body

    def test_a_correct_password_is_refused_once_the_budget_is_gone(
        self, limited: TestClient
    ) -> None:
        # The limiter counts attempts, not failures, so the check runs before the
        # hash. That is deliberate: it bounds the work rather than merely the
        # wrong guesses, and it means the refusal cannot be used to tell a
        # registered address from an unregistered one.
        limited.post("/ui/register", data={"email": "ada@example.com", "password": PASSWORD})
        limited.post("/ui/login", data={"email": "ada@example.com", "password": PASSWORD})
        limited.post("/ui/login", data={"email": "ada@example.com", "password": "wrong"})
        limited.post("/ui/login", data={"email": "ada@example.com", "password": "wrong"})

        response = limited.post(
            "/ui/login", data={"email": "ada@example.com", "password": PASSWORD}
        )

        assert response.status_code == 429

    def test_the_json_and_form_surfaces_share_one_budget(self, limited: TestClient) -> None:
        # Otherwise alternating between them buys twice the attempts for free.
        mixed = [
            limited.post(
                "/auth/login", json={"email": "ada@example.com", "password": "wrong"}
            ).status_code
            for _ in range(self.LIMIT)
        ]
        # 401 here, 400 on the form: the surfaces report failure differently and
        # must still have been spending one budget between them.
        assert mixed == [401] * self.LIMIT

        response = limited.post("/ui/login", data={"email": "ada@example.com", "password": "w"})

        assert response.status_code == 429

    def test_one_route_cannot_exhaust_another(self, limited: TestClient) -> None:
        # Exhausting sign-in must not lock anyone out of requesting a reset link.
        self._attempts(limited, self.LIMIT + 1)

        assert (
            limited.post(
                "/ui/password-reset/request",
                data={"email": "ada@example.com"},
                follow_redirects=False,
            ).status_code
            == 200
        )

    def test_registration_is_bounded(self, limited: TestClient) -> None:
        statuses = [
            limited.post(
                "/ui/register",
                data={"email": f"user{i}@example.com", "password": PASSWORD},
                follow_redirects=False,
            ).status_code
            for i in range(self.LIMIT + 1)
        ]

        assert statuses[: self.LIMIT] == [303] * self.LIMIT
        assert statuses[self.LIMIT] == 429

    def test_reset_confirmation_is_bounded(self, limited: TestClient) -> None:
        # The token is a SHA-256 digest, so guessing it is hopeless -- but the
        # endpoint still costs a lookup per attempt and deserves the same bound.
        statuses = [
            limited.post(
                "/ui/password-reset/confirm",
                data={
                    "token": f"deadbeef{i}",
                    "new_password": PASSWORD,
                    "confirm_password": PASSWORD,
                },
            ).status_code
            for i in range(self.LIMIT + 1)
        ]

        assert statuses[: self.LIMIT] == [400] * self.LIMIT
        assert statuses[self.LIMIT] == 429

    def test_assets_and_health_are_not_limited(self, limited: TestClient) -> None:
        # Decision 60's stated reason for scoping the quota limiter was that a
        # blanket limit would throttle assets and the liveness probe, turning a
        # protective limiter into an outage. The auth limiter must not repeat that.
        for _ in range(self.LIMIT + 5):
            assert limited.get("/health").status_code == 200
            assert limited.get("/static/app.css").status_code == 200

    def test_the_pages_stay_reachable(self, limited: TestClient) -> None:
        # A refused POST must not lock the user out of the form that would fix it.
        self._attempts(limited, self.LIMIT + 1)

        assert limited.get("/login").status_code == 200
        assert limited.get("/forgot-password").status_code == 200

    def test_separate_addresses_have_separate_budgets(self, settings) -> None:
        import dataclasses

        from knowledgedock.infrastructure.repositories.user_repository import (
            InMemoryUserRepository,
        )
        from tests.conftest import _build_app

        # The whole point of keying on the address: one abuser must not be able to
        # lock out everyone else. `TestClient(client=...)` is how the ASGI scope's
        # peer address is set, and `--proxy-headers` is what fills it from
        # X-Forwarded-For in production.
        app = _build_app(
            dataclasses.replace(settings, auth_rate_limit_per_minute=self.LIMIT),
            InMemoryUserRepository(),
            workspaces=None,
        )
        with TestClient(app, client=("198.51.100.7", 5000)) as attacker:
            for _ in range(self.LIMIT):
                attacker.post("/ui/login", data={"email": "a@b.co", "password": "wrong"})
            blocked = attacker.post(
                "/ui/login", data={"email": "a@b.co", "password": "wrong"}
            ).status_code

        with TestClient(app, client=("203.0.113.9", 5000)) as bystander:
            allowed = bystander.post(
                "/ui/login", data={"email": "a@b.co", "password": "wrong"}
            ).status_code

        assert blocked == 429
        assert allowed == 400

    def test_a_zero_limit_disables_it(self, settings) -> None:
        import dataclasses

        from knowledgedock.infrastructure.repositories.user_repository import (
            InMemoryUserRepository,
        )
        from tests.conftest import _build_app

        app = _build_app(
            dataclasses.replace(settings, auth_rate_limit_per_minute=0),
            InMemoryUserRepository(),
            workspaces=None,
        )
        with TestClient(app) as client:
            statuses = self._attempts(client, self.LIMIT + 5)

        assert set(statuses) == {400}
