"""Langfuse at its SDK boundary: prompts come from fixture files in ``tests/replay/prompts``.

``app.agent.prompts._fetch`` keeps its own label logic (lane label first, then production);
only the client underneath it is replaced. A fixture is ``<prompt id>.txt`` (a text prompt, one
SYSTEM message) or ``<prompt id>.json`` (a chat prompt: a list of ``{"role", "content"}``). Every
lookup returns a fresh prompt object that remembers the variables it was compiled with, so the
model boundary can record exactly which variables the code passed for that call.

A prompt id with no fixture fails the round loudly: add the fixture rather than letting the call
render from nothing.

A scenario can have Langfuse serve a text prompt that declares one variable more than its fixture
(:meth:`FixtureLangfuse.misconfigure`), as a release that added a variable no plugin gives would:
a configuration error for the round that fetches it, until :meth:`FixtureLangfuse.fix`.
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


def load_fixture(prompt_id: str, *, declares: str | None = None):
    """The fixture of ``prompt_id``; with ``declares``, a text fixture that also declares that
    variable (``{{declares}}`` on a line of its own at the end)."""
    text_path = PROMPTS_DIR / f"{prompt_id}.txt"
    chat_path = PROMPTS_DIR / f"{prompt_id}.json"
    if declares is not None and not text_path.exists():
        raise MissingPromptFixture(f"replay: {prompt_id!r} has no text fixture to add to")
    if text_path.exists():
        text = text_path.read_text(encoding="utf-8")
        if declares is not None:
            text += f"\n{{{{{declares}}}}}"
        return FixtureTextPrompt(
            Prompt_Text(
                name=prompt_id,
                version=FIXTURE_VERSION,
                prompt=text,
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

    def __init__(self) -> None:
        self._declares: dict[str, str] = {}

    def misconfigure(self, prompt_id: str, variable: str) -> None:
        """From now on ``prompt_id`` declares ``variable`` too, which no plugin gives."""
        self._declares[prompt_id] = variable

    def fix(self, prompt_id: str) -> None:
        """``prompt_id`` is its fixture again."""
        del self._declares[prompt_id]

    def get_prompt(self, prompt_id: str, *, label: str | None = None, **_: Any):
        return load_fixture(prompt_id, declares=self._declares.get(prompt_id))


def config_errors(prompt_id: str, label: str, participant: str) -> float:
    """How many configuration errors ``prompt_config_errors_total`` has counted so far for
    ``prompt_id`` served under ``label`` to ``participant`` (the counter lives as long as the test
    process, so a scenario compares before and after)."""
    from prometheus_client import REGISTRY

    sample = REGISTRY.get_sample_value(
        "prompt_config_errors_total",
        {"prompt": prompt_id, "label": label, "participant": participant},
    )
    return sample or 0.0
