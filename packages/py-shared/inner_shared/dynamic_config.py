"""
DynamicConfig — 运行时动态配置 SDK (Python)

用法::

    from inner_shared.dynamic_config import dynamic_config

    model = dynamic_config.get("default_model", default="gemini")
    threshold = dynamic_config.get_float("proactive_threshold", default=0.7)
    enabled = dynamic_config.get_bool("feature_x_enabled", default=False)
    count = dynamic_config.get_int("max_retry", default=3)
"""

import logging
import os
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_CACHE_TTL = 10  # seconds


class DynamicConfig:
    """
    运行时动态配置读取器。

    从 paas-engine 拉取全量配置快照，按泳道缓存 10s。
    lane 通过 lane_provider 自动获取（从 context），取不到则为 "prod"。
    """

    def __init__(
        self,
        paas_engine_url: str = "http://paas-engine:8080",
        lane_provider: Callable[[], str | None] | None = None,
    ):
        self._paas_engine_url = paas_engine_url.rstrip("/")
        self._lane_provider = lane_provider
        self._cache: dict[str, tuple[dict[str, dict[str, str]], float]] = {}
        self._lock = threading.Lock()

    def _get_lane(self) -> str:
        if self._lane_provider:
            lane = self._lane_provider()
            if lane:
                return lane
        return "prod"

    def _fetch_snapshot(self, lane: str) -> dict[str, dict[str, str]]:
        """从 paas-engine 拉取合并后的配置快照。"""
        url = f"{self._paas_engine_url}/internal/dynamic-config/resolved"
        try:
            with httpx.Client(timeout=5.0) as client:
                resp = client.get(url, params={"lane": lane})
                if resp.status_code == 200:
                    body = resp.json()
                    data = body.get("data", body)
                    return data.get("configs", {})
                logger.warning(
                    "[DynamicConfig] paas-engine responded %d", resp.status_code
                )
        except Exception as e:
            logger.warning("[DynamicConfig] failed to fetch config: %s", e)
        return {}

    def _get_snapshot(self, lane: str) -> dict[str, dict[str, str]]:
        """获取缓存的快照，过期则刷新。"""
        now = time.monotonic()
        with self._lock:
            if lane in self._cache:
                snapshot, expire_at = self._cache[lane]
                if now < expire_at:
                    return snapshot

        # 缓存过期或不存在，拉取新数据（lock 外执行网络请求）
        snapshot = self._fetch_snapshot(lane)
        with self._lock:
            self._cache[lane] = (snapshot, now + _CACHE_TTL)
        return snapshot

    def get(self, key: str, *, default: str = "") -> str:
        """获取配置值（字符串），不存在则返回 default。"""
        lane = self._get_lane()
        snapshot = self._get_snapshot(lane)
        entry = snapshot.get(key)
        if entry is None:
            return default
        return entry.get("value", default)

    def get_int(self, key: str, *, default: int = 0) -> int:
        """获取配置值（整数），转换失败返回 default。"""
        raw = self.get(key, default="")
        if raw == "":
            return default
        try:
            return int(raw)
        except (ValueError, TypeError):
            return default

    def get_float(self, key: str, *, default: float = 0.0) -> float:
        """获取配置值（浮点），转换失败返回 default。"""
        raw = self.get(key, default="")
        if raw == "":
            return default
        try:
            return float(raw)
        except (ValueError, TypeError):
            return default

    def get_bool(self, key: str, *, default: bool = False) -> bool:
        """获取配置值（布尔），true/1/yes 为 True，其他为 False。"""
        raw = self.get(key, default="")
        if raw == "":
            return default
        return raw.lower() in ("true", "1", "yes")


# Module-level singleton — lane_provider defaults to None (resolves to "prod").
# Apps that need lane-aware config should call dynamic_config.set_lane_provider()
# after setting up their context middleware.
dynamic_config = DynamicConfig(
    paas_engine_url=os.getenv("PAAS_ENGINE_URL", "http://paas-engine:8080"),
)
