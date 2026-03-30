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

    # You Search 配置（已弃用，保留兼容）
    you_search_host: str | None = None
    you_search_api_key: str | None = None

    # Google Custom Search
    google_search_host: str | None = None  # 完整 endpoint URL
    google_search_api_key: str | None = None
    google_search_cx: str | None = None

    bangumi_access_token: str | None = None

    inner_http_secret: str | None = None

    main_server_timeout: int = 10  # 超时时间，默认10秒

    # RabbitMQ
    rabbitmq_url: str | None = None

    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_host: str | None = None

    # SiliconFlow（rerank 模型）
    siliconflow_base_url: str = "https://api.siliconflow.cn/v1"
    siliconflow_api_key: str | None = None

    # 正向代理（供 Google 等需要代理的供应商使用）
    forward_proxy_url: str | None = None

    # 长期任务配置
    long_task_batch_size: int = 5
    long_task_lock_timeout: int = 1800  # 30分钟

    # 日记生成
    diary_chat_ids: str = ""  # 逗号分隔的 chat_id 列表
    diary_model: str = "diary-model"

    # 泳道标识（worker 无 HTTP 请求上下文时用此 fallback）
    lane: str | None = None

    # Identity 漂移
    identity_drift_model: str = "offline-model"
    identity_drift_debounce_seconds: int = 300  # 一阶段等待: 5 分钟
    identity_drift_max_buffer: int = 20  # 强制 flush 阈值
    identity_drift_ttl_seconds: int = 86400  # Redis TTL: 24 小时

    class Config:
        env_file = ".env"
        extra = "ignore"


# 实例化 settings 对象
settings = Settings()
