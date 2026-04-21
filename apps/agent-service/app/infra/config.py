"""Application settings — single frozen dataclass, module-level ``settings``."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from os import environ


def _env(key: str, default: str = "") -> str:
    return environ.get(key, default)


def _env_or_none(key: str) -> str | None:
    return environ.get(key) or None


def _env_int(key: str, default: int) -> int:
    raw = environ.get(key)
    return int(raw) if raw else default


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable application settings loaded from environment variables."""

    # -- Redis --
    redis_host: str | None = field(default_factory=lambda: _env_or_none("REDIS_HOST"))
    redis_password: str | None = field(
        default_factory=lambda: _env_or_none("REDIS_PASSWORD")
    )

    # -- PostgreSQL --
    postgres_host: str | None = field(
        default_factory=lambda: _env_or_none("POSTGRES_HOST")
    )
    postgres_port: int = field(default_factory=lambda: _env_int("POSTGRES_PORT", 5432))
    postgres_user: str | None = field(
        default_factory=lambda: _env_or_none("POSTGRES_USER")
    )
    postgres_password: str | None = field(
        default_factory=lambda: _env_or_none("POSTGRES_PASSWORD")
    )
    postgres_db: str | None = field(default_factory=lambda: _env_or_none("POSTGRES_DB"))

    # -- Qdrant --
    qdrant_service_host: str | None = field(
        default_factory=lambda: _env_or_none("QDRANT_SERVICE_HOST")
    )
    qdrant_service_port: int = field(
        default_factory=lambda: _env_int("QDRANT_SERVICE_PORT", 6333)
    )
    qdrant_service_api_key: str | None = field(
        default_factory=lambda: _env_or_none("QDRANT_SERVICE_API_KEY")
    )

    # -- Search (You Search is the primary provider) --
    you_search_host: str | None = field(
        default_factory=lambda: _env_or_none("YOU_SEARCH_HOST")
    )
    you_search_api_key: str | None = field(
        default_factory=lambda: _env_or_none("YOU_SEARCH_API_KEY")
    )

    # -- Google Custom Search --
    google_search_host: str | None = field(
        default_factory=lambda: _env_or_none("GOOGLE_SEARCH_HOST")
    )
    google_search_api_key: str | None = field(
        default_factory=lambda: _env_or_none("GOOGLE_SEARCH_API_KEY")
    )
    google_search_cx: str | None = field(
        default_factory=lambda: _env_or_none("GOOGLE_SEARCH_CX")
    )

    # -- Misc --
    bangumi_access_token: str | None = field(
        default_factory=lambda: _env_or_none("BANGUMI_ACCESS_TOKEN")
    )
    inner_http_secret: str | None = field(
        default_factory=lambda: _env_or_none("INNER_HTTP_SECRET")
    )
    main_server_timeout: int = field(
        default_factory=lambda: _env_int("MAIN_SERVER_TIMEOUT", 10)
    )

    # -- RabbitMQ --
    rabbitmq_url: str | None = field(
        default_factory=lambda: _env_or_none("RABBITMQ_URL")
    )

    # -- Langfuse --
    langfuse_public_key: str | None = field(
        default_factory=lambda: _env_or_none("LANGFUSE_PUBLIC_KEY")
    )
    langfuse_secret_key: str | None = field(
        default_factory=lambda: _env_or_none("LANGFUSE_SECRET_KEY")
    )
    langfuse_host: str | None = field(
        default_factory=lambda: _env_or_none("LANGFUSE_HOST")
    )

    # -- SiliconFlow (rerank) --
    siliconflow_base_url: str = field(
        default_factory=lambda: _env(
            "SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1"
        )
    )
    siliconflow_api_key: str | None = field(
        default_factory=lambda: _env_or_none("SILICONFLOW_API_KEY")
    )

    # -- Forward proxy --
    forward_proxy_url: str | None = field(
        default_factory=lambda: _env_or_none("FORWARD_PROXY_URL")
    )

    # -- Long task --
    long_task_batch_size: int = field(
        default_factory=lambda: _env_int("LONG_TASK_BATCH_SIZE", 5)
    )
    long_task_lock_timeout: int = field(
        default_factory=lambda: _env_int("LONG_TASK_LOCK_TIMEOUT", 1800)
    )

    # -- Lane (fallback for workers without HTTP context) --
    lane: str | None = field(default_factory=lambda: _env_or_none("LANE"))

    # -- Life Engine --
    life_engine_model: str = field(
        default_factory=lambda: _env("LIFE_ENGINE_MODEL", "offline-model")
    )

    # -- Identity drift --
    identity_drift_model: str = field(
        default_factory=lambda: _env("IDENTITY_DRIFT_MODEL", "offline-model")
    )
    identity_drift_debounce_seconds: int = field(
        default_factory=lambda: _env_int("IDENTITY_DRIFT_DEBOUNCE_SECONDS", 120)
    )
    identity_drift_max_buffer: int = field(
        default_factory=lambda: _env_int("IDENTITY_DRIFT_MAX_BUFFER", 10)
    )
    identity_drift_ttl_seconds: int = field(
        default_factory=lambda: _env_int("IDENTITY_DRIFT_TTL_SECONDS", 86400)
    )

    # -- Utility --

    def field_names(self) -> list[str]:
        """Return all field names (useful for introspection / tests)."""
        return [f.name for f in fields(self)]


settings = Settings()
