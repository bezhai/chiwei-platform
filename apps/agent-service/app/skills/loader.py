"""Skill 加载器

解析 SKILL.md 文件：YAML frontmatter + Markdown body + !`command` 预处理指令。
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# 匹配 YAML frontmatter: --- 开头，--- 结尾
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)

# 匹配预处理指令: !`command`（在代码块内或行内）
_PREPROCESS_RE = re.compile(r"!`([^`]+)`")


@dataclass(frozen=True)
class PreprocessDirective:
    """预处理指令"""

    command: str
    label: str = ""


@dataclass(frozen=True)
class SkillDefinition:
    """解析后的 Skill 定义"""

    name: str
    description: str
    raw_body: str
    preprocessing: tuple[PreprocessDirective, ...] = field(default_factory=tuple)
    base_path: Path = field(default_factory=lambda: Path("."))


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """分离 YAML frontmatter 和 body。

    使用简单的字符串解析，不依赖 yaml 库。
    仅支持 `key: value` 单行格式。
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text

    raw_yaml = match.group(1)
    body = text[match.end() :]

    meta: dict[str, str] = {}
    for line in raw_yaml.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()

    return meta, body


def _extract_preprocessing(body: str) -> tuple[PreprocessDirective, ...]:
    """从 body 中提取所有 !`command` 预处理指令。"""
    directives: list[PreprocessDirective] = []
    for match in _PREPROCESS_RE.finditer(body):
        command = match.group(1)
        # 尝试找到最近的 ## 标题作为 label
        preceding = body[: match.start()]
        label = ""
        for heading_match in re.finditer(r"^##\s+(.+)$", preceding, re.MULTILINE):
            label = heading_match.group(1).strip()
        directives.append(PreprocessDirective(command=command, label=label))
    return tuple(directives)


def parse_skill_file(path: Path) -> SkillDefinition:
    """解析单个 SKILL.md 文件。

    Args:
        path: SKILL.md 文件的绝对路径

    Returns:
        SkillDefinition 实例

    Raises:
        ValueError: frontmatter 缺少 description 字段
        FileNotFoundError: 文件不存在
    """
    text = path.read_text(encoding="utf-8")
    meta, body = _parse_frontmatter(text)

    description = meta.get("description", "").strip()
    if not description:
        raise ValueError(
            f"Skill {path} 缺少 description 字段 (YAML frontmatter 中必须包含 description)"
        )

    # name: 优先用 frontmatter 中的 name，否则用目录名
    name = meta.get("name", "").strip() or path.parent.name

    preprocessing = _extract_preprocessing(body)

    return SkillDefinition(
        name=name,
        description=description,
        raw_body=body,
        preprocessing=preprocessing,
        base_path=path.parent,
    )
