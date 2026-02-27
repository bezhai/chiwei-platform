from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    redis_host: str | None = None
    redis_password: str | None = None

    # 数据库配置
    postgres_host: str | None = None
    postgres_port: int = 5432
    postgres_user: str | None = None
    postgres_password: str | None = None
    postgres_db: str | None = None

    # Qdrant配置
    qdrant_service_host: str | None = None
    qdrant_service_port: int = 6333
    qdrant_service_api_key: str | None = None

    search_api_key: str | None = None

    bangumi_access_token: str | None = None

    inner_http_secret: str | None = None

    main_server_timeout: int = 10  # 超时时间，默认10秒

    # RabbitMQ
    rabbitmq_url: str | None = None

    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_host: str | None = None

    # 长期任务配置
    long_task_batch_size: int = 5
    long_task_lock_timeout: int = 1800  # 30分钟

    class Config:
        env_file = ".env"
        extra = "ignore"


# 实例化 settings 对象
settings = Settings()
