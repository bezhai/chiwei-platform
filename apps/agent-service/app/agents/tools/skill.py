"""load_skill 工具

按需加载技能上下文（渐进式上下文加载），返回技能指令供后续操作参考。
对标 Claude Code 的 Skill tool。
"""

import logging

from langchain.tools import tool

from app.agents.tools.decorators import tool_error_handler
from app.skills.registry import SkillRegistry
from app.skills.renderer import render_skill

logger = logging.getLogger(__name__)


@tool
@tool_error_handler(error_message="技能加载失败")
async def load_skill(skill_name: str, arguments: str = "") -> str:
    """加载指定技能的上下文和指令。

    这是一个上下文加载工具，不会直接执行任何操作。
    返回的内容包含技能的使用说明和预处理数据，请根据返回的指令进行后续操作。

    Args:
        skill_name: 技能名称（如 donjin_search）
        arguments: 用户的原始请求或参数
    """
    skill = SkillRegistry.get(skill_name)
    rendered = await render_skill(skill, arguments)

    logger.info("Skill %s loaded, rendered length: %d", skill_name, len(rendered))
    return rendered
