"""Tests for skill renderer (preprocessing + variable substitution)"""

import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.skills.loader import PreprocessDirective, SkillDefinition
from app.skills.renderer import render_skill


def _make_skill(
    raw_body: str,
    preprocessing: tuple[PreprocessDirective, ...] = (),
    name: str = "test_skill",
) -> SkillDefinition:
    return SkillDefinition(
        name=name,
        description="test",
        raw_body=raw_body,
        preprocessing=preprocessing,
        base_path=Path("/fake/skills/test_skill"),
    )


class TestRenderSkill:
    @pytest.mark.asyncio
    async def test_no_preprocessing_no_variables(self):
        skill = _make_skill("Just some instructions.")
        result = await render_skill(skill, "")
        assert result == "Just some instructions."

    @pytest.mark.asyncio
    async def test_arguments_substitution(self):
        skill = _make_skill("Query: $ARGUMENTS\nDo it.")
        result = await render_skill(skill, "SELECT * FROM users")
        assert "SELECT * FROM users" in result
        assert "$ARGUMENTS" not in result

    @pytest.mark.asyncio
    async def test_skill_dir_substitution(self):
        skill = _make_skill("Script at $SKILL_DIR/scripts/run.py")
        result = await render_skill(skill, "")
        assert "/fake/skills/test_skill/scripts/run.py" in result
        assert "$SKILL_DIR" not in result

    @pytest.mark.asyncio
    @patch("app.skills.renderer.sandbox_client")
    async def test_preprocessing_executed(self, mock_sandbox):
        mock_sandbox.execute = AsyncMock(return_value="Linux 5.15 x86_64")

        skill = _make_skill(
            raw_body='## Info\n\n```\n!`uname -a`\n```\n\n## Instructions\n\nShow info.',
            preprocessing=(PreprocessDirective(command="uname -a", label="Info"),),
        )

        result = await render_skill(skill, "")

        mock_sandbox.execute.assert_called_once_with("uname -a", "test_skill")
        assert "Linux 5.15 x86_64" in result
        assert "!`uname -a`" not in result

    @pytest.mark.asyncio
    @patch("app.skills.renderer.sandbox_client")
    async def test_preprocessing_with_arguments(self, mock_sandbox):
        mock_sandbox.execute = AsyncMock(return_value="hello world")

        skill = _make_skill(
            raw_body='```\n!`echo "$ARGUMENTS"`\n```\n\nDone.',
            preprocessing=(
                PreprocessDirective(command='echo "$ARGUMENTS"', label=""),
            ),
        )

        result = await render_skill(skill, "hello world")

        # 预处理命令中的 $ARGUMENTS 应被替换
        mock_sandbox.execute.assert_called_once_with(
            'echo "hello world"', "test_skill"
        )
        assert "hello world" in result

    @pytest.mark.asyncio
    @patch("app.skills.renderer.sandbox_client")
    async def test_preprocessing_failure_graceful(self, mock_sandbox):
        mock_sandbox.execute = AsyncMock(side_effect=RuntimeError("sandbox down"))

        skill = _make_skill(
            raw_body="```\n!`broken_cmd`\n```\n\nInstructions.",
            preprocessing=(PreprocessDirective(command="broken_cmd", label=""),),
        )

        result = await render_skill(skill, "")

        assert "预处理失败" in result
        assert "sandbox down" in result
        assert "Instructions." in result

    @pytest.mark.asyncio
    @patch("app.skills.renderer.sandbox_client")
    async def test_multiple_preprocessing(self, mock_sandbox):
        mock_sandbox.execute = AsyncMock(
            side_effect=["table_schema", "query_result"]
        )

        skill = _make_skill(
            raw_body='```\n!`get_schema`\n```\n\n```\n!`run_query`\n```\n\nAnalyze.',
            preprocessing=(
                PreprocessDirective(command="get_schema", label="Schema"),
                PreprocessDirective(command="run_query", label="Query"),
            ),
        )

        result = await render_skill(skill, "")

        assert mock_sandbox.execute.call_count == 2
        assert "table_schema" in result
        assert "query_result" in result
        assert "Analyze." in result
