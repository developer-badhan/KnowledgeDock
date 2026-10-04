"""Phase 09 — the HTML surface.

The point of these tests is not that a page contains a string. It is that the UI
calls the *same* use cases the JSON API does, so a form cannot become a looser
second path around validation or the workspace boundary. Each test here therefore
checks a rule that already has a JSON equivalent, through the form.
"""

from __future__ import annotations

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
