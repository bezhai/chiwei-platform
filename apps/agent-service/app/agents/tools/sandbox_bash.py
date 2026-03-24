"""sandbox_bash 工具

在安全沙箱中执行 bash 命令。供 skill 指令引用或 agent 直接调用。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from langchain.tools import tool

from app.agents.tools.decorators import tool_error_handler

if TYPE_CHECKING:
    from app.skills.sandbox_client import SandboxClient

logger = logging.getLogger(__name__)

# 模块级引用，支持 mock patch
_sandbox_client: SandboxClient | None = None


def _get_sandbox_client() -> SandboxClient:
    global _sandbox_client
    if _sandbox_client is None:
        from app.skills.sandbox_client import sandbox_client

        _sandbox_client = sandbox_client
    return _sandbox_client


@tool
@tool_error_handler(error_message="沙箱执行失败")
async def sandbox_bash(command: str) -> str:
    """在安全沙箱中执行 bash 命令。

    用于执行数据查询、脚本运行等需要 bash 环境的操作。
    沙箱有资源限制（CPU/内存）和超时（30s），无网络访问。

    Args:
        command: 要执行的 bash 命令
    """
    client = _get_sandbox_client()
    result = await client.execute(command)

    logger.info("Sandbox bash executed, output length: %d", len(result))
    return result
