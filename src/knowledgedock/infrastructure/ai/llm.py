"""Answer generation.

`LLMProvider` is the same seam Phase 05 built for embeddings: business logic
depends on this interface, never on a vendor SDK. The difference is that the
input is no longer a string to embed but a structured, already-partitioned prompt
— the prompt builder decides what is trusted, and this layer only transports it.

Gemini is called over REST with `httpx`, for the same three reasons as the
embedding provider: an explicit timeout on every outbound call, bounded retry
that never retries a 4xx, and token accounting. `generateContent` is a single
request with the whole conversation in `contents`, unlike the embedding endpoint's
one-call-per-item shape, so there is no batching to do.

Two Gemini response details that are easy to get wrong, and are handled in
`_read_text`: the text lives at `candidates[0].content.parts[*].text` and may be
split across several parts, and a candidate can carry `finishReason: "SAFETY"`
with no parts at all. Indexing `parts[0]` directly returns empty text on a
blocked response rather than an error, which would look like a model that
answered nothing rather than one that refused.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from knowledgedock.application.rag.prompt import GroundedPrompt
from knowledgedock.infrastructure.security.errors import (
    ProviderTimeout,
    ProviderUnavailable,
)

logger = logging.getLogger(__name__)

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
GENERATE_PATH = "/models/{model}:generateContent"


@dataclass(frozen=True, slots=True)
class GeneratedAnswer:
    text: str
    model: str
    provider: str
    input_tokens: int = 0
    output_tokens: int = 0
    finish_reason: str = ""


class LLMProvider(Protocol):
    @property
    def model(self) -> str: ...

    @property
    def provider_name(self) -> str: ...

    async def generate_answer(self, prompt: GroundedPrompt) -> GeneratedAnswer: ...


class GeminiChatProvider:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        temperature: float = 0.2,
        max_output_tokens: int = 1024,
        timeout_seconds: float = 60.0,
        max_retries: int = 3,
        backoff_seconds: float = 1.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._temperature = temperature
        self._max_output_tokens = max_output_tokens
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._backoff = backoff_seconds
        self._client = client
        self._owns_client = client is None

    @property
    def model(self) -> str:
        return self._model

    @property
    def provider_name(self) -> str:
        return "gemini"

    async def generate_answer(self, prompt: GroundedPrompt) -> GeneratedAnswer:
        if self._client is None:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                return await self._generate(client, prompt)
        return await self._generate(self._client, prompt)

    async def _generate(self, client: httpx.AsyncClient, prompt: GroundedPrompt) -> GeneratedAnswer:
        url = f"{GEMINI_BASE_URL}{GENERATE_PATH.format(model=self._model)}"
        payload: dict[str, Any] = {
            "model": f"models/{self._model}",
            # Separate top-level field, not the first turn. Keeping instructions
            # out of `contents` is what stops context text being read as rules.
            "systemInstruction": {"parts": [{"text": prompt.system}]},
            "contents": prompt.to_gemini_contents(),
            "generationConfig": {
                "temperature": self._temperature,
                "maxOutputTokens": self._max_output_tokens,
            },
        }
        headers = {"x-goog-api-key": self._api_key, "Content-Type": "application/json"}

        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = await client.post(url, json=payload, headers=headers)
            except httpx.TimeoutException as exc:
                last_error = ProviderTimeout(f"Generation timed out after {self._timeout}s.")
                logger.warning("llm.timeout", extra={"attempt": attempt, "error": str(exc)})
            except httpx.HTTPError as exc:
                last_error = ProviderUnavailable(f"Generation failed: {type(exc).__name__}")
                logger.warning(
                    "llm.network_error",
                    extra={"attempt": attempt, "error": type(exc).__name__},
                )
            else:
                if response.status_code == 200:
                    return self._read_answer(response)
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = ProviderUnavailable(f"Provider returned {response.status_code}.")
                    logger.warning(
                        "llm.retryable_status",
                        extra={"attempt": attempt, "status": response.status_code},
                    )
                else:
                    raise ProviderUnavailable(
                        "Generation provider rejected the request.",
                        detail=f"status={response.status_code} body={response.text[:200]}",
                    )
            if attempt < self._max_retries:
                await asyncio.sleep(self._delay_for(attempt))

        raise last_error or ProviderUnavailable("Generation failed.")

    def _delay_for(self, attempt: int) -> float:
        """Exponential backoff with full jitter, as in the embedding provider."""
        ceiling = min(self._backoff * (2**attempt), 30.0)
        return random.uniform(0, ceiling)

    def _read_answer(self, response: httpx.Response) -> GeneratedAnswer:
        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderUnavailable(
                "Generation provider returned a non-JSON response.",
                detail=response.text[:200],
            ) from exc

        candidates = body.get("candidates") or []
        if not candidates:
            # No candidate at all means the prompt was blocked upstream. Treated
            # as a provider failure rather than an empty answer, because silently
            # returning "" would surface as a successful answer with no content.
            raise ProviderUnavailable(
                "Generation provider returned no candidates.",
                detail=str(body.get("promptFeedback", ""))[:200],
            )

        candidate = candidates[0]
        finish_reason = candidate.get("finishReason", "")
        # Parts are a list and can be empty; the text can be split across several.
        parts = (candidate.get("content") or {}).get("parts") or []
        text = "".join(part.get("text", "") for part in parts).strip()

        if not text:
            raise ProviderUnavailable(
                "Generation provider returned an empty answer.",
                detail=f"finishReason={finish_reason or 'unknown'}",
            )

        usage = body.get("usageMetadata") or {}
        return GeneratedAnswer(
            text=text,
            model=self._model,
            provider=self.provider_name,
            input_tokens=int(usage.get("promptTokenCount", 0) or 0),
            output_tokens=int(usage.get("candidatesTokenCount", 0) or 0),
            finish_reason=finish_reason,
        )


class NullLLMProvider:
    """Deterministic answers for tests and credential-free local runs.

    Reports that it was given the context rather than pretending to reason about
    it. A fake that generated fluent prose would let a test pass while the real
    grounding path was broken, so the stub is deliberately obvious: it echoes the
    first line of the evidence it received, which is also the only way a test can
    assert that the context actually reached the provider.
    """

    def __init__(self, *, model: str = "null-llm", text: str | None = None) -> None:
        self._model = model
        self._text = text
        #: Every prompt this provider has seen, so a test can assert on the exact
        #: assembled request without intercepting HTTP.
        self.prompts: list[GroundedPrompt] = []

    @property
    def model(self) -> str:
        return self._model

    @property
    def provider_name(self) -> str:
        return "null"

    async def generate_answer(self, prompt: GroundedPrompt) -> GeneratedAnswer:
        self.prompts.append(prompt)
        if self._text is not None:
            return GeneratedAnswer(
                text=self._text,
                model=self._model,
                provider=self.provider_name,
                input_tokens=len(prompt.context) // 4,
                output_tokens=len(self._text) // 4,
                finish_reason="STOP",
            )
        return GeneratedAnswer(
            text=self._summarise(prompt),
            model=self._model,
            provider=self.provider_name,
            input_tokens=len(prompt.context) // 4,
            output_tokens=0,
            finish_reason="STOP",
        )

    def _summarise(self, prompt: GroundedPrompt) -> str:
        """Quote the first evidence line, proving the context arrived."""
        body = [
            line
            for line in prompt.context.splitlines()
            if line.strip() and not line.startswith(("[", "<"))
        ]
        if not body:
            return "I could not find anything in this workspace that answers that question."
        return body[0].strip()[:500]
