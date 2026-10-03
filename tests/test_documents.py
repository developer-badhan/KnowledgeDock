"""Phase 04 — documents: upload, validation, metadata, processing status.

The properties under test, in order of how badly they would hurt if broken:

1. **Isolation.** The workspace is the only boundary, and a document in another
   workspace must be indistinguishable from one that never existed.
2. **Upload validation.** Nothing is written to disk before the type and size are
   checked, so a rejected upload leaves no trace.
3. **Re-upload replaces in place.** Identical content updates the same document.
   No duplicate row, no rejection.
4. **The state machine.** A client cannot set `status`, and only permitted
   transitions are allowed.
"""

from __future__ import annotations

import io
import uuid

import pytest
from fastapi.testclient import TestClient

from knowledgedock.domain.documents import (
    ALLOWED_TRANSITIONS,
    Document,
    DocumentStatus,
)
from knowledgedock.domain.errors import Conflict
from knowledgedock.domain.users import utcnow
from knowledgedock.infrastructure.storage import LocalFileStorage, hash_bytes
from tests.conftest import Actor, assert_hidden_from_outsider, make_actor, workspace_of

PASSWORD = "a-perfectly-fine-password"
TEXT = b"Credentials can be regenerated from the account security settings."


class _EmptyResult:
    deleted_count = 0
    matched_count = 0
    upserted_id = None


class _EmptyCursor:
    """A cursor that yields nothing, for the dead collections below."""

    async def to_list(self, length: object = None) -> list:
        return []


def upload(
    client: TestClient,
    ws_id: str,
    content: bytes = TEXT,
    *,
    filename: str = "notes.txt",
    content_type: str = "text/plain",
):
    return client.post(
        f"/workspaces/{ws_id}/documents",
        files={"file": (filename, content, content_type)},
    )


@pytest.fixture
def env(settings, workspaces, documents):
    """One app, many cookie jars.

    Every actor must share a single app instance. Separate instances mean
    separate in-memory repositories, so "the helper is a member of the owner's
    workspace" silently stops being true and the test asserts nothing. Only the
    first client enters the lifespan, so app.state is built once.
    """
    from knowledgedock.infrastructure.repositories.user_repository import (
        InMemoryUserRepository,
    )
    from tests.conftest import _build_app

    app = _build_app(settings, InMemoryUserRepository(), workspaces, documents=documents)
    with TestClient(app) as primary:
        extra: list[TestClient] = []

        def factory() -> TestClient:
            client = TestClient(app)
            extra.append(client)
            return client

        yield primary, factory

        for client in extra:
            client.close()


@pytest.fixture
def actor(env) -> callable:
    def _make(label: str) -> Actor:
        return make_actor(env[1](), label)

    return _make


@pytest.fixture
def owner(env) -> Actor:
    return make_actor(env[0], "boss")


@pytest.fixture
def ws(owner: Actor) -> dict:
    return workspace_of(owner.client, "Acme")


class TestUpload:
    def test_accepts_a_file_and_returns_202(self, owner: Actor, ws: dict) -> None:
        response = upload(owner.client, ws["id"])

        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "pending"
        assert body["filename"] == "notes.txt"
        assert body["size_bytes"] == len(TEXT)
        assert body["chunk_count"] == 0
        assert body["replaced"] is False

    def test_starts_pending_not_ready(self, owner: Actor, ws: dict) -> None:
        """202 means accepted, not processed. Phase 5 does the work."""
        assert upload(owner.client, ws["id"]).json()["status"] == DocumentStatus.PENDING

    def test_never_exposes_the_storage_path(self, owner: Actor, ws: dict) -> None:
        """A filesystem path must not reach a client."""
        assert "storage_path" not in upload(owner.client, ws["id"]).json()

    def test_records_the_uploader_without_gating_on_it(self, owner: Actor, ws: dict) -> None:
        assert upload(owner.client, ws["id"]).json()["uploaded_by"] == owner.user_id

    def test_requires_authentication(self, client: TestClient, ws: dict) -> None:
        assert upload(client, ws["id"]).status_code == 401

    def test_non_member_cannot_upload(self, actor, owner: Actor, ws: dict) -> None:
        outsider = actor("outsider")

        assert upload(outsider.client, ws["id"]).status_code == 404

    @pytest.mark.parametrize(
        "content_type",
        ["application/x-msdownload", "text/csv", "application/zip", "image/png"],
    )
    def test_rejects_unlisted_content_types(
        self, owner: Actor, ws: dict, content_type: str
    ) -> None:
        response = upload(owner.client, ws["id"], content_type=content_type)

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_failed"

    def test_accepts_a_charset_parameter(self, owner: Actor, ws: dict) -> None:
        """`text/plain; charset=utf-8` is the same type as `text/plain`."""
        response = upload(owner.client, ws["id"], content_type="text/plain; charset=utf-8")

        assert response.status_code == 202
        assert response.json()["content_type"] == "text/plain"

    def test_rejects_an_empty_file(self, owner: Actor, ws: dict) -> None:
        assert upload(owner.client, ws["id"], b"").status_code == 422

    def test_rejects_an_oversized_file(self, owner: Actor, ws: dict) -> None:
        oversized = b"x" * (10 * 1024 * 1024 + 1)

        assert upload(owner.client, ws["id"], oversized).status_code == 422

    @pytest.mark.parametrize(
        "raw",
        ["../../etc/passwd", "/etc/passwd", "..\\..\\secrets.txt", "....//x.txt"],
    )
    def test_strips_path_traversal_from_the_display_name(
        self, owner: Actor, ws: dict, raw: str
    ) -> None:
        """The name is display-only, but it must still not carry a path."""
        body = upload(owner.client, ws["id"], filename=raw).json()

        assert "/" not in body["filename"]
        assert "\\" not in body["filename"]
        assert ".." not in body["filename"].lstrip(".")

    def test_storage_path_ignores_the_supplied_filename(self, owner: Actor, ws: dict) -> None:
        """Content addressing is what makes re-upload a replacement."""
        body = upload(owner.client, ws["id"], filename="evil.sh").json()

        assert body["filename"] == "evil.sh"
        # sha256 of TEXT, in a content-addressed path, never the client string.
        assert hash_bytes(TEXT) not in body["filename"]


class TestReuploadReplacesInPlace:
    def test_same_content_updates_the_same_document(self, owner: Actor, ws: dict) -> None:
        first = upload(owner.client, ws["id"]).json()
        second = upload(owner.client, ws["id"]).json()

        assert second["id"] == first["id"]
        assert second["replaced"] is True
        assert len(owner.client.get(f"/workspaces/{ws['id']}/documents").json()["items"]) == 1

    def test_renaming_the_same_content_updates_metadata(self, owner: Actor, ws: dict) -> None:
        upload(owner.client, ws["id"], filename="first.txt")

        second = upload(owner.client, ws["id"], filename="second.txt").json()

        assert second["filename"] == "second.txt"
        assert second["replaced"] is True

    def test_different_content_creates_a_second_document(self, owner: Actor, ws: dict) -> None:
        upload(owner.client, ws["id"])
        upload(owner.client, ws["id"], content=b"Completely different text here.")

        listing = owner.client.get(f"/workspaces/{ws['id']}/documents").json()

        assert listing["total"] == 2

    def test_identical_content_in_a_different_workspace_is_separate(
        self, actor, owner: Actor, ws: dict
    ) -> None:
        """Content identity is scoped per workspace, not global.

        Otherwise uploading a file to workspace B would overwrite the document a
        member of workspace A already owns.
        """
        other_owner = actor("other")
        other_ws = workspace_of(other_owner.client, "Other")

        mine = upload(owner.client, ws["id"]).json()
        theirs = upload(other_owner.client, other_ws["id"]).json()

        assert theirs["id"] != mine["id"]

    def test_reupload_clears_a_previous_failure(self, owner: Actor, ws: dict, documents) -> None:
        """A stale error must not survive into the retry, and chunks must go.

        Driving the document to FAILED through the repository is what Phase 5's
        worker will do; the re-upload then has to reset both.
        """
        created = upload(owner.client, ws["id"]).json()
        doc_id = uuid.UUID(created["id"])
        failed = (
            documents.documents[doc_id]
            .transition_to(DocumentStatus.PROCESSING)
            .mark_failed("extractor crashed")
        )
        documents.documents[doc_id] = failed

        second = upload(owner.client, ws["id"]).json()

        assert second["status"] == DocumentStatus.PENDING
        assert second["processing_error"] is None
        assert second["chunk_count"] == 0
        assert second["id"] == created["id"]

    def test_reupload_discards_chunks_from_the_previous_run(
        self, owner: Actor, ws: dict, documents
    ) -> None:
        """Otherwise Phase 6 could retrieve text that no longer matches the file."""
        created = upload(owner.client, ws["id"]).json()
        doc_id = uuid.UUID(created["id"])
        documents.chunks[doc_id] = ["stale chunk from run one"]

        upload(owner.client, ws["id"])

        assert documents.chunks.get(doc_id) in (None, [])

    def test_reupload_while_processing_is_refused(self, owner: Actor, ws: dict, documents) -> None:
        """Refusing beats racing: two workers on one document would interleave."""
        created = upload(owner.client, ws["id"]).json()
        doc_id = uuid.UUID(created["id"])
        documents.documents[doc_id] = documents.documents[doc_id].transition_to(
            DocumentStatus.PROCESSING
        )

        response = upload(owner.client, ws["id"])

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "conflict"
        # The in-flight document is left exactly as it was.
        assert documents.documents[doc_id].status is DocumentStatus.PROCESSING

    def test_empty_workspace_lists_nothing(self, owner: Actor, ws: dict) -> None:
        body = owner.client.get(f"/workspaces/{ws['id']}/documents").json()

        assert body["items"] == []
        assert body["total"] == 0
        assert body["has_more"] is False

    def test_lists_documents_newest_first(self, owner: Actor, ws: dict) -> None:
        upload(owner.client, ws["id"], content=b"first file content")
        upload(owner.client, ws["id"], content=b"second file content")
        upload(owner.client, ws["id"], content=b"third file content")

        items = owner.client.get(f"/workspaces/{ws['id']}/documents").json()["items"]

        assert [d["filename"] for d in items] == ["notes.txt", "notes.txt", "notes.txt"]
        assert len({d["id"] for d in items}) == 3

    def test_only_lists_its_own_workspaces(self, actor, owner: Actor, ws: dict) -> None:
        other = actor("other")
        other_ws = workspace_of(other.client, "Other")
        upload(owner.client, ws["id"])
        upload(other.client, other_ws["id"], content=b"their file entirely")

        mine = owner.client.get(f"/workspaces/{ws['id']}/documents").json()
        theirs = other.client.get(f"/workspaces/{other_ws['id']}/documents").json()

        assert mine["total"] == 1
        assert theirs["total"] == 1

    def test_requires_authentication(self, client: TestClient, ws: dict) -> None:
        assert client.get(f"/workspaces/{ws['id']}/documents").status_code == 401

    def test_non_member_gets_404(self, actor, ws: dict) -> None:
        outsider = actor("outsider")

        assert outsider.client.get(f"/workspaces/{ws['id']}/documents").status_code == 404

    @pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"offset": -1}])
    def test_rejects_out_of_range_pagination(self, owner: Actor, ws: dict, params) -> None:
        assert (
            owner.client.get(f"/workspaces/{ws['id']}/documents", params=params).status_code == 422
        )

    def test_paginates(self, owner: Actor, ws: dict) -> None:
        for i in range(5):
            upload(owner.client, ws["id"], content=f"file number {i}".encode())

        first = owner.client.get(f"/workspaces/{ws['id']}/documents", params={"limit": 2}).json()
        second = owner.client.get(
            f"/workspaces/{ws['id']}/documents", params={"limit": 2, "offset": 2}
        ).json()

        assert len(first["items"]) == 2
        assert first["total"] == 5
        assert first["has_more"] is True
        assert {d["id"] for d in first["items"]}.isdisjoint({d["id"] for d in second["items"]})


class TestGetDocument:
    def test_owner_can_read_their_own(self, owner: Actor, ws: dict) -> None:
        created = upload(owner.client, ws["id"]).json()

        response = owner.client.get(f"/workspaces/{ws['id']}/documents/{created['id']}")

        assert response.status_code == 200
        assert response.json()["id"] == created["id"]

    def test_same_workspace_member_can_read_it(self, actor, owner: Actor, ws: dict) -> None:
        """The settled contract: every member sees every document.

        The uploader is irrelevant to access. This test exists so that changing
        the access model has to be a deliberate edit here, not a silent
        regression.
        """
        helper = actor("helper")
        created = upload(owner.client, ws["id"]).json()
        owner.client.post(f"/workspaces/{ws['id']}/members", json={"email": helper.email})

        response = helper.client.get(f"/workspaces/{ws['id']}/documents/{created['id']}")

        assert response.status_code == 200
        assert response.json()["filename"] == "notes.txt"

    def test_same_workspace_member_can_list_and_delete(self, actor, owner: Actor, ws: dict) -> None:
        """Workspace membership is the whole permission set."""
        helper = actor("helper")
        created = upload(owner.client, ws["id"]).json()
        owner.client.post(f"/workspaces/{ws['id']}/members", json={"email": helper.email})

        assert helper.client.get(f"/workspaces/{ws['id']}/documents").json()["total"] == 1
        assert (
            helper.client.delete(f"/workspaces/{ws['id']}/documents/{created['id']}").status_code
            == 204
        )

    def test_outsider_and_missing_document_are_indistinguishable(
        self, actor, owner: Actor, ws: dict
    ) -> None:
        created = upload(owner.client, ws["id"]).json()
        outsider = actor("outsider")

        real = outsider.client.get(f"/workspaces/{ws['id']}/documents/{created['id']}")
        missing = outsider.client.get(f"/workspaces/{ws['id']}/documents/{uuid.uuid4()}")

        assert_hidden_from_outsider(real, missing, "GET document")

    def test_document_in_another_workspace_is_not_found(
        self, actor, owner: Actor, ws: dict
    ) -> None:
        """A member of workspace B gets 404 for workspace A's document."""
        other = actor("other")
        other_ws = workspace_of(other.client, "Other")
        created = upload(owner.client, ws["id"]).json()

        response = other.client.get(f"/workspaces/{other_ws['id']}/documents/{created['id']}")

        assert response.status_code == 404

    def test_malformed_id_is_a_validation_error(self, owner: Actor, ws: dict) -> None:
        assert owner.client.get(f"/workspaces/{ws['id']}/documents/nope").status_code == 422

    def test_requires_authentication(self, client: TestClient, ws: dict) -> None:
        assert client.get(f"/workspaces/{ws['id']}/documents/{uuid.uuid4()}").status_code == 401


class TestHardDelete:
    def test_deletes_the_document(self, owner: Actor, ws: dict) -> None:
        created = upload(owner.client, ws["id"]).json()

        response = owner.client.delete(f"/workspaces/{ws['id']}/documents/{created['id']}")

        assert response.status_code == 204
        assert (
            owner.client.get(f"/workspaces/{ws['id']}/documents/{created['id']}").status_code == 404
        )
        assert owner.client.get(f"/workspaces/{ws['id']}/documents").json()["total"] == 0

    def test_is_not_a_soft_delete(self, owner: Actor, ws: dict, documents) -> None:
        """No tombstone survives. A soft-deleted row would still be returned by
        Phase 6's vector search through its orphaned chunks."""
        created = upload(owner.client, ws["id"]).json()
        doc_id = uuid.UUID(created["id"])

        owner.client.delete(f"/workspaces/{ws['id']}/documents/{created['id']}")

        assert doc_id not in documents.documents

    def test_deletes_the_stored_file(self, owner: Actor, ws: dict, documents) -> None:
        storage: LocalFileStorage = owner.client.app.state.delete_document._storage
        created = upload(owner.client, ws["id"]).json()
        stored = storage.root / documents.documents[uuid.UUID(created["id"])].storage_path
        assert stored.is_file()

        owner.client.delete(f"/workspaces/{ws['id']}/documents/{created['id']}")

        assert not stored.exists()

    def test_reupload_leaves_the_stored_file_alone(self, owner: Actor, ws: dict, documents) -> None:
        """Identical content addresses the same path, so nothing is rewritten."""
        storage: LocalFileStorage = owner.client.app.state.delete_document._storage
        created = upload(owner.client, ws["id"]).json()
        stored = storage.root / documents.documents[uuid.UUID(created["id"])].storage_path
        stamp = stored.stat().st_mtime_ns

        upload(owner.client, ws["id"], filename="renamed.txt")

        assert stored.exists()
        assert stored.stat().st_mtime_ns == stamp

    def test_deleting_twice_is_not_found_the_second_time(self, owner: Actor, ws: dict) -> None:
        created = upload(owner.client, ws["id"]).json()
        owner.client.delete(f"/workspaces/{ws['id']}/documents/{created['id']}")

        second = owner.client.delete(f"/workspaces/{ws['id']}/documents/{created['id']}")

        assert second.status_code == 404

    def test_outsider_cannot_delete(self, actor, owner: Actor, ws: dict) -> None:
        created = upload(owner.client, ws["id"]).json()
        outsider = actor("outsider")

        response = outsider.client.delete(f"/workspaces/{ws['id']}/documents/{created['id']}")

        assert response.status_code == 404
        assert (
            owner.client.get(f"/workspaces/{ws['id']}/documents/{created['id']}").status_code == 200
        )

    def test_requires_authentication(self, client: TestClient, ws: dict) -> None:
        assert client.delete(f"/workspaces/{ws['id']}/documents/{uuid.uuid4()}").status_code == 401


class TestStateMachine:
    """The transition guard, tested on the domain object directly.

    Routes never accept `status`, so this is the only place a transition can
    happen before Phase 5's worker arrives.
    """

    def _doc(self, status: DocumentStatus = DocumentStatus.PENDING) -> Document:
        now = utcnow()
        return Document(
            id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            filename="a.txt",
            content_type="text/plain",
            size_bytes=10,
            content_hash="a" * 64,
            storage_path="ws/a.txt",
            uploaded_by=uuid.uuid4(),
            status=status,
            created_at=now,
            updated_at=now,
        )

    def test_pending_moves_to_processing(self) -> None:
        assert (
            self._doc().transition_to(DocumentStatus.PROCESSING).status is DocumentStatus.PROCESSING
        )

    def test_pending_cannot_jump_to_ready(self) -> None:
        """The transition this guard exists to prevent."""
        with pytest.raises(Conflict, match="pending"):
            self._doc().transition_to(DocumentStatus.READY)

    def test_pending_cannot_jump_to_failed(self) -> None:
        with pytest.raises(Conflict):
            self._doc().transition_to(DocumentStatus.FAILED)

    def test_processing_reaches_ready(self) -> None:
        doc = self._doc().transition_to(DocumentStatus.PROCESSING)

        assert doc.transition_to(DocumentStatus.READY).status is DocumentStatus.READY

    def test_processing_can_fail_with_a_reason(self) -> None:
        doc = self._doc().transition_to(DocumentStatus.PROCESSING)

        failed = doc.mark_failed("PDF has no extractable text")

        assert failed.status is DocumentStatus.FAILED
        assert failed.processing_error == "PDF has no extractable text"

    def test_ready_is_terminal(self) -> None:
        doc = (
            self._doc().transition_to(DocumentStatus.PROCESSING).transition_to(DocumentStatus.READY)
        )

        for target in DocumentStatus:
            if target is not DocumentStatus.READY:
                with pytest.raises(Conflict):
                    doc.transition_to(target)

    def test_failed_can_be_requeued_but_not_become_ready(self) -> None:
        failed = self._doc().transition_to(DocumentStatus.PROCESSING).mark_failed("boom")

        assert failed.requeue().status is DocumentStatus.PENDING
        with pytest.raises(Conflict):
            failed.transition_to(DocumentStatus.READY)

    def test_requeue_clears_the_error_and_chunk_count(self) -> None:
        doc = self._doc().transition_to(DocumentStatus.PROCESSING).mark_failed("boom")

        requeued = doc.requeue()

        assert requeued.processing_error is None
        assert requeued.chunk_count == 0

    def test_only_failed_documents_can_be_requeued(self) -> None:
        with pytest.raises(Conflict, match="Only a failed document"):
            self._doc().requeue()

    def test_processing_increments_attempts(self) -> None:
        doc = self._doc()

        first = doc.transition_to(DocumentStatus.PROCESSING)
        second = first.mark_failed("boom").requeue().transition_to(DocumentStatus.PROCESSING)

        assert second.attempts == 2

    def test_transition_to_self_is_a_no_op(self) -> None:
        doc = self._doc()

        assert doc.transition_to(DocumentStatus.PENDING) is doc

    def test_error_is_cleared_by_a_successful_transition(self) -> None:
        doc = self._doc().transition_to(DocumentStatus.PROCESSING).mark_failed("old failure")

        assert doc.requeue().processing_error is None

    def test_transition_table_is_exhaustive(self) -> None:
        """Every status has an entry, so a new state cannot be transition-less."""
        assert set(ALLOWED_TRANSITIONS) == set(DocumentStatus)


class TestStorageAdapter:
    def test_round_trips_content(self, tmp_path) -> None:
        storage = LocalFileStorage(tmp_path / "up")
        storage.ensure_root()
        ws_id = uuid.uuid4()
        digest = hash_bytes(TEXT)

        path, written = storage.save(ws_id, digest, "text/plain", io.BytesIO(TEXT))

        assert written == len(TEXT)
        assert storage.exists(path)
        assert (storage.root / path).read_bytes() == TEXT

    def test_path_is_content_addressed(self, tmp_path) -> None:
        storage = LocalFileStorage(tmp_path / "up")
        storage.ensure_root()
        ws_id = uuid.uuid4()
        digest = hash_bytes(TEXT)

        path, _ = storage.save(ws_id, digest, "text/plain", io.BytesIO(TEXT))

        assert path == f"{ws_id}/{digest}.txt"

    def test_extension_comes_from_content_type(self, tmp_path) -> None:
        storage = LocalFileStorage(tmp_path / "up")
        storage.ensure_root()

        path, _ = storage.save(uuid.uuid4(), "b" * 64, "application/pdf", io.BytesIO(b"%PDF"))

        assert path.endswith(".pdf")

    def test_unknown_content_type_gets_a_safe_suffix(self, tmp_path) -> None:
        storage = LocalFileStorage(tmp_path / "up")
        storage.ensure_root()

        path, _ = storage.save(uuid.uuid4(), "c" * 64, "application/x-thing", io.BytesIO(b"x"))

        assert path.endswith(".bin")

    def test_identical_content_lands_on_the_same_path(self, tmp_path) -> None:
        storage = LocalFileStorage(tmp_path / "up")
        storage.ensure_root()
        ws_id, digest = uuid.uuid4(), hash_bytes(TEXT)

        first, _ = storage.save(ws_id, digest, "text/plain", io.BytesIO(TEXT))
        second, _ = storage.save(ws_id, digest, "text/plain", io.BytesIO(TEXT))

        assert first == second

    def test_delete_is_idempotent(self, tmp_path) -> None:
        storage = LocalFileStorage(tmp_path / "up")
        storage.ensure_root()
        path, _ = storage.save(uuid.uuid4(), "d" * 64, "text/plain", io.BytesIO(b"x"))

        storage.delete(path)
        storage.delete(path)

        assert not storage.exists(path)

    @pytest.mark.parametrize("escape", ["../outside.txt", "../../etc/passwd", "ws/../../x"])
    def test_delete_cannot_escape_the_root(self, tmp_path, escape: str) -> None:
        """`resolve()` collapses `..`, so the guard runs on the real path.

        `delete` swallows a refused path rather than failing the request, so the
        assertion is the security property itself: the file outside the root is
        still there afterwards.
        """
        storage = LocalFileStorage(tmp_path / "up")
        storage.ensure_root()
        canary = tmp_path / "outside.txt"
        canary.write_text("secret")

        storage.delete(escape)

        assert canary.exists(), "a traversal path deleted a file outside the root"

    @pytest.mark.parametrize("escape", ["../outside.txt", "../../etc/passwd"])
    def test_absolute_raises_for_escaping_paths(self, tmp_path, escape: str) -> None:
        from knowledgedock.infrastructure.storage import StorageError

        storage = LocalFileStorage(tmp_path / "up")
        storage.ensure_root()

        with pytest.raises(StorageError):
            storage._absolute(escape)

    def test_exists_is_false_for_an_escaping_path(self, tmp_path) -> None:
        storage = LocalFileStorage(tmp_path / "up")
        storage.ensure_root()
        (tmp_path / "outside.txt").write_text("secret")

        assert storage.exists("../outside.txt") is False


class TestDocumentOutage:
    def test_database_failure_is_503_not_an_empty_workspace(self, settings) -> None:
        """A broken deployment must not look like an empty workspace."""

        from fastapi.testclient import TestClient as TC

        from knowledgedock.app import create_app
        from knowledgedock.infrastructure.repositories.document_repository import (
            UnavailableDocumentRepository,
        )
        from tests.conftest import (
            FakeMongoManager,
            InMemoryUserRepository,
            InMemoryWorkspaceRepository,
        )

        app = create_app(
            settings,
            mongo_manager=FakeMongoManager(reachable=False),
            user_repository=InMemoryUserRepository(),
            workspace_repository=InMemoryWorkspaceRepository(),
            document_repository=UnavailableDocumentRepository(RuntimeError("no Atlas")),
        )
        with TC(app) as client:
            client.post("/auth/register", json={"email": "d@example.com", "password": PASSWORD})
            client.post("/auth/login", json={"email": "d@example.com", "password": PASSWORD})
            ws = client.post("/workspaces", json={"name": "Acme"}).json()

            response = client.get(f"/workspaces/{ws['id']}/documents")

            assert response.status_code == 503
            assert response.json()["error"]["code"] == "service_unavailable"
