"""Per-principal rate limiting for the expensive endpoints.

The reason this exists is quota, not abuse. Gemini's free tier caps requests per
minute and per day; without a limiter a single client in a retry loop can spend a
workspace's whole daily allowance in seconds, and the symptom -- a wall of 503s
hours later -- points at nothing useful. Limiting turns that into an immediate,
attributable 429 with a `Retry-After`.

A sliding window rather than a fixed one. Fixed windows let a caller send the full
quota at 11:59 and again at 12:00, which is twice the intended rate at the boundary
and exactly the burst shape a limiter is supposed to prevent.

**In-process, deliberately.** State lives in this process's memory and resets on
restart. That is sufficient for a single Render free instance, which is what the
deployment targets, and it needs no Redis and no Atlas round trip on the hot path.
The cost is real and stated plainly: with more than one instance the effective
limit is per instance, so N instances allow N times the rate. The roadmap says
Redis only if in-process proves insufficient, and it has not been measured yet.

Requests are counted, not queued, and the window is tracked per key with a bounded
dictionary. Unbounded per-key state is how a limiter becomes a memory leak: keys
come from ids, and an unauthenticated caller should not be able to create millions
of them. Anonymous callers share one key.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from uuid import UUID


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    #: Seconds until the oldest request in the window expires, which is when a
    #: caller gains a slot. Zero when allowed with capacity to spare.
    retry_after: int = 0

    @property
    def headers(self) -> dict[str, str]:
        headers = {"x-ratelimit-limit": str(self.limit)}
        if self.remaining >= 0:
            headers["x-ratelimit-remaining"] = str(self.remaining)
        if not self.allowed:
            headers["retry-after"] = str(self.retry_after)
        return headers


@dataclass
class _Bucket:
    """Timestamps of recent requests, oldest first."""

    window: deque[float] = field(default_factory=deque)

    def prune(self, now: float, window_seconds: float) -> None:
        cutoff = now - window_seconds
        while self.window and self.window[0] <= cutoff:
            self.window.popleft()

    def full(self, limit: int, window_seconds: float, now: float) -> bool:
        self.prune(now, window_seconds)
        return len(self.window) >= limit

    def add(self, now: float) -> None:
        self.window.append(now)

    def seconds_until_slot(self, window_seconds: float, now: float) -> float:
        if not self.window:
            return 0.0
        return max(0.0, (self.window[0] + window_seconds) - now)


class RateLimiter:
    def __init__(
        self,
        *,
        limit: int,
        window_seconds: float,
        clock: Callable[[], float] | None = None,
        max_keys: int = 10_000,
    ) -> None:
        self._limit = limit
        self._window = window_seconds
        self._clock = clock or time.monotonic
        self._max_keys = max_keys
        self._buckets: dict[str, _Bucket] = {}

    @property
    def limit(self) -> int:
        return self._limit

    def check(self, key: str) -> RateLimitDecision:
        now = self._clock()
        bucket = self._buckets.get(key)
        if bucket is None:
            if len(self._buckets) >= self._max_keys:
                # Drop the oldest bucket rather than refuse to track new ones. A
                # full table is a signal to shed state, not to lock out callers.
                self._buckets.pop(next(iter(self._buckets)))
            bucket = self._buckets[key] = _Bucket()
        if bucket.full(self._limit, self._window, now):
            return RateLimitDecision(
                allowed=False,
                limit=self._limit,
                remaining=0,
                retry_after=max(1, int(bucket.seconds_until_slot(self._window, now))),
            )
        bucket.add(now)
        bucket.prune(now, self._window)
        return RateLimitDecision(
            allowed=True,
            limit=self._limit,
            remaining=max(0, self._limit - len(bucket.window)),
        )

    def reset(self, key: str | None = None) -> None:
        if key is None:
            self._buckets.clear()
        else:
            self._buckets.pop(key, None)


def principal_key(
    *,
    user_id: UUID | None,
    workspace_id: UUID | None,
    route: str,
) -> str:
    """Identify who is spending quota.

    User first, then workspace: a limit per user keeps one client in a workspace
    from consuming everyone else's allowance, and a limit per workspace bounds the
    damage when a workspace has many members. Anonymous callers share one key,
    so an unauthenticated flood cannot mint unlimited buckets.
    """
    if user_id is not None:
        return f"u:{user_id}:{route}"
    if workspace_id is not None:
        return f"w:{workspace_id}:{route}"
    return f"anon:{route}"


def client_key(*, client_host: str | None, route: str) -> str:
    """Identify an anonymous caller who has no account or workspace yet.

    `principal_key` is the wrong tool for a sign-in attempt: by definition the
    caller has no user id, so it collapses every anonymous request into one shared
    bucket and one abuser locks out the whole service. Keying on the client address
    keeps each caller separate while still bounding each one.

    The address is whatever ASGI reports, which under `--proxy-headers` is the
    leftmost `X-Forwarded-For` entry, so behind Render's proxy it is the real
    caller. That trust is only as good as the edge: if a client could reach the
    app directly it could present its own header and mint a fresh bucket per
    request, making the limiter decorative. It cannot, because the only ingress
    is the proxy.

    `unknown` is a real bucket rather than a fallback onto a shared key, so an
    absent address cannot become a free pass for everyone else.
    """
    return f"ip:{client_host or 'unknown'}:{route}"
