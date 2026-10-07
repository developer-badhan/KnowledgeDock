"""The ingestion pipeline for one document.

```text
PENDING -> PROCESSING -> extract -> normalise -> chunk -> embed -> store -> READY
                        |                                        |
                        +------------> FAILED <-------------------+
```

`SKILL.md` §8 requires the failure branch to persist something diagnosable, so
every path out of here leaves the document in a terminal state with
`processing_error` set to a message safe to show an owner.

Two invariants the ordering exists to protect:

- **Chunks are never written without vectors.** A document is only marked READY
  after its vectors are stored, so `chunk_count` is never a promise the database
  cannot keep.
- **Failure is always terminal.** Any exception marks FAILED rather than leaving
  the document stuck in PROCESSING, which is the state that strands a workspace
  when an instance spins down mid-job.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

from knowledgedock.application.ingestion.chunker import document_chunks, estimate_tokens
from knowledgedock.application.ingestion.extractors import ExtractionError, extractor_for, normalize
from knowledgedock.domain.documents import Document, DocumentStatus
from knowledgedock.infrastructure.ai.embedding import EmbeddingProvider
from knowledgedock.infrastructure.repositories.chunk_repository import ChunkRepository
from knowledgedock.infrastructure.repositories.document_repository import DocumentRepository
from knowledgedock.infrastructure.repositories.document_text_repository import (
    DocumentTextRepository,
)
from knowledgedock.infrastructure.security.errors import AiProviderError
from knowledgedock.infrastructure.storage import FileStorage, StorageError

logger = logging.getLogger(__name__)

#: Chunks longer than this are refused before spending an embedding request.
#: Gemini's embedContent caps input at 2048 tokens; the limit is set below that so
#: the provider never sees an over-long request.
DEFAULT_MAX_EMBED_TOKENS = 1800


@dataclass(frozen=True, slots=True)
class IngestionResult:
    document_id: UUID
    status: DocumentStatus
    chunk_count: int
    input_tokens: int
    provider: str
    model: str
    error: str | None = None


class ProcessDocument:
    def __init__(
        self,
        *,
        documents: DocumentRepository,
        chunks: ChunkRepository,
        storage: FileStorage,
        texts: DocumentTextRepository,
        embeddings: EmbeddingProvider,
        chunk_size: int,
        chunk_overlap: int,
        min_chunk_size: int,
        task_type: str,
        max_embed_tokens: int = DEFAULT_MAX_EMBED_TOKENS,
    ) -> None:
        self._documents = documents
        self._chunks = chunks
        self._storage = storage
        self._texts = texts
        self._embeddings = embeddings
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap
        self._min_chunk_size = min_chunk_size
        self._task_type = task_type
        self._max_embed_tokens = max_embed_tokens

    async def execute(self, document: Document) -> IngestionResult:
        """Process one document. Never raises for an expected failure."""
        # PROCESSING is a valid entry state, not a reason to skip. The worker
        # claims a document by flipping it to PROCESSING atomically before calling
        # this, so a claimed document arrives here already PROCESSING. Refusing
        # that would make the worker skip everything it just claimed.
        #
        # READY and FAILED are the states that mean "already dealt with".
        if document.status not in (DocumentStatus.PENDING, DocumentStatus.PROCESSING):
            logger.info(
                "ingestion.skipped",
                extra={
                    "document_id": str(document.id),
                    "status": str(document.status),
                },
            )
            return IngestionResult(
                document_id=document.id,
                status=document.status,
                chunk_count=document.chunk_count,
                input_tokens=0,
                provider=self._embeddings.provider_name,
                model=self._embeddings.model,
            )

        started = document.transition_to(DocumentStatus.PROCESSING)
        await self._documents.replace_for_reupload(started)

        try:
            return await self._run(started)
        except (ExtractionError, AiProviderError, StorageError) as exc:
            return await self._fail(started, str(exc), detail=exc.__dict__.get("detail"))
        except Exception as exc:
            # Never let one bad document stall the worker. SKILL.md §17.
            logger.exception(
                "ingestion.unexpected_failure",
                extra={"document_id": str(document.id)},
            )
            return await self._fail(
                started, f"Processing failed unexpectedly ({type(exc).__name__})."
            )

    async def _run(self, document: Document) -> IngestionResult:
        raw = await self._read(document)
        text = normalize(raw)
        if not text:
            return await self._fail(document, "No usable text could be extracted.")

        chunks = document_chunks(
            document,
            text,
            chunk_size=self._chunk_size,
            chunk_overlap=self._chunk_overlap,
            min_chunk_size=self._min_chunk_size,
        )
        if not chunks:
            return await self._fail(
                document, "The document is too short to produce any searchable text."
            )

        oversized = [c for c in chunks if estimate_tokens(c["text"]) > self._max_embed_tokens]
        if oversized:
            logger.warning(
                "ingestion.chunk_over_token_limit",
                extra={
                    "document_id": str(document.id),
                    "chunks": len(oversized),
                    "chunk_size": self._chunk_size,
                },
            )
            chunks = [c for c in chunks if estimate_tokens(c["text"]) <= self._max_embed_tokens]
            if not chunks:
                return await self._fail(document, "All chunks exceed the embedding token limit.")

        batch = await self._embeddings.embed(
            [chunk["text"] for chunk in chunks], task_type=self._task_type
        )
        for chunk, embedded in zip(chunks, batch.embeddings, strict=True):
            chunk["embedding"] = embedded.vector

        stored = await self._chunks.replace_for_document(document.id, document.workspace_id, chunks)
        ready = document.transition_to(DocumentStatus.READY)
        ready = _with_chunk_count(ready, stored)
        await self._documents.replace_for_reupload(ready)

        logger.info(
            "ingestion.completed",
            extra={
                "document_id": str(document.id),
                "workspace_id": str(document.workspace_id),
                "chunks": stored,
                "input_tokens": batch.input_tokens,
                "provider": batch.provider,
                "model": batch.model,
            },
        )
        return IngestionResult(
            document_id=document.id,
            status=DocumentStatus.READY,
            chunk_count=stored,
            input_tokens=batch.input_tokens,
            provider=batch.provider,
            model=batch.model,
        )

    async def _read(self, document: Document) -> str:
        """Prefer the text extracted at upload; fall back to the stored file.

        Extraction happens at upload (see UploadDocument) and that text is the
        only durable copy — a deploy can wipe the file storage and leave the row
        behind. Most documents therefore never touch the file here. Old rows are
        still honored until a re-upload re-extracts them.
        """
        text = await self._texts.get(document.id)
        if text is not None:
            return text
        return self._extract_from_file(document)

    def _extract_from_file(self, document: Document) -> str:
        try:
            with self._storage.open(document.storage_path) as handle:
                return extractor_for(document.content_type).extract(handle)
        except StorageError:
            raise
        except ExtractionError as exc:
            raise ExtractionError(
                f"Could not read {document.filename!r} for processing ({exc}). "
                "Re-upload the file to process it."
            ) from exc

    async def _fail(
        self, document: Document, message: str, *, detail: str | None = None
    ) -> IngestionResult:
        failed = document.mark_failed(message)
        failed = _with_chunk_count(failed, 0)
        await self._chunks.replace_for_document(document.id, document.workspace_id, [])
        await self._documents.replace_for_reupload(failed)
        logger.warning(
            "ingestion.failed",
            extra={
                "document_id": str(document.id),
                "workspace_id": str(document.workspace_id),
                "error": message,
                "detail": detail,
            },
        )
        return IngestionResult(
            document_id=document.id,
            status=DocumentStatus.FAILED,
            chunk_count=0,
            input_tokens=0,
            provider=self._embeddings.provider_name,
            model=self._embeddings.model,
            error=message,
        )


def _with_chunk_count(document: Document, count: int) -> Document:
    from dataclasses import replace

    return replace(document, chunk_count=count)
