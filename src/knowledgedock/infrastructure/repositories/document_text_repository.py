"""Extracted document text, kept durable.

Render's filesystem is ephemeral: `STORAGE_DIR` lives under `/tmp` and is wiped
on every deploy or restart. Ingestion used to re-open the stored raw file to
extract text, so a deploy landing between an upload and its processing turned the
worker's retry into `StorageError` — a permanent, confusing failure of a file the
user uploaded successfully.

The text is now extracted once, at upload time, and stored here. Ingestion never
needs the raw file again, a restart cannot lose it, and a retry after a provider
429 re-embeds from the same durable text instead of re-extracting a gone file.

The collection is deliberately separate from `documents` so nothing that lists
documents drags a whole file's text across the wire. The primary key is the
document id.
"""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

from pymongo import ASCENDING


class DocumentTextRepository(Protocol):
    async def ensure_indexes(self) -> None: ...

    async def save(self, document_id: UUID, workspace_id: UUID, text: str) -> None: ...

    async def get(self, document_id: UUID) -> str | None: ...

    async def delete(self, document_id: UUID) -> None: ...


class MongoDocumentTextRepository:
    def __init__(self, database: Any) -> None:
        self._texts = database["document_texts"]

    async def ensure_indexes(self) -> None:
        # _id is already the document id, unique by default. The workspace is
        # indexed so a future workspace purge can clear texts in one pass.
        await self._texts.create_index(
            [("workspace_id", ASCENDING)], name="document_texts_workspace"
        )

    async def save(self, document_id: UUID, workspace_id: UUID, text: str) -> None:
        await self._texts.replace_one(
            {"_id": document_id},
            {"_id": document_id, "workspace_id": workspace_id, "text": text},
            upsert=True,
        )

    async def get(self, document_id: UUID) -> str | None:
        found = await self._texts.find_one({"_id": document_id}, {"text": 1})
        return found["text"] if found else None

    async def delete(self, document_id: UUID) -> None:
        await self._texts.delete_one({"_id": document_id})


class InMemoryDocumentTextRepository:
    """Test double mirroring the Mongo semantics."""

    def __init__(self) -> None:
        self.texts: dict[UUID, str] = {}

    async def ensure_indexes(self) -> None:
        return None

    async def save(self, document_id: UUID, workspace_id: UUID, text: str) -> None:
        del workspace_id
        self.texts[document_id] = text

    async def get(self, document_id: UUID) -> str | None:
        return self.texts.get(document_id)

    async def delete(self, document_id: UUID) -> None:
        self.texts.pop(document_id, None)


class UnavailableDocumentTextRepository:
    """Stands in when the database is unreachable at startup.

    Same reasoning as the other Unavailable repositories: an outage is reported
    as an outage, not as missing text.
    """

    _message = "Document text is unavailable. The database connection failed at startup."

    def __init__(self, cause: Exception | None = None) -> None:
        self.cause = cause

    async def ensure_indexes(self) -> None:
        return None

    async def save(self, document_id: UUID, workspace_id: UUID, text: str) -> None:
        del document_id, workspace_id, text
        raise self._error()

    async def get(self, document_id: UUID) -> str | None:
        del document_id
        raise self._error()

    async def delete(self, document_id: UUID) -> None:
        del document_id
        raise self._error()

    def _error(self) -> Exception:
        from knowledgedock.domain.errors import ServiceUnavailable

        return ServiceUnavailable(self._message, detail=f"cause={self.cause!r}")
