"""use_skill 工具

加载指定技能，执行预处理，返回渲染后的指令内容。
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
async def use_skill(skill_name: str, arguments: str = "") -> str:
    """加载并执行指定技能。

    根据技能名称加载技能定义，执行预处理脚本获取上下文数据，
    返回技能指令和预处理结果。请严格按照返回的指令操作。

    Args:
        skill_name: 技能名称（如 hello_sandbox）
        arguments: 传递给技能的参数
    """
    skill = SkillRegistry.get(skill_name)
    rendered = await render_skill(skill, arguments)

    logger.info("Skill %s loaded, rendered length: %d", skill_name, len(rendered))
    return rendered
