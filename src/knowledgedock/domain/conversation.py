"""Conversations, messages, and grounded answers.

A conversation exists so a follow-up question can mean something. "What about the
deadline?" retrieves nothing on its own: the words carry no subject, and every
embedding is near-orthogonal to every document. Keeping the turns is what lets
the retrieval step see the topic.

Messages and conversations are separate collections rather than an embedded array
because a conversation grows without bound and Atlas M0 caps a document at 16 MB.
One conversation that eventually embeds a year's history would stop being
insertable, and the failure would arrive as a write error on an ordinary-looking
request. Appending a turn is also a single small write, so two concurrent queries
in one conversation cannot corrupt each other's history.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from knowledgedock.domain.users import utcnow


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True, slots=True)
class Conversation:
    id: UUID
    workspace_id: UUID
    created_by: UUID
    created_at: datetime
    updated_at: datetime

    def to_document(self) -> dict[str, Any]:
        return {
            "_id": self.id,
            "workspace_id": self.workspace_id,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_document(cls, document: dict[str, Any] | None) -> Conversation | None:
        if document is None:
            return None
        return cls(
            id=document["_id"],
            workspace_id=document["workspace_id"],
            created_by=document["created_by"],
            created_at=document["created_at"],
            updated_at=document["updated_at"],
        )


@dataclass(frozen=True, slots=True)
class Message:
    id: UUID
    conversation_id: UUID
    workspace_id: UUID
    role: MessageRole
    content: str
    created_at: datetime
    #: Assistant turns only: the evidence the answer was built from, so a history
    #: read can show why an answer was given without re-running the search.
    citations: tuple[Citation, ...] = ()
    #: Assistant turns only. Recorded rather than inferred so "I could not find
    #: anything" stays visible in history instead of looking like silence.
    no_answer: bool = False

    def to_document(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "_id": self.id,
            "conversation_id": self.conversation_id,
            "workspace_id": self.workspace_id,
            "role": self.role.value,
            "content": self.content,
            "created_at": self.created_at,
        }
        if self.role is MessageRole.ASSISTANT:
            document["citations"] = [c.to_dict() for c in self.citations]
            document["no_answer"] = self.no_answer
        return document

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> Message:
        return cls(
            id=document["_id"],
            conversation_id=document["conversation_id"],
            workspace_id=document["workspace_id"],
            role=MessageRole(document["role"]),
            content=document["content"],
            created_at=document["created_at"],
            citations=tuple(
                Citation(
                    document_id=c["document_id"],
                    filename=c["filename"],
                    chunk_index=c["chunk_index"],
                    score=c["score"],
                )
                for c in document.get("citations", [])
            ),
            no_answer=document.get("no_answer", False),
        )


@dataclass(frozen=True, slots=True)
class Citation:
    """One chunk the answer was built from.

    `score` is included because a reader deciding whether to trust an answer needs
    to know how strongly the evidence matched. It is not persisted per document --
    it belongs to this particular question, and Phase 06 established that storing
    one would create state that goes stale against the corpus.
    """

    document_id: UUID
    filename: str
    chunk_index: int
    score: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": str(self.document_id),
            "filename": self.filename,
            "chunk_index": self.chunk_index,
            "score": round(self.score, 6),
        }


@dataclass(frozen=True, slots=True)
class Answer:
    """The response to one question."""

    question: str
    answer: str
    no_answer: bool
    conversation_id: UUID | None
    citations: tuple[Citation, ...] = ()
    #: Retrieval diagnostics, carried through so a caller can see *why* an answer
    #: was thin without a second search. Same fields Phase 06 exposes.
    top_score: float | None = None
    threshold: float = 0.0
    context_characters: int = 0
    model: str = ""
    provider: str = ""
    input_tokens: int = 0
    output_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "answer": self.answer,
            "no_answer": self.no_answer,
            "conversation_id": str(self.conversation_id) if self.conversation_id else None,
            "sources": [c.to_dict() for c in self.citations],
            "retrieval": {
                "top_score": round(self.top_score, 6) if self.top_score is not None else None,
                "threshold": self.threshold,
                "context_characters": self.context_characters,
            },
            "model": self.model,
            "provider": self.provider,
            "tokens": {"input": self.input_tokens, "output": self.output_tokens},
        }


NO_ANSWER_TEXT = (
    "I could not find anything in this workspace that answers that question. "
    "Try different wording, or upload a document that covers it."
)


def new_conversation(workspace_id: UUID, created_by: UUID) -> Conversation:
    now = utcnow()
    return Conversation(
        id=uuid4(),
        workspace_id=workspace_id,
        created_by=created_by,
        created_at=now,
        updated_at=now,
    )


def new_message(
    conversation_id: UUID,
    workspace_id: UUID,
    role: MessageRole,
    content: str,
    *,
    citations: tuple[Citation, ...] = (),
    no_answer: bool = False,
) -> Message:
    return Message(
        id=uuid4(),
        conversation_id=conversation_id,
        workspace_id=workspace_id,
        role=role,
        content=content,
        created_at=utcnow(),
        citations=citations,
        no_answer=no_answer,
    )
