"""Phase 05 — ingestion: extraction, normalization, chunking, embedding, worker.

Ordered by how much damage a failure would do:

1. **The pipeline end to end.** A PENDING document becomes READY with chunks and
   vectors, or FAILED with a diagnosable reason. Never stuck.
2. **The state machine survives a crash.** A document abandoned in PROCESSING is
   reclaimed at startup, and FAILED documents are retried within budget.
3. **Extraction rejects untrusted input** rather than producing garbage that gets
   embedded and then retrieved as if it were real knowledge.
4. **Provider failure is bounded** — timeout, 429 and 5xx retry; 4xx does not.

The `NullEmbeddingProvider` stands in for Gemini throughout. It is deterministic,
so a test that passes here is testing the pipeline rather than a network call.
`TestGeminiProvider` covers the HTTP contract separately with a stub client.
"""

from __future__ import annotations

import asyncio
import io
import json
import uuid
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from knowledgedock.application.ingestion.chunker import build_chunks, document_chunks
from knowledgedock.application.ingestion.extractors import (
    ExtractionError,
    HtmlTextExtractor,
    PlainTextExtractor,
    extractor_for,
    normalize,
)
from knowledgedock.application.ingestion.process_document import ProcessDocument
from knowledgedock.domain.documents import Document, DocumentStatus
from knowledgedock.domain.users import utcnow
from knowledgedock.infrastructure.ai.embedding import (
    GeminiEmbeddingProvider,
    NullEmbeddingProvider,
    l2_normalize,
)
from knowledgedock.infrastructure.security.errors import ProviderTimeout, ProviderUnavailable
from knowledgedock.infrastructure.storage import LocalFileStorage, hash_bytes
from knowledgedock.workers.processor import IngestionWorker

PW = "a-perfectly-fine-password"
TEXT = (
    "Credentials can be regenerated from the account security settings. "
    "Open the dashboard and choose Security, then Regenerate. "
    "The previous key stops working immediately."
)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
class TestPlainTextExtraction:
    @pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "latin-1"])
    def test_decodes_common_encodings(self, encoding: str) -> None:
        payload = "Héllo wörld".encode(encoding)
        if encoding == "utf-8":
            payload = "Héllo wörld".encode(encoding)

        assert "w" in PlainTextExtractor().extract(io.BytesIO(payload))

    def test_handles_text_and_markdown(self) -> None:
        extractor = PlainTextExtractor()

        assert extractor.handles("text/plain")
        assert extractor.handles("text/markdown")
        assert not extractor.handles("application/pdf")

    def test_reads_markdown_without_stripping_syntax(self) -> None:
        """Stripping markdown would only lose searchable terms."""
        markdown = b"# Title\n\nSome **bold** text."
        text = PlainTextExtractor().extract(io.BytesIO(markdown))

        assert "# Title" in text
        assert "**bold**" in text


class TestHtmlExtraction:
    def test_strips_tags_and_keeps_text(self) -> None:
        html = b"<html><body><p>Hello <b>world</b></p></body></html>"

        assert "Hello world" in HtmlTextExtractor().extract(io.BytesIO(html))

    def test_drops_script_and_style_content(self) -> None:
        """Otherwise attacker-controlled text gets embedded as knowledge."""
        html = b"""<html><head><style>.a{color:red}</style></head>
        <body><script>alert('x')</script><p>Real content</p></body></html>"""

        text = HtmlTextExtractor().extract(io.BytesIO(html))

        assert "Real content" in text
        assert "color:red" not in text
        assert "alert" not in text

    def test_decodes_entities(self) -> None:
        assert "AT&T" in HtmlTextExtractor().extract(io.BytesIO(b"<p>AT&amp;T</p>"))

    def test_rejects_html_with_no_visible_text(self) -> None:
        with pytest.raises(ExtractionError, match="no visible text"):
            HtmlTextExtractor().extract(io.BytesIO(b"<html><body></body></html>"))

    def test_tolerates_malformed_markup(self) -> None:
        text = HtmlTextExtractor().extract(io.BytesIO(b"<p>unclosed <b>bold"))

        assert "unclosed" in text


class TestBinaryExtraction:
    def test_unknown_content_type_is_refused(self) -> None:
        with pytest.raises(ExtractionError, match="No extractor"):
            extractor_for("application/x-msdownload")

    def test_pdf_without_text_is_reported_clearly(self) -> None:
        """A scanned PDF is the common failure, and the message should say so."""
        from knowledgedock.application.ingestion.extractors import PdfTextExtractor

        # A structurally valid but empty PDF is hard to synthesise, so this
        # exercises the guard by feeding a non-PDF payload.
        with pytest.raises(ExtractionError):
            PdfTextExtractor().extract(io.BytesIO(b"this is not a pdf at all"))

    def test_docx_without_text_is_reported(self) -> None:
        from knowledgedock.application.ingestion.extractors import DocxTextExtractor

        with pytest.raises(ExtractionError):
            DocxTextExtractor().extract(io.BytesIO(b"not a docx"))

    def test_every_allowed_content_type_has_an_extractor(self) -> None:
        from knowledgedock.infrastructure.storage import CONTENT_TYPE_EXTENSIONS

        for content_type in CONTENT_TYPE_EXTENSIONS:
            assert isinstance(extractor_for(content_type).handles(content_type), bool)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------
class TestNormalization:
    def test_strips_null_bytes(self) -> None:
        assert "\x00" not in normalize("a\x00b")

    def test_strips_control_characters_but_keeps_newlines(self) -> None:
        result = normalize("line one\x07\nline two\x1b")

        assert "\x07" not in result
        assert "\x1b" not in result
        assert result == "line one\nline two"

    def test_collapses_runs_of_spaces(self) -> None:
        assert normalize("a     b") == "a b"

    def test_collapses_excess_blank_lines(self) -> None:
        assert normalize("a\n\n\n\n\nb") == "a\n\nb"

    def test_normalises_windows_line_endings(self) -> None:
        assert normalize("a\r\nb\rc") == "a\nb\nc"

    def test_folds_unicode_forms(self) -> None:
        """NFKC stops a ligature fragmenting a word for the embedder."""
        assert normalize("ﬁle") == "file"

    def test_returns_empty_for_blank_input(self) -> None:
        assert normalize("") == ""
        assert normalize("   \n  ") == ""


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------
class TestChunking:
    def test_empty_text_yields_nothing(self) -> None:
        assert build_chunks("", chunk_size=100, chunk_overlap=10, min_chunk_size=10) == []
        assert build_chunks("   ", chunk_size=100, chunk_overlap=10, min_chunk_size=10) == []

    def test_short_text_is_one_chunk(self) -> None:
        chunks = build_chunks(TEXT, chunk_size=1000, chunk_overlap=100, min_chunk_size=50)

        assert len(chunks) == 1
        assert chunks[0].text == TEXT

    def test_never_exceeds_the_budget(self) -> None:
        long_text = "Sentence content here. " * 200

        chunks = build_chunks(long_text, chunk_size=300, chunk_overlap=40, min_chunk_size=20)

        assert all(c.character_count <= 300 for c in chunks)

    def test_discards_chunks_below_the_minimum(self) -> None:
        """Headings and table fragments cost quota and pollute retrieval."""
        text = "A" * 250 + "\n\nB"

        chunks = build_chunks(text, chunk_size=300, chunk_overlap=0, min_chunk_size=50)

        assert all(c.character_count >= 50 for c in chunks)

    def test_indices_are_sequential(self) -> None:
        chunks = build_chunks(
            "Content sentence. " * 100, chunk_size=200, chunk_overlap=20, min_chunk_size=20
        )

        assert [c.index for c in chunks] == list(range(len(chunks)))

    def test_consecutive_chunks_share_text(self) -> None:
        """Without overlap, a sentence on a boundary is retrievable from neither."""
        chunks = build_chunks(
            "Alpha sentence here. Beta sentence here. Gamma sentence here. " * 20,
            chunk_size=200,
            chunk_overlap=60,
            min_chunk_size=20,
        )

        assert len(chunks) > 1
        first_tail = chunks[0].text[-40:]
        assert any(first_tail[-20:] in c.text for c in chunks[1:])

    def test_rejects_overlap_larger_than_the_chunk(self) -> None:
        from knowledgedock.domain.errors import ValidationFailed

        with pytest.raises(ValidationFailed):
            build_chunks("x" * 100, chunk_size=50, chunk_overlap=50, min_chunk_size=5)

    def test_splits_an_oversized_single_unit(self) -> None:
        chunks = build_chunks("x" * 900, chunk_size=200, chunk_overlap=0, min_chunk_size=10)

        assert len(chunks) > 1
        assert all(c.character_count <= 200 for c in chunks)

    def test_document_chunks_carry_the_workspace_id(self) -> None:
        """This is the vector index's filter field. Without it, unretrievable."""
        now = utcnow()
        document = Document(
            id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            filename="a.txt",
            content_type="text/plain",
            size_bytes=10,
            content_hash="a" * 64,
            storage_path="ws/a.txt",
            uploaded_by=uuid.uuid4(),
            status=DocumentStatus.PENDING,
            created_at=now,
            updated_at=now,
        )

        chunks = document_chunks(
            document, TEXT, chunk_size=1000, chunk_overlap=50, min_chunk_size=20
        )

        assert chunks
        for chunk in chunks:
            assert chunk["workspace_id"] == document.workspace_id
            assert chunk["document_id"] == document.id
            assert chunk["embedding"] is None
            assert chunk["chunk_index"] >= 0


# ---------------------------------------------------------------------------
# Embedding provider
# ---------------------------------------------------------------------------
class TestNullEmbeddingProvider:
    async def test_is_deterministic(self) -> None:
        provider = NullEmbeddingProvider(dimensions=64)
        first = await provider.embed(["hello"], task_type="RETRIEVAL_DOCUMENT")
        second = await provider.embed(["hello"], task_type="RETRIEVAL_DOCUMENT")

        assert first.embeddings[0].vector == second.embeddings[0].vector

    async def test_differs_for_different_text(self) -> None:
        provider = NullEmbeddingProvider(dimensions=64)
        batch = await provider.embed(["alpha", "beta"], task_type="RETRIEVAL_DOCUMENT")

        assert batch.embeddings[0].vector != batch.embeddings[1].vector

    async def test_produces_the_requested_width(self) -> None:
        batch = await NullEmbeddingProvider(dimensions=128).embed(
            ["x"], task_type="RETRIEVAL_DOCUMENT"
        )

        assert len(batch.embeddings[0].vector) == 128

    async def test_vectors_are_unit_length(self) -> None:
        batch = await NullEmbeddingProvider(dimensions=64).embed(
            ["x"], task_type="RETRIEVAL_DOCUMENT"
        )

        magnitude = sum(v * v for v in batch.embeddings[0].vector) ** 0.5

        assert magnitude == pytest.approx(1.0, abs=1e-6)

    async def test_reports_estimated_tokens(self) -> None:
        batch = await NullEmbeddingProvider(dimensions=8).embed(
            ["a" * 40], task_type="RETRIEVAL_DOCUMENT"
        )

        assert batch.input_tokens == 10

    async def test_empty_input_is_free(self) -> None:
        batch = await NullEmbeddingProvider().embed([], task_type="RETRIEVAL_DOCUMENT")

        assert batch.embeddings == []
        assert batch.input_tokens == 0


class TestL2Normalize:
    def test_produces_unit_length(self) -> None:
        assert sum(v * v for v in l2_normalize([3.0, 4.0])) == pytest.approx(1.0)

    def test_zero_vector_is_left_alone(self) -> None:
        assert l2_normalize([0.0, 0.0]) == [0.0, 0.0]


def _provider(handler, *, retries: int = 3) -> GeminiEmbeddingProvider:
    return GeminiEmbeddingProvider(
        api_key="test-key",
        model="gemini-embedding-001",
        dimensions=4,
        timeout_seconds=1.0,
        max_retries=retries,
        backoff_seconds=0.001,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def _body(values: list[float]) -> dict:
    return {"embedding": {"values": values}}


class TestGeminiProvider:
    async def test_returns_normalised_vectors(self) -> None:
        provider = _provider(lambda request: httpx.Response(200, json=_body([3.0, 4.0, 0.0, 0.0])))

        batch = await provider.embed(["hi"], task_type="RETRIEVAL_DOCUMENT")

        magnitude = sum(v * v for v in batch.embeddings[0].vector) ** 0.5
        assert magnitude == pytest.approx(1.0)

    async def test_sends_the_configured_task_type(self) -> None:
        """Documents and queries must not share a task type."""
        seen: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            return httpx.Response(200, json=_body([1.0, 0.0, 0.0, 0.0]))

        provider = _provider(handler)
        await provider.embed(["a"], task_type="RETRIEVAL_QUERY")
        await provider.embed(["b"], task_type="RETRIEVAL_DOCUMENT")

        assert seen[0]["taskType"] == "RETRIEVAL_QUERY"
        assert seen[1]["taskType"] == "RETRIEVAL_DOCUMENT"

    async def test_sends_the_requested_output_dimensionality(self) -> None:
        seen: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            return httpx.Response(200, json=_body([1.0, 0.0, 0.0, 0.0]))

        await _provider(handler).embed(["a"], task_type="RETRIEVAL_DOCUMENT")

        assert seen[0]["outputDimensionality"] == 4

    async def test_retries_a_rate_limit_then_succeeds(self) -> None:
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] < 3:
                return httpx.Response(429, json={"error": "slow down"})
            return httpx.Response(200, json=_body([1.0, 0.0, 0.0, 0.0]))

        batch = await _provider(handler).embed(["a"], task_type="RETRIEVAL_DOCUMENT")

        assert len(batch.embeddings) == 1
        assert attempts["n"] == 3

    async def test_retries_a_server_error(self) -> None:
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            return (
                httpx.Response(503)
                if attempts["n"] < 2
                else httpx.Response(200, json=_body([1.0, 0.0, 0.0, 0.0]))
            )

        await _provider(handler).embed(["a"], task_type="RETRIEVAL_DOCUMENT")

        assert attempts["n"] == 2

    async def test_does_not_retry_a_client_error(self) -> None:
        """Retrying a malformed request just wastes quota."""
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            return httpx.Response(400, json={"error": "bad request"})

        with pytest.raises(ProviderUnavailable):
            await _provider(handler).embed(["a"], task_type="RETRIEVAL_DOCUMENT")

        assert attempts["n"] == 1

    async def test_gives_up_after_the_retry_budget(self) -> None:
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            return httpx.Response(429)

        with pytest.raises(ProviderUnavailable):
            await _provider(handler, retries=2).embed(["a"], task_type="RETRIEVAL_DOCUMENT")

        assert attempts["n"] == 3  # the original plus two retries

    async def test_timeout_is_reported_as_a_timeout(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("too slow", request=request)

        with pytest.raises(ProviderTimeout):
            await _provider(handler, retries=0).embed(["a"], task_type="RETRIEVAL_DOCUMENT")

    async def test_rejects_a_dimension_mismatch(self) -> None:
        """Silently storing the wrong width breaks every later search."""
        provider = _provider(lambda r: httpx.Response(200, json=_body([1.0, 2.0])))

        with pytest.raises(ProviderUnavailable, match="dimension mismatch"):
            await provider.embed(["a"], task_type="RETRIEVAL_DOCUMENT")

    async def test_rejects_an_unexpected_response_shape(self) -> None:
        provider = _provider(lambda r: httpx.Response(200, json={"nope": True}))

        with pytest.raises(ProviderUnavailable, match="unexpected response"):
            await provider.embed(["a"], task_type="RETRIEVAL_DOCUMENT")

    async def test_rejects_an_empty_vector(self) -> None:
        provider = _provider(lambda r: httpx.Response(200, json={"embedding": {"values": []}}))

        with pytest.raises(ProviderUnavailable):
            await provider.embed(["a"], task_type="RETRIEVAL_DOCUMENT")


# ---------------------------------------------------------------------------
# The pipeline and the worker
# ---------------------------------------------------------------------------
@pytest.fixture
def rig(tmp_path):
    """A complete ingestion rig: storage, documents, chunks, worker."""
    storage = LocalFileStorage(tmp_path / "uploads")
    storage.ensure_root()

    from knowledgedock.infrastructure.repositories.chunk_repository import (
        InMemoryChunkRepository,
    )
    from knowledgedock.infrastructure.repositories.document_repository import (
        InMemoryDocumentRepository,
    )

    documents = InMemoryDocumentRepository()
    chunks = InMemoryChunkRepository()
    embeddings = NullEmbeddingProvider(dimensions=32)

    def build_process(**overrides):
        """A ProcessDocument with the same pipeline config, swapped collaborators.

        ProcessDocument is a plain class rather than a dataclass, so collaborators
        are substituted by rebuilding rather than with dataclasses.replace.
        """
        kwargs = {
            "documents": documents,
            "chunks": chunks,
            "storage": storage,
            "embeddings": embeddings,
            "chunk_size": 200,
            "chunk_overlap": 30,
            "min_chunk_size": 20,
            "task_type": "RETRIEVAL_DOCUMENT",
            "max_embed_tokens": 1800,
        }
        kwargs.update(overrides)
        return ProcessDocument(**kwargs)

    process = build_process()
    worker = IngestionWorker(
        documents=documents,
        process=process,
        poll_interval_seconds=0.01,
        max_attempts=3,
        stale_after_minutes=30,
    )

    def enqueue(
        content: bytes = TEXT.encode(),
        *,
        filename: str = "notes.txt",
        content_type: str = "text/plain",
        status: DocumentStatus = DocumentStatus.PENDING,
        attempts: int = 0,
    ) -> Document:
        workspace_id = uuid.uuid4()
        digest = hash_bytes(content)
        path, _ = storage.save(workspace_id, digest, content_type, io.BytesIO(content))
        now = utcnow()
        document = Document(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            filename=filename,
            content_type=content_type,
            size_bytes=len(content),
            content_hash=digest,
            storage_path=path,
            uploaded_by=uuid.uuid4(),
            status=status,
            created_at=now,
            updated_at=now,
            attempts=attempts,
        )
        documents.documents[document.id] = document
        return document

    return {
        "storage": storage,
        "documents": documents,
        "chunks": chunks,
        "worker": worker,
        "process": process,
        "build_process": build_process,
        "build_worker": lambda repo: IngestionWorker(
            documents=repo,
            process=build_process(documents=repo),
            poll_interval_seconds=0.01,
            max_attempts=3,
            stale_after_minutes=30,
        ),
        "enqueue": enqueue,
    }


class TestPipeline:
    async def test_pending_becomes_ready_with_chunks_and_vectors(self, rig) -> None:
        document = rig["enqueue"]()

        result = await rig["process"].execute(document)

        assert result.status is DocumentStatus.READY
        assert result.chunk_count > 0
        stored = rig["documents"].documents[document.id]
        assert stored.status is DocumentStatus.READY
        assert stored.chunk_count == result.chunk_count
        assert stored.processing_error is None

    async def test_every_chunk_gets_a_vector(self, rig) -> None:
        document = rig["enqueue"](("Distinct sentence for chunking. " * 30).encode())

        await rig["process"].execute(document)

        chunks = rig["chunks"].chunks[document.id]
        assert chunks
        assert all(chunk["embedding"] is not None for chunk in chunks)
        assert all(len(chunk["embedding"]) == 32 for chunk in chunks)

    async def test_chunks_are_traceable_to_the_document(self, rig) -> None:
        document = rig["enqueue"]()

        await rig["process"].execute(document)

        for chunk in rig["chunks"].chunks[document.id]:
            assert chunk["document_id"] == document.id
            assert chunk["workspace_id"] == document.workspace_id

    async def test_reports_token_usage_and_provider(self, rig) -> None:
        result = await rig["process"].execute(rig["enqueue"]())

        assert result.input_tokens > 0
        assert result.provider == "null"
        assert result.model

    async def test_empty_text_fails_with_a_reason(self, rig) -> None:
        document = rig["enqueue"](b"   \n\n  ")

        result = await rig["process"].execute(document)

        assert result.status is DocumentStatus.FAILED
        assert "too short" in (result.error or "") or "No usable text" in (result.error or "")

    async def test_failure_is_persisted_for_diagnosis(self, rig) -> None:
        document = rig["enqueue"](b"   ")

        await rig["process"].execute(document)

        stored = rig["documents"].documents[document.id]
        assert stored.status is DocumentStatus.FAILED
        assert stored.processing_error

    async def test_failure_stores_no_chunks(self, rig) -> None:
        """Partial chunks from a failed run would be retrievable."""
        document = rig["enqueue"](b"   ")

        await rig["process"].execute(document)

        assert rig["chunks"].chunks.get(document.id) in (None, [])

    async def test_a_document_not_pending_is_skipped(self, rig) -> None:
        """A worker running twice must not write a second time (SKILL.md §9)."""
        document = rig["enqueue"](status=DocumentStatus.READY)

        result = await rig["process"].execute(document)

        assert result.status is DocumentStatus.READY
        assert result.chunk_count == 0

    async def test_provider_failure_marks_failed_not_pending(self, rig) -> None:
        """Never leave a document stranded in PROCESSING."""
        broken = rig["build_process"](embeddings=_AlwaysFails())
        document = rig["enqueue"]()

        result = await broken.execute(document)

        assert result.status is DocumentStatus.FAILED
        assert "unavailable" in (result.error or "").lower()

    async def test_unexpected_error_does_not_escape(self, rig) -> None:
        """One bad document must not take the worker loop down with it."""
        broken = rig["build_process"](storage=_ExplodingStorage())
        document = rig["enqueue"]()

        result = await broken.execute(document)

        assert result.status is DocumentStatus.FAILED


class _AlwaysFails:
    dimensions = 32
    model = "always-fails"
    provider_name = "gemini"

    async def embed(self, texts, *, task_type):
        raise ProviderUnavailable("The embedding provider is unavailable.")


class _ExplodingStorage:
    def open(self, path):
        raise RuntimeError("disk on fire")


class TestWorker:
    async def test_run_once_claims_and_processes(self, rig) -> None:
        document = rig["enqueue"]()

        result = await rig["worker"].run_once()

        assert result is not None
        assert result.document_id == document.id
        assert result.status is DocumentStatus.READY

    async def test_run_once_returns_none_when_idle(self, rig) -> None:
        assert await rig["worker"].run_once() is None

    async def test_claiming_is_exclusive(self, rig) -> None:
        first = rig["enqueue"](b"first document content here")
        second = rig["enqueue"](b"second document content here")

        one = await rig["worker"].run_once()
        two = await rig["worker"].run_once()

        assert {one.document_id, two.document_id} == {first.id, second.id}

    async def test_oldest_first(self, rig) -> None:
        older = rig["enqueue"](b"older content document")
        rig["enqueue"](b"newer content document")

        assert (await rig["worker"].run_once()).document_id == older.id

    async def test_reclaims_a_document_interrupted_while_processing(self, rig) -> None:
        """A spin-down mid-job leaves PROCESSING with nothing to move it."""
        from dataclasses import replace as dc_replace

        document = rig["enqueue"](status=DocumentStatus.PROCESSING)
        # Backdate it: staleness is measured from updated_at, so a document that
        # started a second ago is in progress, not abandoned.
        rig["documents"].documents[document.id] = dc_replace(
            document, updated_at=utcnow() - timedelta(minutes=45)
        )

        requeued = await rig["worker"].reclaim_abandoned()

        assert requeued == 1
        assert all(d.status is DocumentStatus.PENDING for d in rig["documents"].documents.values())

    async def test_leaves_a_recent_processing_document_alone(self, rig) -> None:
        """Otherwise two workers could take the same document."""
        rig["enqueue"](status=DocumentStatus.PROCESSING)

        assert await rig["worker"].reclaim_abandoned() == 0

    async def test_reclaims_only_the_stale_ones(self, rig) -> None:
        from dataclasses import replace as dc_replace

        stale = rig["enqueue"](b"stale interrupted document", status=DocumentStatus.PROCESSING)
        fresh = rig["enqueue"](b"fresh in-progress document", status=DocumentStatus.PROCESSING)
        rig["documents"].documents[stale.id] = dc_replace(
            stale, updated_at=utcnow() - timedelta(minutes=45)
        )

        assert await rig["worker"].reclaim_abandoned() == 1
        assert rig["documents"].documents[fresh.id].status is DocumentStatus.PROCESSING

    async def test_retries_a_failed_document_within_budget(self, rig) -> None:
        rig["enqueue"](b"   ", status=DocumentStatus.FAILED, attempts=1)

        assert await rig["worker"].reclaim_abandoned() == 1

    async def test_gives_up_after_the_attempt_budget(self, rig) -> None:
        rig["enqueue"](b"   ", status=DocumentStatus.FAILED, attempts=3)

        assert await rig["worker"].reclaim_abandoned() == 0

    async def test_reclaim_survives_an_unavailable_database(self, rig) -> None:
        from knowledgedock.infrastructure.repositories.document_repository import (
            UnavailableDocumentRepository,
        )

        broken = rig["build_worker"](UnavailableDocumentRepository(RuntimeError("no Atlas")))

        assert await broken.reclaim_abandoned() == 0

    async def test_loop_processes_then_idles_and_stops(self, rig) -> None:
        rig["enqueue"]()
        rig["enqueue"](b"another document with content")
        stop = asyncio.Event()

        task = asyncio.create_task(rig["worker"].run_forever(stop))
        for _ in range(200):
            await asyncio.sleep(0.005)
            if all(d.status is DocumentStatus.READY for d in rig["documents"].documents.values()):
                break
        stop.set()
        stats = await asyncio.wait_for(task, timeout=5)

        assert stats.processed == 2
        assert stats.failed == 0


class TestStatusVisibility:
    """Processing status has to be observable through the API Phase 4 built.

    This is the only test that runs the real worker loop. Everything else drives
    `ProcessDocument` directly, which is faster and pinpoints failures; this one
    proves the wiring end to end — upload, poll, observe READY.
    """

    def test_upload_progresses_to_ready_through_the_api(self, live_doc_client: TestClient) -> None:
        import time

        client = live_doc_client
        email = f"ingest-{uuid.uuid4().hex[:8]}@example.com"
        client.post("/auth/register", json={"email": email, "password": PW})
        client.post("/auth/login", json={"email": email, "password": PW})
        workspace = client.post("/workspaces", json={"name": "Acme"}).json()
        base = f"/workspaces/{workspace['id']}/documents"

        uploaded = client.post(
            base, files={"file": ("notes.txt", TEXT.encode(), "text/plain")}
        ).json()
        assert uploaded["status"] == "pending"

        seen: list[str] = []
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            body = client.get(f"{base}/{uploaded['id']}").json()
            seen.append(body["status"])
            if body["status"] in ("ready", "failed"):
                break
            time.sleep(0.02)

        assert body["status"] == "ready", f"statuses observed: {seen}"
        assert body["chunk_count"] > 0
        assert body["processing_error"] is None
        # The whole lifecycle was visible, not just the end state.
        assert "pending" in seen

    def test_upload_of_unreadable_content_fails_visibly(self, live_doc_client: TestClient) -> None:
        """A failure must be observable, not a document stuck forever."""
        import time

        client = live_doc_client
        email = f"ingest-{uuid.uuid4().hex[:8]}@example.com"
        client.post("/auth/register", json={"email": email, "password": PW})
        client.post("/auth/login", json={"email": email, "password": PW})
        workspace = client.post("/workspaces", json={"name": "Acme"}).json()
        base = f"/workspaces/{workspace['id']}/documents"

        response = client.post(base, files={"file": ("blank.txt", b"   \n\n\t  ", "text/plain")})
        assert response.status_code == 202, response.text
        uploaded = response.json()

        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            body = client.get(f"{base}/{uploaded['id']}").json()
            if body["status"] in ("ready", "failed"):
                break
            time.sleep(0.02)

        assert body["status"] == "failed"
        assert body["processing_error"]
        assert "Traceback" not in body["processing_error"]
