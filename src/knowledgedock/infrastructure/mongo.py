"""MongoDB connection lifecycle.

The client is created once at startup and reused for the process lifetime.
Connections are lazy: `ping()` on the lifespan is what proves Atlas is actually
reachable, so `/health/ready` can report it honestly.
"""

from __future__ import annotations

import logging
from typing import Any

from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import PyMongoError

from knowledgedock.core.config import Settings

logger = logging.getLogger(__name__)


class MongoManager:
    """Owns the async client and exposes the application database."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: AsyncMongoClient[dict[str, Any]] | None = None

    async def connect(self) -> None:
        self._client = AsyncMongoClient(
            self._settings.mongodb_uri,
            maxPoolSize=self._settings.mongodb_max_pool_size,
            serverSelectionTimeoutMS=self._settings.mongodb_server_selection_timeout_ms,
            connectTimeoutMS=self._settings.mongodb_connect_timeout_ms,
            socketTimeoutMS=self._settings.mongodb_socket_timeout_ms,
            appname="knowledgedock",
        )
        if await self.ping():
            logger.info(
                "mongodb.connected",
                extra={"database": self._settings.mongodb_db},
            )
        else:
            # Do not raise: on Render the app must still bind $PORT so the
            # health endpoint stays reachable and /health/ready reports the
            # real state. Startup does not become a crash loop on a transient
            # Atlas outage.
            logger.warning(
                "mongodb.connect_unavailable",
                extra={"database": self._settings.mongodb_db},
            )

    async def ping(self) -> bool:
        if self._client is None:
            return False
        try:
            await self._client.admin.command("ping")
        except PyMongoError as exc:
            # Called on every probe, so log one line rather than a traceback.
            logger.warning("mongodb.ping_failed", extra={"error": str(exc)[:300]})
            return False
        return True

    def database(self) -> AsyncDatabase[dict[str, Any]]:
        if self._client is None:
            raise RuntimeError("MongoDB is not connected; call connect() during startup")
        return self._client[self._settings.mongodb_db]

    async def disconnect(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
            logger.info("mongodb.disconnected")
