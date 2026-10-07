"""Phase 06 — semantic search, thresholding and context construction.

The embedding provider here is a stub rather than `NullEmbeddingProvider`,
because these tests assert on *similarity values*. A hash-based provider produces
arbitrary scores, which would let a ranking or threshold regression pass by luck.
The stub returns vectors chosen to sit at known distances from the query, so
every assertion about ordering and the cutoff is deterministic.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from knowledgedock.application.retrieval.search import BuildContext, SemanticSearch
from knowledgedock.domain.retrieval import (
    NoAnswerReason,
    RetrievedChunk,
    SearchOutcome,
    cosine_similarity,
)
from knowledgedock.infrastructure.ai.embedding import EmbeddedText, EmbeddingBatch
from knowledgedock.infrastructure.repositories.chunk_repository import (
    InMemoryChunkRepository,
)
from tests.conftest import PASSWORD, make_actor, workspace_of

# --- geometry fixtures -------------------------------------------------------
# Two-dimensional vectors, deliberately. The math is dimension-agnostic, and 2-D
# keeps "why is this score that value" checkable by hand.
E_X = [1.0, 0.0]
NEAR_X = [0.98, 0.2]  # ~0.98 cosine: a close match.
MID = [0.71, 0.71]  # ~0.71 cosine: between cut and junk.
NEAR_MISS = [0.6, 0.8]  # ~0.60 cosine: should fail a 0.65 threshold.
UNRELATED = [0.0, 1.0]  # 0.0 cosine: orthogonal.
OPPOSED = [-1.0, 0.0]  # -1.0 cosine: genuinely opposite.


class StubEmbeddings:
    """Returns a fixed vector per query string."""

    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self._vectors = vectors
        self.calls: list[tuple[str, str]] = []

    # Properties, not methods: the real providers expose these as properties,
    # and a fake with a different shape would let a wrong call site pass here
    # and fail against Gemini.
    @property
    def dimensions(self) -> int:
        return len(next(iter(self._vectors.values()), [0.0]))

    @property
    def model(self) -> str:
        return "stub-embeddings"

    @property
    def provider_name(self) -> str:
        return "stub"

    async def embed(self, texts: list[str], *, task_type: str) -> EmbeddingBatch:
        self.calls.append((task_type, texts[0]))
        return EmbeddingBatch(
            embeddings=[
                EmbeddedText(text=text, vector=self._vectors[text], reported_tokens=1)
                for text in texts
            ],
            input_tokens=len(texts),
            model=self.model,
            provider=self.provider_name,
        )


def chunk(
    *,
    workspace_id,
    document_id,
    text: str,
    embedding: list[float],
    index: int = 0,
    filename: str = "notes.txt",
) -> dict:
    return {
        "workspace_id": workspace_id,
        "document_id": document_id,
        "chunk_index": index,
        "text": text,
        "embedding": embedding,
        "character_count": len(text),
        "filename": filename,
    }


def use_case(
    chunks: InMemoryChunkRepository,
    *,
    query: str,
    top_k: int = 5,
    min_score: float = 0.65,
    weak_min_score: float = 0.5,
    budget: int = 6000,
    embeddings: StubEmbeddings | None = None,
) -> SemanticSearch:
    return SemanticSearch(
        chunks=chunks,
        embeddings=embeddings or StubEmbeddings({query: E_X}),
        index_name="vector_index",
        task_type="RETRIEVAL_QUERY",
        max_embed_tokens=2048,
        top_k=top_k,
        min_score=min_score,
        weak_min_score=weak_min_score,
        context_max_characters=budget,
    )


@pytest.fixture
def ws_id():
    from uuid import uuid4

    return uuid4()


# --- cosine ------------------------------------------------------------------


class TestCosineSimilarity:
    def test_identical_vectors_score_one(self):
        assert cosine_similarity(E_X, E_X) == pytest.approx(1.0)

    def test_orthogonal_vectors_score_zero(self):
        assert cosine_similarity(E_X, UNRELATED) == pytest.approx(0.0, abs=1e-9)

    def test_opposed_vectors_score_negative_one(self):
        # Not clamped. A threshold that cannot reject opposing text is not a
        # threshold; the floor stays inside [-1, 1] so this is representable.
        assert cosine_similarity(E_X, OPPOSED) == pytest.approx(-1.0)

    def test_unnormalised_vectors_are_scored_by_angle_not_magnitude(self):
        # Both point the same way, so a huge magnitude must not read as a better
        # match. Phase 05 L2-normalises, but Phase 06 must not depend on that.
        assert cosine_similarity([3.0, 0.0], [0.001, 0.0]) == pytest.approx(1.0)

    def test_length_mismatch_scores_zero(self):
        # A dimension change must fail closed, never raise or pad.
        assert cosine_similarity(E_X, [1.0, 0.0, 0.0]) == 0.0

    @pytest.mark.parametrize("vector", [[], [0.0, 0.0]])
    def test_degenerate_vectors_score_zero(self, vector):
        assert cosine_similarity(E_X, vector) == 0.0


# --- context builder ---------------------------------------------------------


def retrieved(text: str, score: float = 0.9, index: int = 0) -> RetrievedChunk:
    from uuid import uuid4

    return RetrievedChunk(
        document_id=uuid4(),
        chunk_index=index,
        text=text,
        filename="notes.txt",
        score=score,
        character_count=len(text),
    )


class TestBuildContext:
    def test_empty_input_yields_empty_context(self):
        context = BuildContext(max_characters=100).build([])
        assert context.is_empty
        assert context.text == ""

    def test_blocks_are_ordered_by_descending_score(self):
        context = BuildContext(max_characters=1000).build(
            [retrieved("weak", 0.7), retrieved("strong", 0.9)]
        )
        assert [b.chunk.text for b in context.blocks] == ["strong", "weak"]
        assert [b.position for b in context.blocks] == [1, 2]

    def test_whitespace_differing_duplicates_are_removed(self):
        # Overlapping chunks from one document repeat text verbatim. Collapsing
        # whitespace before comparing drops that without merging real passages.
        context = BuildContext(max_characters=1000).build(
            [retrieved("alpha beta", 0.9), retrieved("alpha   beta\n", 0.85)]
        )
        assert len(context.blocks) == 1
        assert context.duplicates_removed == 1

    def test_distinct_text_is_not_deduplicated(self):
        context = BuildContext(max_characters=1000).build(
            [retrieved("alpha", 0.9), retrieved("beta", 0.85)]
        )
        assert len(context.blocks) == 2
        assert context.duplicates_removed == 0

    def test_budget_drops_the_weakest_chunks_first(self):
        # Ordering and budget interact deliberately: when the budget truncates,
        # what is lost is the least relevant, so the answer survives.
        context = BuildContext(max_characters=8).build(
            [retrieved("aaaa", 0.95), retrieved("bbbb", 0.75), retrieved("cccc", 0.66)]
        )
        assert [b.chunk.text for b in context.blocks] == ["aaaa"]
        assert context.dropped_chunks == 2

    def test_context_never_exceeds_budget(self):
        context = BuildContext(max_characters=20).build(
            [retrieved("a" * 15, 0.9), retrieved("b" * 15, 0.8)]
        )
        assert context.character_count <= 20
        assert len(context.text) <= 20

    def test_separator_is_charged_to_the_budget(self):
        # Two blocks of 8 chars need 18 characters, not 16. If the "\n\n" were
        # free the assembled prompt would silently overrun the budget.
        context = BuildContext(max_characters=18).build(
            [retrieved("a" * 8, 0.9), retrieved("b" * 8, 0.8)]
        )
        assert len(context.blocks) == 2
        assert context.character_count == 18
        assert (
            BuildContext(max_characters=17)
            .build([retrieved("a" * 8, 0.9), retrieved("b" * 8, 0.8)])
            .dropped_chunks
            == 1
        )

    def test_blank_text_is_dropped_not_counted_as_duplicate(self):
        context = BuildContext(max_characters=1000).build([retrieved("   ", 0.9)])
        assert context.is_empty
        assert context.dropped_chunks == 1
        assert context.duplicates_removed == 0

    def test_truncation_is_reported_so_weak_answers_are_explainable(self):
        context = BuildContext(max_characters=10).build([retrieved("a" * 40, 0.9)])
        assert context.to_dict()["truncated"] is True
        assert context.dropped_characters == 40


# --- semantic search ---------------------------------------------------------


class TestSemanticSearch:
    async def test_returns_chunks_above_the_threshold_in_rank_order(self, ws_id):
        from uuid import uuid4

        repo = InMemoryChunkRepository()
        doc = uuid4()
        repo.chunks[doc] = [
            chunk(workspace_id=ws_id, document_id=doc, text="junk", embedding=UNRELATED),
            chunk(workspace_id=ws_id, document_id=doc, text="hit", embedding=NEAR_X),
            chunk(workspace_id=ws_id, document_id=doc, text="edge", embedding=NEAR_MISS),
        ]
        outcome = await use_case(repo, query="q").execute(ws_id, "q")

        assert [c.text for c in outcome.chunks] == ["hit"]
        assert outcome.no_answer is False
        assert outcome.reason is None
        assert outcome.top_score == pytest.approx(cosine_similarity(E_X, NEAR_X))
        # The near miss lands between the confident bar and the weak floor, so
        # it is reported as weak evidence rather than silently discarded.
        assert [c.text for c in outcome.weak_chunks] == ["edge"]
        assert outcome.below_threshold == 1

    async def test_query_is_embedded_with_the_query_task_type(self, ws_id):
        # Documents embed as RETRIEVAL_DOCUMENT and queries as RETRIEVAL_QUERY.
        # Mixing them quietly degrades every score.
        embeddings = StubEmbeddings({"q": E_X})
        outcome = await use_case(
            InMemoryChunkRepository(), query="q", embeddings=embeddings
        ).execute(ws_id, "q")
        assert embeddings.calls == [("RETRIEVAL_QUERY", "q")]
        assert outcome.embedding_model == "stub-embeddings"
        assert outcome.embedding_provider == "stub"

    async def test_a_near_miss_becomes_weak_evidence_not_a_no_answer(self, ws_id):
        from uuid import uuid4

        repo = InMemoryChunkRepository()
        doc = uuid4()
        repo.chunks[doc] = [
            chunk(workspace_id=ws_id, document_id=doc, text="miss", embedding=NEAR_MISS)
        ]
        outcome = await use_case(repo, query="q").execute(ws_id, "q")

        # The production SKILL.md case: a 0.6 not a 0.65. It stays a no-answer
        # for the read-only search page, but is reported -- and a generation
        # caller may still hand it to the model, which is allowed to decline.
        assert outcome.no_answer is False
        assert outcome.reason is NoAnswerReason.WEAK_EVIDENCE
        assert outcome.chunks == ()
        assert outcome.context.is_empty
        assert [c.text for c in outcome.weak_chunks] == ["miss"]
        # The score is still reported: that is how the threshold gets tuned.
        assert outcome.top_score == pytest.approx(cosine_similarity(E_X, NEAR_MISS))

    async def test_below_the_weak_floor_is_a_hard_no_answer(self, ws_id):
        from uuid import uuid4

        repo = InMemoryChunkRepository()
        doc = uuid4()
        repo.chunks[doc] = [
            chunk(workspace_id=ws_id, document_id=doc, text="junk", embedding=UNRELATED)
        ]
        outcome = await use_case(repo, query="q").execute(ws_id, "q")

        assert outcome.no_answer is True
        assert outcome.reason is NoAnswerReason.BELOW_THRESHOLD
        assert outcome.weak_chunks == ()
        assert outcome.context.is_empty

    async def test_the_weak_band_is_empty_when_the_floor_equals_the_bar(self, ws_id):
        from uuid import uuid4

        # weak_min_score == min_score is the opt-out: single-cutoff behaviour
        # preserved for callers that do not configure a weak band.
        repo = InMemoryChunkRepository()
        doc = uuid4()
        repo.chunks[doc] = [
            chunk(workspace_id=ws_id, document_id=doc, text="miss", embedding=NEAR_MISS)
        ]
        outcome = await use_case(repo, query="q", weak_min_score=0.65).execute(ws_id, "q")
        assert outcome.no_answer is True
        assert outcome.reason is NoAnswerReason.BELOW_THRESHOLD
        assert outcome.weak_chunks == ()

    async def test_empty_index_is_distinguished_from_a_high_threshold(self, ws_id):
        outcome = await use_case(InMemoryChunkRepository(), query="q").execute(ws_id, "q")
        assert outcome.no_answer is True
        assert outcome.reason is NoAnswerReason.NO_MATCHES
        assert outcome.top_score is None
        assert outcome.candidates_returned == 0

    async def test_other_workspaces_are_never_returned(self, ws_id):
        from uuid import uuid4

        other = uuid4()
        repo = InMemoryChunkRepository()
        theirs = uuid4()
        repo.chunks[theirs] = [
            chunk(workspace_id=other, document_id=theirs, text="secret", embedding=E_X)
        ]
        outcome = await use_case(repo, query="q").execute(ws_id, "q")
        assert outcome.chunks == ()
        assert outcome.candidates_returned == 0

    async def test_top_k_caps_the_result_count(self, ws_id):
        from uuid import uuid4

        repo = InMemoryChunkRepository()
        doc = uuid4()
        repo.chunks[doc] = [
            chunk(
                workspace_id=ws_id,
                document_id=doc,
                text=f"c{i}",
                embedding=[0.99 - i * 0.01, 0.14],
                index=i,
            )
            for i in range(8)
        ]
        outcome = await use_case(repo, query="q", top_k=3).execute(ws_id, "q")
        assert len(outcome.chunks) == 3
        assert outcome.limit == 3

    async def test_per_request_limit_overrides_configured_top_k(self, ws_id):
        repo = InMemoryChunkRepository()
        outcome = await use_case(repo, query="q", top_k=5).execute(ws_id, "q", limit=2)
        assert outcome.limit == 2

    async def test_per_request_limit_is_clamped_to_a_sane_range(self, ws_id):
        repo = InMemoryChunkRepository()
        # An unbounded K would ask the index for arbitrary 768-dimension vectors.
        assert (await use_case(repo, query="q").execute(ws_id, "q", limit=0)).limit == 1
        assert (await use_case(repo, query="q").execute(ws_id, "q", limit=9999)).limit == 50

    async def test_fewer_results_than_limit_is_tolerated(self, ws_id):
        # Atlas returned fewer rows than `limit` against a live cluster, so
        # ranking must not assume it filled the request.
        from uuid import uuid4

        repo = InMemoryChunkRepository()
        doc = uuid4()
        repo.chunks[doc] = [
            chunk(workspace_id=ws_id, document_id=doc, text="only", embedding=NEAR_X)
        ]
        outcome = await use_case(repo, query="q", top_k=5).execute(ws_id, "q")
        assert len(outcome.chunks) == 1
        assert outcome.no_answer is False

    async def test_blank_query_short_circuits_without_calling_the_index(self, ws_id):
        embeddings = StubEmbeddings({"q": E_X})
        outcome = await use_case(
            InMemoryChunkRepository(), query="q", embeddings=embeddings
        ).execute(ws_id, "   ")
        assert outcome.no_answer is True
        assert outcome.reason is NoAnswerReason.NO_MATCHES
        assert embeddings.calls == []

    async def test_context_is_budgeted_and_reported(self, ws_id):
        from uuid import uuid4

        repo = InMemoryChunkRepository()
        doc = uuid4()
        repo.chunks[doc] = [
            chunk(workspace_id=ws_id, document_id=doc, text="a" * 50, embedding=NEAR_X),
            chunk(workspace_id=ws_id, document_id=doc, text="b" * 50, embedding=E_X, index=1),
        ]
        outcome = await use_case(repo, query="q", budget=60).execute(ws_id, "q")
        assert len(outcome.context.blocks) == 1
        assert outcome.context.character_count <= 60
        assert outcome.context.dropped_chunks == 1

    async def test_outcome_serialises_for_the_api(self, ws_id):
        outcome = SearchOutcome(
            query="q",
            chunks=(retrieved("hit", 0.9),),
            weak_chunks=(retrieved("edge", 0.6, index=1),),
            no_answer=True,
            reason=NoAnswerReason.BELOW_THRESHOLD,
            top_score=0.9,
            threshold=0.65,
            weak_min_score=0.5,
            limit=5,
            candidates_returned=3,
            below_threshold=2,
            embedding_model="stub-embeddings",
            embedding_provider="stub",
            embedding_dimensions=2,
        )
        payload = outcome.to_dict()
        assert payload["reason"] == "below_threshold"
        assert payload["hits"][0]["score"] == 0.9
        assert payload["weak_hits"][0]["score"] == 0.6
        assert payload["weak_threshold"] == 0.5
        assert payload["embedding"] == {
            "provider": "stub",
            "model": "stub-embeddings",
            "dimensions": 2,
        }


class TestSearchApi:
    def _app(self, settings, chunks, workspaces):
        from knowledgedock.infrastructure.repositories.user_repository import (
            InMemoryUserRepository,
        )
        from tests.conftest import _build_app

        return _build_app(
            settings,
            InMemoryUserRepository(),
            workspaces,
            chunks=chunks,
            embeddings=StubEmbeddings({"anything": E_X, "about keys": E_X}),
        )

    @pytest.fixture
    def env(self, settings, workspaces):
        chunks = InMemoryChunkRepository()
        app = self._app(settings, chunks, workspaces)
        with TestClient(app) as primary:
            yield primary, chunks

    @pytest.fixture
    def owner(self, env):
        return make_actor(env[0], "boss")

    @pytest.fixture
    def ws(self, owner):
        return workspace_of(owner.client, "Acme")

    def test_search_returns_scores_threshold_and_context(self, env, owner, ws):
        from uuid import uuid4

        owner.client.post("/workspaces", json={"name": "Acme"})
        doc = uuid4()
        env[1].chunks[doc] = [
            chunk(
                workspace_id=ws["id"],
                document_id=doc,
                text="rotate keys from the security page",
                embedding=NEAR_X,
            )
        ]
        response = owner.client.post(f"/workspaces/{ws['id']}/search", json={"query": "about keys"})
        assert response.status_code == 200
        body = response.json()
        assert body["no_answer"] is False
        assert body["hits"][0]["text"] == "rotate keys from the security page"
        assert body["top_score"] > body["threshold"]
        assert body["threshold"] == pytest.approx(0.65)
        assert body["embedding"]["model"] == "stub-embeddings"
        assert body["context"]["block_count"] == 1

    def test_no_answer_is_explained_not_silently_empty(self, env, owner, ws):
        owner.client.post("/workspaces", json={"name": "Acme"})
        body = owner.client.post(
            f"/workspaces/{ws['id']}/search", json={"query": "about keys"}
        ).json()
        assert body["no_answer"] is True
        assert body["reason"] == "no_matches"
        assert body["hits"] == []

    def test_outsider_cannot_search_a_workspace(self, env, owner, ws):
        owner.client.post("/workspaces", json={"name": "Acme"})
        stranger = make_actor(env[0], "stranger")
        # Indistinguishable from a workspace that does not exist.
        assert (
            stranger.client.post(f"/workspaces/{ws['id']}/search", json={"query": "x"}).status_code
            == 404
        )

    def test_unauthenticated_search_is_rejected(self, env, owner, ws):
        owner.client.post("/workspaces", json={"name": "Acme"})
        anonymous = env[0].__class__(env[0].app)
        assert (
            anonymous.post(f"/workspaces/{ws['id']}/search", json={"query": "x"}).status_code == 401
        )

    def test_per_request_limit_is_honoured(self, env, owner, ws):
        from uuid import uuid4

        owner.client.post("/workspaces", json={"name": "Acme"})
        doc = uuid4()
        env[1].chunks[doc] = [
            chunk(
                workspace_id=ws["id"],
                document_id=doc,
                text=f"c{i}",
                embedding=[0.99 - i * 0.01, 0.14],
                index=i,
            )
            for i in range(4)
        ]
        body = owner.client.post(
            f"/workspaces/{ws['id']}/search", json={"query": "about keys", "limit": 2}
        ).json()
        assert body["limit"] == 2
        assert len(body["hits"]) == 2

    @pytest.mark.parametrize("limit", [0, 51])
    def test_out_of_range_limits_are_rejected(self, env, owner, ws, limit):
        owner.client.post("/workspaces", json={"name": "Acme"})
        response = owner.client.post(
            f"/workspaces/{ws['id']}/search", json={"query": "about keys", "limit": limit}
        )
        assert response.status_code == 422

    def test_blank_query_is_rejected(self, env, owner, ws):
        owner.client.post("/workspaces", json={"name": "Acme"})
        assert (
            owner.client.post(f"/workspaces/{ws['id']}/search", json={"query": ""}).status_code
            == 422
        )

    def test_registered_credentials_can_reach_the_endpoint(self, env, owner, ws):
        owner.client.post("/workspaces", json={"name": "Acme"})
        fresh = env[0].__class__(env[0].app)
        fresh.post("/auth/register", json={"email": "s@example.com", "password": PASSWORD})
        fresh.post("/auth/login", json={"email": "s@example.com", "password": PASSWORD})
        assert (
            fresh.post(f"/workspaces/{ws['id']}/search", json={"query": "about keys"}).status_code
            == 404
        )
