"""Chunking.

Turns normalised document text into the units that get embedded, indexed and
retrieved. `SKILL.md` §10 sets the constraint that matters: chunks must be big
enough to carry meaning and small enough to retrieve precisely.

Three decisions in here are deliberate.

**Split on structure first, size second.** Text is divided at blank lines, then
sentences, then hard word boundaries, and only cut mid-word if a single unit is
still over budget. Mid-word cuts produce tokens that match nothing.

**Overlap is real overlap.** Consecutive chunks share `chunk_overlap`
characters, so a sentence that straddles a boundary is retrievable from either
side. Without it, the answer to a question spanning a boundary is split across
two chunks and neither contains it.

**Too small is discarded.** A chunk under `min_chunk_size` is almost always a
heading or a table row fragment. Embedding it costs quota — Gemini's free tier is
rate limited — and pollutes retrieval with matches nobody wants.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from knowledgedock.domain.documents import Document
from knowledgedock.domain.errors import ValidationFailed

# Sentence ends, including the abbreviations that would otherwise split wrongly.
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])|\n{2,}")
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class Chunk:
    index: int
    text: str

    @property
    def character_count(self) -> int:
        return len(self.text)


def split_units(text: str) -> list[str]:
    """Break text into the largest sensible units: paragraphs, then sentences."""
    units: list[str] = []
    for paragraph in text.split("\n\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        units.extend(part.strip() for part in _SENTENCE.split(paragraph) if part.strip())
    return units


def _hard_wrap(text: str, size: int) -> list[str]:
    """Last resort: cut on word boundaries, never mid-word."""
    pieces: list[str] = []
    current: list[str] = []
    current_length = 0
    for word in _WHITESPACE.split(text):
        if not word:
            continue
        addition = len(word) + (1 if current else 0)
        if current and current_length + addition > size:
            pieces.append(" ".join(current))
            current, current_length = [word], len(word)
        else:
            current.append(word)
            current_length += addition
        # A single word longer than the budget still has to go somewhere.
        while len(current) == 1 and current_length > size:
            pieces.append(current[0][:size])
            remainder = current[0][size:]
            current, current_length = [remainder], len(remainder)
    if current:
        pieces.append(" ".join(current))
    return [p for p in pieces if p.strip()]


def build_chunks(
    text: str,
    *,
    chunk_size: int,
    chunk_overlap: int,
    min_chunk_size: int,
) -> list[Chunk]:
    """Split normalised text into overlapping chunks.

    Raises `ValidationFailed` on nonsense configuration rather than looping
    forever, and returns an empty list for text that yields nothing usable — the
    caller decides whether that is a failure.
    """
    if chunk_size <= 0:
        raise ValidationFailed("chunk_size must be greater than zero.")
    if chunk_overlap < 0:
        raise ValidationFailed("chunk_overlap cannot be negative.")
    if chunk_overlap >= chunk_size:
        raise ValidationFailed("chunk_overlap must be smaller than chunk_size.")
    if not text.strip():
        return []

    chunks: list[Chunk] = []
    buffer = ""

    def emit(payload: str) -> None:
        candidate = payload.strip()
        if len(candidate) >= min_chunk_size:
            chunks.append(Chunk(index=len(chunks), text=candidate))

    for unit in split_units(text):
        # A single unit can still exceed the budget, e.g. a long unbroken table.
        for piece in _hard_wrap(unit, chunk_size) if len(unit) > chunk_size else [unit]:
            if len(buffer) + len(piece) + 1 <= chunk_size:
                buffer = f"{buffer} {piece}".strip() if buffer else piece
                continue
            emit(buffer)
            # Carry the tail forward so the boundary sentence survives.
            carry = buffer[-chunk_overlap:] if chunk_overlap else ""
            carry = carry.lstrip()
            if carry and carry in piece:
                piece = piece[len(carry) :].lstrip()
            buffer = f"{carry} {piece}".strip() if carry else piece
        if len(buffer) >= chunk_size:
            emit(buffer)
            carry = buffer[-chunk_overlap:] if chunk_overlap else ""
            buffer = carry.lstrip()

    emit(buffer)
    return [Chunk(index=i, text=c.text) for i, c in enumerate(chunks)]


def document_chunks(
    document: Document,
    text: str,
    *,
    chunk_size: int,
    chunk_overlap: int,
    min_chunk_size: int,
) -> list[dict]:
    """Shape chunks into `document_chunks` documents.

    Carries `workspace_id` on every chunk. That field is not decorative: it is the
    `filter` field of the single Atlas vector index, and Phase 6's `$vectorSearch`
    filters on it. A chunk without it would be unretrievable, and a chunk with the
    wrong one would be retrievable by the wrong tenant.
    """
    built = build_chunks(
        text,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        min_chunk_size=min_chunk_size,
    )
    now = document.updated_at
    return [
        {
            "workspace_id": document.workspace_id,
            "document_id": document.id,
            "chunk_index": chunk.index,
            "text": chunk.text,
            "embedding": None,
            "character_count": chunk.character_count,
            "filename": document.filename,
            "created_at": now,
        }
        for chunk in built
    ]


def estimate_tokens(text: str) -> int:
    """Rough token count for Gemini's tokenizer (ESTIMATE ONLY).

    Gemini does not publish an exact local tokenizer, and calling the API to
    count tokens would defeat the purpose of a local estimate. Four characters per
    token is the standard approximation for English prose. This estimate may
    over- or under-count actual tokens depending on content. It is used only to
    refuse an over-long chunk before spending a request, never for billing.
    """
    return max(1, len(text) // 4)
