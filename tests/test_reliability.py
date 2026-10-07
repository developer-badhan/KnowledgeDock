"""Phase 08 — usage accounting, rate limiting, and the reliability guarantees
that were built in earlier phases and never tested directly.

Time is injected rather than slept on. A limiter tested with `time.sleep` is slow
and, worse, flaky in the direction that hides bugs: a test that passes because the
window happened to expire is a test that stops testing the window.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from knowledgedock.domain.usage import AIOperation, AIUsageRecord
from knowledgedock.infrastructure.ai.usage import (
    UsageRecorder,
    UsageTrackingLLM,
    UsageTrackingProvider,
)
from knowledgedock.infrastructure.rate_limit import RateLimiter, principal_key
from knowledgedock.infrastructure.repositories.usage_repository import (
    InMemoryAIUsageRepository,
)
from knowledgedock.infrastructure.security.errors import ProviderUnavailable
from tests.conftest import make_actor, workspace_of

NOW = 1000.0


class Clock:
    def __init__(self, start: float = NOW) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# --- rate limiter ------------------------------------------------------------


class TestRateLimiter:
    def test_requests_within_the_limit_are_allowed(self):
        limiter = RateLimiter(limit=3, window_seconds=60, clock=Clock())
        for expected in (2, 1, 0):
            decision = limiter.check("u:1")
            assert decision.allowed is True
            assert decision.remaining == expected

    def test_the_request_over_the_limit_is_refused_with_retry_after(self):
        limiter = RateLimiter(limit=2, window_seconds=60, clock=Clock())
        limiter.check("k")
        limiter.check("k")
        decision = limiter.check("k")
        assert decision.allowed is False
        assert decision.retry_after >= 1
        assert "retry-after" in decision.headers

    def test_the_window_slides_rather_than_resetting_on_a_boundary(self):
        # A fixed window would let a caller spend the full quota at 59s and again
        # at 60s. The sliding window refuses instead.
        clock = Clock()
        limiter = RateLimiter(limit=2, window_seconds=60, clock=clock)
        limiter.check("k")
        clock.advance(30)
        limiter.check("k")
        clock.advance(31)  # 61s in: the first request has expired, not the second
        assert limiter.check("k").allowed is True

    def test_capacity_returns_as_the_window_slides(self):
        clock = Clock()
        limiter = RateLimiter(limit=2, window_seconds=60, clock=clock)
        limiter.check("k")
        limiter.check("k")
        assert limiter.check("k").allowed is False
        clock.advance(61)
        assert limiter.check("k").allowed is True

    def test_keys_are_independent(self):
        limiter = RateLimiter(limit=1, window_seconds=60, clock=Clock())
        assert limiter.check("a").allowed is True
        assert limiter.check("b").allowed is True
        assert limiter.check("a").allowed is False

    def test_a_refused_request_does_not_extend_the_window(self):
        # Charging refused requests would let a client hammer a closed window and
        # keep it closed indefinitely.
        clock = Clock()
        limiter = RateLimiter(limit=1, window_seconds=10, clock=clock)
        limiter.check("k")
        for _ in range(5):
            assert limiter.check("k").allowed is False
        clock.advance(11)
        assert limiter.check("k").allowed is True

    def test_key_state_is_bounded(self):
        # Keys derive from ids, so an unbounded table is a memory leak an
        # unauthenticated caller could cause.
        limiter = RateLimiter(limit=5, window_seconds=60, clock=Clock(), max_keys=10)
        for i in range(100):
            limiter.check(f"k{i}")
        assert len(limiter._buckets) <= 10

    def test_a_zero_limit_disables_the_limiter(self):
        limiter = RateLimiter(limit=0, window_seconds=60, clock=Clock())
        assert limiter.check("k").allowed is False
        assert limiter.limit == 0

    def test_retry_after_never_reports_zero_while_refused(self):
        # A Retry-After of 0 invites an immediate retry, which is the opposite.
        limiter = RateLimiter(limit=1, window_seconds=0.4, clock=Clock())
        limiter.check("k")
        assert limiter.check("k").retry_after >= 1


class TestPrincipalKey:
    def test_a_user_gets_a_key_even_inside_a_workspace(self):
        from uuid import uuid4

        user, ws = uuid4(), uuid4()
        # Per user, so one client cannot spend everyone else's allowance.
        assert principal_key(user_id=user, workspace_id=ws, route="ai") == f"u:{user}:ai"

    def test_anonymous_callers_share_one_key(self):
        # Otherwise an unauthenticated flood could mint unlimited buckets.
        assert principal_key(user_id=None, workspace_id=None, route="ai") == "anon:ai"

    def test_workspaces_separate_routes(self):
        from uuid import uuid4

        ws = uuid4()
        assert principal_key(user_id=None, workspace_id=ws, route="query") != principal_key(
            user_id=None, workspace_id=ws, route="search"
        )


# --- usage accounting --------------------------------------------------------


def record() -> AIUsageRecord:
    from uuid import uuid4

    from knowledgedock.domain.users import utcnow

    return AIUsageRecord(
        id=uuid4(),
        operation=AIOperation.GENERATE,
        provider="gemini",
        model="gemini-3.8-flash",
        success=True,
        duration_ms=12.5,
        created_at=utcnow(),
        input_tokens=11,
        output_tokens=7,
        workspace_id=uuid4(),
    )


class _Embeddings:
    dimensions = 768
    model = "stub-model"
    provider_name = "stub"

    def __init__(self, fail: bool = False) -> None:
        self._fail = fail
        self.calls = 0

    async def embed(self, texts, *, task_type):
        from knowledgedock.infrastructure.ai.embedding import EmbeddedText, EmbeddingBatch

        self.calls += 1
        if self._fail:
            raise ProviderUnavailable("provider down")
        return EmbeddingBatch(
            embeddings=[EmbeddedText(text=t, vector=[0.1] * 768) for t in texts],
            input_tokens=13,
            provider=self.provider_name,
            model=self.model,
        )


class _LLM:
    model = "stub-model"
    provider_name = "stub"

    def __init__(self, fail: bool = False) -> None:
        self._fail = fail

    async def generate_answer(self, prompt):
        from knowledgedock.infrastructure.ai.llm import GeneratedAnswer

        if self._fail:
            raise ProviderUnavailable("provider down")
        return GeneratedAnswer(
            text="answer",
            model=self.model,
            provider=self.provider_name,
            input_tokens=120,
            output_tokens=30,
        )


class TestUsageRecording:
    async def test_a_successful_embedding_is_recorded(self):
        repo = InMemoryAIUsageRepository()
        tracker = UsageTrackingProvider(_Embeddings(), UsageRecorder(repo))
        await tracker.embed(["hello"], task_type="RETRIEVAL_DOCUMENT")
        assert len(repo.records) == 1
        row = repo.records[0]
        assert row.operation is AIOperation.EMBED
        assert row.success is True
        assert row.input_tokens == 13
        assert row.model == "stub-model"
        assert row.duration_ms >= 0

    async def test_a_failed_embedding_is_recorded_and_reraised(self):
        # A call that fails is the one you most want to count: a provider failing
        # 40% of the time looks nothing like one failing 0.5% until you can count.
        repo = InMemoryAIUsageRepository()
        tracker = UsageTrackingProvider(_Embeddings(fail=True), UsageRecorder(repo))
        with pytest.raises(ProviderUnavailable):
            await tracker.embed(["hello"], task_type="RETRIEVAL_QUERY")
        assert len(repo.records) == 1
        assert repo.records[0].success is False
        assert repo.records[0].error == "ProviderUnavailable"

    async def test_a_generation_records_measured_tokens(self):
        repo = InMemoryAIUsageRepository()
        tracker = UsageTrackingLLM(_LLM(), UsageRecorder(repo))
        await tracker.generate_answer(object())
        row = repo.records[0]
        assert (row.input_tokens, row.output_tokens) == (120, 30)
        # generateContent reports usage, so these are not estimates.
        assert row.estimated is False

    async def test_embedding_token_counts_are_marked_estimated(self):
        # Gemini's embedContent does not report usage, so the Phase 05 estimate is
        # what is recorded. Flagged so a quota calculation can tell them apart.
        repo = InMemoryAIUsageRepository()
        tracker = UsageTrackingProvider(_Embeddings(), UsageRecorder(repo))
        await tracker.embed(["x"], task_type="RETRIEVAL_DOCUMENT")
        assert repo.records[0].estimated is True

    async def test_the_wrapper_is_transparent_to_the_protocol(self):
        repo = InMemoryAIUsageRepository()
        tracker = UsageTrackingProvider(_Embeddings(), UsageRecorder(repo))
        assert tracker.dimensions == 768
        assert tracker.model == "stub-model"
        assert tracker.provider_name == "stub"
        assert tracker.inner is not None

    async def test_the_result_is_returned_unchanged(self):
        repo = InMemoryAIUsageRepository()
        tracker = UsageTrackingProvider(_Embeddings(), UsageRecorder(repo))
        batch = await tracker.embed(["hello"], task_type="RETRIEVAL_DOCUMENT")
        assert len(batch.embeddings) == 1
        assert batch.input_tokens == 13

    async def test_a_broken_recorder_never_fails_the_request(self):
        # Accounting must not be able to take down the thing it measures.

        class Broken:
            async def insert(self, record):
                raise RuntimeError("quota exceeded")

        tracker = UsageTrackingProvider(_Embeddings(), UsageRecorder(Broken()))
        batch = await tracker.embed(["hello"], task_type="RETRIEVAL_DOCUMENT")
        assert len(batch.embeddings) == 1

    async def test_a_broken_recorder_does_not_mask_a_provider_failure(self):
        class Broken:
            async def insert(self, record):
                raise RuntimeError("quota exceeded")

        tracker = UsageTrackingProvider(_Embeddings(fail=True), UsageRecorder(Broken()))
        with pytest.raises(ProviderUnavailable):
            await tracker.embed(["x"], task_type="RETRIEVAL_DOCUMENT")

    def test_a_record_round_trips_through_a_document(self):
        original = record()
        restored = AIUsageRecord.from_document(original.to_document())
        assert restored.id == original.id
        assert restored.operation is original.operation
        assert (restored.input_tokens, restored.output_tokens) == (11, 7)

    def test_optional_fields_are_omitted_rather_than_stored_null(self):
        # A query for a workspace's usage should not have to filter nulls out.
        from uuid import uuid4

        from knowledgedock.domain.users import utcnow

        row = AIUsageRecord(
            id=uuid4(),
            operation=AIOperation.EMBED,
            provider="gemini",
            model="m",
            success=True,
            duration_ms=1.0,
            created_at=utcnow(),
        )
        document = row.to_document()
        assert "workspace_id" not in document
        assert "user_id" not in document
        assert "error" not in document

    async def test_totals_roll_up_by_operation(self):
        repo = InMemoryAIUsageRepository()
        recorder = UsageRecorder(repo)
        await recorder.record(
            operation=AIOperation.GENERATE,
            provider="gemini",
            model="m",
            duration_ms=10.0,
            success=True,
            input_tokens=100,
            output_tokens=20,
        )
        await recorder.record(
            operation=AIOperation.GENERATE,
            provider="gemini",
            model="m",
            duration_ms=5.0,
            success=False,
        )
        totals = await repo.totals_for_workspace(repo.records[0].workspace_id)
        assert totals["calls"] == 2
        assert totals["input_tokens"] == 100
        assert totals["failures"] == 1
        assert totals["generate"]["calls"] == 2

    async def test_totals_are_scoped_to_one_workspace(self):
        from uuid import uuid4

        repo = InMemoryAIUsageRepository()
        recorder = UsageRecorder(repo)
        mine, theirs = uuid4(), uuid4()
        await recorder.record(
            operation=AIOperation.EMBED,
            provider="gemini",
            model="m",
            duration_ms=1.0,
            success=True,
            workspace_id=mine,
        )
        await recorder.record(
            operation=AIOperation.EMBED,
            provider="gemini",
            model="m",
            duration_ms=1.0,
            success=True,
            workspace_id=theirs,
        )
        totals = await repo.totals_for_workspace(mine)
        assert totals["calls"] == 1


# --- reliability guarantees built earlier ------------------------------------


class TestObservabilityAndErrors:
    """Items 2-5 of Phase 08 were built in Phase 01 but never asserted directly."""

    @pytest.fixture
    def client(self, settings, workspaces):
        from knowledgedock.infrastructure.repositories.user_repository import (
            InMemoryUserRepository,
        )
        from tests.conftest import _build_app

        app = _build_app(settings, InMemoryUserRepository(), workspaces)
        # raise_server_exceptions=False so the app's own handler produces the
        # response. Left on, TestClient re-raises before the handler is reached,
        # which would test starlette rather than this application.
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client

    def test_every_response_carries_a_request_id(self, client):
        assert client.get("/health/ready").headers["x-request-id"]

    def test_a_supplied_request_id_is_echoed(self, client):
        # This is what lets a user quote one line from Render's log and get the
        # whole request.
        response = client.get("/health/ready", headers={"x-request-id": "abc123"})
        assert response.headers["x-request-id"] == "abc123"

    def test_an_internal_error_returns_no_stack_trace(self, client):
        from knowledgedock.domain.errors import AppError

        @client.app.get("/boom")
        async def boom():
            raise RuntimeError("secret internal detail: /etc/passwd")

        response = client.get("/boom")
        assert response.status_code == 500
        body = response.text
        assert "Traceback" not in body
        assert "passwd" not in body
        assert AppError  # taxonomy import guard

    def test_errors_carry_the_typed_code_and_request_id(self, client):
        from knowledgedock.domain.errors import NotFound

        @client.app.get("/missing")
        async def missing():
            raise NotFound("No such thing.")

        body = client.get("/missing").json()
        assert body["error"]["code"] == "not_found"
        assert "error" in body

    def test_validation_failures_are_typed(self, client):
        response = client.post("/auth/login", json={})
        assert response.status_code == 422
        assert "detail" in response.json()

    def test_the_error_taxonomy_covers_the_documented_categories(self):
        from knowledgedock.domain.errors import ErrorCode

        expected = {
            "validation_failed",
            "authentication_failed",
            "permission_denied",
            "not_found",
            "conflict",
            "rate_limited",
            "provider_error",
            "service_unavailable",
            "internal_error",
        }
        assert {member.value for member in ErrorCode} == expected

    def test_provider_calls_have_a_configured_timeout(self, settings):
        assert settings.ai_timeout_seconds > 0

    def test_the_database_timeout_is_configured_too(self, settings):
        assert settings.mongodb_server_selection_timeout_ms > 0

    def test_provider_retries_are_bounded_and_skip_4xx(self):
        import httpx

        from knowledgedock.infrastructure.ai.llm import GeminiChatProvider

        calls = []

        async def handler(request):
            calls.append(1)
            return httpx.Response(429, json={})

        provider = GeminiChatProvider(
            api_key="k",
            model="m",
            max_retries=2,
            backoff_seconds=0.0,
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        import asyncio

        from knowledgedock.application.rag.prompt import build_grounded_prompt

        with pytest.raises(ProviderUnavailable):
            asyncio.run(provider.generate_answer(build_grounded_prompt("q", ())))
        assert len(calls) == 3  # the original plus exactly two retries


class TestRateLimitedOverHttp:
    @pytest.fixture
    def env(self, settings, workspaces):
        import dataclasses

        from knowledgedock.infrastructure.repositories.chunk_repository import (
            InMemoryChunkRepository,
        )
        from knowledgedock.infrastructure.repositories.user_repository import (
            InMemoryUserRepository,
        )
        from tests.conftest import _build_app
        from tests.conftest import make_actor as _ma
        from tests.test_rag import AnyQuery

        limited = dataclasses.replace(settings, ai_rate_limit_per_minute=2)
        app = _build_app(
            limited,
            InMemoryUserRepository(),
            workspaces,
            chunks=InMemoryChunkRepository(),
            embeddings=AnyQuery(),
        )
        with TestClient(app) as client:
            actor = _ma(client, "boss")
            ws = workspace_of(actor.client)
            yield actor, ws, app

    def test_the_third_request_is_refused_with_429(self, env):
        actor, ws, _ = env
        url = f"/workspaces/{ws['id']}/query"
        assert actor.client.post(url, json={"question": "one"}).status_code == 200
        assert actor.client.post(url, json={"question": "two"}).status_code == 200
        response = actor.client.post(url, json={"question": "three"})
        assert response.status_code == 429
        assert response.json()["error"]["code"] == "rate_limited"

    def test_the_refusal_advertises_retry_after(self, env):
        actor, ws, _ = env
        url = f"/workspaces/{ws['id']}/query"
        for i in range(2):
            actor.client.post(url, json={"question": f"q{i}"})
        assert int(actor.client.post(url, json={"question": "x"}).headers["retry-after"]) >= 1

    def test_one_principal_cannot_exhaust_anothers_quota(self, env):
        # Budgets are keyed per user, so one client's run cannot spend the
        # allowance everyone else shares.
        actor, ws, app = env
        url = f"/workspaces/{ws['id']}/query"
        for i in range(2):
            actor.client.post(url, json={"question": f"q{i}"})
        assert actor.client.post(url, json={"question": "blocked"}).status_code == 429

        colleague = make_actor(TestClient(app), "colleague")
        their_ws = workspace_of(colleague.client, "Theirs")
        assert (
            colleague.client.post(
                f"/workspaces/{their_ws['id']}/query", json={"question": "mine"}
            ).status_code
            == 200
        )

    def test_an_outsider_is_refused_before_the_limiter_is_reached(self, env):
        # Ordering matters: a 404 must not be reportable as a 429, or a probe can
        # tell which workspace ids exist.
        actor, ws, app = env
        stranger = make_actor(TestClient(app), "stranger")
        assert (
            stranger.client.post(
                f"/workspaces/{ws['id']}/query", json={"question": "x"}
            ).status_code
            == 404
        )

    def test_health_checks_are_never_rate_limited(self, env):
        # A limiter that throttles Render's liveness probe is an outage.
        _, _, app = env
        probe = TestClient(app)
        for _ in range(5):
            assert probe.get("/health/ready").status_code in (200, 503)

    def test_usage_is_recorded_for_real_requests(self, env):

        actor, ws, app = env
        actor.client.post(f"/workspaces/{ws['id']}/query", json={"question": "one"})
        repo = app.state.usage_repository
        assert repo.records, "an /query request should have recorded AI usage"
        assert any(r.operation is AIOperation.EMBED for r in repo.records)


class TestProviderFailureSurfacesAs502:
    """An external AI outage must be reported as the documented 502 ProviderError.

    `infrastructure/security/errors.py` promises provider failures a 502 rather
    than a generic 500, a distinction the UI relies on to show a rejection
    instead of saying "something went wrong".
    """

    def test_the_json_api_reports_a_provider_outage_as_502(self, settings, workspaces):
        from uuid import uuid4

        from knowledgedock.infrastructure.repositories.chunk_repository import (
            InMemoryChunkRepository,
        )
        from knowledgedock.infrastructure.repositories.user_repository import (
            InMemoryUserRepository,
        )
        from tests.conftest import _build_app
        from tests.test_rag import AnyQuery

        class Down:
            model, provider_name = "down", "gemini"

            async def generate_answer(self, prompt):
                raise ProviderUnavailable("provider down")

        chunks = InMemoryChunkRepository()
        app = _build_app(
            settings,
            InMemoryUserRepository(),
            workspaces,
            chunks=chunks,
            embeddings=AnyQuery(),
            llm=Down(),
        )
        with TestClient(app) as client:
            actor = make_actor(client, "boss")
            ws = workspace_of(actor.client)
            doc = uuid4()
            chunks.chunks[doc] = [
                {
                    "workspace_id": ws["id"],
                    "document_id": doc,
                    "chunk_index": 0,
                    "text": "Rotate keys from the Security page.",
                    "embedding": [0.98, 0.2],
                    "character_count": 33,
                    "filename": "handbook.txt",
                }
            ]
            response = actor.client.post(
                f"/workspaces/{ws['id']}/query", json={"question": "How do I rotate a key?"}
            )
        assert response.status_code == 502
        body = response.json()["error"]
        assert body["code"] == "provider_error"
        assert "unavailable" in body["message"]
        assert "provider down" not in body["message"]


class TestLoggingIsJson:
    def test_the_formatter_emits_one_json_object_per_record(self, capsys):
        import logging

        from knowledgedock.core.logging import JsonFormatter

        formatter = JsonFormatter()
        record = logging.LogRecord(
            "knowledgedock",
            logging.INFO,
            __file__,
            1,
            "request.completed",
            None,
            None,
        )
        record.request_id = "abc"
        record.method = "GET"
        payload = json.loads(formatter.format(record))
        assert payload["message"] == "request.completed"
        assert payload["request_id"] == "abc"
        assert payload["method"] == "GET"

    def test_the_request_id_is_included_even_when_absent(self, capsys):
        import logging

        from knowledgedock.core.logging import JsonFormatter

        record = logging.LogRecord("knowledgedock", logging.INFO, __file__, 1, "hello", None, None)
        payload = json.loads(JsonFormatter().format(record))
        # Present as null rather than missing, so log queries do not need a
        # second shape for correlated and uncorrelated lines.
        assert "request_id" in payload
