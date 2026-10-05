"""Typed application configuration loaded with `python-decouple`.

Every value the application needs is declared explicitly in `load_settings()`.
Nothing else in the codebase reads the environment, so the full configuration
surface is one readable list and one obvious place to audit for secrets.

Resolution order for each variable (`decouple.Config` semantics):

    1. real process environment  ->  production on Render
    2. the nearest `.env` file   ->  local development

A variable that is in neither place raises `MissingConfigurationError` naming the
variable, instead of silently becoming `None` deep inside a request.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from decouple import AutoConfig, Choices, Csv

PACKAGE_DIR = Path(__file__).resolve().parent.parent

AI_PROVIDERS = ("gemini", "null")
EMBEDDING_TASKS = (
    "RETRIEVAL_DOCUMENT",
    "RETRIEVAL_QUERY",
    "SEMANTIC_SIMILARITY",
    "CLASSIFICATION",
    "CLUSTERING",
    "QUESTION_ANSWERING",
    "FACT_VERIFICATION",
    "CODE_RETRIEVAL_QUERY",
)
VECTOR_SIMILARITIES = ("cosine", "dotProduct", "euclidean")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

# `gemini-embedding-001` accepts 128-3072 dimensions; 2048 input tokens max.
GEMINI_MIN_DIMENSIONS = 128
GEMINI_MAX_DIMENSIONS = 3072
GEMINI_MAX_INPUT_TOKENS = 2048


class MissingConfigurationError(RuntimeError):
    """A required environment variable was not supplied."""


@dataclass(frozen=True, slots=True)
class Settings:
    # -- Runtime -----------------------------------------------------------
    environment: str
    port: int

    # -- Security ----------------------------------------------------------
    secret_key: str
    jwt_expire_minutes: int
    password_min_length: int
    password_reset_expire_minutes: int

    # -- Database ----------------------------------------------------------
    mongodb_uri: str
    mongodb_db: str
    mongodb_server_selection_timeout_ms: int

    # -- AI provider -------------------------------------------------------
    ai_provider: str
    gemini_api_key: str
    gemini_chat_model: str
    gemini_temperature: float
    gemini_max_output_tokens: int
    gemini_embedding_model: str
    gemini_embedding_dimensions: int
    gemini_embedding_task_document: str
    gemini_embedding_task_query: str
    gemini_embedding_max_input_tokens: int
    ai_timeout_seconds: float
    ai_max_retries: int
    gemini_chat_max_output_tokens: int
    gemini_chat_temperature: float
    llm_history_max_messages: int
    llm_max_question_characters: int
    ai_rate_limit_per_minute: int
    ai_retry_backoff_seconds: float
    ai_embedding_requests_per_minute: int
    ai_embedding_burst: int

    # -- Uploads -----------------------------------------------------------
    storage_dir: Path
    max_upload_size_mb: int
    allowed_content_types: tuple[str, ...]

    # -- Chunking ----------------------------------------------------------
    chunk_size: int
    chunk_overlap: int
    min_chunk_size: int

    # -- Retrieval ---------------------------------------------------------
    vector_index_name: str
    vector_similarity: str
    retrieval_top_k: int
    retrieval_min_score: float
    context_max_chars: int

    # -- Rate limiting -----------------------------------------------------
    rate_limit_per_minute: int
    rate_limit_query_per_minute: int
    rate_limit_max_keys: int
    auth_rate_limit_per_minute: int

    # -- Background processing ---------------------------------------------
    processing_max_attempts: int
    processing_stale_after_minutes: int
    processing_poll_interval_seconds: int
    processing_enabled: bool

    # -- Logging -----------------------------------------------------------
    log_level: str

    def __post_init__(self) -> None:
        _check_choice("AI_PROVIDER", self.ai_provider, AI_PROVIDERS)
        _check_choice(
            "GEMINI_EMBEDDING_TASK_DOCUMENT", self.gemini_embedding_task_document, EMBEDDING_TASKS
        )
        _check_choice(
            "GEMINI_EMBEDDING_TASK_QUERY", self.gemini_embedding_task_query, EMBEDDING_TASKS
        )
        _check_choice("VECTOR_SIMILARITY", self.vector_similarity, VECTOR_SIMILARITIES)
        _check_choice("LOG_LEVEL", self.log_level.upper(), LOG_LEVELS)

        if len(self.secret_key) < 32:
            raise ValueError("SECRET_KEY must be at least 32 characters")
        if not self.mongodb_uri.startswith(("mongodb://", "mongodb+srv://")):
            raise ValueError("MONGODB_URI must start with mongodb:// or mongodb+srv://")
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("CHUNK_OVERLAP must be smaller than CHUNK_SIZE")
        if not GEMINI_MIN_DIMENSIONS <= self.gemini_embedding_dimensions <= GEMINI_MAX_DIMENSIONS:
            raise ValueError(
                f"GEMINI_EMBEDDING_DIMENSIONS must be "
                f"{GEMINI_MIN_DIMENSIONS}-{GEMINI_MAX_DIMENSIONS}"
            )
        if not 1 <= self.gemini_embedding_max_input_tokens <= GEMINI_MAX_INPUT_TOKENS:
            raise ValueError(
                f"GEMINI_EMBEDDING_MAX_INPUT_TOKENS must be 1-{GEMINI_MAX_INPUT_TOKENS}"
            )
        # Zero would make the pacing interval infinite and stall every ingestion
        # forever, which reads as a hang rather than a misconfiguration.
        if self.ai_embedding_requests_per_minute < 1:
            raise ValueError("AI_EMBEDDING_REQUESTS_PER_MINUTE must be at least 1")
        if self.ai_embedding_burst < 1:
            raise ValueError("AI_EMBEDDING_BURST must be at least 1")
        # Raw cosine, so the range is -1.0..1.0 and negative is meaningful:
        # opposing text genuinely scores below zero. Clamping the bound to 0.0
        # would silently accept a threshold that rejects everything.
        if not -1.0 <= self.retrieval_min_score <= 1.0:
            raise ValueError("RETRIEVAL_MIN_SCORE must be between -1.0 and 1.0")
        if self.gemini_chat_max_output_tokens < 1:
            raise ValueError("GEMINI_CHAT_MAX_OUTPUT_TOKENS must be at least 1")
        if not 0.0 <= self.gemini_chat_temperature <= 2.0:
            raise ValueError("GEMINI_CHAT_TEMPERATURE must be between 0.0 and 2.0")
        if self.llm_history_max_messages < 0:
            raise ValueError("LLM_HISTORY_MAX_MESSAGES cannot be negative")
        if self.llm_max_question_characters < 1:
            raise ValueError("LLM_MAX_QUESTION_CHARACTERS must be at least 1")
        if self.ai_rate_limit_per_minute < 0:
            raise ValueError("AI_RATE_LIMIT_PER_MINUTE cannot be negative")
        if self.rate_limit_max_keys <= 0:
            raise ValueError("RATE_LIMIT_MAX_KEYS must be positive")
        if self.auth_rate_limit_per_minute < 0:
            raise ValueError("AUTH_RATE_LIMIT_PER_MINUTE cannot be negative")
        if not self.allowed_content_types:
            raise ValueError("ALLOWED_CONTENT_TYPES must list at least one content type")

    @property
    def templates_dir(self) -> Path:
        return PACKAGE_DIR / "templates"

    @property
    def static_dir(self) -> Path:
        return PACKAGE_DIR / "static"

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"


def _check_choice(name: str, value: str, choices: tuple[str, ...]) -> None:
    """Raise a readable error instead of decouple's generic ValueError."""
    try:
        Choices(flat=list(choices), cast=str)(value)
    except ValueError as exc:
        raise ValueError(f"{name}: {exc}") from None


def _reader(source: Any | None) -> Any:
    """Return the decouple reader.

    Defaults to `AutoConfig`, which walks up from the working directory looking
    for a `.env` (or `settings.ini`) and otherwise falls back to the process
    environment — which is how the Render deployment is configured.
    """
    if source is not None:
        return source
    return AutoConfig(search_path=Path.cwd())


def _require(reader: Any, name: str, cast: Any = str) -> Any:
    # Read without a cast first: decouple applies `cast` to the value it returns,
    # so casting before the emptiness check would turn a missing variable into
    # the string "None" instead of raising.
    raw = reader(name, default=None)
    if raw is None or raw == "":
        raise MissingConfigurationError(
            f"{name} is not set. Add it to your environment or to a local .env file. "
            f"See .env.sample for the full list of required variables."
        )
    return cast(raw)


def load_settings(source: Any | None = None) -> Settings:
    """Read every application setting from the environment.

    Pass `source` in tests to read from an isolated `.env` file instead of the
    developer's real one.
    """
    reader = _reader(source)

    def required(name: str, cast: Any = str) -> Any:
        return _require(reader, name, cast)

    def optional(name: str, default: Any, cast: Any = str) -> Any:
        return reader(name, default=default, cast=cast)

    content_types = reader("ALLOWED_CONTENT_TYPES", default=None, cast=Csv(cast=str))

    return Settings(
        environment=required("ENVIRONMENT"),
        # Render injects PORT itself; the fallback only matters for local runs.
        port=optional("PORT", 8000, int),
        secret_key=required("SECRET_KEY"),
        jwt_expire_minutes=optional("JWT_EXPIRE_MINUTES", 60, int),
        password_min_length=optional("PASSWORD_MIN_LENGTH", 8, int),
        password_reset_expire_minutes=optional("PASSWORD_RESET_EXPIRE_MINUTES", 30, int),
        mongodb_uri=required("MONGODB_URI"),
        mongodb_db=optional("MONGODB_DB", "knowledgedock"),
        mongodb_server_selection_timeout_ms=optional(
            "MONGODB_SERVER_SELECTION_TIMEOUT_MS", 8000, int
        ),
        ai_provider=required("AI_PROVIDER"),
        gemini_api_key=required("GEMINI_API_KEY"),
        gemini_chat_model=optional("GEMINI_CHAT_MODEL", "gemini-3.8-flash"),
        gemini_temperature=optional("GEMINI_TEMPERATURE", 0.0, float),
        gemini_max_output_tokens=optional("GEMINI_MAX_OUTPUT_TOKENS", 800, int),
        gemini_embedding_model=optional("GEMINI_EMBEDDING_MODEL", "gemini-embedding-001"),
        gemini_embedding_dimensions=optional("GEMINI_EMBEDDING_DIMENSIONS", 768, int),
        gemini_embedding_task_document=required("GEMINI_EMBEDDING_TASK_DOCUMENT"),
        gemini_embedding_task_query=required("GEMINI_EMBEDDING_TASK_QUERY"),
        gemini_embedding_max_input_tokens=optional("GEMINI_EMBEDDING_MAX_INPUT_TOKENS", 1800, int),
        ai_timeout_seconds=optional("AI_TIMEOUT_SECONDS", 30, float),
        ai_max_retries=optional("AI_MAX_RETRIES", 3, int),
        gemini_chat_max_output_tokens=optional("GEMINI_CHAT_MAX_OUTPUT_TOKENS", 1024, int),
        # 0.2 rather than 0: this is a grounded summariser of retrieved text, and
        # a lower temperature keeps it from embellishing evidence it was given.
        gemini_chat_temperature=optional("GEMINI_CHAT_TEMPERATURE", 0.2, float),
        llm_history_max_messages=optional("LLM_HISTORY_MAX_MESSAGES", 10, int),
        llm_max_question_characters=optional("LLM_MAX_QUESTION_CHARACTERS", 2000, int),
        # 20 questions a minute per user is far above human reading speed and far
        # below Gemini's free per-minute ceiling, so it bounds a runaway client
        # without getting in a real user's way.
        ai_rate_limit_per_minute=optional("AI_RATE_LIMIT_PER_MINUTE", 20, int),
        # Base for exponential backoff with full jitter on provider retries.
        ai_retry_backoff_seconds=optional("AI_RETRY_BACKOFF_SECONDS", 1.0, float),
        # Pacing for outbound embedding calls. Gemini's free tier allows only a
        # handful of embedding requests a minute, and retry backoff cannot fix a
        # call rate that structurally exceeds quota -- it only smooths spikes.
        # Five a minute is the free-tier ceiling for gemini-embedding-001, so a
        # large document takes real time instead of failing. Raise this if the
        # key is on a paid tier.
        ai_embedding_requests_per_minute=optional("AI_EMBEDDING_REQUESTS_PER_MINUTE", 5, int),
        # Bucket capacity. It lets an interactive query embed immediately instead
        # of queueing behind a document that is mid-ingestion, while still
        # bounding the sustained rate. Keep at least 1.
        ai_embedding_burst=optional("AI_EMBEDDING_BURST", 5, int),
        storage_dir=optional("STORAGE_DIR", "/tmp/knowledgedock/uploads"),
        max_upload_size_mb=optional("MAX_UPLOAD_SIZE_MB", 10, int),
        allowed_content_types=tuple(content_types or ()),
        chunk_size=optional("CHUNK_SIZE", 1000, int),
        chunk_overlap=optional("CHUNK_OVERLAP", 200, int),
        min_chunk_size=optional("MIN_CHUNK_SIZE", 100, int),
        vector_index_name=optional("VECTOR_INDEX_NAME", "vector_index"),
        vector_similarity=optional("VECTOR_SIMILARITY", "cosine"),
        retrieval_top_k=optional("RETRIEVAL_TOP_K", 5, int),
        retrieval_min_score=optional("RETRIEVAL_MIN_SCORE", 0.65, float),
        context_max_chars=optional("CONTEXT_MAX_CHARS", 6000, int),
        rate_limit_per_minute=optional("RATE_LIMIT_PER_MINUTE", 60, int),
        rate_limit_query_per_minute=optional("RATE_LIMIT_QUERY_PER_MINUTE", 10, int),
        rate_limit_max_keys=optional("RATE_LIMIT_MAX_KEYS", 10000, int),
        # Credential endpoints, per client IP. Ten a minute is far above how fast
        # anyone signs in, and low enough to bound a password guess: every login
        # attempt costs an Argon2id hash, which is deliberately slow, so the
        # limiter's job here is to bound server work rather than to count guesses.
        auth_rate_limit_per_minute=optional("AUTH_RATE_LIMIT_PER_MINUTE", 10, int),
        processing_max_attempts=optional("PROCESSING_MAX_ATTEMPTS", 3, int),
        processing_stale_after_minutes=optional("PROCESSING_STALE_AFTER_MINUTES", 30, int),
        # How often the worker looks for PENDING documents. Render free tier has no
        # always-on instance, so this is the latency between an upload and its
        # first processing attempt.
        processing_poll_interval_seconds=optional("PROCESSING_POLL_INTERVAL_SECONDS", 10, int),
        # On by default. Disable only where ingestion cannot run, e.g. a
        # read-only replica process.
        processing_enabled=optional("PROCESSING_ENABLED", True, bool),
        log_level=optional("LOG_LEVEL", "INFO"),
    )


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def reset_settings() -> None:
    """Drop the cached singleton. Test-only seam."""
    global _settings
    _settings = None
