"""Document use cases.

`UploadDocument` is the interesting one. It has to answer three questions in
order, and each has a failure mode worth naming:

1. **Is this file acceptable?** Content type and size are checked before anything
   is written, so a rejected upload leaves nothing on disk.
2. **Have we seen these bytes?** Content is hashed and compared within the
   workspace. A match means *replace in place*: same document id, metadata
   updated, chunks discarded, status back to `PENDING` so it is processed again.
   No duplicate row, no rejection.
3. **Did the write actually happen?** The unique index is the authority on
   identity. Two concurrent uploads of the same bytes race here; the loser
   re-reads the winner and treats its own upload as a replacement too.

Validation lives here rather than in the route so the JSON API and any future
import path cannot disagree about what is uploadable.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from typing import BinaryIO
from uuid import UUID, uuid4

from knowledgedock.application.ingestion.extractors import ExtractionError, extractor_for, normalize
from knowledgedock.domain.documents import (
    Document,
    DocumentPage,
    DocumentStatus,
)
from knowledgedock.domain.errors import NotFound, ValidationFailed
from knowledgedock.domain.users import User, utcnow
from knowledgedock.domain.workspaces import WorkspaceAccess
from knowledgedock.infrastructure.repositories.document_repository import (
    DocumentRepository,
    DuplicateContentError,
)
from knowledgedock.infrastructure.repositories.document_text_repository import (
    DocumentTextRepository,
)
from knowledgedock.infrastructure.storage import FileStorage, hash_stream

logger = logging.getLogger(__name__)

MAX_FILENAME_LENGTH = 255
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


@dataclass(frozen=True, slots=True)
class UploadResult:
    document: Document
    replaced: bool


def validate_content_type(content_type: str | None, allowed: tuple[str, ...]) -> str:
    """Accept only an allowlisted type, ignoring parameters like `; charset=`."""
    if not content_type:
        raise ValidationFailed("The upload has no content type.")
    base = content_type.split(";", 1)[0].strip().lower()
    if base not in allowed:
        raise ValidationFailed(
            f"'{base}' files are not accepted.",
            detail=f"content_type={base!r} allowed={allowed}",
        )
    return base


def validate_size(size_bytes: int, max_bytes: int) -> int:
    if size_bytes <= 0:
        raise ValidationFailed("The uploaded file is empty.")
    if size_bytes > max_bytes:
        limit_mb = max_bytes // (1024 * 1024)
        raise ValidationFailed(
            f"Files must be {limit_mb} MB or smaller.",
            detail=f"size={size_bytes} max={max_bytes}",
        )
    return size_bytes


def sanitize_filename(raw: str | None) -> str:
    """Keep a display name without letting it influence anything else.

    The stored path is derived from the content hash, never from this string, so
    the only job here is to strip control characters and separators so the name
    cannot break a header, a log line or a download filename.
    """
    name = (raw or "").replace("\\", "/").split("/")[-1].strip()
    name = "".join(ch for ch in name if ch.isprintable() and ch not in '<>:"|?*')
    name = name.lstrip(".") or "upload"
    return name[:MAX_FILENAME_LENGTH]


class UploadDocument:
    def __init__(
        self,
        repository: DocumentRepository,
        storage: FileStorage,
        texts: DocumentTextRepository,
        *,
        allowed_content_types: tuple[str, ...],
        max_bytes: int,
    ) -> None:
        self._repository = repository
        self._storage = storage
        self._texts = texts
        self._allowed = allowed_content_types
        self._max_bytes = max_bytes

    async def execute(
        self,
        access: WorkspaceAccess,
        user: User,
        *,
        filename: str | None,
        content_type: str | None,
        source: BinaryIO,
        declared_size: int | None = None,
    ) -> UploadResult:
        """Accept an upload and return the document, created or replaced."""
        resolved_type = validate_content_type(content_type, self._allowed)
        display_name = sanitize_filename(filename)

        if declared_size is not None:
            validate_size(declared_size, self._max_bytes)

        content_hash, size = self._measure(source)
        validate_size(size, self._max_bytes)

        # Text is extracted here, before anything is written, for two reasons.
        # It is the only durable copy: Render's disk is ephemeral, so waiting
        # until the worker runs lets a deploy between upload and processing
        # destroy the file and strand the document. And an unreadable upload
        # fails now, with the reason at hand, instead of silently becoming a
        # FAILED document later.
        text = self._extract(source, resolved_type)

        existing = await self._repository.find_by_content(access.workspace_id, content_hash)
        if existing is not None:
            return await self._replace(
                existing, access, display_name, resolved_type, source, size, text=text
            )

        return await self._create(
            access, user, display_name, resolved_type, source, content_hash, size, text=text
        )

    # -- helpers ----------------------------------------------------------
    def _measure(self, source: BinaryIO) -> tuple[str, int]:
        """Hash and measure, then rewind so the same stream can be stored."""
        content_hash, size = hash_stream(source)
        source.seek(0)
        return content_hash, size

    def _extract(self, source: BinaryIO, content_type: str) -> str:
        """Extract and normalize the document's text, refusing a bad file now."""
        source.seek(0)
        try:
            raw = extractor_for(content_type).extract(source)
        except ExtractionError as exc:
            raise ValidationFailed(str(exc)) from exc
        return normalize(raw)

    async def _create(
        self,
        access: WorkspaceAccess,
        user: User,
        filename: str,
        content_type: str,
        source: BinaryIO,
        content_hash: str,
        size: int,
        text: str,
    ) -> UploadResult:
        document_id = uuid4()
        # Extraction consumed the stream; rewind so the same bytes are stored.
        source.seek(0)
        stored_path, written = self._storage.save(
            access.workspace_id, content_hash, content_type, source
        )
        now = utcnow()
        document = Document(
            id=document_id,
            workspace_id=access.workspace_id,
            filename=filename,
            content_type=content_type,
            size_bytes=written,
            content_hash=content_hash,
            storage_path=stored_path,
            uploaded_by=user.id,
            status=DocumentStatus.PENDING,
            created_at=now,
            updated_at=now,
        )
        try:
            await self._repository.create(document)
        except DuplicateContentError:
            # Lost a race with an identical upload. The winner's row is the truth;
            # treat this upload as a replacement of it rather than an error, so a
            # client that double-submits gets a sane result either way.
            winner = await self._repository.find_by_content(access.workspace_id, content_hash)
            if winner is None:  # pragma: no cover - only if the winner vanished
                raise
            logger.info(
                "documents.upload_race_resolved",
                extra={"document_id": str(winner.id), "workspace_id": str(access.workspace_id)},
            )
            with contextlib.suppress(OSError, ValueError):
                source.seek(0)
            return await self._replace(
                winner,
                access,
                filename,
                content_type,
                source,
                written,
                replace_bytes=written,
                text=text,
            )
        await self._texts.save(document_id, access.workspace_id, text)

        logger.info(
            "documents.uploaded",
            extra={
                "document_id": str(document.id),
                "workspace_id": str(access.workspace_id),
                "content_type": content_type,
                "size_bytes": written,
                "chars": len(text),
            },
        )
        return UploadResult(document=document, replaced=False)

    async def _replace(
        self,
        existing: Document,
        access: WorkspaceAccess,
        filename: str,
        content_type: str,
        source: BinaryIO,
        size: int,
        *,
        replace_bytes: int | None = None,
        text: str,
    ) -> UploadResult:
        """Re-upload of identical content: update in place, never duplicate."""
        updated = existing.requeued_with_new_content(
            filename=filename,
            content_type=content_type,
            size_bytes=replace_bytes if replace_bytes is not None else size,
            # Content is identical, so the content-addressed path is unchanged and
            # needs no rewrite.
            storage_path=existing.storage_path,
            content_hash=existing.content_hash,
        )
        # Chunks belong to the previous processing run. Leaving them would let
        # Phase 6 retrieve text that no longer matches the stored file.
        removed = await self._repository.delete_chunks(existing.id)
        await self._texts.save(existing.id, access.workspace_id, text)
        await self._repository.replace_for_reupload(updated)
        logger.info(
            "documents.reupload_replaced",
            extra={
                "document_id": str(updated.id),
                "workspace_id": str(access.workspace_id),
                "chunks_discarded": removed,
                "previous_status": str(existing.status),
                "chars": len(text),
            },
        )
        return UploadResult(document=updated, replaced=True)


class ListDocuments:
    def __init__(self, repository: DocumentRepository) -> None:
        self._repository = repository

    async def execute(
        self, workspace_id: UUID, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0
    ) -> DocumentPage:
        if limit < 1 or limit > MAX_PAGE_SIZE:
            raise ValidationFailed(f"limit must be between 1 and {MAX_PAGE_SIZE}.")
        if offset < 0:
            raise ValidationFailed("offset cannot be negative.")
        return await self._repository.list_for_workspace(workspace_id, limit=limit, offset=offset)


class GetDocument:
    def __init__(self, repository: DocumentRepository) -> None:
        self._repository = repository

    async def execute(self, workspace_id: UUID, document_id: UUID) -> Document:
        """Load a document scoped to its workspace.

        Membership was already proven by the route's dependency. This query still
        filters on `workspace_id`, so a mismatched pair is simply not found —
        identical to an id that never existed.
        """
        document = await self._repository.find_scoped(workspace_id, document_id)
        if document is None:
            raise NotFound("Document not found.")
        return document


class DeleteDocument:
    def __init__(
        self, repository: DocumentRepository, storage: FileStorage, texts: DocumentTextRepository
    ) -> None:
        self._repository = repository
        self._storage = storage
        self._texts = texts

    async def execute(self, workspace_id: UUID, document_id: UUID) -> Document:
        """Hard delete: document row, its chunks, its text, and the stored file.

        Not a soft delete. Render's filesystem is ephemeral and the extracted text
        lives in MongoDB, so a recoverable tombstone would guard against nothing
        while keeping chunks that Phase 6's vector search would still return for
        a document that no longer exists.
        """
        document = await self._repository.find_scoped(workspace_id, document_id)
        if document is None:
            raise NotFound("Document not found.")

        deleted = await self._repository.delete(workspace_id, document_id)
        if not deleted:  # pragma: no cover - row vanished mid-request
            raise NotFound("Document not found.")

        await self._texts.delete(document_id)
        self._storage.delete(document.storage_path)
        logger.info(
            "documents.deleted",
            extra={
                "document_id": str(document.id),
                "workspace_id": str(workspace_id),
                "content_type": document.content_type,
            },
        )
        return document
