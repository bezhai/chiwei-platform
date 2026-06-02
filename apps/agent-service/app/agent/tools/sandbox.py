"""Sandbox bash tool — code execution in a secure sandbox."""

from __future__ import annotations

import logging

from app.agent.tooling import tool
from app.agent.tools._common import tool_error
from app.capabilities.sandbox import run as _sandbox_run

logger = logging.getLogger(__name__)

# Module-level reference for easy mock patching in tests
# (patch ``app.agent.tools.sandbox.run`` to stub sandbox execution).
run = _sandbox_run


@tool
@tool_error("沙箱执行失败")
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
    result = await run(command=command)
    if result.exit_code != 0:
        output = (
            f"命令退出码 {result.exit_code}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    else:
        output = result.stdout
    logger.info("Sandbox executed, output length: %d", len(output))
    return output
