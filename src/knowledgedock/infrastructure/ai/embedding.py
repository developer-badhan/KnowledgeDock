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

Texts are embedded through `batchEmbedContents`, which accepts up to 100 items
per HTTP call. Batching is why a small file is a single round trip instead of
one request per chunk — the dominant cost before was not the embedding, it was
the pacing sleep between requests. `batchEmbedContents` counts every item against
quota, so the pacer sits *inside* the batch loop and spends one token per item,
never one per HTTP call.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from knowledgedock.application.ingestion.chunker import estimate_tokens
from knowledgedock.infrastructure.security.errors import (
    ProviderTimeout,
    ProviderUnavailable,
)

logger = logging.getLogger(__name__)

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
BATCH_EMBED_PATH = "/models/{model}:batchEmbedContents"


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


class _Pacer:
    """Spaces outbound embedding work to a fixed rate, with a burst allowance.

    Gemini's free tier answers a burst of embedding requests with 429, and retry
    backoff cannot rescue that: backing off a rate that is structurally above
    quota just fails later. The rate has to be lowered instead.

    The token is an ITEM (one chunk or one query), not an HTTP call. Quota is
    consumed per embedded text even on `batchEmbedContents`, so charging per call
    would let batching spend quota ~batch_size times faster than configured.

    A token bucket rather than a clock: the bucket starts full, so a document
    whose chunks fit in the burst is embedded immediately — which is every small
    file — while a large one is paced. The same bucket serves query embedding, so
    an interactive question arriving mid-ingestion gets a slot rather than a
    queue behind hundreds of untouched chunks.

    The lock is held only while deciding, never while sleeping, so one paced
    waiter cannot block the others from making progress.
    """

    def __init__(self, items_per_minute: int, burst_items: int) -> None:
        self._interval = 60.0 / items_per_minute
        self._burst = max(1, burst_items)
        self._tokens = float(self._burst)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, count: int = 1) -> None:
        """Spend `count` item tokens, waiting as long as the rate demands."""
        if count < 1:
            return
        while True:
            async with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self._burst, self._tokens + (now - self._updated) / self._interval
                )
                self._updated = now
                if self._tokens >= count:
                    self._tokens -= count
                    return
                wait = (count - self._tokens) * self._interval
            await asyncio.sleep(wait)


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
        batch_size: int = 50,
        items_per_minute: int = 120,
        burst_items: int = 240,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not 1 <= batch_size <= 100:
            raise ValueError("batch_size must be 1-100")
        if items_per_minute < 1:
            raise ValueError("items_per_minute must be at least 1")
        if burst_items < batch_size:
            raise ValueError("burst_items must be at least batch_size")
        self._api_key = api_key
        self._model = model
        self._dimensions = dimensions
        self._batch_size = batch_size
        self._timeout = httpx.Timeout(timeout_seconds)
        self._max_retries = max_retries
        self._backoff = backoff_seconds
        self._limiter = _Pacer(items_per_minute, burst_items)
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
            for start in range(0, len(texts), self._batch_size):
                batch_texts = texts[start : start + self._batch_size]
                # Paced by item, so batching cannot spend quota faster than the
                # configured rate, and ingestion and query share one budget.
                await self._limiter.acquire(len(batch_texts))
                vectors = await self._embed_batch(client, batch_texts, task_type)
                for text, vector in zip(batch_texts, vectors, strict=True):
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

    async def _embed_batch(
        self, client: httpx.AsyncClient, texts: list[str], task_type: str
    ) -> list[list[float]]:
        """Embed `texts` in one `batchEmbedContents` call, with bounded retries.

        The endpoint answers all items in a batch or errors as a batch, so a
        short list is treated as a provider failure rather than silently
        dropping chunks.
        """
        url = f"{GEMINI_BASE_URL}{BATCH_EMBED_PATH.format(model=self._model)}"
        payload = {
            "requests": [
                {
                    "model": f"models/{self._model}",
                    "content": {"parts": [{"text": text}]},
                    "taskType": task_type,
                    "outputDimensionality": self._dimensions,
                }
                for text in texts
            ]
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
                    return self._read_batch(response, len(texts))
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

    def _read_batch(self, response: httpx.Response, expected: int) -> list[list[float]]:
        try:
            embeddings = response.json()["embeddings"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderUnavailable(
                "Embedding provider returned an unexpected response shape.",
                detail=response.text[:200],
            ) from exc
        if not isinstance(embeddings, list) or len(embeddings) != expected:
            raise ProviderUnavailable(
                "Embedding provider returned the wrong number of vectors.",
                detail=response.text[:200],
            )
        return [self._read_values(entry) for entry in embeddings]

    def _read_values(self, entry: Any) -> list[float]:
        try:
            values = entry["values"]
        except (KeyError, TypeError) as exc:
            raise ProviderUnavailable(
                "Embedding provider returned an unexpected response shape."
            ) from exc
        if not isinstance(values, list) or not values:
            raise ProviderUnavailable("Embedding provider returned an empty vector.")
        if len(values) != self._dimensions:
            raise ProviderUnavailable(
                "Embedding dimension mismatch.",
                detail=f"expected {self._dimensions}, got {len(values)}",
            )
        return [float(value) for value in values]


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
