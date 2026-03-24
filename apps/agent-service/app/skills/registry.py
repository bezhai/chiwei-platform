"""Skill 注册表

启动时扫描 skills/definitions/ 目录，加载所有 SKILL.md。
提供查询和描述列表生成功能。
"""

import logging
from pathlib import Path
from typing import ClassVar

from app.skills.loader import SkillDefinition, parse_skill_file

logger = logging.getLogger(__name__)


class SkillRegistry:
    """Skill 注册表（参照 AgentRegistry 模式）"""

    _skills: ClassVar[dict[str, SkillDefinition]] = {}

    @classmethod
    def load_all(cls, skills_dir: Path) -> None:
        """扫描目录下所有含 SKILL.md 的子目录并注册。

        Args:
            skills_dir: 技能定义根目录
        """
        cls._skills.clear()

        if not skills_dir.exists():
            logger.warning("Skills directory not found: %s", skills_dir)
            return

        for child in sorted(skills_dir.iterdir()):
            skill_file = child / "SKILL.md"
            if child.is_dir() and skill_file.exists():
                try:
                    skill = parse_skill_file(skill_file)
                    cls._skills[skill.name] = skill
                    logger.info("Loaded skill: %s (%s)", skill.name, skill.description)
                except Exception as e:
                    logger.error("Failed to load skill from %s: %s", skill_file, e)

        logger.info("Loaded %d skills total", len(cls._skills))

    @classmethod
    def get(cls, name: str) -> SkillDefinition:
        """获取指定名称的 Skill。

        Raises:
            KeyError: 技能不存在
        """
        if name not in cls._skills:
            raise KeyError(
                f"未知技能 '{name}'，可用: {', '.join(sorted(cls._skills.keys()))}"
            )
        return cls._skills[name]

    @classmethod
    def list_descriptions(cls) -> str:
        """返回格式化的技能列表（用于注入 system prompt）。

        格式:
        - query_db: 查询数据库并分析结果
        - hello_sandbox: 沙箱测试技能
        """
        if not cls._skills:
            return ""
        lines = [
            f"- {skill.name}: {skill.description}"
            for skill in sorted(cls._skills.values(), key=lambda s: s.name)
        ]
        return "\n".join(lines)

    @classmethod
    def list_all(cls) -> list[SkillDefinition]:
        """返回所有已注册的 Skill 列表。"""
        return list(cls._skills.values())
