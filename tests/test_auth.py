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
