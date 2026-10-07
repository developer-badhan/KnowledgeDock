"""Phase 09 — the HTML surface.

The point of these tests is not that a page contains a string. It is that the UI
calls the *same* use cases the JSON API does, so a form cannot become a looser
second path around validation or the workspace boundary. Each test here therefore
checks a rule that already has a JSON equivalent, through the form.
"""

from __future__ import annotations

import pathlib
import re

import pytest
from fastapi.testclient import TestClient

from knowledgedock.infrastructure.repositories.chunk_repository import (
    InMemoryChunkRepository,
)
from knowledgedock.infrastructure.repositories.conversation_repository import (
    InMemoryConversationRepository,
)
from knowledgedock.infrastructure.repositories.document_repository import (
    InMemoryDocumentRepository,
)
from knowledgedock.infrastructure.repositories.user_repository import (
    InMemoryUserRepository,
)
from knowledgedock.infrastructure.repositories.workspace_repository import (
    InMemoryWorkspaceRepository,
)
from tests.conftest import PASSWORD, _build_app, make_actor, workspace_of
from tests.test_rag import AnyQuery

TEXT = b"Payroll runs on the 15th. Timesheets lock 48 hours beforehand."


def seed_document(repository, document) -> None:
    """Insert a document straight into the repository the app reads from.

    Uploading through the form would leave it PENDING, and settling it requires
    the worker; this is for tests about how a *given* status is rendered.
    """
    import asyncio

    asyncio.run(repository.create(document))


@pytest.fixture
def env(settings):
    chunks = InMemoryChunkRepository()
    conversations = InMemoryConversationRepository()
    documents = InMemoryDocumentRepository()
    app = _build_app(
        settings,
        InMemoryUserRepository(),
        InMemoryWorkspaceRepository(),
        documents=documents,
        chunks=chunks,
        conversations=conversations,
        embeddings=AnyQuery(),
    )
    with TestClient(app) as client:
        yield client, chunks, conversations, app, documents


@pytest.fixture
def owner(env):
    return make_actor(env[0], "boss")


@pytest.fixture
def ws(owner):
    return workspace_of(owner.client, "Acme")


def seed(chunks, workspace_id, text: str = "Rotate keys from the Security page."):
    from uuid import uuid4

    doc = uuid4()
    chunks.chunks[doc] = [
        {
            "workspace_id": workspace_id,
            "document_id": doc,
            "chunk_index": 0,
            "text": text,
            "embedding": [0.98, 0.2],
            "character_count": len(text),
            "filename": "handbook.txt",
        }
    ]
    return doc


class TestAuthPages:
    def test_login_page_renders_with_htmx_and_bootstrap(self, env):
        page = env[0].get("/login")
        assert page.status_code == 200
        body = page.text
        assert "bootstrap@5" in body
        assert "htmx" in body

    def test_register_page_renders(self, env):
        assert env[0].get("/register").status_code == 200

    def test_signing_in_via_the_form_sets_a_session(self, env):
        client = env[0]
        client.post("/auth/register", json={"email": "form@example.com", "password": PASSWORD})
        response = client.post(
            "/ui/login",
            data={"email": "form@example.com", "password": PASSWORD},
            follow_redirects=False,
        )
        # 303, so a reload cannot re-post the password.
        assert response.status_code == 303
        assert "kd_session" in response.cookies

    def test_a_bad_password_rerenders_the_form_rather_than_redirecting(self, env):
        client = env[0]
        client.post("/auth/register", json={"email": "form@example.com", "password": PASSWORD})
        response = client.post(
            "/ui/login",
            data={"email": "form@example.com", "password": "wrong-password"},
        )
        # 400 with the form re-rendered, not a redirect: the submitted address
        # comes back so it need not be retyped, and the password never does.
        assert response.status_code == 400
        assert "form@example.com" in response.text

    def test_signing_out_clears_the_cookie(self, env, owner):
        response = owner.client.post("/ui/logout", follow_redirects=False)
        assert response.status_code == 303
        assert "kd_session" not in response.cookies


class TestPasswordResetForm:
    """The reset flow through the form, which must keep the JSON route's guarantees.

    The form reaches the same two use cases, so it inherits their behaviour. What
    is tested here is the part the form adds: that rendering and re-rendering a
    page cannot leak whether an address is registered, and that the token never
    appears where a client could read it.
    """

    @staticmethod
    def _without_echoed_address(html: str, *addresses: str) -> str:
        """Strip the submitted address so two responses can be compared.

        The page echoes the address back so it need not be retyped, and that echo
        is the only thing permitted to differ between a known and an unknown
        address. It is the user's own input, so it discloses nothing.
        """
        for address in addresses:
            html = html.replace(address, "")
        return html

    @staticmethod
    def _token(client: TestClient, email: str = "reset@example.com") -> str:
        """Fetch a reset token the way the use case hands it to the server.

        It is absent from every HTTP response by design, so a test cannot read it
        from one.
        """
        import asyncio

        result = asyncio.run(client.app.state.request_password_reset.execute(email))
        assert result.delivered_token, "development must surface the link somewhere"
        return result.delivered_token

    def test_the_login_page_offers_a_way_back_in(self, env):
        # Without this link the flow is unreachable from the UI, since nothing
        # else links to it.
        assert 'href="/forgot-password"' in env[0].get("/login").text

    def test_the_request_page_renders_a_form(self, env):
        page = env[0].get("/forgot-password")
        assert page.status_code == 200
        assert 'action="/ui/password-reset/request"' in page.text
        assert 'name="email"' in page.text

    def test_requesting_a_link_confirms_without_saying_whether_it_exists(self, env):
        client = env[0]
        client.post("/auth/register", json={"email": "reset@example.com", "password": PASSWORD})

        known = client.post("/ui/password-reset/request", data={"email": "reset@example.com"})
        unknown = client.post("/ui/password-reset/request", data={"email": "nobody@example.com"})
        malformed = client.post("/ui/password-reset/request", data={"email": "garbage"})

        assert known.status_code == unknown.status_code == malformed.status_code == 200
        stripped = [
            self._without_echoed_address(
                response.text, "reset@example.com", "nobody@example.com", "garbage"
            )
            for response in (known, unknown, malformed)
        ]
        assert stripped[0] == stripped[1] == stripped[2]

    def test_the_token_never_appears_in_the_request_response(self, env):
        client = env[0]
        client.post("/auth/register", json={"email": "reset@example.com", "password": PASSWORD})
        token = self._token(client)

        response = client.post("/ui/password-reset/request", data={"email": "reset@example.com"})

        # Returning it would distinguish a known address from an unknown one.
        assert token not in response.text

    def test_a_signed_in_user_is_sent_away_from_the_request_page(self, env, owner):
        response = owner.client.get("/forgot-password", follow_redirects=False)
        assert response.status_code == 303

    def test_the_confirm_page_carries_the_token_from_the_link(self, env):
        page = env[0].get("/reset-password", params={"token": "a-token"})
        assert page.status_code == 200

        hidden = re.search(r'<input type="hidden" name="token" value="([^"]+)"', page.text)
        assert hidden is not None, "the token must reach the form as a hidden field"
        assert hidden.group(1) == "a-token"

    def test_a_link_without_a_token_explains_itself(self, env):
        # A pasted or truncated link. Offering the form would only ever fail.
        response = env[0].get("/reset-password")
        assert response.status_code == 400
        assert 'name="token"' not in response.text
        assert 'href="/forgot-password"' in response.text

    def test_confirming_changes_the_password_and_lands_on_sign_in(self, env):
        client = env[0]
        client.post("/auth/register", json={"email": "reset@example.com", "password": PASSWORD})
        token = self._token(client)

        response = client.post(
            "/ui/password-reset/confirm",
            data={
                "token": token,
                "new_password": "a-brand-new-secret",
                "confirm_password": "a-brand-new-secret",
            },
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"].startswith("/login")
        assert (
            client.post(
                "/auth/login",
                json={"email": "reset@example.com", "password": "a-brand-new-secret"},
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/auth/login", json={"email": "reset@example.com", "password": PASSWORD}
            ).status_code
            == 401
        )

    def test_the_login_page_confirms_the_reset(self, env):
        assert "Password reset" in env[0].get("/login", params={"reset": 1}).text

    def test_mismatched_passwords_are_refused_server_side(self, env):
        client = env[0]
        client.post("/auth/register", json={"email": "reset@example.com", "password": PASSWORD})
        token = self._token(client)

        response = client.post(
            "/ui/password-reset/confirm",
            data={
                "token": token,
                "new_password": "a-brand-new-secret",
                "confirm_password": "something-else",
            },
        )

        # minlength and the match constraint are conveniences, not enforcement.
        assert response.status_code == 400
        assert "do not match" in response.text
        # The old password still works, so nothing was consumed.
        assert (
            client.post(
                "/auth/login", json={"email": "reset@example.com", "password": PASSWORD}
            ).status_code
            == 200
        )

    def test_a_weak_password_is_refused_and_the_link_survives(self, env):
        client = env[0]
        client.post("/auth/register", json={"email": "reset@example.com", "password": PASSWORD})
        token = self._token(client)

        response = client.post(
            "/ui/password-reset/confirm",
            data={"token": token, "new_password": "short", "confirm_password": "short"},
        )

        assert response.status_code == 400
        # Still usable: a rejected password must not burn the token.
        retry = client.post(
            "/ui/password-reset/confirm",
            data={
                "token": token,
                "new_password": "a-brand-new-secret",
                "confirm_password": "a-brand-new-secret",
            },
            follow_redirects=False,
        )
        assert retry.status_code == 303

    def test_an_invalid_token_says_so_and_suggests_a_new_link(self, env):
        response = env[0].post(
            "/ui/password-reset/confirm",
            data={
                "token": "not-a-real-token",
                "new_password": "a-brand-new-secret",
                "confirm_password": "a-brand-new-secret",
            },
        )
        assert response.status_code == 400
        assert 'href="/forgot-password"' in response.text

    def test_a_token_cannot_be_used_twice(self, env):
        client = env[0]
        client.post("/auth/register", json={"email": "reset@example.com", "password": PASSWORD})
        token = self._token(client)
        data = {
            "token": token,
            "new_password": "a-brand-new-secret",
            "confirm_password": "a-brand-new-secret",
        }

        assert (
            client.post("/ui/password-reset/confirm", data=data, follow_redirects=False).status_code
            == 303
        )
        assert client.post("/ui/password-reset/confirm", data=data).status_code == 400

    def test_the_reset_flow_stays_out_of_the_api_schema(self, env):
        # The JSON surface is the product; these are HTML routes only.
        paths = env[0].get("/api/openapi.json").json()["paths"]
        assert not any(path.startswith("/ui/password-reset") for path in paths)
        assert not any(path in {"/forgot-password", "/reset-password"} for path in paths)


class TestDashboardAccess:
    def test_dashboard_requires_a_session(self, env):
        response = env[0].get("/app", follow_redirects=False)
        assert response.status_code in (302, 303, 307, 401)

    def test_dashboard_renders_for_a_member(self, env, owner, ws):
        page = owner.client.get("/app")
        assert page.status_code == 200
        assert "Acme" in page.text

    def test_dashboard_shows_the_empty_state_with_no_workspace(self, env, owner):
        page = owner.client.get("/app")
        assert "No workspace yet" in page.text

    def test_workspace_switcher_lists_memberships(self, env, owner, ws):
        workspace_of(owner.client, "Second")
        page = owner.client.get("/app")
        assert page.status_code == 200
        assert "Acme" in page.text and "Second" in page.text
        assert 'class="kd-switcher' in page.text

    def test_one_workspace_needs_no_switcher(self, env, owner, ws):
        # A switcher with one option is noise.
        page = owner.client.get("/app")
        assert "kd-switcher" not in page.text

    def test_the_active_workspace_is_marked_for_aria(self, env, owner, ws):
        workspace_of(owner.client, "Second")
        page = owner.client.get(f"/w/{ws['id']}")
        assert 'aria-current="page"' in page.text

    def test_a_workspace_the_user_cannot_reach_is_404(self, env, owner, ws):
        stranger = make_actor(env[0], "stranger")
        assert stranger.client.get(f"/w/{ws['id']}").status_code == 404


class TestCreateWorkspace:
    """The dead end this fixes.

    A new account signed in, landed on /app, and was told "Create a workspace to
    start" by a page whose only button was Sign out. `POST /workspaces` existed and
    was the sole way to create one, so the whole product was reachable only through
    a JSON call. These tests exist so the empty state can never again be a dead end.
    """

    def test_the_empty_state_offers_a_way_to_create_one(self, env, owner):
        page = owner.client.get("/app")

        assert 'action="/ui/workspaces"' in page.text
        assert 'name="name"' in page.text
        assert "Create workspace" in page.text

    def test_creating_from_the_form_lands_in_the_new_workspace(self, env, owner):
        response = owner.client.post(
            "/ui/workspaces", data={"name": "Research"}, follow_redirects=False
        )

        assert response.status_code == 303
        assert response.headers["location"].startswith("/w/")

    def test_the_new_workspace_is_reachable_and_scoped(self, env, owner):
        owner.client.post("/ui/workspaces", data={"name": "Research"}, follow_redirects=False)

        listed = owner.client.get("/workspaces").json()

        assert [w["name"] for w in listed] == ["Research"]
        assert owner.client.get(f"/w/{listed[0]['id']}").status_code == 200

    @pytest.mark.parametrize("submitted", ["", "   "])
    def test_an_empty_name_is_refused_and_explained(self, env, owner, submitted):
        response = owner.client.post("/ui/workspaces", data={"name": submitted})

        assert response.status_code == 400
        assert "alert-danger" in response.text
        # Not a bare JSON 422: a blank submit has to come back as this page.
        assert "text/html" in response.headers["content-type"]
        assert "Give the workspace a name" in response.text

    def test_a_refused_name_is_kept_so_it_is_not_retyped(self, env, owner):
        response = owner.client.post("/ui/workspaces", data={"name": "x" * 81})

        assert response.status_code == 400
        assert "x" * 81 in response.text

    def test_a_whitespace_padded_name_is_normalised_by_the_use_case(self, env, owner):
        owner.client.post("/ui/workspaces", data={"name": "  Research  "})

        assert owner.client.get("/workspaces").json()[0]["name"] == "Research"

    def test_somebody_with_a_workspace_can_make_a_second_one(self, env, owner, ws):
        # Otherwise the absence of a button would have capped every user at one,
        # forever, with no route out of it.
        page = owner.client.get(f"/w/{ws['id']}")
        assert "New workspace" in page.text

        owner.client.post("/ui/workspaces", data={"name": "Second"})

        assert len(owner.client.get("/workspaces").json()) == 2

    def test_the_form_works_without_javascript(self, env, owner):
        # The empty state is the first thing anybody sees, possibly before the
        # bundle finishes loading, so it may not depend on HTMX.
        page = owner.client.get("/app")

        assert "hx-post" not in page.text
        assert 'method="post"' in page.text

    def test_a_signed_out_visitor_cannot_create_one(self, env):
        env[0].cookies.clear()

        assert env[0].post("/ui/workspaces", data={"name": "Sneaky"}).status_code in (302, 303, 401)

    def test_the_nav_ask_link_resolves_once_a_workspace_is_active(self, env, owner, ws):
        # base.html gated the nav "Ask" link on a `workspace_id` that no template
        # context ever supplied, so it was dead on every page.
        page = owner.client.get(f"/w/{ws['id']}")

        assert f'href="/w/{ws["id"]}/ask"' in page.text
        assert "/w//ask" not in page.text

    def test_the_nav_ask_link_is_absent_without_a_workspace(self, env, owner):
        # There is nothing to ask yet, so the link must not appear and must not
        # render as a broken path.
        page = owner.client.get("/app")

        assert ">Ask<" not in page.text
        assert "/ask" not in page.text


class TestUploadScreen:
    def test_the_upload_form_posts_multipart_to_the_ui_endpoint(self, env, owner, ws):
        page = owner.client.get(f"/w/{ws['id']}")
        assert "multipart/form-data" in page.text
        assert f"/ui/workspaces/{ws['id']}/documents" in page.text

    def test_the_dropzone_accepts_the_allowed_types(self, env, owner, ws):
        page = owner.client.get(f"/w/{ws['id']}")
        assert ".pdf" in page.text and ".docx" in page.text

    def test_uploading_through_the_form_creates_a_pending_document(self, env, owner, ws):
        response = owner.client.post(
            f"/ui/workspaces/{ws['id']}/documents",
            files={"file": ("notes.txt", TEXT, "text/plain")},
            follow_redirects=False,
        )
        assert response.status_code == 303
        api = owner.client.get(f"/workspaces/{ws['id']}/documents").json()
        assert api["items"][0]["status"] == "pending"

    def test_an_htmx_upload_is_told_to_navigate_not_given_a_document(self, env, owner, ws):
        """A 303 into an htmx swap puts a whole HTML document in a one-line div.

        The dialog was `hx-target="#uploadFeedback"`, so a successful upload
        followed the redirect inside the XHR and swapped the entire dashboard into
        that div. The user saw a blank dialog and no confirmation. `HX-Redirect`
        hands navigation back to the browser instead.
        """
        response = owner.client.post(
            f"/ui/workspaces/{ws['id']}/documents",
            files={"file": ("notes.txt", TEXT, "text/plain")},
            headers={"HX-Request": "true"},
        )

        assert response.status_code == 200
        assert response.headers["HX-Redirect"] == f"/w/{ws['id']}"
        # The body must not be a page: it is swapped into the feedback div.
        assert "<html" not in response.text.lower()

    def test_a_plain_form_post_still_redirects_normally(self, env, owner, ws):
        # Without JavaScript there is no htmx, and the 303 is the only way back.
        response = owner.client.post(
            f"/ui/workspaces/{ws['id']}/documents",
            files={"file": ("notes.txt", TEXT, "text/plain")},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"] == f"/w/{ws['id']}"

    def test_a_refused_htmx_upload_still_swaps_the_message(self, env, owner, ws):
        # The failure path has to keep returning a fragment, not a redirect.
        response = owner.client.post(
            f"/ui/workspaces/{ws['id']}/documents",
            files={"file": ("evil.exe", b"MZ\x90\x00", "application/x-msdownload")},
            headers={"HX-Request": "true"},
        )

        assert response.status_code in (400, 415, 422)
        assert "<html" not in response.text.lower()

    def test_the_dialog_declares_the_hooks_app_js_needs(self, env, owner, ws):
        page = owner.client.get(f"/w/{ws['id']}")

        # Each of these was missing or wrong while the button was unclickable.
        assert 'class="modal fade"' in page.text
        assert 'id="kdUploadForm"' in page.text
        assert "hx-disabled-elt" in page.text
        assert 'id="uploadSpinner"' in page.text
        assert 'id="kdUploadStatus"' in page.text

    def test_every_bootstrap_attribute_in_the_markup_is_implemented(self, env, owner, ws):
        """The failure behind the dead "New workspace" button.

        Only Bootstrap's CSS is loaded, never its bundle, so every
        `data-bs-toggle` is a promise app.js has to keep. `collapse` was used and
        never implemented, which left a button that looked live and did nothing.
        """
        from knowledgedock.api import ui

        # src/knowledgedock/api/ui.py -> src/knowledgedock
        package = pathlib.Path(ui.__file__).parent.parent
        source = (package / "static" / "app.js").read_text()
        toggles = {"modal", "collapse"}
        used = {
            match
            for name in ("dashboard.html", "_upload_modal.html")
            for match in re.findall(
                r'data-bs-toggle="([^"]+)"',
                (package / "templates" / name).read_text(),
            )
        }

        assert used <= toggles
        for kind in used:
            assert f'data-bs-toggle="{kind}"' in source, (
                f'the markup uses data-bs-toggle="{kind}" but app.js never handles it'
            )

    def test_the_form_uses_the_same_validation_as_the_api(self, env, owner, ws):
        # A form must not be a looser path around the content-type allowlist.
        response = owner.client.post(
            f"/ui/workspaces/{ws['id']}/documents",
            files={"file": ("evil.exe", b"MZ\x90\x00", "application/x-msdownload")},
        )
        assert response.status_code in (400, 415, 422)
        assert "Could not upload" not in response.text

    def test_a_rejected_upload_says_why(self, env, owner, ws):
        response = owner.client.post(
            f"/ui/workspaces/{ws['id']}/documents",
            files={"file": ("evil.exe", b"MZ", "application/x-msdownload")},
        )
        assert response.status_code >= 400
        assert "not accepted" in response.text

    def test_an_outsider_cannot_upload_to_a_workspace(self, env, owner, ws):
        stranger = make_actor(env[0], "stranger")
        response = stranger.client.post(
            f"/ui/workspaces/{ws['id']}/documents",
            files={"file": ("notes.txt", TEXT, "text/plain")},
        )
        assert response.status_code == 404

    def test_an_empty_file_is_refused_rather_than_stored(self, env, owner, ws):
        response = owner.client.post(
            f"/ui/workspaces/{ws['id']}/documents",
            files={"file": ("empty.txt", b"", "text/plain")},
        )
        assert response.status_code >= 400


class TestDocumentListAndStatus:
    def test_the_list_renders_a_row_per_document(self, env, owner, ws):
        owner.client.post(
            f"/ui/workspaces/{ws['id']}/documents",
            files={"file": ("notes.txt", TEXT, "text/plain")},
            follow_redirects=False,
        )
        page = owner.client.get(f"/w/{ws['id']}")
        assert "notes.txt" in page.text
        assert "<table" in page.text

    def test_the_status_fragment_is_html_rows_not_json(self, env, owner, ws):
        owner.client.post(
            f"/ui/workspaces/{ws['id']}/documents",
            files={"file": ("notes.txt", TEXT, "text/plain")},
            follow_redirects=False,
        )
        response = owner.client.get(f"/ui/workspaces/{ws['id']}/documents")
        assert "text/html" in response.headers["content-type"]
        assert "<tr" in response.text

    def test_a_pending_row_polls_itself_until_it_settles(self, env, owner, ws):
        owner.client.post(
            f"/ui/workspaces/{ws['id']}/documents",
            files={"file": ("notes.txt", TEXT, "text/plain")},
            follow_redirects=False,
        )
        fragment = owner.client.get(f"/ui/workspaces/{ws['id']}/documents").text
        assert "hx-trigger" in fragment
        assert "hx-swap" in fragment

    def test_a_ready_row_does_not_poll(self, env, owner, ws):
        documents = env[4]
        # An always-on poll against a free-tier M0 is pure waste once nothing is
        # in flight, so a settled row must not schedule another request.
        from uuid import UUID, uuid4

        from knowledgedock.domain.documents import Document, DocumentStatus
        from knowledgedock.domain.users import utcnow

        seed_document(
            documents,
            Document(
                id=uuid4(),
                workspace_id=UUID(ws["id"]),
                filename="settled.txt",
                content_type="text/plain",
                size_bytes=42,
                content_hash="b" * 64,
                storage_path="settled",
                uploaded_by=uuid4(),
                status=DocumentStatus.READY,
                created_at=utcnow(),
                updated_at=utcnow(),
            ),
        )
        fragment = owner.client.get(f"/ui/workspaces/{ws['id']}/documents").text
        assert "settled.txt" in fragment
        assert "hx-trigger" not in fragment

    def test_a_pending_row_does_poll(self, env, owner, ws):
        documents = env[4]
        from uuid import UUID, uuid4

        from knowledgedock.domain.documents import Document, DocumentStatus
        from knowledgedock.domain.users import utcnow

        seed_document(
            documents,
            Document(
                id=uuid4(),
                workspace_id=UUID(ws["id"]),
                filename="inflight.txt",
                content_type="text/plain",
                size_bytes=42,
                content_hash="c" * 64,
                storage_path="inflight",
                uploaded_by=uuid4(),
                status=DocumentStatus.PROCESSING,
                created_at=utcnow(),
                updated_at=utcnow(),
            ),
        )
        fragment = owner.client.get(f"/ui/workspaces/{ws['id']}/documents").text
        assert "hx-trigger" in fragment

    def test_the_status_filter_narrows_the_fragment(self, env, owner, ws):
        owner.client.post(
            f"/ui/workspaces/{ws['id']}/documents",
            files={"file": ("notes.txt", TEXT, "text/plain")},
            follow_redirects=False,
        )
        pending = owner.client.get(f"/ui/workspaces/{ws['id']}/documents?status=pending").text
        assert "notes.txt" in pending
        ready = owner.client.get(f"/ui/workspaces/{ws['id']}/documents?status=ready").text
        assert "notes.txt" not in ready

    def test_an_outsider_gets_an_empty_fragment_not_another_workspaces_rows(self, env, owner, ws):
        owner.client.post(
            f"/ui/workspaces/{ws['id']}/documents",
            files={"file": ("secret.txt", TEXT, "text/plain")},
            follow_redirects=False,
        )
        stranger = make_actor(env[0], "stranger")
        response = stranger.client.get(f"/ui/workspaces/{ws['id']}/documents")
        assert response.status_code == 404
        assert "secret.txt" not in response.text

    def test_deleting_through_the_form_removes_the_document(self, env, owner, ws):
        listing = owner.client.post(
            f"/ui/workspaces/{ws['id']}/documents",
            files={"file": ("notes.txt", TEXT, "text/plain")},
            follow_redirects=False,
        )
        assert listing.status_code == 303
        api = owner.client.get(f"/workspaces/{ws['id']}/documents").json()
        document_id = api["items"][0]["id"]
        response = owner.client.post(
            f"/ui/workspaces/{ws['id']}/documents/{document_id}/delete",
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert owner.client.get(f"/workspaces/{ws['id']}/documents").json()["total"] == 0

    def test_deleting_another_workspaces_document_leaves_it_untouched(self, env, owner, ws):
        # A form cannot distinguish "not yours" from "not there" without
        # rendering them differently, which would leak. Both redirect, and the
        # document survives.
        owner.client.post(
            f"/ui/workspaces/{ws['id']}/documents",
            files={"file": ("notes.txt", TEXT, "text/plain")},
            follow_redirects=False,
        )
        api = owner.client.get(f"/workspaces/{ws['id']}/documents").json()
        stranger = make_actor(TestClient(env[3]), "stranger")
        theirs = workspace_of(stranger.client, "Theirs")
        response = stranger.client.post(
            f"/ui/workspaces/{theirs['id']}/documents/{api['items'][0]['id']}/delete",
            follow_redirects=False,
        )
        assert response.status_code == 303
        _r = owner.client.get(f"/workspaces/{ws['id']}/documents")
        assert _r.json()["total"] == 1


class TestAskScreen:
    def test_the_ask_page_renders(self, env, owner, ws):
        page = owner.client.get(f"/w/{ws['id']}/ask")
        assert page.status_code == 200
        assert "Ask Acme" in page.text

    def test_the_ask_page_declares_the_wait_experience_hooks(self, env, owner, ws):
        """An ask can take tens of seconds, so the page promises an animated
        "thinking" turn while the request is out instead of looking frozen."""
        from knowledgedock.api import ui

        package = pathlib.Path(ui.__file__).parent.parent
        page = owner.client.get(f"/w/{ws['id']}/ask")
        # Turns must accumulate, or an answer wipes every previous exchange.
        assert 'hx-target="#kdAnswer"' in page.text
        assert 'hx-swap="beforeend"' in page.text
        assert 'id="kdAskForm"' in page.text

        source = (package / "static" / "app.js").read_text()
        assert "htmx:beforeRequest" in source
        assert "htmx:afterRequest" in source
        assert "form.kd-followup" in source
        assert "kd-think" in source
        # htmx 2 only swaps 2xx/3xx by default, so an error response (an outage,
        # a refused question) would be dropped and the page would go quiet after
        # the loader. The ask target must force the swap.
        assert 'target.id !== "kdAnswer"' in source
        assert "shouldSwap = true" in source

        css = (package / "static" / "app.css").read_text()
        assert ".kd-think-bar-fill" in css
        assert ".kd-typing-dot" in css
        assert "@keyframes kd-typing-pulse" in css

    def test_asking_returns_an_answer_fragment_with_citations(self, env, owner, ws):
        seed(env[1], ws["id"])
        response = owner.client.post(
            f"/ui/workspaces/{ws['id']}/ask", data={"question": "How do I rotate a key?"}
        )
        assert response.status_code == 200
        body = response.text
        assert "handbook.txt" in body
        assert "relevance" in body
        # Sources are rendered as a list, not as JSON for script to parse.
        assert "<li" in body

    def test_the_fragment_escapes_the_answer(self, env, owner, ws):
        # The payload is mid-line on purpose: a leading `<` would be filtered out
        # as a delimiter by the context renderer, so it would never reach output
        # and the test would pass without exercising escaping.
        seed(env[1], ws["id"], "Rotate keys from Security. <script>alert(1)</script>")
        response = owner.client.post(
            f"/ui/workspaces/{ws['id']}/ask", data={"question": "How do I rotate a key?"}
        )
        assert "<script>alert(1)</script>" not in response.text
        assert "&lt;script&gt;" in response.text

    def test_a_no_answer_is_shown_as_such(self, env, owner, ws):
        response = owner.client.post(
            f"/ui/workspaces/{ws['id']}/ask", data={"question": "unknown topic"}
        )
        assert "nothing found" in response.text

    def test_the_answer_offers_a_follow_up_carrying_the_conversation(self, env, owner, ws):
        seed(env[1], ws["id"])
        response = owner.client.post(
            f"/ui/workspaces/{ws['id']}/ask", data={"question": "How do I rotate a key?"}
        )
        assert "conversation_id" in response.text
        assert "Ask a follow-up" in response.text

    def test_the_retrieval_score_is_shown_for_tuning(self, env, owner, ws):
        seed(env[1], ws["id"])
        response = owner.client.post(
            f"/ui/workspaces/{ws['id']}/ask", data={"question": "How do I rotate a key?"}
        )
        assert "threshold" in response.text

    def test_asking_in_another_workspace_is_404(self, env, owner, ws):
        seed(env[1], ws["id"])
        stranger = make_actor(env[0], "stranger")
        response = stranger.client.post(
            f"/ui/workspaces/{ws['id']}/ask", data={"question": "How do I rotate a key?"}
        )
        assert response.status_code == 404
        assert "handbook.txt" not in response.text

    def test_a_blank_question_is_refused_with_a_visible_message(self, env, owner, ws):
        response = owner.client.post(f"/ui/workspaces/{ws['id']}/ask", data={"question": "   "})
        assert response.status_code >= 400
        assert "Could not answer" in response.text

    def test_asking_is_rate_limited_like_the_api(self, env, owner, ws, settings):

        app = env[3]
        app.state.rate_limiter = _limiter(1)
        first = owner.client.post(f"/ui/workspaces/{ws['id']}/ask", data={"question": "one"})
        second = owner.client.post(f"/ui/workspaces/{ws['id']}/ask", data={"question": "two"})
        assert first.status_code == 200
        assert second.status_code == 429

    def test_a_conversation_from_another_workspace_is_not_readable(self, env, owner, ws):
        seed(env[1], ws["id"])
        answer = owner.client.post(
            f"/ui/workspaces/{ws['id']}/ask", data={"question": "How do I rotate a key?"}
        ).text
        conversation_id = re.search(r'value="([0-9a-f-]{36})"', answer).group(1)
        stranger = make_actor(env[0], "stranger")
        theirs = workspace_of(stranger.client, "Theirs")
        page = stranger.client.get(f"/w/{theirs['id']}/ask?conversation_id={conversation_id}")
        assert "How do I rotate a key?" not in page.text


def _limiter(limit: int):
    from knowledgedock.infrastructure.rate_limit import RateLimiter

    return RateLimiter(limit=limit, window_seconds=60.0, clock=lambda: 1000.0)


class TestConversationHistory:
    def test_the_list_fragment_renders_after_a_question(self, env, owner, ws):
        seed(env[1], ws["id"])
        owner.client.post(
            f"/ui/workspaces/{ws['id']}/ask", data={"question": "How do I rotate a key?"}
        )
        response = owner.client.get(f"/ui/workspaces/{ws['id']}/conversations")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert re.search(r"[0-9]{2} [A-Z][a-z]{2} 20", response.text)

    def test_reopening_a_conversation_shows_its_turns(self, env, owner, ws):
        seed(env[1], ws["id"])
        answer = owner.client.post(
            f"/ui/workspaces/{ws['id']}/ask", data={"question": "How do I rotate a key?"}
        ).text
        conversation_id = re.search(r'value="([0-9a-f-]{36})"', answer).group(1)
        page = owner.client.get(f"/w/{ws['id']}/ask?conversation_id={conversation_id}")
        assert "How do I rotate a key?" in page.text
        assert "handbook.txt" in page.text

    def test_an_unknown_conversation_id_falls_back_to_a_fresh_thread(self, env, owner, ws):
        page = owner.client.get(
            f"/w/{ws['id']}/ask?conversation_id=00000000-0000-0000-0000-000000000000"
        )
        assert page.status_code == 200
        assert "Starts a new conversation" in page.text

    def test_deleting_a_conversation_removes_it_from_the_list(self, env, owner, ws):
        seed(env[1], ws["id"])
        answer = owner.client.post(
            f"/ui/workspaces/{ws['id']}/ask", data={"question": "How do I rotate a key?"}
        ).text
        conversation_id = re.search(r'value="([0-9a-f-]{36})"', answer).group(1)
        response = owner.client.post(
            f"/ui/workspaces/{ws['id']}/conversations/{conversation_id}/delete",
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert owner.client.get(f"/ui/workspaces/{ws['id']}/conversations").text.count("/ask?") <= 1

    def test_an_outsider_cannot_delete_another_conversation(self, env, owner, ws):
        seed(env[1], ws["id"])
        answer = owner.client.post(
            f"/ui/workspaces/{ws['id']}/ask", data={"question": "How do I rotate a key?"}
        ).text
        conversation_id = re.search(r'value="([0-9a-f-]{36})"', answer).group(1)
        stranger = make_actor(TestClient(env[3]), "stranger")
        theirs = workspace_of(stranger.client, "Theirs")
        response = stranger.client.post(
            f"/ui/workspaces/{theirs['id']}/conversations/{conversation_id}/delete",
            follow_redirects=False,
        )
        # Suppressed like the JSON API, so the redirect is identical to a success.
        assert response.status_code == 303
        # Still there for its owner.
        assert conversation_id in owner.client.get(f"/ui/workspaces/{ws['id']}/conversations").text


class TestNullProviderRefusalInTheUi:
    @pytest.fixture
    def null_env(self, settings):
        import dataclasses

        null_settings = dataclasses.replace(settings, ai_provider="null")
        app = _build_app(
            null_settings,
            InMemoryUserRepository(),
            InMemoryWorkspaceRepository(),
            chunks=InMemoryChunkRepository(),
            embeddings=AnyQuery(),
        )
        with TestClient(app) as client:
            yield client

    def test_asking_says_the_service_is_not_configured(self, null_env):
        actor = make_actor(null_env, "boss")
        ws = workspace_of(actor.client)
        response = actor.client.post(
            f"/ui/workspaces/{ws['id']}/ask", data={"question": "anything"}
        )
        assert response.status_code == 503
        assert "AI_PROVIDER" in response.text

    def test_uploads_still_work_without_a_provider(self, null_env):
        # The guard is on answering, not on ingestion.
        actor = make_actor(null_env, "boss")
        ws = workspace_of(actor.client)
        response = actor.client.post(
            f"/ui/workspaces/{ws['id']}/documents",
            files={"file": ("notes.txt", TEXT, "text/plain")},
            follow_redirects=False,
        )
        assert response.status_code == 303


class TestAskProviderFailureInTheUi:
    """A provider outage must answer the ask with a readable rejection.

    `ProviderUnavailable` is not an `AppError`, so it used to fall through the
    generic 500, which htmx 2 then dropped (its default reply handling swaps only
    2xx/3xx). The result was a page that went quiet after the animated loader:
    no answer, and no explanation.
    """

    def test_a_provider_failure_answers_with_a_rejection_fragment(self, settings):
        from knowledgedock.infrastructure.security.errors import ProviderUnavailable

        class Down:
            model, provider_name = "down", "gemini"

            async def generate_answer(self, prompt):
                raise ProviderUnavailable("provider down")

        chunks = InMemoryChunkRepository()
        app = _build_app(
            settings,
            InMemoryUserRepository(),
            InMemoryWorkspaceRepository(),
            chunks=chunks,
            embeddings=AnyQuery(),
            llm=Down(),
        )
        with TestClient(app) as client:
            actor = make_actor(client, "boss")
            ws = workspace_of(actor.client)
            seed(chunks, ws["id"])
            response = actor.client.post(
                f"/ui/workspaces/{ws['id']}/ask",
                data={"question": "How do I rotate a key?"},
                headers={"HX-Request": "true"},
            )
        assert response.status_code == 502
        assert "Could not answer" in response.text
        assert "unavailable" in response.text
        # The rejection is the small conversation fragment, not an error page.
        assert "<html" not in response.text.lower()


class TestNoFrontendRegression:
    """The marketing page and auth screens must keep working."""

    def test_the_landing_page_still_renders(self, env):
        assert env[0].get("/").status_code == 200

    def test_static_assets_are_served(self, env):
        assert env[0].get("/static/app.css").status_code == 200
        assert env[0].get("/static/app.js").status_code == 200

    def test_the_json_api_is_still_documented(self, env):
        # The UI is excluded from the schema, so the API stays the product surface.
        schema = env[0].get("/api/openapi.json").json()
        assert not any(path.startswith("/ui/") for path in schema["paths"])
        assert not any(path == "/app" or path.startswith("/w/") for path in schema["paths"])
        assert "/workspaces/{workspace_id}/query" in schema["paths"]
        assert "/workspaces/{workspace_id}/search" in schema["paths"]
