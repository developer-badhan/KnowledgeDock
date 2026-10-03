"""Chunk persistence.

Chunks are written twice during processing: once with `embedding: None` so the
count is recorded, then again with vectors. That is deliberate — it means a
document is never marked READY while its vectors are missing, and a crash between
the two leaves chunks that Phase 6's search will simply skip, because a `None`
vector has nothing to match against.

The `replace_for_document` helper is how re-upload stays clean: Phase 4 already
discarded the old chunks, and this guarantees the set is exactly what was just
produced rather than an accumulation.
"""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

from pymongo import ASCENDING
from pymongo.errors import BulkWriteError, DuplicateKeyError

from knowledgedock.domain.errors import ServiceUnavailable


class ChunkRepository(Protocol):
    async def ensure_indexes(self) -> None: ...

    async def replace_for_document(
        self, document_id: UUID, workspace_id: UUID, chunks: list[dict[str, Any]]
    ) -> int: ...

    async def count_for_document(self, document_id: UUID) -> int: ...


class MongoChunkRepository:
    def __init__(self, database: Any) -> None:
        self._chunks = database["document_chunks"]

    async def ensure_indexes(self) -> None:
        # One chunk per (document, index). Re-processing the same document twice
        # must not silently double its retrievable text.
        await self._chunks.create_index(
            [("document_id", ASCENDING), ("chunk_index", ASCENDING)],
            unique=True,
            name="chunks_document_index_unique",
        )
        # The only index used by an explicit query today. The vector index below
        # is an Atlas Search index and does not count against this budget.
        await self._chunks.create_index([("workspace_id", ASCENDING)], name="chunks_workspace")

    async def replace_for_document(
        self, document_id: UUID, workspace_id: UUID, chunks: list[dict[str, Any]]
    ) -> int:
        if not chunks:
            await self._chunks.delete_many({"document_id": document_id})
            return 0
        try:
            await self._chunks.delete_many({"document_id": document_id})
            await self._chunks.insert_many(chunks, ordered=True)
        except (DuplicateKeyError, BulkWriteError) as exc:
            raise ServiceUnavailable(
                "Could not store the document chunks.",
                detail=f"{type(exc).__name__}: {exc}",
            ) from exc
        return len(chunks)

    async def count_for_document(self, document_id: UUID) -> int:
        return await self._chunks.count_documents({"document_id": document_id})


class InMemoryChunkRepository:
    def __init__(self) -> None:
        self.chunks: dict[UUID, list[dict[str, Any]]] = {}

    async def ensure_indexes(self) -> None:
        return None

    async def replace_for_document(
        self, document_id: UUID, workspace_id: UUID, chunks: list[dict[str, Any]]
    ) -> int:
        del workspace_id
        self.chunks[document_id] = list(chunks)
        return len(chunks)

    async def count_for_document(self, document_id: UUID) -> int:
        return len(self.chunks.get(document_id, []))


class UnavailableChunkRepository:
    """Stands in when the chunk collection is unreachable at startup.

    Without this, `MongoChunkRepository(mongo.database())` raises before the
    process binds `$PORT`, which is exactly the crash-on-startup that Phase 01
    decision 2 forbids.
    """

    _message = "Document chunks are unavailable. The database connection failed at startup."

    def __init__(self, cause: Exception | None = None) -> None:
        self.cause = cause

    async def ensure_indexes(self) -> None:
        return None

    async def replace_for_document(
        self, document_id: UUID, workspace_id: UUID, chunks: list[dict[str, Any]]
    ) -> int:
        raise self._error()

    async def count_for_document(self, document_id: UUID) -> int:
        raise self._error()

    def _error(self) -> Exception:
        from knowledgedock.domain.errors import ServiceUnavailable

        return ServiceUnavailable(self._message, detail=f"cause={self.cause!r}")
