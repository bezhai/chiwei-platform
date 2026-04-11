"""Sandbox bash tool — code execution in a secure sandbox."""

from __future__ import annotations

import functools
import logging
from typing import TYPE_CHECKING

from langchain.tools import tool

if TYPE_CHECKING:
    from app.skills.sandbox_client import SandboxClient

logger = logging.getLogger(__name__)

# Module-level reference for easy mock patching in tests
_sandbox_client: SandboxClient | None = None


def _get_sandbox_client() -> SandboxClient:
    global _sandbox_client
    if _sandbox_client is None:
        from app.skills.sandbox_client import sandbox_client

        _sandbox_client = sandbox_client
    return _sandbox_client


def _tool_error(error_message: str):
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except Exception as exc:
                logger.error("%s failed: %s", func.__name__, exc, exc_info=True)
                return f"{error_message}: {exc}"

        return wrapper

    return decorator


@tool
@_tool_error("沙箱执行失败")
async def sandbox_bash(command: str) -> str:
    """在安全沙箱中执行 bash 命令，获取精确的计算或处理结果。

    适用场景：
    - Python 代码执行（数学计算、数据处理、格式转换）
    - 文本处理（正则、编码、统计）
    - 技能脚本执行（按 use_skill 返回的指令操作）

    限制：无网络访问、30 秒超时、256MB 内存、仅 Python 标准库可用。

    Args:
        command: 要执行的 bash 命令（如 python3 -c "print(1+1)"）
    """
    client = _get_sandbox_client()
    result = await client.execute(command)

    logger.info("Sandbox executed, output length: %d", len(result))
    return result
