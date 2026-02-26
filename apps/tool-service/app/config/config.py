from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    host: str = "0.0.0.0"
    port: int = 8000
    max_upload_size: int = 20 * 1024 * 1024  # 20MB

    model_config = {"env_prefix": "TOOL_"}


settings = Settings()
