"""LLMClient — thin adapter over ``build_model_client`` for dataflow nodes.

Lazy async init keeps construction sync so instances can be held as fields on
Node decorators (instantiated at module import time). First call amortises the
async model build. Internally it speaks neutral ``Message`` / ``StreamChunk`` to
the resolved ``ModelClient``; the outward contract stays plain ``str`` in / out.
"""

from collections.abc import AsyncIterator
from typing import Any

from app.agent.client import ModelClient, build_model_client
from app.agent.neutral import Message, Role


class LLMClient:
    def __init__(self, model_id: str):
        self._model_id = model_id
        self._model: ModelClient | None = None

    async def _get_model(self) -> ModelClient:
        if self._model is None:
            self._model = await build_model_client(self._model_id)
        return self._model

    async def complete(self, prompt: str, **kwargs: Any) -> str:
        model = await self._get_model()
        message = await model.complete(
            [Message(role=Role.USER, content=prompt)], **kwargs
        )
        return message.text()

    async def stream(self, prompt: str, **kwargs: Any) -> AsyncIterator[str]:
        model = await self._get_model()
        async for chunk in model.stream(
            [Message(role=Role.USER, content=prompt)], **kwargs
        ):
            if chunk.text:
                yield chunk.text
