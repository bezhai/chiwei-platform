"""Tests for app.agent.prompts — compile_to_messages (neutral Message output)."""

from unittest.mock import MagicMock

import pytest
from langfuse.model import ChatPromptClient, TextPromptClient

from app.agent.neutral import Message, Role
from app.agent.prompts import compile_to_messages

pytestmark = pytest.mark.unit


class TestCompileToMessages:
    def test_text_prompt_returns_system_message(self):
        prompt = MagicMock(spec=TextPromptClient)
        prompt.compile.return_value = "You are a helpful assistant."

        result = compile_to_messages(prompt, name="test")

        assert len(result) == 1
        assert isinstance(result[0], Message)
        assert result[0].role == Role.SYSTEM
        assert result[0].content == "You are a helpful assistant."
        prompt.compile.assert_called_once_with(name="test")

    def test_chat_prompt_maps_roles(self):
        prompt = MagicMock(spec=ChatPromptClient)
        prompt.compile.return_value = [
            {"role": "system", "content": "You are a guard."},
            {"role": "user", "content": "Check: hello"},
        ]

        result = compile_to_messages(prompt, message="hello")

        assert len(result) == 2
        assert result[0].role == Role.SYSTEM
        assert result[1].role == Role.USER
        assert result[0].content == "You are a guard."
        assert result[1].content == "Check: hello"

    def test_chat_prompt_assistant_role(self):
        prompt = MagicMock(spec=ChatPromptClient)
        prompt.compile.return_value = [
            {"role": "system", "content": "sys"},
            {"role": "assistant", "content": "example response"},
            {"role": "user", "content": "now your turn"},
        ]

        result = compile_to_messages(prompt)

        assert len(result) == 3
        assert result[1].role == Role.ASSISTANT
        assert result[1].content == "example response"

    def test_chat_prompt_unknown_role_defaults_to_system(self):
        prompt = MagicMock(spec=ChatPromptClient)
        prompt.compile.return_value = [
            {"role": "unknown_role", "content": "some content"},
        ]

        result = compile_to_messages(prompt)

        assert len(result) == 1
        assert result[0].role == Role.SYSTEM

    def test_chat_prompt_missing_content_defaults_to_empty(self):
        prompt = MagicMock(spec=ChatPromptClient)
        prompt.compile.return_value = [
            {"role": "system"},
        ]

        result = compile_to_messages(prompt)

        assert result[0].content == ""
