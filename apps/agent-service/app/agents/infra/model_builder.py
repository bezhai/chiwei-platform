"""ModelBuilder - 统一的模型构建器

直接基于数据库操作，为langgraph提供统一的BaseChatModel实例构建功能
"""

import logging
import time
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_openai import AzureChatOpenAI, ChatOpenAI

from .exceptions import ModelBuilderError, ModelConfigError, UnsupportedModelError

logger = logging.getLogger(__name__)


class _ReasoningChatOpenAI(ChatOpenAI):
    """ChatOpenAI 子类，保留 reasoning_content 供 DeepSeek 等推理模型使用。

    langchain-openai 在两个阶段丢失 reasoning_content：
    1. _convert_dict_to_message 解析响应时不提取 reasoning_content
    2. _format_message_content 构建请求时丢弃 reasoning_content block

    此子类通过重写 _create_chat_result 和 _get_request_payload 修复这两个环节。
    """

    def _create_chat_result(self, response, generation_info=None):
        """从原始响应中提取 reasoning_content 存入 additional_kwargs。"""
        import openai

        result = super()._create_chat_result(response, generation_info)

        response_dict = (
            response if isinstance(response, dict) else response.model_dump()
        )
        choices = response_dict.get("choices") or []
        for choice, gen in zip(choices, result.generations):
            rc = choice.get("message", {}).get("reasoning_content")
            if rc is not None and isinstance(gen.message, AIMessage):
                gen.message.additional_kwargs["reasoning_content"] = rc

        return result

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        """将 additional_kwargs 中的 reasoning_content 注入回 message dict。

        同时将 assistant 消息的 content 归一化为字符串——
        AIMessage.content 属性会自动注入 reasoning block 导致 content 变成数组，
        DeepSeek API 只接受字符串。
        """
        messages = self._convert_input(input_).to_messages()
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)

        if "messages" not in payload:
            return payload

        for lc_msg, api_msg in zip(messages, payload["messages"]):
            if (
                isinstance(lc_msg, AIMessage)
                and api_msg.get("role") == "assistant"
            ):
                # 注入 reasoning_content 为顶层字段
                rc = lc_msg.additional_kwargs.get("reasoning_content")
                if rc is not None:
                    api_msg["reasoning_content"] = rc

                # content 归一化：数组 → 字符串（提取纯文本，丢弃 reasoning block）
                content = api_msg.get("content")
                if isinstance(content, list):
                    text_parts = []
                    for block in content:
                        if isinstance(block, str):
                            text_parts.append(block)
                        elif isinstance(block, dict) and block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                    api_msg["content"] = "".join(text_parts) or None

        return payload

# ---------------------------------------------------------------------------
# 模块级 TTL 缓存（asyncio 单线程安全，无需锁）
# ---------------------------------------------------------------------------
_CACHE_TTL_SECONDS: int = 300  # 5 分钟
_SENTINEL = object()  # 区分"未缓存"和"缓存了 None"

# { model_id: (value, expire_at) }
_model_info_cache: dict[str, tuple[Any, float]] = {}


def clear_model_info_cache() -> None:
    """清空 model_info 缓存（供测试和 admin 接口使用）"""
    _model_info_cache.clear()


class ModelBuilder:
    """
    模型构建器

    提供统一的接口来构建langgraph可用的BaseChatModel实例
    当前统一映射到ChatOpenAI，后期可扩展支持其他模型类型
    """

    @staticmethod
    async def _get_model_and_provider_info(model_id: str) -> dict[str, Any] | None:
        """
        从数据库获取供应商信息（带 TTL 缓存）

        解析model_id格式："{供应商名称}:模型原名"
        如果找不到供应商名称，则使用默认的302.ai

        缓存策略：
        - 命中且未过期 → 直接返回
        - 未命中或已过期 → 查 DB → 写入缓存
        - DB 异常 → 不缓存（允许下次重试），返回 None
        - DB 正常返回 None → 缓存（防穿透）

        Args:
            model_id: 格式为"供应商名称/模型原名"的字符串

        Returns:
            Dict: 包含模型和供应商信息的字典，如果未找到返回None
        """
        now = time.monotonic()

        # 查缓存
        cached = _model_info_cache.get(model_id, _SENTINEL)
        if cached is not _SENTINEL:
            value, expire_at = cached
            if now < expire_at:
                return value

        # 未命中或已过期 → 查 DB
        try:
            from app.orm.crud import get_model_and_provider_info

            result = await get_model_and_provider_info(model_id)
        except Exception as e:
            logger.error(f"数据库查询错误: {e}")
            # DB 异常不缓存，允许下次重试
            return None

        # 写入缓存（包括 None 结果，防穿透）
        _model_info_cache[model_id] = (result, now + _CACHE_TTL_SECONDS)
        return result

    @staticmethod
    async def get_basic_model_params(model_id: str) -> dict[str, Any] | None:
        """
        获取基础模型参数

        Args:
            model_id: 内部模型ID，对应数据库中的model_id

        Returns:
            Dict: 包含基础模型参数的字典，如果未找到返回None
        """
        model_info = await ModelBuilder._get_model_and_provider_info(model_id)
        if model_info is None or not model_info.get("is_active", True):
            return None

        required_fields = ["api_key", "base_url", "model_name"]
        if any(
            field not in model_info or not model_info[field]
            for field in required_fields
        ):
            return None

        return {
            "api_key": model_info["api_key"],
            "base_url": model_info["base_url"],
            "model": model_info["model_name"],
            "client_type": model_info["client_type"],
        }

    @staticmethod
    async def build_chat_model(
        model_id: str, *, max_retries: int = 3, **kwargs
    ) -> BaseChatModel:
        """
        根据model_id构建BaseChatModel实例

        Args:
            model_id: 内部模型ID，对应数据库中的model_id
            max_retries: SDK 层面的自动重试次数（针对瞬时网络错误），默认 3

        Returns:
            BaseChatModel实例，可直接用于langgraph

        Raises:
            ModelConfigError: 模型配置错误
            UnsupportedModelError: 不支持的模型类型
            ModelBuilderError: 其他构建错误
        """
        # 允许 kwargs 覆盖 max_retries
        max_retries = kwargs.pop("max_retries", max_retries)

        try:
            # 从数据库获取模型信息
            model_info = await ModelBuilder._get_model_and_provider_info(model_id)

            if model_info is None:
                raise UnsupportedModelError(model_id, f"未找到模型信息: {model_id}")

            # 检查模型是否激活
            if not model_info.get("is_active", True):
                raise UnsupportedModelError(model_id, f"模型已禁用: {model_id}")

            # 验证必要字段
            required_fields = ["api_key", "base_url", "model_name"]
            missing_fields = [
                field for field in required_fields if not model_info.get(field)
            ]
            if missing_fields:
                raise ModelConfigError(
                    model_id, f"模型配置缺少必要字段: {', '.join(missing_fields)}"
                )

            # 根据 client_type 选择不同的模型类
            client_type = model_info.get("client_type", "")

            if client_type == "azure-http":
                # 使用 AzureChatOpenAI
                chat_params = {
                    "openai_api_type": "azure",
                    "openai_api_version": "2024-08-01-preview",
                    "azure_endpoint": model_info["base_url"],
                    "openai_api_key": model_info["api_key"],
                    "deployment_name": model_info["model_name"],
                    "max_retries": max_retries,
                    **kwargs,
                }

                logger.info(
                    f"为模型 {model_id} 构建AzureChatOpenAI实例，"
                    f"参数: {list(chat_params.keys())}"
                )

                return AzureChatOpenAI(**chat_params)
            elif client_type == "google":
                from langchain_google_genai import ChatGoogleGenerativeAI

                from app.config.config import settings

                chat_params = {
                    "api_key": model_info["api_key"],
                    "base_url": model_info["base_url"],
                    "model": model_info["model_name"],
                    "max_retries": max_retries,
                    **kwargs,
                }
                if settings.forward_proxy_url:
                    chat_params["client_args"] = {
                        "proxy": settings.forward_proxy_url
                    }

                logger.info(
                    f"为模型 {model_id} 构建ChatGoogleGenerativeAI实例，"
                    f"参数: {list(chat_params.keys())}"
                )

                return ChatGoogleGenerativeAI(**chat_params)
            elif client_type == "openai-responses":
                chat_params = {
                    "api_key": model_info["api_key"],
                    "base_url": model_info["base_url"],
                    "model": model_info["model_name"],
                    "max_retries": max_retries,
                    "use_responses_api": True,
                    **kwargs,
                }
                logger.info(
                    f"为模型 {model_id} 构建ChatOpenAI（Responses API）"
                )
                return ChatOpenAI(**chat_params)
            elif client_type == "deepseek":
                chat_params = {
                    "api_key": model_info["api_key"],
                    "base_url": model_info["base_url"],
                    "model": model_info["model_name"],
                    "max_retries": max_retries,
                    **kwargs,
                }
                logger.info(
                    f"为模型 {model_id} 构建DeepSeek ChatOpenAI（Completions API）"
                )
                return _ReasoningChatOpenAI(**chat_params)
            else:
                # openai 及其他: 标准 Chat Completions API
                chat_params = {
                    "api_key": model_info["api_key"],
                    "base_url": model_info["base_url"],
                    "model": model_info["model_name"],
                    "max_retries": max_retries,
                    **kwargs,
                }
                logger.info(
                    f"为模型 {model_id} 构建ChatOpenAI（Completions API）"
                )
                return ChatOpenAI(**chat_params)

        except Exception as e:
            if isinstance(e, ModelBuilderError):
                raise

            # 其他未知异常
            logger.error(f"构建模型 {model_id} 时发生未知错误: {e}")
            raise ModelBuilderError(f"构建模型失败: {str(e)}") from e
