"""Sandbox Worker 客户端

调用 sandbox-worker 服务在安全沙箱中执行命令。
"""

import logging

import httpx

from app.api.middleware import get_trace_id
from app.infra.config import settings
from app.infra.lane import lane_router

logger = logging.getLogger(__name__)


class SandboxClient:
    """Sandbox Worker API 客户端"""

    async def execute(
        self,
        command: str,
        skill_name: str = "",
        envs: dict[str, str] | None = None,
        timeout: int = 30,
    ) -> str:
        """在沙箱中执行命令，返回 stdout。

        Args:
            command: 要执行的 bash 命令
            skill_name: 技能名称（设置沙箱工作目录到对应 skill）
            envs: 额外的环境变量
            timeout: 执行超时秒数

        Returns:
            命令的 stdout 输出

        Raises:
            httpx.HTTPStatusError: API 调用失败
        """
        base_url = lane_router.base_url("sandbox-worker")
        headers = {
            "Content-Type": "application/json",
            "X-Trace-Id": get_trace_id() or "",
            **lane_router.get_headers(),
        }
        if settings.inner_http_secret:
            headers["Authorization"] = f"Bearer {settings.inner_http_secret}"

        async with httpx.AsyncClient(timeout=timeout + 15) as client:
            resp = await client.post(
                f"{base_url}/exec",
                json={
                    "command": command,
                    "skill_name": skill_name,
                    "envs": envs or {},
                    "timeout_sec": timeout,
                },
                headers=headers,
            )
            resp.raise_for_status()
            result = resp.json()

            if result["exit_code"] != 0:
                return (
                    f"命令退出码 {result['exit_code']}\n"
                    f"stdout:\n{result['stdout']}\n"
                    f"stderr:\n{result['stderr']}"
                )
            return result["stdout"]


sandbox_client = SandboxClient()
