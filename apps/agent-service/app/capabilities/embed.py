"""EmbedderClient — thin adapter over ``embed_dense`` / ``embed_hybrid``.

Fixes ``model_id`` per instance so dataflow nodes can hold an embedder field
without repeating the model alias at every call site.
"""

from __future__ import annotations

from app.agent.embedding import HybridEmbedding, embed_dense, embed_hybrid


class EmbedderClient:
    """Adapter over ``app.agent.embedding`` — one model per instance."""

    def __init__(self, model_id: str) -> None:
        self._model_id = model_id

    async def dense(
        self,
        *,
        text: str | None = None,
        image_base64_list: list[str] | None = None,
        instructions: str = "",
    ) -> list[float]:
        return await embed_dense(
            self._model_id,
            text=text,
            image_base64_list=image_base64_list,
            instructions=instructions,
        )

    async def hybrid(
        self,
        *,
        text: str | None = None,
        image_base64_list: list[str] | None = None,
        instructions: str = "",
    ) -> HybridEmbedding:
        return await embed_hybrid(
            self._model_id,
            text=text,
            image_base64_list=image_base64_list,
            instructions=instructions,
        )
