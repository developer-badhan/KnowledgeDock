"""Phase 1 — health probes and configuration loading.

Focus is business behaviour: does the app answer correctly, does readiness
reflect reality, and does configuration fail loudly and legibly when it is wrong.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from knowledgedock.core.config import (
    MissingConfigurationError,
    Settings,
    load_settings,
)


class _EmptyResult:
    deleted_count = 0
    matched_count = 0
    upserted_id = None


class _EmptyCursor:
    """A cursor that yields nothing, for the dead collections below."""

    async def to_list(self, length: object = None) -> list:
        return []


class TestLiveness:
    def test_health_is_200_and_needs_no_database(self, client: TestClient) -> None:
        response = client.get("/health")

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_health_still_answers_when_database_is_down(self, degraded_client: TestClient) -> None:
        response = degraded_client.get("/health")

        assert response.status_code == 200

    def test_every_response_carries_a_request_id(self, client: TestClient) -> None:
        response = client.get("/health")

        assert response.headers["x-request-id"]

    def test_supplied_request_id_is_echoed(self, client: TestClient) -> None:
        response = client.get("/health", headers={"x-request-id": "abc123"})

        assert response.headers["x-request-id"] == "abc123"


class TestReadiness:
    def test_ready_when_database_answers(self, client: TestClient) -> None:
        response = client.get("/health/ready")

        assert response.status_code == 200
        assert response.json() == {"status": "ready", "database": "up"}

    def test_503_when_database_is_unreachable(self, degraded_client: TestClient) -> None:
        response = degraded_client.get("/health/ready")

        assert response.status_code == 503
        assert response.json() == {"status": "degraded", "database": "down"}


class TestProbeAliases:
    """Hosts configure their own probe path; a 404 there blocks the deploy.

    Render polls /healthz by convention. Both spellings must answer identically.
    """

    @pytest.mark.parametrize("path", ["/health", "/healthz"])
    def test_liveness_aliases_match(self, client: TestClient, path: str) -> None:
        assert client.get(path).status_code == 200
        assert client.get(path).json() == {"status": "ok"}

    @pytest.mark.parametrize("path", ["/health/ready", "/readyz"])
    def test_readiness_aliases_match(self, client: TestClient, path: str) -> None:
        assert client.get(path).status_code == 200
        assert client.get(path).json() == {"status": "ready", "database": "up"}

    @pytest.mark.parametrize("path", ["/health/ready", "/readyz"])
    def test_readiness_aliases_report_503_when_database_is_down(
        self, degraded_client: TestClient, path: str
    ) -> None:
        assert degraded_client.get(path).status_code == 503

    def test_aliases_do_not_pollute_the_schema(self, client: TestClient) -> None:
        # Aliases exist for infrastructure probes, not for API consumers.
        paths = client.get("/api/openapi.json").json()["paths"]

        assert "/healthz" not in paths
        assert "/readyz" not in paths


class TestTemplates:
    def test_index_renders_through_jinja(self, client: TestClient) -> None:
        response = client.get("/")

        assert response.status_code == 200
        assert "KnowledgeDock" in response.text

    def test_static_assets_are_served(self, client: TestClient) -> None:
        assert client.get("/static/app.css").status_code == 200

    def test_bootstrap_and_htmx_come_from_cdn(self, client: TestClient) -> None:
        html = client.get("/").text

        assert "cdn.jsdelivr.net/npm/bootstrap" in html
        assert "cdn.jsdelivr.net/npm/htmx.org" in html

    def test_bootstrap_javascript_bundle_is_not_loaded(self, client: TestClient) -> None:
        # Only the Bootstrap stylesheet is needed; no Bootstrap JS component is
        # used, so the Popper-carrying bundle would be dead weight.
        assert "bootstrap.bundle.min.js" not in client.get("/").text


class TestSettingsLoading:
    def test_reads_values_from_the_env_file(self, settings: Settings) -> None:
        assert settings.environment == "test"
        assert settings.mongodb_db == "knowledgedock_test"
        assert settings.ai_provider == "gemini"

    def test_optional_values_fall_back_to_code_defaults(self, settings: Settings) -> None:
        # None of these appear in the test .env, so each must come from the
        # code default rather than from configuration.
        assert settings.port == 8000
        assert settings.jwt_expire_minutes == 60
        assert settings.chunk_size == 1000
        assert settings.chunk_overlap == 200
        assert settings.retrieval_top_k == 5
        assert settings.retrieval_min_score == pytest.approx(0.65)
        assert settings.context_max_chars == 6000
        assert settings.gemini_embedding_dimensions == 768
        assert settings.gemini_chat_model == "gemini-3.8-flash"
        assert settings.rate_limit_per_minute == 60
        assert settings.processing_max_attempts == 3

    def test_comma_separated_content_types_become_a_tuple(self, load_from) -> None:
        loaded = load_from("ALLOWED_CONTENT_TYPES=text/plain, text/markdown ,application/pdf\n")

        assert loaded.allowed_content_types == ("text/plain", "text/markdown", "application/pdf")

    def test_every_allowlisted_type_has_an_extractor(self, settings: Settings) -> None:
        """An accepted type with no extractor would fail later, mid-pipeline."""
        from knowledgedock.application.ingestion.extractors import extractor_for

        for content_type in settings.allowed_content_types:
            assert extractor_for(content_type) is not None

    def test_env_file_overrides_code_defaults(self, load_from) -> None:
        loaded = load_from("CHUNK_SIZE=2500\nRETRIEVAL_TOP_K=12\nPORT=9000\n")

        assert loaded.chunk_size == 2500
        assert loaded.retrieval_top_k == 12
        assert loaded.port == 9000

    def test_integer_and_float_casts(self, load_from) -> None:
        loaded = load_from("AI_MAX_RETRIES=5\nRETRIEVAL_MIN_SCORE=0.62\nGEMINI_TEMPERATURE=0.7\n")

        assert loaded.ai_max_retries == 5
        assert loaded.retrieval_min_score == pytest.approx(0.62)
        assert loaded.gemini_temperature == pytest.approx(0.7)

    def test_missing_secret_key_names_the_variable(self, load_from) -> None:
        with pytest.raises(MissingConfigurationError, match="SECRET_KEY"):
            load_from("SECRET_KEY=\n")

    def test_missing_mongodb_uri_names_the_variable(self, load_from) -> None:
        with pytest.raises(MissingConfigurationError, match="MONGODB_URI"):
            load_from("MONGODB_URI=\n")

    def test_missing_gemini_api_key_names_the_variable(self, load_from) -> None:
        with pytest.raises(MissingConfigurationError, match="GEMINI_API_KEY"):
            load_from("GEMINI_API_KEY=\n")

    def test_missing_embedding_task_names_the_variable(self, load_from) -> None:
        with pytest.raises(MissingConfigurationError, match="GEMINI_EMBEDDING_TASK_QUERY"):
            load_from("GEMINI_EMBEDDING_TASK_QUERY=\n")

    def test_absent_variable_is_reported_not_defaulted(self, tmp_path) -> None:
        path = tmp_path / ".env"
        path.write_text("ENVIRONMENT=test\n", encoding="utf-8")

        from decouple import Config, RepositoryEnv

        with pytest.raises(MissingConfigurationError, match="SECRET_KEY"):
            load_settings(Config(RepositoryEnv(str(path))))


class TestSettingsValidation:
    def test_short_secret_key_is_rejected(self, load_from) -> None:
        with pytest.raises(ValueError, match="SECRET_KEY"):
            load_from("SECRET_KEY=too-short\n")

    def test_non_mongodb_uri_is_rejected(self, load_from) -> None:
        with pytest.raises(ValueError, match="MONGODB_URI"):
            load_from("MONGODB_URI=postgres://localhost/db\n")

    def test_unknown_ai_provider_is_rejected(self, load_from) -> None:
        with pytest.raises(ValueError, match="AI_PROVIDER"):
            load_from("AI_PROVIDER=anthropic\n")

    def test_unknown_embedding_task_is_rejected(self, load_from) -> None:
        with pytest.raises(ValueError, match="GEMINI_EMBEDDING_TASK_DOCUMENT"):
            load_from("GEMINI_EMBEDDING_TASK_DOCUMENT=MAKE_IT_UP\n")

    def test_unknown_vector_metric_is_rejected(self, load_from) -> None:
        with pytest.raises(ValueError, match="VECTOR_SIMILARITY"):
            load_from("VECTOR_SIMILARITY=hamming\n")

    def test_overlap_larger_than_chunk_size_is_rejected(self, load_from) -> None:
        with pytest.raises(ValueError, match="CHUNK_OVERLAP"):
            load_from("CHUNK_SIZE=500\nCHUNK_OVERLAP=800\n")

    def test_embedding_dimensions_respect_the_gemini_ceiling(self, load_from) -> None:
        with pytest.raises(ValueError, match="3072"):
            load_from("GEMINI_EMBEDDING_DIMENSIONS=4096\n")

    def test_embedding_input_tokens_stay_under_the_gemini_limit(self, load_from) -> None:
        with pytest.raises(ValueError, match="2048"):
            load_from("GEMINI_EMBEDDING_MAX_INPUT_TOKENS=4096\n")

    def test_zero_embedding_rate_is_rejected(self, load_from) -> None:
        # Zero would divide by zero in the pacer and hang ingestion forever,
        # which presents as a silent stall rather than a startup error.
        with pytest.raises(ValueError, match="AI_EMBEDDING_ITEMS_PER_MINUTE"):
            load_from("AI_EMBEDDING_ITEMS_PER_MINUTE=0\n")

    def test_zero_embedding_burst_is_rejected(self, load_from) -> None:
        with pytest.raises(ValueError, match="AI_EMBEDDING_BURST_ITEMS"):
            load_from("AI_EMBEDDING_BURST_ITEMS=0\n")

    def test_embedding_batch_size_is_bounded(self, load_from) -> None:
        # batchEmbedContents refuses more than 100 items, and a batch of zero
        # would make the pacer spend nothing.
        with pytest.raises(ValueError, match="AI_EMBEDDING_BATCH_SIZE"):
            load_from("AI_EMBEDDING_BATCH_SIZE=101\n")
        with pytest.raises(ValueError, match="AI_EMBEDDING_BATCH_SIZE"):
            load_from("AI_EMBEDDING_BATCH_SIZE=0\n")

    def test_embedding_burst_must_cover_a_batch(self, load_from) -> None:
        # A burst smaller than one batch would make every batch wait, defeating
        # the burst's purpose of keeping small files instant.
        with pytest.raises(ValueError, match="AI_EMBEDDING_BURST_ITEMS"):
            load_from("AI_EMBEDDING_BURST_ITEMS=10\nAI_EMBEDDING_BATCH_SIZE=50\n")

    def test_embedding_rate_defaults_to_the_free_tier(self, settings) -> None:
        # The defaults have to fit the free tier; higher ones reproduce the 429
        # failures this pacing exists to prevent.
        assert settings.ai_embedding_batch_size == 50
        assert settings.ai_embedding_items_per_minute == 120
        assert settings.ai_embedding_burst_items == 240

    def test_similarity_score_outside_zero_to_one_is_rejected(self, load_from) -> None:
        with pytest.raises(ValueError, match="RETRIEVAL_MIN_SCORE"):
            load_from("RETRIEVAL_MIN_SCORE=1.4\n")

    def test_empty_content_type_allowlist_is_rejected(self, load_from) -> None:
        with pytest.raises(ValueError, match="ALLOWED_CONTENT_TYPES"):
            load_from("ALLOWED_CONTENT_TYPES=\n")

    def test_render_injected_variables_do_not_break_loading(self, load_from, monkeypatch) -> None:
        monkeypatch.setenv("RENDER", "true")
        monkeypatch.setenv("RENDER_INSTANCE_ID", "srv-abc")

        assert load_from().is_production is False


class TestProductionWiring:
    """Boot the app the way production does: no injected repository.

    Every other auth test injects `InMemoryUserRepository`, which means it never
    exercises the code that builds `MongoUserRepository` from a real client. That
    gap hid two startup-only bugs: constructing the repository before
    `mongo.connect()`, and PyMongo's refusal to encode a native `uuid.UUID`.
    """

    def test_repository_is_built_after_connect(self, settings: Settings) -> None:
        from fastapi.testclient import TestClient

        from knowledgedock.app import create_app
        from knowledgedock.infrastructure.repositories.user_repository import (
            MongoUserRepository,
        )
        from tests.conftest import FakeDatabase, FakeMongoManager

        app = create_app(
            settings,
            mongo_manager=FakeMongoManager(database=FakeDatabase()),
        )
        with TestClient(app) as client:
            assert isinstance(app.state.register_user._repository, MongoUserRepository)
            assert client.get("/health").status_code == 200

    def test_database_is_requested_only_after_the_client_exists(self, settings: Settings) -> None:
        """`MongoManager.database()` raises before `connect()`. Prove the order."""
        from fastapi.testclient import TestClient

        from knowledgedock.app import create_app
        from tests.conftest import FakeDatabase, FakeMongoManager

        fake = FakeMongoManager(database=FakeDatabase())
        app = create_app(settings, mongo_manager=fake)
        with TestClient(app):
            assert fake.connect_called, "connect() must run during startup"
            assert fake.database_called_after_connect, (
                "database() was called before connect(); this crashes on a real startup"
            )

    def test_startup_survives_an_unreachable_database(self, settings: Settings) -> None:
        """A dead Atlas must not stop the process binding $PORT (Phase 01 decision).

        `connect()` deliberately does not raise, so the client object exists even
        when the ping failed. `database()` therefore succeeds and the failure
        surfaces later, as a `ServerSelectionTimeoutError` on the first query.
        """
        from fastapi.testclient import TestClient
        from pymongo.errors import ServerSelectionTimeoutError

        from knowledgedock.app import create_app
        from tests.conftest import FakeMongoManager

        class DeadDatabase:
            def __getitem__(self, name: str) -> object:
                class DeadCollection:
                    async def find_one(self, *a: object, **k: object) -> object:
                        raise ServerSelectionTimeoutError("no Atlas reachable")

                    async def insert_one(self, *a: object, **k: object) -> object:
                        raise ServerSelectionTimeoutError("no Atlas reachable")

                    async def create_index(self, *a: object, **k: object) -> str:
                        return "ok"

                    async def count_documents(self, *a: object, **k: object) -> int:
                        return 0

                    def find(self, *a: object, **k: object) -> object:
                        return _EmptyCursor()

                    async def find_one_and_update(self, *a: object, **k: object) -> None:
                        return None

                    async def delete_many(self, *a: object, **k: object) -> object:
                        return _EmptyResult()

                    async def insert_many(self, *a: object, **k: object) -> None:
                        return None

                    async def list_search_indexes(self) -> object:
                        return _EmptyCursor()

                return DeadCollection()

        app = create_app(
            settings, mongo_manager=FakeMongoManager(reachable=False, database=DeadDatabase())
        )
        with TestClient(app) as client:
            # The process is up and honest about it.
            assert client.get("/health").status_code == 200
            assert client.get("/readyz").status_code == 503

            # A failing query becomes a clean 503, not an unhandled 500.
            registration = client.post(
                "/auth/register", json={"email": "a@b.com", "password": "password123"}
            )
            assert registration.status_code == 503
            assert registration.json()["error"]["code"] == "service_unavailable"

            login = client.post("/auth/login", json={"email": "a@b.com", "password": "password123"})
            assert login.status_code == 503
            # Crucially not this: an outage must not look like a bad password.
            assert login.json()["error"]["code"] != "authentication_failed"

    def test_missing_client_produces_a_service_unavailable_repository(
        self, settings: Settings
    ) -> None:
        """Defensive: no client at all means no usable repository, not a crash."""
        from fastapi.testclient import TestClient

        from knowledgedock.app import create_app
        from knowledgedock.infrastructure.repositories.user_repository import (
            UnavailableUserRepository,
        )
        from tests.conftest import FakeMongoManager

        # `connected=False` makes `connect()` a no-op that never yields a client.
        class NeverConnects(FakeMongoManager):
            async def connect(self) -> None:
                self.connect_called = True
                self._connected = False

        app = create_app(settings, mongo_manager=NeverConnects(reachable=False))
        with TestClient(app) as client:
            assert isinstance(app.state.register_user._repository, UnavailableUserRepository)
            assert client.get("/health").status_code == 200
            assert (
                client.post(
                    "/auth/register", json={"email": "a@b.com", "password": "password123"}
                ).status_code
                == 503
            )
