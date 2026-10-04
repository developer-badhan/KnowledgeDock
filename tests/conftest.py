"""Shared test fixtures.

Settings are read through `python-decouple` from a throwaway `.env` file, so the
developer's real `.env` is never touched. MongoDB is replaced through the
`mongo_manager` seam on `create_app`, and the user repository is replaced with
its in-memory double, so the whole suite runs without a database. These tests
verify HTTP contract and configuration; the deployment smoke test covers Atlas.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from decouple import Config, RepositoryEnv
from fastapi.testclient import TestClient

from knowledgedock.app import create_app
from knowledgedock.core.config import Settings, load_settings
from knowledgedock.infrastructure.ai.embedding import NullEmbeddingProvider
from knowledgedock.infrastructure.ai.llm import NullLLMProvider
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
from knowledgedock.infrastructure.repositories.user_repository import InMemoryUserRepository
from knowledgedock.infrastructure.repositories.workspace_repository import (
    InMemoryWorkspaceRepository,
)
from knowledgedock.infrastructure.storage import LocalFileStorage

PASSWORD = "a-perfectly-fine-password"

BASE_ENV = """\
ENVIRONMENT=test
SECRET_KEY=test-secret-key-long-enough-to-pass-validation-0123456789
MONGODB_URI=mongodb://localhost:27017
MONGODB_DB=knowledgedock_test
# Declared as gemini even though every test injects stub providers. The null
# providers are refused by /search and /query, because answering from hash-derived
# vectors would return confident nonsense; tests that want null mode build a
# settings object with it explicitly.
AI_PROVIDER=gemini
GEMINI_API_KEY=test-key
GEMINI_EMBEDDING_TASK_DOCUMENT=RETRIEVAL_DOCUMENT
GEMINI_EMBEDDING_TASK_QUERY=RETRIEVAL_QUERY
ALLOWED_CONTENT_TYPES=text/plain,text/markdown,text/html,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document
# `create_app` calls `configure_logging`, which clears the root logger's
# handlers and installs its own stdout stream. That is right in production and
# wrong in a test run, where it buries assertion failures under JSON. Tests run
# at CRITICAL; flip to INFO when debugging a specific failure.
LOG_LEVEL=CRITICAL
# Production polls every 10s. Tests that exercise the live worker override this so
# they are not racing the production poll cadence -- at 10s with a 10s deadline,
# whether a document is picked up in time is a coin flip.
PROCESSING_POLL_INTERVAL_SECONDS=1
"""

# Derived from .env.sample so the list cannot drift from the real contract.
APP_ENV_KEYS = tuple(
    line.split("=", 1)[0].strip()
    for line in (Path(__file__).resolve().parents[1] / ".env.sample")
    .read_text(encoding="utf-8")
    .splitlines()
    if line.strip() and not line.startswith("#") and "=" in line
)


@pytest.fixture(autouse=True)
def _isolate_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's real shell environment out of the tests.

    decouple resolves `os.environ` before the `.env` file, so a variable that
    was exported in the shell (a stray `GEMINI_API_KEY`, for example) would
    silently override the throwaway fixture and make tests pass or fail
    depending on whose terminal ran them.
    """
    for key in APP_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


class FakeDatabase:
    """Minimal stand-in for an `AsyncDatabase`, enough to construct a repository.

    Nothing here executes queries. Its job is to let a test build the *production*
    object graph so the wiring is exercised rather than bypassed.
    """

    def __getitem__(self, name: str) -> Any:
        return _FakeCollection(name)


class _FakeCollection:
    """Answers the calls a repository makes at startup, and nothing more.

    Enough to construct the production object graph so the wiring is exercised.
    Queries against it are not meaningful — tests that assert on data inject
    `InMemory*Repository` instead.
    """

    def __init__(self, name: str) -> None:
        self.name = name

    async def create_index(self, *args: Any, **kwargs: Any) -> str:
        return f"{self.name}_index"

    async def count_documents(self, *args: Any, **kwargs: Any) -> int:
        return 0

    def find(self, *args: Any, **kwargs: Any) -> Any:
        # Deliberately NOT async: PyMongo's async collection returns the cursor
        # synchronously and the cursor is what you await `.to_list()` on.
        return _FakeCursor([])

    async def find_one(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def find_one_and_update(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def find_one_and_delete(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def insert_one(self, *args: Any, **kwargs: Any) -> Any:
        return None

    async def insert_many(self, *args: Any, **kwargs: Any) -> Any:
        return None

    async def delete_one(self, *args: Any, **kwargs: Any) -> Any:
        return _FakeResult()

    async def delete_many(self, *args: Any, **kwargs: Any) -> Any:
        return _FakeResult()

    async def update_one(self, *args: Any, **kwargs: Any) -> Any:
        return _FakeResult()

    async def list_search_indexes(self) -> Any:
        return _FakeCursor([])

    async def create_search_index(self, *args: Any, **kwargs: Any) -> str:
        return "vector_index"


class _FakeResult:
    deleted_count = 0
    matched_count = 0
    upserted_id = None


class _FakeCursor:
    def __init__(self, items: list) -> None:
        self._items = items

    async def to_list(self, length: Any = None) -> list:
        return self._items

    def sort(self, *args: Any, **kwargs: Any) -> _FakeCursor:
        return self

    def skip(self, *args: Any, **kwargs: Any) -> _FakeCursor:
        return self

    def limit(self, *args: Any, **kwargs: Any) -> _FakeCursor:
        return self

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class FakeMongoManager:
    """Stands in for MongoManager so tests run without a live database.

    `database()` mirrors the real one: it raises until `connect()` has run, and
    records the ordering. That constraint is what caught the startup bug where the
    user repository was constructed before the client existed.
    """

    def __init__(
        self,
        *,
        reachable: bool = True,
        connected: bool = True,
        database: FakeDatabase | None = None,
    ) -> None:
        self.reachable = reachable
        self._connected = connected
        self._database = database if database is not None else FakeDatabase()
        self.connect_called = False
        self.database_called_after_connect = False

    async def ping(self) -> bool:
        return self.reachable

    async def connect(self) -> None:
        self.connect_called = True
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    def database(self) -> Any:
        if not self._connected:
            raise RuntimeError("MongoDB is not connected; call connect() during startup")
        self.database_called_after_connect = self.connect_called
        return self._database


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    path = tmp_path / ".env"
    path.write_text(BASE_ENV, encoding="utf-8")
    return path


@pytest.fixture
def settings(env_file: Path) -> Settings:
    # _env_file=None keeps the developer's real .env out of the test run.
    return load_settings(Config(RepositoryEnv(str(env_file))))


@pytest.fixture
def load_from(tmp_path: Path):
    """Factory: build Settings from an ad-hoc .env body."""
    counter = iter(range(1000))

    def _load(extra: str = "") -> Settings:
        index = next(counter)
        path = tmp_path / f".env.{index}"
        path.write_text(BASE_ENV + extra, encoding="utf-8")
        return load_settings(Config(RepositoryEnv(str(path))))

    return _load


@pytest.fixture
def users() -> InMemoryUserRepository:
    return InMemoryUserRepository()


def _build_app(
    settings,
    users,
    workspaces,
    reachable=True,
    documents=None,
    storage=None,
    chunks=None,
    embeddings=None,
    llm=None,
    conversations=None,
    usage=None,
    clock=None,
    processing_enabled=False,
):
    return create_app(
        settings,
        mongo_manager=FakeMongoManager(reachable=reachable),
        user_repository=users,
        workspace_repository=workspaces,
        document_repository=(documents if documents is not None else InMemoryDocumentRepository()),
        chunk_repository=chunks if chunks is not None else InMemoryChunkRepository(),
        storage=storage if storage is not None else LocalFileStorage(make_storage_root()),
        embeddings=embeddings or NullEmbeddingProvider(),
        llm=llm or NullLLMProvider(),
        conversation_repository=conversations or InMemoryConversationRepository(),
        usage_repository=usage or InMemoryAIUsageRepository(),
        clock=clock,
        processing_enabled=processing_enabled,
    )


def make_storage_root() -> str:
    """A private upload directory per app instance.

    Tests share one process, so a fixed path would let one test's upload appear
    in another's assertions.
    """
    return tempfile.mkdtemp(prefix="kd-test-uploads-")


def _client(
    settings: Settings,
    *,
    reachable: bool = True,
    users: InMemoryUserRepository | None = None,
    workspaces: InMemoryWorkspaceRepository | None = None,
    documents: InMemoryDocumentRepository | None = None,
    storage: LocalFileStorage | None = None,
    chunks: InMemoryChunkRepository | None = None,
    embeddings=None,
    processing_enabled: bool = False,
) -> Iterator[TestClient]:
    app = _build_app(
        settings,
        users if users is not None else InMemoryUserRepository(),
        workspaces if workspaces is not None else InMemoryWorkspaceRepository(),
        reachable=reachable,
        documents=documents,
        storage=storage,
        chunks=chunks,
        embeddings=embeddings,
        processing_enabled=processing_enabled,
    )
    with TestClient(app) as test_client:
        test_client.users = app.state.register_user._repository  # type: ignore[attr-defined]
        test_client.workspaces = app.state.create_workspace._workspaces  # type: ignore[attr-defined]
        yield test_client


@pytest.fixture
def client(settings: Settings, users: InMemoryUserRepository) -> Iterator[TestClient]:
    yield from _client(settings, users=users)


@pytest.fixture
def workspaces() -> InMemoryWorkspaceRepository:
    return InMemoryWorkspaceRepository()


@pytest.fixture
def ws_client(settings: Settings, workspaces: InMemoryWorkspaceRepository) -> Iterator[TestClient]:
    yield from _client(settings, workspaces=workspaces)


@pytest.fixture
def documents() -> InMemoryDocumentRepository:
    return InMemoryDocumentRepository()


@pytest.fixture
def live_doc_client(
    settings: Settings,
    workspaces: InMemoryWorkspaceRepository,
    documents: InMemoryDocumentRepository,
) -> Iterator[TestClient]:
    """A client whose ingestion worker is actually running."""
    yield from _client(
        settings, workspaces=workspaces, documents=documents, processing_enabled=True
    )


@pytest.fixture
def doc_client(
    settings: Settings,
    workspaces: InMemoryWorkspaceRepository,
    documents: InMemoryDocumentRepository,
) -> Iterator[TestClient]:
    yield from _client(settings, workspaces=workspaces, documents=documents)


@pytest.fixture
def degraded_client(settings: Settings) -> Iterator[TestClient]:
    yield from _client(settings, reachable=False)


@pytest.fixture
def production_client(settings: Settings) -> Iterator[TestClient]:
    """A client running with ENVIRONMENT=production."""
    import dataclasses

    prod = dataclasses.replace(settings, environment="production")
    app = _build_app(prod, InMemoryUserRepository(), InMemoryWorkspaceRepository())
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def new_client(
    settings: Settings, users: InMemoryUserRepository, workspaces: InMemoryWorkspaceRepository
):
    """Factory for independent clients that share one app and its repositories.

    Each returned client has its own cookie jar, which is what makes an
    isolation test meaningful: two actors must not accidentally share a session.
    Only the first client enters the lifespan, so `app.state` (and therefore the
    repositories) is built once and shared.
    """
    app = _build_app(settings, users, workspaces)
    with TestClient(app) as primary:
        clients: list[TestClient] = [primary]

        def factory() -> TestClient:
            client = TestClient(app)
            clients.append(client)
            return client

        yield factory

        for extra in clients[1:]:
            extra.close()


# --------------------------------------------------------------------------
# Workspace helpers
# --------------------------------------------------------------------------
@dataclass
class Actor:
    """A signed-in test client plus its user id."""

    client: TestClient
    user_id: str
    email: str


def make_actor(client: TestClient, label: str, password: str = PASSWORD) -> Actor:
    """Register and sign in, returning an actor bound to its own cookie jar.

    `client` must be a distinct client per actor. Sharing one would give every
    actor the same session, and an isolation test would silently assert nothing.
    The address is suffixed with a random tag so repeated runs do not collide in
    the shared in-memory repository.
    """
    address = f"{label}-{uuid4().hex[:8]}@example.com"
    response = client.post("/auth/register", json={"email": address, "password": password})
    assert response.status_code == 201, response.text
    login = client.post("/auth/login", json={"email": address, "password": password})
    assert login.status_code == 200, login.text
    return Actor(client=client, user_id=response.json()["id"], email=address)


def workspace_of(client: TestClient, name: str = "Acme") -> dict:
    response = client.post("/workspaces", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()


def assert_hidden_from_outsider(real: object, missing: object, label: str) -> None:
    """Assert a refusal is indistinguishable from the resource not existing.

    The workspace boundary is the only access control in KnowledgeDock: every
    member of a workspace can see everything in it, and the uploader is
    irrelevant. So there is exactly one refusal for a resource you cannot reach —
    404 — and it must be indistinguishable from a 404 for an id that never
    existed.

    A 403, or a 404 with a different body, would confirm the resource exists and
    leak existence across tenants. Both responses are compared, not just the
    status code.
    """
    assert real.status_code == 404, f"{label}: expected 404, got {real.status_code}"
    assert missing.status_code == 404, f"{label}: expected 404 for absent id"
    assert real.json() == missing.json(), (
        f"{label}: a refusal must be byte-identical to the absent-resource response"
    )


class _EmptyResult:
    deleted_count = 0
    matched_count = 0
    upserted_id = None


class _EmptyCursor:
    """A cursor that yields nothing, for hand-rolled dead collections."""

    async def to_list(self, length: Any = None) -> list:
        return []
