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
            assert s.lane is None

    def test_qdrant_fields_removed(self):
        """qdrant 已随 v4 记忆整机删除：settings 不再有任何 qdrant 字段。"""
        from app.infra.config import Settings

        names = Settings().field_names()
        leftovers = [n for n in names if "qdrant" in n]
        assert leftovers == []

    def test_default_int_fields(self):
        """Integer fields have correct defaults."""
        with patch.dict("os.environ", {}, clear=True):
            from app.infra.config import Settings

            s = Settings()
            assert s.postgres_port == 5432
            assert s.main_server_timeout == 10

    def test_redis_port_defaults_to_6379(self):
        """REDIS_PORT defaults to 6379 so prod (port 6379) is unaffected."""
        with patch.dict("os.environ", {}, clear=True):
            from app.infra.config import Settings

            s = Settings()
            assert s.redis_port == 6379

    def test_default_string_fields(self):
        """String fields with non-None defaults."""
        with patch.dict("os.environ", {}, clear=True):
            from app.infra.config import Settings

            s = Settings()
            assert s.siliconflow_base_url == "https://api.siliconflow.cn/v1"
            assert s.life_engine_model == "offline-model"


class TestSettingsFromEnv:
    """Settings should read values from environment variables."""

    def test_reads_env_vars(self):
        env = {
            "REDIS_HOST": "redis.local",
            "REDIS_PORT": "6380",
            "REDIS_PASSWORD": "s3cret",
            "POSTGRES_PORT": "5433",
            "RABBITMQ_URL": "amqp://guest:guest@mq:5672/",
            "LANE": "dev",
            "MAIN_SERVER_TIMEOUT": "30",
            "SILICONFLOW_BASE_URL": "https://custom.url/v1",
        }
        with patch.dict("os.environ", env, clear=True):
            from app.infra.config import Settings

            s = Settings()
            assert s.redis_host == "redis.local"
            assert s.redis_port == 6380
            assert s.redis_password == "s3cret"
            assert s.postgres_port == 5433
            assert s.rabbitmq_url == "amqp://guest:guest@mq:5672/"
            assert s.lane == "dev"
            assert s.main_server_timeout == 30
            assert s.siliconflow_base_url == "https://custom.url/v1"

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
        # voice 子系统拆除：drift（voice 再生成）配置不得残留。
        assert not any(n.startswith("identity_drift") for n in names)
        assert len(names) > 20  # sanity check


class TestModuleLevelInstance:
    """The module-level ``settings`` should be a Settings instance."""

    def test_module_settings_is_settings(self):
        from app.infra.config import Settings, settings

        assert isinstance(settings, Settings)
