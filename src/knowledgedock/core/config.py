"""Application settings.

Fail fast: a missing or malformed value raises at import time of `get_settings`
rather than surfacing as a confusing error deep inside a request.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PACKAGE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        # Render injects its own variables (RENDER, RENDER_INSTANCE_ID, ...).
        extra="ignore",
        # Comma-separated values (e.g. ALLOWED_CONTENT_TYPES) are not JSON, so
        # the source layer must hand the raw string to the field validators
        # instead of trying to json.loads() it and failing.
        enable_decoding=False,
    )

    # -- Runtime -----------------------------------------------------------
    environment: str = "production"
    port: int = Field(default=8000, ge=1, le=65535)

    # -- Security ----------------------------------------------------------
    secret_key: str = Field(min_length=32)
    jwt_expire_minutes: int = Field(default=60, ge=1)
    password_min_length: int = Field(default=8, ge=8)
    argon2_memory_cost: int = Field(default=19456, ge=1024)
    bootstrap_admin_email: str | None = None
    bootstrap_admin_password: str | None = None

    @field_validator("bootstrap_admin_email", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        return None if value == "" else value

    # -- Database ----------------------------------------------------------
    mongodb_uri: str
    mongodb_db: str = "knowledgedock"
    mongodb_max_pool_size: int = Field(default=20, ge=1)
    mongodb_server_selection_timeout_ms: int = Field(default=8000, ge=100)
    mongodb_connect_timeout_ms: int = Field(default=8000, ge=100)
    mongodb_socket_timeout_ms: int = Field(default=30000, ge=100)

    # -- AI provider -------------------------------------------------------
    ai_provider: str = "gemini"
    gemini_api_key: str = ""
    gemini_chat_model: str = "gemini-2.5-flash"
    gemini_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    gemini_max_output_tokens: int = Field(default=800, ge=1)
    gemini_embedding_model: str = "gemini-embedding-001"
    gemini_embedding_dimensions: int = Field(default=768, ge=128, le=3072)
    gemini_embedding_task_document: str = "RETRIEVAL_DOCUMENT"
    gemini_embedding_task_query: str = "RETRIEVAL_QUERY"
    gemini_embedding_max_input_tokens: int = Field(default=1800, ge=1, le=2048)
    ai_timeout_seconds: float = Field(default=30.0, gt=0)
    ai_max_retries: int = Field(default=3, ge=0, le=10)

    @field_validator("ai_provider")
    @classmethod
    def _known_provider(cls, value: str) -> str:
        allowed = {"gemini", "null"}
        if value not in allowed:
            raise ValueError(f"AI_PROVIDER must be one of {sorted(allowed)}, got {value!r}")
        return value

    # -- Uploads -----------------------------------------------------------
    storage_dir: Path = Path("/tmp/knowledgedock/uploads")
    max_upload_size_mb: int = Field(default=10, ge=1)
    allowed_content_types: tuple[str, ...] = (
        "text/plain",
        "text/markdown",
        "text/html",
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )

    @field_validator("allowed_content_types", mode="before")
    @classmethod
    def _split_content_types(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        return value

    # -- Chunking ----------------------------------------------------------
    chunk_size: int = Field(default=1000, ge=100)
    chunk_overlap: int = Field(default=200, ge=0)
    min_chunk_size: int = Field(default=100, ge=1)

    @field_validator("chunk_overlap")
    @classmethod
    def _overlap_smaller_than_chunk(cls, value: int, info) -> int:
        chunk_size = info.data.get("chunk_size")
        if chunk_size is not None and value >= chunk_size:
            raise ValueError("CHUNK_OVERLAP must be smaller than CHUNK_SIZE")
        return value

    # -- Retrieval ---------------------------------------------------------
    vector_index_name: str = "vector_index"
    vector_similarity: str = "cosine"
    retrieval_top_k: int = Field(default=5, ge=1, le=100)
    retrieval_min_score: float = Field(default=0.35, ge=0.0, le=1.0)
    context_max_chars: int = Field(default=6000, ge=1)

    @field_validator("vector_similarity")
    @classmethod
    def _known_metric(cls, value: str) -> str:
        allowed = {"cosine", "dotProduct", "euclidean"}
        if value not in allowed:
            raise ValueError(f"VECTOR_SIMILARITY must be one of {sorted(allowed)}")
        return value

    # -- Rate limiting -----------------------------------------------------
    rate_limit_enabled: bool = True
    rate_limit_per_minute: int = Field(default=60, ge=1)
    rate_limit_query_per_minute: int = Field(default=10, ge=1)

    # -- Background processing ---------------------------------------------
    processing_max_attempts: int = Field(default=3, ge=1)
    processing_stale_after_minutes: int = Field(default=30, ge=1)

    # -- Logging -----------------------------------------------------------
    log_level: str = "INFO"
    log_format: str = "json"

    # -- Derived paths -----------------------------------------------------
    @property
    def templates_dir(self) -> Path:
        return PACKAGE_DIR / "templates"

    @property
    def static_dir(self) -> Path:
        return PACKAGE_DIR / "static"

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()  # type: ignore[call-arg]
