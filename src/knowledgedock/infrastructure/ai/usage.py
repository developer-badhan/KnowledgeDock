"""Per-request AI usage accounting.

Every outbound AI call is recorded: provider, model, operation, tokens, duration,
and whether it succeeded. The reason is quota, not billing. Gemini's free tier is
rate limited per minute and per day, KnowledgeDock runs on a free tier, and a
silent exhaustion shows up as an unexplained wall of 503s with nothing to point at
why. Without a record of what was spent, that failure has no cause attached.

Recording is a decorator rather than provider internals for three reasons: the
providers stay about HTTP, the same accounting covers every provider including
the stubs, and a call that *fails* is still recorded — which is exactly the
request you want to find. A failure is recorded with `success=False` and the error
class, because a provider timing out on 40% of calls looks nothing like one
timing out on 0.5% until you can count them.

Nothing is recorded for the null providers. Their tokens are arithmetic on string
lengths, and writing those rows would make real quota usage look like noise.

`workspace_id` is nullable on purpose. An embedding call made while ingesting a
document does have one, but a failed upload or a health probe may not, and losing
those rows would hide exactly the failures worth seeing.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from uuid import UUID, uuid4

from knowledgedock.domain.usage import AIOperation, AIUsageRecord
from knowledgedock.domain.users import utcnow

logger = logging.getLogger(__name__)


class UsageRecorder:
    """Writes usage rows without ever becoming a reason a request fails.

    Accounting must not be able to take down the thing it measures. If the insert
    fails -- quota, a network blip, a schema change -- the AI call still returns
    its result and the failure is logged. The alternative is an observability
    feature that takes down the product when the database hiccups.
    """

    def __init__(self, repository: Any) -> None:
        self._repository = repository

    async def record(
        self,
        *,
        operation: AIOperation,
        provider: str,
        model: str,
        duration_ms: float,
        success: bool,
        input_tokens: int = 0,
        output_tokens: int = 0,
        estimated: bool = False,
        workspace_id: UUID | None = None,
        user_id: UUID | None = None,
        request_id: str | None = None,
        error: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        record = AIUsageRecord(
            id=uuid4(),
            operation=operation,
            provider=provider,
            model=model,
            success=success,
            duration_ms=duration_ms,
            created_at=utcnow(),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            workspace_id=workspace_id,
            user_id=user_id,
            request_id=request_id,
            estimated=estimated,
            error=error,
            extra=extra or {},
        )
        try:
            await self._repository.insert(record)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "ai.usage_record_failed",
                extra={"error": type(exc).__name__, "operation": operation.value},
            )


class UsageTrackingProvider:
    """Wraps an embedding provider and records every call.

    Wrapped rather than subclassed: `GeminiEmbeddingProvider` and the stubs have
    no shared base beyond the Protocol, so a decorator is the one thing that can
    wrap all of them without touching them.
    """

    def __init__(
        self,
        inner: Any,
        recorder: UsageRecorder,
        *,
        workspace_id: UUID | None = None,
        user_id: UUID | None = None,
        request_id: str | None = None,
    ) -> None:
        self._inner = inner
        self._recorder = recorder
        self._workspace_id = workspace_id
        self._user_id = user_id
        self._request_id = request_id

    @property
    def dimensions(self) -> int:
        return self._inner.dimensions

    @property
    def model(self) -> str:
        return self._inner.model

    @property
    def provider_name(self) -> str:
        return self._inner.provider_name

    @property
    def inner(self) -> Any:
        return self._inner

    async def embed(self, texts: list[str], *, task_type: str) -> Any:
        started = time.perf_counter()
        try:
            batch = await self._inner.embed(texts, task_type=task_type)
        except Exception as exc:
            await self._recorder.record(
                operation=AIOperation.EMBED,
                provider=self.provider_name,
                model=self.model,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
                success=False,
                workspace_id=self._workspace_id,
                user_id=self._user_id,
                request_id=self._request_id,
                error=type(exc).__name__,
                extra={"count": len(texts), "task_type": task_type},
            )
            raise
        await self._recorder.record(
            operation=AIOperation.EMBED,
            provider=self.provider_name,
            model=self.model,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
            success=True,
            input_tokens=batch.input_tokens,
            # Gemini's embedContent does not report token counts, so the estimate
            # from Phase 05 is what gets recorded and `estimated` says so.
            estimated=True,
            workspace_id=self._workspace_id,
            user_id=self._user_id,
            request_id=self._request_id,
            extra={"count": len(texts), "task_type": task_type},
        )
        return batch


class UsageTrackingLLM:
    """Wraps an LLM provider and records every call."""

    def __init__(
        self,
        inner: Any,
        recorder: UsageRecorder,
        *,
        workspace_id: UUID | None = None,
        user_id: UUID | None = None,
        request_id: str | None = None,
    ) -> None:
        self._inner = inner
        self._recorder = recorder
        self._workspace_id = workspace_id
        self._user_id = user_id
        self._request_id = request_id

    @property
    def model(self) -> str:
        return self._inner.model

    @property
    def provider_name(self) -> str:
        return self._inner.provider_name

    @property
    def inner(self) -> Any:
        return self._inner

    async def generate_answer(self, prompt: Any) -> Any:
        started = time.perf_counter()
        try:
            result = await self._inner.generate_answer(prompt)
        except Exception as exc:
            await self._recorder.record(
                operation=AIOperation.GENERATE,
                provider=self.provider_name,
                model=self.model,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
                success=False,
                workspace_id=self._workspace_id,
                user_id=self._user_id,
                request_id=self._request_id,
                error=type(exc).__name__,
            )
            raise
        await self._recorder.record(
            operation=AIOperation.GENERATE,
            provider=self.provider_name,
            model=self.model,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
            success=True,
            # generateContent does report usage, so these are measured.
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            estimated=False,
            workspace_id=self._workspace_id,
            user_id=self._user_id,
            request_id=self._request_id,
        )
        return result
