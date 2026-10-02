"""Shared test fixtures.

Settings are read through `python-decouple` from a throwaway `.env` file, so the
developer's real `.env` is never touched. MongoDB is replaced through the
`mongo_manager` seam on `create_app`: these tests verify the HTTP contract and
configuration loading, not Atlas connectivity (the deployment smoke test covers
that).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from decouple import Config, RepositoryEnv
from fastapi.testclient import TestClient

from knowledgedock.app import create_app
from knowledgedock.core.config import Settings, load_settings

BASE_ENV = """\
ENVIRONMENT=test
SECRET_KEY=test-secret-key-long-enough-to-pass-validation-0123456789
MONGODB_URI=mongodb://localhost:27017
MONGODB_DB=knowledgedock_test
AI_PROVIDER=null
GEMINI_API_KEY=test-key
GEMINI_EMBEDDING_TASK_DOCUMENT=RETRIEVAL_DOCUMENT
GEMINI_EMBEDDING_TASK_QUERY=RETRIEVAL_QUERY
ALLOWED_CONTENT_TYPES=text/plain,text/markdown
"""


class FakeMongoManager:
    """Stands in for MongoManager so tests run without a live database."""

    def __init__(self, *, reachable: bool = True) -> None:
        self.reachable = reachable
        self.connected = False

    async def ping(self) -> bool:
        return self.reachable

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    def database(self) -> Any:  # pragma: no cover - not used in phase 1
        raise NotImplementedError


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    path = tmp_path / ".env"
    path.write_text(BASE_ENV, encoding="utf-8")
    return path


@pytest.fixture
def settings(env_file: Path) -> Settings:
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


def _client(settings: Settings, *, reachable: bool) -> Iterator[TestClient]:
    app = create_app(settings, mongo_manager=FakeMongoManager(reachable=reachable))
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    yield from _client(settings, reachable=True)


@pytest.fixture
def degraded_client(settings: Settings) -> Iterator[TestClient]:
    yield from _client(settings, reachable=False)
