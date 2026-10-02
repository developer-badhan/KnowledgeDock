"""Phase 1 — health probes and configuration.

Focus is business behaviour: does the app answer correctly, does readiness
reflect reality, and does configuration fail loudly when it is wrong.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from knowledgedock.core.config import Settings

from .conftest import TEST_ENV


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


class TestTemplates:
    def test_index_renders_through_jinja(self, client: TestClient) -> None:
        response = client.get("/")

        assert response.status_code == 200
        assert "KnowledgeDock" in response.text

    def test_static_assets_are_served(self, client: TestClient) -> None:
        assert client.get("/static/app.css").status_code == 200


class TestSettings:
    def test_reads_defaults_for_unset_values(self, settings: Settings) -> None:
        assert settings.mongodb_db == "knowledgedock_test"
        assert settings.retrieval_top_k == 5
        assert settings.gemini_embedding_dimensions == 768

    def test_comma_separated_content_types_become_a_tuple(self, settings: Settings) -> None:
        assert settings.allowed_content_types == ("text/plain", "text/markdown")

    def test_missing_secret_key_is_rejected(self) -> None:
        env = {k: v for k, v in TEST_ENV.items() if k != "SECRET_KEY"}

        with pytest.raises(ValidationError):
            Settings(**env, _env_file=None)  # type: ignore[arg-type]

    def test_short_secret_key_is_rejected(self) -> None:
        env = {**TEST_ENV, "SECRET_KEY": "too-short"}

        with pytest.raises(ValidationError):
            Settings(**env, _env_file=None)  # type: ignore[arg-type]

    def test_missing_mongodb_uri_is_rejected(self) -> None:
        env = {k: v for k, v in TEST_ENV.items() if k != "MONGODB_URI"}

        with pytest.raises(ValidationError):
            Settings(**env, _env_file=None)  # type: ignore[arg-type]

    def test_unknown_ai_provider_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="AI_PROVIDER"):
            Settings(**{**TEST_ENV, "AI_PROVIDER": "anthropic"}, _env_file=None)  # type: ignore[arg-type]

    def test_overlap_larger_than_chunk_size_is_rejected(self) -> None:
        env = {**TEST_ENV, "CHUNK_SIZE": "500", "CHUNK_OVERLAP": "800"}

        with pytest.raises(ValidationError, match="CHUNK_OVERLAP"):
            Settings(**env, _env_file=None)  # type: ignore[arg-type]

    def test_embedding_dimensions_respect_the_gemini_ceiling(self) -> None:
        with pytest.raises(ValidationError, match="less than or equal to 3072"):
            Settings(**{**TEST_ENV, "GEMINI_EMBEDDING_DIMENSIONS": "4096"}, _env_file=None)  # type: ignore[arg-type]

    def test_embedding_input_tokens_stay_under_the_gemini_limit(self) -> None:
        with pytest.raises(ValidationError, match="less than or equal to 2048"):
            Settings(**{**TEST_ENV, "GEMINI_EMBEDDING_MAX_INPUT_TOKENS": "4096"}, _env_file=None)  # type: ignore[arg-type]

    def test_render_injected_variables_are_ignored(self) -> None:
        env = {**TEST_ENV, "RENDER": "true", "RENDER_INSTANCE_ID": "srv-abc"}

        assert Settings(**env, _env_file=None).is_production is False
