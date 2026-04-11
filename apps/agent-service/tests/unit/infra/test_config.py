"""Tests for app.infra.config.Settings."""

from __future__ import annotations

import dataclasses
from unittest.mock import patch


class TestSettingsDefaults:
    """Settings should have correct defaults when env vars are absent."""

    def test_frozen(self):
        """Settings must be a frozen dataclass."""
        from app.infra.config import Settings

        assert dataclasses.is_dataclass(Settings)
        # frozen -> FrozenInstanceError on assignment
        s = Settings()
        try:
            s.redis_host = "nope"  # type: ignore[misc]
            raise AssertionError("Expected FrozenInstanceError")
        except dataclasses.FrozenInstanceError:
            pass

    def test_default_none_fields(self):
        """Optional string fields default to None when env is empty."""
        with patch.dict("os.environ", {}, clear=True):
            from app.infra.config import Settings

            s = Settings()
            assert s.redis_host is None
            assert s.redis_password is None
            assert s.postgres_host is None
            assert s.rabbitmq_url is None
            assert s.langfuse_public_key is None
            assert s.qdrant_service_api_key is None
            assert s.lane is None

    def test_default_int_fields(self):
        """Integer fields have correct defaults."""
        with patch.dict("os.environ", {}, clear=True):
            from app.infra.config import Settings

            s = Settings()
            assert s.postgres_port == 5432
            assert s.qdrant_service_port == 6333
            assert s.main_server_timeout == 10
            assert s.long_task_batch_size == 5
            assert s.long_task_lock_timeout == 1800
            assert s.identity_drift_debounce_seconds == 120
            assert s.identity_drift_max_buffer == 10
            assert s.identity_drift_ttl_seconds == 86400

    def test_default_string_fields(self):
        """String fields with non-None defaults."""
        with patch.dict("os.environ", {}, clear=True):
            from app.infra.config import Settings

            s = Settings()
            assert s.siliconflow_base_url == "https://api.siliconflow.cn/v1"
            assert s.diary_chat_ids == ""
            assert s.diary_model == "diary-model"
            assert s.life_engine_model == "offline-model"
            assert s.relationship_model == "relationship-model"
            assert s.identity_drift_model == "offline-model"


class TestSettingsFromEnv:
    """Settings should read values from environment variables."""

    def test_reads_env_vars(self):
        env = {
            "REDIS_HOST": "redis.local",
            "REDIS_PASSWORD": "s3cret",
            "POSTGRES_PORT": "5433",
            "RABBITMQ_URL": "amqp://guest:guest@mq:5672/",
            "LANE": "dev",
            "MAIN_SERVER_TIMEOUT": "30",
            "SILICONFLOW_BASE_URL": "https://custom.url/v1",
            "LONG_TASK_BATCH_SIZE": "10",
        }
        with patch.dict("os.environ", env, clear=True):
            from app.infra.config import Settings

            s = Settings()
            assert s.redis_host == "redis.local"
            assert s.redis_password == "s3cret"
            assert s.postgres_port == 5433
            assert s.rabbitmq_url == "amqp://guest:guest@mq:5672/"
            assert s.lane == "dev"
            assert s.main_server_timeout == 30
            assert s.siliconflow_base_url == "https://custom.url/v1"
            assert s.long_task_batch_size == 10

    def test_empty_string_treated_as_none(self):
        """Empty string env vars should become None for optional fields."""
        env = {"REDIS_HOST": "", "LANE": ""}
        with patch.dict("os.environ", env, clear=True):
            from app.infra.config import Settings

            s = Settings()
            assert s.redis_host is None
            assert s.lane is None


class TestFieldNames:
    """field_names() utility."""

    def test_returns_all_fields(self):
        from app.infra.config import Settings

        s = Settings()
        names = s.field_names()
        assert "redis_host" in names
        assert "lane" in names
        assert "identity_drift_ttl_seconds" in names
        assert len(names) > 20  # sanity check


class TestModuleLevelInstance:
    """The module-level ``settings`` should be a Settings instance."""

    def test_module_settings_is_settings(self):
        from app.infra.config import Settings, settings

        assert isinstance(settings, Settings)
