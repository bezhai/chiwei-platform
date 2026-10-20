"""AgentRunner — thin adapter over ``app.agent.core.Agent``.

Holds a preconfigured ``Agent`` so a caller can declare an agent once and call
``run`` / ``stream`` / ``extract`` per event.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from typing import Any

from pydantic import BaseModel

from app.agent.contracts import AgentSpec
from app.agent.core import Agent
from app.agent.tooling import Tool


class AgentRunner:
    """Adapter over ``app.agent.core.Agent`` — one preconfigured agent per instance."""

    def __init__(self, spec: AgentSpec, *, tools: Sequence[Tool] = ()) -> None:
        self._agent = Agent(spec, tools=tools)

    async def run(self, messages: list[Any], **kwargs: Any) -> Any:
        return await self._agent.run(messages, **kwargs)

    async def stream(
        self, messages: list[Any], **kwargs: Any
    ) -> AsyncGenerator[Any, None]:
        async for chunk in self._agent.stream(messages, **kwargs):
            yield chunk

    async def extract(
        self,
        response_model: type[BaseModel],
        messages: list[Any],
        **kwargs: Any,
    ) -> BaseModel:
        return await self._agent.extract(response_model, messages, **kwargs)
