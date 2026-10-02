"""Shared test fixtures.

Settings are built from an explicit environment so tests never depend on a
developer's local `.env`. MongoDB is replaced through the `mongo_manager`
seam on `create_app`: these tests verify the HTTP contract and configuration
parsing, not Atlas connectivity (that is exercised by the deployment smoke test).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from knowledgedock.app import create_app
from knowledgedock.core.config import Settings

TEST_ENV: dict[str, str] = {
    "ENVIRONMENT": "test",
    "SECRET_KEY": "test-secret-key-long-enough-to-pass-validation-0123456789",
    "MONGODB_URI": "mongodb://localhost:27017",
    "MONGODB_DB": "knowledgedock_test",
    "AI_PROVIDER": "null",
    "ALLOWED_CONTENT_TYPES": "text/plain,text/markdown",
}


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
def settings() -> Settings:
    # _env_file=None keeps the developer's real .env out of the test run.
    return Settings(**TEST_ENV, _env_file=None)  # type: ignore[arg-type]


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
