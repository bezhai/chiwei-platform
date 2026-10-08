"""Langfuse at its SDK boundary: prompts come from fixture files in ``tests/replay/prompts``.

``app.agent.prompts.get_prompt`` keeps its own label logic (lane label first, then production);
only the client underneath it is replaced. A fixture is ``<prompt id>.txt`` (a text prompt, one
SYSTEM message) or ``<prompt id>.json`` (a chat prompt: a list of ``{"role", "content"}``). Every
lookup returns a fresh prompt object that remembers the variables it was compiled with, so the
model boundary can record exactly which variables the code passed for that call.

A prompt id with no fixture fails the round loudly: add the fixture rather than letting the call
render from nothing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langfuse.api import Prompt_Chat, Prompt_Text
from langfuse.model import ChatPromptClient, TextPromptClient

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# The version every fixture reports. The baseline pins one version combination (spec decision 5).
FIXTURE_VERSION = 1


class _Remembers:
    replay_variables: dict[str, Any] | None = None

    def compile(self, **variables: Any):
        self.replay_variables = dict(variables)
        return super().compile(**variables)


class FixtureTextPrompt(_Remembers, TextPromptClient):
    pass


class FixtureChatPrompt(_Remembers, ChatPromptClient):
    pass


class MissingPromptFixture(LookupError):
    pass


def load_fixture(prompt_id: str):
    text_path = PROMPTS_DIR / f"{prompt_id}.txt"
    chat_path = PROMPTS_DIR / f"{prompt_id}.json"
    if text_path.exists():
        return FixtureTextPrompt(
            Prompt_Text(
                name=prompt_id,
                version=FIXTURE_VERSION,
                prompt=text_path.read_text(encoding="utf-8"),
                config={},
                labels=["production"],
                tags=[],
                type="text",
            )
        )
    if chat_path.exists():
        return FixtureChatPrompt(
            Prompt_Chat(
                name=prompt_id,
                version=FIXTURE_VERSION,
                prompt=json.loads(chat_path.read_text(encoding="utf-8")),
                config={},
                labels=["production"],
                tags=[],
                type="chat",
            )
        )
    raise MissingPromptFixture(
        f"replay: no prompt fixture for {prompt_id!r}; add {text_path} (or {chat_path.name})"
    )


class FixtureLangfuse:
    """Stands in for the Langfuse client that ``app.agent.prompts`` talks to."""

    def get_prompt(self, prompt_id: str, *, label: str | None = None, **_: Any):
        return load_fixture(prompt_id)
