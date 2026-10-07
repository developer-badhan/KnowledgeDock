"""Semantic search and prompt-context construction.

The shape of this use case is: embed the query, ask the index for neighbours,
score them locally, cut at the threshold, then spend a character budget on what
survives. Each step is a separate, individually testable transformation — and
that separation is the point, because "why did the assistant answer badly" has
four different answers (bad query embedding, bad top-K, wrong threshold, or a
budget that truncated the evidence), and `POST /search` needs to distinguish them.

The workspace filter is pushed into `$vectorSearch` rather than applied to the
results. See `vector_index.py` for why: a post-filter takes the global top-k and
then discards other tenants' rows, which returns fewer than k for a small
workspace and leaks the existence of documents outside it.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from uuid import UUID

from knowledgedock.domain.retrieval import (
    BuiltContext,
    ContextBlock,
    NoAnswerReason,
    RetrievedChunk,
    SearchOutcome,
    cosine_similarity,
)
from knowledgedock.infrastructure.ai.embedding import EmbeddingProvider

logger = logging.getLogger(__name__)

#: Collapsing whitespace before de-duplicating. Overlapping chunks from the same
#: document share long identical runs, and an identical string under any amount of
#: whitespace carries no extra information — so this drops redundancy without ever
#: merging two genuinely different passages.
_WHITESPACE = re.compile(r"\s+")


class BuildContext:
    """Turn ranked chunks into prompt-ready context under a character budget.

    Ordering is by descending score, strongest evidence first, and that ordering
    is applied *here* rather than assumed from the caller. The budget guarantee
    depends on it: when the budget truncates, the chunks lost are the weakest
    ones, so a truncated context degrades gracefully instead of losing the answer.
    Sorting defensively means that holds even if a future caller hands over
    unsorted input.
    """

    def __init__(self, *, max_characters: int) -> None:
        self._max_characters = max_characters

    def build(self, chunks: list[RetrievedChunk]) -> BuiltContext:
        if not chunks:
            return BuiltContext(budget=self._max_characters)

        blocks: list[ContextBlock] = []
        seen: set[str] = set()
        duplicates = 0
        used = 0
        dropped = 0
        dropped_characters = 0

        for chunk in sorted(chunks, key=lambda item: item.score, reverse=True):
            key = _WHITESPACE.sub(" ", chunk.text).strip().lower()
            if not key:
                dropped += 1
                dropped_characters += chunk.character_count
                continue
            if key in seen:
                duplicates += 1
                continue
            # Two newlines between blocks: one block, one separator. Counted
            # against the budget so the assembled text cannot exceed it.
            separator = 2 if blocks else 0
            cost = len(chunk.text) + separator
            if used + cost > self._max_characters:
                dropped += 1
                dropped_characters += chunk.character_count
                continue
            seen.add(key)
            blocks.append(ContextBlock(chunk=chunk, position=len(blocks) + 1))
            used += cost

        return BuiltContext(
            blocks=tuple(blocks),
            character_count=used,
            dropped_chunks=dropped,
            dropped_characters=dropped_characters,
            duplicates_removed=duplicates,
            budget=self._max_characters,
        )


class SemanticSearch:
    """Query embedding -> vector search -> threshold -> context.

    Two cutoffs, deliberately. `min_score` is the confident bar: chunks at or
    above it become the grounded context and the question is answered. Chunks in
    `[weak_min_score, min_score)` are *weak evidence* — below the bar, but still
    worth a model's judgment rather than a number's. They are returned in
    `weak_chunks` with `no_answer` left False so a caller can choose whether to
    generate. Only chunks below the weak floor are dropped without a hearing;
    that is what `below_threshold` counts.

    `weak_min_score` defaults to `min_score`, which leaves the weak band empty
    and restores the old single-cutoff behaviour for callers that do not opt in.

    `num_candidates` is Atlas's approximation budget and is deliberately larger
    than `limit`: it controls how many neighbours the index examines before
    ranking. Too small and a workspace's real matches can be crowded out by other
    rows the filter discards, which silently lowers recall.
    """

    def __init__(
        self,
        *,
        chunks: Any,
        embeddings: EmbeddingProvider,
        index_name: str,
        task_type: str,
        max_embed_tokens: int,
        top_k: int,
        min_score: float,
        weak_min_score: float | None = None,
        context_max_characters: int,
    ) -> None:
        self._chunks = chunks
        self._embeddings = embeddings
        self._index_name = index_name
        self._task_type = task_type
        self._max_embed_tokens = max_embed_tokens
        self._top_k = top_k
        self._min_score = min_score
        self._weak_min_score = min_score if weak_min_score is None else weak_min_score
        self._context_budget = context_max_characters
        self._context = BuildContext(max_characters=context_max_characters)

    @property
    def min_score(self) -> float:
        return self._min_score

    @property
    def weak_min_score(self) -> float:
        return self._weak_min_score

    async def execute(
        self, workspace_id: UUID, query: str, *, limit: int | None = None
    ) -> SearchOutcome:
        limit = self._top_k if limit is None else max(1, min(limit, 50))
        query = query.strip()
        if not query:
            return SearchOutcome(
                query=query,
                no_answer=True,
                reason=NoAnswerReason.NO_MATCHES,
                threshold=self._min_score,
                weak_min_score=self._weak_min_score,
                limit=limit,
                embedding_model=self._embeddings.model,
                embedding_provider=self._embeddings.provider_name,
                embedding_dimensions=self._embeddings.dimensions,
            )

        batch = await self._embeddings.embed([query], task_type=self._task_type)
        vector = batch.embeddings[0].vector

        rows = await self._chunks.vector_search(
            workspace_id=workspace_id,
            query_vector=vector,
            index_name=self._index_name,
            limit=limit,
            num_candidates=min(max(limit * 10, 100), 10_000),
        )

        scored: list[RetrievedChunk] = []
        for row in rows:
            embedding = row.get("embedding") or []
            score = cosine_similarity(vector, list(embedding))
            scored.append(
                RetrievedChunk(
                    document_id=row["document_id"],
                    chunk_index=row.get("chunk_index", 0),
                    text=row.get("text", ""),
                    filename=row.get("filename", ""),
                    score=score,
                    character_count=row.get("character_count") or len(row.get("text", "")),
                )
            )

        # Atlas returns an approximation of the true ordering, and in testing it
        # returned fewer rows than `limit` asked for. Re-sorting and re-cutting
        # locally means the threshold and top-K hold regardless.
        scored.sort(key=lambda item: item.score, reverse=True)
        kept = scored[:limit]
        passing = [chunk for chunk in kept if chunk.score >= self._min_score]
        weak = [chunk for chunk in kept if self._weak_min_score <= chunk.score < self._min_score]

        if not rows:
            reason = NoAnswerReason.NO_MATCHES
        elif passing:
            reason = None
        elif weak:
            # Sub-threshold but still worth a model's judgement: reported as weak
            # evidence, not a no-answer, because the caller may still generate.
            reason = NoAnswerReason.WEAK_EVIDENCE
        else:
            reason = NoAnswerReason.BELOW_THRESHOLD

        context = self._context.build(passing)
        if not passing:
            # Nothing confident, so the budget is not the limiting factor.
            context = BuiltContext(budget=self._context_budget)

        return SearchOutcome(
            query=query,
            chunks=tuple(passing),
            weak_chunks=tuple(weak),
            context=context,
            no_answer=reason in (NoAnswerReason.NO_MATCHES, NoAnswerReason.BELOW_THRESHOLD),
            reason=reason,
            top_score=kept[0].score if kept else None,
            threshold=self._min_score,
            weak_min_score=self._weak_min_score,
            limit=limit,
            candidates_returned=len(rows),
            below_threshold=len(kept) - len(passing) - len(weak),
            embedding_model=self._embeddings.model,
            embedding_provider=self._embeddings.provider_name,
            embedding_dimensions=self._embeddings.dimensions,
        )

    @property
    def context_budget(self) -> int:
        return self._context_budget
