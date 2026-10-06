"""world 的感知判断遇到模型的临时失败会整次重来；试完了还是失败时，每一次失败的调用在 Langfuse 里
都是一条单独的、标成 ERROR 的 trace，看得出这次判断没做成、为什么。

走真的那条路：:func:`app.world.perception.judge_who_notices` → :func:`app.world.agents.run_agent` →
``AgentRunner`` → ``Agent.run``，根 span 是真的，导出到内存（conftest 的 ``exported_spans``）。模型
换成一直返回 500 的替身，prompt 跳过按泳道取那一步，Dynamic Config 用默认值。
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from langfuse._client.attributes import LangfuseOtelSpanAttributes
from langfuse.api import Prompt_Text
from langfuse.model import TextPromptClient
from openai import InternalServerError

from app.agent.client import ModelClient
from app.agent.neutral import Message, ToolDef
from app.messaging.message import Kind, new_message
from app.world import perception

LEVEL = LangfuseOtelSpanAttributes.OBSERVATION_LEVEL
STATUS = LangfuseOtelSpanAttributes.OBSERVATION_STATUS_MESSAGE


def _server_error() -> InternalServerError:
    request = httpx.Request("POST", "https://model.invalid/v1/chat/completions")
    return InternalServerError(
        "Error code: 500 - The server had an error while processing your request.",
        response=httpx.Response(500, request=request),
        body=None,
    )


class _AlwaysFails(ModelClient):
    """每一次调用都返回 500 的模型。"""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(
        self, messages: list[Message], *, tools: list[ToolDef] | None = None, **_: Any
    ) -> Message:
        self.calls += 1
        raise _server_error()

    def stream(self, *_args: Any, **_kwargs: Any):  # pragma: no cover - not used
        raise NotImplementedError

    async def structured(self, *_args: Any, **_kwargs: Any):  # pragma: no cover - not used
        raise NotImplementedError


def _prompt(name: str) -> TextPromptClient:
    return TextPromptClient(
        Prompt_Text(
            name=name, version=1, prompt=f"你是 {name}。", config={}, labels=["coe-world"], tags=[]
        )
    )


async def test_an_exhausted_perception_retry_leaves_one_error_trace_per_attempt(
    exported_spans, monkeypatch
):
    from inner_shared.dynamic_config import dynamic_config

    model = _AlwaysFails()

    async def build_model_client(_model_id: str) -> ModelClient:
        return model

    monkeypatch.setattr("app.agent.core.build_model_client", build_model_client)
    monkeypatch.setattr("app.agent.core.get_prompt", _prompt)
    monkeypatch.setattr(dynamic_config, "get", lambda k, default="": default)
    monkeypatch.setattr(dynamic_config, "get_int", lambda k, default=0: default)
    monkeypatch.setattr(perception, "RETRY_BASE_SECONDS", 0.0)
    woke = new_message(sender="赤尾", recipient="world", body="我把窗关上了。", kind=Kind.MESSAGE)

    with pytest.raises(InternalServerError):
        await perception.judge_who_notices("窗关上之后，屋里的雨声小了。", round_messages=[woke])

    roots = [
        s for s in exported_spans.get_finished_spans() if s.name == perception.PERCEPTION.trace_name
    ]
    assert model.calls == len(roots) == perception.PERCEPTION_ATTEMPTS > 1
    assert len({s.context.trace_id for s in roots}) == len(roots), "每一次是一条单独的 trace"
    for root in roots:
        assert root.attributes[LEVEL] == "ERROR"
        assert root.attributes[STATUS].startswith("InternalServerError: Error code: 500")
