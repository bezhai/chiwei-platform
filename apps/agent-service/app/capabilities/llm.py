"""LLMClient — thin adapter over ``build_chat_model`` for dataflow nodes.

Lazy async init keeps construction sync so instances can be held as fields on
Node decorators (instantiated at module import time). First call amortises the
async model build.
"""

from typing import Any, AsyncIterator

from langchain_core.language_models import BaseChatModel

from app.agent.models import build_chat_model


class LLMClient:
    def __init__(self, model_id: str):
        self._model_id = model_id
        self._model: BaseChatModel | None = None

    async def _get_model(self) -> BaseChatModel:
        if self._model is None:
            self._model = await build_chat_model(self._model_id)
        return self._model

    async def complete(self, prompt: str, **kwargs: Any) -> str:
        model = await self._get_model()
        r = await model.ainvoke(prompt, **kwargs)
        content = r.content
        if not isinstance(content, str):
            raise TypeError(
                f"LLMClient.complete expected str content, got {type(content).__name__}; "
                f"multimodal responses are not supported in the MVP adapter"
            )
        return content

    async def stream(self, prompt: str, **kwargs: Any) -> AsyncIterator[str]:
        model = await self._get_model()
        async for chunk in model.astream(prompt, **kwargs):
            content = chunk.content
            if not isinstance(content, str):
                raise TypeError(
                    f"LLMClient.stream expected str content per chunk, got {type(content).__name__}"
                )
            yield content
