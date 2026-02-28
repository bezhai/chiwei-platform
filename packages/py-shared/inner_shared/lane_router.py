"""
LaneRouter - 泳道感知的服务路由器 (Python SDK)

后台轮询 lite-registry 获取服务路由表，根据 context 中的 lane 自动拼接 URL。
"""

import logging
import threading
from collections.abc import Callable
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class LaneRouter:
    """
    泳道感知的服务路由器。

    用法::

        from inner_shared.lane_router import LaneRouter

        lane_router = LaneRouter(
            registry_url="http://lite-registry:8080",
            lane_provider=get_lane,  # 注入 contextvars 读取函数
        )

        url = lane_router.resolve_url("lark-server", "/api/image/process")
        headers = lane_router.get_headers()
    """

    def __init__(
        self,
        registry_url: str,
        poll_interval: int = 30,
        lane_provider: Callable[[], str | None] | None = None,
    ):
        """
        Args:
            registry_url: lite-registry 地址
            poll_interval: 轮询间隔（秒）
            lane_provider: 从 contextvars 读取当前 lane 的回调函数
        """
        self._registry_url = registry_url.rstrip("/")
        self._poll_interval = poll_interval
        self._lane_provider = lane_provider
        self._services: dict[str, dict[str, Any]] = {}
        self._stop_event = threading.Event()

        # 立即拉取一次
        self._poll()

        # 启动 daemon 线程轮询
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def _poll(self) -> None:
        """从 registry 拉取路由表。"""
        try:
            with httpx.Client(timeout=5.0) as client:
                resp = client.get(f"{self._registry_url}/v1/routes")
                if resp.status_code == 200:
                    data = resp.json()
                    self._services = data.get("services", data)
                else:
                    logger.warning(
                        "[LaneRouter] registry responded %d", resp.status_code
                    )
        except Exception as e:
            logger.warning("[LaneRouter] failed to poll registry: %s", e)

    def _poll_loop(self) -> None:
        """后台轮询循环。"""
        while not self._stop_event.wait(self._poll_interval):
            self._poll()

    def _get_lane(self) -> str | None:
        """通过 lane_provider 获取当前 lane。"""
        if self._lane_provider:
            return self._lane_provider()
        return None

    def resolve_url(self, service: str, path: str = "", lane: str | None = None) -> str:
        """
        解析服务的完整 URL。

        Args:
            service: 服务名（如 'lark-server'）
            path: 请求路径（如 '/api/image/process'）
            lane: 可选泳道覆盖，不传则通过 lane_provider 自动获取
        """
        effective_lane = lane if lane is not None else self._get_lane()
        info = self._services.get(service)
        port = info.get("port", 0) if info else 0

        if (
            effective_lane
            and effective_lane != "prod"
            and info
            and effective_lane in info.get("lanes", [])
        ):
            host = f"{service}-{effective_lane}"
        else:
            host = service

        if port and port != 80:
            return f"http://{host}:{port}{path}"
        return f"http://{host}{path}"

    def base_url(self, service: str, lane: str | None = None) -> str:
        """返回 http://host:port（不含 path）。"""
        return self.resolve_url(service, "", lane)

    def get_headers(self) -> dict[str, str]:
        """返回需注入的 headers（x-lane）。"""
        headers: dict[str, str] = {}
        lane = self._get_lane()
        if lane:
            headers["x-lane"] = lane
        return headers

    def stop(self) -> None:
        """停止后台轮询。"""
        self._stop_event.set()
