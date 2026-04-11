"""Model building — resolve model_id to a LangChain BaseChatModel.

Responsibilities:
  - TTL cache for DB lookups (5 min, asyncio-safe without locks)
  - _ReasoningChatOpenAI subclass preserving DeepSeek reasoning_content
  - Dispatch to AzureChatOpenAI / ChatGoogleGenerativeAI / ChatOpenAI
    based on provider's ``client_type``
"""

from __future__ import annotations

import logging
import time
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_openai import AzureChatOpenAI, ChatOpenAI

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DeepSeek reasoning_content preservation
# ---------------------------------------------------------------------------


class _ReasoningChatOpenAI(ChatOpenAI):
    """ChatOpenAI subclass that preserves ``reasoning_content`` for DeepSeek.

    langchain-openai loses reasoning_content in two stages:
      1. ``_create_chat_result`` ignores it when parsing the raw response.
      2. ``_get_request_payload`` drops reasoning_content blocks when
         formatting messages for the next request.

    This subclass patches both stages.
    """

    def _create_chat_result(self, response: Any, generation_info: Any = None) -> Any:
        """Extract reasoning_content from raw response into additional_kwargs."""
        result = super()._create_chat_result(response, generation_info)

        response_dict = (
            response if isinstance(response, dict) else response.model_dump()
        )
        choices = response_dict.get("choices") or []
        for choice, gen in zip(choices, result.generations, strict=False):
            rc = choice.get("message", {}).get("reasoning_content")
            if rc is not None and isinstance(gen.message, AIMessage):
                gen.message.additional_kwargs["reasoning_content"] = rc

        return result

    @staticmethod
    def _normalize_content(content: Any) -> str:
        """Normalise content to a plain string (DeepSeek rejects null / arrays)."""
        if isinstance(content, list):
            text_parts: list[str] = []
            for block in content:
                if isinstance(block, str):
                    text_parts.append(block)
                elif isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
            return "".join(text_parts)
        if content is None:
            return ""
        return content

    def _get_request_payload(
        self, input_: Any, *, stop: Any = None, **kwargs: Any
    ) -> dict:
        """Inject reasoning_content and normalise content for DeepSeek."""
        messages = self._convert_input(input_).to_messages()
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)

        if "messages" not in payload:
            return payload

        # 1) assistant messages: inject reasoning_content
        for lc_msg, api_msg in zip(messages, payload["messages"], strict=False):
            if isinstance(lc_msg, AIMessage) and api_msg.get("role") == "assistant":
                rc = lc_msg.additional_kwargs.get("reasoning_content")
                if rc is not None:
                    api_msg["reasoning_content"] = rc

        # 2) all messages: normalise content to string
        for api_msg in payload["messages"]:
            api_msg["content"] = self._normalize_content(api_msg.get("content"))

        return payload


# ---------------------------------------------------------------------------
# TTL cache (asyncio single-threaded, no lock needed)
# ---------------------------------------------------------------------------

_CACHE_TTL_SECONDS: int = 300  # 5 minutes
_SENTINEL = object()

# { model_id: (value, expire_at) }
_model_info_cache: dict[str, tuple[Any, float]] = {}


def clear_model_info_cache() -> None:
    """Clear the model info cache (for tests and admin endpoints)."""
    _model_info_cache.clear()


# ---------------------------------------------------------------------------
# DB lookup (with TTL cache)
# ---------------------------------------------------------------------------


async def _get_model_and_provider_info(model_id: str) -> dict[str, Any] | None:
    """Resolve *model_id* to provider config via DB, with TTL cache.

    Lookup strategy:
      1. Try ModelMapping by alias.
      2. Fall back to parsing ``"provider:model"`` (default provider ``302.ai``).
      3. Query ModelProvider by name (fall back to ``302.ai`` if missing).

    Cache policy:
      - Hit and fresh -> return cached value.
      - Miss or stale -> query DB -> cache (including None, to prevent stampede).
      - DB exception -> do NOT cache (allow retry next call), return None.
    """
    now = time.monotonic()

    cached = _model_info_cache.get(model_id, _SENTINEL)
    if cached is not _SENTINEL:
        value, expire_at = cached  # type: ignore[misc]
        if now < expire_at:
            return value  # type: ignore[return-value]

    try:
        from app.data.queries import (
            find_model_mapping,
            find_provider_by_name,
            parse_model_id,
        )
        from app.data.session import get_session

        async with get_session() as session:
            mapping = await find_model_mapping(session, model_id)

            if mapping:
                provider_name = mapping.provider_name
                actual_model_name = mapping.real_model_name
            else:
                provider_name, actual_model_name = parse_model_id(model_id)

            provider = await find_provider_by_name(session, provider_name)

            if not provider:
                provider = await find_provider_by_name(session, "302.ai")

            if not provider:
                _model_info_cache[model_id] = (None, now + _CACHE_TTL_SECONDS)
                return None

            result: dict[str, Any] = {
                "model_name": actual_model_name,
                "api_key": provider.api_key,
                "base_url": provider.base_url,
                "is_active": provider.is_active,
                "client_type": provider.client_type or "openai",
                "use_proxy": provider.use_proxy,
            }
    except Exception as e:
        logger.error("DB lookup failed for model %s: %s", model_id, e)
        return None

    _model_info_cache[model_id] = (result, now + _CACHE_TTL_SECONDS)
    return result


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ModelBuildError(Exception):
    """Raised when model construction fails."""

    def __init__(self, model_id: str, detail: str):
        self.model_id = model_id
        super().__init__(f"[{model_id}] {detail}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def build_chat_model(
    model_id: str, *, max_retries: int = 3, **kwargs: Any
) -> BaseChatModel:
    """Build a LangChain ``BaseChatModel`` from a model_id.

    Dispatches to the correct LangChain class based on ``client_type``:
      - ``azure-http``        -> ``AzureChatOpenAI``
      - ``google``            -> ``ChatGoogleGenerativeAI``
      - ``openai-responses``  -> ``ChatOpenAI`` (Responses API)
      - ``deepseek``          -> ``_ReasoningChatOpenAI`` (preserves reasoning_content)
      - default               -> ``ChatOpenAI`` (Completions API)

    Raises:
        ModelBuildError: when model info is missing / inactive / incomplete.
    """
    max_retries = kwargs.pop("max_retries", max_retries)

    info = await _get_model_and_provider_info(model_id)
    if info is None:
        raise ModelBuildError(model_id, "model info not found")
    if not info.get("is_active", True):
        raise ModelBuildError(model_id, "model is disabled")

    required = ("api_key", "base_url", "model_name")
    missing = [f for f in required if not info.get(f)]
    if missing:
        raise ModelBuildError(model_id, f"missing fields: {', '.join(missing)}")

    client_type = info.get("client_type", "")

    if client_type == "azure-http":
        return AzureChatOpenAI(
            openai_api_type="azure",
            openai_api_version="2024-08-01-preview",
            azure_endpoint=info["base_url"],
            openai_api_key=info["api_key"],
            deployment_name=info["model_name"],
            max_retries=max_retries,
            **kwargs,
        )

    if client_type == "google":
        from langchain_google_genai import ChatGoogleGenerativeAI

        from app.infra.config import settings

        params: dict[str, Any] = {
            "api_key": info["api_key"],
            "base_url": info["base_url"],
            "model": info["model_name"],
            "max_retries": max_retries,
            **kwargs,
        }
        if info.get("use_proxy") and settings.forward_proxy_url:
            params["client_args"] = {"proxy": settings.forward_proxy_url}
        return ChatGoogleGenerativeAI(**params)

    if client_type == "openai-responses":
        params = {
            "api_key": info["api_key"],
            "base_url": info["base_url"],
            "model": info["model_name"],
            "max_retries": max_retries,
            "use_responses_api": True,
            **kwargs,
        }
        if info.get("use_proxy"):
            _inject_proxy(params)
        return ChatOpenAI(**params)

    if client_type == "deepseek":
        params = {
            "api_key": info["api_key"],
            "base_url": info["base_url"],
            "model": info["model_name"],
            "max_retries": max_retries,
            "use_responses_api": False,
            **kwargs,
        }
        if info.get("use_proxy"):
            _inject_proxy(params)
        return _ReasoningChatOpenAI(**params)

    # default: openai completions
    params = {
        "api_key": info["api_key"],
        "base_url": info["base_url"],
        "model": info["model_name"],
        "max_retries": max_retries,
        "use_responses_api": False,
        **kwargs,
    }
    if info.get("use_proxy"):
        _inject_proxy(params)
    return ChatOpenAI(**params)


def _inject_proxy(params: dict[str, Any]) -> None:
    """Add ``openai_proxy`` to *params* if the forward proxy is configured."""
    from app.infra.config import settings

    if settings.forward_proxy_url:
        params["openai_proxy"] = settings.forward_proxy_url
