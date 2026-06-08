from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    return default if raw is None or raw == "" else int(raw)


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    return default if raw is None or raw == "" else float(raw)


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.lower() in {"1", "true", "yes", "y", "on"}


def _path_env(name: str) -> Path | None:
    raw = os.getenv(name)
    return Path(raw) if raw else None


def _csv_env(name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return tuple(part.strip() for part in raw.split(",") if part.strip())


@dataclass(frozen=True)
class Settings:
    role: str
    host: str
    port: int
    sqlite_path: Path
    max_batch_paths: int
    queue_size: int
    idle_unload_seconds: float
    preload_local_qwen: bool
    local_infer_timeout_seconds: float
    exit_on_local_timeout: bool
    callback_retries: int
    callback_timeout_seconds: float
    callback_retry_delay_seconds: float
    callback_allowed_hosts: tuple[str, ...]
    callback_allowed_networks: tuple[str, ...]
    callback_auth_token: str
    api_tokens: tuple[str, ...]
    remote_tagger_url: str | None
    remote_auth_token: str
    remote_timeout_seconds: float
    remote_retries: int
    qwen_model_path: str
    wd14_model_dir: Path | None
    eva02_model_dir: Path | None
    minio_endpoint: str
    minio_port: int
    minio_access_key: str
    minio_secret_key: str
    minio_bucket: str
    minio_secure: bool


def load_settings() -> Settings:
    return Settings(
        role=os.getenv("TAGGER_ROLE", "entry"),
        host=os.getenv("TAGGER_HOST", "0.0.0.0"),
        port=_int_env("TAGGER_PORT", 8000),
        sqlite_path=Path(os.getenv("TAGGER_SQLITE_PATH", "data/tagger_tasks.sqlite3")),
        max_batch_paths=_int_env("TAGGER_MAX_BATCH_PATHS", 64),
        queue_size=_int_env("TAGGER_QUEUE_SIZE", 16),
        idle_unload_seconds=_float_env("TAGGER_IDLE_UNLOAD_SECONDS", 900.0),
        preload_local_qwen=_bool_env("TAGGER_PRELOAD_LOCAL_QWEN", True),
        local_infer_timeout_seconds=_float_env("TAGGER_LOCAL_INFER_TIMEOUT_SECONDS", 180.0),
        exit_on_local_timeout=_bool_env("TAGGER_EXIT_ON_LOCAL_TIMEOUT", True),
        callback_retries=_int_env("TAGGER_CALLBACK_RETRIES", 5),
        callback_timeout_seconds=_float_env("TAGGER_CALLBACK_TIMEOUT_SECONDS", 10.0),
        callback_retry_delay_seconds=_float_env("TAGGER_CALLBACK_RETRY_DELAY_SECONDS", 5.0),
        callback_allowed_hosts=_csv_env("TAGGER_CALLBACK_ALLOWED_HOSTS", ("localhost",)),
        callback_allowed_networks=_csv_env(
            "TAGGER_CALLBACK_ALLOWED_NETWORKS",
            ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"),
        ),
        callback_auth_token=os.getenv("TAGGER_CALLBACK_AUTH_TOKEN", ""),
        api_tokens=_csv_env("TAGGER_API_TOKENS"),
        remote_tagger_url=os.getenv("TAGGER_REMOTE_URL"),
        remote_auth_token=os.getenv("TAGGER_REMOTE_AUTH_TOKEN", ""),
        remote_timeout_seconds=_float_env("TAGGER_REMOTE_TIMEOUT_SECONDS", 120.0),
        remote_retries=_int_env("TAGGER_REMOTE_RETRIES", 2),
        qwen_model_path=os.getenv("TAGGER_QWEN_MODEL_PATH", ""),
        wd14_model_dir=_path_env("TAGGER_WD14_MODEL_DIR"),
        eva02_model_dir=_path_env("TAGGER_EVA02_MODEL_DIR"),
        minio_endpoint=os.getenv("MINIO_ENDPOINT", "minio.prod"),
        minio_port=_int_env("MINIO_PORT", 9000),
        minio_access_key=os.getenv("MINIO_ACCESS_KEY", ""),
        minio_secret_key=os.getenv("MINIO_SECRET_KEY", ""),
        minio_bucket=os.getenv("MINIO_BUCKET", "pixiv"),
        minio_secure=_bool_env("MINIO_SECURE", False),
    )
