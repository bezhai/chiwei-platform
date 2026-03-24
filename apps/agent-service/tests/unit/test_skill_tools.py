"""Tests for load_skill and sandbox_bash tools"""

import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.skills.registry import SkillRegistry


@pytest.fixture(autouse=True)
def _clean_registry():
    """每个测试前清空注册表"""
    SkillRegistry._skills.clear()
    yield
    SkillRegistry._skills.clear()


@pytest.fixture
def skills_dir(tmp_path: Path) -> Path:
    skill = tmp_path / "greet"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        textwrap.dedent("""\
            ---
            description: 打招呼技能
            ---

            # greet

            向 $ARGUMENTS 打招呼。用 sandbox_bash 执行 `echo hello`。
        """)
    )
    SkillRegistry.load_all(tmp_path)
    return tmp_path


class TestLoadSkill:
    @pytest.mark.asyncio
    async def test_load_skill_loads_and_renders(self, skills_dir):
        from app.agents.tools.skill import load_skill

        result = await load_skill.ainvoke(
            {"skill_name": "greet", "arguments": "赤尾"}
        )

        assert "赤尾" in result
        assert "sandbox_bash" in result
        assert "$ARGUMENTS" not in result

    @pytest.mark.asyncio
    async def test_load_skill_unknown_name(self, skills_dir):
        from app.agents.tools.skill import load_skill

        result = await load_skill.ainvoke(
            {"skill_name": "nonexistent", "arguments": ""}
        )

        # tool_error_handler 捕获 KeyError，返回错误消息
        assert "技能加载失败" in result


class TestSandboxBash:
    @pytest.mark.asyncio
    @patch("app.agents.tools.sandbox_bash._sandbox_client")
    async def test_sandbox_bash_executes(self, mock_client):
        mock_client.execute = AsyncMock(return_value="hello world")

        from app.agents.tools.sandbox_bash import sandbox_bash

        result = await sandbox_bash.ainvoke({"command": "echo hello world"})

        mock_client.execute.assert_called_once_with("echo hello world")
        assert "hello world" in result

    @pytest.mark.asyncio
    @patch("app.agents.tools.sandbox_bash._sandbox_client")
    async def test_sandbox_bash_error(self, mock_client):
        mock_client.execute = AsyncMock(side_effect=RuntimeError("sandbox offline"))

        from app.agents.tools.sandbox_bash import sandbox_bash

        result = await sandbox_bash.ainvoke({"command": "broken"})

        assert "沙箱执行失败" in result
