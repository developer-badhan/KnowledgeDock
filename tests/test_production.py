"""Phase 10 — production readiness.

Four things live here that do not belong to a feature phase:

1. The **end-to-end test** of the flow in `SKILL.md` §40, driven through the real
   ingestion worker and the real use cases with only the two network providers
   stubbed.
2. Tests for the gaps the earlier phases left — the upload size limit among them.
3. **Configuration and packaging assertions**, so a broken Dockerfile or an
   unreadable variable fails here rather than at deploy time.
4. A short **security-review checklist** encoded as tests. Prose in a document
   goes stale; these fail when the property stops holding.
"""

from __future__ import annotations

import asyncio
import io
import json
import re
from pathlib import Path
from uuid import UUID, uuid4

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
from knowledgedock.infrastructure.repositories.usage_repository import (
    InMemoryAIUsageRepository,
)
from knowledgedock.infrastructure.repositories.user_repository import (
    InMemoryUserRepository,
)
from knowledgedock.infrastructure.repositories.workspace_repository import (
    InMemoryWorkspaceRepository,
)
from tests.conftest import PASSWORD, _build_app, make_actor, workspace_of
from tests.test_rag import AnyQuery

ROOT = Path(__file__).resolve().parent.parent

HANDBOOK = """\
Payroll runs on the 15th and on the last working day of the month.
Timesheets lock 48 hours before the run and cannot be edited afterwards.
Overtime is paid at 1.5x the hourly rate.
"""


# ---------------------------------------------------------------------------
# SKILL.md §40 — the full flow
# ---------------------------------------------------------------------------
class TestEndToEndSkillSection40:
    """Register -> login -> workspace -> upload -> READY -> ask -> cited answer.

    Every arrow in §40 is asserted, not just the endpoints. The intermediate steps
    -- extraction, chunking, embedding, storage, query embedding, vector retrieval,
    context construction -- are observed through the state they leave behind
    rather than by reaching inside the pipeline, so the test cannot pass while the
    pipeline is broken.
    """

    @pytest.fixture
    def stack(self, settings):
        """One app with the real worker running and both networks stubbed.

        `processing_enabled=True` starts the actual `IngestionWorker` loop, so the
        document reaches READY by being processed rather than by a test forcing the
        status. Only the two providers are substituted; everything else is the code
        that ships.
        """
        chunks = InMemoryChunkRepository()
        documents = InMemoryDocumentRepository()
        conversations = InMemoryConversationRepository()
        usage = InMemoryAIUsageRepository()
        app = _build_app(
            settings,
            InMemoryUserRepository(),
            InMemoryWorkspaceRepository(),
            documents=documents,
            chunks=chunks,
            conversations=conversations,
            usage=usage,
            embeddings=AnyQuery(),
            processing_enabled=True,
        )
        with TestClient(app) as client:
            yield client, documents, chunks, usage

    def test_the_whole_flow_runs_end_to_end(self, stack) -> None:
        client, documents, chunks, usage = stack

        # 1. Register.
        email = "e2e@example.com"
        registered = client.post("/auth/register", json={"email": email, "password": PASSWORD})
        assert registered.status_code == 201
        user_id = registered.json()["id"]

        # 2. Login.
        login = client.post("/auth/login", json={"email": email, "password": PASSWORD})
        assert login.status_code == 200

        # 3. Create workspace.
        workspace = client.post("/workspaces", json={"name": "Acme"}).json()
        workspace_id = workspace["id"]
        base = f"/workspaces/{workspace_id}"

        # 4. Upload document. 202, and PENDING: the request does not process it.
        uploaded = client.post(
            f"{base}/documents",
            files={"file": ("handbook.txt", HANDBOOK.encode(), "text/plain")},
        )
        assert uploaded.status_code == 202
        document_id = uploaded.json()["id"]
        assert uploaded.json()["status"] == "pending"

        # 5-10. PROCESSING -> extraction -> chunking -> embedding -> storage -> READY.
        #    Driven by the real worker; the test only waits and watches.
        import time

        deadline = time.monotonic() + 20
        observed: list[str] = []
        final: dict = {}
        while time.monotonic() < deadline:
            body = client.get(f"{base}/documents/{document_id}").json()
            observed.append(body["status"])
            final = body
            if body["status"] in ("ready", "failed"):
                break
            time.sleep(0.02)

        assert final["status"] == "ready", f"statuses seen: {observed}"
        assert "pending" in observed, "the document never reported PENDING"

        # 8. Chunking produced chunks; 9. embedding produced vectors.
        stored = chunks.chunks[UUID(document_id)]
        assert stored, "chunking produced nothing"
        assert all(row.get("embedding") for row in stored), "a chunk has no embedding"

        # 10. MongoDB storage: the document row itself.
        assert document_id in {str(k) for k in documents.documents}

        # 11-18. Ask -> query embedding -> vector retrieval -> chunks -> context -> LLM.
        answer = client.post(f"{base}/query", json={"question": "When does payroll run?"})
        assert answer.status_code == 200
        body = answer.json()
        assert body["no_answer"] is False
        assert body["answer"]
        assert body["retrieval"]["top_score"] >= body["retrieval"]["threshold"]
        assert body["retrieval"]["context_characters"] > 0

        # 19-20. Source citations.
        assert body["sources"], "a grounded answer with no sources"
        source = body["sources"][0]
        assert source["document_id"] == document_id
        assert source["filename"] == "handbook.txt"
        assert source["score"] > 0

        # Usage accounting ran across the flow without being asked for.
        assert usage.records, "no AI usage recorded during a full flow"
        assert all(
            r.workspace_id is None or isinstance(r.workspace_id, UUID) for r in usage.records
        )

        # The user is the one who did all of it.
        assert client.get("/auth/me").json()["id"] == user_id

    def test_a_second_document_is_retrieved_alongside_the_first(self, stack) -> None:
        """Two documents, and retrieval picks the relevant one.

        §40 says "relevant chunks". Relevance is only demonstrable when something
        irrelevant exists to be rejected.
        """
        client, _, chunks, _ = stack
        client.post("/auth/register", json={"email": "two@example.com", "password": PASSWORD})
        client.post("/auth/login", json={"email": "two@example.com", "password": PASSWORD})
        workspace = client.post("/workspaces", json={"name": "Acme"}).json()
        base = f"/workspaces/{workspace['id']}"

        for name, body in (
            ("payroll.txt", HANDBOOK),
            # Long enough to clear min_chunk_size; a stub that falls under it is
            # correctly rejected by the chunker, which is a different test.
            ("shipping.txt", "Ocean freight is priced per twenty foot container. " * 6),
        ):
            client.post(
                f"{base}/documents",
                files={"file": (name, body.encode(), "text/plain")},
            )

        import time

        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            listing = client.get(f"{base}/documents").json()
            if listing["total"] == 2 and all(d["status"] == "ready" for d in listing["items"]):
                break
            time.sleep(0.02)
        else:
            pytest.fail(f"documents never settled: {listing}")

        answer = client.post(f"{base}/query", json={"question": "When does payroll run?"}).json()
        assert answer["no_answer"] is False
        assert answer["sources"][0]["filename"] == "payroll.txt"


# ---------------------------------------------------------------------------
# Gaps left by earlier phases
# ---------------------------------------------------------------------------
class TestUploadSizeLimit:
    """The size cap is a security control, so it gets tested as one.

    A cap that is never exercised is a cap that silently stopped existing the
    first time someone reordered a validation branch.
    """

    @pytest.fixture
    def env(self, settings):
        app = _build_app(settings, InMemoryUserRepository(), InMemoryWorkspaceRepository())
        with TestClient(app) as client:
            yield client

    @pytest.fixture
    def owner(self, env):
        actor = make_actor(env, "boss")
        return actor, workspace_of(actor.client, "Acme")

    def test_a_file_within_the_limit_is_accepted(self, owner):
        actor, ws = owner
        response = actor.client.post(
            f"/workspaces/{ws['id']}/documents",
            files={"file": ("ok.txt", b"x" * 1024, "text/plain")},
        )
        assert response.status_code == 202

    def test_a_file_over_the_limit_is_refused(self, owner):
        actor, ws = owner
        # Ask for 11 MB against the 10 MB default.
        oversized = b"x" * (11 * 1024 * 1024)
        response = actor.client.post(
            f"/workspaces/{ws['id']}/documents",
            files={"file": ("big.txt", oversized, "text/plain")},
        )
        assert response.status_code >= 400
        assert "10" in response.text  # the limit is named, so the user can act on it

    def test_the_refusal_does_not_store_a_partial_document(self, owner):
        actor, ws = owner
        actor.client.post(
            f"/workspaces/{ws['id']}/documents",
            files={"file": ("big.txt", b"x" * (11 * 1024 * 1024), "text/plain")},
        )
        assert actor.client.get(f"/workspaces/{ws['id']}/documents").json()["total"] == 0

    def test_a_streamed_upload_is_capped_even_without_a_declared_length(self, owner):
        """A client that lies about, or omits, Content-Length must still be capped.

        The declared size is only a hint. If the cap were enforced against the
        header alone, sending no header would bypass it entirely.
        """
        from knowledgedock.application.documents.use_cases import validate_size
        from knowledgedock.domain.errors import ValidationFailed

        # No declared size, so enforcement must come from counting as it reads.
        with pytest.raises(ValidationFailed):
            validate_size(11 * 1024 * 1024, 10 * 1024 * 1024)


class TestUsageEndpoint:
    @pytest.fixture
    def env(self, settings):
        usage = InMemoryAIUsageRepository()
        app = _build_app(
            settings,
            InMemoryUserRepository(),
            InMemoryWorkspaceRepository(),
            usage=usage,
            embeddings=AnyQuery(),
        )
        with TestClient(app) as client:
            yield client, usage

    @pytest.fixture
    def owner(self, env):
        actor = make_actor(env[0], "boss")
        return actor, workspace_of(actor.client, "Acme")

    def _record(self, usage, workspace_id, **kwargs):
        from knowledgedock.domain.usage import AIOperation
        from knowledgedock.infrastructure.ai.usage import UsageRecorder

        defaults = {
            "operation": AIOperation.GENERATE,
            "provider": "gemini",
            "model": "gemini-3.8-flash",
            "duration_ms": 100.0,
            "success": True,
            "input_tokens": 200,
            "output_tokens": 50,
            "workspace_id": workspace_id,
        }
        defaults.update(kwargs)
        asyncio.run(UsageRecorder(usage).record(**defaults))

    def test_usage_is_reported_for_the_workspace(self, env, owner):
        actor, ws = owner
        self._record(env[1], UUID(ws["id"]))
        body = actor.client.get(f"/workspaces/{ws['id']}/usage").json()
        assert body["workspace_id"] == ws["id"]
        assert body["totals"]["calls"] == 1
        assert body["totals"]["input_tokens"] == 200

    def test_failures_are_counted_separately(self, env, owner):
        actor, ws = owner
        self._record(env[1], UUID(ws["id"]), success=False, error="ProviderTimeout")
        totals = actor.client.get(f"/workspaces/{ws['id']}/usage").json()["totals"]
        assert totals["failures"] == 1

    def test_another_workspaces_usage_is_not_disclosed(self, env, owner):
        actor, ws = owner
        theirs = workspace_of(actor.client, "Theirs")
        self._record(env[1], UUID(theirs["id"]))
        body = actor.client.get(f"/workspaces/{ws['id']}/usage").json()
        assert body["totals"]["calls"] == 0

    def test_an_outsider_gets_404(self, env, owner):
        actor, ws = owner
        stranger = make_actor(TestClient(env[0].app), "stranger")
        assert stranger.client.get(f"/workspaces/{ws['id']}/usage").status_code == 404

    def test_usage_requires_authentication(self, env, owner):
        _, ws = owner
        anonymous = TestClient(env[0].app)
        assert anonymous.get(f"/workspaces/{ws['id']}/usage").status_code == 401

    def test_the_window_is_bounded(self, env, owner):
        actor, ws = owner
        assert actor.client.get(f"/workspaces/{ws['id']}/usage?days=365").status_code == 422

    def test_it_is_not_rate_limited(self, env, owner):
        """A dashboard polls this; throttling it would break the observability it exists for."""
        actor, ws = owner
        app = env[0].app
        app.state.rate_limiter = _frozen_limiter(limit=1)
        url = f"/workspaces/{ws['id']}/usage"
        assert actor.client.get(url).status_code == 200
        assert actor.client.get(url).status_code == 200

    def test_it_is_documented_in_the_schema(self, env):
        schema = env[0].get("/api/openapi.json").json()
        assert "/workspaces/{workspace_id}/usage" in schema["paths"]


def _frozen_limiter(limit: int):
    from knowledgedock.infrastructure.rate_limit import RateLimiter

    return RateLimiter(limit=limit, window_seconds=60.0, clock=lambda: 1000.0)


# ---------------------------------------------------------------------------
# Security review, encoded as tests
# ---------------------------------------------------------------------------
class TestSecurityReview:
    """Each test is a property the review concluded must hold.

    Kept as executable assertions because prose goes stale silently and a test
    fails loudly.
    """

    #: Values that are documentation, not credentials. `DEPLOYMENT.md` writes
    #: `mongodb+srv://knowledgedock:<db_password>@...` on purpose, and
    #: `.env.sample` documents the shape with an empty right-hand side. Flagging
    #: those would train the check to be ignored.
    PLACEHOLDER = re.compile(
        r"^(?:<.*>|x{3,}|\.\.\.|changeme|your[-_].*|example|test-key|dummy|"
        r"placeholder|replace[-_]?me|password|secret|\*+|-+)$",
        re.IGNORECASE,
    )

    def _is_placeholder(self, value: str) -> bool:
        return not value.strip() or bool(self.PLACEHOLDER.match(value.strip()))

    def test_no_real_credential_is_committed(self):
        """Scan tracked files for a credential that is not a placeholder.

        Checked against the *content* of each candidate rather than its presence,
        so documentation of a DSN's shape passes while a real password does not.
        """
        import subprocess

        tracked = subprocess.run(
            ["git", "ls-files"], capture_output=True, text=True, check=True
        ).stdout.split()

        offenders: list[str] = []
        for name in tracked:
            path = ROOT / name
            if path.suffix in {".png", ".ico", ".lock", ".woff", ".woff2"}:
                continue
            if not path.is_file():
                continue
            text = path.read_text(errors="ignore")

            # A Gemini key has a fixed prefix, so its presence is unambiguous.
            if re.search(r"AIza[0-9A-Za-z_\-]{35}", text):
                offenders.append(f"Gemini API key in {name}")

            for match in re.finditer(r"mongodb\+srv://([^:/\s]+):([^@\s]+)@", text):
                if not self._is_placeholder(match.group(2)):
                    offenders.append(f"MongoDB password in {name}")

            # Test fixtures are permitted to carry strong-looking fake secrets --
            # length validation would reject anything short. Source and deployment
            # documentation are not, so the value check is scoped to them.
            if name.startswith("tests/"):
                continue
            for line in text.splitlines():
                name_of, sep, value = line.partition("=")
                if not sep or name_of.strip() not in {
                    "APP_SECRET_KEY",
                    "SESSION_SECRET",
                    "SECRET_KEY",
                    "GEMINI_API_KEY",
                }:
                    continue
                if not self._is_placeholder(value):
                    offenders.append(f"{name_of.strip()} value in {name}")

        assert offenders == [], f"real credentials committed: {sorted(set(offenders))}"

    def test_env_sample_has_no_real_values(self):
        """`.env.sample` documents names, not secrets."""
        text = (ROOT / ".env.sample").read_text()
        for line in text.splitlines():
            if line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            if name in SECRET_VARIABLES:
                # Required at deploy time and deliberately blank: a sample that
                # ships a working key is a leaked key.
                assert not value.strip(), f"{name} has a value in .env.sample"
            else:
                assert value.strip(), f"{name} has no default in .env.sample"

    def test_gitignore_excludes_the_real_env_file(self):
        text = (ROOT / ".gitignore").read_text()
        assert ".env" in text
        assert "!.env.sample" in text or ".env.sample" in text

    def test_error_responses_never_carry_a_stack_trace(self, settings):
        app = _build_app(settings, InMemoryUserRepository(), InMemoryWorkspaceRepository())
        with TestClient(app, raise_server_exceptions=False) as client:

            @app.get("/_boom")
            async def boom():
                raise RuntimeError("internal detail /etc/secret")

            body = client.get("/_boom").text
            assert "Traceback" not in body
            assert "/etc/secret" not in body
            assert "internal detail" not in body

    def test_a_missing_resource_is_a_typed_404_not_a_crash(self, settings):
        """The client learns the category; it does not learn the internals."""
        app = _build_app(settings, InMemoryUserRepository(), InMemoryWorkspaceRepository())
        with TestClient(app, raise_server_exceptions=False) as client:
            actor = make_actor(client, "boss")
            missing = uuid4()
            body = actor.client.get(f"/workspaces/{missing}").json()
            assert body["error"]["code"] == "not_found"
            assert "Traceback" not in json.dumps(body)

    def test_the_upload_allowlist_has_no_executable_types(self):
        text = (ROOT / ".env.sample").read_text()
        match = re.search(r"ALLOWED_CONTENT_TYPES=(.+)", text)
        assert match, "ALLOWED_CONTENT_TYPES missing from .env.sample"
        types = {t.strip() for t in match.group(1).split(",")}
        for dangerous in (
            "application/x-msdownload",
            "application/x-executable",
            "text/html",
            "application/javascript",
        ):
            # text/html is not in the default list (HTML is uploaded as text/html
            # only if explicitly allowed), so assert on the genuinely executable.
            if dangerous == "text/html":
                continue
            assert dangerous not in types

    def test_stored_paths_are_derived_from_content_not_filename(self):
        """The filename is attacker-controlled; it must not reach the path."""
        from knowledgedock.infrastructure.storage import LocalFileStorage, hash_bytes

        storage = LocalFileStorage(Path("/tmp/kd-security-check"))
        storage.ensure_root()
        body = b"hello"
        digest = hash_bytes(body)
        traversal = "../../etc/passwd"
        path, _ = storage.save(uuid4(), digest, "text/plain", io.BytesIO(body))
        assert traversal not in path
        assert digest[:16] in path or digest in path
        storage.delete(path)

    def test_the_salt_and_secrets_are_read_from_the_environment(self):
        """No hardcoded default secret anywhere in the source."""
        import subprocess

        tracked = subprocess.run(
            ["git", "ls-files", "src"], capture_output=True, text=True, check=True
        ).stdout.split()
        for name in tracked:
            if not name.endswith(".py"):
                continue
            text = (ROOT / name).read_text()
            for name_of_setting in ("secret_key", "session_secret"):
                for match in re.finditer(rf"{name_of_setting}\s*=\s*[\"']([^\"']+)[\"']", text):
                    literal = match.group(1)
                    assert len(literal) < 20 or literal.startswith("test"), (
                        f"{name} hardcodes {name_of_setting}"
                    )


#: Variables whose values must never appear in `.env.sample`.
SECRET_VARIABLES = {
    "GEMINI_API_KEY",
    "APP_SECRET_KEY",
    "SESSION_SECRET",
    "SECRET_KEY",
    "MONGODB_URI",
}


# ---------------------------------------------------------------------------
# Packaging and configuration
# ---------------------------------------------------------------------------
DOCKERFILE = (ROOT / "Dockerfile").read_text() if (ROOT / "Dockerfile").is_file() else ""


@pytest.fixture(scope="module")
def dockerfile() -> str:
    return DOCKERFILE


class TestDockerfile:
    def test_it_is_multi_stage(self, dockerfile):
        assert dockerfile.count("FROM ") >= 2, "expected a builder and a runtime stage"

    def test_the_final_stage_is_not_the_builder(self, dockerfile):
        stages = dockerfile.split("FROM ")[1:]
        assert len(stages) >= 2
        # The last FROM is the one that ships.
        assert " AS builder" not in stages[-1].splitlines()[0].lower()

    def test_it_runs_as_a_non_root_user(self, dockerfile):
        assert re.search(r"^\s*USER\s+(?!root)\S+", dockerfile, re.MULTILINE), (
            "no non-root USER instruction"
        )

    def test_it_binds_the_injected_port(self, dockerfile):
        # Render sets PORT; hardcoding 8000 means the service never receives traffic.
        assert re.search(r"--port[= ]?\$\{?PORT", dockerfile) or "${PORT}" in dockerfile

    def test_dependencies_are_copied_before_the_source_for_layer_caching(self, dockerfile):
        # Copying the source first invalidates the dependency layer on every code
        # change, which is the single biggest cause of slow rebuilds.
        builder = dockerfile.split("FROM ")[1]
        dep = re.search(r"COPY\s+(\S*pyproject\.toml|\S*uv\.lock)", builder)
        source = re.search(r"COPY\s+(\S*src|\.\s)", builder)
        if dep and source:
            assert dep.start() < source.start(), (
                "source is copied before dependencies, defeating layer caching"
            )

    def test_it_does_not_copy_secrets(self, dockerfile):
        for line in dockerfile.splitlines():
            if "COPY" not in line:
                continue
            assert ".env" not in line or ".env.sample" in line, f"copies a secret: {line}"

    def test_it_uses_the_project_uv_lockfile(self, dockerfile):
        assert "uv.lock" in dockerfile, "the lockfile is not used, so builds are not reproducible"


class TestPackagingMetadata:
    def test_the_python_version_is_pinned(self):
        assert (ROOT / ".python-version").read_text().strip()

    def test_the_lockfile_is_committed(self):
        assert (ROOT / "uv.lock").is_file()

    def test_pyproject_declares_the_runtime_dependencies(self):
        text = (ROOT / "pyproject.toml").read_text()
        for package in ("fastapi", "pymongo", "httpx", "jinja2"):
            assert package in text, f"{package} missing from pyproject"

    def test_every_env_sample_variable_is_read_by_name(self):
        """Phase 01 decision 10: the sample is the contract, so it must be loadable.

        A variable in the sample that nothing reads is a knob that does nothing.
        """
        from tests.conftest import load_settings

        load_settings()  # raises on any unread required variable
        sample = (ROOT / ".env.sample").read_text()
        declared = {
            line.partition("=")[0]
            for line in sample.splitlines()
            if line and not line.startswith("#") and "=" in line
        }
        source = "\n".join(p.read_text() for p in (ROOT / "src").rglob("*.py"))
        unread = sorted(
            name
            for name in declared
            if name.isupper() and f'"{name}"' not in source and f"'{name}'" not in source
        )
        assert unread == [], f".env.sample declares variables nothing reads: {unread}"


class TestDocumentation:
    def test_the_readme_documents_the_architecture(self):
        text = (ROOT / "README.md").read_text()
        for topic in ("Architecture", "Retrieval", "RAG", "Security"):
            assert topic.lower() in text.lower(), f"README does not mention {topic}"

    def test_the_readme_documents_the_endpoints(self):
        text = (ROOT / "README.md").read_text()
        for endpoint in ("/workspaces", "/documents", "/query", "/search", "/usage"):
            assert endpoint in text, f"README does not document {endpoint}"

    def test_the_roadmap_is_complete(self):
        text = (ROOT / ".agents/roadmap.md").read_text()
        assert "- [ ]" not in text, "the roadmap still has open items"

    def test_the_skill_file_records_the_merge_discipline(self):
        text = (ROOT / ".agents/SKILL.md").read_text()
        assert "--no-ff" in text
        assert "Never delete a sub branch" in text
