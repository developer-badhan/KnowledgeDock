"""Document persistence.

Indexes follow the query patterns Phase 4 and Phase 5 actually use:

  * find one document, scoped to a workspace -> `_id` lookup, filtered
  * list a workspace's documents, newest first -> `(workspace_id, created_at desc)`
  * find a document by content, to decide replace-or-create ->
    **unique** `(workspace_id, content_hash)`

That unique index is load-bearing. It is what makes "one document per distinct
content per workspace" a database guarantee rather than a convention, so two
concurrent uploads of the same bytes cannot both create a row. The loser gets a
`DuplicateKeyError` and re-reads the winner.

Deleting a document hard-deletes its chunks. `SKILL.md` §9 wants processing to be
safe to repeat, and orphaned chunks would otherwise be returned by Phase 6's
vector search for a document that no longer exists — a cross-tenant leak waiting
to happen, since the query filters on `workspace_id` but not on document
existence.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

from knowledgedock.domain.documents import Document, DocumentPage, DocumentStatus


class DuplicateContentError(Exception):
    """The unique `(workspace_id, content_hash)` index rejected an insert."""


class DocumentRepository(Protocol):
    async def ensure_indexes(self) -> None: ...

    async def create(self, document: Document) -> Document: ...

    async def find_scoped(self, workspace_id: UUID, document_id: UUID) -> Document | None: ...

    async def find_by_content(self, workspace_id: UUID, content_hash: str) -> Document | None: ...

    async def list_for_workspace(
        self, workspace_id: UUID, *, limit: int, offset: int
    ) -> DocumentPage: ...

    async def replace_for_reupload(self, document: Document) -> Document: ...

    async def delete(self, workspace_id: UUID, document_id: UUID) -> bool: ...

    async def delete_chunks(self, document_id: UUID) -> int: ...

    async def count_chunks(self, document_id: UUID) -> int: ...

    async def claim_next_pending(self) -> Document | None: ...

    async def find_stale_processing(self, cutoff: datetime) -> list[Document]: ...

    async def find_retryable_failures(self, max_attempts: int) -> list[Document]: ...


class MongoDocumentRepository:
    def __init__(self, database: Any) -> None:
        self._documents = database["documents"]
        self._chunks = database["document_chunks"]

    async def ensure_indexes(self) -> None:
        # Identity. Also decides replace-or-create on upload.
        await self._documents.create_index(
            [("workspace_id", ASCENDING), ("content_hash", ASCENDING)],
            unique=True,
            name="documents_workspace_hash_unique",
        )
        # The listing query: one workspace, newest first.
        await self._documents.create_index(
            [("workspace_id", ASCENDING), ("created_at", DESCENDING)],
            name="documents_workspace_recent",
        )
        # Phase 5 lists what a worker should pick up.
        await self._documents.create_index(
            [("status", ASCENDING), ("created_at", ASCENDING)],
            name="documents_status_queue",
        )
        # Phase 5/6 look up chunks by document, and Phase 6 filters by workspace.
        await self._chunks.create_index(
            [("document_id", ASCENDING), ("chunk_index", ASCENDING)],
            unique=True,
            name="chunks_document_index_unique",
        )
        await self._chunks.create_index([("workspace_id", ASCENDING)], name="chunks_workspace")

    async def create(self, document: Document) -> Document:
        try:
            await self._documents.insert_one(document.to_document())
        except DuplicateKeyError as exc:
            raise DuplicateContentError from exc
        return document

    async def find_scoped(self, workspace_id: UUID, document_id: UUID) -> Document | None:
        # Both fields in one query. Fetching by `_id` and comparing afterwards
        # would let "wrong workspace" and "does not exist" diverge in timing.
        found = await self._documents.find_one({"_id": document_id, "workspace_id": workspace_id})
        return Document.from_document(found) if found else None

    async def find_by_content(self, workspace_id: UUID, content_hash: str) -> Document | None:
        found = await self._documents.find_one(
            {"workspace_id": workspace_id, "content_hash": content_hash}
        )
        return Document.from_document(found) if found else None

    async def list_for_workspace(
        self, workspace_id: UUID, *, limit: int, offset: int
    ) -> DocumentPage:
        query = {"workspace_id": workspace_id}
        total = await self._documents.count_documents(query)
        found = (
            await self._documents.find(query)
            .sort("created_at", DESCENDING)
            .skip(offset)
            .limit(limit)
            .to_list(length=limit)
        )
        return DocumentPage(
            items=[Document.from_document(d) for d in found],
            total=total,
            limit=limit,
            offset=offset,
        )

    async def replace_for_reupload(self, document: Document) -> Document:
        """Overwrite the metadata of an existing document, keeping its id.

        `_id` is intentionally absent from the update so this cannot become an
        upsert that silently inserts.
        """
        payload = document.to_document()
        payload.pop("_id")
        await self._documents.update_one({"_id": document.id}, {"$set": payload})
        return document

    async def delete(self, workspace_id: UUID, document_id: UUID) -> bool:
        # Chunks first. If this fails the document row survives and a retry can
        # finish the job; the reverse order would leave chunks with no document.
        await self.delete_chunks(document_id)
        result = await self._documents.delete_one(
            {"_id": document_id, "workspace_id": workspace_id}
        )
        return result.deleted_count == 1

    async def delete_chunks(self, document_id: UUID) -> int:
        result = await self._chunks.delete_many({"document_id": document_id})
        return result.deleted_count

    async def count_chunks(self, document_id: UUID) -> int:
        return await self._chunks.count_documents({"document_id": document_id})

    async def claim_next_pending(self) -> Document | None:
        """Atomically take the oldest PENDING document and mark it PROCESSING.

        The filter and the update are one operation, so two workers cannot claim
        the same document. A plain `find_one` followed by an `update_one` would have
        a window between them.
        """
        document = await self._documents.find_one_and_update(
            {"status": str(DocumentStatus.PENDING)},
            {"$set": {"status": str(DocumentStatus.PROCESSING)}},
            sort=[("created_at", ASCENDING)],
            return_document=ReturnDocument.AFTER,
        )
        return Document.from_document(document) if document else None

    async def find_stale_processing(self, cutoff: datetime) -> list[Document]:
        """Documents stuck in PROCESSING since before `cutoff`.

        These were interrupted by a restart. `attempts` is left intact so the
        retry budget still applies.
        """
        found = await self._documents.find(
            {"status": str(DocumentStatus.PROCESSING), "updated_at": {"$lt": cutoff}}
        ).to_list(length=None)
        return [Document.from_document(d) for d in found]

    async def find_retryable_failures(self, max_attempts: int) -> list[Document]:
        """Failed documents that still have attempts left."""
        found = await self._documents.find(
            {
                "status": str(DocumentStatus.FAILED),
                "attempts": {"$lt": max_attempts},
            }
        ).to_list(length=None)
        return [Document.from_document(d) for d in found]


class InMemoryDocumentRepository:
    """Test double with the same semantics as `MongoDocumentRepository`."""

    def __init__(self) -> None:
        self.documents: dict[UUID, Document] = {}
        self.chunks: dict[UUID, list] = {}

    async def ensure_indexes(self) -> None:
        return None

    async def create(self, document: Document) -> Document:
        for existing in self.documents.values():
            if (
                existing.workspace_id == document.workspace_id
                and existing.content_hash == document.content_hash
            ):
                raise DuplicateContentError
        self.documents[document.id] = document
        return document

    async def find_scoped(self, workspace_id: UUID, document_id: UUID) -> Document | None:
        found = self.documents.get(document_id)
        if found is None or found.workspace_id != workspace_id:
            return None
        return found

    async def find_by_content(self, workspace_id: UUID, content_hash: str) -> Document | None:
        for document in self.documents.values():
            if document.workspace_id == workspace_id and document.content_hash == content_hash:
                return document
        return None

    async def list_for_workspace(
        self, workspace_id: UUID, *, limit: int, offset: int
    ) -> DocumentPage:
        mine = [d for d in self.documents.values() if d.workspace_id == workspace_id]
        mine.sort(key=lambda d: d.created_at, reverse=True)
        return DocumentPage(
            items=mine[offset : offset + limit],
            total=len(mine),
            limit=limit,
            offset=offset,
        )

    async def replace_for_reupload(self, document: Document) -> Document:
        self.documents[document.id] = document
        self.chunks[document.id] = []
        return document

    async def delete(self, workspace_id: UUID, document_id: UUID) -> bool:
        existing = self.documents.get(document_id)
        if existing is None or existing.workspace_id != workspace_id:
            return False
        del self.documents[document_id]
        self.chunks.pop(document_id, None)
        return True

    async def delete_chunks(self, document_id: UUID) -> int:
        return len(self.chunks.pop(document_id, []))

    async def count_chunks(self, document_id: UUID) -> int:
        return len(self.chunks.get(document_id, []))

    async def claim_next_pending(self) -> Document | None:
        pending = [d for d in self.documents.values() if d.status is DocumentStatus.PENDING]
        if not pending:
            return None
        pending.sort(key=lambda d: d.created_at)
        chosen = pending[0]
        # Mirror the atomic find_one_and_update: the claimed document is already
        # PROCESSING in the returned object, exactly as the driver would.
        claimed = chosen.transition_to(DocumentStatus.PROCESSING)
        self.documents[chosen.id] = claimed
        return claimed

    async def find_stale_processing(self, cutoff: datetime) -> list[Document]:
        return [
            d
            for d in self.documents.values()
            if d.status is DocumentStatus.PROCESSING and d.updated_at < cutoff
        ]

    async def find_retryable_failures(self, max_attempts: int) -> list[Document]:
        return [
            d
            for d in self.documents.values()
            if d.status is DocumentStatus.FAILED and d.attempts < max_attempts
        ]


class UnavailableDocumentRepository:
    """Stands in when the document collections are unreachable at startup.

    Same reasoning as the user and workspace equivalents: an outage is reported
    as an outage, not as "this workspace has no documents", which would be
    indistinguishable from a genuinely empty workspace.
    """

    _message = "Documents are unavailable. The database connection failed at startup."

    def __init__(self, cause: Exception | None = None) -> None:
        self.cause = cause

    async def ensure_indexes(self) -> None:
        return None

    async def create(self, document: Document) -> Document:
        raise self._error()

    async def find_scoped(self, workspace_id: UUID, document_id: UUID) -> Document | None:
        raise self._error()

    async def find_by_content(self, workspace_id: UUID, content_hash: str) -> Document | None:
        raise self._error()

    async def list_for_workspace(
        self, workspace_id: UUID, *, limit: int, offset: int
    ) -> DocumentPage:
        raise self._error()

    async def replace_for_reupload(self, document: Document) -> Document:
        raise self._error()

    async def delete(self, workspace_id: UUID, document_id: UUID) -> bool:
        raise self._error()

    async def delete_chunks(self, document_id: UUID) -> int:
        raise self._error()

    async def count_chunks(self, document_id: UUID) -> int:
        raise self._error()

    async def claim_next_pending(self) -> Document | None:
        raise self._error()

    async def find_stale_processing(self, cutoff: datetime) -> list[Document]:
        raise self._error()

    async def find_retryable_failures(self, max_attempts: int) -> list[Document]:
        raise self._error()

    def _error(self) -> Exception:
        from knowledgedock.domain.errors import ServiceUnavailable

        return ServiceUnavailable(self._message, detail=f"cause={self.cause!r}")
