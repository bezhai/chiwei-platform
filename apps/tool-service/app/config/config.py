from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    host: str = "0.0.0.0"
    port: int = 8000
    max_upload_size: int = 20 * 1024 * 1024  # 20MB

    # Auth
    inner_http_secret: str | None = None

    # Database (read bot_config table)
    database_url: str | None = None  # postgresql+asyncpg://...

    # Redis
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_password: str | None = None

    # TOS
    tos_access_key_id: str | None = None
    tos_access_key_secret: str | None = None
    tos_region: str | None = None
    tos_endpoint: str | None = None
    tos_bucket: str | None = None

    model_config = {"env_prefix": "TOOL_"}


settings = Settings()
