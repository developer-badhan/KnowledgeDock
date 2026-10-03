"""Document entity and the processing state machine.

Two decisions shape this file.

**The state machine is explicit.** A client cannot set `status`; nothing accepts
it as input. Every change goes through `transition_to`, which consults
`ALLOWED_TRANSITIONS`. Without that, a document could jump from `PENDING` to
`READY` with no chunks behind it, and a retry could silently resurrect a document
whose processing had already failed. `SKILL.md` §7 requires the transitions to be
explicit and forbids arbitrary client-driven status changes.

**Re-upload replaces in place.** A workspace holds at most one document per
distinct content, enforced by a unique `(workspace_id, content_hash)` index.
Uploading the same bytes again updates that document and requeues it, rather than
creating a duplicate or rejecting the request. `SKILL.md` §9 asks for processing
that survives a worker running twice; content-addressed identity is what makes a
retry idempotent rather than additive.

`uploaded_by` is recorded for display only. It is deliberately not part of any
access check: the workspace is the boundary (see roadmap decision 31).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from knowledgedock.domain.errors import Conflict
from knowledgedock.domain.users import utcnow


class DocumentStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


#: The only permitted transitions. Anything absent here is refused.
ALLOWED_TRANSITIONS: dict[DocumentStatus, frozenset[DocumentStatus]] = {
    # Accepted, not yet picked up by a worker.
    DocumentStatus.PENDING: frozenset({DocumentStatus.PROCESSING}),
    # A worker has it. Failure is the other exit.
    DocumentStatus.PROCESSING: frozenset({DocumentStatus.READY, DocumentStatus.FAILED}),
    # Terminal. A re-upload reopens it by starting again at PENDING.
    DocumentStatus.READY: frozenset(),
    # Terminal, but retryable: `retry` explicitly moves it back to PENDING.
    DocumentStatus.FAILED: frozenset({DocumentStatus.PENDING}),
}

TERMINAL_STATUSES = frozenset({DocumentStatus.READY, DocumentStatus.FAILED})


@dataclass(frozen=True, slots=True)
class Document:
    id: UUID
    workspace_id: UUID
    filename: str
    content_type: str
    size_bytes: int
    content_hash: str
    storage_path: str
    uploaded_by: UUID
    status: DocumentStatus
    created_at: datetime
    updated_at: datetime
    chunk_count: int = 0
    processing_error: str | None = None
    attempts: int = 0

    # -- state machine ----------------------------------------------------
    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def can_transition_to(self, target: DocumentStatus) -> bool:
        return target in ALLOWED_TRANSITIONS[self.status]

    def transition_to(self, target: DocumentStatus, *, error: str | None = None) -> Document:
        """Move to `target`, or refuse.

        The guard lives here rather than in the route layer so that the worker in
        Phase 5 and the retry path in Phase 8 are subject to the same rule as
        anything else. `SKILL.md` §31: business rules should not require
        understanding framework internals to verify.

        `error` is only meaningful for FAILED, and is cleared on any successful
        transition so a retry does not inherit a stale failure message.
        """
        if target == self.status:
            return self
        if not self.can_transition_to(target):
            allowed = ", ".join(sorted(str(s) for s in ALLOWED_TRANSITIONS[self.status]))
            raise Conflict(
                f"Cannot move a document from '{self.status}' to '{target}'. "
                f"Allowed from '{self.status}': {allowed or 'nothing, it is terminal'}."
            )
        return replace(
            self,
            status=target,
            updated_at=utcnow(),
            processing_error=error if target is DocumentStatus.FAILED else None,
            attempts=self.attempts + 1 if target is DocumentStatus.PROCESSING else self.attempts,
        )

    def mark_failed(self, error: str) -> Document:
        return self.transition_to(DocumentStatus.FAILED, error=error)

    def requeue(self) -> Document:
        """Return to PENDING for a retry. Clears chunks and the failure message.

        Phase 8's retry path uses this. The caller is responsible for deleting the
        existing chunks, because this object cannot reach the database.
        """
        if self.status is not DocumentStatus.FAILED:
            raise Conflict(f"Only a failed document can be retried; this one is '{self.status}'.")
        return replace(
            self,
            status=DocumentStatus.PENDING,
            updated_at=utcnow(),
            processing_error=None,
            chunk_count=0,
        )

    def requeued_with_new_content(
        self,
        *,
        filename: str,
        content_type: str,
        size_bytes: int,
        storage_path: str,
        content_hash: str,
    ) -> Document:
        """Apply a re-upload of the same content to this document.

        Content is already identical, so the hash is unchanged. Chunks are cleared
        and the document returns to PENDING so it is processed again: a retry must
        not leave chunks from the previous attempt behind, or retrieval would mix
        two versions of the same file.
        """
        if self.status is DocumentStatus.PROCESSING:
            raise Conflict(
                "This document is being processed right now. "
                "Wait for it to finish, then upload again."
            )
        return replace(
            self,
            filename=filename,
            content_type=content_type,
            size_bytes=size_bytes,
            storage_path=storage_path,
            content_hash=content_hash,
            status=DocumentStatus.PENDING,
            updated_at=utcnow(),
            processing_error=None,
            chunk_count=0,
        )

    # -- persistence ------------------------------------------------------
    def to_document(self) -> dict[str, Any]:
        return {
            "_id": self.id,
            "workspace_id": self.workspace_id,
            "filename": self.filename,
            "content_type": self.content_type,
            "size_bytes": self.size_bytes,
            "content_hash": self.content_hash,
            "storage_path": self.storage_path,
            "uploaded_by": self.uploaded_by,
            "status": str(self.status),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "chunk_count": self.chunk_count,
            "processing_error": self.processing_error,
            "attempts": self.attempts,
        }

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> Document:
        return cls(
            id=document["_id"],
            workspace_id=document["workspace_id"],
            filename=document["filename"],
            content_type=document["content_type"],
            size_bytes=document["size_bytes"],
            content_hash=document["content_hash"],
            storage_path=document["storage_path"],
            uploaded_by=document["uploaded_by"],
            status=DocumentStatus(document["status"]),
            created_at=document["created_at"],
            updated_at=document["updated_at"],
            chunk_count=document.get("chunk_count", 0),
            processing_error=document.get("processing_error"),
            attempts=document.get("attempts", 0),
        )


@dataclass(frozen=True, slots=True)
class DocumentPage:
    """One page of a workspace's documents, newest first."""

    items: list[Document] = field(default_factory=list)
    total: int = 0
    limit: int = 20
    offset: int = 0

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total
