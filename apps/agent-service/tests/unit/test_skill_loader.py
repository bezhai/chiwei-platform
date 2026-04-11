"""Tests for skill loader and registry"""

import textwrap
from pathlib import Path

import pytest

from app.skills.loader import SkillDefinition, parse_skill_file
from app.skills.registry import SkillRegistry

# ─── Fixtures ───────────────────────────────────────────────


@pytest.fixture
def skills_dir(tmp_path: Path) -> Path:
    """创建包含多个 skill 的临时目录"""
    # Skill 1: 有预处理指令
    skill1 = tmp_path / "query_db"
    skill1.mkdir()
    (skill1 / "SKILL.md").write_text(
        textwrap.dedent("""\
            ---
            description: 查询数据库并分析结果
            ---

            # query-db

            ## 表结构

            ```
            !`python3 scripts/schema.py`
            ```

            ## 参数

            ```
            !`echo "$ARGUMENTS"`
            ```

            ## 指令

            1. 阅读上方的表结构
            2. 用 sandbox_bash 执行 SQL 查询
        """)
    )

    # Skill 2: 无预处理指令
    skill2 = tmp_path / "explain_code"
    skill2.mkdir()
    (skill2 / "SKILL.md").write_text(
        textwrap.dedent("""\
            ---
            description: 解释代码的功能和设计意图
            ---

            # explain-code

            ## 指令

            仔细阅读用户提供的代码，解释其功能和设计意图。
        """)
    )

    # Skill 3: 没有 SKILL.md（应被忽略）
    not_skill = tmp_path / "not_a_skill"
    not_skill.mkdir()
    (not_skill / "README.md").write_text("This is not a skill")

    return tmp_path


@pytest.fixture
def single_skill_path(tmp_path: Path) -> Path:
    """单个 skill 文件"""
    skill = tmp_path / "hello"
    skill.mkdir()
    path = skill / "SKILL.md"
    path.write_text(
        textwrap.dedent("""\
            ---
            description: 测试技能
            ---

            # hello

            ## 预处理

            ```
            !`uname -a`
            ```

            ## 指令

            展示环境信息给用户。参数: $ARGUMENTS
        """)
    )
    return path


# ─── parse_skill_file tests ────────────────────────────────


class TestParseSkillFile:
    def test_parse_basic_skill(self, single_skill_path: Path):
        skill = parse_skill_file(single_skill_path)

        assert isinstance(skill, SkillDefinition)
        assert skill.name == "hello"
        assert skill.description == "测试技能"
        assert "$ARGUMENTS" in skill.raw_body

    def test_parse_extracts_preprocessing(self, single_skill_path: Path):
        skill = parse_skill_file(single_skill_path)

        assert len(skill.preprocessing) == 1
        assert skill.preprocessing[0].command == "uname -a"

    def test_parse_multiple_preprocessing(self, skills_dir: Path):
        skill = parse_skill_file(skills_dir / "query_db" / "SKILL.md")

        assert len(skill.preprocessing) == 2
        assert skill.preprocessing[0].command == "python3 scripts/schema.py"
        assert skill.preprocessing[1].command == 'echo "$ARGUMENTS"'

    def test_parse_no_preprocessing(self, skills_dir: Path):
        skill = parse_skill_file(skills_dir / "explain_code" / "SKILL.md")

        assert len(skill.preprocessing) == 0

    def test_parse_name_from_directory(self, skills_dir: Path):
        skill = parse_skill_file(skills_dir / "query_db" / "SKILL.md")
        assert skill.name == "query_db"

    def test_parse_base_path(self, skills_dir: Path):
        skill = parse_skill_file(skills_dir / "query_db" / "SKILL.md")
        assert skill.base_path == skills_dir / "query_db"

    def test_parse_missing_description_raises(self, tmp_path: Path):
        skill_dir = tmp_path / "bad"
        skill_dir.mkdir()
        path = skill_dir / "SKILL.md"
        path.write_text("# no-frontmatter\n\nJust content.")

        with pytest.raises(ValueError, match="description"):
            parse_skill_file(path)

    def test_parse_empty_frontmatter_raises(self, tmp_path: Path):
        skill_dir = tmp_path / "bad2"
        skill_dir.mkdir()
        path = skill_dir / "SKILL.md"
        path.write_text("---\n---\n\n# empty\n\nContent.")

        with pytest.raises(ValueError, match="description"):
            parse_skill_file(path)


# ─── SkillRegistry tests ───────────────────────────────────


class TestSkillRegistry:
    def setup_method(self):
        """每个测试前清空注册表"""
        SkillRegistry._skills.clear()

    def test_load_all(self, skills_dir: Path):
        SkillRegistry.load_all(skills_dir)

        # 只加载有 SKILL.md 的目录
        assert len(SkillRegistry._skills) == 2
        assert "query_db" in SkillRegistry._skills
        assert "explain_code" in SkillRegistry._skills
        assert "not_a_skill" not in SkillRegistry._skills

    def test_get_existing(self, skills_dir: Path):
        SkillRegistry.load_all(skills_dir)

        skill = SkillRegistry.get("query_db")
        assert skill.description == "查询数据库并分析结果"

    def test_get_missing_raises(self, skills_dir: Path):
        SkillRegistry.load_all(skills_dir)

        with pytest.raises(KeyError, match="unknown_skill"):
            SkillRegistry.get("unknown_skill")

    def test_list_descriptions(self, skills_dir: Path):
        SkillRegistry.load_all(skills_dir)

        desc = SkillRegistry.list_descriptions()
        assert "query_db" in desc
        assert "查询数据库并分析结果" in desc
        assert "explain_code" in desc
        assert "解释代码的功能和设计意图" in desc

    def test_load_empty_dir(self, tmp_path: Path):
        SkillRegistry.load_all(tmp_path)
        assert len(SkillRegistry._skills) == 0

    def test_load_nonexistent_dir(self, tmp_path: Path):
        SkillRegistry.load_all(tmp_path / "nonexistent")
        assert len(SkillRegistry._skills) == 0
