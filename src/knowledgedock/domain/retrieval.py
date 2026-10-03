"""Retrieval results and the similarity arithmetic behind them.

Similarity is computed **here, locally**, rather than read from Atlas. That is a
measured decision, not a preference. Against this cluster `$vectorSearch` returns
no `score` field at all — verified with no projection, and again with an explicit
`$project: {"score": 1}`, which silently produced documents without it. Phase 05
stored the vectors, so the cosine similarity is one dot product away and costs
nothing for the handful of hits a single query returns.

Computing it locally also fixes the *scale*, which is what makes a threshold
meaningful. Atlas documents its cosine score as `(cosineSimilarity + 1) / 2`, so
an orthogonal pair scores 0.5 and a threshold of `0.35` looks strict while
admitting almost everything. Measured against real Gemini embeddings:

    relevant match        +0.7162
    unrelated match       +0.7033
    near miss             +0.5611
    different topic       +0.4985

Those are raw cosine values, where 0.0 means orthogonal and 1.0 means identical,
so the separator between a near miss and a real match sits around 0.65. That is
where `RETRIEVAL_MIN_SCORE` now defaults, and why it moved off 0.35.

Similarity is deliberately *not* clamped to [0, 1]. Genuinely opposite text
produces a negative cosine, and letting that reach the threshold is what makes
the cutoff meaningful. A clamp would map "-0.4" and "+0.1" to the same value.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID


class NoAnswerReason(StrEnum):
    """Why retrieval produced nothing the caller should answer from.

    Distinct from "the workspace has no documents" — that case still reaches the
    index and comes back empty, reported as `NO_MATCHES`. The distinction matters
    to whoever tunes the system: `NO_MATCHES` points at ingestion, `BELOW_THRESHOLD`
    points at the threshold.
    """

    NO_MATCHES = "no_matches"
    BELOW_THRESHOLD = "below_threshold"


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    """One chunk that survived top-K selection and the similarity floor."""

    document_id: UUID
    chunk_index: int
    text: str
    filename: str
    score: float
    character_count: int

    def to_dict(self) -> dict:
        return {
            "document_id": str(self.document_id),
            "chunk_index": self.chunk_index,
            "filename": self.filename,
            "score": round(self.score, 6),
            "character_count": self.character_count,
            "text": self.text,
        }


@dataclass(frozen=True, slots=True)
class ContextBlock:
    """A chunk admitted into the prompt budget."""

    chunk: RetrievedChunk
    position: int


@dataclass(frozen=True, slots=True)
class BuiltContext:
    """The prompt-ready context, plus what the budget discarded.

    `dropped` is reported so a weak answer can be distinguished from a strong one
    that merely had its tail truncated. Both look identical downstream otherwise.
    """

    blocks: tuple[ContextBlock, ...] = ()
    character_count: int = 0
    dropped_chunks: int = 0
    dropped_characters: int = 0
    duplicates_removed: int = 0
    budget: int = 0

    @property
    def is_empty(self) -> bool:
        return not self.blocks

    @property
    def text(self) -> str:
        """Blocks joined in rank order, strongest evidence first."""
        return "\n\n".join(block.chunk.text for block in self.blocks)

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "character_count": self.character_count,
            "budget": self.budget,
            "block_count": len(self.blocks),
            "dropped_chunks": self.dropped_chunks,
            "dropped_characters": self.dropped_characters,
            "duplicates_removed": self.duplicates_removed,
            "truncated": self.dropped_chunks > 0,
        }


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    """Everything a caller needs to judge a retrieval, including why it failed."""

    query: str
    chunks: tuple[RetrievedChunk, ...] = ()
    context: BuiltContext = field(default_factory=BuiltContext)
    no_answer: bool = False
    reason: NoAnswerReason | None = None
    top_score: float | None = None
    threshold: float = 0.0
    limit: int = 0
    candidates_returned: int = 0
    below_threshold: int = 0
    embedding_model: str = ""
    embedding_provider: str = ""
    embedding_dimensions: int = 0

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "hits": [chunk.to_dict() for chunk in self.chunks],
            "no_answer": self.no_answer,
            "reason": self.reason.value if self.reason else None,
            "top_score": round(self.top_score, 6) if self.top_score is not None else None,
            "threshold": self.threshold,
            "limit": self.limit,
            "candidates_returned": self.candidates_returned,
            "below_threshold": self.below_threshold,
            "context": self.context.to_dict(),
            "embedding": {
                "provider": self.embedding_provider,
                "model": self.embedding_model,
                "dimensions": self.embedding_dimensions,
            },
        }


def cosine_similarity(left: list[float], right: list[float]) -> float:
    """Cosine similarity of two vectors, or 0.0 if they cannot be compared.

    Guards the three ways this can go wrong at runtime rather than trusting the
    schema: mismatched lengths (a dimension change mid-life), a zero vector (a
    chunk that never embedded), and NaN from a degenerate norm. Each returns 0.0,
    which fails the threshold — a chunk that cannot be scored must not be able to
    pass as a perfect match.
    """
    if len(left) != len(right) or not left:
        return 0.0
    dot = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for a, b in zip(left, right, strict=True):
        dot += a * b
        left_norm += a * a
        right_norm += b * b
    if left_norm <= 0.0 or right_norm <= 0.0:
        return 0.0
    score = dot / (math.sqrt(left_norm) * math.sqrt(right_norm))
    if math.isnan(score):
        return 0.0
    return max(-1.0, min(1.0, score))
