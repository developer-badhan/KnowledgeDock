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
        assert settings.ai_provider == "null"

    def test_optional_values_fall_back_to_code_defaults(self, settings: Settings) -> None:
        assert settings.port == 8000
        assert settings.jwt_expire_minutes == 60
        assert settings.chunk_size == 1000
        assert settings.retrieval_top_k == 5
        assert settings.gemini_embedding_dimensions == 768
        assert settings.log_level == "INFO"

    def test_comma_separated_content_types_become_a_tuple(self, settings: Settings) -> None:
        assert settings.allowed_content_types == ("text/plain", "text/markdown")

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
