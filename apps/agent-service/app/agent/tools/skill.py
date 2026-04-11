"""Skill loading tool — progressive context loading.

Loads skill instructions on demand so the agent can follow
domain-specific guides (drawing, donjin_search, etc.).
"""

from __future__ import annotations

import functools
import logging

from langchain.tools import tool

logger = logging.getLogger(__name__)


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
@_tool_error("技能加载失败")
async def load_skill(skill_name: str, arguments: str = "") -> str:
    """加载指定技能的上下文和指令。

    这是一个上下文加载工具，不会直接执行任何操作。
    返回的内容包含技能的使用说明和预处理数据，请根据返回的指令进行后续操作。

    Args:
        skill_name: 技能名称（如 donjin_search）
        arguments: 用户的原始请求或参数
    """
    from app.skills.registry import SkillRegistry
    from app.skills.renderer import render_skill

    skill = SkillRegistry.get(skill_name)
    rendered = await render_skill(skill, arguments)

    logger.info("Skill %s loaded, rendered length: %d", skill_name, len(rendered))
    return rendered
