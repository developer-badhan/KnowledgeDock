"""AI usage records.

Kept in the domain layer because a usage row is a fact about the system, not an
artefact of how it is stored. The recorder in `infrastructure/ai/usage.py` builds
these; nothing else should have to know the record's shape to read one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID


class AIOperation(StrEnum):
    EMBED = "embed"
    GENERATE = "generate"


@dataclass(frozen=True, slots=True)
class AIUsageRecord:
    id: UUID
    operation: AIOperation
    provider: str
    model: str
    success: bool
    duration_ms: float
    created_at: datetime
    input_tokens: int = 0
    output_tokens: int = 0
    workspace_id: UUID | None = None
    user_id: UUID | None = None
    request_id: str | None = None
    #: Provider-reported token counts are authoritative when present. Gemini's
    #: REST surface does not report them for embedContent, so those rows carry an
    #: estimate instead and this records which it was.
    estimated: bool = False
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_document(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "_id": self.id,
            "operation": self.operation.value,
            "provider": self.provider,
            "model": self.model,
            "success": self.success,
            "duration_ms": self.duration_ms,
            "created_at": self.created_at,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "estimated": self.estimated,
        }
        # Nulls omitted rather than stored, so a query for a workspace's usage
        # does not have to filter them out on every read.
        for name in ("workspace_id", "user_id", "request_id", "error"):
            value = getattr(self, name)
            if value is not None:
                document[name] = value
        if self.extra:
            document["extra"] = self.extra
        return document

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> AIUsageRecord:
        return cls(
            id=document["_id"],
            operation=AIOperation(document["operation"]),
            provider=document["provider"],
            model=document["model"],
            success=document["success"],
            duration_ms=document["duration_ms"],
            created_at=document["created_at"],
            input_tokens=document.get("input_tokens", 0),
            output_tokens=document.get("output_tokens", 0),
            workspace_id=document.get("workspace_id"),
            user_id=document.get("user_id"),
            request_id=document.get("request_id"),
            estimated=document.get("estimated", False),
            error=document.get("error"),
            extra=document.get("extra", {}),
        )
