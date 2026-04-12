"""Skill 渲染器

执行 !`command` 预处理指令 + 变量替换。
对标 Claude Code 的 skill preprocessing 机制：
- !`command` 在返回给 LLM 之前由 harness 同步执行
- LLM 看到的是执行结果，不是命令本身
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.skills.loader import SkillDefinition

if TYPE_CHECKING:
    from app.skills.sandbox_client import SandboxClient

logger = logging.getLogger(__name__)

# 模块级引用，支持 mock patch
sandbox_client: SandboxClient | None = None


def _get_sandbox_client() -> SandboxClient:
    """获取 sandbox client 单例（延迟初始化）。"""
    global sandbox_client
    if sandbox_client is None:
        from app.skills.sandbox_client import sandbox_client as _client

        sandbox_client = _client
    return sandbox_client


async def render_skill(skill: SkillDefinition) -> str:
    """加载 skill 内容，执行预处理，返回渲染后的 markdown。

    Args:
        skill: Skill 定义

    Returns:
        渲染后的 markdown 文本
    """
    client = _get_sandbox_client()

    content = skill.raw_body

    # 1. 执行 !`command` 预处理
    for directive in skill.preprocessing:
        try:
            result = await client.execute(directive.command, skill.name)
        except Exception as e:
            logger.error(
                "Skill %s preprocessing failed: %s (command: %s)",
                skill.name,
                e,
                directive.command,
            )
            result = f"(预处理失败: {e})"

        # 替换 !`original_command` 为执行结果
        content = content.replace(f"!`{directive.command}`", result)

    # 2. 变量替换
    sandbox_skill_dir = f"/sandbox/skills/{skill.name}"
    content = content.replace("$SKILL_DIR", sandbox_skill_dir)

    return content
