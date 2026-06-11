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


def _env_list(key: str) -> list[str]:
    raw = environ.get(key, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable application settings loaded from environment variables."""

    # -- Redis --
    redis_host: str | None = field(default_factory=lambda: _env_or_none("REDIS_HOST"))
    redis_port: int = field(default_factory=lambda: _env_int("REDIS_PORT", 6379))
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
    qweather_api_key: str | None = field(
        default_factory=lambda: _env_or_none("QWEATHER_API_KEY")
    )
    # 和风 2024 改版后每个账号分配专属 API Host（统一 devapi/api 域名对新 key 返
    # Invalid Host 403）。host 是账号级敏感信息，走 env 注入、不入代码。
    qweather_api_host: str | None = field(
        default_factory=lambda: _env_or_none("QWEATHER_API_HOST")
    )
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

    # -- Lane (fallback for workers without HTTP context) --
    lane: str | None = field(default_factory=lambda: _env_or_none("LANE"))

    # -- Life Engine --
    life_engine_model: str = field(
        default_factory=lambda: _env("LIFE_ENGINE_MODEL", "offline-model")
    )

    # -- Utility --

    def field_names(self) -> list[str]:
        """Return all field names (useful for introspection / tests)."""
        return [f.name for f in fields(self)]


settings = Settings()
