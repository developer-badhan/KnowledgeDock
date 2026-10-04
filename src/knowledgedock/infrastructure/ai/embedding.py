"""Embedding generation.

`EmbeddingProvider` is the seam `SKILL.md` §11 asks for. Business logic depends
on this interface, never on a vendor SDK, so switching providers is a new class
rather than a refactor.

Gemini is called over its REST API with `httpx` rather than through the
`google-genai` SDK. That is a considered choice, not a preference for hand-rolling:
`SKILL.md` §35 prefers direct provider APIs, and this phase needs explicit control
over three things an SDK would hide — the timeout on every outbound call
(§17), bounded retry with backoff (§17), and token accounting (§18).

Two Gemini specifics that are easy to get wrong:

- **Task types are not interchangeable.** Documents embed with
  `RETRIEVAL_DOCUMENT` and queries with `RETRIEVAL_QUERY`. Using one for both
  measurably degrades retrieval, so the provider takes the task per call.
- **Below 3072 dimensions the response is not normalised.** Only the full-width
  output is pre-normalised by the API. KnowledgeDock uses 768, so vectors are
  L2-normalised here. Atlas' `cosine` metric would normalise anyway, but doing it
  explicitly means the stored value matches what a caller computing cosine
  similarity locally would compute.

Only one embedding is requested per HTTP call. Gemini's REST surface has no batch
endpoint, and the free tier is rate limited, so batching is done by issuing
sequential requests under one logical batch.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
from dataclasses import dataclass
from typing import Protocol

import httpx

from knowledgedock.application.ingestion.chunker import estimate_tokens
from knowledgedock.infrastructure.security.errors import (
    ProviderTimeout,
    ProviderUnavailable,
)

logger = logging.getLogger(__name__)

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
EMBED_PATH = "/models/{model}:embedContent"


@dataclass(frozen=True, slots=True)
class EmbeddedText:
    text: str
    vector: list[float]
    #: Only set when the provider actually reports usage. Gemini's embedContent
    #: does not, so this is usually None and the count is an estimate instead.
    reported_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class EmbeddingBatch:
    embeddings: list[EmbeddedText]
    input_tokens: int
    provider: str
    model: str


class EmbeddingProvider(Protocol):
    @property
    def dimensions(self) -> int: ...

    @property
    def model(self) -> str: ...

    @property
    def provider_name(self) -> str: ...

    async def embed(self, texts: list[str], *, task_type: str) -> EmbeddingBatch: ...


def l2_normalize(vector: list[float]) -> list[float]:
    magnitude = math.sqrt(sum(component * component for component in vector))
    if magnitude == 0.0:  # pragma: no cover - only a zero vector from a provider
        return vector
    return [component / magnitude for component in vector]


class GeminiEmbeddingProvider:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        dimensions: int,
        timeout_seconds: float,
        max_retries: int,
        backoff_seconds: float,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._dimensions = dimensions
        self._timeout = httpx.Timeout(timeout_seconds)
        self._max_retries = max_retries
        self._backoff = backoff_seconds
        self._client = client
        self._owns_client = client is None

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def model(self) -> str:
        return self._model

    @property
    def provider_name(self) -> str:
        return "gemini"

    async def embed(self, texts: list[str], *, task_type: str) -> EmbeddingBatch:
        if not texts:
            return EmbeddingBatch([], 0, self.provider_name, self._model)

        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        try:
            results = []
            for text in texts:
                vector = await self._embed_one(client, text, task_type)
                results.append(
                    EmbeddedText(
                        text=text,
                        # Gemini only pre-normalises its full 3072-dim output.
                        vector=(vector if len(vector) == 3072 else l2_normalize(vector)),
                    )
                )
        finally:
            if self._owns_client:
                await client.aclose()

        return EmbeddingBatch(
            embeddings=results,
            input_tokens=sum(estimate_tokens(r.text) for r in results),
            provider=self.provider_name,
            model=self._model,
        )

    async def _embed_one(self, client: httpx.AsyncClient, text: str, task_type: str) -> list[float]:
        url = f"{GEMINI_BASE_URL}{EMBED_PATH.format(model=self._model)}"
        payload = {
            "model": f"models/{self._model}",
            "content": {"parts": [{"text": text}]},
            "taskType": task_type,
            "outputDimensionality": self._dimensions,
        }
        headers = {"x-goog-api-key": self._api_key, "Content-Type": "application/json"}

        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = await client.post(url, json=payload, headers=headers)
            except httpx.TimeoutException as exc:
                last_error = ProviderTimeout(f"Embedding request timed out after {self._timeout}s.")
                logger.warning(
                    "ai.embedding_timeout", extra={"attempt": attempt, "error": str(exc)}
                )
            except httpx.HTTPError as exc:
                last_error = ProviderUnavailable(f"Embedding request failed: {type(exc).__name__}")
                logger.warning(
                    "ai.embedding_network_error",
                    extra={"attempt": attempt, "error": type(exc).__name__},
                )
            else:
                if response.status_code == 200:
                    return self._read_vector(response)
                # 429 and 5xx are worth another attempt; 4xx will not become valid.
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = ProviderUnavailable(
                        f"Embedding provider returned {response.status_code}."
                    )
                    logger.warning(
                        "ai.embedding_retryable_status",
                        extra={"attempt": attempt, "status": response.status_code},
                    )
                else:
                    raise ProviderUnavailable(
                        "Embedding provider rejected the request.",
                        detail=f"status={response.status_code} body={response.text[:200]}",
                    )
            if attempt < self._max_retries:
                await asyncio.sleep(self._delay_for(attempt))

        raise last_error or ProviderUnavailable("Embedding request failed.")

    def _delay_for(self, attempt: int) -> float:
        """Exponential backoff with full jitter.

        Jitter matters more than the exponent: without it, every document that
        failed together retries together and reproduces the burst that caused the
        failure.
        """
        ceiling = min(self._backoff * (2**attempt), 30.0)
        return random.uniform(0, ceiling)

    def _read_vector(self, response: httpx.Response) -> list[float]:
        try:
            vector = response.json()["embedding"]["values"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderUnavailable(
                "Embedding provider returned an unexpected response shape.",
                detail=response.text[:200],
            ) from exc
        if not isinstance(vector, list) or not vector:
            raise ProviderUnavailable("Embedding provider returned an empty vector.")
        if len(vector) != self._dimensions:
            raise ProviderUnavailable(
                "Embedding dimension mismatch.",
                detail=f"expected {self._dimensions}, got {len(vector)}",
            )
        return [float(value) for value in vector]


class NullEmbeddingProvider:
    """Deterministic vectors for tests and credential-free local runs.

    `AI_PROVIDER=null` in `.env` selects this. Vectors are derived from a hash of
    the text, so the same input always yields the same vector — which is what
    makes a retrieval test meaningful — while requiring no API key and no quota.
    """

    def __init__(self, *, dimensions: int = 768, model: str = "null-embedding") -> None:
        self._dimensions = dimensions
        self._model = model

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def model(self) -> str:
        return self._model

    @property
    def provider_name(self) -> str:
        return "null"

    async def embed(self, texts: list[str], *, task_type: str) -> EmbeddingBatch:
        del task_type
        return EmbeddingBatch(
            embeddings=[EmbeddedText(text=t, vector=self._vector(t)) for t in texts],
            input_tokens=sum(estimate_tokens(t) for t in texts),
            provider=self.provider_name,
            model=self._model,
        )

    def _vector(self, text: str) -> list[float]:
        import hashlib

        vector: list[float] = []
        counter = 0
        while len(vector) < self._dimensions:
            digest = hashlib.sha256(f"{counter}:{text}".encode()).digest()
            for index in range(0, len(digest), 2):
                if len(vector) >= self._dimensions:
                    break
                raw = int.from_bytes(digest[index : index + 2], "big")
                vector.append((raw / 32767.5) - 1.0)
            counter += 1
        return l2_normalize(vector)
